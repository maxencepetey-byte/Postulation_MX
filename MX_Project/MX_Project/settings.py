"""Settings Django : SECRET_KEY/DEBUG via env, DB via dj-database-url, sécurité prod."""

import sys
from pathlib import Path
from decouple import config
from django.contrib.messages import constants as message_constants
import dj_database_url

BASE_DIR = Path(__file__).resolve().parent.parent

# --- CRITIQUE 1 : SECRET_KEY hors du code source ---
SECRET_KEY = config('DJANGO_SECRET_KEY')

# --- CRITIQUE 2 : DEBUG et ALLOWED_HOSTS sécurisés ---
DEBUG = config('DJANGO_DEBUG', default=False, cast=bool)

ALLOWED_HOSTS = [
    h.strip()
    for h in config("DJANGO_ALLOWED_HOSTS", default="127.0.0.1,localhost").split(",")
    if h.strip()
]

# Render injecte RENDER_EXTERNAL_HOSTNAME automatiquement (ex: postulation-mx.onrender.com)
_render_host = config("RENDER_EXTERNAL_HOSTNAME", default="").strip()
if _render_host and _render_host not in ALLOWED_HOSTS:
    ALLOWED_HOSTS.append(_render_host)


# --- CRITIQUE 3 : django.contrib.admin ajouté ---
INSTALLED_APPS = [
    'django.contrib.admin',        # <-- était absent, admin.py et urls.py l'utilisaient
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'core',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'MX_Project.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'MX_Project.wsgi.application'

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}

# Render/Neon: préfère DATABASE_URL si défini
database_url = (config("DATABASE_URL", default="") or "").strip()
if database_url:
    # conn_health_checks : Neon ferme les connexions inactives, sans ce test la requête suivante échoue.
    DATABASES["default"] = dj_database_url.parse(
        database_url, conn_max_age=600, conn_health_checks=True, ssl_require=True,
    )

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

# Localisation correcte pour une app Suisse francophone
LANGUAGE_CODE = 'fr-ch'
TIME_ZONE = 'Europe/Zurich'
USE_I18N = True
USE_TZ = True

STATIC_URL = '/static/'
STATICFILES_DIRS = [
    # `resources/` est à la racine du repo, au-dessus de `MX_Project/`
    BASE_DIR.parent / 'resources',
]
STATIC_ROOT = BASE_DIR / "staticfiles"
# STATICFILES_STORAGE a été retiré de Django 5.1 : seul STORAGES est lu.
# Pendant les tests, pas de manifest (collectstatic n'a pas tourné) : stockage simple.
_TESTING = len(sys.argv) > 1 and sys.argv[1] == "test"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": (
            "django.contrib.staticfiles.storage.StaticFilesStorage" if _TESTING
            else "whitenoise.storage.CompressedManifestStaticFilesStorage"
        ),
    },
}
# Un fichier présent sur disque mais absent du manifest est servi sans hachage, au lieu d'une erreur 500.
WHITENOISE_MANIFEST_STRICT = False

# Bootstrap n'a pas de classe « alert-error » : les erreurs sortent en « alert-danger ».
# « error » reste dans le tag : l'admin Django ne colore que « li.error ».
MESSAGE_TAGS = {message_constants.ERROR: "danger error"}

CSRF_TRUSTED_ORIGINS = [o.strip() for o in (config("DJANGO_CSRF_TRUSTED_ORIGINS", default="") or "").split(",") if o.strip()]
if _render_host:
    _render_origin = f"https://{_render_host}"
    if _render_origin not in CSRF_TRUSTED_ORIGINS:
        CSRF_TRUSTED_ORIGINS.append(_render_origin)

# Cookies sécurisés en production (HTTPS uniquement)
SESSION_COOKIE_SECURE = not DEBUG
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Lax'
CSRF_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_HTTPONLY = True
CSRF_COOKIE_SAMESITE = 'Lax'
SECURE_BROWSER_XSS_FILTER = True
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"

# HSTS + HTTPS en production
# Render termine le SSL au niveau du load balancer et transmet X-Forwarded-Proto.
# Sans SECURE_PROXY_SSL_HEADER, SECURE_SSL_REDIRECT bouclerait infiniment.
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
if not DEBUG:
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SECURE_SSL_REDIRECT = True

LOGIN_URL = 'login'
LOGIN_REDIRECT_URL = '/'
LOGOUT_REDIRECT_URL = '/login/'

MEDIA_URL = '/media/'
# En déploiement (Render), le filesystem peut être éphémère.
# Utilise un disque persistant et pointe MEDIA_ROOT dessus (ex: /var/data/media).
MEDIA_ROOT = config("DJANGO_MEDIA_ROOT", default=str(BASE_DIR / "media"))

# Token pour protéger l'endpoint de sync (appelé par cron-job.org)
CRON_SYNC_TOKEN = config("CRON_SYNC_TOKEN", default="")

# --- Taille max upload (sécurité supplémentaire niveau Django) ---
DATA_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024  # 5 Mo
FILE_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024



LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {
            'format': '{asctime} {levelname} {name} {message}',
            'style': '{',
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'verbose',
        },
    },
    'root': {
        'handlers': ['console'],
        'level': 'WARNING',
    },
    'loggers': {
        'django': {
            'handlers': ['console'],
            'level': 'ERROR',
            'propagate': False,
        },
        'core': {
            'handlers': ['console'],
            'level': 'INFO',
            'propagate': False,
        },
    },
}