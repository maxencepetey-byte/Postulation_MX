from django.contrib import admin

# Register your models here.
from django.contrib import admin
from .models import EntrepriseCible, DocumentUtilisateur, Recherche

admin.site.register(EntrepriseCible)
admin.site.register(DocumentUtilisateur)
admin.site.register(Recherche)