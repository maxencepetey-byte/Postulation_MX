"""Création de brouillons Gmail via OAuth."""

import base64
import html
import logging
import time
from email.message import EmailMessage
from email.policy import SMTP
from urllib.parse import urlencode

import requests

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, connection, transaction
from django.http import JsonResponse
from django.shortcuts import redirect
from django.utils.timezone import now
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from ..models import (
    Candidature,
    DocumentUtilisateur,
    GmailOAuthToken,
    LettreSecteurTemplate,
    LotBrouillons,
    ProfilUtilisateur,
)
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
# Attentes (secondes) avant chaque nouvelle tentative quand Gmail répond 429 ou 5xx.
DELAIS_REPRISE = (2, 4, 8, 16)
ATTENTE_MAX = 60
# Au-delà, Gmail est considéré comme indisponible : inutile d'user tout le lot en échecs.
MAX_ECHECS_DE_SUITE = 5

_REPRISE = "relance, le lot reprendra où il s'est arrêté."
MSG_GMAIL_EXPIRE = "La connexion Gmail a expiré : reconnecte Gmail dans les Réglages, puis relance."
MSG_CV_ABSENT = "Aucun CV trouvé : ajoute ton CV dans la zone Documents, puis relance."
MSG_CV_ILLISIBLE = "Le fichier de ton CV n'est plus sur le serveur : renvoie-le dans la zone Documents, puis relance."
MSG_DEBIT = f"Gmail limite le nombre de brouillons pour l'instant : attends quelques minutes, puis {_REPRISE}"
MSG_GMAIL_INDISPONIBLE = f"Gmail refuse les brouillons pour l'instant : attends un peu, puis {_REPRISE}"
MSG_SERVEUR_ARRETE = f"Le serveur s'est arrêté avant la fin : {_REPRISE}"
MSG_ERREUR_INATTENDUE = f"Une erreur inattendue a arrêté le lot : {_REPRISE}"
MSG_LOT_DEJA_EN_COURS = "Un lot de brouillons est déjà en cours : attends sa fin avant d'en lancer un autre."


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


class GmailApiError(RuntimeError):
    """Refus de l'API Gmail, avec le code HTTP pour distinguer ce qui mérite une nouvelle tentative."""

    def __init__(self, status: int, texte: str = "", retry_after: int | None = None):
        super().__init__(f"Gmail API error {status}: {texte[:300]}")
        self.status = status
        self.texte = texte
        self.retry_after = retry_after

    @property
    def limite_de_debit(self) -> bool:
        # Gmail signale aussi le débit par un 403 « rateLimitExceeded » / « userRateLimitExceeded ».
        return self.status == 429 or (self.status == 403 and "ratelimitexceeded" in self.texte.lower())

    @property
    def temporaire(self) -> bool:
        return self.limite_de_debit or self.status in (500, 502, 503, 504)


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
        raise GmailApiError(r.status_code, r.text, _entier(r.headers.get("Retry-After")))


def _entier(valeur) -> int | None:
    """Entier positif lu d'une saisie ; isascii écarte « ² », que isdigit accepte et que int refuse."""
    valeur = (valeur or "").strip()
    return int(valeur) if valeur.isascii() and valeur.isdigit() and len(valeur) <= 9 else None


def _filtrer_candidatures(qs, secteur: str, pack_num: int | None):
    qs = qs.filter(est_dans_paquet=False, entreprise__email_valide=True)
    if secteur:
        qs = qs.filter(secteur_activite=secteur)
    if pack_num:
        qs = qs.filter(numero_pack=pack_num)
    return qs


def _est_erreur_auth(err: Exception) -> bool:
    return getattr(err, "status", None) == 401 or "invalid_grant" in str(err).lower()


def _fichier_present(doc) -> bool:
    try:
        return bool(doc.fichier) and doc.fichier.storage.exists(doc.fichier.name)
    except Exception:
        return False


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


# ---------------------------------------------------------------------------
# Lot de brouillons : verrou par utilisateur et compte rendu
# ---------------------------------------------------------------------------

