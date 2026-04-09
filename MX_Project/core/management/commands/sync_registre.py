"""
Management command: sync_registre
----------------------------------
Usage:
    python manage.py sync_registre
    python manage.py sync_registre --secteurs 62 64 71
    python manage.py sync_registre --dry-run
    python manage.py sync_registre --min-new 500

Ce script tourne en Cron Job (Render) pour synchroniser le registre SITG Genève
dans un référentiel GLOBAL (EntrepriseReferentiel).

Les utilisateurs ne sont plus synchronisés individuellement: chaque user fait des requêtes
sur le référentiel via `lancer_scan`.
"""

import logging
import requests
from datetime import datetime, timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

logger = logging.getLogger(__name__)

SERVICE_URL = (
    "https://app2.ge.ch/tergeoservices/rest/services/Hosted/"
    "REG_ENTREPRISE_ETABLISSEMENT/MapServer/0"
)

NOGA_MAP = {
    "62": "Informatique",
    "64": "Banque",
    "71": "Architecture",
    "86": "Santé",
    "43": "Construction",
    "47": "Luxe",
    "87": "Social (Héb.)",
    "88": "Social (Action)",
}


def _verifier_email(email: str) -> bool:
    """Vérifie l'existence du domaine MX (identique à views.py)."""
    import dns.resolver
    if not email:
        return False
    try:
        domaine = email.split("@")[1]
        dns.resolver.resolve(domaine, "MX")
        return True
    except Exception:
        return False


_DATE_FIELDS_CANDIDATES = [
    # Les noms exacts peuvent varier côté SITG: on essaye plusieurs.
    "date_maj",
    "date_modif",
    "date_modification",
    "date_mise_a_jour",
    "date_creation",
    "created_date",
    "editdate",
    "last_edited_date",
]


def _fetch_sector(noga_code: str, since_ms: int | None = None) -> list[dict]:
    """
    Télécharge TOUTES les entrées d'un secteur NOGA depuis le SITG.
    Pagine automatiquement par tranches de 1000.
    Retourne une liste de dicts { nom, email, adresse, secteur_activite }.
    """
    API_URL = f"{SERVICE_URL}/query"
    results = []
    offset = 0
    limit = 1000

    def _do_query(where: str) -> dict | None:
        params = {
            "where": where,
            "outFields": "*",
            "f": "json",
            "resultRecordCount": limit,
            "resultOffset": offset,
        }
        try:
            r = requests.get(API_URL, params=params, timeout=30)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            logger.warning("Erreur fetch secteur %s offset %s: %s", noga_code, offset, e)
            return None

    date_filter_where = None
    if since_ms:
        # ESRI SQL accepte typiquement un timestamp en millisecondes depuis l'époque:
        # ex: editdate >= 1712600000000
        # On ne sait pas quel champ date existe côté SITG, donc on essaie plusieurs.
        for f in _DATE_FIELDS_CANDIDATES:
            candidate = f"(code_noga LIKE '{noga_code}%') AND ({f} >= {since_ms})"
            data = _do_query(candidate)
            if not data:
                continue
            # Si le champ n'existe pas, ESRI renvoie souvent une erreur JSON contenant "error"
            if isinstance(data, dict) and data.get("error"):
                continue
            date_filter_where = candidate
            break

    where = date_filter_where or f"code_noga LIKE '{noga_code}%'"

    while True:
        data = _do_query(where)
        if not data:
            break
        if isinstance(data, dict) and data.get("error"):
            # En dernier recours, si le filtre date ne passe pas, on retombe sur le mode complet.
            if where != f"code_noga LIKE '{noga_code}%'":
                logger.warning("Filtre date refusé par SITG, fallback sans filtre (secteur=%s)", noga_code)
                where = f"code_noga LIKE '{noga_code}%'"
                offset = 0
                continue
            break

        features = data.get("features", [])
        if not features:
            break

        for feat in features:
            attr = {k.lower(): v for k, v in feat["attributes"].items()}
            mail = (attr.get("email") or "").strip().lower()
            nom = (attr.get("raison_sociale") or "").strip()
            if not mail or not nom:
                continue
            # Identifiant SITG (si présent). On stocke ce qu'on peut.
            id_sitg = (
                attr.get("objectid")
                or attr.get("id")
                or attr.get("id_sitg")
                or attr.get("identifiant")
            )
            results.append(
                {
                    "nom": nom,
                    "email": mail,
                    "adresse": f"{attr.get('phys_rue', '')} {attr.get('phys_numrue', '')}".strip(),
                    "secteur_activite": NOGA_MAP.get(noga_code[:2], "Général"),
                    "noga_code": noga_code,
                    "id_sitg": id_sitg,
                }
            )

        if len(features) < limit:
            break
        offset += limit

    return results


