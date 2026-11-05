"""
Génération PDF des lettres de motivation + assemblage en archives ZIP.

Mise en page selon l'usage suisse (norme SN 010130) :
expéditeur en haut à gauche, adresse du destinataire à droite (fenêtre d'enveloppe C5/C6),
« Lieu, le jour mois année », objet en gras sans la mention « Objet : »,
texte aligné à gauche, signature sous la formule de politesse, mention des annexes.
"""

import io
import logging
import zipfile
from xml.sax.saxutils import escape

from django.utils.timezone import now
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer

from ..models import Candidature, DocumentUtilisateur, LettreSecteurTemplate
from ._lettre import (
    contenu_lettre,
    contexte,
    documents_a_joindre,
    libelles_annexes,
    ligne_lieu_date,
    nom_complet,
    recopie_du_mail,
)
from ._utils import _lm_pdf_name

logger = logging.getLogger(__name__)

PAGE_W, PAGE_H = A4
MARGE_G = 25 * mm
MARGE_D = 20 * mm
MARGE_HAUT = 20 * mm
MARGE_BAS = 20 * mm
# Champ d'adresse « fenêtre à droite » : 118 mm du bord gauche, 50 mm du haut, ~80 mm de large.
ADRESSE_X = 118 * mm
ADRESSE_Y = 50 * mm
ADRESSE_W = PAGE_W - ADRESSE_X - MARGE_D
ADRESSE_H = 27 * mm
# Le texte commence sous le bloc adresse + date.
DEBUT_TEXTE = 92 * mm
# Padding interne par défaut des Frame reportlab, compensé pour aligner texte et en-tête.
_PAD = 6

_BASE = ParagraphStyle("base", fontName="Helvetica", fontSize=11, leading=15, alignment=TA_LEFT)
ST_EXPEDITEUR = ParagraphStyle("exp", parent=_BASE, fontSize=10, leading=13)
ST_DESTINATAIRE = ParagraphStyle("dest", parent=_BASE, fontSize=11, leading=14)
ST_DESTINATAIRE_SERRE = ParagraphStyle("dest_serre", parent=ST_DESTINATAIRE, fontSize=9, leading=11)
ST_DATE = ParagraphStyle("date", parent=_BASE, leftIndent=ADRESSE_X - MARGE_G)
ST_OBJET = ParagraphStyle("objet", parent=_BASE, fontName="Helvetica-Bold", spaceAfter=18)
ST_CORPS = ParagraphStyle("corps", parent=_BASE, spaceAfter=10)
ST_SIGNATURE = ParagraphStyle("sig", parent=_BASE, leftIndent=ADRESSE_X - MARGE_G)
ST_ANNEXES = ParagraphStyle("annexes", parent=_BASE, fontSize=10, leading=13)


def _p(texte: str, style) -> Paragraph:
    """Paragraphe reportlab à partir de texte brut : échappe & < > et garde les retours à la ligne."""
    return Paragraph(escape(texte or "").replace("\n", "<br/>"), style)


def _lignes(*lignes: str) -> str:
    return "\n".join(l.strip() for l in lignes if l and l.strip())


def _annexes_utilisateur(user) -> list[str]:
    docs = DocumentUtilisateur.objects.filter(utilisateur=user).order_by("-date_upload")
    return libelles_annexes(documents_a_joindre(docs))


