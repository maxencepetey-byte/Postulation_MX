import base64
import logging
import os
from email.message import EmailMessage

import requests

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import redirect
from django.utils.timezone import now
from django.views.decorators.http import require_GET, require_POST

from ..models import DocumentUtilisateur, EntrepriseCible, LettreSecteurTemplate, ProfilUtilisateur
from ._utils import _email_to_pdf_name, _read_filefield_bytes, _run_in_background, _safe_format, get_accroche
from ._pdf import generer_pdf_lm
from .auth import _gmail_get_access_token

logger = logging.getLogger(__name__)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("utf-8").rstrip("=")


def _build_mime_message(to_email: str, subject: str, body: str, attachments: list[tuple[str, bytes, str]]) -> bytes:
    msg = EmailMessage()
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.set_content(body or "")
    for filename, content, mime in attachments:
        if not content:
            continue
        maintype, subtype = (mime.split("/", 1) + ["octet-stream"])[:2]
        msg.add_attachment(content, maintype=maintype, subtype=subtype, filename=filename)
    return msg.as_bytes()


def _gmail_create_draft(access_token: str, raw_mime_bytes: bytes) -> None:
    url = "https://gmail.googleapis.com/gmail/v1/users/me/drafts"
    r = requests.post(
        url,
        json={"message": {"raw": _b64url(raw_mime_bytes)}},
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=30,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"Gmail API error {r.status_code}: {r.text[:300]}")


