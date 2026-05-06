import logging
import re
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from django.core.management.base import BaseCommand
from django.db import transaction
from core.models import EntrepriseReferentiel
from core.management.commands.check_emails import _verifier_email, STATUTS_HARD_KO
from core.constants import NOGA_MAP, SERVICE_URL

logger = logging.getLogger(__name__)



def _fetch_sector(noga_code, since_ms=None):
    if not re.match(r'^\d{2}$', str(noga_code)):
        logger.warning("_fetch_sector: code NOGA invalide ignoré: %r", noga_code)
        return []

    API_URL = f"{SERVICE_URL}/query"
    results = []
    offset = 0
    limit = 1000

    where = f"code_noga LIKE '{noga_code}%'"
    if since_ms:
        where += f" AND (Last_Edited_Date >= {since_ms})"

    while True:
        params = {
            "where": where, "outFields": "*", "f": "json",
            "resultRecordCount": limit, "resultOffset": offset,
        }
        try:
            r = requests.get(API_URL, params=params, timeout=20)
            data = r.json()
            features = data.get("features", [])
            if not features:
                break

            for feat in features:
                attr = {k.lower(): v for k, v in feat["attributes"].items()}
                mail = (attr.get("email") or "").strip().lower()
                nom = (attr.get("raison_sociale") or "").strip()
                if mail and nom:
                    results.append({
                        "nom": nom, "email": mail, "noga_code": noga_code,
                        "id_sitg": attr.get("objectid"),
                        "adresse": f"{attr.get('phys_rue', '')} {attr.get('phys_numrue', '')}".strip(),
                    })
            if len(features) < limit:
                break
            offset += limit
        except Exception as e:
            logger.error(f"Erreur API: {e}")
            break
    return results


class Command(BaseCommand):
    help = "Synchronise le référentiel SITG (tous secteurs NOGA)"

    def add_arguments(self, parser):
        parser.add_argument("--secteurs", nargs="*", help="Codes NOGA à synchroniser (ex: 62 64). Tous par défaut.")
        parser.add_argument("--min_new", type=int, default=500, help="Seuil minimum de nouveaux avant log SUCCESS.")
        parser.add_argument("--since_hours", type=int, default=24, help="Filtrer sur les N dernières heures.")
        parser.add_argument("--dry_run", action="store_true", help="Simulation sans écriture en base.")

    def handle(self, *args, **options):
        self.stdout.write("Démarrage de la synchronisation...")

        codes_a_sync = options.get("secteurs") or list(NOGA_MAP.keys())
        since_hours = options.get("since_hours", 24)
        dry_run = options.get("dry_run", False)

        emails_existants = set(EntrepriseReferentiel.objects.values_list("email", flat=True))

        buffer_new = []
        buffer_update = []

        since_ms = int((datetime.now() - timedelta(hours=since_hours)).timestamp() * 1000) if since_hours else None

        candidates = []
        seen_this_run = set()

        for code in codes_a_sync:
            if code not in NOGA_MAP:
                self.stdout.write(self.style.WARNING(f"Code NOGA inconnu ignoré : {code}"))
                continue
            self.stdout.write(f"  → Secteur {code} : {NOGA_MAP[code]}")
            entreprises = _fetch_sector(code, since_ms=since_ms)
            for ent in entreprises:
                mail = ent["email"]
                if mail in emails_existants:
                    buffer_update.append(ent)
                elif mail not in seen_this_run:
                    seen_this_run.add(mail)
                    candidates.append(ent)

        if candidates:
            self.stdout.write(f"  → Validation email de {len(candidates)} nouveaux candidats...")

            def _validate(ent):
                statut, raison = _verifier_email(ent["email"], timeout=8)
                return ent, statut, raison

            with ThreadPoolExecutor(max_workers=20) as pool:
                futures = [pool.submit(_validate, ent) for ent in candidates]
                for fut in as_completed(futures):
                    ent, statut, _ = fut.result()
                    if statut not in STATUTS_HARD_KO:
                        buffer_new.append(ent)
                    else:
                        self.stdout.write(f"    ✗ {ent['email']} ({statut})")

        if dry_run:
            self.stdout.write(self.style.WARNING(
                f"[DRY RUN] +{len(buffer_new)} nouveaux, {len(buffer_update)} mises à jour (rien écrit)."
            ))
            return

        with transaction.atomic():
            to_create = [EntrepriseReferentiel(
                id_sitg=e["id_sitg"], raison_sociale=e["nom"],
                email=e["email"], code_noga=e["noga_code"], adresse=e["adresse"]
            ) for e in buffer_new]
            EntrepriseReferentiel.objects.bulk_create(to_create, ignore_conflicts=True)

            for e in buffer_update[:1000]:
                EntrepriseReferentiel.objects.filter(email=e["email"]).update(
                    raison_sociale=e["nom"], adresse=e["adresse"]
                )

        msg = f"Terminé: +{len(buffer_new)} nouveaux, {len(buffer_update)} maj."
        if len(buffer_new) >= options.get("min_new", 500):
            self.stdout.write(self.style.SUCCESS(msg))
        else:
            self.stdout.write(msg)
