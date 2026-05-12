from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from core.models import (
    DocumentUtilisateur,
    EntrepriseCible,
    EntrepriseReferentiel,
    ProfilUtilisateur,
    ScanSession,
)
from core.views import get_accroche, verifier_email_existence


# ---------------------------------------------------------------------------
# Utils / fonctions pures
# ---------------------------------------------------------------------------

class UtilsTests(SimpleTestCase):
    def test_verifier_email_existence_returns_false_on_empty(self):
        self.assertFalse(verifier_email_existence(""))
        self.assertFalse(verifier_email_existence(None))

    @patch("core.views._utils.dns.resolver.resolve")
    def test_verifier_email_existence_returns_true_when_dns_ok(self, mock_resolve):
        mock_resolve.return_value = object()
        self.assertTrue(verifier_email_existence("a@b.com"))

    @patch("core.views._utils.dns.resolver.resolve", side_effect=Exception("DNS fail"))
    def test_verifier_email_existence_returns_false_when_dns_fails(self, _):
        self.assertFalse(verifier_email_existence("a@b.com"))

    def test_get_accroche_social_overrides(self):
        class P:
            phrase_informatique = "info"
            phrase_banque = "banque"
            phrase_luxe = "luxe"
            phrase_generale = "gen"

        self.assertIn("engagement", get_accroche(P(), "Social (Action)"))

    def test_get_accroche_fallback_to_phrase_generale(self):
        class P:
            phrase_informatique = "info"
            phrase_banque = "banque"
            phrase_luxe = "luxe"
            phrase_generale = "gen"

        self.assertEqual(get_accroche(P(), "Secteur inconnu"), "gen")


# ---------------------------------------------------------------------------
# Base commune
# ---------------------------------------------------------------------------

class BaseAuthTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="u", password="p")
        self.client.login(username="u", password="p")


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

class DashboardTests(BaseAuthTestCase):
    def _setup_complete_profil(self):
        from core.models import GmailOAuthToken, LettreSecteurTemplate
        profil, _ = ProfilUtilisateur.objects.get_or_create(user=self.user)
        profil.onboarding_done = True
        profil.onboarding_secteurs = "62"
        profil.prenom_lm = "Prenom"
        profil.nom_lm = "Nom"
        profil.email_lm = "a@b.com"
        profil.save(update_fields=["onboarding_done", "onboarding_secteurs", "prenom_lm", "nom_lm", "email_lm"])
        GmailOAuthToken.objects.get_or_create(
            utilisateur=self.user, defaults={"refresh_token": "rt", "access_token": "at"}
        )
        LettreSecteurTemplate.objects.get_or_create(
            utilisateur=self.user, secteur_nom="Email",
            defaults={"paragraph_1": "Template email"},
        )
        # Nom exact issu du NOGA_MAP pour le code "62"
        LettreSecteurTemplate.objects.get_or_create(
            utilisateur=self.user, secteur_nom="Informatique et programmation",
            defaults={"paragraph_1": "Template info"},
        )
        return profil

    def test_dashboard_renders_and_contains_secteurs_uniques_and_packs(self):
        self._setup_complete_profil()
        secteur = "Informatique et programmation"
        session = ScanSession.objects.create(utilisateur=self.user, secteurs=secteur, nb_entreprises=0)
        EntrepriseCible.objects.create(
            utilisateur=self.user, scan_session=session,
            nom="A", email="a@example.com", secteur_activite=secteur, numero_pack=1,
        )
        EntrepriseCible.objects.create(
            utilisateur=self.user, scan_session=session,
            nom="B", email="b@example.com", secteur_activite=secteur, numero_pack=1,
        )

        resp = self.client.get(reverse("dashboard"))
        self.assertEqual(resp.status_code, 200)
        self.assertIn("secteurs_uniques", resp.context)

    def test_first_login_redirects_to_onboarding(self):
        resp = self.client.get(reverse("dashboard"))
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp["Location"].endswith(reverse("onboarding")))


# ---------------------------------------------------------------------------
# Inscription
# ---------------------------------------------------------------------------

class RegisterTests(TestCase):
    def test_register_creates_user_and_redirects_to_onboarding(self):
        resp = self.client.post(reverse("register"), {
            "username": "nouveau_user",
            "password1": "MotDePasseStrong123!",
            "password2": "MotDePasseStrong123!",
        }, follow=True)
        # Doit créer le user
        self.assertTrue(User.objects.filter(username="nouveau_user").exists())
        # Doit finir sur la page onboarding
        self.assertEqual(resp.status_code, 200)
        final_url = resp.redirect_chain[-1][0]
        self.assertIn(reverse("onboarding"), final_url)

    def test_register_invalid_username_already_exists(self):
        User.objects.create_user(username="existant", password="Abc123!")
        resp = self.client.post(reverse("register"), {
            "username": "existant",
            "password1": "MotDePasseStrong123!",
            "password2": "MotDePasseStrong123!",
        })
        # Pas de redirect, formulaire re-rendu avec erreur
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.context["form"].is_valid())


