import os
import re
import threading
import unicodedata

import dns.resolver


_BG_SEMAPHORE = threading.BoundedSemaphore(3)


def _compute_static_version():
    try:
        from django.contrib.staticfiles import finders
        p = finders.find("js/scan-history.min.js")
        return int(os.path.getmtime(p)) if p else None
    except (OSError, TypeError):
        return None


_STATIC_VERSION = _compute_static_version()


def _run_in_background(target, *args, **kwargs):
    """Lance target(*args, **kwargs) dans un thread daemon protégé par sémaphore."""
    def _wrapper():
        with _BG_SEMAPHORE:
            target(*args, **kwargs)
    threading.Thread(target=_wrapper, daemon=True).start()


def verifier_email_existence(email: str) -> bool:
    if not email:
        return False
    try:
        domaine = email.split("@")[1]
        dns.resolver.resolve(domaine, "MX")
        return True
    except Exception:
        return False


def get_accroche(profil, secteur_activite):
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


def _email_to_pdf_name(email: str) -> str:
    e = (email or "").strip().lower()
    e = unicodedata.normalize("NFKD", e)
    e = "".join(c for c in e if not unicodedata.combining(c))
    e = re.sub(r"[^a-z0-9@._+\-]", "_", e)
    e = e.replace("@", "_AT_")
    e = re.sub(r"_+", "_", e).strip("_")
    return f"LM_{e}.pdf"


def _safe_format(text: str, ctx: dict) -> str:
    if not text:
        return ""
    try:
        return text.format_map(ctx)
    except Exception:
        return text


def _slugify_loose(s: str) -> str:
    s = (s or "").lower()
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
