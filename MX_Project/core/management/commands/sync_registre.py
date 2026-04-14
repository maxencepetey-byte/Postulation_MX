import logging
import requests
import logging
import requests
import dns.resolver
from datetime import datetime, timedelta
from django.core.management.base import BaseCommand
from django.db import transaction
from core.models import EntrepriseReferentiel

logger = logging.getLogger(__name__)

SERVICE_URL = "https://app2.ge.ch/tergeoservices/rest/services/Hosted/REG_ENTREPRISE_ETABLISSEMENT/MapServer/0"

NOGA_MAP = {
    # SECTEUR PRIMAIRE
    "01": "Agriculture et chasse", "02": "Sylviculture et exploitation forestière",
    "03": "Pêche et aquaculture", "05": "Extraction de houille",
    "06": "Extraction d'hydrocarbures", "07": "Extraction de minerais",
    "08": "Autres industries extractives", "09": "Soutien aux industries extractives",
    # SECTEUR SECONDAIRE
    "10": "Industrie alimentaire", "11": "Fabrication de boissons",
    "12": "Industrie du tabac", "13": "Fabrication de textiles",
    "14": "Industrie de l'habillement", "15": "Industrie du cuir",
    "16": "Travail du bois", "17": "Industrie du papier",
    "18": "Imprimerie et reproduction", "19": "Cokéfaction et raffinage",
    "20": "Industrie chimique", "21": "Industrie pharmaceutique",
    "22": "Produits en caoutchouc et plastique", "23": "Produits minéraux non métalliques",
    "24": "Métallurgie", "25": "Produits métalliques (hors machines)",
    "26": "Produits informatiques et électroniques", "27": "Équipements électriques",
    "28": "Machines et équipements n.c.a.", "29": "Industrie automobile",
    "30": "Autres matériels de transport", "31": "Fabrication de meubles",
    "32": "Autres industries manufacturières", "33": "Réparation et installation de machines",
    # ÉNERGIE & CONSTRUCTION
    "35": "Production d'électricité et gaz", "36": "Captage et distribution d'eau",
    "37": "Gestion des eaux usées", "38": "Collecte et traitement des déchets",
    "39": "Dépollution", "41": "Construction de bâtiments",
    "42": "Génie civil", "43": "Travaux de construction spécialisés",
    # TERTIAIRE
    "45": "Commerce et réparation automobile", "46": "Commerce de gros",
    "47": "Commerce de détail (incl. Luxe)", "49": "Transports terrestres",
    "50": "Transports par eau", "51": "Transports aériens",
    "52": "Entreposage et soutien aux transports", "53": "Activités de poste et de courrier",
    "55": "Hébergement", "56": "Restauration", "58": "Édition",
    "59": "Cinéma et musique", "60": "Radio et Télévision",
    "61": "Télécommunications", "62": "Informatique et programmation",
    "63": "Services d'information", "64": "Services financiers (Banques)",
    "65": "Assurances", "66": "Activités auxiliaires financières",
    "68": "Activités immobilières", "69": "Juridique et comptabilité",
    "70": "Conseil de gestion (Sièges sociaux)", "71": "Architecture et ingénierie",
    "72": "Recherche-développement", "73": "Publicité et études de marché (Marketing)",
    "74": "Activités spécialisées (Design, Photo)", "75": "Activités vétérinaires",
    "77": "Location et location-bail", "78": "Activités liées à l'emploi",
    "79": "Agences de voyage", "80": "Enquêtes et sécurité",
    "81": "Services relatifs aux bâtiments", "82": "Administration et soutien bureau",
    # SERVICES PUBLICS & HUMAINS
    "84": "Administration publique", "85": "Enseignement",
    "86": "Santé humaine", "87": "Hébergement médico-social",
    "88": "Action sociale sans hébergement", "90": "Arts et spectacles",
    "91": "Musées et culture", "92": "Jeux de hasard",
    "93": "Sport, loisirs et récréation", "94": "Activités des organisations associatives",
    "95": "Réparation d'ordinateurs et biens personnels",
    "96": "Autres services personnels (Esthétique)",
}


def _verifier_email_mx(email: str) -> bool:
    if not email:
        return False
    try:
        domaine = email.split("@")[1]
        dns.resolver.resolve(domaine, "MX")
        return True
    except Exception:
        return False


def _fetch_sector(noga_code, since_ms=None):
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

        since_ms = int((datetime.now() - timedelta(hours=since_hours)).timestamp() * 1000)

        for code in codes_a_sync:
            if code not in NOGA_MAP:
                self.stdout.write(self.style.WARNING(f"Code NOGA inconnu ignoré : {code}"))
                continue
            self.stdout.write(f"  → Secteur {code} : {NOGA_MAP[code]}")
            entreprises = _fetch_sector(code, since_ms=since_ms)
            for ent in entreprises:
                mail = ent["email"]
                if mail not in emails_existants:
                    if _verifier_email_mx(mail):
                        buffer_new.append(ent)
                        emails_existants.add(mail)
                else:
                    buffer_update.append(ent)

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
