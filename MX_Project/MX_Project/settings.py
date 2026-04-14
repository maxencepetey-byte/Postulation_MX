from pathlib import Path
import os
from decouple import config
import dj_database_url

BASE_DIR = Path(__file__).resolve().parent.parent

# --- CRITIQUE 1 : SECRET_KEY hors du code source ---
SECRET_KEY = config('DJANGO_SECRET_KEY')

# --- CRITIQUE 2 : DEBUG et ALLOWED_HOSTS sécurisés ---
DEBUG = config('DJANGO_DEBUG', default=False, cast=bool)

#ALLOWED_HOSTS = config(
   # 'DJANGO_ALLOWED_HOSTS',
   # default='localhost,127.0.0.1'
#).split(',')
# Remplace par ton vrai nom de domaine Render
ALLOWED_HOSTS = [
    'postulation-mx.onrender.com', 
    '127.0.0.1', 
    'localhost'
]


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
    DATABASES["default"] = dj_database_url.parse(database_url, conn_max_age=600, ssl_require=True)

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
STATICFILES_STORAGE = "whitenoise.storage.CompressedManifestStaticFilesStorage"

CSRF_TRUSTED_ORIGINS = [o.strip() for o in (config("DJANGO_CSRF_TRUSTED_ORIGINS", default="") or "").split(",") if o.strip()]

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
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
        },
    },
    'root': {
        'handlers': ['console'],
        'level': 'ERROR',
    },
    'loggers': {
        'django': {
            'handlers': ['console'],
            'level': 'ERROR',
            'propagate': False,
        },
    },
}