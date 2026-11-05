"""Contenu textuel commun à la lettre PDF et au corps de l'e-mail (une seule source de vérité)."""

import os
import re
from dataclasses import dataclass, field
from datetime import date

from ._utils import _safe_format, get_accroche

MOIS_FR = (
    "janvier", "février", "mars", "avril", "mai", "juin",
    "juillet", "août", "septembre", "octobre", "novembre", "décembre",
)

SALUTATION_DEFAUT = "Madame, Monsieur,"
POLITESSE_LETTRE_DEFAUT = "Je vous prie d'agréer, Madame, Monsieur, mes salutations distinguées."
POLITESSE_EMAIL_DEFAUT = "Meilleures salutations"
OBJET_DEFAUT = "Candidature spontanée"

# Types de documents qui ne doivent jamais partir chez un employeur.
TYPES_DOC_NON_JOINTS = ("PACK_LM", "GUIDE")


def date_longue_fr(d: date | None = None) -> str:
    """Date au format de correspondance suisse romande : « 5 novembre 2026 »."""
    d = d or date.today()
    return f"{d.day} {MOIS_FR[d.month - 1]} {d.year}"


def ligne_lieu_date(profil, d: date | None = None) -> str:
    return f"{(profil.ville or '').strip() or 'Genève'}, le {date_longue_fr(d)}"


def nom_complet(profil) -> str:
    return f"{(profil.prenom_lm or '').strip()} {(profil.nom_lm or '').strip()}".strip()


def contexte(profil, cand) -> dict:
    secteur_nom = (cand.secteur_activite or "").strip()
    return {
        "accroche": get_accroche(cand.secteur_activite),
        "entreprise": cand.entreprise.raison_sociale,
        "secteur": secteur_nom,
        "ville": (profil.ville or "").strip() or "Genève",
        "prenom": (profil.prenom_lm or "").strip(),
        "nom": (profil.nom_lm or "").strip(),
    }


def _a_du_contenu(tpl) -> bool:
    return bool(tpl and any(
        (getattr(tpl, f) or "").strip()
        for f in ("salutation", "paragraph_1", "paragraph_2", "paragraph_3", "paragraph_4", "conclusion")
    ))


def _normaliser(texte: str) -> str:
    """Retire les espaces parasites et les sauts de ligne multiples saisis dans les textarea."""
    texte = (texte or "").replace("\r\n", "\n").replace("\r", "\n")
    texte = "\n".join(l.strip() for l in texte.split("\n"))
    return re.sub(r"\n{3,}", "\n\n", texte).strip()


@dataclass
class Contenu:
    objet: str
    salutation: str
    paragraphes: list[str] = field(default_factory=list)
    politesse: str = ""


def contenu_lettre(tpl, ctx: dict) -> Contenu:
    """Textes de la lettre de motivation (template du secteur, sinon texte générique non genré)."""
    objet = _normaliser(_safe_format(tpl.objet, ctx)) if tpl else ""
    objet = objet or OBJET_DEFAUT

    if _a_du_contenu(tpl):
        paras = [_normaliser(_safe_format(getattr(tpl, f"paragraph_{i}"), ctx)) for i in range(1, 5)]
        return Contenu(
            objet=objet,
            salutation=_normaliser(_safe_format(tpl.salutation, ctx)) or SALUTATION_DEFAUT,
            paragraphes=[p for p in paras if p],
            politesse=_normaliser(_safe_format(tpl.conclusion, ctx)) or POLITESSE_LETTRE_DEFAUT,
        )

    return Contenu(
        objet=objet,
        salutation=SALUTATION_DEFAUT,
        paragraphes=[
            f"C'est avec un vif intérêt que je vous adresse ma candidature spontanée. "
            f"{ctx['entreprise']} retient particulièrement mon attention pour {ctx['accroche']}.",
            "Rejoindre une structure dynamique comme la vôtre correspond pleinement à mon projet "
            "professionnel, et j'ai la conviction que mon expérience et ma motivation sauront "
            "répondre à vos attentes.",
            "Vous trouverez en annexe mon dossier de candidature. Je me tiens volontiers à votre "
            "disposition pour un entretien afin de vous présenter plus en détail mes motivations.",
        ],
        politesse=POLITESSE_LETTRE_DEFAUT,
    )