def _ouvrir_lot(user, secteur: str, pack_num: int | None, total: int) -> LotBrouillons | None:
    """Réserve le lot de l'utilisateur ; None si un autre lot tourne déjà."""
    LotBrouillons.objects.filter(
        utilisateur=user, statut=LotBrouillons.EN_COURS, date_maj__lt=now() - LotBrouillons.DELAI_ABANDON,
    ).update(statut=LotBrouillons.INTERROMPU, date_fin=now(), message=MSG_SERVEUR_ARRETE)
    # Seul le dernier lot est affiché : les anciens ne servent plus et la table ne doit pas grossir sans fin.
    LotBrouillons.objects.filter(
        utilisateur=user, date_maj__lt=now() - LotBrouillons.DUREE_AFFICHAGE,
    ).exclude(statut=LotBrouillons.EN_COURS).delete()
    try:
        # atomic : le refus de la contrainte d'unicité ne doit pas casser la transaction de la requête.
        with transaction.atomic():
            return LotBrouillons.objects.create(
                utilisateur=user, secteur=secteur, numero_pack=pack_num, total=total,
            )
    except IntegrityError:
        return None


def _battement(lot: LotBrouillons) -> bool:
    """Enregistre l'avancement ; False si le lot n'est plus le lot en cours (repris ou clos ailleurs)."""
    return LotBrouillons.objects.filter(pk=lot.pk, statut=LotBrouillons.EN_COURS).update(
        crees=lot.crees, ignores=lot.ignores, date_maj=now(),
    ) == 1


def _clore_lot(lot: LotBrouillons, statut: str, message: str = "") -> None:
    LotBrouillons.objects.filter(pk=lot.pk, statut=LotBrouillons.EN_COURS).update(
        statut=statut, message=message[:255], crees=lot.crees, ignores=lot.ignores,
        date_maj=now(), date_fin=now(),
    )


def etat_du_lot(user) -> dict | None:
    """Dernier lot de l'utilisateur, mis en forme pour le bandeau du tableau de bord (None si trop ancien)."""
    lot = LotBrouillons.objects.filter(utilisateur=user).order_by("-id").first()
    if not lot or lot.date_maj < now() - LotBrouillons.DUREE_AFFICHAGE:
        return None

    cible = " — ".join(p for p in (lot.secteur, f"pack {lot.numero_pack}" if lot.numero_pack else "") if p)
    prefixe = f"{cible} : " if cible else ""
    ignores = f" {lot.ignores} entreprise(s) ignorée(s)." if lot.ignores else ""

    if lot.statut == LotBrouillons.EN_COURS and not lot.abandonne:
        return {
            "en_cours": True, "classe": "info",
            "texte": f"{prefixe}création des brouillons Gmail en cours, {lot.crees} sur {lot.total}.{ignores}",
        }
    if lot.statut == LotBrouillons.TERMINE:
        return {
            "en_cours": False, "classe": "success" if lot.crees else "warning",
            "texte": f"{prefixe}{lot.crees} brouillon(s) créé(s) dans Gmail.{ignores}",
        }
    raison = lot.message or MSG_SERVEUR_ARRETE
    return {
        "en_cours": False, "classe": "warning",
        "texte": f"{prefixe}lot interrompu après {lot.crees} brouillon(s) sur {lot.total}.{ignores} {raison}",
    }


def _deposer_avec_reprises(lot: LotBrouillons, access_token: str, raw: bytes) -> None:
    """Dépose le brouillon ; un refus temporaire de Gmail (429, 5xx) est retenté après une attente croissante."""
    for delai in (*DELAIS_REPRISE, None):
        try:
            return _gmail_create_draft(access_token, raw)
        except GmailApiError as e:
            if delai is None or not e.temporaire:
                raise
            attente = min(e.retry_after or delai, ATTENTE_MAX)
            logger.warning("brouillons_bg: Gmail %s, nouvelle tentative dans %ss (lot %s)", e.status, attente, lot.pk)
        # Sans battement, une longue attente ferait passer le lot pour abandonné.
        _battement(lot)
        time.sleep(attente)


