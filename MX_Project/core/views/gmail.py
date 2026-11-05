"""Création de brouillons Gmail via OAuth."""

import base64
import html
import logging
from email.message import EmailMessage
from email.policy import SMTP
from urllib.parse import urlencode

import requests

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.shortcuts import redirect
from django.utils.timezone import now
from django.views.decorators.http import require_POST

from ..models import Candidature, DocumentUtilisateur, GmailOAuthToken, LettreSecteurTemplate, ProfilUtilisateur
from ._lettre import (
    Contenu,
    bloc_signature,
    contenu_email,
    contexte,
    documents_a_joindre,
    libelles_annexes,
    nom_fichier_document,
    objet_email,
)
from ._pdf import generer_pdf_lm
from ._utils import _lm_pdf_name, _read_filefield_bytes, _run_in_background
from .auth import _gmail_get_access_token

logger = logging.getLogger(__name__)

MAX_BROUILLONS_PAR_LOT = 500


def _b64url(data: bytes) -> str:
    """Encode en base64 URL-safe sans padding, format attendu par l'API Gmail pour `raw`."""
    return base64.urlsafe_b64encode(data).decode("utf-8").rstrip("=")


def _corps_texte(contenu: Contenu, signature: list[str]) -> str:
    blocs = [contenu.salutation, *contenu.paragraphes, contenu.politesse, "\n".join(signature)]
    return "\n\n".join(b for b in blocs if b) + "\n"


def _corps_html(contenu: Contenu, signature: list[str]) -> str:
    """Version HTML : Gmail ouvre le brouillon avec de vrais paragraphes au lieu de lignes brutes."""
    def para(t: str) -> str:
        return '<p style="margin:0 0 1em 0">' + html.escape(t).replace("\n", "<br>") + "</p>"

    blocs = [contenu.salutation, *contenu.paragraphes, contenu.politesse, "\n".join(signature)]
    corps = "".join(para(b) for b in blocs if b)
    return f'<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.5">{corps}</div>'


def _build_mime_message(
    to_email: str,
    subject: str,
    body: str,
    attachments: list[tuple[str, bytes, str]],
    body_html: str = "",
) -> bytes:
    """Construit un message MIME (texte + HTML + pièces jointes) prêt à être encodé pour l'API Gmail."""
    msg = EmailMessage(policy=SMTP)
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.set_content(body or "", charset="utf-8")
    if body_html:
        msg.add_alternative(body_html, subtype="html", charset="utf-8")
    for filename, content, mime in attachments:
        if not content:
            continue
        maintype, subtype = (mime.split("/", 1) + ["octet-stream"])[:2]
        msg.add_attachment(content, maintype=maintype, subtype=subtype, filename=filename)
    return msg.as_bytes()


def _gmail_create_draft(access_token: str, raw_mime_bytes: bytes) -> None:
    """Crée un brouillon dans le compte Gmail de l'utilisateur via l'API Gmail."""
    url = "https://gmail.googleapis.com/gmail/v1/users/me/drafts"
    r = requests.post(
        url,
        json={"message": {"raw": _b64url(raw_mime_bytes)}},
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=30,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"Gmail API error {r.status_code}: {r.text[:300]}")


def _filtrer_candidatures(qs, secteur: str, pack_num: int | None):
    qs = qs.filter(est_dans_paquet=False, entreprise__email_valide=True)
    if secteur:
        qs = qs.filter(secteur_activite=secteur)
    if pack_num:
        qs = qs.filter(numero_pack=pack_num)
    return qs


def _est_erreur_auth(err: Exception) -> bool:
    txt = str(err)
    return "error 401" in txt or "invalid_grant" in txt.lower()


