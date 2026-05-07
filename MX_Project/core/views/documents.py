import logging
import re

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect
from urllib.parse import urlencode
from django.views.decorators.http import require_POST

from ..models import DocumentUtilisateur, EntrepriseCible

logger = logging.getLogger(__name__)


def _delete_all_user_documents(user) -> int:
    docs = list(DocumentUtilisateur.objects.filter(utilisateur=user).only("id", "fichier"))
    file_names = []
    for d in docs:
        try:
            if getattr(d, "fichier", None) and getattr(d.fichier, "name", ""):
                file_names.append(d.fichier.name)
        except Exception:
            continue

    DocumentUtilisateur.objects.filter(utilisateur=user).delete()

    try:
        from django.core.files.storage import default_storage
        for name in file_names:
            try:
                default_storage.delete(name)
            except Exception:
                continue
    except Exception:
        pass

    return len(docs)


@login_required
def upload_cv(request):
    if request.method == 'POST' and request.FILES.get('cv_file'):
        doc = DocumentUtilisateur(
            utilisateur=request.user,
            nom_affichage=request.POST.get('nom_doc', 'Document'),
            type_doc=request.POST.get('type_doc', 'CV'),
            fichier=request.FILES['cv_file'],
        )
        try:
            doc.full_clean()
            doc.save()
            messages.success(request, "Document ajouté avec succès.")
        except ValidationError as e:
            messages.error(request, " ".join(e.messages))
    elif request.method == 'POST':
        messages.error(request, "Aucun fichier sélectionné.")
    return redirect('dashboard')


@login_required
@require_POST
def delete_document(request, doc_id: int):
    doc = get_object_or_404(DocumentUtilisateur, id=doc_id, utilisateur=request.user)
    try:
        if getattr(doc, "fichier", None):
            doc.fichier.delete(save=False)
    except Exception:
        pass
    doc.delete()
    messages.success(request, "Document supprimé.")
    return redirect("dashboard")


@login_required
@require_POST
def delete_pack(request, doc_id: int):
    doc = get_object_or_404(DocumentUtilisateur, id=doc_id, utilisateur=request.user, type_doc="PACK_LM")
    secteur = doc.secteur_nom or ""

    m = re.search(r'_PACK_(\d+)$', doc.nom_affichage)
    pack_num = int(m.group(1)) if m else None

    with transaction.atomic():
        qs = EntrepriseCible.objects.filter(utilisateur=request.user, secteur_activite=secteur)
        if pack_num:
            qs = qs.filter(numero_pack=pack_num)
        nb = qs.count()
        qs.update(est_dans_paquet=False, brouillon_gmail_cree=False, date_traitement=None)

        try:
            if getattr(doc, "fichier", None):
                doc.fichier.delete(save=False)
        except Exception:
            pass
        doc.delete()

    messages.success(request, f"Pack supprimé — {nb} entreprise(s) réinitialisées.")
    return redirect(f"/?{urlencode({'secteur': secteur})}" if secteur else "dashboard")


@login_required
@require_POST
def supprimer_tout(request):
    EntrepriseCible.objects.filter(utilisateur=request.user).delete()
    return redirect('dashboard')


@login_required
@require_POST
def supprimer_documents(request):
    _delete_all_user_documents(request.user)
    return redirect('dashboard')


@login_required
@require_POST
def vider_liste_et_documents(request):
    try:
        with transaction.atomic():
            EntrepriseCible.objects.filter(utilisateur=request.user).delete()
            _delete_all_user_documents(request.user)
        messages.success(request, "Liste et documents (CV + packs ZIP) vidés. L'historique a été conservé.")
        return redirect("dashboard")
    except Exception as e:
        logger.exception("vider_liste_et_documents failed user=%s", request.user.id)
        return HttpResponse(
            f"Erreur suppression: {type(e).__name__}: {str(e)[:500]}",
            status=500,
            content_type="text/plain; charset=utf-8",
        )
