import io
import os
import re
import unicodedata
import zipfile


_STOP = {
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


def lm_slug(name: str) -> str:
    """
    Slug tolérant pour noms d'entreprises.
    - lower
    - remplace ligatures (œ/æ)
    - supprime accents
    - garde a-z0-9 et espaces
    - espaces -> underscore
    """
    s = (name or "").strip().lower()
    s = s.replace("œ", "oe").replace("æ", "ae")
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"[\s_]+", " ", s).strip()
    s = re.sub(r"[^a-z0-9 ]+", "", s)
    return s.replace(" ", "_")


def lm_filename(ent_name: str) -> str:
    return f"LM_{lm_slug(ent_name) or 'entreprise'}.pdf"


def find_lm_in_zip(zip_bytes: bytes, ent_name: str) -> tuple[str, bytes] | None:
    """
    Trouve le meilleur PDF dans un ZIP en fonction du nom de l'entreprise.
    Retourne (filename, bytes) ou None.
    """
    needle = lm_slug(ent_name)
    tokens = [t for t in needle.split("_") if t and t not in _STOP]
    tokens_long = [t for t in tokens if len(t) >= 3]

    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
            best: tuple[int, int, str] | None = None  # (score, hits, zip_path)
            for zip_path in zf.namelist():
                base = os.path.basename(zip_path)
                if not base.lower().endswith(".pdf"):
                    continue
                base_slug = lm_slug(base)

                if needle and needle in base_slug:
                    return base, zf.read(zip_path)
                if needle and needle.replace("_", "") in base_slug.replace("_", ""):
                    return base, zf.read(zip_path)

                if tokens_long:
                    compact = base_slug.replace("_", "")
                    hits = sum(1 for t in tokens_long if t in compact)
                    if hits <= 0:
                        continue
                    score = hits * 100 - abs(len(compact) - len(needle.replace("_", "")))
                    cand = (score, hits, zip_path)
                    if best is None or cand > best:
                        best = cand

            if best is not None:
                _score, hits, zip_path = best
                min_hits = 2 if len(tokens_long) >= 2 else 1
                if hits >= min_hits:
                    return os.path.basename(zip_path), zf.read(zip_path)
    except Exception:
        return None
    return None

