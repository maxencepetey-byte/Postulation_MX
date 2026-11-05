"""Vues documents utilisateur : upload CV, suppression unitaire et purge groupée."""

import logging
import re

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect
from urllib.parse import urlencode
from django.views.decorators.http import require_POST

from ..models import Candidature, DocumentUtilisateur

logger = logging.getLogger(__name__)


def _delete_all_user_documents(user) -> int:
    """Supprime tous les DocumentUtilisateur d'un user, en essayant aussi d'effacer les fichiers du storage."""
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
    """Upload d'un document utilisateur (CV ou autre pièce jointe)."""
    if request.method == 'POST' and request.FILES.get('cv_file'):
        type_doc = request.POST.get('type_doc', 'CV')
        # PACK_LM et GUIDE sont générés par l'application, pas uploadés.
        if type_doc not in ("CV", "CERTIFICAT", "DIPLOME", "AUTRE"):
            type_doc = "AUTRE"
        doc = DocumentUtilisateur(
            utilisateur=request.user,
            nom_affichage=re.sub(r'[\x00-\x1f\x7f]', ' ', request.POST.get('nom_doc') or '').strip()[:100] or 'Document',
            type_doc=type_doc,
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
    """Supprime un document unitaire (fichier + enregistrement)."""
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
    """Supprime un pack ZIP de LM et réinitialise les candidatures associées (redevient à traiter)."""
    doc = get_object_or_404(DocumentUtilisateur, id=doc_id, utilisateur=request.user, type_doc="PACK_LM")
    secteur = doc.secteur_nom or ""

    m = re.search(r'_PACK_(\d+)$', doc.nom_affichage)
    pack_num = int(m.group(1)) if m else None

    with transaction.atomic():
        nb = 0
        if pack_num:
            # Les candidatures déjà parties en brouillon Gmail restent traitées : sinon elles
            # repartiraient en double au prochain lot.
            nb = (
                Candidature.objects
                .filter(utilisateur=request.user, secteur_activite=secteur, numero_pack=pack_num,
                        brouillon_gmail_cree=False)
                .update(est_dans_paquet=False, date_traitement=None)
            )

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
    """Vide la liste de candidatures ; les packs ZIP, devenus orphelins, partent avec."""
    with transaction.atomic():
        Candidature.objects.filter(utilisateur=request.user).delete()
        for doc in DocumentUtilisateur.objects.filter(utilisateur=request.user, type_doc="PACK_LM"):
            try:
                doc.fichier.delete(save=False)
            except Exception:
                pass
            doc.delete()
    messages.success(request, "Liste des candidatures vidée.")
    return redirect('dashboard')


@login_required
@require_POST
def supprimer_documents(request):
    """Supprime tous les documents de l'utilisateur (garde la liste de candidatures)."""
    _delete_all_user_documents(request.user)
    return redirect('dashboard')


@login_required
@require_POST
def vider_liste_et_documents(request):
    """Reset complet : candidatures + documents (CV et packs), sans toucher à l'historique de scan."""
    try:
        with transaction.atomic():
            Candidature.objects.filter(utilisateur=request.user).delete()
            _delete_all_user_documents(request.user)
        messages.success(request, "Liste et documents (CV + packs ZIP) vidés.")
    except Exception:
        logger.exception("vider_liste_et_documents failed user=%s", request.user.id)
        messages.error(request, "La suppression a échoué. Merci de réessayer.")
    return redirect("dashboard")