def construire_brouillon(profil, cand, tpl_email, templates_map, pieces_fixes, annexes, signature) -> bytes:
    """Message MIME complet d'une candidature : corps (template « Email ») + LM PDF + pièces fixes."""
    ctx = contexte(profil, cand)
    contenu = contenu_email(tpl_email, templates_map.get((cand.secteur_activite or "").strip()), ctx)
    lm_pdf = generer_pdf_lm(profil, cand, annexes=annexes, templates=templates_map)
    return _build_mime_message(
        cand.entreprise.email,
        objet_email(contenu, profil),
        _corps_texte(contenu, signature),
        [(_lm_pdf_name(cand.entreprise.raison_sociale), lm_pdf, "application/pdf"), *pieces_fixes],
        body_html=_corps_html(contenu, signature),
    )


def _supprimer_pack_si_termine(user, secteur, pack_num):
    remaining_filter = {"utilisateur": user, "est_dans_paquet": False, "entreprise__email_valide": True}
    if secteur:
        remaining_filter["secteur_activite"] = secteur
    if pack_num:
        remaining_filter["numero_pack"] = pack_num
    if Candidature.objects.filter(**remaining_filter).exists():
        return
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
        logger.info("brouillons_bg: pack '%s' auto-supprimé (user %s)", pack_doc.nom_affichage, user.id)


def _run_brouillons(user_id, secteur, pack_num=None):
    """Worker de fond : génère et dépose les brouillons Gmail, rafraîchit le token si besoin."""
    from django.contrib.auth.models import User
    try:
        user = User.objects.get(id=user_id)
        profil, _ = ProfilUtilisateur.objects.get_or_create(utilisateur=user)

        try:
            access_token = _gmail_get_access_token(user)
        except Exception as e:
            logger.error("brouillons_bg: token Gmail inaccessible (user %s): %s", user_id, e)
            GmailOAuthToken.objects.filter(utilisateur=user).update(expires_at=None)
            return

        candidatures = list(
            _filtrer_candidatures(Candidature.objects.filter(utilisateur=user), secteur, pack_num)
            .select_related("entreprise", "utilisateur")
            .order_by("id")[:MAX_BROUILLONS_PAR_LOT]
        )
        logger.info("brouillons_bg: %d candidatures à traiter (user %s)", len(candidatures), user_id)

        docs = list(DocumentUtilisateur.objects.filter(utilisateur=user).order_by("-date_upload"))
        cv_doc = next((d for d in docs if d.type_doc == "CV"), None)
        if not cv_doc:
            logger.error("brouillons_bg: CV introuvable pour user %s", user_id)
            return

        docs_joints = documents_a_joindre(docs)
        pieces_fixes: list[tuple[str, bytes, str]] = []
        noms_pris: set[str] = set()
        for d in docs_joints:
            try:
                nom = nom_fichier_document(d)
                if nom.lower() in noms_pris:
                    nom = f"{nom[:-4]} ({d.pk}).pdf"
                noms_pris.add(nom.lower())
                pieces_fixes.append((nom, _read_filefield_bytes(d.fichier), "application/pdf"))
            except (OSError, ValueError) as e:
                if d is cv_doc:
                    logger.error("brouillons_bg: impossible de lire le CV: %s", e)
                    return
                logger.warning("brouillons_bg: pièce jointe illisible ignorée (doc %s): %s", d.pk, e)

        annexes = libelles_annexes(docs_joints)
        templates_map = {t.secteur_nom: t for t in LettreSecteurTemplate.objects.filter(utilisateur=user)}
        tpl_email = templates_map.get("Email")
        signature = bloc_signature(profil)

        created = 0
        skipped = 0
        to_update: list[Candidature] = []
        now_dt = now()
        deja_rafraichi = False

        for cand in candidatures:
            try:
                raw = construire_brouillon(profil, cand, tpl_email, templates_map, pieces_fixes, annexes, signature)
            except Exception as e:
                logger.warning("brouillons_bg: génération échouée '%s': %s", cand.entreprise.email, e)
                skipped += 1
                continue

            try:
                try:
                    _gmail_create_draft(access_token, raw)
                except (RuntimeError, requests.RequestException) as e:
                    if not _est_erreur_auth(e):
                        raise
                    if deja_rafraichi:
                        logger.error("brouillons_bg: 401 persistant après refresh, arrêt (user %s)", user_id)
                        break
                    deja_rafraichi = True
                    # Token expiré en cours de lot : on le rafraîchit et on réessaie ce même brouillon.
                    logger.warning("brouillons_bg: 401 reçu, tentative refresh (user %s)", user_id)
                    GmailOAuthToken.objects.filter(utilisateur=user).update(expires_at=None)
                    try:
                        access_token = _gmail_get_access_token(user)
                    except Exception:
                        logger.error("brouillons_bg: refresh impossible, arrêt (user %s)", user_id)
                        break
                    _gmail_create_draft(access_token, raw)
            except (RuntimeError, requests.RequestException) as e:
                logger.warning("brouillons_bg: brouillon refusé '%s': %s", cand.entreprise.email, str(e)[:200])
                skipped += 1
                continue

            cand.est_dans_paquet = True
            cand.brouillon_gmail_cree = True
            cand.date_traitement = now_dt
            to_update.append(cand)
            created += 1

        if to_update:
            with transaction.atomic():
                Candidature.objects.bulk_update(
                    to_update, ["est_dans_paquet", "brouillon_gmail_cree", "date_traitement"],
                )

        logger.info("brouillons_bg: terminé — %d créés, %d ignorés (user %s)", created, skipped, user_id)

        if created > 0:
            _supprimer_pack_si_termine(user, secteur, pack_num)

    except Exception:
        logger.exception("brouillons_bg: exception non gérée (user %s)", user_id)


