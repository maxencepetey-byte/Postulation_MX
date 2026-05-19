import logging
import re
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.management import call_command
from django.core.paginator import Paginator
from django.db import IntegrityError
from django.db.models import Count, Min
from django.db.models.functions import TruncDate
from django.http import HttpResponseBadRequest, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET

from ..constants import NOGA_MAP
from ..models import Candidature, EntrepriseReferentiel, ProfilUtilisateur
from ._utils import _run_in_background

logger = logging.getLogger(__name__)


class _VirtualSession:
    """
    Pseudo-objet imitant l'ancienne ScanSession pour la compatibilité des templates.
    Construit à la volée en groupant les Candidatures par (secteurs, date_scan).
    """
    __slots__ = ('id', 'date_scan', 'secteurs', 'nb_entreprises', 'nb_doublons_evites')

    def __init__(self, session_id, date_scan, secteurs, nb_entreprises):
        self.id = session_id
        self.date_scan = date_scan
        self.secteurs = secteurs
        self.nb_entreprises = nb_entreprises
        self.nb_doublons_evites = 0  # non stocké après migration — affiché comme 0


def _run_scan_for_user(user, secteurs):
    """
    Crée une Candidature par entreprise du référentiel pour chaque code NOGA demandé.
    Les doublons (même user + même entreprise) sont silencieusement ignorés via IntegrityError.
    """
    if not secteurs:
        return

    noms_secteurs = [NOGA_MAP.get(s[:2], s) for s in secteurs]
    secteurs_str = ", ".join(noms_secteurs)

    try:
        total_ajoutes = 0
        total_doublons = 0
        base_par_secteur: dict[str, int] = {}
        ajoutes_par_secteur: dict[str, int] = {}

        for s in secteurs:
            if not re.match(r'^\d{2}$', s):
                logger.warning("_run_scan_for_user: code NOGA invalide ignoré: %r", s)
                continue

            secteur_nom = NOGA_MAP.get(s[:2], "Général")
            if secteur_nom not in base_par_secteur:
                base_par_secteur[secteur_nom] = Candidature.objects.filter(
                    utilisateur=user, secteur_activite=secteur_nom,
                ).count()
                ajoutes_par_secteur[secteur_nom] = 0

            qs = (
                EntrepriseReferentiel.objects
                .filter(code_noga__startswith=s, email_valide=True)
                .only("id", "raison_sociale", "email", "adresse")
                .order_by("raison_sociale")
            )
            for ref in qs.iterator(chunk_size=2000):
                total_courant = base_par_secteur[secteur_nom] + ajoutes_par_secteur[secteur_nom]
                pack_id = (total_courant // 500) + 1
                try:
                    Candidature.objects.create(
                        utilisateur=user,
                        entreprise=ref,
                        secteurs=secteurs_str,
                        numero_pack=pack_id,
                        secteur_activite=secteur_nom,
                    )
                    total_ajoutes += 1
                    ajoutes_par_secteur[secteur_nom] += 1
                except IntegrityError:
                    total_doublons += 1

        logger.info(
            "_run_scan_for_user: terminé — %d ajoutés, %d doublons (user %s)",
            total_ajoutes, total_doublons, user.id,
        )
    except Exception:
        logger.exception("_run_scan_for_user failed (user %s)", user.id)


@login_required
def lancer_scan(request):
    secteurs = request.GET.getlist('secteurs')
    secteur_libre = request.GET.get('secteur_libre', '').strip()
    if secteur_libre:
        secteurs.append(secteur_libre)

    secteurs = [s for s in secteurs if re.match(r'^\d{2}$', s)]
    if not secteurs:
        return redirect('dashboard')

    noms_secteurs = [NOGA_MAP.get(s[:2], s) for s in secteurs]
    _run_in_background(_run_scan_for_user, request.user, secteurs)
    messages.success(
        request,
        f"⏳ Scan lancé pour {', '.join(noms_secteurs)}. Les résultats apparaîtront dans quelques instants."
    )
    secteur_default = (noms_secteurs[0] if noms_secteurs else "").strip()
    if secteur_default:
        return redirect(f"/?{urlencode({'secteur': secteur_default})}")
    return redirect('dashboard')


@require_GET
def cron_sync_registre(request):
    auth_header = request.META.get("HTTP_AUTHORIZATION", "")
    token = auth_header.removeprefix("Bearer ").strip()
    expected = (getattr(settings, "CRON_SYNC_TOKEN", "") or "").strip()
    if not expected or token != expected:
        return HttpResponseForbidden("Forbidden")

    secteurs = request.GET.getlist("secteurs")
    min_new_raw = (request.GET.get("min_new") or "500").strip()
    since_hours_raw = (request.GET.get("since_hours") or "24").strip()
    dry_run = (request.GET.get("dry_run") or "").strip().lower() in ("1", "true", "yes")
    skip_email_check = (request.GET.get("skip_email_check") or "").strip().lower() in ("1", "true", "yes")

    try:
        min_new = int(min_new_raw)
    except Exception:
        return HttpResponseBadRequest("min_new must be an integer")

    try:
        since_hours = int(since_hours_raw)
    except Exception:
        return HttpResponseBadRequest("since_hours must be an integer")

    def _run_cron():
        try:
            kwargs = {
                "min_new": min_new,
                "dry_run": dry_run,
                "since_hours": since_hours,
                "skip_email_check": skip_email_check,
            }
            if secteurs:
                kwargs["secteurs"] = secteurs
            call_command("sync_registre", **kwargs)
            logger.info(
                "cron_sync_registre finished (secteurs=%s, min_new=%s, since_hours=%s, dry_run=%s, skip_email_check=%s)",
                secteurs, min_new, since_hours, dry_run, skip_email_check,
            )
        except Exception:
            logger.exception("cron_sync_registre failed")

    _run_in_background(_run_cron)
    return JsonResponse(
        {
            "status": "started",
            "secteurs": secteurs,
            "min_new": min_new,
            "since_hours": since_hours,
            "dry_run": dry_run,
            "skip_email_check": skip_email_check,
        },
        status=202,
    )


@login_required
def historique_scans(request):
    """
    Affiche l'historique des scans sous forme de sessions virtuelles.
    Chaque session = groupe de Candidatures partageant le même champ 'secteurs'
    et la même date de scan (tronquée au jour).
    L'ID de session = ID minimum du groupe (utilisé pour l'URL detail_scan).
    """
    groups = (
        Candidature.objects
        .filter(utilisateur=request.user)
        .annotate(scan_date=TruncDate('date_scan'))
        .values('secteurs', 'scan_date')
        .annotate(
            nb_entreprises=Count('id'),
            date_scan=Min('date_scan'),
            session_id=Min('id'),
        )
        .order_by('-date_scan')
    )
    sessions = [
        _VirtualSession(g['session_id'], g['date_scan'], g['secteurs'], g['nb_entreprises'])
        for g in groups
    ]
    return render(request, 'core/historique.html', {'sessions': sessions})


@login_required
def detail_scan(request, session_id):
    """
    Affiche les Candidatures d'une session virtuelle identifiée par l'ID de la
    première Candidature créée lors de ce scan (même secteurs + même jour).
    """
    anchor = get_object_or_404(Candidature, id=session_id, utilisateur=request.user)
    anchor_date = anchor.date_scan.date()

    qs = (
        Candidature.objects
        .filter(utilisateur=request.user, secteurs=anchor.secteurs)
        .annotate(scan_date=TruncDate('date_scan'))
        .filter(scan_date=anchor_date)
        .select_related('entreprise')
        .order_by('secteur_activite', 'entreprise__raison_sociale')
    )

    session = _VirtualSession(
        session_id=session_id,
        date_scan=anchor.date_scan,
        secteurs=anchor.secteurs,
        nb_entreprises=qs.count(),
    )

    paginator = Paginator(qs, 50)
    return render(request, 'core/detail_scan.html', {
        'session': session,
        'entreprises': paginator.get_page(request.GET.get('page')),
    })
