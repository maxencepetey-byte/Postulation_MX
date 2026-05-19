import logging
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from datetime import datetime, timedelta

import requests
from django.core.management.base import BaseCommand
from django.db import transaction

from core.constants import NOGA_MAP, SERVICE_URL
from core.management.commands.check_emails import STATUTS_HARD_KO, _verifier_email
from core.models import EntrepriseReferentiel

logger = logging.getLogger(__name__)


# ==========================================================================
# Configuration
# ==========================================================================
DEFAULT_PAGE_SIZE = 1000
DEFAULT_REQUEST_TIMEOUT = 30
DEFAULT_MAX_RETRIES = 5
DEFAULT_INTER_PAGE_DELAY = 0.5  # secondes entre pages d'un même secteur
DEFAULT_INTER_SECTOR_DELAY = 1.0  # secondes entre secteurs
DEFAULT_EMAIL_WORKERS = 5  # workers parallèles pour validation email


# ==========================================================================
# Helpers HTTP
# ==========================================================================
def _build_session() -> requests.Session:
    """Session HTTP réutilisable avec en-tête User-Agent identifiant."""
    session = requests.Session()
    session.headers.update({
        "User-Agent": "MX-Project-Sync/1.0 (registre referentiel)",
        "Accept": "application/json",
    })
    return session


def _request_with_retry(
    session: requests.Session,
    url: str,
    params: dict,
    *,
    max_retries: int = DEFAULT_MAX_RETRIES,
    timeout: int = DEFAULT_REQUEST_TIMEOUT,
) -> dict | None:
    """
    Effectue une requête GET avec gestion du rate limiting et backoff exponentiel.
    Retourne le dict JSON ou None si échec définitif.
    """
    for attempt in range(max_retries):
        try:
            response = session.get(url, params=params, timeout=timeout)

            # Rate limiting
            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After")
                wait = float(retry_after) if retry_after else (2 ** attempt) + random.uniform(0, 1)
                logger.warning(
                    "429 Too Many Requests — attente %.1fs (tentative %d/%d)",
                    wait, attempt + 1, max_retries,
                )
                time.sleep(wait)
                continue

            # Erreurs serveur transitoires (5xx)
            if 500 <= response.status_code < 600:
                wait = (2 ** attempt) + random.uniform(0, 1)
                logger.warning(
                    "HTTP %d — retry dans %.1fs (tentative %d/%d)",
                    response.status_code, wait, attempt + 1, max_retries,
                )
                time.sleep(wait)
                continue

            response.raise_for_status()
            data = response.json()

            # ⚠️ ArcGIS renvoie ses erreurs en 200 OK avec {"error": {...}}
            if isinstance(data, dict) and "error" in data:
                err = data["error"]
                logger.error(
                    "Erreur API ArcGIS: code=%s message=%s details=%s",
                    err.get("code"), err.get("message"), err.get("details"),
                )
                return None

            return data

        except requests.Timeout:
            wait = (2 ** attempt) + random.uniform(0, 1)
            logger.warning("Timeout — retry dans %.1fs (tentative %d/%d)", wait, attempt + 1, max_retries)
            time.sleep(wait)
        except requests.RequestException as e:
            wait = (2 ** attempt) + random.uniform(0, 1)
            logger.error("Erreur réseau: %s — retry dans %.1fs (tentative %d/%d)", e, wait, attempt + 1, max_retries)
            time.sleep(wait)
        except ValueError as e:  # JSON invalide
            logger.error("JSON invalide: %s", e)
            return None

    logger.error("Échec définitif après %d tentatives pour params=%s", max_retries, params)
    return None


# ==========================================================================
# Récupération des données SITG par secteur
# ==========================================================================
def _fetch_sector(
    noga_code: str,
    *,
    since_ms: int | None = None,
    session: requests.Session | None = None,
    page_size: int = DEFAULT_PAGE_SIZE,
    inter_page_delay: float = DEFAULT_INTER_PAGE_DELAY,
) -> list[dict]:
    """Récupère toutes les entreprises d'un secteur NOGA via pagination."""
    if not re.match(r"^\d{2}$", str(noga_code)):
        logger.warning("Code NOGA invalide ignoré: %r", noga_code)
        return []

    session = session or _build_session()
    url = f"{SERVICE_URL}/query"
    results: list[dict] = []
    offset = 0

    where = f"code_noga LIKE '{noga_code}%'"
    if since_ms:
        where += f" AND (Last_Edited_Date >= {since_ms})"

    while True:
        params = {
            "where": where,
            "outFields": "*",
            "f": "json",
            "resultRecordCount": page_size,
            "resultOffset": offset,
        }

        data = _request_with_retry(session, url, params)
        if data is None:
            logger.error("Abandon secteur %s à offset %d", noga_code, offset)
            break

        features = data.get("features", [])
        if not features:
            break

        for feat in features:
            attr = {k.lower(): v for k, v in feat.get("attributes", {}).items()}
            mail = (attr.get("email") or "").strip().lower()
            nom = (attr.get("raison_sociale") or "").strip()
            if not (mail and nom):
                continue
            results.append({
                "nom": nom,
                "email": mail,
                "noga_code": noga_code,
                "id_sitg": attr.get("objectid"),
                "adresse": f"{attr.get('phys_rue', '')} {attr.get('phys_numrue', '')}".strip(),
            })

        # Détection fin de pagination
        if len(features) < page_size or data.get("exceededTransferLimit") is False:
            break

        offset += page_size
        if inter_page_delay > 0:
            time.sleep(inter_page_delay)

    return results


