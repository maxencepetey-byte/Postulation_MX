"""Helpers transverses : versionnement statique, threads daemon, accroches LM, slug & I/O fichiers."""

import os
import re
import threading


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
            try:
                target(*args, **kwargs)
            finally:
                from django.db import connection
                connection.close()
    threading.Thread(target=_wrapper, daemon=True).start()


def get_accroche(secteur_activite):
    """Phrase d'accroche pour la LM, choisie selon le secteur d'activité (fallback générique sinon)."""
    mapping = {
        'Informatique': "votre expertise dans le développement et l'innovation numérique",
        'Banque':       "la rigueur et l'excellence de votre institution financière",
        'Luxe':         "votre savoir-faire d'exception et votre rayonnement international",
        'Architecture': "votre vision architecturale et la qualité de vos réalisations",
        'Santé':        "votre engagement dans les soins et le bien-être des patients",
        'Construction': "votre expertise technique et vos projets d'envergure",
    }
    if secteur_activite and 'Social' in secteur_activite:
        return "votre engagement quotidien dans l'accompagnement et l'impact social de vos projets"
    return mapping.get(secteur_activite, "le dynamisme et les projets de votre entreprise")


def _lm_pdf_name(raison_sociale: str) -> str:
    """Nom de fichier lisible pour la LM, ex: 'Lettre de Motivation Google Suisse.pdf'."""
    nom = (raison_sociale or "").strip()
    nom = re.sub(r'[\\/:*?"<>|]', "", nom)
    nom = re.sub(r"\s+", " ", nom).strip(" .")
    return f"Lettre de Motivation {nom}.pdf"


def _safe_format(text: str, ctx: dict) -> str:
    if not text:
        return ""
    try:
        return text.format_map(ctx)
    except Exception:
        return text


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
