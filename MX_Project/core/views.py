import io
import zipfile
import requests
import dns.resolver
from datetime import date
import math
import os
import threading
import json
import re
import base64
import secrets
from datetime import timedelta
from urllib.parse import urlencode
import zipfile
import unicodedata

from django.shortcuts import render, redirect, get_object_or_404
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth import login, logout
from django.core.paginator import Paginator
from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseForbidden
from django.utils.timezone import now
from django.db import IntegrityError
from django.db.models import Max
from django.views.decorators.http import require_POST, require_GET
from django.http import JsonResponse
from django.template.loader import render_to_string
from django.contrib.staticfiles import finders
from django.utils import timezone
from decouple import config
from django.contrib import messages
import logging
from django.db import transaction
from django.db import connection
from django.core.management import call_command

from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.platypus import Paragraph, Spacer, Frame
from reportlab.lib.styles import getSampleStyleSheet
from django.core.files.base import ContentFile

from .models import (
    EntrepriseCible,
    Recherche,
    DocumentUtilisateur,
    ProfilUtilisateur,
    ScanSession,
    LettreSecteurTemplate,
    GmailOAuthToken,
)
from .forms import ProfilForm

logger = logging.getLogger(__name__)
SERVICE_URL = "https://app2.ge.ch/tergeoservices/rest/services/Hosted/REG_ENTREPRISE_ETABLISSEMENT/MapServer/0"


# ---------------------------------------------------------------------------
# Delete helpers (storage-safe)
# ---------------------------------------------------------------------------
def _delete_all_user_documents(user) -> int:
    """
    Supprime les DocumentUtilisateur + les fichiers physiques associés.
    Retourne le nombre de documents supprimés.
    """
    docs = list(DocumentUtilisateur.objects.filter(utilisateur=user).only("id", "fichier"))
    file_names = []
    for d in docs:
        try:
            if getattr(d, "fichier", None) and getattr(d.fichier, "name", ""):
                file_names.append(d.fichier.name)
        except Exception:
            continue

    # 1) DB d'abord: garantit disparition immédiate du dashboard
    # IMPORTANT: ancienne table `core_lmmapping` (prod) peut référencer pack_doc_id
    # et empêcher la suppression via FK. On purge cette table si elle existe.
    try:
        tables = set(connection.introspection.table_names())
        if "core_lmmapping" in tables and file_names:
            doc_ids = list(DocumentUtilisateur.objects.filter(utilisateur=user).values_list("id", flat=True))
            if doc_ids:
                with connection.cursor() as cur:
                    cur.execute("DELETE FROM core_lmmapping WHERE pack_doc_id = ANY(%s)", [doc_ids])
    except Exception:
        pass

    DocumentUtilisateur.objects.filter(utilisateur=user).delete()

    # 2) Fichiers ensuite (best-effort)
    try:
        from django.core.files.storage import default_storage

        for name in file_names:
            try:
                default_storage.delete(name)
            except Exception:
                continue
    except Exception:
        pass

    return len(docs)


# ---------------------------------------------------------------------------
# LM filename by email (source of truth)
# ---------------------------------------------------------------------------
def _email_to_pdf_name(email: str) -> str:
    """
    Convertit une adresse email en nom de fichier PDF déterministe.
    Même email → toujours même nom de fichier → 0 matching flou nécessaire.
    """
    import unicodedata as _ud

    e = (email or "").strip().lower()
    # Normaliser les accents éventuels
    e = _ud.normalize("NFKD", e)
    e = "".join(c for c in e if not _ud.combining(c))
    # Garder uniquement les caractères valides dans un nom de fichier
    e = re.sub(r"[^a-z0-9@._+\-]", "_", e)
    e = e.replace("@", "_AT_")
    e = re.sub(r"_+", "_", e).strip("_")
    return f"LM_{e}.pdf"

NOGA_MAP = {
    '62': 'Informatique',
    '64': 'Banque',
    '71': 'Architecture',
    '86': 'Santé',
    '43': 'Construction',
    '47': 'Luxe',
    '87': 'Social (Héb.)',
    '88': 'Social (Action)',
}


# ---------------------------------------------------------------------------
# UTILS
# ---------------------------------------------------------------------------

def verifier_email_existence(email):
    if not email:
        return False
    try:
        domaine = email.split('@')[1]
        dns.resolver.resolve(domaine, 'MX')
        return True
    except Exception:
        return False


def get_accroche(profil, secteur_activite):
    """Retourne la phrase d'accroche adaptée au secteur — utilisée partout."""
    mapping = {
        'Informatique': profil.phrase_informatique,
        'Banque':        profil.phrase_banque,
        'Luxe':          profil.phrase_luxe,
        'Architecture':  "votre vision architecturale et la qualité de vos réalisations",
        'Santé':         "votre engagement dans les soins et le bien-être des patients",
        'Construction':  "votre expertise technique et vos projets d'envergure",
    }
    if secteur_activite and 'Social' in secteur_activite:
        return "votre engagement quotidien dans l'accompagnement et l'impact social de vos projets"
    return mapping.get(secteur_activite, profil.phrase_generale)


# ---------------------------------------------------------------------------
# AUTHENTIFICATION
# ---------------------------------------------------------------------------

def register(request):
    if request.method == 'POST':
        form = UserCreationForm(request.POST)
        if form.is_valid():
            user = form.save()
            login(request, user)
            return redirect('dashboard')
    else:
        form = UserCreationForm()
    return render(request, 'registration/register.html', {'form': form})


@require_POST
def logout_view(request):
    logout(request)
    return redirect('login')

def _google_oauth_config():
    # Le projet utilise python-decouple pour lire `.env`
    client_id = (config("GOOGLE_CLIENT_ID", default="") or "").strip()
    client_secret = (config("GOOGLE_CLIENT_SECRET", default="") or "").strip()
    redirect_uri = (config("GOOGLE_REDIRECT_URI", default="") or "").strip()
    return client_id, client_secret, redirect_uri


@login_required
def gmail_connect(request):
    client_id, _client_secret, redirect_uri = _google_oauth_config()
    if not client_id or not redirect_uri:
        return HttpResponse(
            "Config OAuth Gmail manquante. Vérifie `GOOGLE_CLIENT_ID` et `GOOGLE_REDIRECT_URI` dans `.env`, puis redémarre le serveur.",
            status=500,
            content_type="text/plain; charset=utf-8",
        )

    state = secrets.token_urlsafe(24)
    request.session["gmail_oauth_state"] = state

    scope = "https://www.googleapis.com/auth/gmail.compose"
    qs = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": scope,
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
            "state": state,
        }
    )
    auth_url = f"https://accounts.google.com/o/oauth2/v2/auth?{qs}"
    return redirect(auth_url)