# ==========================================================================
# Validation des emails en parallèle
# ==========================================================================
_EMAIL_TIMEOUT = 8          # timeout par email passé à _verifier_email
_WATCHDOG_SLACK = 5         # secondes de marge supplémentaires avant de déclarer un worker mort
_PROGRESS_INTERVAL = 2.0   # rafraîchissement de la barre de progression (secondes)


def _validate_emails(candidates: list[dict], max_workers: int = DEFAULT_EMAIL_WORKERS, mx_only: bool = False) -> tuple[list[dict], list[tuple[str, str]]]:
    """
    Valide les emails en parallèle avec watchdog et barre de progression.
    Retourne (emails_valides, [(email, statut_ko), ...]).

    Si mx_only=True, ne fait que la vérification MX (pas de SMTP), ce qui
    évite le blocage du port 25 sortant sur Render free / autres hébergeurs.
    """
    valides: list[dict] = []
    rejets: list[tuple[str, str]] = []
    total = len(candidates)

    def _worker(ent: dict):
        statut, raison = _verifier_email(ent["email"], timeout=_EMAIL_TIMEOUT, mx_only=mx_only)
        return ent, statut, raison

    futures_map: dict = {}
    last_print = time.monotonic()
    done_count = 0

    def _print_progress():
        pct = done_count * 100 // total if total else 100
        print(
            f"\r  Validation email : {done_count}/{total} ({pct}%)"
            f" — {len(valides)} valides, {len(rejets)} rejetés   ",
            end="", flush=True,
        )

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        pending = {pool.submit(_worker, e): e for e in candidates}
        futures_map = dict(pending)

        while pending:
            # Attente bornée : évite le blocage si un thread est coincé dans getaddrinfo()
            watchdog = _EMAIL_TIMEOUT + _WATCHDOG_SLACK
            done_set, pending = wait(pending, timeout=watchdog, return_when=FIRST_COMPLETED)

            if not done_set:
                # Aucun future terminé dans le délai → workers coincés, on abandonne le lot
                stuck = [futures_map[f]["email"] for f in pending]
                logger.warning(
                    "Watchdog: %d worker(s) bloqué(s) après %ds — emails ignorés: %s",
                    len(stuck), watchdog, ", ".join(stuck[:5]),
                )
                break

            for fut in done_set:
                done_count += 1
                try:
                    ent, statut, _ = fut.result()
                except Exception as e:
                    logger.error("Erreur validation email %s: %s", futures_map[fut]["email"], e)
                    continue
                if statut not in STATUTS_HARD_KO:
                    valides.append(ent)
                else:
                    rejets.append((ent["email"], statut))

            now = time.monotonic()
            if done_count % 50 == 0 or now - last_print >= _PROGRESS_INTERVAL:
                _print_progress()
                last_print = now

    print()  # saut de ligne après la barre de progression
    return valides, rejets


