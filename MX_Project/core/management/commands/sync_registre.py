"""Commande : python manage.py sync_registre — synchronise EntrepriseReferentiel depuis le registre GE."""

import logging
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from datetime import timedelta

import requests
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.core.validators import validate_email
from django.db import connection, transaction
from django.utils import timezone

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

# Seuls champs lus : sans cette liste (et returnGeometry=false), l'API renvoie 35 champs et la géométrie.
CHAMPS_SITG = "raison_sociale,email,code_noga,phys_rue,phys_numrue,phys_npa,phys_localite"
CHAMPS_MIS_A_JOUR = ["raison_sociale", "code_noga", "adresse", "npa", "localite", "radiee", "absente_depuis"]
# Divisions présentes au registre mais pas dans NOGA_MAP (organisations extraterritoriales…).
SECTEURS_HORS_CARTE = ("97", "98", "99")
# Le registre compte environ 100 000 lignes : au-delà, le nombre annoncé est aberrant.
MAX_LIGNES_PAR_SECTEUR = 200_000

# Garde-fou : au-delà de cette part de la base absente du registre, c'est la publication du
# registre qui est suspecte, pas les entreprises. Rien n'est alors marqué radié.
PART_MAX_RADIEES = 0.3
MIN_POUR_GARDE_FOU = 50
# Une entreprise n'est radiée que si elle manque encore à une synchro ultérieure (le cron est quotidien).
DELAI_CONFIRMATION_RADIATION = timedelta(hours=20)
# Au-delà, la validation des adresses n'a pas vraiment eu lieu : la synchro est signalée en échec.
PART_MAX_NON_VERIFIEES = 0.05
MIN_NON_VERIFIEES = 10


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


def _attente(attempt: int, retry_after: str | None = None) -> float:
    # Retry-After peut être une date HTTP : seul le nombre de secondes est exploité.
    if retry_after and retry_after.strip().isascii() and retry_after.strip().isdigit():
        return min(float(retry_after), 120.0)
    return (2 ** attempt) + random.uniform(0, 1)


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

            if response.status_code == 429:
                delai = _attente(attempt, response.headers.get("Retry-After"))
                logger.warning(
                    "429 Too Many Requests — attente %.1fs (tentative %d/%d)",
                    delai, attempt + 1, max_retries,
                )
                time.sleep(delai)
                continue

            if 500 <= response.status_code < 600:
                delai = _attente(attempt)
                logger.warning(
                    "HTTP %d — retry dans %.1fs (tentative %d/%d)",
                    response.status_code, delai, attempt + 1, max_retries,
                )
                time.sleep(delai)
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
            delai = _attente(attempt)
            logger.warning("Timeout — retry dans %.1fs (tentative %d/%d)", delai, attempt + 1, max_retries)
            time.sleep(delai)
        except requests.HTTPError as e:
            # 4xx : la requête elle-même est refusée, la répéter ne changera rien.
            logger.error("Requête refusée: %s", e)
            return None
        except requests.RequestException as e:
            delai = _attente(attempt)
            logger.error("Erreur réseau: %s — retry dans %.1fs (tentative %d/%d)", e, delai, attempt + 1, max_retries)
            time.sleep(delai)
        except ValueError as e:  # JSON invalide
            logger.error("JSON invalide: %s", e)
            return None

    logger.error("Échec définitif après %d tentatives pour params=%s", max_retries, params)
    return None


# ==========================================================================
# Récupération des données SITG par secteur
# ==========================================================================
_SEPARATEURS_EMAIL = re.compile(r"[;,/\s]+")


def _texte(valeur, longueur: int | None = None) -> str:
    """Champ du registre ramené à un texte que Postgres accepte (ni caractère NUL, ni surrogate isolé)."""
    if valeur is None:
        return ""
    texte = str(valeur).replace("\x00", "").encode("utf-8", "ignore").decode("utf-8").strip()
    return texte[:longueur] if longueur else texte