def _traiter_lot(lot: LotBrouillons) -> str:
    """Dépose les brouillons du lot. Retourne la raison de l'arrêt, ou "" si le lot est allé au bout."""
    user = lot.utilisateur
    secteur, pack_num = lot.secteur, lot.numero_pack
    # Le thread a pu attendre son tour assez longtemps pour qu'un autre lot ait pris la place.
    if not _battement(lot):
        return ""
    profil, _ = ProfilUtilisateur.objects.get_or_create(utilisateur=user)

    try:
        access_token = _gmail_get_access_token(user)
    except Exception as e:
        logger.error("brouillons_bg: token Gmail inaccessible (user %s): %s", user.id, e)
        GmailOAuthToken.objects.filter(utilisateur=user).update(expires_at=None)
        return MSG_GMAIL_EXPIRE

    candidatures = list(
        _filtrer_candidatures(Candidature.objects.filter(utilisateur=user), secteur, pack_num)
        .select_related("entreprise", "utilisateur")
        .order_by("id")[:MAX_BROUILLONS_PAR_LOT]
    )
    logger.info("brouillons_bg: %d candidatures à traiter (user %s)", len(candidatures), user.id)

    docs = list(DocumentUtilisateur.objects.filter(utilisateur=user).order_by("-date_upload"))
    cv_doc = next((d for d in docs if d.type_doc == "CV"), None)
    if not cv_doc:
        logger.error("brouillons_bg: CV introuvable pour user %s", user.id)
        return MSG_CV_ABSENT

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
                return MSG_CV_ILLISIBLE
            logger.warning("brouillons_bg: pièce jointe illisible ignorée (doc %s): %s", d.pk, e)

    annexes = libelles_annexes(docs_joints)
    templates_map = {t.secteur_nom: t for t in LettreSecteurTemplate.objects.filter(utilisateur=user)}
    tpl_email = templates_map.get("Email")
    signature = bloc_signature(profil)

    deja_rafraichi = False
    echecs_de_suite = 0

    for cand in candidatures:
        if not _battement(lot):
            logger.warning("brouillons_bg: lot %s repris ailleurs, arrêt (user %s)", lot.pk, user.id)
            return ""
        # Relecture : la candidature a pu être traitée ou supprimée depuis le chargement de la liste.
        if not Candidature.objects.filter(pk=cand.pk, est_dans_paquet=False).exists():
            continue

        try:
            raw = construire_brouillon(profil, cand, tpl_email, templates_map, pieces_fixes, annexes, signature)
        except Exception as e:
            logger.warning("brouillons_bg: génération échouée '%s': %s", cand.entreprise.email, e)
            lot.ignores += 1
            continue

        try:
            try:
                _deposer_avec_reprises(lot, access_token, raw)
            except GmailApiError as e:
                if not _est_erreur_auth(e):
                    raise
                if deja_rafraichi:
                    logger.error("brouillons_bg: 401 persistant après refresh, arrêt (user %s)", user.id)
                    return MSG_GMAIL_EXPIRE
                deja_rafraichi = True
                # Token expiré en cours de lot : on le rafraîchit et on réessaie ce même brouillon.
                logger.warning("brouillons_bg: 401 reçu, tentative refresh (user %s)", user.id)
                GmailOAuthToken.objects.filter(utilisateur=user).update(expires_at=None)
                try:
                    access_token = _gmail_get_access_token(user)
                except Exception:
                    logger.error("brouillons_bg: refresh impossible, arrêt (user %s)", user.id)
                    return MSG_GMAIL_EXPIRE
                try:
                    _deposer_avec_reprises(lot, access_token, raw)
                except GmailApiError as e2:
                    if _est_erreur_auth(e2):
                        logger.error("brouillons_bg: 401 malgré un jeton neuf, arrêt (user %s)", user.id)
                        return MSG_GMAIL_EXPIRE
                    raise
        except (GmailApiError, requests.RequestException) as e:
            logger.warning("brouillons_bg: brouillon refusé '%s': %s", cand.entreprise.email, str(e)[:200])
            if isinstance(e, GmailApiError) and e.limite_de_debit:
                # Insister ferait échouer tout le reste du lot : mieux vaut s'arrêter et reprendre plus tard.
                return MSG_DEBIT
            lot.ignores += 1
            # Un refus propre à ce message (400 : adresse rejetée) ne dit rien de l'état de Gmail :
            # compté ici, cinq entreprises refusées de suite bloqueraient le lot à chaque relance.
            if not isinstance(e, GmailApiError) or e.temporaire:
                echecs_de_suite += 1
                if echecs_de_suite >= MAX_ECHECS_DE_SUITE:
                    return MSG_GMAIL_INDISPONIBLE
            continue

        echecs_de_suite = 0
        # Enregistré tout de suite : si le serveur s'arrête, le lot suivant ne recrée pas ce brouillon.
        Candidature.objects.filter(pk=cand.pk).update(
            est_dans_paquet=True, brouillon_gmail_cree=True, date_traitement=now(),
        )
        lot.crees += 1

    return ""


