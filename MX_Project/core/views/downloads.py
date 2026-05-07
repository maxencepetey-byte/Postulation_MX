import logging
import mimetypes
import os
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.files.base import ContentFile
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.http import require_POST

from ..models import DocumentUtilisateur, EntrepriseCible, ProfilUtilisateur
from ._pdf import _generer_zip, generer_pdf_lm
from ._utils import _slugify_loose

logger = logging.getLogger(__name__)


@login_required
def telecharger_toutes_lm(request):
    entreprises = list(
        EntrepriseCible.objects.filter(utilisateur=request.user, est_dans_paquet=False)
        .exclude(email="")[:500]
    )
    if not entreprises:
        return redirect('dashboard')

    profil, _ = ProfilUtilisateur.objects.get_or_create(user=request.user)
    zip_bytes = _generer_zip(profil, entreprises, marquer_traitees=True)

    response = HttpResponse(zip_bytes, content_type='application/zip')
    response['Content-Disposition'] = 'attachment; filename="Pack_Candidatures_MX.zip"'
    return response


@login_required
@require_POST
def generer_pack_500_lm(request):
    secteur = (request.POST.get("secteur") or "").strip()
    qs = EntrepriseCible.objects.filter(utilisateur=request.user, est_dans_paquet=False).exclude(email="").order_by("id")
    if secteur:
        qs = qs.filter(secteur_activite=secteur)

    entreprises = list(qs[:500])
    if not entreprises:
        return redirect('dashboard')

    profil, _ = ProfilUtilisateur.objects.get_or_create(user=request.user)
    zip_bytes = _generer_zip(profil, entreprises)

    packs_user_qs = DocumentUtilisateur.objects.filter(utilisateur=request.user, type_doc='PACK_LM')
    if secteur:
        packs_user_qs = packs_user_qs.filter(secteur_nom=secteur)
    pack_num = packs_user_qs.count() + 1

    if secteur:
        secteur_clean = secteur.replace(" ", "_").replace("/", "-")
        nom_base = f"MX_SCAN_{secteur_clean}_PACK_{pack_num}"
    else:
        nom_base = f"MX_PACK_{pack_num}"

    doc = DocumentUtilisateur(
        utilisateur=request.user,
        nom_affichage=nom_base,
        type_doc='PACK_LM',
        secteur_nom=secteur or "MULTI",
    )
    doc.fichier.save(f"{nom_base}.zip", ContentFile(zip_bytes), save=True)
    return redirect('dashboard')


@login_required
@require_POST
def generer_pack_secteur_numero(request, pack_num: int):
    secteur = (request.POST.get("secteur") or "").strip()
    if not secteur or pack_num < 1:
        return redirect("dashboard")

    entreprises = list(
        EntrepriseCible.objects.filter(
            utilisateur=request.user,
            est_dans_paquet=False,
            secteur_activite=secteur,
            numero_pack=pack_num,
        )
        .exclude(email="")
        .order_by("id")[:500]
    )
    if not entreprises:
        return redirect("dashboard")

    profil, _ = ProfilUtilisateur.objects.get_or_create(user=request.user)
    zip_bytes = _generer_zip(profil, entreprises)

    secteur_clean = secteur.replace(" ", "_").replace("/", "-")
    nom_base = f"MX_SCAN_{secteur_clean}_PACK_{pack_num}"

    existing = DocumentUtilisateur.objects.filter(
        utilisateur=request.user,
        type_doc="PACK_LM",
        secteur_nom=secteur,
        nom_affichage=nom_base,
    ).first()
    if existing:
        messages.info(request, "Pack déjà généré.")
        return redirect(f"/?{urlencode({'secteur': secteur})}")

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
def telecharger_pack_specifique(request, pack_num):
    entreprises = list(
        EntrepriseCible.objects.filter(utilisateur=request.user, numero_pack=pack_num, est_dans_paquet=False)
        .exclude(email="")
    )
    if not entreprises:
        return redirect('dashboard')

    profil, _ = ProfilUtilisateur.objects.get_or_create(user=request.user)
    premier_secteur = entreprises[0].secteur_activite or 'General'
    secteur_clean = premier_secteur.replace(' ', '_').replace('/', '-')
    nom_base = f"MX_SCAN_{secteur_clean}_PACK_{pack_num}"
    nom_zip = f"{nom_base}.zip"

    zip_bytes = _generer_zip(profil, entreprises, marquer_traitees=True)

    doc = DocumentUtilisateur(
        utilisateur=request.user,
        nom_affichage=nom_base,
        type_doc='PACK_LM',
        secteur_nom=premier_secteur,
    )
    doc.fichier.save(nom_zip, ContentFile(zip_bytes), save=True)

    response = HttpResponse(zip_bytes, content_type='application/zip')
    response['Content-Disposition'] = f'attachment; filename="{nom_zip}"'
    return response


@login_required
def telecharger_lm(request, ent_id):
    ent = get_object_or_404(EntrepriseCible, id=ent_id, utilisateur=request.user)
    profil, _ = ProfilUtilisateur.objects.get_or_create(user=request.user)
    try:
        pdf_bytes = generer_pdf_lm(profil, ent)
    except Exception:
        logger.exception("telecharger_lm failed user=%s ent_id=%s", request.user.id, ent_id)
        return HttpResponse(
            "Erreur lors de la génération du PDF. Regarde les logs Render pour le détail.",
            status=500,
            content_type="text/plain; charset=utf-8",
        )
    resp = HttpResponse(pdf_bytes, content_type="application/pdf")
    resp["Content-Disposition"] = f'attachment; filename="LM_{_slugify_loose(ent.nom or "lettre")}.pdf"'
    return resp


@login_required
def serve_protected_media(request, path):
    media_root = settings.MEDIA_ROOT
    full_path = os.path.normpath(os.path.join(media_root, path))

    if not full_path.startswith(os.path.normpath(media_root) + os.sep):
        raise Http404

    if not os.path.isfile(full_path):
        raise Http404

    content_type, _ = mimetypes.guess_type(full_path)
    return FileResponse(open(full_path, "rb"), content_type=content_type or "application/octet-stream")
