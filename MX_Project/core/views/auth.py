import logging
import secrets
import threading
from datetime import timedelta
from urllib.parse import urlencode

import requests
from decouple import config

from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import UserCreationForm
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from ..models import GmailOAuthToken

logger = logging.getLogger(__name__)

# Mutex par user_id pour éviter les double-refresh simultanés (race condition F1/A).
_TOKEN_REFRESH_LOCKS: dict[int, threading.Lock] = {}
_TOKEN_REFRESH_LOCKS_MUTEX = threading.Lock()


def _get_token_lock(user_id: int) -> threading.Lock:
    with _TOKEN_REFRESH_LOCKS_MUTEX:
        if user_id not in _TOKEN_REFRESH_LOCKS:
            _TOKEN_REFRESH_LOCKS[user_id] = threading.Lock()
        return _TOKEN_REFRESH_LOCKS[user_id]


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

    qs = urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "https://www.googleapis.com/auth/gmail.compose",
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
    })
    return redirect(f"https://accounts.google.com/o/oauth2/v2/auth?{qs}")


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

    r = requests.post("https://oauth2.googleapis.com/token", data={
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }, timeout=30)
    if r.status_code >= 400:
        return redirect("settings_page")
    payload = r.json()

    refresh_token = (payload.get("refresh_token") or "").strip()
    access_token = (payload.get("access_token") or "").strip()
    expires_in = payload.get("expires_in")
    scope = (payload.get("scope") or "").strip()
    token_type = (payload.get("token_type") or "").strip()

    if not refresh_token:
        existing = GmailOAuthToken.objects.filter(utilisateur=request.user).first()
        if existing:
            refresh_token = existing.refresh_token
        else:
            return redirect("settings_page")

    expires_at = None
    try:
        if expires_in:
            expires_at = timezone.now() + timedelta(seconds=int(expires_in))
    except (TypeError, ValueError):
        pass

    tok, _ = GmailOAuthToken.objects.get_or_create(
        utilisateur=request.user, defaults={"refresh_token": refresh_token}
    )
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
    if tok and tok.refresh_token:
        # Révoquer le refresh_token invalide toute la session OAuth (access + refresh).
        # Révoquer seulement l'access_token laisserait le refresh_token actif.
        try:
            requests.post(
                "https://oauth2.googleapis.com/revoke",
                params={"token": tok.refresh_token},
                timeout=10,
            )
        except requests.RequestException:
            pass
    GmailOAuthToken.objects.filter(utilisateur=request.user).delete()
    return redirect("settings_page")


def _gmail_get_access_token(user) -> str:
    # Lock par user : empêche deux threads de faire un refresh simultané (race condition
    # qui produirait un double appel à /token avec le même refresh_token — fatal si
    # Google active la rotation des refresh_tokens → invalid_grant sur le 2e appel).
    with _get_token_lock(user.id):
        # Relecture DB à l'intérieur du lock : si un autre thread vient de rafraîchir
        # le token, on récupère directement la valeur fraîche sans rappeler Google.
        tok = GmailOAuthToken.objects.filter(utilisateur=user).first()
        if not tok:
            raise RuntimeError("Gmail not connected")

        if tok.access_token and tok.expires_at and tok.expires_at > timezone.now() + timedelta(seconds=30):
            return tok.access_token

        client_id, client_secret, _ = _google_oauth_config()
        if not client_id or not client_secret:
            raise RuntimeError("Missing Google OAuth server config")

        r = requests.post("https://oauth2.googleapis.com/token", data={
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": tok.refresh_token,
            "grant_type": "refresh_token",
        }, timeout=30)
        if r.status_code >= 400:
            raise RuntimeError(f"Token refresh failed: HTTP {r.status_code}")
        payload = r.json()

        tok.access_token = (payload.get("access_token") or "").strip()
        tok.token_type = (payload.get("token_type") or tok.token_type or "").strip()
        try:
            expires_in = payload.get("expires_in")
            if expires_in:
                tok.expires_at = timezone.now() + timedelta(seconds=int(expires_in))
        except (TypeError, ValueError):
            tok.expires_at = None

        # Google peut émettre un nouveau refresh_token lors du refresh (rotation).
        # Si on l'ignore, le prochain refresh échoue avec invalid_grant.
        new_rt = (payload.get("refresh_token") or "").strip()
        if new_rt:
            tok.refresh_token = new_rt
            tok.save(update_fields=["access_token", "refresh_token", "expires_at", "token_type", "updated_at"])
        else:
            tok.save(update_fields=["access_token", "expires_at", "token_type", "updated_at"])

        return tok.access_token