@login_required
@require_POST
def creer_brouillons_gmail(request):
    """
    Lance en tâche de fond la création d'un brouillon Gmail (LM + CV joints) par candidature
    non encore traitée du secteur/pack ciblé. Valide le token Gmail avant de démarrer le job.
    """
    try:
        access_token = _gmail_get_access_token(request.user)
    except Exception:
        messages.error(request, "Connexion Gmail invalide ou expirée. Merci de reconnecter Gmail dans Réglages.")
        return redirect("/settings/?tab=connexions")

    secteur = (request.POST.get("secteur") or "").strip()
    pack_num_raw = (request.POST.get("pack_num") or "").strip()
    pack_num = int(pack_num_raw) if pack_num_raw.isdigit() else None

    qs_ent = _filtrer_candidatures(Candidature.objects.filter(utilisateur=request.user), secteur, pack_num)
    redirect_dashboard = redirect(f"/?{urlencode({'secteur': secteur})}" if secteur else "dashboard")

    if not qs_ent.exists():
        messages.info(request, "Aucune entreprise à traiter (toutes déjà traitées ou sans e-mail valide).")
        return redirect_dashboard

    if not DocumentUtilisateur.objects.filter(utilisateur=request.user, type_doc="CV").exists():
        messages.error(
            request,
            "Aucun CV trouvé. Ajoute ton CV (type « CV ») dans la zone Documents du tableau de bord "
            "avant de créer des brouillons.",
        )
        return redirect_dashboard

    # Valider le token Gmail AVANT de lancer le job de fond
    try:
        probe = requests.get(
            "https://gmail.googleapis.com/gmail/v1/users/me/profile",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        if probe.status_code >= 400:
            messages.error(
                request,
                f"Connexion Gmail rejetée (code {probe.status_code}). Reconnecte ton compte Gmail dans les Réglages.",
            )
            return redirect("/settings/?tab=connexions")
    except requests.RequestException:
        messages.error(request, "Impossible de joindre Gmail. Vérifie ta connexion réseau puis réessaie.")
        return redirect_dashboard

    nb = min(qs_ent.count(), MAX_BROUILLONS_PAR_LOT)
    _run_in_background(_run_brouillons, request.user.id, secteur, pack_num)

    messages.success(
        request,
        f"⏳ Création de {nb} brouillon(s) lancée en arrière-plan. "
        f"Rafraîchis le tableau de bord dans quelques minutes pour voir la progression."
    )
    return redirect_dashboard
