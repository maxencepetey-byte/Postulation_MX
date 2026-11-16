"""Scan du registre des entreprises : exécution, historique groupé, détail par session."""

import hmac
import logging
import re
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.management import call_command
from django.core.paginator import Paginator
from django.db.models import Count, Min
from django.db.models.functions import TruncDate
from django.http import HttpResponseBadRequest, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

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

    @property
    def secteurs_liste(self):
        return _decouper_secteurs(self.secteurs)


_LIBELLES_NOGA = frozenset(NOGA_MAP.values())
# Borne la recherche : sans elle, une longue chaîne coûte un temps cubique à chaque affichage.
_MORCEAUX_MAX = max(libelle.count(", ") for libelle in _LIBELLES_NOGA) + 1


def _decouper_secteurs(chaine: str) -> list[str]:
    """Redécoupe « A, B, C » en libellés ; certains libellés contiennent eux-mêmes une virgule."""
    morceaux = [m for m in (chaine or "").split(", ") if m.strip()]
    libelles, i = [], 0
    while i < len(morceaux):
        for j in range(min(len(morceaux), i + _MORCEAUX_MAX), i, -1):
            candidat = ", ".join(morceaux[i:j])
            if j == i + 1 or candidat in _LIBELLES_NOGA:
                libelles.append(candidat.strip())
                i = j
                break
    return libelles


def _run_scan_for_user(user, secteurs):
    """
    Crée une Candidature par entreprise du référentiel pour chaque code NOGA demandé.
    Les entreprises déjà dans la liste de l'utilisateur sont ignorées.
    """
    # Un même code répété relancerait le scan du secteur et gonflerait la chaîne enregistrée.
    secteurs = list(dict.fromkeys(secteurs))
    if not secteurs:
        return

    noms_secteurs = [NOGA_MAP.get(s[:2], s) for s in secteurs]
    secteurs_str = ", ".join(noms_secteurs)

    try:
        total_ajoutes = 0
        total_doublons = 0
        base_par_secteur: dict[str, int] = {}
        ajoutes_par_secteur: dict[str, int] = {}
        deja = set(Candidature.objects.filter(utilisateur=user).values_list("entreprise_id", flat=True))

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

            ids = (
                EntrepriseReferentiel.objects
                .filter(code_noga__startswith=s, email_valide=True, radiee=False)
                .order_by("raison_sociale")
                .values_list("id", flat=True)
            )
            nouvelles: list[Candidature] = []
            for ref_id in ids.iterator(chunk_size=2000):
                if ref_id in deja:
                    total_doublons += 1
                    continue
                deja.add(ref_id)
                total_courant = base_par_secteur[secteur_nom] + ajoutes_par_secteur[secteur_nom]
                nouvelles.append(Candidature(
                    utilisateur=user,
                    entreprise_id=ref_id,
                    secteurs=secteurs_str,
                    numero_pack=(total_courant // 500) + 1,
                    secteur_activite=secteur_nom,
                ))
                ajoutes_par_secteur[secteur_nom] += 1

            # Une requête par 500 lignes au lieu d'une par entreprise.
            # ignore_conflicts : un second scan lancé au même moment ne fait pas échouer tout le lot.
            Candidature.objects.bulk_create(nouvelles, batch_size=500, ignore_conflicts=True)
            total_ajoutes += len(nouvelles)

        logger.info(
            "_run_scan_for_user: terminé — %d ajoutés, %d doublons (user %s)",
            total_ajoutes, total_doublons, user.id,
        )
    except Exception:
        logger.exception("_run_scan_for_user failed (user %s)", user.id)


@login_required
@require_POST
def lancer_scan(request):
    """Lance un scan manuel du registre pour les secteurs cochés (+ code NOGA libre), en tâche de fond."""
    secteurs = request.POST.getlist('secteurs')
    secteur_libre = request.POST.get('secteur_libre', '').strip()
    if secteur_libre:
        secteurs.append(secteur_libre)

    secteurs = [s for s in secteurs if s in NOGA_MAP]
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
    """Endpoint cron (token Bearer) qui déclenche `sync_registre` en tâche de fond et répond 202 immédiatement."""
    auth_header = request.META.get("HTTP_AUTHORIZATION", "")
    token = auth_header.removeprefix("Bearer ").strip()
    expected = (getattr(settings, "CRON_SYNC_TOKEN", "") or "").strip()
    if not expected or not hmac.compare_digest(token.encode(), expected.encode()):
        return HttpResponseForbidden("Forbidden")

    secteurs = request.GET.getlist("secteurs")
    min_new_raw = (request.GET.get("min_new") or "500").strip()
    since_hours_raw = (request.GET.get("since_hours") or "24").strip()
    dry_run = (request.GET.get("dry_run") or "").strip().lower() in ("1", "true", "yes")
    skip_email_check = (request.GET.get("skip_email_check") or "").strip().lower() in ("1", "true", "yes")
    # MX seul par défaut : l'hébergeur bloque le port 25, la vérification SMTP n'aboutirait pas.
    mx_only = (request.GET.get("mx_only") or "1").strip().lower() in ("1", "true", "yes")

    try:
        min_new = int(min_new_raw)
    except Exception:
        return HttpResponseBadRequest("min_new must be an integer")

    try:
        since_hours = int(since_hours_raw)
    except Exception:
        return HttpResponseBadRequest("since_hours must be an integer")

    def _run_cron():
        """Worker de fond : exécute la management command de synchro du registre."""
        try:
            kwargs = {
                "min_new": min_new,
                "dry_run": dry_run,
                "since_hours": since_hours,
                "skip_email_check": skip_email_check,
                "mx_only": mx_only,
            }
            if secteurs:
                kwargs["secteurs"] = secteurs
            call_command("sync_registre", **kwargs)
            logger.info(
                "cron_sync_registre finished (secteurs=%s, min_new=%s, since_hours=%s, dry_run=%s, skip_email_check=%s, mx_only=%s)",
                secteurs, min_new, since_hours, dry_run, skip_email_check, mx_only,
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
            "mx_only": mx_only,
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
    # TruncDate tronque en Europe/Zurich : .date() seul donnerait le jour UTC (faux entre 0 h et 2 h).
    anchor_date = timezone.localtime(anchor.date_scan).date()

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