# ---------------------------------------------------------------------------
# AJAX filtrage secteur
# ---------------------------------------------------------------------------

class FiltrerSecteurAjaxTests(BaseAuthTestCase):
    def test_filtrer_secteur_returns_partial_html(self):
        session = ScanSession.objects.create(utilisateur=self.user, secteurs="Informatique", nb_entreprises=0)
        EntrepriseCible.objects.create(
            utilisateur=self.user, scan_session=session,
            nom="A", email="a@example.com", secteur_activite="Informatique", numero_pack=1,
        )
        resp = self.client.get(
            reverse("entreprises_filtrer_secteur"),
            {"secteur": "Informatique"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("tbody", data)
        self.assertIn("packs", data)
        self.assertIn("A", data["tbody"])


# ---------------------------------------------------------------------------
# Génération pack ZIP
# ---------------------------------------------------------------------------

class PackGenerationTests(BaseAuthTestCase):
    @patch("core.views._pdf.generer_pdf_lm", return_value=b"%PDF-1.4 fake")
    def test_telecharger_toutes_lm_marks_entreprises_and_returns_zip(self, _mock_pdf):
        for i in range(3):
            EntrepriseCible.objects.create(
                utilisateur=self.user, nom=f"A{i}", email=f"a{i}@example.com",
                est_dans_paquet=False, numero_pack=1, secteur_activite="Informatique",
            )
        ProfilUtilisateur.objects.get_or_create(user=self.user)

        resp = self.client.get(reverse("telecharger_toutes_lm"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "application/zip")
        self.assertEqual(
            EntrepriseCible.objects.filter(utilisateur=self.user, est_dans_paquet=False).count(), 0,
        )

    @patch("core.views._pdf.generer_pdf_lm", return_value=b"%PDF-1.4 fake")
    def test_telecharger_pack_specifique_saves_document_and_returns_zip(self, _mock_pdf):
        for i in range(2):
            EntrepriseCible.objects.create(
                utilisateur=self.user, nom=f"P2_{i}", email=f"p2_{i}@example.com",
                est_dans_paquet=False, numero_pack=2, secteur_activite="Santé",
            )
        ProfilUtilisateur.objects.get_or_create(user=self.user)

        resp = self.client.get(reverse("telecharger_pack_specifique", args=[2]))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "application/zip")
        self.assertEqual(
            EntrepriseCible.objects.filter(utilisateur=self.user, numero_pack=2, est_dans_paquet=False).count(), 0,
        )


# ---------------------------------------------------------------------------
# Génération PDF
# ---------------------------------------------------------------------------

class PDFGenerationTests(BaseAuthTestCase):
    def _make_ent(self, secteur="Informatique"):
        session = ScanSession.objects.create(utilisateur=self.user, secteurs=secteur, nb_entreprises=0)
        return EntrepriseCible.objects.create(
            utilisateur=self.user, scan_session=session,
            nom="TestCorp SA", email="contact@testcorp.ch",
            secteur_activite=secteur, numero_pack=1,
        )

    def test_generer_pdf_lm_returns_pdf_bytes(self):
        from core.views import generer_pdf_lm
        profil, _ = ProfilUtilisateur.objects.get_or_create(user=self.user)
        profil.prenom_lm = "Jean"
        profil.nom_lm = "Dupont"
        profil.email_lm = "jean@dupont.ch"
        profil.ville = "Genève"
        profil.save()
        ent = self._make_ent()
        pdf = generer_pdf_lm(profil, ent)
        self.assertIsInstance(pdf, bytes)
        self.assertTrue(pdf.startswith(b"%PDF"))

    def test_generer_pdf_lm_with_custom_template(self):
        from core.models import LettreSecteurTemplate
        from core.views import generer_pdf_lm
        profil, _ = ProfilUtilisateur.objects.get_or_create(user=self.user)
        profil.prenom_lm = "Marie"
        profil.nom_lm = "Martin"
        profil.email_lm = "marie@martin.ch"
        profil.save()
        LettreSecteurTemplate.objects.create(
            utilisateur=self.user, secteur_nom="Informatique",
            objet="Candidature dev", salutation="Bonjour,",
            paragraph_1="Je suis développeur.",
        )
        ent = self._make_ent()
        pdf = generer_pdf_lm(profil, ent)
        self.assertIsInstance(pdf, bytes)
        self.assertTrue(pdf.startswith(b"%PDF"))


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------

class ScanFlowTests(BaseAuthTestCase):
    @patch("core.views.scan._run_in_background", side_effect=lambda f, *a, **kw: f(*a, **kw))
    def test_lancer_scan_creates_session_and_entreprises(self, _mock_bg):
        EntrepriseReferentiel.objects.create(
            raison_sociale="RS1", email="x1@example.com", code_noga="62", adresse="Rue 1",
        )
        EntrepriseReferentiel.objects.create(
            raison_sociale="RS2", email="x2@example.com", code_noga="62", adresse="Rue 2",
        )
        resp = self.client.get(reverse("lancer_scan"), {"secteurs": ["62"]})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(ScanSession.objects.filter(utilisateur=self.user).count(), 1)
        self.assertEqual(EntrepriseCible.objects.filter(utilisateur=self.user).count(), 2)


# ---------------------------------------------------------------------------
# Upload / suppression documents
# ---------------------------------------------------------------------------

class DocumentTests(BaseAuthTestCase):
    def _pdf_file(self, name="test.pdf"):
        content = b"%PDF-1.4 1 0 obj<</Type/Catalog>>endobj"
        return ContentFile(content, name=name)

    def test_upload_cv_invalid_file_type_rejected(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        bad_file = SimpleUploadedFile("hack.exe", b"MZ\x00\x00", content_type="application/octet-stream")
        resp = self.client.post(reverse("upload_cv"), {
            "cv_file": bad_file, "nom_doc": "Bad", "type_doc": "CV",
        })
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(DocumentUtilisateur.objects.filter(utilisateur=self.user).count(), 0)

    def test_delete_document_removes_entry(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        # Create a doc directly in DB (bypass file validation)
        doc = DocumentUtilisateur.objects.create(
            utilisateur=self.user, nom_affichage="CV Test",
            type_doc="CV",
            fichier=SimpleUploadedFile("cv.pdf", b"%PDF-1.4", content_type="application/pdf"),
        )
        resp = self.client.post(reverse("delete_document", args=[doc.id]))
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(DocumentUtilisateur.objects.filter(id=doc.id).exists())

    def test_delete_other_user_document_returns_404(self):
        user2 = User.objects.create_user(username="u2", password="p2")
        from django.core.files.uploadedfile import SimpleUploadedFile
        doc = DocumentUtilisateur.objects.create(
            utilisateur=user2, nom_affichage="CV U2",
            type_doc="CV",
            fichier=SimpleUploadedFile("cv2.pdf", b"%PDF-1.4", content_type="application/pdf"),
        )
        resp = self.client.post(reverse("delete_document", args=[doc.id]))
        self.assertEqual(resp.status_code, 404)


# ---------------------------------------------------------------------------
# Gmail OAuth
# ---------------------------------------------------------------------------

class GmailOAuthTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="u2", password="p2")
        self.client.login(username="u2", password="p2")

    def test_gmail_connect_redirects_to_google(self):
        with patch.dict("os.environ", {"GOOGLE_CLIENT_ID": "cid", "GOOGLE_REDIRECT_URI": "http://127.0.0.1:8000/gmail/callback/"}):
            r = self.client.get(reverse("gmail_connect"))
        self.assertEqual(r.status_code, 302)
        self.assertIn("accounts.google.com", r["Location"])

    def test_gmail_callback_saves_tokens(self):
        session = self.client.session
        session["gmail_oauth_state"] = "abc"
        session.save()

        with patch.dict("os.environ", {
            "GOOGLE_CLIENT_ID": "cid",
            "GOOGLE_CLIENT_SECRET": "csec",
            "GOOGLE_REDIRECT_URI": "http://127.0.0.1:8000/gmail/callback/",
        }):
            with patch("core.views.auth.requests.post") as post:
                post.return_value.status_code = 200
                post.return_value.json.return_value = {
                    "access_token": "at",
                    "refresh_token": "rt",
                    "expires_in": 3600,
                    "scope": "https://www.googleapis.com/auth/gmail.compose",
                    "token_type": "Bearer",
                }
                r = self.client.get(reverse("gmail_callback") + "?code=ccc&state=abc")
        self.assertEqual(r.status_code, 302)
        self.user.refresh_from_db()
        # Le token est stocké chiffré, from_db le déchiffre → on lit "rt"
        self.assertEqual(self.user.gmail_oauth.refresh_token, "rt")

    def test_gmail_callback_wrong_state_redirects(self):
        session = self.client.session
        session["gmail_oauth_state"] = "correct"
        session.save()
        r = self.client.get(reverse("gmail_callback") + "?code=xxx&state=wrong")
        self.assertEqual(r.status_code, 302)


# ---------------------------------------------------------------------------
# Sécurité : accès non authentifié
# ---------------------------------------------------------------------------

class SecurityTests(TestCase):
    PROTECTED_URLS = [
        "dashboard",
        "settings_page",
        "historique_scans",
        "telecharger_toutes_lm",
        "gmail_connect",
    ]

    def test_unauthenticated_get_redirects_to_login(self):
        for name in self.PROTECTED_URLS:
            with self.subTest(view=name):
                resp = self.client.get(reverse(name))
                self.assertIn(resp.status_code, [302, 403], msg=f"{name} doit être protégée")
                if resp.status_code == 302:
                    self.assertIn("login", resp["Location"].lower())

    def test_cross_user_scan_session_is_404(self):
        owner = User.objects.create_user(username="owner", password="p")
        viewer = User.objects.create_user(username="viewer", password="p")
        session = ScanSession.objects.create(utilisateur=owner, secteurs="Informatique", nb_entreprises=0)
        self.client.login(username="viewer", password="p")
        resp = self.client.get(reverse("detail_scan", args=[session.id]))
        self.assertEqual(resp.status_code, 404)


# ---------------------------------------------------------------------------
# Cron endpoint
# ---------------------------------------------------------------------------

class CronSyncRegistreTests(SimpleTestCase):
    def test_cron_sync_registre_forbidden_without_token(self):
        resp = self.client.get(reverse("cron_sync_registre"))
        self.assertEqual(resp.status_code, 403)


# ---------------------------------------------------------------------------
# Commandes de gestion : vérification email
# ---------------------------------------------------------------------------

class CheckEmailsCommandTests(SimpleTestCase):
    def test_verifier_email_invalid_format(self):
        from core.management.commands.check_emails import ST_INVALIDE, _verifier_email
        statut, _ = _verifier_email("pas-un-email", timeout=2)
        self.assertEqual(statut, ST_INVALIDE)

    def test_verifier_email_empty(self):
        from core.management.commands.check_emails import ST_INVALIDE, _verifier_email
        statut, _ = _verifier_email("", timeout=2)
        self.assertEqual(statut, ST_INVALIDE)

    def test_verifier_email_no_at_sign(self):
        from core.management.commands.check_emails import ST_INVALIDE, _verifier_email
        statut, _ = _verifier_email("notanemail", timeout=2)
        self.assertEqual(statut, ST_INVALIDE)


# ---------------------------------------------------------------------------
# Chiffrement tokens OAuth
# ---------------------------------------------------------------------------

class TokenEncryptionTests(TestCase):
    def test_tokens_are_encrypted_at_rest_and_decrypted_in_memory(self):
        from core.models import GmailOAuthToken, _decrypt_token
        from django.db import connection

        user = User.objects.create_user(username="enc_user", password="p")
        tok = GmailOAuthToken.objects.create(
            utilisateur=user,
            refresh_token="my_plain_refresh_token",
            access_token="my_plain_access_token",
        )
        # En mémoire : valeur en clair restaurée par save()
        self.assertEqual(tok.refresh_token, "my_plain_refresh_token")
        self.assertEqual(tok.access_token, "my_plain_access_token")

        # En DB : valeur chiffrée (doit différer du plain text)
        with connection.cursor() as cur:
            cur.execute("SELECT refresh_token, access_token FROM core_gmailoauthtoken WHERE id=%s", [tok.id])
            row = cur.fetchone()
        self.assertNotEqual(row[0], "my_plain_refresh_token")
        self.assertNotEqual(row[1], "my_plain_access_token")
        # La valeur chiffrée doit être déchiffrable
        self.assertEqual(_decrypt_token(row[0]), "my_plain_refresh_token")

    def test_from_db_decrypts_transparently(self):
        from core.models import GmailOAuthToken

        user = User.objects.create_user(username="enc_user2", password="p")
        GmailOAuthToken.objects.create(
            utilisateur=user,
            refresh_token="secret_refresh",
            access_token="secret_access",
        )
        loaded = GmailOAuthToken.objects.get(utilisateur=user)
        self.assertEqual(loaded.refresh_token, "secret_refresh")
        self.assertEqual(loaded.access_token, "secret_access")