def _premier_email(valeur) -> str:
    """Première adresse valide du champ, qui en contient parfois plusieurs ; "" s'il n'y en a pas."""
    for morceau in _SEPARATEURS_EMAIL.split(_texte(valeur).lower()):
        # 254 : longueur maximale d'EmailField ; au-delà, Postgres refuserait tout le lot.
        if not morceau or len(morceau) > 254:
            continue
        try:
            validate_email(morceau)
        except ValidationError:
            continue
        return morceau
    return ""


def _fetch_sector(
    noga_code: str,
    *,
    session: requests.Session | None = None,
    page_size: int = DEFAULT_PAGE_SIZE,
    inter_page_delay: float = DEFAULT_INTER_PAGE_DELAY,
) -> tuple[list[dict], bool]:
    """
    Récupère les entreprises d'un secteur NOGA qui ont un e-mail.
    Retourne (entreprises, complet). `complet` est faux si une page a échoué ou si le nombre
    de lignes reçues diffère du nombre annoncé par le registre : la liste est alors partielle.
    """
    if not re.fullmatch(r"[0-9]{2}", str(noga_code)):
        logger.warning("Code NOGA invalide ignoré: %r", noga_code)
        return [], False

    session = session or _build_session()
    url = f"{SERVICE_URL}/query"
    where = f"code_noga LIKE '{noga_code}%' AND email IS NOT NULL AND email <> ''"

    compte = _request_with_retry(session, url, {"where": where, "returnCountOnly": "true", "f": "json"})
    attendu = compte.get("count") if compte else None
    if not isinstance(attendu, int) or not 0 <= attendu <= MAX_LIGNES_PAR_SECTEUR:
        logger.error("Secteur %s : nombre de lignes indisponible ou aberrant (%r)", noga_code, attendu)
        return [], False

    results: list[dict] = []
    recues = 0
    offset = 0

    while recues < attendu:
        data = _request_with_retry(session, url, {
            "where": where,
            "outFields": CHAMPS_SITG,
            "returnGeometry": "false",
            # Sans tri, l'ordre peut changer d'une page à l'autre : lignes en double ou sautées.
            "orderByFields": "objectid",
            "f": "json",
            "resultRecordCount": page_size,
            "resultOffset": offset,
        })
        if data is None:
            logger.error("Abandon secteur %s à offset %d", noga_code, offset)
            return results, False

        features = data.get("features", [])
        if not features:
            break

        for feat in features:
            attr = {k.lower(): v for k, v in feat.get("attributes", {}).items()}
            mail = _premier_email(attr.get("email"))
            nom = _texte(attr.get("raison_sociale"), 255)
            if not (mail and nom):
                continue
            results.append({
                "nom": nom,
                "email": mail,
                "noga_code": noga_code,
                "adresse": _texte(f"{_texte(attr.get('phys_rue'))} {_texte(attr.get('phys_numrue'))}", 255),
                "npa": _texte(attr.get("phys_npa"), 10),
                "localite": _texte(attr.get("phys_localite"), 100),
            })

        # Le serveur peut plafonner la page sous page_size : on avance du nombre réellement reçu.
        recues += len(features)
        offset += len(features)
        if recues < attendu and inter_page_delay > 0:
            time.sleep(inter_page_delay)

    if recues != attendu:
        logger.error("Secteur %s incomplet : %d lignes reçues sur %d annoncées", noga_code, recues, attendu)
        return results, False
    return results, True


# ==========================================================================
# Validation des emails en parallèle
# ==========================================================================
_EMAIL_TIMEOUT = 8          # timeout par email passé à _verifier_email
_WATCHDOG_SLACK = 5         # secondes de marge supplémentaires avant de déclarer un worker mort
_PROGRESS_INTERVAL = 2.0   # rafraîchissement de la barre de progression (secondes)
_MAX_RELANCES = 10          # chaque relance abandonne les threads coincés : on borne leur nombre