@login_required
def gmail_callback(request):
    code = (request.GET.get("code") or "").strip()
    state = (request.GET.get("state") or "").strip()
    expected_state = request.session.get("gmail_oauth_state")
    request.session.pop("gmail_oauth_state", None)

    if not code or not expected_state or state != expected_state:
        return redirect("settings_page")

    client_id, client_secret, redirect_uri = _google_oauth_config()
    if not client_id or not client_secret or not redirect_uri:
        return redirect("settings_page")

    token_url = "https://oauth2.googleapis.com/token"
    data = {
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }
    r = requests.post(token_url, data=data, timeout=30)
    if r.status_code >= 400:
        return redirect("settings_page")
    payload = r.json()

    refresh_token = (payload.get("refresh_token") or "").strip()
    access_token = (payload.get("access_token") or "").strip()
    expires_in = payload.get("expires_in")
    scope = (payload.get("scope") or "").strip()
    token_type = (payload.get("token_type") or "").strip()

    if not refresh_token:
        # Google ne renvoie pas toujours refresh_token si déjà consenti.
        # On garde l'existant si présent.
        existing = GmailOAuthToken.objects.filter(utilisateur=request.user).first()
        if existing:
            refresh_token = existing.refresh_token
        else:
            return redirect("settings_page")

    expires_at = None
    try:
        if expires_in:
            expires_at = timezone.now() + timedelta(seconds=int(expires_in))
    except Exception:
        expires_at = None

    tok, _ = GmailOAuthToken.objects.get_or_create(utilisateur=request.user, defaults={"refresh_token": refresh_token})
    tok.refresh_token = refresh_token
    tok.access_token = access_token
    tok.expires_at = expires_at
    tok.scope = scope
    tok.token_type = token_type
    tok.save()

    return redirect("settings_page")


@login_required
@require_POST
def gmail_disconnect(request):
    GmailOAuthToken.objects.filter(utilisateur=request.user).delete()
    return redirect("settings_page")


def _gmail_get_access_token(user) -> str:
    tok = GmailOAuthToken.objects.filter(utilisateur=user).first()
    if not tok:
        raise RuntimeError("Gmail not connected")

    if tok.access_token and tok.expires_at and tok.expires_at > timezone.now() + timedelta(seconds=30):
        return tok.access_token

    client_id, client_secret, _redirect_uri = _google_oauth_config()
    if not client_id or not client_secret:
        raise RuntimeError("Missing Google OAuth server config")

    token_url = "https://oauth2.googleapis.com/token"
    data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": tok.refresh_token,
        "grant_type": "refresh_token",
    }
    r = requests.post(token_url, data=data, timeout=30)
    if r.status_code >= 400:
        raise RuntimeError(f"Token refresh failed {r.status_code}: {r.text[:200]}")
    payload = r.json()

    tok.access_token = (payload.get("access_token") or "").strip()
    expires_in = payload.get("expires_in")
    tok.token_type = (payload.get("token_type") or tok.token_type or "").strip()
    try:
        if expires_in:
            tok.expires_at = timezone.now() + timedelta(seconds=int(expires_in))
    except Exception:
        tok.expires_at = None
    tok.save(update_fields=["access_token", "expires_at", "token_type", "updated_at"])
    return tok.access_token

