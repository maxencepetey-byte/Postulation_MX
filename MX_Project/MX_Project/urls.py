from django.contrib import admin
from django.urls import path
from django.conf import settings
from django.conf.urls.static import static
from django.contrib.staticfiles.urls import staticfiles_urlpatterns
from django.contrib.auth import views as auth_views
from core.views import (
    dashboard, lancer_scan, telecharger_lm, upload_cv,
    supprimer_tout, settings_page, supprimer_documents,
    creer_brouillons_gmail, register, telecharger_toutes_lm,
    telecharger_pack_specifique,
    entreprises_filtrer_secteur,
    generer_pack_500_lm,
    logout_view,
    onboarding,
    gmail_connect,
    gmail_callback,
    gmail_disconnect,
)

urlpatterns = [
    # Admin Django (fonctionnel maintenant que django.contrib.admin est dans INSTALLED_APPS)
    path('admin/', admin.site.urls),

    # Authentification
    path('login/', auth_views.LoginView.as_view(template_name='registration/login.html'), name='login'),
    path('logout/', logout_view, name='logout'),
    path('register/', register, name='register'),

    # Application
    path('', dashboard, name='dashboard'),
    path('onboarding/', onboarding, name='onboarding'),
    path('scan/', lancer_scan, name='lancer_scan'),
    path('upload-cv/', upload_cv, name='upload_cv'),
    path('telecharger-lm/<int:ent_id>/', telecharger_lm, name='telecharger_lm'),
    path('settings/', settings_page, name='settings_page'),

    # Packs & Téléchargements
    path('download-all-zip/', telecharger_toutes_lm, name='telecharger_toutes_lm'),
    path('download-pack/<int:pack_num>/', telecharger_pack_specifique, name='telecharger_pack_specifique'),
    path('packs/generer-500/', generer_pack_500_lm, name='generer_pack_500_lm'),

    # Nettoyage & Gmail
    path('delete-all/', supprimer_tout, name='supprimer_tout'),
    path('delete-docs/', supprimer_documents, name='supprimer_documents'),
    path('gmail-drafts/', creer_brouillons_gmail, name='creer_brouillons_gmail'),
    path("gmail/connect/", gmail_connect, name="gmail_connect"),
    path("gmail/callback/", gmail_callback, name="gmail_callback"),
    path("gmail/disconnect/", gmail_disconnect, name="gmail_disconnect"),
    path('entreprises/filtrer-secteur', entreprises_filtrer_secteur, name='entreprises_filtrer_secteur'),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    urlpatterns += staticfiles_urlpatterns()