def _fermer_connexion_morte() -> None:
    """Après une panne de base, la connexion du thread est inutilisable : la fermer pour que Django la rouvre."""
    try:
        if connection.connection is not None and not connection.is_usable():
            connection.close()
    except Exception:
        connection.close()


def _run_brouillons(lot_id):
    """Worker de fond : dépose les brouillons d'un lot et consigne son résultat."""
    try:
        lot = LotBrouillons.objects.select_related("utilisateur").get(pk=lot_id)
    except LotBrouillons.DoesNotExist:
        return
    try:
        raison = _traiter_lot(lot)
    except Exception:
        logger.exception("brouillons_bg: exception non gérée (lot %s)", lot_id)
        raison = MSG_ERREUR_INATTENDUE
        _fermer_connexion_morte()
    try:
        _clore_lot(lot, LotBrouillons.INTERROMPU if raison else LotBrouillons.TERMINE, raison)
    except Exception:
        # Le lot reste EN_COURS : le délai d'abandon le libérera.
        logger.exception("brouillons_bg: clôture impossible (lot %s)", lot_id)
        return
    logger.info(
        "brouillons_bg: lot %s clos — %d créés, %d ignorés (user %s)%s",
        lot_id, lot.crees, lot.ignores, lot.utilisateur_id, f" — {raison}" if raison else "",
    )
    if lot.crees > 0:
        try:
            _supprimer_pack_si_termine(lot.utilisateur, lot.secteur, lot.numero_pack)
        except Exception:
            logger.exception("brouillons_bg: nettoyage du pack échoué (lot %s)", lot_id)


@login_required
@require_POST
def creer_brouillons_gmail(request):
    """
    Lance en tâche de fond la création d'un brouillon Gmail (LM + CV joints) par candidature
    non encore traitée du secteur/pack ciblé. Valide le token Gmail avant de démarrer le job.
    """
    secteur = (request.POST.get("secteur") or "").strip()
    pack_num = _entier(request.POST.get("pack_num"))
    redirect_dashboard = redirect(f"/?{urlencode({'secteur': secteur})}" if secteur else "dashboard")

    etat = etat_du_lot(request.user)
    if etat and etat["en_cours"]:
        messages.info(request, MSG_LOT_DEJA_EN_COURS)
        return redirect_dashboard

    try:
        access_token = _gmail_get_access_token(request.user)
    except Exception:
        messages.error(request, "Connexion Gmail invalide ou expirée. Merci de reconnecter Gmail dans Réglages.")
        return redirect("/settings/?tab=connexions")

    qs_ent = _filtrer_candidatures(Candidature.objects.filter(utilisateur=request.user), secteur, pack_num)

    if not qs_ent.exists():
        messages.info(request, "Aucune entreprise à traiter (toutes déjà traitées ou sans e-mail valide).")
        return redirect_dashboard

    cv_doc = (
        DocumentUtilisateur.objects.filter(utilisateur=request.user, type_doc="CV").order_by("-date_upload").first()
    )
    if not cv_doc:
        messages.error(
            request,
            "Aucun CV trouvé. Ajoute ton CV (type « CV ») dans la zone Documents du tableau de bord "
            "avant de créer des brouillons.",
        )
        return redirect_dashboard
    if not _fichier_present(cv_doc):
        messages.error(request, MSG_CV_ILLISIBLE)
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

    lot = _ouvrir_lot(request.user, secteur, pack_num, min(qs_ent.count(), MAX_BROUILLONS_PAR_LOT))
    if lot is None:
        messages.info(request, MSG_LOT_DEJA_EN_COURS)
        return redirect_dashboard

    # Pas de message flash : le bandeau du tableau de bord affiche l'avancement du lot.
    # Battement pendant l'attente d'une place : sinon un lot en file passerait pour abandonné.
    _run_in_background(_run_brouillons, lot.pk, en_attente=lambda: _battement(lot))
    return redirect_dashboard


@login_required
@require_GET
@never_cache
def etat_lot_brouillons(request):
    """Avancement du dernier lot, interrogé par le bandeau du tableau de bord."""
    return JsonResponse(etat_du_lot(request.user) or {"en_cours": False, "texte": "", "classe": ""})
