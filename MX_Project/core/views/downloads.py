"""Téléchargements : LM unitaire, pack ZIP par secteur, pack global, media protégés."""

import logging
import os
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.files.base import ContentFile
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.http import require_POST

from ..models import Candidature, DocumentUtilisateur, ProfilUtilisateur
from ._pdf import _generer_zip, generer_pdf_lm
from ._utils import _lm_pdf_name

logger = logging.getLogger(__name__)


@login_required
@require_POST
def generer_pack_secteur_numero(request, pack_num: int):
    """Génère un pack ZIP de LM pour un secteur + numéro de pack donnés, et le range dans les documents."""
    secteur = (request.POST.get("secteur") or "").strip()
    if not secteur or pack_num < 1:
        return redirect("dashboard")

    existing_any = DocumentUtilisateur.objects.filter(
        utilisateur=request.user, type_doc="PACK_LM"
    ).first()
    if existing_any:
        messages.error(
            request,
            f"Tu as déjà un pack « {existing_any.nom_affichage} » dans tes documents. "
            "Supprime-le d'abord pour pouvoir en générer un nouveau."
        )
        return redirect(f"/?{urlencode({'secteur': secteur})}")

    entreprises = list(
        Candidature.objects
        .filter(
            utilisateur=request.user,
            est_dans_paquet=False,
            secteur_activite=secteur,
            numero_pack=pack_num,
            entreprise__email_valide=True,
        )
        .select_related('entreprise', 'utilisateur')
        .order_by("id")[:500]
    )
    if not entreprises:
        messages.info(request, "Aucune entreprise à traiter dans ce pack.")
        return redirect(f"/?{urlencode({'secteur': secteur})}")

    profil, _ = ProfilUtilisateur.objects.get_or_create(utilisateur=request.user)
    zip_bytes = _generer_zip(profil, entreprises)

    secteur_clean = secteur.replace(" ", "_").replace("/", "-")
    nom_base = f"MX_SCAN_{secteur_clean}_PACK_{pack_num}"

    doc = DocumentUtilisateur(
        utilisateur=request.user,
        nom_affichage=nom_base,
        type_doc="PACK_LM",
        secteur_nom=secteur,
    )
    doc.fichier.save(f"{nom_base}.zip", ContentFile(zip_bytes), save=True)
    messages.success(request, "Pack généré et ajouté à tes documents.")
    return redirect(f"/?{urlencode({'secteur': secteur})}")


@login_required
def telecharger_lm(request, ent_id):
    """Télécharge la LM PDF d'une candidature précise, générée à la volée."""
    ent = get_object_or_404(
        Candidature.objects.select_related('entreprise'),
        id=ent_id, utilisateur=request.user,
    )
    profil, _ = ProfilUtilisateur.objects.get_or_create(utilisateur=request.user)
    try:
        pdf_bytes = generer_pdf_lm(profil, ent)
    except Exception:
        logger.exception("telecharger_lm failed user=%s ent_id=%s", request.user.id, ent_id)
        return HttpResponse(
            "Erreur lors de la génération du PDF. Merci de réessayer ; si le problème persiste, contacte le support.",
            status=500,
            content_type="text/plain; charset=utf-8",
        )
    resp = HttpResponse(pdf_bytes, content_type="application/pdf")
    resp["Content-Disposition"] = f'attachment; filename="{_lm_pdf_name(ent.entreprise.raison_sociale)}"'
    return resp


@login_required
def serve_protected_media(request, path):
    """
    Sert un fichier de MEDIA_ROOT uniquement à son propriétaire.
    Le type MIME est imposé (jamais déduit du nom choisi à l'upload) pour empêcher
    qu'un fichier piégé soit interprété comme du HTML.
    """
    doc = DocumentUtilisateur.objects.filter(fichier=path, utilisateur=request.user).first()
    if doc is None:
        raise Http404
    ext = os.path.splitext(path)[1].lower()
    try:
        fh = doc.fichier.open("rb")
    except (OSError, ValueError):
        raise Http404
    nom = f"{doc.nom_affichage or 'document'}{ext}"
    if ext == ".pdf":
        return FileResponse(fh, content_type="application/pdf", filename=nom)
    return FileResponse(fh, content_type="application/octet-stream", as_attachment=True, filename=nom)
