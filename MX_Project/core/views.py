import io
import zipfile
import requests
import dns.resolver
from datetime import date
import os
import threading
import re
import base64
import secrets
import unicodedata
from datetime import timedelta
from urllib.parse import urlencode

from django.shortcuts import render, redirect, get_object_or_404
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth import login, logout
from django.core.paginator import Paginator
from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseForbidden
from django.utils.timezone import now
from django.db import IntegrityError
from django.db.models import Max, Count
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
    e = _ud.normalize("NFKD", e)
    e = "".join(c for c in e if not _ud.combining(c))
    e = re.sub(r"[^a-z0-9@._+\-]", "_", e)
    e = e.replace("@", "_AT_")
    e = re.sub(r"_+", "_", e).strip("_")
    return f"LM_{e}.pdf"

from .constants import NOGA_MAP, SECTEURS_NOGA_GROUPS
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
    client_id, _, redirect_uri = _google_oauth_config()
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
    tok = GmailOAuthToken.objects.filter(utilisateur=request.user).first()
    if tok and tok.access_token:
        try:
            requests.post(
                "https://oauth2.googleapis.com/revoke",
                params={"token": tok.access_token},
                timeout=10,
            )
        except Exception:
            pass
    GmailOAuthToken.objects.filter(utilisateur=request.user).delete()
    return redirect("settings_page")


def _gmail_get_access_token(user) -> str:
    tok = GmailOAuthToken.objects.filter(utilisateur=user).first()
    if not tok:
        raise RuntimeError("Gmail not connected")

    if tok.access_token and tok.expires_at and tok.expires_at > timezone.now() + timedelta(seconds=30):
        return tok.access_token

    client_id, client_secret, _ = _google_oauth_config()
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
        if not re.match(r'^\d{2}$', s):
            logger.warning("_run_scan_for_user: code NOGA invalide ignoré: %r", s)
            continue
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
        if not choix:
            return render(request, "core/onboarding.html", {
                "secteurs": secteurs,
                "erreur": "Coche au moins un secteur pour continuer.",
            })
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


@login_required
def add_secteurs(request):
    profil, _ = ProfilUtilisateur.objects.get_or_create(user=request.user)

    existing_codes = set(
        c.strip() for c in profil.onboarding_secteurs.split(",") if c.strip()
    )

    if request.method == "POST":
        submitted_codes = set(request.POST.getlist("secteurs"))

        if not submitted_codes:
            return render(request, "core/add_secteurs.html", {
                "existing_codes": existing_codes,
                "groups": SECTEURS_NOGA_GROUPS,
                "erreur": "Coche au moins un secteur pour continuer.",
            })

        new_codes = submitted_codes - existing_codes
        all_codes = existing_codes | submitted_codes

        profil.onboarding_secteurs = ",".join(sorted(all_codes))
        profil.save(update_fields=["onboarding_secteurs"])

        if new_codes:
            t = threading.Thread(
                target=_run_scan_for_user,
                args=(request.user, list(new_codes)),
                daemon=True,
            )
            t.start()
            n = len(new_codes)
            messages.success(
                request,
                f"✅ {n} nouveau(x) secteur(s) ajouté(s). Configure les templates LM associés pour débloquer le dashboard."
            )
        else:
            messages.info(request, "Aucun nouveau secteur — ta sélection est déjà active.")

        from django.urls import reverse
        return redirect(reverse('settings_page') + '?tab=templates')

    return render(request, "core/add_secteurs.html", {
        "existing_codes": existing_codes,
        "groups": SECTEURS_NOGA_GROUPS,
    })


# ---------------------------------------------------------------------------
# DASHBOARD
# ---------------------------------------------------------------------------
# core/views.py