def _run_scan_for_user(user, secteurs):
    """
    Lance un scan en arrière-plan pour un user (sans request).
    """
    if not secteurs:
        return

    noms_secteurs = [NOGA_MAP.get(s[:2], s) for s in secteurs]

    session = ScanSession.objects.create(
        utilisateur=user,
        secteurs=", ".join(noms_secteurs),
    )

    recherche, _ = Recherche.objects.get_or_create(
        utilisateur=user, secteur_noga="SCAN_GENEVE"
    )

    API_URL = f"{SERVICE_URL}/query"
    total_ajoutes = 0
    total_doublons = 0
    total_user_initial = EntrepriseCible.objects.filter(utilisateur=user).count()

    for s in secteurs:
        offset = 0
        limit = 1000

        while True:
            params = {
                "where": f"code_noga LIKE '{s}%'",
                "outFields": "*",
                "f": "json",
                "resultRecordCount": limit,
                "resultOffset": offset,
            }
            try:
                r = requests.get(API_URL, params=params, timeout=20).json()
                features = r.get("features", [])
                if not features:
                    break

                for feat in features:
                    attr = {k.lower(): v for k, v in feat["attributes"].items()}
                    nom = attr.get("raison_sociale") or ""
                    mail = (attr.get("email") or "").strip()
                    if not mail or not verifier_email_existence(mail):
                        continue

                    total_courant = total_user_initial + total_ajoutes
                    pack_id = (total_courant // 500) + 1

                    try:
                        EntrepriseCible.objects.create(
                            recherche=recherche,
                            scan_session=session,
                            utilisateur=user,
                            nom=nom,
                            email=mail,
                            numero_pack=pack_id,
                            secteur_activite=NOGA_MAP.get(s[:2], "Général"),
                            adresse=f"{attr.get('phys_rue', '')} {attr.get('phys_numrue', '')}".strip(),
                        )
                        total_ajoutes += 1
                    except IntegrityError:
                        total_doublons += 1

                if len(features) < limit:
                    break
                offset += limit
            except Exception:
                break

    session.nb_entreprises = total_ajoutes
    session.nb_doublons_evites = total_doublons
    session.save()


@login_required
def onboarding(request):
    profil, _ = ProfilUtilisateur.objects.get_or_create(user=request.user)
    if profil.onboarding_done:
        return redirect("settings_page")

    secteurs = [
        ("62", "Informatique (62)"),
        ("71", "Architecture (71)"),
        ("64", "Banque (64)"),
        ("86", "Santé (86)"),
        ("43", "Construction (43)"),
        ("47", "Horlogerie/Luxe (47)"),
        ("88", "Social (88)"),
        ("87", "Hébergement (87)"),
    ]

    if request.method == "POST":
        choix = request.POST.getlist("secteurs")
        profil.onboarding_done = True
        profil.onboarding_secteurs = ",".join(choix)
        profil.save(update_fields=["onboarding_done", "onboarding_secteurs"])

        t = threading.Thread(
            target=_run_scan_for_user,
            args=(request.user, choix),
            daemon=True,
        )
        t.start()
        return redirect("settings_page")

    return render(request, "core/onboarding.html", {"secteurs": secteurs})


# ---------------------------------------------------------------------------
# DASHBOARD
# ---------------------------------------------------------------------------

@login_required
def dashboard(request):
    profil, _ = ProfilUtilisateur.objects.get_or_create(user=request.user)
    if not profil.onboarding_done and ScanSession.objects.filter(utilisateur=request.user).count() == 0:
        return redirect("onboarding")
    # profil obligatoire après onboarding
    if profil.onboarding_done and (not profil.prenom_lm or not profil.nom_lm or not profil.email_lm):
        return redirect("settings_page")
    entreprises_list = EntrepriseCible.objects.filter(
        utilisateur=request.user
    ).order_by('-id')
    total_entreprises = entreprises_list.count()
    paginator = Paginator(entreprises_list, 50)
    page_obj = paginator.get_page(request.GET.get('page'))
    tous_les_docs = DocumentUtilisateur.objects.filter(utilisateur=request.user).order_by("-date_upload")
    sessions = ScanSession.objects.filter(utilisateur=request.user)[:5]
    secteurs_uniques = list(
        EntrepriseCible.objects.filter(
            utilisateur=request.user,
            scan_session__isnull=False,
        )
        .exclude(secteur_activite__isnull=True)
        .exclude(secteur_activite="")
        .values_list("secteur_activite", flat=True)
        .distinct()
        .order_by("secteur_activite")
    )
    # packs: affichés dynamiquement selon le secteur sélectionné (via AJAX)

    static_version = None
    try:
        p = finders.find("js/scan-history.min.js")
        if p:
            static_version = int(os.path.getmtime(p))
    except Exception:
        static_version = None

    return render(request, 'core/dashboard.html', {
        'entreprises': page_obj,
        'total_entreprises': total_entreprises,
        'tous_les_docs': tous_les_docs,
        'sessions_recentes': sessions,
        'secteurs_uniques': secteurs_uniques,
        "gmail_connected": GmailOAuthToken.objects.filter(utilisateur=request.user).exists(),
        'static_version': static_version,
    })


@login_required
def entreprises_filtrer_secteur(request):
    secteur = (request.GET.get("secteur") or "").strip()
    qs = EntrepriseCible.objects.filter(
        utilisateur=request.user,
        scan_session__isnull=False,
    ).order_by("-id")

    if secteur:
        qs = qs.filter(secteur_activite=secteur)

    paginator = Paginator(qs, 50)
    page_obj = paginator.get_page(request.GET.get("page"))
    tbody_html = render_to_string(
        "partials/entreprises_table.html",
        {"entreprises": page_obj},
        request=request,
    )

    pack_infos = []
    if secteur:
        qs_pack_all = EntrepriseCible.objects.filter(
            utilisateur=request.user,
            secteur_activite=secteur,
        ).exclude(email="")

        qs_pack_remaining = qs_pack_all.filter(est_dans_paquet=False)

        max_pack = qs_pack_all.aggregate(m=Max("numero_pack")).get("m") or 0
        secteur_clean = secteur.replace(" ", "_").replace("/", "-")
        docs = {
            d.nom_affichage: d
            for d in DocumentUtilisateur.objects.filter(
                utilisateur=request.user,
                type_doc="PACK_LM",
                secteur_nom=secteur,
            )
        }
        for i in range(1, int(max_pack) + 1):
            nom_base = f"MX_SCAN_{secteur_clean}_PACK_{i}"
            doc = docs.get(nom_base)
            total_cnt = qs_pack_all.filter(numero_pack=i).count()
            remaining_cnt = qs_pack_remaining.filter(numero_pack=i).count()

            # On affiche le pack s'il y a des entreprises dedans OU s'il existe déjà un ZIP sauvegardé.
            if not total_cnt and not doc:
                continue
            pack_infos.append(
                {
                    "pack_num": i,
                    "count": total_cnt,
                    "remaining": remaining_cnt,
                    "secteur": secteur,
                    "doc_url": (doc.fichier.url if doc else ""),
                    "is_used": bool(getattr(doc, "used_for_gmail", False)) if doc else False,
                }
            )

    packs_html = render_to_string(
        "partials/packs_cards.html",
        {"pack_infos": pack_infos},
        request=request,
    )

    accept = request.headers.get("Accept", "")
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    wants_json = "application/json" in accept
    if is_ajax or wants_json:
        return JsonResponse({"tbody": tbody_html, "packs": packs_html})

    return render(request, "partials/entreprises_table.html", {"entreprises": page_obj})


@login_required
def settings_page(request):
    profil, _ = ProfilUtilisateur.objects.get_or_create(user=request.user)
    secteurs_codes = [c for c in (profil.onboarding_secteurs or "").split(",") if c]
    required_fields = ["prenom_lm", "nom_lm", "email_lm"]

    # Secteurs disponibles pour templates (avec bouton "Général")
    secteurs_templates = ["Général"] + sorted(set(list(NOGA_MAP.values())))
    default_template_secteur = "Général"
    templates_qs = LettreSecteurTemplate.objects.filter(utilisateur=request.user)
    templates_by_secteur = {t.secteur_nom: t for t in templates_qs}
    templates_json = json.dumps(
        {
            t.secteur_nom: {
                "objet": t.objet,
                "salutation": t.salutation,
                "paragraph_1": t.paragraph_1,
                "paragraph_2": t.paragraph_2,
                "paragraph_3": t.paragraph_3,
                "paragraph_4": t.paragraph_4,
                "conclusion": t.conclusion,
            }
            for t in templates_qs
        },
        ensure_ascii=False,
    )

    if request.method == 'POST':
        form = ProfilForm(request.POST, instance=profil, required_fields=required_fields)
        if form.is_valid():
            form.save()
            # Sauvegarde template lettre pour le secteur sélectionné
            secteur_tpl = (request.POST.get("template_secteur") or default_template_secteur).strip()
            if secteur_tpl:
                tpl, _ = LettreSecteurTemplate.objects.get_or_create(
                    utilisateur=request.user,
                    secteur_nom=secteur_tpl,
                )
                tpl.objet = (request.POST.get("objet") or "").strip()
                tpl.salutation = (request.POST.get("introduction") or "").strip()
                tpl.paragraph_1 = (request.POST.get("paragraph_1") or "").strip()
                tpl.paragraph_2 = (request.POST.get("paragraph_2") or "").strip()
                tpl.paragraph_3 = (request.POST.get("paragraph_3") or "").strip()
                tpl.paragraph_4 = (request.POST.get("paragraph_4") or "").strip()
                tpl.conclusion = (request.POST.get("salutation") or "").strip()
                tpl.save()
            return redirect('dashboard')
    else:
        form = ProfilForm(instance=profil, required_fields=required_fields)
    return render(request, 'core/settings.html', {
        'form': form,
        'secteurs_templates': secteurs_templates,
        'templates_json': templates_json,
        'default_template_secteur': default_template_secteur,
        "gmail_connected": GmailOAuthToken.objects.filter(utilisateur=request.user).exists(),
    })


def _safe_format(text: str, ctx: dict) -> str:
    if not text:
        return ""
    try:
        return text.format_map(ctx)
    except Exception:
        return text


def _slugify_loose(s: str) -> str:
    s = (s or "").lower()
    # ligatures fréquentes en français
    s = s.replace("œ", "oe").replace("æ", "ae")
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"[\s_]+", " ", s).strip()
    s = re.sub(r"[^a-z0-9 ]+", "", s)
    return s.replace(" ", "_")


