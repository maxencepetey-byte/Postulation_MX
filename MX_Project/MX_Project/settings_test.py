"""Settings de test : surcharge la DB avec SQLite en mémoire."""
from MX_Project.settings import *  # noqa: F401, F403

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}

# Désactive le logging fichier en test
LOGGING["handlers"].pop("file", None)
for logger in LOGGING.get("loggers", {}).values():
    logger["handlers"] = ["console"]
_LOG_HANDLERS = ["console"]