def generer_pdf_lm(profil, ent: Candidature, annexes: list[str] | None = None, templates: dict | None = None) -> bytes:
    """
    Génère le PDF de lettre de motivation pour une Candidature.
    `annexes` et `templates` peuvent être pré-calculés par l'appelant pour éviter
    des requêtes répétées lors d'une génération en lot.
    """
    entreprise = ent.entreprise
    secteur_nom = (ent.secteur_activite or "").strip()

    if templates is not None:
        tpl = templates.get(secteur_nom)
    else:
        tpl = LettreSecteurTemplate.objects.filter(
            utilisateur=ent.utilisateur, secteur_nom=secteur_nom
        ).first() if secteur_nom else None

    if annexes is None:
        annexes = _annexes_utilisateur(ent.utilisateur)

    if tpl is not None:
        tpl_email = templates.get("Email") if templates is not None else LettreSecteurTemplate.objects.filter(
            utilisateur=ent.utilisateur, secteur_nom="Email").first()
        if recopie_du_mail([getattr(tpl, f"paragraph_{i}") for i in range(1, 5)], tpl_email):
            # Jamais le texte du mail comme lettre : le texte générique est un moindre mal.
            logger.warning("generer_pdf_lm: template '%s' identique au mail, lettre générique utilisée (user %s)",
                           secteur_nom, ent.utilisateur_id)
            tpl = None

    ctx = contexte(profil, ent)
    contenu = contenu_lettre(tpl, ctx)

    expediteur = _lignes(
        profil.rue or "",
        f"{profil.npa or ''} {profil.ville or ''}",
        profil.telephone or "",
        profil.email_lm or "",
    )
    destinataire = _lignes(
        entreprise.raison_sociale,
        entreprise.adresse or "",
        f"{getattr(entreprise, 'npa', '') or ''} {getattr(entreprise, 'localite', '') or ''}",
    )
    signature = nom_complet(profil)

    def _entete(canvas, doc):
        canvas.saveState()
        y_haut = PAGE_H - MARGE_HAUT
        if signature:
            nom = Paragraph(f"<b>{escape(signature)}</b>", ST_EXPEDITEUR)
            _, h = nom.wrap(ADRESSE_X - MARGE_G - 5 * mm, 30 * mm)
            nom.drawOn(canvas, MARGE_G, y_haut - h)
            y_haut -= h
        if expediteur:
            exp = _p(expediteur, ST_EXPEDITEUR)
            _, h = exp.wrap(ADRESSE_X - MARGE_G - 5 * mm, 30 * mm)
            exp.drawOn(canvas, MARGE_G, y_haut - h)

        dest = _p(destinataire, ST_DESTINATAIRE)
        _, h = dest.wrap(ADRESSE_W, ADRESSE_H)
        if h > ADRESSE_H:
            # Raison sociale très longue : on réduit le corps plutôt que de chevaucher la date.
            dest = _p(destinataire, ST_DESTINATAIRE_SERRE)
            _, h = dest.wrap(ADRESSE_W, ADRESSE_H)
        dest.drawOn(canvas, ADRESSE_X, PAGE_H - ADRESSE_Y - h)
        canvas.restoreState()

    def _pied(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.drawRightString(PAGE_W - MARGE_D, 10 * mm, f"{signature} – page {doc.page}")
        canvas.restoreState()

    story = [
        Spacer(1, DEBUT_TEXTE - MARGE_HAUT - 12 * mm),
        _p(ligne_lieu_date(profil), ST_DATE),
        Spacer(1, 12 * mm),
        _p(contenu.objet, ST_OBJET),
        _p(contenu.salutation, ST_CORPS),
    ]
    story += [_p(par, ST_CORPS) for par in contenu.paragraphes]

    fin = [Spacer(1, 4), _p(contenu.politesse, ST_CORPS)]
    if signature:
        # Espace réservé à la signature manuscrite.
        fin += [Spacer(1, 14 * mm), _p(signature, ST_SIGNATURE)]
    if annexes:
        fin += [Spacer(1, 10 * mm), _p("Annexes\n" + "\n".join(annexes), ST_ANNEXES)]
    story.append(KeepTogether(fin))

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        leftMargin=MARGE_G - _PAD, rightMargin=MARGE_D - _PAD,
        topMargin=MARGE_HAUT - _PAD, bottomMargin=MARGE_BAS - _PAD,
        title=f"Lettre de motivation – {entreprise.raison_sociale}",
        author=signature,
    )
    doc.build(story, onFirstPage=_entete, onLaterPages=_pied)

    logger.info(
        "generer_pdf_lm: ent_id=%s secteur=%s tpl=%s",
        ent.pk, secteur_nom, tpl.secteur_nom if tpl else "FALLBACK_GÉNÉRIQUE",
    )
    return buffer.getvalue()


def _generer_zip(profil, entreprises, marquer_traitees=False):
    """Assemble les LM PDF de plusieurs candidatures dans une archive ZIP en mémoire."""
    entreprises = list(entreprises)
    if not entreprises:
        return b""
    user = entreprises[0].utilisateur
    annexes = _annexes_utilisateur(user)
    templates = {t.secteur_nom: t for t in LettreSecteurTemplate.objects.filter(utilisateur=user)}

    zip_buffer = io.BytesIO()
    to_update = []
    noms_utilises: set[str] = set()
    with zipfile.ZipFile(zip_buffer, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
        for ent in entreprises:
            nom = _lm_pdf_name(ent.entreprise.raison_sociale)
            # Deux entreprises homonymes écraseraient leur lettre dans l'archive.
            if nom in noms_utilises:
                nom = nom[:-4] + f" ({ent.pk}).pdf"
            noms_utilises.add(nom)
            zf.writestr(nom, generer_pdf_lm(profil, ent, annexes=annexes, templates=templates))
            if marquer_traitees:
                ent.est_dans_paquet = True
                ent.date_traitement = now()
                to_update.append(ent)
    if to_update:
        Candidature.objects.bulk_update(to_update, ["est_dans_paquet", "date_traitement"])
    zip_buffer.seek(0)
    return zip_buffer.read()