def _sync_referentiel(
    secteurs_codes: list[str],
    dry_run: bool = False,
    min_new: int = 0,
    since_hours: int | None = None,
) -> dict:
    """
    Synchronise le référentiel global EntrepriseReferentiel.
    - Comptabilise les nouvelles entrées (par email unique)
    - Si `min_new` est défini, n'écrit pas en base tant que le delta < min_new
    """
    from core.models import EntrepriseReferentiel

    # Emails déjà présents dans le référentiel (lookup O(1))
    emails_existants = set(EntrepriseReferentiel.objects.values_list("email", flat=True))

    stats = {"ajoutes": 0, "updates": 0, "ignores_email": 0, "total_remote": 0}
    buffer_new = []
    buffer_update = []

    since_ms = None
    if since_hours is not None:
        since_ms = int((datetime.utcnow() - timedelta(hours=since_hours)).timestamp() * 1000)

    for noga_code in secteurs_codes:
        entreprises_remote = _fetch_sector(noga_code, since_ms=since_ms)
        stats["total_remote"] += len(entreprises_remote)
        for ent in entreprises_remote:
            mail = ent["email"]
            if not _verifier_email(mail):
                stats["ignores_email"] += 1
                continue
            if mail not in emails_existants:
                stats["ajoutes"] += 1
                if not dry_run:
                    buffer_new.append(ent)
                emails_existants.add(mail)
            else:
                # on met à jour l'adresse / code noga / raison sociale (best-effort)
                stats["updates"] += 1
                if not dry_run:
                    buffer_update.append(ent)

    if dry_run:
        return stats

    if min_new and stats["ajoutes"] < min_new:
        logger.info("Delta (%d) < min_new (%d): skip write", stats["ajoutes"], min_new)
        stats["skipped_write"] = True
        return stats

    # Écriture en base en transaction
    with transaction.atomic():
        to_create = []
        for ent in buffer_new:
            to_create.append(
                EntrepriseReferentiel(
                    id_sitg=int(ent["id_sitg"]) if ent.get("id_sitg") not in (None, "") else None,
                    raison_sociale=ent["nom"],
                    email=ent["email"],
                    code_noga=str(ent.get("noga_code") or ""),
                    adresse=ent.get("adresse") or "",
                )
            )
        if to_create:
            EntrepriseReferentiel.objects.bulk_create(to_create, ignore_conflicts=True, batch_size=2000)

        # Updates: on fait un update_or_create par email (moins performant mais safe)
        for ent in buffer_update[:20000]:
            EntrepriseReferentiel.objects.update_or_create(
                email=ent["email"],
                defaults={
                    "id_sitg": int(ent["id_sitg"]) if ent.get("id_sitg") not in (None, "") else None,
                    "raison_sociale": ent["nom"],
                    "code_noga": str(ent.get("noga_code") or ""),
                    "adresse": ent.get("adresse") or "",
                },
            )

    return stats


class Command(BaseCommand):
    help = (
        "Synchronise le registre SITG Genève pour TOUS les utilisateurs. "
        "Ajoute uniquement les nouvelles entreprises (delta). "
        "Conçu pour tourner en Cron Job toutes les 24h."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--secteurs",
            nargs="+",
            type=str,
            default=list(NOGA_MAP.keys()),
            metavar="CODE",
            help=(
                "Codes NOGA à synchroniser (ex: 62 64 71). "
                f"Par défaut: tous ({', '.join(NOGA_MAP.keys())})"
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            default=False,
            help="Simule la sync sans écrire en base.",
        )
        parser.add_argument(
            "--user",
            type=str,
            default=None,
            metavar="USERNAME",
            help="Synchronise uniquement pour cet utilisateur (debug).",
        )
        parser.add_argument(
            "--min-new",
            type=int,
            default=0,
            metavar="N",
            help="N'écrit en base que si au moins N nouvelles entreprises sont détectées.",
        )
        parser.add_argument(
            "--since-hours",
            type=int,
            default=None,
            metavar="H",
            help=(
                "Filtre côté SITG: ne récupère que les entrées des H dernières heures "
                "(si le service expose un champ date compatible). Ex: 24"
            ),
        )
        parser.add_argument(
            "--parallel",
            action="store_true",
            default=False,
            help=(
                "Traite les utilisateurs en parallèle (threads). "
                "Attention: charge réseau et DB plus élevée."
            ),
        )

    def handle(self, *args, **options):
        secteurs = options["secteurs"]
        dry_run = options["dry_run"]
        min_new = options["min_new"]
        since_hours = options["since_hours"]

        # Validation des codes NOGA
        unknown = [s for s in secteurs if s[:2] not in NOGA_MAP]
        if unknown:
            raise CommandError(
                f"Codes NOGA inconnus: {unknown}. "
                f"Codes valides: {list(NOGA_MAP.keys())}"
            )

        start = datetime.now()
        mode_label = "[DRY-RUN] " if dry_run else ""
        self.stdout.write(
            self.style.SUCCESS(
                f"\n{mode_label}=== sync_registre démarré à {start.strftime('%Y-%m-%d %H:%M:%S')} ==="
            )
        )
        self.stdout.write(
            f"Secteurs: {', '.join(NOGA_MAP.get(s[:2], s) for s in secteurs)}\n"
        )

        totaux = _sync_referentiel(secteurs, dry_run=dry_run, min_new=min_new, since_hours=since_hours)

        elapsed = (datetime.now() - start).total_seconds()
        self.stdout.write(
            self.style.SUCCESS(
                f"\n{mode_label}=== Terminé en {elapsed:.1f}s ===\n"
                f"Total remote: {totaux.get('total_remote', 0)} | "
                f"+{totaux.get('ajoutes', 0)} nouvelles | "
                f"{totaux.get('updates', 0)} updates | "
                f"{totaux.get('ignores_email', 0)} emails invalides ignorés\n"
            )
        )
