"""Helpers transverses : threads daemon, accroches LM, slug & I/O fichiers."""

import re
import threading


_BG_SEMAPHORE = threading.BoundedSemaphore(3)


def _run_in_background(target, *args, en_attente=None, **kwargs):
    """
    Lance target(*args, **kwargs) dans un thread daemon protégé par sémaphore.
    `en_attente` est rappelé chaque minute tant que le thread attend une place libre.
    """
    def _wrapper():
        from django.db import connection
        try:
            while not _BG_SEMAPHORE.acquire(timeout=60):
                if en_attente:
                    try:
                        en_attente()
                    except Exception:
                        pass
            try:
                target(*args, **kwargs)
            finally:
                _BG_SEMAPHORE.release()
        finally:
            connection.close()
    threading.Thread(target=_wrapper, daemon=True).start()


# Recherche par mot-clé : secteur_activite contient le libellé NOGA complet
# (« Informatique et programmation », « Santé humaine »), jamais le mot seul.
_ACCROCHES = (
    ("social",       "votre engagement quotidien dans l'accompagnement et l'impact social de vos projets"),
    ("informatique", "votre expertise dans le développement et l'innovation numérique"),
    ("banque",       "la rigueur et l'excellence de votre institution financière"),
    ("architecture", "votre vision architecturale et la qualité de vos réalisations"),
    ("santé",        "votre engagement dans les soins et le bien-être des patients"),
    ("construction", "votre expertise technique et vos projets d'envergure"),
)
_ACCROCHE_PAR_DEFAUT = "le dynamisme et les projets de votre entreprise"


def get_accroche(secteur_activite):
    """Phrase d'accroche pour la LM, choisie selon le secteur d'activité (fallback générique sinon)."""
    secteur = (secteur_activite or "").lower()
    for mot_cle, accroche in _ACCROCHES:
        if mot_cle in secteur:
            return accroche
    return _ACCROCHE_PAR_DEFAUT


def _lm_pdf_name(raison_sociale: str) -> str:
    """Nom de fichier lisible pour la LM, ex: 'Lettre de Motivation Google Suisse.pdf'."""
    nom = (raison_sociale or "").strip()
    nom = re.sub(r'[\\/:*?"<>|]', "", nom)
    nom = re.sub(r"\s+", " ", nom).strip(" .")
    return f"Lettre de Motivation {nom}.pdf"


_PLACEHOLDER_RE = re.compile(r"(?<!\{)\{\s*(\w+)\s*\}(?!\})")


def _safe_format(text: str, ctx: dict) -> str:
    """
    Remplace les variables {entreprise}, {prenom}… connues.
    Contrairement à str.format, une accolade isolée ou une variable inconnue
    ne bloque pas le remplacement des autres variables.
    """
    if not text:
        return ""

    def _sub(m):
        key = m.group(1).lower()
        return str(ctx[key]) if key in ctx else m.group(0)

    return _PLACEHOLDER_RE.sub(_sub, text)


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