# ==========================================================================
# Commande Django
# ==========================================================================
class Command(BaseCommand):
    help = "Synchronise le référentiel SITG (tous secteurs NOGA)"

    def add_arguments(self, parser):
        parser.add_argument("--secteurs", nargs="*", help="Codes NOGA à synchroniser (ex: 62 64). Tous par défaut.")
        parser.add_argument("--min_new", type=int, default=500, help="Seuil minimum de nouveaux pour log SUCCESS.")
        parser.add_argument("--since_hours", type=int, default=0,
                            help="Filtrer sur les N dernières heures. 0 = pas de filtre temporel (défaut).")
        parser.add_argument("--dry_run", action="store_true", help="Simulation sans écriture en base.")
        parser.add_argument("--email_workers", type=int, default=DEFAULT_EMAIL_WORKERS,
                            help="Nombre de workers pour la validation email (défaut: 5).")
        parser.add_argument("--inter_sector_delay", type=float, default=DEFAULT_INTER_SECTOR_DELAY,
                            help="Délai en secondes entre secteurs (défaut: 1.0).")
        parser.add_argument("--skip_email_check", action="store_true",
                            help="Ne pas valider les emails (utile pour tests).")
        parser.add_argument("--mx_only", action="store_true",
                            help="Validation MX uniquement (skip SMTP). "
                                 "Recommandé sur les hébergeurs qui bloquent le port 25 sortant (Render, Heroku…).")
        parser.add_argument("--verbose_api", action="store_true",
                            help="Logs détaillés des appels API.")

    def handle(self, *args, **options):
        codes_a_sync = options.get("secteurs") or list(NOGA_MAP.keys())
        since_hours = options["since_hours"]
        dry_run = options["dry_run"]
        email_workers = options["email_workers"]
        inter_sector_delay = options["inter_sector_delay"]
        skip_email_check = options["skip_email_check"]
        mx_only = options["mx_only"]

        if options["verbose_api"]:
            logging.getLogger(__name__).setLevel(logging.DEBUG)

        self.stdout.write(self.style.HTTP_INFO(
            f"Démarrage synchronisation — {len(codes_a_sync)} secteurs, "
            f"since_hours={since_hours}, dry_run={dry_run}"
        ))

        emails_existants = set(EntrepriseReferentiel.objects.values_list("email", flat=True))
        self.stdout.write(f"  → {len(emails_existants)} emails déjà en base")

        since_ms = (
            int((datetime.now() - timedelta(hours=since_hours)).timestamp() * 1000)
            if since_hours else None
        )

        candidates: list[dict] = []
        buffer_update: list[dict] = []
        seen_this_run: set[str] = set()
        stats_par_secteur: dict[str, int] = {}

        session = _build_session()

        # ----- 1. Récupération par secteur -----
        for idx, code in enumerate(codes_a_sync, 1):
            if code not in NOGA_MAP:
                self.stdout.write(self.style.WARNING(f"  ✗ Code NOGA inconnu ignoré : {code}"))
                continue

            self.stdout.write(f"  [{idx}/{len(codes_a_sync)}] Secteur {code} : {NOGA_MAP[code]}")
            entreprises = _fetch_sector(code, since_ms=since_ms, session=session)
            stats_par_secteur[code] = len(entreprises)

            for ent in entreprises:
                mail = ent["email"]
                if mail in emails_existants:
                    buffer_update.append(ent)
                elif mail not in seen_this_run:
                    seen_this_run.add(mail)
                    candidates.append(ent)

            self.stdout.write(f"      → {len(entreprises)} récupérées")

            # Throttling entre secteurs
            if inter_sector_delay > 0 and idx < len(codes_a_sync):
                time.sleep(inter_sector_delay)

        self.stdout.write(self.style.HTTP_INFO(
            f"\nRécupération terminée: {sum(stats_par_secteur.values())} entrées totales, "
            f"{len(candidates)} candidats nouveaux, {len(buffer_update)} à mettre à jour"
        ))

        # ----- 2. Validation des emails -----
        buffer_new: list[dict] = []
        if candidates and not skip_email_check:
            mode = "MX uniquement" if mx_only else "MX + SMTP"
            self.stdout.write(
                f"  → Validation email de {len(candidates)} candidats "
                f"({email_workers} workers, mode: {mode})..."
            )
            buffer_new, rejets = _validate_emails(candidates, max_workers=email_workers, mx_only=mx_only)
            self.stdout.write(f"      → {len(buffer_new)} valides, {len(rejets)} rejetés")
            for email, statut in rejets[:20]:  # log limité
                self.stdout.write(f"        ✗ {email} ({statut})")
        elif skip_email_check:
            buffer_new = candidates
            self.stdout.write(self.style.WARNING("  → Validation email IGNORÉE (--skip_email_check)"))

        # ----- 3. Écriture en base -----
        if dry_run:
            self.stdout.write(self.style.WARNING(
                f"\n[DRY RUN] +{len(buffer_new)} nouveaux, {len(buffer_update)} maj (rien écrit)."
            ))
            self._print_stats(stats_par_secteur)
            return

        with transaction.atomic():
            to_create = [
                EntrepriseReferentiel(
                    id_sitg=e["id_sitg"],
                    raison_sociale=e["nom"],
                    email=e["email"],
                    code_noga=e["noga_code"],
                    adresse=e["adresse"],
                )
                for e in buffer_new
            ]
            EntrepriseReferentiel.objects.bulk_create(to_create, ignore_conflicts=True, batch_size=500)

            for e in buffer_update[:1000]:
                EntrepriseReferentiel.objects.filter(email=e["email"]).update(
                    raison_sociale=e["nom"],
                    adresse=e["adresse"],
                )

        msg = f"Terminé: +{len(buffer_new)} nouveaux, {len(buffer_update)} maj."
        style = self.style.SUCCESS if len(buffer_new) >= options["min_new"] else self.style.HTTP_INFO
        self.stdout.write(style(msg))
        self._print_stats(stats_par_secteur)

    def _print_stats(self, stats: dict[str, int]):
        """Affiche un résumé des secteurs avec des résultats."""
        non_zero = {k: v for k, v in stats.items() if v > 0}
        if not non_zero:
            self.stdout.write(self.style.WARNING("⚠️  Aucun secteur n'a renvoyé de résultats."))
            return
        self.stdout.write(f"\nTop 10 secteurs:")
        for code, count in sorted(non_zero.items(), key=lambda x: -x[1])[:10]:
            self.stdout.write(f"  {code} ({NOGA_MAP.get(code, '?')}): {count}")