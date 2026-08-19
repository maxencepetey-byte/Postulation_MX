"""Génération PDF des lettres de motivation + assemblage en archives ZIP."""

import io
import logging
import zipfile
from datetime import date

from django.utils.timezone import now
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.pdfgen import canvas
from reportlab.platypus import Frame, Paragraph, Spacer

from ..models import Candidature, LettreSecteurTemplate
from ._utils import _lm_pdf_name, _safe_format, get_accroche

logger = logging.getLogger(__name__)


def generer_pdf_lm(profil, ent: Candidature):
    """
    Génère le PDF de lettre de motivation pour une Candidature.
    Les données entreprise sont lues via ent.entreprise (FK → EntrepriseReferentiel).
    """
    buffer = io.BytesIO()
    p = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4
    styles = getSampleStyleSheet()

    style_corps = styles["Normal"]
    style_corps.fontName = "Helvetica"
    style_corps.fontSize = 11
    style_corps.leading = 15

    p.setFont("Helvetica-Bold", 11)
    p.drawString(2 * cm, height - 2 * cm, f"{profil.prenom_lm or ''} {profil.nom_lm or ''}")
    p.setFont("Helvetica", 10)
    p.drawString(2 * cm, height - 2.6 * cm, profil.rue or "")
    p.drawString(2 * cm, height - 3.1 * cm, f"{profil.npa or ''} {profil.ville or ''}")
    p.drawString(2 * cm, height - 3.6 * cm, profil.email_lm or "")

    p.setFont("Helvetica-Bold", 11)
    p.drawString(12 * cm, height - 5 * cm, ent.entreprise.raison_sociale)
    p.setFont("Helvetica", 10)
    p.drawString(12 * cm, height - 5.5 * cm, (ent.entreprise.adresse or '')[:40])
    p.drawRightString(
        width - 2 * cm, height - 8.5 * cm,
        f"Fait à {profil.ville or 'Genève'}, le {date.today().strftime('%d.%m.%Y')}"
    )

    accroche = get_accroche(ent.secteur_activite)
    secteur_nom = (ent.secteur_activite or "").strip()

    tpl = None
    if secteur_nom:
        tpl = LettreSecteurTemplate.objects.filter(
            utilisateur=ent.utilisateur, secteur_nom=secteur_nom
        ).first()

    ctx = {
        "accroche": accroche,
        "entreprise": ent.entreprise.raison_sociale,
        "secteur": secteur_nom,
        "ville": profil.ville or "Genève",
        "prenom": profil.prenom_lm or "",
        "nom": profil.nom_lm or "",
    }

    objet = _safe_format(tpl.objet, ctx).strip() if tpl else ""
    if not objet:
        objet = "Candidature spontanée"

    elements = [
        Paragraph(f"<b>Objet : {objet}</b>", styles["Normal"]),
        Spacer(1, 25),
    ]

    if tpl and (tpl.salutation or tpl.paragraph_1 or tpl.paragraph_2 or tpl.paragraph_3 or tpl.paragraph_4 or tpl.conclusion):
        elements.append(Paragraph(_safe_format(tpl.salutation or "Madame, Monsieur,", ctx), style_corps))
        elements.append(Spacer(1, 15))
        for txt in [tpl.paragraph_1, tpl.paragraph_2, tpl.paragraph_3, tpl.paragraph_4]:
            txt = _safe_format(txt, ctx).strip()
            if not txt:
                continue
            elements.append(Paragraph(txt, style_corps))
            elements.append(Spacer(1, 12))
        elements.append(Spacer(1, 10))
        salutation = _safe_format(
            tpl.conclusion or "Je vous prie d'agréer, Madame, Monsieur, l'expression de mes salutations distinguées.",
            ctx,
        ).strip()
        if salutation:
            elements.append(Paragraph(salutation, style_corps))
    else:
        elements.extend([
            Paragraph("Madame, Monsieur,", style_corps),
            Spacer(1, 15),
            Paragraph(
                f"C'est avec un vif intérêt que je me permets de vous adresser ma candidature. "
                f"En effet, je suis particulièrement attiré par {accroche}.",
                style_corps,
            ),
            Spacer(1, 12),
            Paragraph(
                "Souhaitant intégrer une structure dynamique telle que la vôtre, je suis convaincu "
                "que mon expérience et ma motivation sauront répondre à vos exigences.",
                style_corps,
            ),
            Spacer(1, 12),
            Paragraph(
                "Vous trouverez ci-joint mon dossier complet. Je reste à votre entière disposition "
                "pour un entretien afin de vous exposer plus en détail mes motivations.",
                style_corps,
            ),
            Spacer(1, 25),
            Paragraph(
                "Je vous prie d'agréer, Madame, Monsieur, l'expression de mes salutations distinguées.",
                style_corps,
            ),
        ])

    Frame(2 * cm, 4 * cm, 17 * cm, height - 11.5 * cm, showBoundary=0).addFromList(elements, p)

    signature = f"{profil.prenom_lm or ''} {profil.nom_lm or ''}".strip()
    if signature:
        p.setFont("Helvetica-Bold", 11)
        p.drawRightString(width - 2 * cm, 6 * cm, signature)
    p.save()
    buffer.seek(0)

    logger.info(
        "generer_pdf_lm: ent_id=%s secteur=%s tpl=%s",
        ent.pk, secteur_nom, tpl.secteur_nom if tpl else "FALLBACK_GÉNÉRIQUE",
    )
    return buffer.read()


def _generer_zip(profil, entreprises, marquer_traitees=False):
    zip_buffer = io.BytesIO()
    to_update = []
    with zipfile.ZipFile(zip_buffer, 'w') as zf:
        for ent in entreprises:
            zf.writestr(_lm_pdf_name(ent.entreprise.raison_sociale), generer_pdf_lm(profil, ent))
            if marquer_traitees:
                ent.est_dans_paquet = True
                ent.date_traitement = now()
                to_update.append(ent)
    if to_update:
        Candidature.objects.bulk_update(to_update, ["est_dans_paquet", "date_traitement"])
    zip_buffer.seek(0)
    return zip_buffer.read()