@login_required
def dashboard(request):
    profil, _ = ProfilUtilisateur.objects.get_or_create(user=request.user)
    
    # Redirections de sécurité
    if not profil.onboarding_done:
        return redirect("onboarding")

    status = _get_setup_status(request.user)
    if not status["setup_complete"]:
        return redirect("settings_page")

    # Données principales
    entreprises_list = EntrepriseCible.objects.filter(utilisateur=request.user).order_by('-id')
    total_entreprises = entreprises_list.count()
    
    # --- CALCUL DE LA VARIABLE POUR LA BARRE DE PROGRESSION ---
    # On fait le calcul ici car le template HTML ne peut pas gérer les filtres complexes
    nb_restants = request.user.entreprises.filter(
        est_dans_paquet=False
    ).exclude(email="").count()

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
        'secteurs_noga': SECTEURS_NOGA_GROUPS,
        "gmail_connected": GmailOAuthToken.objects.filter(utilisateur=request.user).exists(),
        'static_version': static_version,
        'nb_restants': nb_restants, # <-- On passe la variable calculée ici
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
        all_counts = dict(
            qs_pack_all.values("numero_pack").annotate(n=Count("id")).values_list("numero_pack", "n")
        )
        remaining_counts = dict(
            qs_pack_remaining.values("numero_pack").annotate(n=Count("id")).values_list("numero_pack", "n")
        )
        for i in range(1, int(max_pack) + 1):
            nom_base = f"MX_SCAN_{secteur_clean}_PACK_{i}"
            doc = docs.get(nom_base)
            total_cnt = all_counts.get(i, 0)
            remaining_cnt = remaining_counts.get(i, 0)

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



def _get_setup_status(user):
    """
    Retourne l'état de complétion du setup obligatoire.
    Utilisé pour bloquer l'accès au dashboard tant que tout n'est pas rempli.
    """
    profil, _ = ProfilUtilisateur.objects.get_or_create(user=user)
    profil_ok = bool(profil.prenom_lm and profil.nom_lm and profil.email_lm)

    # Secteurs choisis par l'utilisateur (source de vérité immédiate, même si le scan
    # tourne encore en arrière-plan et qu'aucune EntrepriseCible n'existe encore).
    secteurs_profil = set(
        NOGA_MAP.get(c.strip()[:2], c.strip())
        for c in profil.onboarding_secteurs.split(",")
        if c.strip()
    )

    # Secteurs déjà présents dans les entreprises scannées (peuvent s'ajouter
    # au cours du temps si le scan révèle des secteurs hors profil).
    secteurs_cibles = set(
        EntrepriseCible.objects
        .filter(utilisateur=user)
        .exclude(secteur_activite__isnull=True)
        .exclude(secteur_activite="")
        .values_list("secteur_activite", flat=True)
        .distinct()
    )
    # On exige : template "Email" + un template par secteur du profil ET par secteur scanné
    secteurs_requis = {"Email"} | secteurs_profil | secteurs_cibles

    templates_ok = set(
        LettreSecteurTemplate.objects
        .filter(utilisateur=user)
        .exclude(paragraph_1="")
        .values_list("secteur_nom", flat=True)
    )
    secteurs_manquants = secteurs_requis - templates_ok
    gmail_connected = GmailOAuthToken.objects.filter(utilisateur=user).exists()

    return {
        "profil_ok": profil_ok,
        "secteurs_manquants": secteurs_manquants,
        "secteurs_requis": secteurs_requis,
        "gmail_connected": gmail_connected,
        "setup_complete": profil_ok and not secteurs_manquants and gmail_connected,
    }




