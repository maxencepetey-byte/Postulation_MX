"""Création de brouillons Gmail via OAuth + suivi de progression côté client."""

import base64
import io
import logging
import os
import zipfile
from email.message import EmailMessage
from urllib.parse import urlencode

import requests

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import redirect
from django.utils.timezone import now
from django.views.decorators.http import require_GET, require_POST

from ..models import Candidature, DocumentUtilisateur, GmailOAuthToken, LettreSecteurTemplate, ProfilUtilisateur
from ._utils import _lm_pdf_name, _read_filefield_bytes, _run_in_background, _safe_format, get_accroche
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

    qs_ent = Candidature.objects.filter(utilisateur=request.user, est_dans_paquet=False)
    if secteur:
        qs_ent = qs_ent.filter(secteur_activite=secteur)
    if pack_num:
        qs_ent = qs_ent.filter(numero_pack=pack_num)

    redirect_dashboard = redirect(f"/?{urlencode({'secteur': secteur})}" if secteur else "dashboard")

    if not qs_ent.exists():
        messages.info(request, "Aucune entreprise à traiter (toutes déjà traitées ou sans email).")
        return redirect_dashboard

    cv_doc = DocumentUtilisateur.objects.filter(utilisateur=request.user, type_doc="CV").order_by("-date_upload").first()
    if not cv_doc:
        messages.error(request, "Aucun CV trouvé. Ajoute un document de type CV dans Réglages avant de créer des brouillons.")
        return redirect("settings_page")

    # Valider le token Gmail AVANT de lancer le job de fond
    try:
        probe = requests.get(
            "https://gmail.googleapis.com/gmail/v1/users/me/profile",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        if probe.status_code >= 400:
            messages.error(request, f"Connexion Gmail rejetée (code {probe.status_code}). Reconnecte ton compte Gmail dans les Réglages.")
            return redirect("settings_page")
    except requests.RequestException as e:
        messages.error(request, f"Impossible de joindre Gmail : {e}. Vérifie ta connexion réseau.")
        return redirect("settings_page")

    def _run_brouillons(user_id, secteur, pack_num=None):
        from django.contrib.auth.models import User
        try:
            user = User.objects.get(id=user_id)
            profil, _ = ProfilUtilisateur.objects.get_or_create(utilisateur=user)

            # Fix 2 : token récupéré ici, pas passé en paramètre (évite le snapshot périmé)
            try:
                access_token = _gmail_get_access_token(user)
            except Exception as e:
                logger.error("brouillons_bg: token Gmail inaccessible (user %s): %s", user_id, e)
                GmailOAuthToken.objects.filter(utilisateur=user).update(expires_at=None)
                return

            qs = Candidature.objects.filter(utilisateur=user, est_dans_paquet=False).select_related('entreprise')
            if secteur:
                qs = qs.filter(secteur_activite=secteur)
            if pack_num:
                qs = qs.filter(numero_pack=pack_num)
            candidatures = list(qs.order_by("id")[:500])
            logger.info("brouillons_bg: %d candidatures à traiter (user %s)", len(candidatures), user_id)

            cv_doc = DocumentUtilisateur.objects.filter(utilisateur=user, type_doc="CV").order_by("-date_upload").first()
            if not cv_doc:
                logger.error("brouillons_bg: CV introuvable pour user %s", user_id)
                return

            other_docs = list(
                DocumentUtilisateur.objects.filter(utilisateur=user)
                .exclude(type_doc="PACK_LM")
                .exclude(id=cv_doc.id)
                .order_by("-date_upload")
            )

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

            templates_map = {
                t.secteur_nom: t
                for t in LettreSecteurTemplate.objects.filter(utilisateur=user)
            }
            tpl_email_fallback = templates_map.get("Email")

            # Réutilise les LM déjà générées dans le pack ZIP correspondant (évite un rendu PDF en double)
            pack_lm_cache: dict[str, bytes] = {}
            if pack_num:
                secteur_clean = (secteur or "").replace(" ", "_").replace("/", "-")
                pack_doc = DocumentUtilisateur.objects.filter(
                    utilisateur=user,
                    type_doc="PACK_LM",
                    nom_affichage=f"MX_SCAN_{secteur_clean}_PACK_{pack_num}",
                ).first()
                if pack_doc:
                    try:
                        zip_bytes = _read_filefield_bytes(pack_doc.fichier)
                        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
                            pack_lm_cache = {name: zf.read(name) for name in zf.namelist()}
                    except (OSError, zipfile.BadZipFile) as e:
                        logger.warning("brouillons_bg: lecture pack LM échouée, régénération complète (user %s): %s", user_id, e)

            created = 0
            skipped = 0
            to_update: list[Candidature] = []
            now_dt = now()

            for cand in candidatures:
                secteur_nom = (cand.secteur_activite or "").strip()
                tpl_email = templates_map.get(secteur_nom) or tpl_email_fallback
                accroche = get_accroche(cand.secteur_activite)
                ctx = {
                    "accroche": accroche,
                    "entreprise": cand.entreprise.raison_sociale,
                    "secteur": secteur_nom,
                    "ville": profil.ville or "Genève",
                    "prenom": profil.prenom_lm or "",
                    "nom": profil.nom_lm or "",
                }

                base_subject = _safe_format(
                    (tpl_email.objet if tpl_email else "") or "Candidature spontanée", ctx
                ).strip()
                subject = f"{base_subject} — {cand.entreprise.raison_sociale}".strip()

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

                lm_name = _lm_pdf_name(cand.entreprise.raison_sociale)
                lm_pdf = pack_lm_cache.get(lm_name)
                if lm_pdf is None:
                    try:
                        lm_pdf = generer_pdf_lm(profil, cand)
                    except Exception as e:
                        logger.warning("brouillons_bg: PDF failed '%s': %s", cand.entreprise.email, e)
                        skipped += 1
                        continue

                attachments = [
                    (lm_name, lm_pdf, "application/pdf"),
                    (os.path.basename(cv_doc.fichier.name), cv_bytes, "application/pdf"),
                    *other_attachments,
                ]
                raw = _build_mime_message(cand.entreprise.email, subject, body, attachments)

                try:
                    _gmail_create_draft(access_token, raw)
                    cand.est_dans_paquet = True
                    cand.brouillon_gmail_cree = True
                    cand.date_traitement = now_dt
                    to_update.append(cand)
                    created += 1
                except (RuntimeError, requests.RequestException) as e:
                    err_str = str(e)
                    if "401" in err_str or "403" in err_str or "invalid_grant" in err_str.lower():
                        # Fix 1 : invalider expires_at et tenter un refresh unique
                        logger.warning("brouillons_bg: 401 reçu, tentative refresh (user %s)", user_id)
                        GmailOAuthToken.objects.filter(utilisateur=user).update(expires_at=None)
                        try:
                            access_token = _gmail_get_access_token(user)
                            logger.info("brouillons_bg: token rafraîchi, poursuite (user %s)", user_id)
                        except Exception:
                            logger.error("brouillons_bg: refresh impossible, arrêt (user %s)", user_id)
                            break
                        skipped += 1  # ce draft est perdu, on continue avec le nouveau token
                    else:
                        logger.warning("brouillons_bg: draft failed '%s': %s", cand.entreprise.email, err_str[:200])
                        skipped += 1

            if to_update:
                with transaction.atomic():
                    Candidature.objects.bulk_update(
                        to_update,
                        ["est_dans_paquet", "brouillon_gmail_cree", "date_traitement"],
                    )

            logger.info("brouillons_bg: terminé — %d créés, %d ignorés (user %s)", created, skipped, user_id)

            # Auto-supprimer le pack document si toutes les candidatures sont traitées
            if created > 0:
                remaining_filter = {"utilisateur": user, "est_dans_paquet": False}
                if secteur:
                    remaining_filter["secteur_activite"] = secteur
                if pack_num:
                    remaining_filter["numero_pack"] = pack_num
                if not Candidature.objects.filter(**remaining_filter).exists():
                    doc_filter = {"utilisateur": user, "type_doc": "PACK_LM"}
                    if secteur:
                        doc_filter["secteur_nom"] = secteur
                    if pack_num:
                        secteur_clean = secteur.replace(" ", "_").replace("/", "-")
                        doc_filter["nom_affichage"] = f"MX_SCAN_{secteur_clean}_PACK_{pack_num}"
                    for pack_doc in DocumentUtilisateur.objects.filter(**doc_filter):
                        try:
                            if getattr(pack_doc, "fichier", None):
                                pack_doc.fichier.delete(save=False)
                        except Exception:
                            pass
                        pack_doc.delete()
                        logger.info("brouillons_bg: pack '%s' auto-supprimé (user %s)", pack_doc.nom_affichage, user_id)

        except Exception:
            logger.exception("brouillons_bg: exception non gérée (user %s)", user_id)

    _run_in_background(_run_brouillons, request.user.id, secteur, pack_num)

    nb = qs_ent.count()
    messages.success(
        request,
        f"⏳ Création de {nb} brouillon(s) lancée en arrière-plan. "
        f"Rafraîchis le dashboard dans quelques minutes pour voir la progression."
    )
    return redirect_dashboard


@login_required
@require_GET
def gmail_progress(request):
    secteur = (request.GET.get("secteur") or "").strip()

    qs_all = Candidature.objects.filter(utilisateur=request.user)
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