def _validate_emails(
    candidates: list[dict], max_workers: int = DEFAULT_EMAIL_WORKERS, mx_only: bool = False,
) -> tuple[list[dict], list[tuple[str, str]], list[dict]]:
    """
    Valide les emails en parallèle avec watchdog et barre de progression.
    Retourne (valides, [(email, statut_ko), ...], non_verifies).

    Un worker coincé (résolution DNS sans délai) ne fait perdre que son adresse : les adresses
    pas encore commencées repartent dans un nouveau groupe de workers. Les non vérifiées ne sont
    pas enregistrées et seront revues à la synchro suivante.

    Si mx_only=True, ne fait que la vérification MX (pas de SMTP), ce qui
    évite le blocage du port 25 sortant sur Render free / autres hébergeurs.
    """
    valides: list[dict] = []
    rejets: list[tuple[str, str]] = []
    non_verifies: list[dict] = []
    total = len(candidates)
    done_count = 0
    last_print = time.monotonic()

    def _worker(ent: dict):
        statut, raison = _verifier_email(ent["email"], timeout=_EMAIL_TIMEOUT, mx_only=mx_only)
        return ent, statut, raison

    def _print_progress():
        pct = done_count * 100 // total if total else 100
        print(
            f"\r  Validation email : {done_count}/{total} ({pct}%)"
            f" — {len(valides)} valides, {len(rejets)} rejetés   ",
            end="", flush=True,
        )

    a_faire = list(candidates)
    relances = 0
    watchdog = _EMAIL_TIMEOUT + _WATCHDOG_SLACK

    while a_faire:
        pool = ThreadPoolExecutor(max_workers=max_workers)
        futures_map = {pool.submit(_worker, e): e for e in a_faire}
        pending = set(futures_map)
        a_faire = []
        try:
            while pending:
                # Attente bornée : évite le blocage si un thread est coincé dans getaddrinfo()
                done_set, pending = wait(pending, timeout=watchdog, return_when=FIRST_COMPLETED)

                if not done_set:
                    done_set = {f for f in pending if f.done()}
                    pending -= done_set
                if not done_set:
                    relances += 1
                    coinces = []
                    for fut in pending:
                        # cancel() ne réussit que sur une tâche pas encore commencée.
                        if fut.cancel() and relances <= _MAX_RELANCES:
                            a_faire.append(futures_map[fut])
                        else:
                            coinces.append(futures_map[fut])
                    non_verifies.extend(coinces)
                    logger.warning(
                        "Watchdog: %d adresse(s) non vérifiée(s) après %ds (%s), %d relancée(s)",
                        len(coinces), watchdog, ", ".join(e["email"] for e in coinces[:5]), len(a_faire),
                    )
                    break

                for fut in done_set:
                    done_count += 1
                    try:
                        ent, statut, _ = fut.result()
                    except Exception as e:
                        logger.error("Erreur validation email %s: %s", futures_map[fut]["email"], e)
                        non_verifies.append(futures_map[fut])
                        continue
                    if statut not in STATUTS_HARD_KO:
                        valides.append(ent)
                    else:
                        rejets.append((ent["email"], statut))

                now = time.monotonic()
                if done_count % 50 == 0 or now - last_print >= _PROGRESS_INTERVAL:
                    _print_progress()
                    last_print = now
        finally:
            # wait=False : attendre ici reviendrait à attendre les threads coincés.
            pool.shutdown(wait=False, cancel_futures=True)

    print()  # saut de ligne après la barre de progression
    return valides, rejets, non_verifies


# ==========================================================================
# Choix de l'établissement retenu pour une adresse e-mail
# ==========================================================================
def _cle(ent: dict) -> tuple:
    """Ordre stable entre établissements d'une même adresse (l'OBJECTID, lui, change à chaque publication)."""
    return (ent["nom"].casefold(), ent["noga_code"], ent["adresse"].casefold(), ent["npa"], ent["localite"])


