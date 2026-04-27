from django.contrib import admin
from django.urls import path
from django.conf import settings
from django.conf.urls.static import static
from django.contrib.staticfiles.urls import staticfiles_urlpatterns
from django.contrib.auth import views as auth_views
from django.views.generic import TemplateView

from core.views import (
    dashboard, lancer_scan, telecharger_lm, upload_cv,
    supprimer_tout, settings_page, supprimer_documents,
    creer_brouillons_gmail, register, telecharger_toutes_lm,
    telecharger_pack_specifique,
    entreprises_filtrer_secteur,
    generer_pack_500_lm,
    generer_pack_secteur_numero,
    logout_view,
    onboarding,
    gmail_connect,
    gmail_callback,
    gmail_disconnect,
    vider_liste_et_documents,
    delete_document,
    cron_sync_registre,
    cron_sync_view,
    historique_scans,
    detail_scan,
    gmail_progress,
)

urlpatterns = [
    path('admin/', admin.site.urls),

    # ─── Authentification ───
    # /login/  → page d'accueil (landing) qui présente le projet
    path(
        'login/',
        TemplateView.as_view(template_name='registration/login.html'),
        name='login',
    ),
    # /signin/ → vrai formulaire de connexion (pour les utilisateurs existants)
    path(
        'signin/',
        auth_views.LoginView.as_view(template_name='registration/signin.html'),
        name='signin',
    ),
    path('logout/', logout_view, name='logout'),
    path('register/', register, name='register'),

    # ─── Application ───
    path('', dashboard, name='dashboard'),
    path('onboarding/', onboarding, name='onboarding'),
    path('scan/', lancer_scan, name='lancer_scan'),
    path('upload-cv/', upload_cv, name='upload_cv'),
    path('delete-doc/<int:doc_id>/', delete_document, name='delete_document'),
    path('telecharger-lm/<int:ent_id>/', telecharger_lm, name='telecharger_lm'),
    path('settings/', settings_page, name='settings_page'),

    # ─── Historique ───
    path('historique/', historique_scans, name='historique_scans'),
    path('historique/<int:session_id>/', detail_scan, name='detail_scan'),

    # ─── Packs & Téléchargements ───
    path('download-all-zip/', telecharger_toutes_lm, name='telecharger_toutes_lm'),
    path('download-pack/<int:pack_num>/', telecharger_pack_specifique, name='telecharger_pack_specifique'),
    path('packs/generer-500/', generer_pack_500_lm, name='generer_pack_500_lm'),
    path('packs/generer/<int:pack_num>/', generer_pack_secteur_numero, name='generer_pack_secteur_numero'),

    # ─── Nettoyage & Gmail ───
    path('delete-all/', supprimer_tout, name='supprimer_tout'),
    path('delete-docs/', supprimer_documents, name='supprimer_documents'),
    path('vider/', vider_liste_et_documents, name='vider_liste_et_documents'),
    path('gmail-drafts/', creer_brouillons_gmail, name='creer_brouillons_gmail'),
    path('gmail/connect/', gmail_connect, name='gmail_connect'),
    path('gmail/callback/', gmail_callback, name='gmail_callback'),
    path('gmail/disconnect/', gmail_disconnect, name='gmail_disconnect'),
    path('gmail-progress/', gmail_progress, name='gmail_progress'),
    path('entreprises/filtrer-secteur', entreprises_filtrer_secteur, name='entreprises_filtrer_secteur'),

    # ─── Cron ───
    path('cron/sync-registre/', cron_sync_registre, name='cron_sync_registre'),
    path('tasks/sync-data/', cron_sync_view, name='cron_sync'),


]

urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

if settings.DEBUG:
    urlpatterns += staticfiles_urlpatterns()