@login_required
@require_POST
def creer_brouillons_gmail(request):
    try:
        access_token = _gmail_get_access_token(request.user)
    except Exception:
        messages.error(request, "Connexion Gmail invalide ou expirée. Merci de reconnecter Gmail dans Réglages.")
        return redirect("settings_page")

    secteur = (request.POST.get("secteur") or "").strip()
    pack_num_raw = (request.POST.get("pack_num") or "").strip()
    pack_num = int(pack_num_raw) if pack_num_raw.isdigit() else None

    qs_ent = EntrepriseCible.objects.filter(utilisateur=request.user, est_dans_paquet=False).exclude(email="")
    if secteur:
        qs_ent = qs_ent.filter(secteur_activite=secteur)
    if pack_num:
        qs_ent = qs_ent.filter(numero_pack=pack_num)

    if not qs_ent.exists():
        messages.info(request, "Aucune entreprise à traiter (toutes déjà traitées ou sans email).")
        return redirect("dashboard")

    cv_doc = DocumentUtilisateur.objects.filter(utilisateur=request.user, type_doc="CV").order_by("-date_upload").first()
    if not cv_doc:
        messages.error(request, "Aucun CV trouvé. Ajoute un document de type CV dans Réglages avant de créer des brouillons.")
        return redirect("settings_page")

    def _run_brouillons(user_id, secteur, access_token, pack_num=None):
        from django.contrib.auth.models import User
        try:
            user = User.objects.get(id=user_id)
            profil, _ = ProfilUtilisateur.objects.get_or_create(user=user)

            qs = EntrepriseCible.objects.filter(utilisateur=user, est_dans_paquet=False).exclude(email="")
            if secteur:
                qs = qs.filter(secteur_activite=secteur)
            if pack_num:
                qs = qs.filter(numero_pack=pack_num)
            entreprises = list(qs.order_by("id")[:500])
            logger.info("brouillons_bg: %d entreprises à traiter (user %s)", len(entreprises), user_id)

            cv_doc = DocumentUtilisateur.objects.filter(utilisateur=user, type_doc="CV").order_by("-date_upload").first()
            if not cv_doc:
                logger.error("brouillons_bg: CV introuvable pour user %s", user_id)
                return

            from django.db.models import Q
            other_docs_qs = (
                DocumentUtilisateur.objects.filter(utilisateur=user)
                .exclude(type_doc="PACK_LM")
                .exclude(id=cv_doc.id)
            )
            if secteur:
                other_docs_qs = other_docs_qs.filter(
                    Q(secteur_nom="") | Q(secteur_nom__isnull=True) | Q(secteur_nom=secteur)
                )
            other_docs = list(other_docs_qs.order_by("-date_upload"))

            try:
                cv_bytes = _read_filefield_bytes(cv_doc.fichier)
            except OSError as e:
                logger.error("brouillons_bg: impossible de lire le CV: %s", e)
                return

            other_attachments: list[tuple[str, bytes, str]] = []
            for d in other_docs:
                try:
                    other_attachments.append((
                        os.path.basename(d.fichier.name),
                        _read_filefield_bytes(d.fichier),
                        "application/pdf",
                    ))
                except OSError:
                    continue

            tpl_email = LettreSecteurTemplate.objects.filter(utilisateur=user, secteur_nom="Email").first()

            created = 0
            skipped = 0
            to_update: list[EntrepriseCible] = []
            now_dt = now()

            for ent in entreprises:
                secteur_nom = (ent.secteur_activite or "").strip()
                accroche = get_accroche(profil, ent.secteur_activite)
                ctx = {
                    "accroche": accroche,
                    "entreprise": ent.nom,
                    "secteur": secteur_nom,
                    "ville": profil.ville or "Genève",
                    "prenom": profil.prenom_lm or "",
                    "nom": profil.nom_lm or "",
                }

                base_subject = _safe_format(
                    (tpl_email.objet if tpl_email else "") or "Candidature spontanée", ctx
                ).strip()
                subject = f"{base_subject} — {ent.nom}".strip()

                if tpl_email and (tpl_email.salutation or tpl_email.paragraph_1 or tpl_email.paragraph_2 or tpl_email.paragraph_3 or tpl_email.paragraph_4 or tpl_email.conclusion):
                    intro = _safe_format(tpl_email.salutation or "Madame, Monsieur,", ctx).strip()
                    paras = [_safe_format(getattr(tpl_email, f"paragraph_{i}"), ctx).strip() for i in range(1, 5)]
                    closing = _safe_format(
                        tpl_email.conclusion or "Je vous prie d'agréer, Madame, Monsieur, l'expression de mes salutations distinguées.",
                        ctx,
                    ).strip()
                    signature = f"{profil.prenom_lm or ''} {profil.nom_lm or ''}".strip()
                    body = "\n\n".join([p for p in [intro, *paras, closing, signature] if p])
                else:
                    body = (
                        "Madame, Monsieur,\n\n"
                        f"C'est avec un vif intérêt que je me permets de vous adresser ma candidature. "
                        f"En effet, je suis particulièrement attiré par {accroche}.\n\n"
                        "Souhaitant intégrer une structure dynamique telle que la vôtre, je suis convaincu "
                        "que mon expérience et ma motivation sauront répondre à vos exigences.\n\n"
                        "Vous trouverez ci-joint mon dossier complet. Je reste à votre entière disposition "
                        "pour un entretien.\n\n"
                        "Je vous prie d'agréer, Madame, Monsieur, l'expression de mes salutations distinguées.\n\n"
                        f"{profil.prenom_lm or ''} {profil.nom_lm or ''}"
                    )

                try:
                    lm_pdf = generer_pdf_lm(profil, ent)
                    lm_name = _email_to_pdf_name(ent.email)
                except Exception as e:
                    logger.warning("brouillons_bg: PDF failed '%s': %s", ent.email, e)
                    skipped += 1
                    continue

                attachments = [
                    (lm_name, lm_pdf, "application/pdf"),
                    (os.path.basename(cv_doc.fichier.name), cv_bytes, "application/pdf"),
                    *other_attachments,
                ]
                raw = _build_mime_message(ent.email, subject, body, attachments)

                try:
                    _gmail_create_draft(access_token, raw)
                    ent.est_dans_paquet = True
                    ent.brouillon_gmail_cree = True
                    ent.date_traitement = now_dt
                    to_update.append(ent)
                    created += 1
                except RuntimeError as e:
                    err_str = str(e)
                    if "401" in err_str or "403" in err_str or "invalid_grant" in err_str.lower():
                        logger.error("brouillons_bg: auth error, stopping. %s", err_str[:200])
                        break
                    logger.warning("brouillons_bg: draft failed '%s': %s", ent.email, err_str[:200])
                    skipped += 1

            if to_update:
                with transaction.atomic():
                    EntrepriseCible.objects.bulk_update(
                        to_update,
                        ["est_dans_paquet", "brouillon_gmail_cree", "date_traitement"],
                    )

            logger.info("brouillons_bg: terminé — %d créés, %d ignorés (user %s)", created, skipped, user_id)

        except Exception:
            logger.exception("brouillons_bg: exception non gérée (user %s)", user_id)

    _run_in_background(_run_brouillons, request.user.id, secteur, access_token, pack_num)

    nb = qs_ent.count()
    messages.success(
        request,
        f"⏳ Création de {nb} brouillon(s) lancée en arrière-plan. "
        f"Rafraîchis le dashboard dans quelques minutes pour voir la progression."
    )
    return redirect("dashboard")


@login_required
@require_GET
def gmail_progress(request):
    secteur = (request.GET.get("secteur") or "").strip()

    qs_all = EntrepriseCible.objects.filter(utilisateur=request.user).exclude(email="")
    qs_done = qs_all.filter(est_dans_paquet=True)

    if secteur:
        qs_all = qs_all.filter(secteur_activite=secteur)
        qs_done = qs_done.filter(secteur_activite=secteur)

    total = qs_all.count()
    done = qs_done.count()

    return JsonResponse({
        "total": total,
        "done": done,
        "remaining": total - done,
        "percent": round((done / total * 100) if total > 0 else 0),
    })