def _choisir(etablissements: list[dict], actuel, registre_entier: bool) -> dict | None:
    """
    Établissement à recopier sur une ligne existante ; None si la ligne est déjà à jour.
    Tant que l'entreprise enregistrée figure encore au registre sous cette adresse, on la garde :
    sans cela, la raison sociale d'une adresse partagée changerait d'une nuit à l'autre.
    """
    nom, noga, adresse, npa, localite, radiee, absente_depuis = actuel
    a_retablir = radiee or absente_depuis is not None
    inchange = {"nom": nom, "noga_code": noga, "adresse": adresse, "npa": npa, "localite": localite}
    for e in etablissements:
        if all(e[champ] == valeur for champ, valeur in inchange.items()):
            return e if a_retablir else None
    meme_nom = [e for e in etablissements if e["nom"].casefold() == (nom or "").casefold()]
    if meme_nom:
        return min(meme_nom, key=_cle)
    # L'entreprise enregistrée n'apparaît plus sous cette adresse. En lecture partielle, elle peut
    # simplement se trouver dans un secteur non lu : on ne la remplace pas par une autre.
    if not registre_entier:
        return inchange if a_retablir else None
    return min(etablissements, key=_cle)


# ==========================================================================
# Commande Django
# ==========================================================================
class Command(BaseCommand):
    help = "Synchronise le référentiel SITG (tous secteurs NOGA)"

    def add_arguments(self, parser):
        parser.add_argument("--secteurs", nargs="*", help="Codes NOGA à synchroniser (ex: 62 64). Tous par défaut.")
        parser.add_argument("--min_new", type=int, default=500, help="Seuil minimum de nouveaux pour log SUCCESS.")
        parser.add_argument("--since_hours", type=int, default=0,
                            help="Sans effet : le registre ne publie pas de date de modification.")
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
        dry_run = options["dry_run"]
        email_workers = options["email_workers"]
        inter_sector_delay = options["inter_sector_delay"]
        skip_email_check = options["skip_email_check"]
        mx_only = options["mx_only"]

        if options["verbose_api"]:
            logging.getLogger(__name__).setLevel(logging.DEBUG)

        self.stdout.write(self.style.HTTP_INFO(
            f"Démarrage synchronisation — {len(codes_a_sync)} secteurs, dry_run={dry_run}"
        ))
        if options["since_hours"]:
            # Le filtre sur Last_Edited_Date faisait échouer chaque secteur : ce champ n'existe pas.
            self.stdout.write(self.style.WARNING(
                "  ⚠️  --since_hours ignoré : le registre ne publie pas de date de modification."
            ))

        # e-mail normalisé → (pk, raison_sociale, code_noga, adresse, npa, localite, radiee, absente_depuis)
        existants: dict[str, tuple] = {}
        # Toutes les lignes, pour décider de la radiation : (pk, e-mail normalisé, radiee, absente_depuis)
        lignes: list[tuple] = []
        for pk, email, nom, noga, adresse, npa, localite, radiee, absente_depuis in (
            EntrepriseReferentiel.objects.values_list(
                "pk", "email", "raison_sociale", "code_noga", "adresse", "npa", "localite", "radiee", "absente_depuis",
            )
        ):
            cle = email.strip().lower()
            lignes.append((pk, cle, radiee, absente_depuis))
            # D'anciennes lignes gardent un e-mail avec majuscules, parfois en double d'une ligne
            # normalisée : comparées telles quelles au registre, elles passeraient pour absentes.
            if cle not in existants or email == cle:
                existants[cle] = (pk, nom, noga, adresse, npa, localite, radiee, absente_depuis)
        self.stdout.write(f"  → {len(lignes)} emails déjà en base")

        # Une même adresse sert souvent à plusieurs établissements (fiduciaires, groupes) :
        # on les rassemble pour n'en retenir qu'un, toujours le même d'une synchro à l'autre.
        registre: dict[str, list[dict]] = {}
        stats_par_secteur: dict[str, int] = {}
        secteurs_incomplets: list[str] = []

        session = _build_session()

        # ----- 1. Récupération par secteur -----
        for idx, code in enumerate(codes_a_sync, 1):
            if code not in NOGA_MAP:
                self.stdout.write(self.style.WARNING(f"  ✗ Code NOGA inconnu ignoré : {code}"))
                continue

            self.stdout.write(f"  [{idx}/{len(codes_a_sync)}] Secteur {code} : {NOGA_MAP[code]}")
            entreprises, complet = _fetch_sector(code, session=session)
            stats_par_secteur[code] = len(entreprises)
            if not complet:
                secteurs_incomplets.append(code)
            for ent in entreprises:
                registre.setdefault(ent["email"], []).append(ent)

            suffixe = "" if complet else self.style.ERROR("  (INCOMPLET)")
            self.stdout.write(f"      → {len(entreprises)} récupérées{suffixe}")

            if inter_sector_delay > 0 and idx < len(codes_a_sync):
                time.sleep(inter_sector_delay)

        # Divisions du registre absentes de NOGA_MAP : lues seulement pour savoir si une adresse
        # y figure encore, sans quoi une entreprise reclassée là passerait pour radiée.
        tous_les_secteurs = set(NOGA_MAP) <= set(codes_a_sync)
        presentes_ailleurs: set[str] = set()
        if tous_les_secteurs:
            for code in SECTEURS_HORS_CARTE:
                entreprises, complet = _fetch_sector(code, session=session)
                if not complet:
                    secteurs_incomplets.append(code)
                presentes_ailleurs.update(e["email"] for e in entreprises)
        registre_entier = tous_les_secteurs and not secteurs_incomplets

        candidates: list[dict] = []
        a_mettre_a_jour: list[EntrepriseReferentiel] = []
        for mail, etablissements in registre.items():
            ligne = existants.get(mail)
            if ligne is None:
                candidates.append(min(etablissements, key=_cle))
                continue
            pk, *actuel = ligne
            ent = _choisir(etablissements, actuel, registre_entier)
            if ent is not None:
                a_mettre_a_jour.append(EntrepriseReferentiel(
                    pk=pk, raison_sociale=ent["nom"], code_noga=ent["noga_code"], adresse=ent["adresse"],
                    npa=ent["npa"], localite=ent["localite"], radiee=False, absente_depuis=None,
                ))

        self.stdout.write(self.style.HTTP_INFO(
            f"\nRécupération terminée: {sum(stats_par_secteur.values())} entrées totales, "
            f"{len(candidates)} candidats nouveaux, {len(a_mettre_a_jour)} à mettre à jour"
        ))

        # ----- 2. Validation des emails -----
        buffer_new: list[dict] = []
        non_verifies: list[dict] = []
        if candidates and not skip_email_check:
            mode = "MX uniquement" if mx_only else "MX + SMTP"
            self.stdout.write(
                f"  → Validation email de {len(candidates)} candidats "
                f"({email_workers} workers, mode: {mode})..."
            )
            buffer_new, rejets, non_verifies = _validate_emails(candidates, max_workers=email_workers, mx_only=mx_only)
            self.stdout.write(
                f"      → {len(buffer_new)} valides, {len(rejets)} rejetés, {len(non_verifies)} non vérifiés"
            )
            for email, statut in rejets[:20]:  # log limité
                self.stdout.write(f"        ✗ {email} ({statut})")
        elif skip_email_check:
            buffer_new = candidates
            self.stdout.write(self.style.WARNING("  → Validation email IGNORÉE (--skip_email_check)"))

        # ----- 3. Entreprises disparues du registre -----
        # Le registre ne liste que les entreprises « En activité » : une radiée en disparaît.
        # On ne conclut que si tout le registre a été lu, sans trou, et seulement à la deuxième
        # synchro où l'adresse manque : une publication incomplète d'une nuit ne radie personne.
        maintenant = timezone.now()
        nouvelles_absentes: list[int] = []
        a_radier: list[int] = []
        radiation_suspecte = False
        if registre_entier:
            actives = 0
            for pk, cle, radiee, absente_depuis in lignes:
                if radiee:
                    continue
                actives += 1
                if cle in registre or cle in presentes_ailleurs:
                    continue
                if absente_depuis is None:
                    nouvelles_absentes.append(pk)
                elif absente_depuis <= maintenant - DELAI_CONFIRMATION_RADIATION:
                    a_radier.append(pk)
            absentes = len(nouvelles_absentes) + len(a_radier)
            if actives >= MIN_POUR_GARDE_FOU and absentes > PART_MAX_RADIEES * actives:
                logger.error("%d entreprises sur %d absentes du registre : rien n'est marqué", absentes, actives)
                nouvelles_absentes, a_radier, radiation_suspecte = [], [], True

        # ----- 4. Écriture en base -----
        if dry_run:
            self.stdout.write(self.style.WARNING(
                f"\n[DRY RUN] +{len(buffer_new)} nouveaux, {len(a_mettre_a_jour)} maj, "
                f"{len(nouvelles_absentes)} absentes à confirmer, {len(a_radier)} radiées (rien écrit)."
            ))
        else:
            # La récupération et la validation peuvent durer plusieurs minutes : la connexion ouverte
            # au début a pu être coupée par le serveur (« AdminShutdown »). Django en rouvrira une.
            # Sauf dans une transaction (tests) : la fermer annulerait tout ce qui précède.
            if not connection.in_atomic_block:
                connection.close()
            with transaction.atomic():
                # id_sitg reste vide : l'OBJECTID du registre est réattribué à chaque republication,
                # et sa contrainte d'unicité faisait rejeter en silence des entreprises nouvelles.
                EntrepriseReferentiel.objects.bulk_create([
                    EntrepriseReferentiel(
                        raison_sociale=e["nom"], email=e["email"], code_noga=e["noga_code"],
                        adresse=e["adresse"], npa=e["npa"], localite=e["localite"],
                    )
                    for e in buffer_new
                ], ignore_conflicts=True, batch_size=500)
                EntrepriseReferentiel.objects.bulk_update(a_mettre_a_jour, CHAMPS_MIS_A_JOUR, batch_size=500)
                for debut in range(0, len(nouvelles_absentes), 1000):
                    EntrepriseReferentiel.objects.filter(
                        pk__in=nouvelles_absentes[debut:debut + 1000],
                    ).update(absente_depuis=maintenant)
                for debut in range(0, len(a_radier), 1000):
                    EntrepriseReferentiel.objects.filter(pk__in=a_radier[debut:debut + 1000]).update(radiee=True)
            msg = (
                f"Terminé: +{len(buffer_new)} nouveaux, {len(a_mettre_a_jour)} maj, "
                f"{len(nouvelles_absentes)} absente(s) à confirmer, {len(a_radier)} radiée(s)."
            )
            style = self.style.SUCCESS if len(buffer_new) >= options["min_new"] else self.style.HTTP_INFO
            self.stdout.write(style(msg))

        self._print_stats(stats_par_secteur)

        # ----- 5. Bilan : une synchro partielle doit faire échouer le cron, pas passer au vert -----
        problemes = []
        if secteurs_incomplets:
            problemes.append(f"secteurs incomplets : {', '.join(secteurs_incomplets)}")
        if radiation_suspecte:
            problemes.append("trop d'entreprises absentes du registre pour les marquer")
        if not any(stats_par_secteur.values()):
            problemes.append("aucun secteur n'a renvoyé de résultat")
        # Port 25 filtré sans --mx_only : les vérifications dépassent le watchdog et rien n'entre.
        if len(non_verifies) > max(MIN_NON_VERIFIEES, PART_MAX_NON_VERIFIEES * len(candidates)):
            problemes.append(
                f"{len(non_verifies)} adresses sur {len(candidates)} non vérifiées (essayer --mx_only)"
            )
        if problemes:
            raise CommandError("Synchronisation partielle — " + " ; ".join(problemes))

    def _print_stats(self, stats: dict[str, int]):
        """Affiche un résumé des secteurs avec des résultats."""
        non_zero = {k: v for k, v in stats.items() if v > 0}
        if not non_zero:
            self.stdout.write(self.style.WARNING("⚠️  Aucun secteur n'a renvoyé de résultats."))
            return
        self.stdout.write("\nTop 10 secteurs:")
        for code, count in sorted(non_zero.items(), key=lambda x: -x[1])[:10]:
            self.stdout.write(f"  {code} ({NOGA_MAP.get(code, '?')}): {count}")
