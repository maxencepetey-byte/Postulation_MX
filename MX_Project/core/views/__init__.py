"""Façade publique des vues : réexporte tous les endpoints routés par `MX_Project.urls`."""

from .auth import (
    gmail_callback,
    gmail_connect,
    gmail_disconnect,
    logout_view,
    register,
)
from .dashboard import (
    add_secteurs,
    dashboard,
    entreprises_filtrer_secteur,
    onboarding,
    settings_page,
)
from .documents import (
    delete_document,
    delete_pack,
    supprimer_documents,
    supprimer_tout,
    upload_cv,
    vider_liste_et_documents,
)
from .downloads import (
    generer_pack_secteur_numero,
    serve_protected_media,
    telecharger_lm,
)
from .gmail import (
    creer_brouillons_gmail,
    gmail_progress,
)
from .scan import (
    cron_sync_registre,
    detail_scan,
    historique_scans,
    lancer_scan,
)
from ._utils import get_accroche
from ._pdf import generer_pdf_lm