@login_required
def settings_page(request):
    profil, _ = ProfilUtilisateur.objects.get_or_create(user=request.user)
    required_fields = ["prenom_lm", "nom_lm", "email_lm"]

    # ── Secteurs requis : Email + secteurs du profil + secteurs déjà scannés ──
    # On inclut profil.onboarding_secteurs pour afficher l'éditeur immédiatement
    # après add_secteurs, même si le scan n'a pas encore créé d'entreprises.
    secteurs_profil = [
        NOGA_MAP.get(c.strip()[:2], c.strip())
        for c in profil.onboarding_secteurs.split(",")
        if c.strip()
    ]
    secteurs_cibles = list(
        EntrepriseCible.objects
        .filter(utilisateur=request.user)
        .exclude(secteur_activite__isnull=True)
        .exclude(secteur_activite="")
        .values_list("secteur_activite", flat=True)
        .distinct()
        .order_by("secteur_activite")
    )
    secteurs_requis_set = {"Email"} | set(secteurs_profil) | set(secteurs_cibles)
    secteurs_requis_list = ["Email"] + sorted(secteurs_requis_set - {"Email"})

    templates_qs = LettreSecteurTemplate.objects.filter(utilisateur=request.user)
    templates_data = {
        t.secteur_nom: {
            "objet":       t.objet or "",
            "salutation":  t.salutation or "",
            "paragraph_1": t.paragraph_1 or "",
            "paragraph_2": t.paragraph_2 or "",
            "paragraph_3": t.paragraph_3 or "",
            "paragraph_4": t.paragraph_4 or "",
            "conclusion":  t.conclusion or "",
        }
        for t in templates_qs
    }

    if request.method == 'POST':
        action = request.POST.get("action", "")

        # ── Action 1 : sauvegarde profil (onglet Identité) ──
        if action == "save_profil":
            form = ProfilForm(request.POST, instance=profil, required_fields=required_fields)
            if form.is_valid():
                form.save()
                messages.success(request, "✅ Informations personnelles sauvegardées.")
            else:
                for field, errors in form.errors.items():
                    for error in errors:
                        messages.error(request, f"Erreur — {field} : {error}")
            return redirect('settings_page')

        # ── Action 2 : sauvegarde template (onglet Templates LM) ──
        elif action == "save_template":
            secteur_tpl = (request.POST.get("template_secteur") or "Email").strip()[:100]
            if not secteur_tpl:
                secteur_tpl = "Email"

            p1 = (request.POST.get("paragraph_1") or "").strip()
            if not p1:
                messages.error(request, f"⚠ Le Paragraphe 1 est obligatoire pour le template « {secteur_tpl} ».")
                return redirect('/settings/?tab=templates')

            tpl, _ = LettreSecteurTemplate.objects.get_or_create(
                utilisateur=request.user,
                secteur_nom=secteur_tpl,
            )
            tpl.objet       = (request.POST.get("objet")        or "").strip()
            tpl.salutation  = (request.POST.get("introduction")  or "").strip()
            tpl.paragraph_1 = p1
            tpl.paragraph_2 = (request.POST.get("paragraph_2") or "").strip()
            tpl.paragraph_3 = (request.POST.get("paragraph_3") or "").strip()
            tpl.paragraph_4 = (request.POST.get("paragraph_4") or "").strip()
            tpl.conclusion  = (request.POST.get("conclusion")   or "").strip()
            tpl.save()

            messages.success(request, f"✅ Template « {secteur_tpl} » sauvegardé.")
            # Redirect vers l'onglet templates avec le secteur actif en paramètre
            return redirect(f"/settings/?tab=templates&secteur={secteur_tpl}")

        # Fallback
        return redirect('settings_page')

    else:
        form = ProfilForm(instance=profil, required_fields=required_fields)

    status = _get_setup_status(request.user)

    # Secteur et onglet actifs (après redirect save_template)
    active_tab     = request.GET.get("tab", "identite")
    active_secteur = request.GET.get("secteur", "Email")

    return render(request, 'core/settings.html', {
    'form':                   form,
    'profil':                 profil,
    'secteurs_requis_list':   secteurs_requis_list,
    'templates_data':         templates_data,
    'templates_by_secteur':   {t.secteur_nom: t for t in templates_qs},
    'gmail_connected':        status["gmail_connected"],
    'secteurs_manquants':     sorted(status["secteurs_manquants"]),
    'profil_ok':              status["profil_ok"],
    'setup_complete':         status["setup_complete"],
    'active_tab':             active_tab,
    'active_secteur':         active_secteur,
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

    secteurs = [s for s in secteurs if re.match(r'^\d{2}$', s)]

    if not secteurs:
        return redirect('dashboard')

    noms_secteurs = [NOGA_MAP.get(s[:2], s) for s in secteurs]

    session = ScanSession.objects.create(
        utilisateur=request.user,
        secteurs=', '.join(noms_secteurs),
    )

    def _run(user_id, session_id, secteurs_list):
        from django.contrib.auth.models import User
        from core.models import EntrepriseReferentiel
        try:
            user = User.objects.get(id=user_id)
            session = ScanSession.objects.get(id=session_id)
            recherche, _ = Recherche.objects.get_or_create(
                utilisateur=user, secteur_noga="SCAN_GENEVE"
            )
            total_ajoutes = 0
            total_doublons = 0
            base_par_secteur: dict[str, int] = {}
            ajoutes_par_secteur: dict[str, int] = {}

            for s in secteurs_list:
                secteur_nom = NOGA_MAP.get(s[:2], "Général")
                if secteur_nom not in base_par_secteur:
                    base_par_secteur[secteur_nom] = EntrepriseCible.objects.filter(
                        utilisateur=user, secteur_activite=secteur_nom,
                    ).count()
                    ajoutes_par_secteur[secteur_nom] = 0
                qs = (
                    EntrepriseReferentiel.objects.filter(code_noga__startswith=s, email_valide=True)
                    .only("raison_sociale", "email", "adresse")
                    .order_by("raison_sociale")
                )
                for ref in qs.iterator(chunk_size=2000):
                    total_courant = base_par_secteur[secteur_nom] + ajoutes_par_secteur[secteur_nom]
                    pack_id = (total_courant // 500) + 1
                    try:
                        EntrepriseCible.objects.create(
                            recherche=recherche,
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
            logger.info("lancer_scan: terminé — %d ajoutés, %d doublons (user %s)", total_ajoutes, total_doublons, user_id)
        except Exception:
            logger.exception("lancer_scan thread failed (user %s, session %s)", user_id, session_id)

    threading.Thread(target=_run, args=(request.user.id, session.id, secteurs), daemon=True).start()

    messages.success(request, f"⏳ Scan lancé pour {', '.join(noms_secteurs)}. Les résultats apparaîtront dans quelques instants.")
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
    Protection: token dans le header Authorization: Bearer <token>.

    Lance le sync en arrière-plan pour éviter les timeouts HTTP.
    """
    auth_header = request.META.get("HTTP_AUTHORIZATION", "")
    token = auth_header.removeprefix("Bearer ").strip()
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


def cron_sync_view(request):
    auth_header = request.META.get("HTTP_AUTHORIZATION", "")
    token_recu = auth_header.removeprefix("Bearer ").strip()
    token_attendu = (getattr(settings, "CRON_SYNC_TOKEN", "") or "").strip()

    if not token_recu or token_recu != token_attendu:
        return HttpResponseForbidden("Token invalide.")

    def run_task():
        try:
            logger.info("cron_sync_view: démarrage sync_registre")
            call_command('sync_registre')
            logger.info("cron_sync_view: sync_registre terminé")
        except Exception:
            logger.exception("cron_sync_view: sync_registre a échoué")

    thread = threading.Thread(target=run_task, daemon=True)
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
    secteur_nom = (ent.secteur_activite or "").strip()

    # PDF utilise uniquement le template du secteur — jamais "Email" ni "Général"
    _tpl_user = ent.utilisateur or (profil.user if profil else None)
    tpl = None
    if _tpl_user and secteur_nom:
        tpl = LettreSecteurTemplate.objects.filter(
            utilisateur=_tpl_user, secteur_nom=secteur_nom
        ).first()

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
        p.drawRightString(width - 2 * cm, 6 * cm, signature)
    p.save()
    buffer.seek(0)

    logger.info(
        "generer_pdf_lm: ent_id=%s secteur=%s tpl=%s",
        ent.pk,
        secteur_nom,
        tpl.secteur_nom if tpl else "FALLBACK_GÉNÉRIQUE",
    )
    return buffer.read()

# ---------------------------------------------------------------------------
# TÉLÉCHARGEMENTS ZIP
# ---------------------------------------------------------------------------

def _generer_zip(profil, entreprises, marquer_traitees=False):
    zip_buffer = io.BytesIO()
    to_update = []
    with zipfile.ZipFile(zip_buffer, 'w') as zf:
        for ent in entreprises:
            pdf = generer_pdf_lm(profil, ent)
            nom = _email_to_pdf_name(ent.email)
            zf.writestr(nom, pdf)
            if marquer_traitees:
                ent.est_dans_paquet = True
                ent.date_traitement = now()
                to_update.append(ent)
    if to_update:
        EntrepriseCible.objects.bulk_update(to_update, ["est_dans_paquet", "date_traitement"])
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
    try:
        access_token = _gmail_get_access_token(request.user)
    except Exception:
        messages.error(request, "Connexion Gmail invalide ou expirée. Merci de reconnecter Gmail dans Réglages.")
        return redirect("settings_page")

    secteur = (request.POST.get("secteur") or "").strip()
    pack_num_raw = (request.POST.get("pack_num") or "").strip()
    pack_num = int(pack_num_raw) if pack_num_raw.isdigit() else None

    qs_ent = EntrepriseCible.objects.filter(utilisateur=request.user, est_dans_paquet=False).exclude(email="")
    if secteur:
        qs_ent = qs_ent.filter(secteur_activite=secteur)
    if pack_num:
        qs_ent = qs_ent.filter(numero_pack=pack_num)
    if not qs_ent.exists():
        messages.info(request, "Aucune entreprise à traiter (toutes déjà traitées ou sans email).")
        return redirect("dashboard")

    cv_doc = DocumentUtilisateur.objects.filter(utilisateur=request.user, type_doc="CV").order_by("-date_upload").first()
    if not cv_doc:
        messages.error(request, "Aucun CV trouvé. Ajoute un document de type CV dans Réglages avant de créer des brouillons.")
        return redirect("settings_page")

    def _run_brouillons(user_id, secteur, access_token, pack_num=None):
        from django.contrib.auth.models import User
        try:
            user = User.objects.get(id=user_id)
            profil, _ = ProfilUtilisateur.objects.get_or_create(user=user)

            qs = EntrepriseCible.objects.filter(utilisateur=user, est_dans_paquet=False).exclude(email="")
            if secteur:
                qs = qs.filter(secteur_activite=secteur)
            if pack_num:
                qs = qs.filter(numero_pack=pack_num)
            entreprises = list(qs.order_by("id")[:500])
            logger.info("brouillons_bg: %d entreprises à traiter (user %s)", len(entreprises), user_id)

            cv_doc = DocumentUtilisateur.objects.filter(utilisateur=user, type_doc="CV").order_by("-date_upload").first()
            if not cv_doc:
                logger.error("brouillons_bg: CV introuvable pour user %s", user_id)
                return

            other_docs = list(
                DocumentUtilisateur.objects.filter(utilisateur=user)
                .exclude(type_doc__in=["PACK_LM"])
                .exclude(id=cv_doc.id)
                .order_by("-date_upload")
            )

            try:
                cv_bytes = _read_filefield_bytes(cv_doc.fichier)
            except Exception as e:
                logger.error("brouillons_bg: impossible de lire le CV: %s", e)
                return

            other_attachments: list[tuple[str, bytes, str]] = []
            for d in other_docs:
                try:
                    other_attachments.append((
                        os.path.basename(d.fichier.name),
                        _read_filefield_bytes(d.fichier),
                        "application/pdf",
                    ))
                except Exception:
                    continue

            # PERF-04: requête unique hors boucle
            tpl_email = LettreSecteurTemplate.objects.filter(
                utilisateur=user, secteur_nom="Email"
            ).first()

            created = 0
            skipped = 0

            for ent in entreprises:
                secteur_nom = (ent.secteur_activite or "").strip()

                accroche = get_accroche(profil, ent.secteur_activite)
                ctx = {
                    "accroche": accroche,
                    "entreprise": ent.nom,
                    "secteur": secteur_nom,
                    "ville": profil.ville or "Genève",
                    "prenom": profil.prenom_lm or "",
                    "nom": profil.nom_lm or "",
                }

                base_subject = _safe_format((tpl_email.objet if tpl_email else "") or "Candidature spontanée", ctx).strip()
                subject = f"{base_subject} — {ent.nom}".strip()

                if tpl_email and (tpl_email.salutation or tpl_email.paragraph_1 or tpl_email.paragraph_2 or tpl_email.paragraph_3 or tpl_email.paragraph_4 or tpl_email.conclusion):
                    intro = _safe_format(tpl_email.salutation or "Madame, Monsieur,", ctx).strip()
                    paras = [
                        _safe_format(tpl_email.paragraph_1, ctx).strip(),
                        _safe_format(tpl_email.paragraph_2, ctx).strip(),
                        _safe_format(tpl_email.paragraph_3, ctx).strip(),
                        _safe_format(tpl_email.paragraph_4, ctx).strip(),
                    ]
                    closing = _safe_format(
                        tpl_email.conclusion or "Je vous prie d'agréer, Madame, Monsieur, l'expression de mes salutations distinguées.",
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

                try:
                    lm_pdf = generer_pdf_lm(profil, ent)
                    lm_name = _email_to_pdf_name(ent.email)
                except Exception as e:
                    logger.warning("brouillons_bg: PDF failed '%s': %s", ent.email, e)
                    skipped += 1
                    continue

                attachments = [
                    (lm_name, lm_pdf, "application/pdf"),
                    (os.path.basename(cv_doc.fichier.name), cv_bytes, "application/pdf"),
                    *other_attachments,
                ]
                raw = _build_mime_message(ent.email, subject, body, attachments)

                try:
                    _gmail_create_draft(access_token, raw)
                    ent.est_dans_paquet = True
                    ent.brouillon_gmail_cree = True
                    ent.date_traitement = now()
                    ent.save(update_fields=["est_dans_paquet", "brouillon_gmail_cree", "date_traitement"])
                    created += 1
                except Exception as e:
                    err_str = str(e)
                    if "401" in err_str or "403" in err_str or "invalid_grant" in err_str.lower():
                        logger.error("brouillons_bg: auth error, stopping. %s", err_str[:200])
                        break
                    logger.warning("brouillons_bg: draft failed '%s': %s", ent.email, err_str[:200])
                    skipped += 1
                    continue

            logger.info("brouillons_bg: terminé — %d créés, %d ignorés (user %s)", created, skipped, user_id)

        except Exception:
            logger.exception("brouillons_bg: exception non gérée (user %s)", user_id)

    threading.Thread(
        target=_run_brouillons,
        args=(request.user.id, secteur, access_token, pack_num),
        daemon=True,
    ).start()

    nb = qs_ent.count()
    messages.success(
        request,
        f"⏳ Création de {nb} brouillon(s) lancée en arrière-plan. "
        f"Rafraîchis le dashboard dans quelques minutes pour voir la progression."
    )
    return redirect("dashboard")

@login_required
@require_GET
def gmail_progress(request):
    """
    Retourne la progression des brouillons Gmail en cours.
    Utilisé par le polling JS du dashboard.
    """
    secteur = (request.GET.get("secteur") or "").strip()

    qs_all = EntrepriseCible.objects.filter(utilisateur=request.user).exclude(email="")
    qs_done = qs_all.filter(est_dans_paquet=True)

    if secteur:
        qs_all = qs_all.filter(secteur_activite=secteur)
        qs_done = qs_done.filter(secteur_activite=secteur)

    total = qs_all.count()
    done = qs_done.count()
    remaining = total - done

    return JsonResponse({
        "total": total,
        "done": done,
        "remaining": remaining,
        "percent": round((done / total * 100) if total > 0 else 0),
    })


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
    resp["Content-Disposition"] = f'attachment; filename="LM_{safe_name}.pdf"'
    return resp


@login_required
def upload_cv(request):
    if request.method == 'POST' and request.FILES.get('cv_file'):
        from django.core.exceptions import ValidationError
        doc = DocumentUtilisateur(
            utilisateur=request.user,
            nom_affichage=request.POST.get('nom_doc', 'Document'),
            type_doc=request.POST.get('type_doc', 'CV'),
            fichier=request.FILES['cv_file'],
        )
        try:
            doc.full_clean()
            doc.save()
            messages.success(request, "Document ajouté avec succès.")
        except ValidationError as e:
            messages.error(request, " ".join(e.messages))
    elif request.method == 'POST':
        messages.error(request, "Aucun fichier sélectionné.")
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
    L’historique (ScanSession) est conservé.
    """
    try:
        with transaction.atomic():
            EntrepriseCible.objects.filter(utilisateur=request.user).delete()
            # DB delete first, then best-effort storage delete inside helper
            _delete_all_user_documents(request.user)
        messages.success(request, "Liste et documents (CV + packs ZIP) vidés. L’historique a été conservé.")
        return redirect("dashboard")
    except Exception as e:
        logger.exception("vider_liste_et_documents failed user=%s", request.user.id)
        return HttpResponse(
            f"Erreur suppression: {type(e).__name__}: {str(e)[:500]}",
            status=500,
            content_type="text/plain; charset=utf-8",
        )


# ---------------------------------------------------------------------------
# MEDIA PROTÉGÉE
# ---------------------------------------------------------------------------

@login_required
def serve_protected_media(request, path):
    """
    Sert les fichiers media uniquement aux utilisateurs authentifiés.
    Remplace le serving direct de MEDIA_ROOT en développement.
    """
    import mimetypes
    from django.http import FileResponse, Http404

    media_root = settings.MEDIA_ROOT
    full_path = os.path.normpath(os.path.join(media_root, path))

    # Empêche le path traversal (ex: ../../etc/passwd)
    if not full_path.startswith(os.path.normpath(media_root) + os.sep):
        raise Http404

    if not os.path.isfile(full_path):
        raise Http404

    content_type, _ = mimetypes.guess_type(full_path)
    content_type = content_type or "application/octet-stream"
    return FileResponse(open(full_path, "rb"), content_type=content_type)
