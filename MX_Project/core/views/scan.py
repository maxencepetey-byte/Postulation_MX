import logging
import re
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.management import call_command
from django.core.paginator import Paginator
from django.db import IntegrityError
from django.http import HttpResponseBadRequest, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET

from ..constants import NOGA_MAP
from ..models import EntrepriseCible, EntrepriseReferentiel, ProfilUtilisateur, ScanSession
from ._utils import _run_in_background

logger = logging.getLogger(__name__)


def _run_scan_for_user(user, secteurs):
    if not secteurs:
        return

    noms_secteurs = [NOGA_MAP.get(s[:2], s) for s in secteurs]
    session = ScanSession.objects.create(
        utilisateur=user,
        secteurs=", ".join(noms_secteurs),
    )

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
                base_par_secteur[secteur_nom] = EntrepriseCible.objects.filter(
                    utilisateur=user, secteur_activite=secteur_nom,
                ).count()
                ajoutes_par_secteur[secteur_nom] = 0

            qs = (
                EntrepriseReferentiel.objects
                .filter(code_noga__startswith=s, email_valide=True)
                .only("raison_sociale", "email", "adresse")
                .order_by("raison_sociale")
            )
            for ref in qs.iterator(chunk_size=2000):
                total_courant = base_par_secteur[secteur_nom] + ajoutes_par_secteur[secteur_nom]
                pack_id = (total_courant // 500) + 1
                try:
                    EntrepriseCible.objects.create(
                        scan_session=session,
                        utilisateur=user,
                        nom=ref.raison_sociale,
                        email=ref.email,
                        numero_pack=pack_id,
                        secteur_activite=secteur_nom,
                        adresse=ref.adresse or "",
                    )
                    total_ajoutes += 1
                    ajoutes_par_secteur[secteur_nom] += 1
                except IntegrityError:
                    total_doublons += 1

        session.nb_entreprises = total_ajoutes
        session.nb_doublons_evites = total_doublons
        session.save()
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
            kwargs = {"min_new": min_new, "dry_run": dry_run, "since_hours": since_hours}
            if secteurs:
                kwargs["secteurs"] = secteurs
            call_command("sync_registre", **kwargs)
            logger.info(
                "cron_sync_registre finished (secteurs=%s, min_new=%s, since_hours=%s, dry_run=%s)",
                secteurs, min_new, since_hours, dry_run,
            )
        except Exception:
            logger.exception("cron_sync_registre failed")

    _run_in_background(_run_cron)
    return JsonResponse(
        {"status": "started", "secteurs": secteurs, "min_new": min_new, "since_hours": since_hours, "dry_run": dry_run},
        status=202,
    )


@login_required
def historique_scans(request):
    sessions = ScanSession.objects.filter(utilisateur=request.user)
    return render(request, 'core/historique.html', {'sessions': sessions})


@login_required
def detail_scan(request, session_id):
    session = get_object_or_404(ScanSession, id=session_id, utilisateur=request.user)
    paginator = Paginator(session.entreprises.all().order_by('secteur_activite', 'nom'), 50)
    return render(request, 'core/detail_scan.html', {
        'session': session,
        'entreprises': paginator.get_page(request.GET.get('page')),
    })
