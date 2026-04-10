import logging
import requests
import dns.resolver
from datetime import datetime, timedelta
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from core.models import EntrepriseReferentiel

logger = logging.getLogger(__name__)

SERVICE_URL = "https://app2.ge.ch/tergeoservices/rest/services/Hosted/REG_ENTREPRISE_ETABLISSEMENT/MapServer/0"

NOGA_MAP = {
    "62": "Informatique", "64": "Banque", "71": "Architecture",
    "86": "Santé", "43": "Construction", "47": "Luxe",
}

def _verifier_email_mx(email: str) -> bool:
    """Vérifie l'existence du domaine MX."""
    if not email: return False
    try:
        domaine = email.split("@")[1]
        dns.resolver.resolve(domaine, "MX")
        return True
    except Exception:
        return False

def _fetch_sector(noga_code, since_ms=None):
    """Récupère les données de l'API SITG avec pagination."""
    API_URL = f"{SERVICE_URL}/query"
    results = []
    offset = 0
    limit = 1000
    
    where = f"code_noga LIKE '{noga_code}%'"
    if since_ms:
        # On tente de filtrer par date si le serveur le supporte (Last_Edited_Date est courant sur ArcGIS)
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
            if not features: break

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
            if len(features) < limit: break
            offset += limit
        except Exception as e:
            logger.error(f"Erreur API: {e}")
            break
    return results

class Command(BaseCommand):
    help = "Synchronise le référentiel SITG"

    def handle(self, *args, **options):
        self.stdout.write("Démarrage de la synchronisation...")
        
        # 1. On récupère les emails déjà connus pour éviter les vérifs DNS inutiles
        emails_existants = set(EntrepriseReferentiel.objects.values_list("email", flat=True))
        
        buffer_new = []
        buffer_update = []
        
        # On filtre sur les dernières 24h pour la performance
        since_ms = int((datetime.now() - timedelta(hours=24)).timestamp() * 1000)

        for code in NOGA_MAP.keys():
            entreprises = _fetch_sector(code, since_ms=since_ms)
            for ent in entreprises:
                mail = ent["email"]
                if mail not in emails_existants:
                    # ON NE VÉRIFIE LE DNS QUE POUR LES NOUVEAUX
                    if _verifier_email_mx(mail):
                        buffer_new.append(ent)
                        emails_existants.add(mail)
                else:
                    buffer_update.append(ent)

        # 2. Écriture groupée (Bulk)
        with transaction.atomic():
            # Création
            to_create = [EntrepriseReferentiel(
                id_sitg=e["id_sitg"], raison_sociale=e["nom"],
                email=e["email"], code_noga=e["noga_code"], adresse=e["adresse"]
            ) for e in buffer_new]
            EntrepriseReferentiel.objects.bulk_create(to_create, ignore_conflicts=True)

            # Mise à jour (limitée aux 1000 premiers pour la rapidité)
            for e in buffer_update[:1000]:
                EntrepriseReferentiel.objects.filter(email=e["email"]).update(
                    raison_sociale=e["nom"], adresse=e["adresse"]
                )

        self.stdout.write(self.style.SUCCESS(f"Terminé: +{len(buffer_new)} nouveaux, {len(buffer_update)} maj."))