def _read_filefield_bytes(ff) -> bytes:
    try:
        ff.open("rb")
    except Exception:
        pass
    try:
        try:
            ff.seek(0)
        except Exception:
            pass
        return ff.read()
    finally:
        try:
            ff.close()
        except Exception:
            pass


def _lm_from_pack_zip_bytes(zip_bytes: bytes, ent_name: str) -> tuple[str, bytes] | None:
    needle = _slugify_loose(ent_name)
    # tokens pour un matching robuste (ignore stopwords/forme juridique)
    stop = {
        "sa",
        "sarl",
        "gmbh",
        "suisse",
        "geneve",
        "genève",
        "des",
        "de",
        "du",
        "la",
        "le",
        "les",
        "et",
        "the",
        "a",
    }
    tokens = [t for t in needle.split("_") if t and t not in stop]
    tokens_long = [t for t in tokens if len(t) >= 3]
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
            # Cherche le meilleur PDF du pack (nom proche du nom entreprise)
            best: tuple[int, int, str] | None = None  # (score, token_hits, filename)
            for name in zf.namelist():
                base = os.path.basename(name)
                if not base.lower().endswith(".pdf"):
                    continue
                base_slug = _slugify_loose(base)

                # 1) match direct (ancien comportement)
                if needle and needle in base_slug:
                    return base, zf.read(name)

                # 2) match sans underscores (ex: "pcshop" vs "pc_shop")
                if needle and needle.replace("_", "") in base_slug.replace("_", ""):
                    return base, zf.read(name)

                # 3) scoring par recouvrement de tokens (tolérant)
                if tokens_long:
                    compact = base_slug.replace("_", "")
                    hits = sum(1 for t in tokens_long if t in compact)
                    if hits <= 0:
                        continue
                    # score: privilégie + de hits, puis plus proche de la longueur du nom
                    score = hits * 100 - abs(len(compact) - len(needle.replace("_", "")))
                    cand = (score, hits, name)
                    if best is None or cand > best:
                        best = cand

            # seuil: au moins 2 tokens matchés (ou 1 si un seul token significatif)
            if best is not None:
                _score, hits, best_name = best
                min_hits = 2 if len(tokens_long) >= 2 else 1
                if hits >= min_hits:
                    return os.path.basename(best_name), zf.read(best_name)
    except Exception:
        return None

    return None


def _lm_candidates_from_pack_zip_bytes(zip_bytes: bytes, ent_name: str, limit: int = 5) -> list[str]:
    """
    Renvoie une liste de noms de PDF "candidats" (top N) pour debug UX.
    """
    needle = _slugify_loose(ent_name)
    stop = {
        "sa",
        "sarl",
        "gmbh",
        "suisse",
        "geneve",
        "genève",
        "des",
        "de",
        "du",
        "la",
        "le",
        "les",
        "et",
        "the",
        "a",
    }
    tokens = [t for t in needle.split("_") if t and t not in stop]
    tokens_long = [t for t in tokens if len(t) >= 3]
    scored: list[tuple[int, int, str]] = []
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
            for name in zf.namelist():
                base = os.path.basename(name)
                if not base.lower().endswith(".pdf"):
                    continue
                base_slug = _slugify_loose(base)
                compact = base_slug.replace("_", "")
                hits = 0
                if tokens_long:
                    hits = sum(1 for t in tokens_long if t in compact)
                if hits <= 0:
                    continue
                score = hits * 100 - abs(len(compact) - len(needle.replace("_", "")))
                scored.append((score, hits, base))
    except Exception:
        return []

    scored.sort(reverse=True)
    out: list[str] = []
    for score, hits, base in scored[: max(1, limit)]:
        out.append(f"{base} (match {hits})")
    return out


def _lm_from_any_pack_zip(user, ent_name: str, secteur_nom: str | None) -> tuple[str, bytes] | None:
    """
    Cherche la LM dans tous les ZIP `PACK_LM` (du plus récent au plus ancien).
    Si un secteur est fourni, on priorise les packs taggés avec ce secteur.
    """
    qs = DocumentUtilisateur.objects.filter(utilisateur=user, type_doc="PACK_LM").order_by("-date_upload")
    if secteur_nom:
        qs = qs.order_by()  # reset ordering for union-like concat (Django limitation)
        preferred = DocumentUtilisateur.objects.filter(
            utilisateur=user, type_doc="PACK_LM", secteur_nom=secteur_nom
        ).order_by("-date_upload")
        fallback = DocumentUtilisateur.objects.filter(utilisateur=user, type_doc="PACK_LM").exclude(
            secteur_nom=secteur_nom
        ).order_by("-date_upload")
        packs = list(preferred) + list(fallback)
    else:
        packs = list(qs)

    for pack in packs:
        try:
            zip_bytes = _read_filefield_bytes(pack.fichier)
        except Exception:
            continue
        if not zip_bytes:
            continue
        found = _lm_from_pack_zip_bytes(zip_bytes, ent_name)
        if found:
            return found
    return None


# ---------------------------------------------------------------------------
# HISTORIQUE
# ---------------------------------------------------------------------------

@login_required
def historique_scans(request):
    """Liste de toutes les sessions de scan de l'utilisateur."""
    sessions = ScanSession.objects.filter(utilisateur=request.user)
    return render(request, 'core/historique.html', {'sessions': sessions})


@login_required
def detail_scan(request, session_id):
    """Détail d'une session de scan : liste des entreprises trouvées."""
    session = get_object_or_404(ScanSession, id=session_id, utilisateur=request.user)
    entreprises_list = session.entreprises.all().order_by('secteur_activite', 'nom')
    paginator = Paginator(entreprises_list, 50)
    page_obj = paginator.get_page(request.GET.get('page'))
    return render(request, 'core/detail_scan.html', {
        'session': session,
        'entreprises': page_obj,
    })


# ---------------------------------------------------------------------------
# SCAN
# ---------------------------------------------------------------------------

