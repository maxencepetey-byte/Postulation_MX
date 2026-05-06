from django.contrib import admin
from .models import EntrepriseCible, DocumentUtilisateur, Recherche


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


@admin.register(EntrepriseCible)
class EntrepriseCibleAdmin(UserFilteredAdmin):
    list_display = ("nom", "email", "secteur_activite", "statut", "utilisateur")
    list_filter = ("statut", "secteur_activite")
    search_fields = ("nom", "email")


@admin.register(DocumentUtilisateur)
class DocumentUtilisateurAdmin(UserFilteredAdmin):
    list_display = ("nom_affichage", "type_doc", "date_upload", "utilisateur")
    list_filter = ("type_doc",)
    search_fields = ("nom_affichage",)


@admin.register(Recherche)
class RechercheAdmin(UserFilteredAdmin):
    list_display = ("secteur_noga", "date_recherche", "utilisateur")
    list_filter = ("secteur_noga",)