def contenu_email(tpl_email, tpl_secteur, ctx: dict) -> Contenu:
    """
    Corps de l'e-mail : toujours le template « Email ».
    Le template du secteur sert à la lettre PDF jointe, jamais au corps du mail
    (sinon la lettre entière était recopiée dans le message).
    """
    objet = ""
    for tpl in (tpl_email, tpl_secteur):
        if tpl and (tpl.objet or "").strip():
            objet = _normaliser(_safe_format(tpl.objet, ctx))
            break
    objet = objet or OBJET_DEFAUT

    if _a_du_contenu(tpl_email):
        paras = [_normaliser(_safe_format(getattr(tpl_email, f"paragraph_{i}"), ctx)) for i in range(1, 5)]
        return Contenu(
            objet=objet,
            salutation=_normaliser(_safe_format(tpl_email.salutation, ctx)) or SALUTATION_DEFAUT,
            paragraphes=[p for p in paras if p],
            politesse=_normaliser(_safe_format(tpl_email.conclusion, ctx)) or POLITESSE_EMAIL_DEFAUT,
        )

    return Contenu(
        objet=objet,
        salutation=SALUTATION_DEFAUT,
        paragraphes=[
            f"Je me permets de vous adresser ma candidature spontanée au sein de {ctx['entreprise']}.",
            "Vous trouverez en pièces jointes ma lettre de motivation ainsi que mon CV. "
            "Je reste volontiers à votre disposition pour un entretien.",
        ],
        politesse=POLITESSE_EMAIL_DEFAUT,
    )


def objet_email(contenu: Contenu, profil) -> str:
    """Ajoute le nom de la personne candidate à l'objet s'il n'y figure pas déjà (repérage en boîte de réception)."""
    objet = contenu.objet
    nom = nom_complet(profil)
    if nom and nom.lower() not in objet.lower():
        objet = f"{objet} – {nom}"
    return objet


def bloc_signature(profil) -> list[str]:
    lignes = [nom_complet(profil)]
    if (profil.telephone or "").strip():
        lignes.append(profil.telephone.strip())
    if (profil.email_lm or "").strip():
        lignes.append(profil.email_lm.strip())
    return [l for l in lignes if l]


def nom_fichier_document(doc) -> str:
    """Nom lisible pour une pièce jointe (« CV.pdf » plutôt que « cv_storage/CV_aB3x9.pdf »)."""
    base = (doc.nom_affichage or "").strip() or os.path.splitext(os.path.basename(doc.fichier.name))[0]
    base = re.sub(r'[\\/:*?"<>|\x00-\x1f\x7f]', "", base).strip(" .") or "Document"
    return base if base.lower().endswith(".pdf") else f"{base}.pdf"


def documents_a_joindre(docs) -> list:
    """
    Documents envoyés aux employeurs : le CV le plus récent en premier, puis les autres pièces.
    Les packs de LM et le guide ne partent jamais.
    """
    docs = [d for d in docs if d.type_doc not in TYPES_DOC_NON_JOINTS]
    cvs = sorted((d for d in docs if d.type_doc == "CV"), key=lambda d: d.date_upload, reverse=True)
    autres = [d for d in docs if d.type_doc != "CV"]
    return cvs + autres


def libelles_annexes(docs) -> list[str]:
    """Libellés de la mention « Annexes » de la lettre, sans doublon."""
    libelles: list[str] = []
    for d in docs:
        if d.type_doc in TYPES_DOC_NON_JOINTS:
            continue
        lib = (d.nom_affichage or "").strip() or d.get_type_doc_display()
        if lib.lower() in ("cv", "mon cv"):
            lib = "Curriculum vitae"
        if lib not in libelles:
            libelles.append(lib)
    return libelles


_CHAMPS_CORPS = ("paragraph_1", "paragraph_2", "paragraph_3", "paragraph_4")
_CONSIGNE_RE = re.compile(r"\[[^\]]{15,}\]")


def _signature_texte(valeurs) -> str:
    return re.sub(r"\s+", " ", " ".join(v or "" for v in valeurs)).strip().lower()


def recopie_du_mail(paragraphes, tpl_email) -> bool:
    """
    Vrai si le corps d'une lettre reprend celui du template « Email » :
    la personne enverrait alors le texte du mail à la place de sa lettre.
    """
    if not tpl_email:
        return False
    corps_email = _signature_texte(getattr(tpl_email, f) for f in _CHAMPS_CORPS)
    return bool(corps_email) and _signature_texte(paragraphes) == corps_email


def contient_consigne(textes) -> bool:
    """Vrai s'il reste une consigne « [À compléter…] » de la trame de lettre."""
    return any(_CONSIGNE_RE.search(t or "") for t in textes)
