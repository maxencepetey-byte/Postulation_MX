from django.contrib import admin
from .models import Candidature, DocumentUtilisateur


class UserFilteredAdmin(admin.ModelAdmin):
    """Base class : les superusers voient tout, le staff ne voit que ses propres données."""

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if request.user.is_superuser:
            return qs
        return qs.filter(utilisateur=request.user)

    def save_model(self, request, obj, form, change):
        if not change:
            obj.utilisateur = request.user
        super().save_model(request, obj, form, change)


@admin.register(Candidature)
class CandidatureAdmin(UserFilteredAdmin):
    list_display = ("entreprise", "secteur_activite", "statut", "est_dans_paquet", "utilisateur")
    list_filter = ("statut", "secteur_activite", "est_dans_paquet")
    search_fields = ("entreprise__raison_sociale", "entreprise__email")
    raw_id_fields = ("entreprise",)


@admin.register(DocumentUtilisateur)
class DocumentUtilisateurAdmin(UserFilteredAdmin):
    list_display = ("nom_affichage", "type_doc", "date_upload", "utilisateur")
    list_filter = ("type_doc",)
    search_fields = ("nom_affichage",)
