from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from core.models import EntrepriseCible, ScanSession, Recherche, ProfilUtilisateur
from core.views import verifier_email_existence, get_accroche


class UtilsTests(SimpleTestCase):
    def test_verifier_email_existence_returns_false_on_empty(self):
        self.assertFalse(verifier_email_existence(""))
        self.assertFalse(verifier_email_existence(None))

    @patch("core.views.dns.resolver.resolve")
    def test_verifier_email_existence_returns_true_when_dns_ok(self, mock_resolve):
        mock_resolve.return_value = object()
        self.assertTrue(verifier_email_existence("a@b.com"))

    @patch("core.views.dns.resolver.resolve", side_effect=Exception("DNS fail"))
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


class BaseAuthTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="u", password="p")
        self.client.login(username="u", password="p")


class DashboardTests(BaseAuthTestCase):
    def test_dashboard_renders_and_contains_secteurs_uniques_and_packs(self):
        # mark onboarding complete to avoid redirect
        profil, _ = ProfilUtilisateur.objects.get_or_create(user=self.user)
        profil.onboarding_done = True
        profil.onboarding_secteurs = "62"
        profil.prenom_lm = "Prenom"
        profil.nom_lm = "Nom"
        profil.email_lm = "a@b.com"
        profil.save(update_fields=["onboarding_done", "onboarding_secteurs", "prenom_lm", "nom_lm", "email_lm"])
        session = ScanSession.objects.create(utilisateur=self.user, secteurs="Informatique", nb_entreprises=0)
        EntrepriseCible.objects.create(
            utilisateur=self.user,
            scan_session=session,
            nom="A",
            email="a@example.com",
            secteur_activite="Informatique",
            numero_pack=1,
        )
        EntrepriseCible.objects.create(
            utilisateur=self.user,
            scan_session=session,
            nom="B",
            email="b@example.com",
            secteur_activite="Banque",
            numero_pack=1,
        )

        # force multiple packs (501 total)
        for i in range(3, 503):
            EntrepriseCible.objects.create(
                utilisateur=self.user,
                nom=f"E{i}",
                email=f"e{i}@example.com",
                numero_pack=(i // 500) + 1,
            )

        resp = self.client.get(reverse("dashboard"))
        self.assertEqual(resp.status_code, 200)
        self.assertIn("secteurs_uniques", resp.context)
        # packs are now computed dynamically per selected sector via AJAX

    def test_first_login_redirects_to_onboarding(self):
        resp = self.client.get(reverse("dashboard"))
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp["Location"].endswith(reverse("onboarding")))


class FiltrerSecteurAjaxTests(BaseAuthTestCase):
    def test_filtrer_secteur_returns_partial_html(self):
        session = ScanSession.objects.create(utilisateur=self.user, secteurs="Informatique", nb_entreprises=0)
        EntrepriseCible.objects.create(
            utilisateur=self.user,
            scan_session=session,
            nom="A",
            email="a@example.com",
            secteur_activite="Informatique",
            numero_pack=1,
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


class PackGenerationTests(BaseAuthTestCase):
    @patch("core.views.generer_pdf_lm", return_value=b"%PDF-1.4 fake")
    def test_telecharger_toutes_lm_marks_entreprises_and_returns_zip(self, _mock_pdf):
        for i in range(3):
            EntrepriseCible.objects.create(
                utilisateur=self.user,
                nom=f"A{i}",
                email=f"a{i}@example.com",
                est_dans_paquet=False,
                numero_pack=1,
                secteur_activite="Informatique",
            )
        ProfilUtilisateur.objects.get_or_create(user=self.user)

        resp = self.client.get(reverse("telecharger_toutes_lm"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "application/zip")
        self.assertEqual(
            EntrepriseCible.objects.filter(utilisateur=self.user, est_dans_paquet=False).count(),
            0,
        )

    @patch("core.views.generer_pdf_lm", return_value=b"%PDF-1.4 fake")
    def test_telecharger_pack_specifique_saves_document_and_returns_zip(self, _mock_pdf):
        for i in range(2):
            EntrepriseCible.objects.create(
                utilisateur=self.user,
                nom=f"P2_{i}",
                email=f"p2_{i}@example.com",
                est_dans_paquet=False,
                numero_pack=2,
                secteur_activite="Santé",
            )
        ProfilUtilisateur.objects.get_or_create(user=self.user)

        resp = self.client.get(reverse("telecharger_pack_specifique", args=[2]))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "application/zip")
        self.assertEqual(
            EntrepriseCible.objects.filter(utilisateur=self.user, numero_pack=2, est_dans_paquet=False).count(),
            0,
        )


class ScanFlowTests(BaseAuthTestCase):
    @patch("core.views.verifier_email_existence", return_value=True)
    @patch("core.views.requests.get")
    def test_lancer_scan_creates_session_and_entreprises(self, mock_get, _mock_email):
        mock_get.return_value.json.return_value = {
            "features": [
                {"attributes": {"raison_sociale": "RS1", "email": "x1@example.com", "phys_rue": "Rue", "phys_numrue": "1"}},
                {"attributes": {"raison_sociale": "RS2", "email": "x2@example.com", "phys_rue": "Rue", "phys_numrue": "2"}},
            ]
        }
        Recherche.objects.get_or_create(utilisateur=self.user, secteur_noga="SCAN_GENEVE")

        resp = self.client.get(reverse("lancer_scan"), {"secteurs": ["62"]})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(ScanSession.objects.filter(utilisateur=self.user).count(), 1)
        self.assertEqual(EntrepriseCible.objects.filter(utilisateur=self.user).count(), 2)


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

        with patch.dict(
            "os.environ",
            {
                "GOOGLE_CLIENT_ID": "cid",
                "GOOGLE_CLIENT_SECRET": "csec",
                "GOOGLE_REDIRECT_URI": "http://127.0.0.1:8000/gmail/callback/",
            },
        ):
            with patch("core.views.requests.post") as post:
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
        self.assertEqual(self.user.gmail_oauth.refresh_token, "rt")