@login_required
def lancer_scan(request):
    secteurs = request.GET.getlist('secteurs')
    secteur_libre = request.GET.get('secteur_libre', '').strip()
    if secteur_libre:
        secteurs.append(secteur_libre)

    if not secteurs:
        return redirect('dashboard')

    noms_secteurs = [NOGA_MAP.get(s[:2], s) for s in secteurs]

    # Création de la session de scan
    session = ScanSession.objects.create(
        utilisateur=request.user,
        secteurs=', '.join(noms_secteurs),
    )

    # Compatibilité avec l'ancien modèle Recherche
    recherche, _ = Recherche.objects.get_or_create(
        utilisateur=request.user, secteur_noga="SCAN_GENEVE"
    )

    # On ne requête plus le SITG ici: on utilise le référentiel global.
    from core.models import EntrepriseReferentiel

    total_ajoutes = 0
    total_doublons = 0
    # Numérotation par secteur: chaque secteur redémarre à Pack 1.
    # On garde une base par secteur pour rester stable avec les anciens scans.
    base_par_secteur: dict[str, int] = {}
    ajoutes_par_secteur: dict[str, int] = {}

    for s in secteurs:
        secteur_nom = NOGA_MAP.get(s[:2], "Général")
        if secteur_nom not in base_par_secteur:
            base_par_secteur[secteur_nom] = EntrepriseCible.objects.filter(
                utilisateur=request.user,
                secteur_activite=secteur_nom,
            ).count()
            ajoutes_par_secteur[secteur_nom] = 0
        # Entreprises du référentiel pour ce secteur (code NOGA préfixe)
        qs = (
            EntrepriseReferentiel.objects.filter(code_noga__startswith=s)
            .only("raison_sociale", "email", "adresse")
            .order_by("raison_sociale")
        )

        for ref in qs.iterator(chunk_size=2000):
            # Numéro de pack par secteur (tranches de 500)
            total_courant_secteur = base_par_secteur[secteur_nom] + ajoutes_par_secteur[secteur_nom]
            pack_id = (total_courant_secteur // 500) + 1
            try:
                EntrepriseCible.objects.create(
                    recherche=recherche,
                    scan_session=session,
                    utilisateur=request.user,
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

    # Mise à jour des compteurs de la session
    session.nb_entreprises = total_ajoutes
    session.nb_doublons_evites = total_doublons
    session.save()

    # Après scan: on pré-sélectionne le secteur dans le dashboard (historique + packs)
    secteur_default = (noms_secteurs[0] if noms_secteurs else "").strip()
    if secteur_default:
        return redirect(f"/?{urlencode({'secteur': secteur_default})}")
    return redirect('dashboard')


# ---------------------------------------------------------------------------
# CRON (SYNC RÉFÉRENTIEL GLOBAL)
# ---------------------------------------------------------------------------

@require_GET
def cron_sync_registre(request):
    """
    Endpoint appelé par cron-job.org pour lancer `sync_registre`.
    Protection: token dans l'URL (?token=...).

    Lance le sync en arrière-plan pour éviter les timeouts HTTP.
    """
    token = (request.GET.get("token") or "").strip()
    expected = (getattr(settings, "CRON_SYNC_TOKEN", "") or "").strip()
    if not expected or token != expected:
        return HttpResponseForbidden("Forbidden")

    secteurs = request.GET.getlist("secteurs")  # ex: ?secteurs=62&secteurs=64
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

    def _run():
        try:
            kwargs = {"min_new": min_new, "dry_run": dry_run, "since_hours": since_hours}
            if secteurs:
                kwargs["secteurs"] = secteurs
            call_command("sync_registre", **kwargs)
            logger.info(
                "cron_sync_registre finished (secteurs=%s, min_new=%s, since_hours=%s, dry_run=%s)",
                secteurs,
                min_new,
                since_hours,
                dry_run,
            )
        except Exception:
            logger.exception("cron_sync_registre failed")

    threading.Thread(target=_run, daemon=True).start()
    return JsonResponse(
        {"status": "started", "secteurs": secteurs, "min_new": min_new, "since_hours": since_hours, "dry_run": dry_run},
        status=202,
    )

    import os
import threading
from django.http import HttpResponse, HttpResponseForbidden
from django.core.management import call_command

def cron_sync_view(request):
    # Sécurité par Token
    token_recu = request.GET.get('token')
    token_attendu = os.environ.get('CRON_SYNC_TOKEN')

    if not token_recu or token_recu != token_attendu:
        return HttpResponseForbidden("Token invalide.")

    # Lancement du scan dans un thread séparé (Arrière-plan)
    def run_task():
        # Appelle la commande de management définie plus haut
        call_command('sync_registre')

    thread = threading.Thread(target=run_task)
    thread.start()

    # Réponse immédiate (Render ne coupera pas la connexion)
    return HttpResponse("Scan démarré en tâche de fond.", status=200)


# ---------------------------------------------------------------------------
# GÉNÉRATION PDF
# ---------------------------------------------------------------------------

def generer_pdf_lm(profil, ent):
    buffer = io.BytesIO()
    p = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4
    styles = getSampleStyleSheet()

    style_corps = styles["Normal"]
    style_corps.fontName = "Helvetica"
    style_corps.fontSize = 11
    style_corps.leading = 15

    p.setFont("Helvetica-Bold", 11)
    p.drawString(2 * cm, height - 2 * cm, f"{profil.prenom_lm or ''} {profil.nom_lm or ''}")
    p.setFont("Helvetica", 10)
    p.drawString(2 * cm, height - 2.6 * cm, profil.rue or "")
    p.drawString(2 * cm, height - 3.1 * cm, f"{profil.npa or ''} {profil.ville or ''}")
    p.drawString(2 * cm, height - 3.6 * cm, profil.email_lm or "")

    p.setFont("Helvetica-Bold", 11)
    p.drawString(12 * cm, height - 5 * cm, ent.nom)
    p.setFont("Helvetica", 10)
    p.drawString(12 * cm, height - 5.5 * cm, (ent.adresse or '')[:40])

    p.drawRightString(
        width - 2 * cm, height - 8.5 * cm,
        f"Fait à {profil.ville or 'Genève'}, le {date.today().strftime('%d.%m.%Y')}"
    )

    accroche = get_accroche(profil, ent.secteur_activite)
    secteur_nom = (ent.secteur_activite or "Général").strip()
    tpl = (
        LettreSecteurTemplate.objects.filter(utilisateur=ent.utilisateur, secteur_nom=secteur_nom).first()
        or LettreSecteurTemplate.objects.filter(utilisateur=ent.utilisateur, secteur_nom="Général").first()
    )

    ctx = {
        "accroche": accroche,
        "entreprise": ent.nom,
        "secteur": secteur_nom,
        "ville": profil.ville or "Genève",
        "prenom": profil.prenom_lm or "",
        "nom": profil.nom_lm or "",
    }

    objet = None
    if tpl:
        objet = _safe_format(tpl.objet, ctx).strip()
    if not objet:
        objet = "Candidature spontanée"

    elements = [
        Paragraph(f"<b>Objet : {objet}</b>", styles["Normal"]),
        Spacer(1, 25),
    ]

    if tpl and (tpl.salutation or tpl.paragraph_1 or tpl.paragraph_2 or tpl.paragraph_3 or tpl.paragraph_4 or tpl.conclusion):
        introduction = _safe_format(tpl.salutation or "Madame, Monsieur,", ctx)
        elements.append(Paragraph(introduction, style_corps))
        elements.append(Spacer(1, 15))
        for txt in [tpl.paragraph_1, tpl.paragraph_2, tpl.paragraph_3, tpl.paragraph_4]:
            txt = _safe_format(txt, ctx).strip()
            if not txt:
                continue
            elements.append(Paragraph(txt, style_corps))
            elements.append(Spacer(1, 12))
        elements.append(Spacer(1, 10))
        salutation = _safe_format(
            (tpl.conclusion or "Je vous prie d'agréer, Madame, Monsieur, l'expression de mes salutations distinguées."),
            ctx,
        ).strip()
        if salutation:
            elements.append(Paragraph(salutation, style_corps))
    else:
        elements.extend([
            Paragraph("Madame, Monsieur,", style_corps),
            Spacer(1, 15),
            Paragraph(
                f"C'est avec un vif intérêt que je me permets de vous adresser ma candidature. "
                f"En effet, je suis particulièrement attiré par {accroche}.",
                style_corps,
            ),
            Spacer(1, 12),
            Paragraph(
                "Souhaitant intégrer une structure dynamique telle que la vôtre, je suis convaincu "
                "que mon expérience et ma motivation sauront répondre à vos exigences.",
                style_corps,
            ),
            Spacer(1, 12),
            Paragraph(
                "Vous trouverez ci-joint mon dossier complet. Je reste à votre entière disposition "
                "pour un entretien afin de vous exposer plus en détail mes motivations.",
                style_corps,
            ),
            Spacer(1, 25),
            Paragraph(
                "Je vous prie d'agréer, Madame, Monsieur, l'expression de mes salutations distinguées.",
                style_corps,
            ),
        ])

    f = Frame(2 * cm, 4 * cm, 17 * cm, height - 11.5 * cm, showBoundary=0)
    f.addFromList(elements, p)

    signature = f"{profil.prenom_lm or ''} {profil.nom_lm or ''}".strip()
    if signature:
        p.setFont("Helvetica-Bold", 11)
        # Alignée à la largeur du bloc texte (même marge droite) et remontée.
        p.drawRightString(width - 2 * cm, 6 * cm, signature)
    p.save()
    buffer.seek(0)
    return buffer.read()


# ---------------------------------------------------------------------------
# TÉLÉCHARGEMENTS ZIP
# ---------------------------------------------------------------------------

def _generer_zip(profil, entreprises):
    """Helper commun pour générer un ZIP en mémoire."""
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w') as zf:
        for ent in entreprises:
            pdf = generer_pdf_lm(profil, ent)
            nom = _email_to_pdf_name(ent.email)  # clé = email, pas nom entreprise
            zf.writestr(nom, pdf)
            ent.est_dans_paquet = True
            ent.date_traitement = now()
            ent.save()
    zip_buffer.seek(0)
    return zip_buffer.read()


@login_required
def telecharger_toutes_lm(request):
    entreprises = list(EntrepriseCible.objects.filter(
        utilisateur=request.user,
        est_dans_paquet=False,
    ).exclude(email="")[:500])

    if not entreprises:
        return redirect('dashboard')

    profil, _ = ProfilUtilisateur.objects.get_or_create(user=request.user)
    zip_bytes = _generer_zip(profil, entreprises)

    response = HttpResponse(zip_bytes, content_type='application/zip')
    response['Content-Disposition'] = 'attachment; filename="Pack_Candidatures_MX.zip"'
    return response


@login_required
@require_POST
def generer_pack_500_lm(request):
    """
    Génère un pack de 500 LM (ZIP) et l'enregistre comme DocumentUtilisateur.
    Les cartes "Pack X" sur le dashboard s'affichent uniquement après génération.
    """
    secteur = (request.POST.get("secteur") or "").strip()
    qs = EntrepriseCible.objects.filter(
        utilisateur=request.user,
        est_dans_paquet=False,
    ).exclude(email="").order_by("id")
    if secteur:
        qs = qs.filter(secteur_activite=secteur)

    entreprises = list(
        qs[:500]
    )

    if not entreprises:
        return redirect('dashboard')

    profil, _ = ProfilUtilisateur.objects.get_or_create(user=request.user)
    zip_bytes = _generer_zip(profil, entreprises)

    packs_user_qs = DocumentUtilisateur.objects.filter(
        utilisateur=request.user,
        type_doc='PACK_LM',
    )
    if secteur:
        packs_user_qs = packs_user_qs.filter(secteur_nom=secteur)
    pack_num = packs_user_qs.count() + 1

    if secteur:
        secteur_clean = secteur.replace(" ", "_").replace("/", "-")
        nom_base = f"MX_SCAN_{secteur_clean}_PACK_{pack_num}"
    else:
        nom_base = f"MX_PACK_{pack_num}"
    nom_zip = f"{nom_base}.zip"

    doc = DocumentUtilisateur(
        utilisateur=request.user,
        nom_affichage=nom_base,
        type_doc='PACK_LM',
        secteur_nom=secteur or "MULTI",
    )
    doc.fichier.save(nom_zip, ContentFile(zip_bytes), save=True)

    return redirect('dashboard')


@login_required
@require_POST
def generer_pack_secteur_numero(request, pack_num: int):
    """
    Génère UN pack (par secteur) à la demande quand l'utilisateur clique sur "Pack N".
    Numérotation redémarre à 1 pour chaque secteur.
    """
    secteur = (request.POST.get("secteur") or "").strip()
    if not secteur or pack_num < 1:
        return redirect("dashboard")

    entreprises = list(
        EntrepriseCible.objects.filter(
            utilisateur=request.user,
            est_dans_paquet=False,
            secteur_activite=secteur,
            numero_pack=pack_num,
        )
        .exclude(email="")
        .order_by("id")[:500]
    )
    if not entreprises:
        return redirect("dashboard")

    profil, _ = ProfilUtilisateur.objects.get_or_create(user=request.user)
    zip_bytes = _generer_zip(profil, entreprises)

    secteur_clean = secteur.replace(" ", "_").replace("/", "-")
    nom_base = f"MX_SCAN_{secteur_clean}_PACK_{pack_num}"
    nom_zip = f"{nom_base}.zip"

    # Si le pack existe déjà: on ne le supprime pas / ne le régénère pas.
    existing = DocumentUtilisateur.objects.filter(
        utilisateur=request.user,
        type_doc="PACK_LM",
        secteur_nom=secteur,
        nom_affichage=nom_base,
    ).first()
    if existing:
        messages.info(request, "Pack déjà généré.")
        return redirect(f"/?{urlencode({'secteur': secteur})}")

    doc = DocumentUtilisateur(
        utilisateur=request.user,
        nom_affichage=nom_base,
        type_doc="PACK_LM",
        secteur_nom=secteur,
    )
    doc.fichier.save(nom_zip, ContentFile(zip_bytes), save=True)
    messages.success(request, "Pack généré et ajouté à tes documents.")
    return redirect(f"/?{urlencode({'secteur': secteur})}")


@login_required
def telecharger_pack_specifique(request, pack_num):
    entreprises = list(EntrepriseCible.objects.filter(
        utilisateur=request.user,
        numero_pack=pack_num,
        est_dans_paquet=False,
    ).exclude(email=""))

    if not entreprises:
        return redirect('dashboard')

    profil, _ = ProfilUtilisateur.objects.get_or_create(user=request.user)
    premier_secteur = entreprises[0].secteur_activite or 'General'
    secteur_clean = premier_secteur.replace(' ', '_').replace('/', '-')
    nom_base = f"MX_SCAN_{secteur_clean}_PACK_{pack_num}"
    nom_zip = f"{nom_base}.zip"

    zip_bytes = _generer_zip(profil, entreprises)

    # Sauvegarde en base sans écrire sur disque
    doc = DocumentUtilisateur(
        utilisateur=request.user,
        nom_affichage=nom_base,
        type_doc='PACK_LM',
        secteur_nom=premier_secteur,
    )
    doc.fichier.save(nom_zip, ContentFile(zip_bytes), save=True)

    response = HttpResponse(zip_bytes, content_type='application/zip')
    response['Content-Disposition'] = f'attachment; filename="{nom_zip}"'
    return response


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("utf-8").rstrip("=")


def _build_mime_message(to_email: str, subject: str, body: str, attachments: list[tuple[str, bytes, str]]) -> bytes:
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.set_content(body or "")

    for filename, content, mime in attachments:
        if not content:
            continue
        maintype, subtype = (mime.split("/", 1) + ["octet-stream"])[:2]
        msg.add_attachment(content, maintype=maintype, subtype=subtype, filename=filename)

    return msg.as_bytes()


def _gmail_create_draft(access_token: str, raw_mime_bytes: bytes) -> None:
    url = "https://gmail.googleapis.com/gmail/v1/users/me/drafts"
    payload = {"message": {"raw": _b64url(raw_mime_bytes)}}
    r = requests.post(url, json=payload, headers={"Authorization": f"Bearer {access_token}"}, timeout=30)
    if r.status_code >= 400:
        raise RuntimeError(f"Gmail API error {r.status_code}: {r.text[:300]}")


@login_required
@require_POST
def creer_brouillons_gmail(request):
    """
    Crée jusqu'à 500 brouillons Gmail (API) pour les entreprises non traitées.
    """
    try:
        access_token = _gmail_get_access_token(request.user)
    except Exception as e:
        messages.error(
            request,
            "Connexion Gmail invalide ou expirée. Merci de reconnecter Gmail dans la page Réglages.",
        )
        return redirect("settings_page")

    secteur = (request.POST.get("secteur") or "").strip()
    if not secteur:
        messages.error(request, "Sélectionne d’abord un secteur sur le dashboard avant de créer des brouillons Gmail.")
        return redirect("dashboard")

    profil, _ = ProfilUtilisateur.objects.get_or_create(user=request.user)

    pack_doc = (
        DocumentUtilisateur.objects.filter(
            utilisateur=request.user,
            type_doc="PACK_LM",
            secteur_nom=secteur,
        )
        .order_by("-date_upload")
        .first()
    )
    if not pack_doc:
        messages.error(
            request,
            f"Aucun pack de LM trouvé pour le secteur '{secteur}'. Génère d’abord un Pack 500 (ZIP) pour ce secteur.",
        )
        return redirect("dashboard")

    try:
        pack_zip_bytes = _read_filefield_bytes(pack_doc.fichier)
    except Exception:
        messages.error(request, "Impossible de lire le ZIP du pack (storage). Ré-ouvre/ré-upload le pack puis réessaye.")
        return redirect("dashboard")

    cv_doc = (
        DocumentUtilisateur.objects.filter(utilisateur=request.user, type_doc="CV")
        .order_by("-date_upload")
        .first()
    )
    if not cv_doc:
        messages.error(request, "Aucun CV trouvé. Ajoute un document de type CV avant de créer des brouillons.")
        return redirect("dashboard")

    other_docs = list(
        DocumentUtilisateur.objects.filter(utilisateur=request.user)
        .exclude(type_doc__in=["PACK_LM"])
        .exclude(id=cv_doc.id)
        .order_by("-date_upload")
    )

    try:
        cv_bytes = cv_doc.fichier.read()
    except Exception:
        messages.error(request, "Impossible de lire ton CV (storage). Ré-uploade le fichier puis réessaye.")
        return redirect("dashboard")

    other_attachments: list[tuple[str, bytes, str]] = []
    for d in other_docs:
        try:
            other_attachments.append((os.path.basename(d.fichier.name), d.fichier.read(), "application/pdf"))
        except Exception:
            continue

    entreprises = list(
        EntrepriseCible.objects.filter(
            utilisateur=request.user,
            est_dans_paquet=False,
        )
        .filter(secteur_activite=secteur)
        .exclude(email="")
        .order_by("id")[:500]
    )
    if not entreprises:
        messages.info(request, "Aucune entreprise à traiter (toutes déjà traitées ou sans email).")
        return redirect("dashboard")

    created = 0
    skipped: list[str] = []
    for ent in entreprises:
        secteur_nom = (ent.secteur_activite or "Général").strip()
        tpl = (
            LettreSecteurTemplate.objects.filter(utilisateur=request.user, secteur_nom=secteur_nom).first()
            or LettreSecteurTemplate.objects.filter(utilisateur=request.user, secteur_nom="Général").first()
        )

        accroche = get_accroche(profil, ent.secteur_activite)
        ctx = {
            "accroche": accroche,
            "entreprise": ent.nom,
            "secteur": secteur_nom,
            "ville": profil.ville or "Genève",
            "prenom": profil.prenom_lm or "",
            "nom": profil.nom_lm or "",
        }

        base_subject = _safe_format((tpl.objet if tpl else "") or "Candidature spontanée", ctx).strip()
        subject = f"{base_subject} — {ent.nom}".strip()

        if tpl and (tpl.salutation or tpl.paragraph_1 or tpl.paragraph_2 or tpl.paragraph_3 or tpl.paragraph_4 or tpl.conclusion):
            intro = _safe_format(tpl.salutation or "Madame, Monsieur,", ctx).strip()
            paras = [
                _safe_format(tpl.paragraph_1, ctx).strip(),
                _safe_format(tpl.paragraph_2, ctx).strip(),
                _safe_format(tpl.paragraph_3, ctx).strip(),
                _safe_format(tpl.paragraph_4, ctx).strip(),
            ]
            closing = _safe_format(
                tpl.conclusion or "Je vous prie d'agréer, Madame, Monsieur, l'expression de mes salutations distinguées.",
                ctx,
            ).strip()
            signature = f"{profil.prenom_lm or ''} {profil.nom_lm or ''}".strip()
            body = "\n\n".join([p for p in [intro, *paras, closing, signature] if p])
        else:
            body = (
                f"Madame, Monsieur,\n\n"
                f"C'est avec un vif intérêt que je me permets de vous adresser ma candidature. "
                f"En effet, je suis particulièrement attiré par {accroche}.\n\n"
                f"Souhaitant intégrer une structure dynamique telle que la vôtre, je suis convaincu "
                f"que mon expérience et ma motivation sauront répondre à vos exigences.\n\n"
                f"Vous trouverez ci-joint mon dossier complet. Je reste à votre entière disposition "
                f"pour un entretien.\n\n"
                f"Je vous prie d'agréer, Madame, Monsieur, l'expression de mes salutations distinguées.\n\n"
                f"{profil.prenom_lm or ''} {profil.nom_lm or ''}"
            )

        # Recherche directe par email — O(1), 0 matching flou
        expected_pdf_name = _email_to_pdf_name(ent.email)
        try:
            with zipfile.ZipFile(io.BytesIO(pack_zip_bytes), "r") as _zf:
                if expected_pdf_name in _zf.namelist():
                    lm_name = expected_pdf_name
                    lm_pdf = _zf.read(expected_pdf_name)
                else:
                    # Fallback : ancien pack généré avec nommage par nom d'entreprise
                    found = _lm_from_pack_zip_bytes(pack_zip_bytes, ent.nom)
                    if not found:
                        skipped.append(f"{ent.nom} ({ent.email})")
                        logger.warning(
                            "LM introuvable pour '%s' (%s) — ni par email ni par nom.",
                            ent.nom,
                            ent.email,
                        )
                        continue  # skip cette entreprise, continue le batch
                    lm_name, lm_pdf = found
        except Exception as _e:
            logger.error("Erreur lecture ZIP pour '%s': %s", ent.nom, _e)
            skipped.append(f"{ent.nom} ({ent.email})")
            continue

        attachments: list[tuple[str, bytes, str]] = [
            (lm_name, lm_pdf, "application/pdf"),
            (os.path.basename(cv_doc.fichier.name), cv_bytes, "application/pdf"),
            *other_attachments,
        ]

        raw = _build_mime_message(ent.email, subject, body, attachments)
        try:
            _gmail_create_draft(access_token, raw)
        except Exception as e:
            messages.error(request, f"Erreur Gmail API (ex: scope/token). Détails: {str(e)[:200]}")
            return redirect("settings_page")

        ent.est_dans_paquet = True
        ent.date_traitement = now()
        ent.save(update_fields=["est_dans_paquet", "date_traitement"])
        created += 1

    msg = f"{created} brouillon(s) créé(s) dans Gmail."
    if skipped:
        noms = ", ".join(skipped[:10])
        suite = f" ... et {len(skipped) - 10} autres." if len(skipped) > 10 else ""
        msg += f" Attention: {len(skipped)} LM absente(s) du pack (ignorées) : {noms}{suite}"
    messages.success(request, msg)
    # Si on a réussi à créer des brouillons, le pack est considéré "utilisé"
    if created > 0:
        try:
            pack_doc.used_for_gmail = True
            pack_doc.save(update_fields=["used_for_gmail"])
        except Exception:
            pass
    return redirect("dashboard")


# ---------------------------------------------------------------------------
# ACTIONS CRUD
# ---------------------------------------------------------------------------

@login_required
def telecharger_lm(request, ent_id):
    ent = get_object_or_404(EntrepriseCible, id=ent_id, utilisateur=request.user)
    profil, _ = ProfilUtilisateur.objects.get_or_create(user=request.user)
    try:
        pdf_bytes = generer_pdf_lm(profil, ent)
    except Exception:
        logger.exception("telecharger_lm failed user=%s ent_id=%s", request.user.id, ent_id)
        return HttpResponse(
            "Erreur lors de la génération du PDF. Regarde les logs Render pour le détail.",
            status=500,
            content_type="text/plain; charset=utf-8",
        )

    resp = HttpResponse(pdf_bytes, content_type="application/pdf")
    safe_name = _slugify_loose(ent.nom or "lettre")
    resp["Content-Disposition"] = f'inline; filename="LM_{safe_name}.pdf"'
    return resp


@login_required
def upload_cv(request):
    if request.method == 'POST' and request.FILES.get('cv_file'):
        DocumentUtilisateur.objects.create(
            utilisateur=request.user,
            nom_affichage=request.POST.get('nom_doc', 'Document'),
            type_doc=request.POST.get('type_doc', 'CV'),
            fichier=request.FILES['cv_file'],
        )
    return redirect('dashboard')


@login_required
@require_POST
def delete_document(request, doc_id: int):
    doc = get_object_or_404(DocumentUtilisateur, id=doc_id, utilisateur=request.user)
    try:
        if getattr(doc, "fichier", None):
            doc.fichier.delete(save=False)
    except Exception:
        pass
    doc.delete()
    messages.success(request, "Document supprimé.")
    return redirect("dashboard")


@login_required
@require_POST
def supprimer_tout(request):
    EntrepriseCible.objects.filter(utilisateur=request.user).delete()
    # On garde l'historique (ScanSession) même si l'utilisateur vide sa liste
    return redirect('dashboard')


@login_required
@require_POST
def supprimer_documents(request):
    _delete_all_user_documents(request.user)
    return redirect('dashboard')


@login_required
@require_POST
def vider_liste_et_documents(request):
    """
    Action unique: vide la liste + les documents.
    L'historique (ScanSession) est conservé.
    """
    try:
        with transaction.atomic():
            EntrepriseCible.objects.filter(utilisateur=request.user).delete()
            # DB delete first, then best-effort storage delete inside helper
            _delete_all_user_documents(request.user)
        messages.success(request, "Liste et documents (CV + packs ZIP) vidés. L’historique a été conservé.")
        return redirect("dashboard")
    except Exception as e:
        # Render n'affiche parfois que les access logs: on renvoie le détail aussi côté client.
        logger.exception("vider_liste_et_documents failed user=%s", request.user.id)
        try:
            import traceback

            print("vider_liste_et_documents exception:", repr(e))
            print(traceback.format_exc())
        except Exception:
            pass
        return HttpResponse(
            f"Erreur suppression: {type(e).__name__}: {str(e)[:500]}",
            status=500,
            content_type="text/plain; charset=utf-8",
        )
