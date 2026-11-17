"""Suite de tests Django : utils, dashboard, scan, documents, Gmail OAuth, sécurité, chiffrement."""

from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from core.models import (
    Candidature,
    DocumentUtilisateur,
    EntrepriseReferentiel,
    ProfilUtilisateur,
)
from core.views import get_accroche


# ---------------------------------------------------------------------------
# Utils / fonctions pures
# ---------------------------------------------------------------------------

class UtilsTests(SimpleTestCase):
    def test_get_accroche_social_overrides(self):
        self.assertIn("engagement", get_accroche("Social (Action)"))

    def test_get_accroche_fallback_to_default(self):
        self.assertEqual(
            get_accroche("Secteur inconnu"),
            "le dynamisme et les projets de votre entreprise",
        )

    def test_get_accroche_reconnait_les_libelles_noga(self):
        self.assertIn("numérique", get_accroche("Informatique et programmation"))
        self.assertIn("soins", get_accroche("Santé humaine"))
        self.assertIn("institution financière", get_accroche("Services financiers (Banques)"))
        self.assertIn("engagement", get_accroche("Action sociale sans hébergement"))

    def test_get_accroche_commerce_de_detail_reste_generique(self):
        self.assertEqual(
            get_accroche("Commerce de détail (incl. Luxe)"),
            get_accroche("Secteur inconnu"),
        )

    def test_get_accroche_sieges_sociaux_n_est_pas_du_social(self):
        self.assertEqual(
            get_accroche("Conseil de gestion (Sièges sociaux)"),
            "le dynamisme et les projets de votre entreprise",
        )

    def test_decouper_secteurs_garde_les_libelles_a_virgule(self):
        from core.views.scan import _decouper_secteurs
        self.assertEqual(
            _decouper_secteurs("Sport, loisirs et récréation, Restauration, Activités spécialisées (Design, Photo)"),
            ["Sport, loisirs et récréation", "Restauration", "Activités spécialisées (Design, Photo)"],
        )
        self.assertEqual(_decouper_secteurs("Services financiers (Banques)"), ["Services financiers (Banques)"])
        self.assertEqual(_decouper_secteurs(""), [])


# ---------------------------------------------------------------------------
# Base commune
# ---------------------------------------------------------------------------

class BaseAuthTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="u", password="p")
        self.client.login(username="u", password="p")

    def _make_ref(self, email="contact@testcorp.ch", nom="TestCorp SA", code_noga="62"):
        return EntrepriseReferentiel.objects.get_or_create(
            email=email,
            defaults={"raison_sociale": nom, "code_noga": code_noga, "adresse": "Rue 1"},
        )[0]

    def _make_candidature(self, ref=None, secteur="Informatique et programmation", pack=1):
        if ref is None:
            ref = self._make_ref()
        return Candidature.objects.get_or_create(
            utilisateur=self.user,
            entreprise=ref,
            defaults={"secteur_activite": secteur, "numero_pack": pack, "secteurs": secteur},
        )[0]


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

class DashboardTests(BaseAuthTestCase):
    def _setup_complete_profil(self):
        from core.models import GmailOAuthToken, LettreSecteurTemplate
        profil, _ = ProfilUtilisateur.objects.get_or_create(utilisateur=self.user)
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
        LettreSecteurTemplate.objects.get_or_create(
            utilisateur=self.user, secteur_nom="Informatique et programmation",
            defaults={"paragraph_1": "Template info"},
        )
        return profil

    def test_dashboard_renders_and_contains_secteurs_uniques_and_packs(self):
        self._setup_complete_profil()
        secteur = "Informatique et programmation"
        ref_a = EntrepriseReferentiel.objects.create(
            raison_sociale="A", email="a@example.com", code_noga="62"
        )
        ref_b = EntrepriseReferentiel.objects.create(
            raison_sociale="B", email="b@example.com", code_noga="62"
        )
        Candidature.objects.create(
            utilisateur=self.user, entreprise=ref_a,
            secteur_activite=secteur, numero_pack=1, secteurs=secteur,
        )
        Candidature.objects.create(
            utilisateur=self.user, entreprise=ref_b,
            secteur_activite=secteur, numero_pack=1, secteurs=secteur,
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
        self.assertTrue(User.objects.filter(username="nouveau_user").exists())
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
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.context["form"].is_valid())


# ---------------------------------------------------------------------------
# AJAX filtrage secteur
# ---------------------------------------------------------------------------

class FiltrerSecteurAjaxTests(BaseAuthTestCase):
    def test_filtrer_secteur_returns_partial_html(self):
        ref = EntrepriseReferentiel.objects.create(
            raison_sociale="A", email="a@example.com", code_noga="62"
        )
        Candidature.objects.create(
            utilisateur=self.user, entreprise=ref,
            secteur_activite="Informatique", numero_pack=1, secteurs="Informatique",
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
# Génération PDF
# ---------------------------------------------------------------------------

class PDFGenerationTests(BaseAuthTestCase):
    def _make_ent(self, secteur="Informatique"):
        ref, _ = EntrepriseReferentiel.objects.get_or_create(
            email="contact@testcorp.ch",
            defaults={"raison_sociale": "TestCorp SA", "code_noga": "62", "adresse": "Rue Test 1"},
        )
        cand, _ = Candidature.objects.get_or_create(
            utilisateur=self.user,
            entreprise=ref,
            defaults={"secteur_activite": secteur, "numero_pack": 1, "secteurs": secteur},
        )
        return cand

    def test_generer_pdf_lm_returns_pdf_bytes(self):
        from core.views import generer_pdf_lm
        profil, _ = ProfilUtilisateur.objects.get_or_create(utilisateur=self.user)
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
        profil, _ = ProfilUtilisateur.objects.get_or_create(utilisateur=self.user)
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
    def test_lancer_scan_creates_candidatures(self, _mock_bg):
        EntrepriseReferentiel.objects.create(
            raison_sociale="RS1", email="x1@example.com", code_noga="62", adresse="Rue 1",
        )
        EntrepriseReferentiel.objects.create(
            raison_sociale="RS2", email="x2@example.com", code_noga="62", adresse="Rue 2",
        )
        resp = self.client.post(reverse("lancer_scan"), {"secteurs": ["62"]})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(Candidature.objects.filter(utilisateur=self.user).count(), 2)


# ---------------------------------------------------------------------------
# Candidature — tests unitaires
# ---------------------------------------------------------------------------

class CandidatureTests(BaseAuthTestCase):
    def test_candidature_creation(self):
        ref = self._make_ref()
        cand = self._make_candidature(ref=ref)
        self.assertEqual(cand.utilisateur, self.user)
        self.assertEqual(cand.entreprise, ref)
        self.assertEqual(cand.statut, "À traiter")
        self.assertFalse(cand.est_dans_paquet)

    def test_candidature_unique_per_user_entreprise(self):
        from django.db import IntegrityError
        ref = self._make_ref()
        self._make_candidature(ref=ref)
        with self.assertRaises(Exception):
            Candidature.objects.create(
                utilisateur=self.user,
                entreprise=ref,
                secteur_activite="Informatique",
                secteurs="Informatique",
            )

    def test_candidature_different_users_same_entreprise(self):
        """Deux users peuvent candidater dans la même entreprise."""
        user2 = User.objects.create_user(username="u2", password="p2")
        ref = self._make_ref()
        c1 = self._make_candidature(ref=ref)
        c2, _ = Candidature.objects.get_or_create(
            utilisateur=user2, entreprise=ref,
            defaults={"secteur_activite": "Informatique", "secteurs": "Informatique"},
        )
        self.assertNotEqual(c1.utilisateur, c2.utilisateur)
        self.assertEqual(c1.entreprise, c2.entreprise)

    def test_historique_par_date_scan(self):
        """L'historique groupe les candidatures par (secteurs, date)."""
        ref_a = EntrepriseReferentiel.objects.create(
            raison_sociale="RefA", email="refa@example.com", code_noga="62"
        )
        ref_b = EntrepriseReferentiel.objects.create(
            raison_sociale="RefB", email="refb@example.com", code_noga="62"
        )
        Candidature.objects.create(
            utilisateur=self.user, entreprise=ref_a,
            secteur_activite="Informatique", secteurs="Informatique", numero_pack=1,
        )
        Candidature.objects.create(
            utilisateur=self.user, entreprise=ref_b,
            secteur_activite="Informatique", secteurs="Informatique", numero_pack=1,
        )

        profil, _ = ProfilUtilisateur.objects.get_or_create(utilisateur=self.user)
        profil.onboarding_done = True
        profil.save()
        from core.models import GmailOAuthToken, LettreSecteurTemplate
        GmailOAuthToken.objects.get_or_create(
            utilisateur=self.user, defaults={"refresh_token": "rt", "access_token": "at"}
        )
        LettreSecteurTemplate.objects.get_or_create(
            utilisateur=self.user, secteur_nom="Email",
            defaults={"paragraph_1": "tpl"},
        )
        LettreSecteurTemplate.objects.get_or_create(
            utilisateur=self.user, secteur_nom="Informatique",
            defaults={"paragraph_1": "tpl"},
        )

        resp = self.client.get(reverse("historique_scans"))
        self.assertEqual(resp.status_code, 200)
        sessions = resp.context["sessions"]
        self.assertGreaterEqual(len(sessions), 1)
        # Toutes les candidatures du même secteur/jour forment une session
        total = sum(s.nb_entreprises for s in sessions)
        self.assertEqual(total, 2)

    def test_migration_preserves_data(self):
        """Vérifie les champs clés sur une Candidature créée directement."""
        ref = EntrepriseReferentiel.objects.create(
            raison_sociale="MigCorp", email="migcorp@example.com", code_noga="62", adresse="Rue Mig 1"
        )
        cand = Candidature.objects.create(
            utilisateur=self.user,
            entreprise=ref,
            secteurs="Informatique et programmation",
            secteur_activite="Informatique et programmation",
            statut="À traiter",
            est_dans_paquet=False,
            numero_pack=1,
            email_valide=True,
            brouillon_gmail_cree=False,
        )
        loaded = Candidature.objects.select_related('entreprise').get(pk=cand.pk)
        self.assertEqual(loaded.entreprise.raison_sociale, "MigCorp")
        self.assertEqual(loaded.entreprise.email, "migcorp@example.com")
        self.assertEqual(loaded.secteurs, "Informatique et programmation")
        self.assertFalse(loaded.est_dans_paquet)
        self.assertTrue(loaded.email_valide)


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
        "gmail_connect",
    ]

    def test_unauthenticated_get_redirects_to_login(self):
        for name in self.PROTECTED_URLS:
            with self.subTest(view=name):
                resp = self.client.get(reverse(name))
                self.assertIn(resp.status_code, [302, 403], msg=f"{name} doit être protégée")
                if resp.status_code == 302:
                    self.assertIn("login", resp["Location"].lower())

    def test_cross_user_detail_scan_is_404(self):
        """Un utilisateur ne peut pas voir le détail de scan d'un autre."""
        owner = User.objects.create_user(username="owner", password="p")
        viewer = User.objects.create_user(username="viewer", password="p")
        ref = EntrepriseReferentiel.objects.create(
            raison_sociale="XCorp", email="x@xcorp.ch", code_noga="62"
        )
        cand = Candidature.objects.create(
            utilisateur=owner, entreprise=ref,
            secteurs="Informatique", secteur_activite="Informatique",
        )
        self.client.login(username="viewer", password="p")
        resp = self.client.get(reverse("detail_scan", args=[cand.id]))
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
        self.assertEqual(tok.refresh_token, "my_plain_refresh_token")
        self.assertEqual(tok.access_token, "my_plain_access_token")

        with connection.cursor() as cur:
            cur.execute("SELECT refresh_token, access_token FROM core_gmailoauthtoken WHERE id=%s", [tok.id])
            row = cur.fetchone()
        self.assertNotEqual(row[0], "my_plain_refresh_token")
        self.assertNotEqual(row[1], "my_plain_access_token")
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


# ---------------------------------------------------------------------------
# Lettre de motivation (format suisse) et corps de l'e-mail
# ---------------------------------------------------------------------------

def _texte_pdf(pdf: bytes) -> str:
    """Contenu brut des flux de page (reportlab : ASCII85 puis Flate)."""
    import base64, re as _re, zlib
    morceaux = []
    for flux in _re.findall(rb"stream\r?\n(.*?)endstream", pdf, _re.S):
        data = flux.strip()
        try:
            if data.endswith(b"~>"):
                data = base64.a85decode(data[:-2])
            data = zlib.decompress(data)
        except Exception:
            pass
        morceaux.append(data.decode("latin-1"))
    return "\n".join(morceaux)


class LettreEtEmailTests(BaseAuthTestCase):
    def setUp(self):
        super().setUp()
        from core.models import LettreSecteurTemplate
        self.profil, _ = ProfilUtilisateur.objects.get_or_create(utilisateur=self.user)
        self.profil.prenom_lm, self.profil.nom_lm = "Camille", "Rochat"
        self.profil.email_lm, self.profil.ville = "camille@example.ch", "Lausanne"
        self.profil.save()
        ref = self._make_ref(email="rh@dupont.ch", nom="Dupont & Fils <SA>")
        self.cand = self._make_candidature(ref=ref)
        LettreSecteurTemplate.objects.create(
            utilisateur=self.user, secteur_nom="Informatique et programmation",
            objet="Candidature – {entreprise}", paragraph_1="TEXTE_LETTRE pour {entreprise} {inconnu}",
        )
        LettreSecteurTemplate.objects.create(
            utilisateur=self.user, secteur_nom="Email", paragraph_1="TEXTE_EMAIL pour {entreprise}",
        )

    def test_safe_format_tolere_variable_inconnue(self):
        from core.views._utils import _safe_format
        self.assertEqual(_safe_format("{entreprise} {x} {", {"entreprise": "A"}), "A {x} {")

    def test_pdf_supporte_caracteres_speciaux_et_format_suisse(self):
        from core.views import generer_pdf_lm
        texte = _texte_pdf(generer_pdf_lm(self.profil, self.cand))
        # reportlab découpe la chaîne en plusieurs Tj autour des caractères spéciaux.
        self.assertIn("(Dupont & Fils <) Tj (SA) Tj (>)", texte)
        self.assertIn("Lausanne, le ", texte)
        self.assertNotIn("Fait à", texte)
        self.assertNotIn("Objet :", texte)

    def test_lettre_longue_tient_sur_plusieurs_pages(self):
        from core.models import LettreSecteurTemplate
        from core.views import generer_pdf_lm
        LettreSecteurTemplate.objects.filter(secteur_nom="Informatique et programmation").update(
            paragraph_1="Très long paragraphe. " * 300, conclusion="FIN_POLITESSE",
        )
        texte = _texte_pdf(generer_pdf_lm(self.profil, self.cand))
        self.assertIn("FIN_POLITESSE", texte)

    def test_corps_email_utilise_le_template_email_et_non_la_lettre(self):
        import email
        from email import policy
        from core.models import LettreSecteurTemplate
        from core.views._lettre import bloc_signature
        from core.views.gmail import construire_brouillon
        templates = {t.secteur_nom: t for t in LettreSecteurTemplate.objects.filter(utilisateur=self.user)}
        cand = Candidature.objects.select_related("entreprise", "utilisateur").get(pk=self.cand.pk)
        raw = construire_brouillon(
            self.profil, cand, templates["Email"], templates,
            [("CV.pdf", b"%PDF-1.4", "application/pdf")], ["Curriculum vitae"], bloc_signature(self.profil),
        )
        msg = email.message_from_bytes(raw, policy=policy.default)
        corps = msg.get_body(("plain",)).get_content()
        self.assertIn("TEXTE_EMAIL pour Dupont & Fils <SA>", corps)
        self.assertNotIn("TEXTE_LETTRE", corps)
        self.assertIn("Camille Rochat", msg["Subject"])
        self.assertIn("&lt;SA&gt;", msg.get_body(("html",)).get_content())
        self.assertEqual(
            [p.get_filename() for p in msg.iter_attachments()],
            ["Lettre de Motivation Dupont & Fils SA.pdf", "CV.pdf"],
        )


class MediaProtegeeTests(BaseAuthTestCase):
    def test_fichier_d_un_autre_utilisateur_inaccessible(self):
        autre = User.objects.create_user(username="autre", password="p")
        doc = DocumentUtilisateur(utilisateur=autre, nom_affichage="CV", type_doc="CV")
        doc.fichier.save("cv.pdf", ContentFile(b"%PDF-1.4"), save=True)
        try:
            resp = self.client.get(f"/media/{doc.fichier.name}")
            self.assertEqual(resp.status_code, 404)
            self.client.login(username="autre", password="p")
            resp = self.client.get(f"/media/{doc.fichier.name}")
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp["Content-Type"], "application/pdf")
            # Fermer le FileResponse (sinon Windows refuse de supprimer le fichier) sans
            # déclencher close_old_connections, qui fermerait la connexion du TestCase.
            from django.core.signals import request_finished
            from django.db import close_old_connections
            request_finished.disconnect(close_old_connections)
            try:
                resp.close()
            finally:
                request_finished.connect(close_old_connections)
        finally:
            doc.fichier.delete(save=False)

    def test_upload_refuse_extension_non_pdf(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        f = SimpleUploadedFile("piege.html", b"%PDF-1.4\n<script>alert(1)</script>", content_type="text/html")
        self.client.post(reverse("upload_cv"), {"cv_file": f, "nom_doc": "x", "type_doc": "CV"})
        self.assertFalse(DocumentUtilisateur.objects.filter(utilisateur=self.user).exists())


class LettreRecopieeDuMailTests(BaseAuthTestCase):
    """Le texte du mail ne doit jamais devenir la lettre de motivation PDF."""

    SECTEUR = "Informatique et programmation"

    def setUp(self):
        super().setUp()
        from core.models import LettreSecteurTemplate
        self.tpl_email = LettreSecteurTemplate.objects.create(
            utilisateur=self.user, secteur_nom="Email", paragraph_1="TEXTE_DU_MAIL court.",
        )

    def _save(self, secteur, p1, **extra):
        return self.client.post(reverse("settings_page"), {
            "action": "save_template", "template_secteur": secteur, "paragraph_1": p1, **extra,
        })

    def test_enregistrement_refuse_si_identique_au_mail(self):
        from core.models import LettreSecteurTemplate
        self._save(self.SECTEUR, "  texte_du_mail   court. ")
        self.assertFalse(LettreSecteurTemplate.objects.filter(secteur_nom=self.SECTEUR).exists())

    def test_enregistrement_refuse_si_consigne_de_trame_restante(self):
        from core.models import LettreSecteurTemplate
        self._save(self.SECTEUR, "[Présentez-vous : formation, diplôme et expérience.]")
        self.assertFalse(LettreSecteurTemplate.objects.filter(secteur_nom=self.SECTEUR).exists())

    def test_vraie_lettre_enregistree(self):
        from core.models import LettreSecteurTemplate
        self._save(self.SECTEUR, "Titulaire d'un CFC, je souhaite rejoindre {entreprise}.")
        self.assertTrue(LettreSecteurTemplate.objects.filter(secteur_nom=self.SECTEUR).exists())

    def test_pdf_n_utilise_jamais_un_template_recopie_du_mail(self):
        from core.models import LettreSecteurTemplate
        from core.views import generer_pdf_lm
        LettreSecteurTemplate.objects.create(
            utilisateur=self.user, secteur_nom=self.SECTEUR, paragraph_1="TEXTE_DU_MAIL court.",
        )
        profil, _ = ProfilUtilisateur.objects.get_or_create(utilisateur=self.user)
        texte = _texte_pdf(generer_pdf_lm(profil, self._make_candidature()))
        self.assertNotIn("TEXTE_DU_MAIL", texte)

    def test_template_recopie_compte_comme_a_refaire(self):
        from core.models import LettreSecteurTemplate
        from core.views.dashboard import _get_setup_status
        LettreSecteurTemplate.objects.create(
            utilisateur=self.user, secteur_nom=self.SECTEUR, paragraph_1="TEXTE_DU_MAIL court.",
        )
        self._make_candidature()
        self.assertIn(self.SECTEUR, _get_setup_status(self.user)["secteurs_manquants"])


# ---------------------------------------------------------------------------
# Correctifs de l'audit (lot 1)
# ---------------------------------------------------------------------------

class AuditLot1Tests(BaseAuthTestCase):
    def test_detail_scan_apres_minuit_heure_suisse(self):
        """Un scan de 01:30 à Genève tombe la veille en UTC : son détail doit quand même le lister."""
        from datetime import datetime, timezone as dt_timezone
        cand = self._make_candidature()
        Candidature.objects.filter(pk=cand.pk).update(
            date_scan=datetime(2026, 7, 1, 23, 30, tzinfo=dt_timezone.utc),
        )
        resp = self.client.get(reverse("detail_scan", args=[cand.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "TestCorp SA")

    def test_historique_affiche_chaque_secteur_une_seule_fois(self):
        cand = self._make_candidature()
        Candidature.objects.filter(pk=cand.pk).update(secteurs="Services financiers (Banques), Restauration")
        resp = self.client.get(reverse("historique_scans"))
        self.assertContains(resp, "Services financiers (Banques)", count=1)
        self.assertContains(resp, "Restauration", count=1)

    def test_scan_avec_beaucoup_de_secteurs_ne_tronque_pas(self):
        from core.constants import NOGA_MAP
        from core.views.scan import _run_scan_for_user
        codes = sorted(NOGA_MAP)[:30]
        EntrepriseReferentiel.objects.create(raison_sociale="Agri SA", email="agri@example.com", code_noga="01")
        _run_scan_for_user(self.user, codes)
        cand = Candidature.objects.get(utilisateur=self.user, entreprise__email="agri@example.com")
        self.assertGreater(len(cand.secteurs), 255)

    def test_scan_ignore_les_codes_repetes(self):
        from core.constants import NOGA_MAP
        from core.views.scan import _run_scan_for_user
        EntrepriseReferentiel.objects.create(raison_sociale="Agri SA", email="agri@example.com", code_noga="01")
        _run_scan_for_user(self.user, ["01"] * 50)
        cand = Candidature.objects.get(utilisateur=self.user, entreprise__email="agri@example.com")
        self.assertEqual(cand.secteurs, NOGA_MAP["01"])

    def test_decouper_secteurs_reste_rapide_sur_une_longue_chaine(self):
        import time
        from core.views.scan import _decouper_secteurs
        debut = time.perf_counter()
        libelles = _decouper_secteurs(", ".join(["Restauration"] * 3000))
        self.assertEqual(len(libelles), 3000)
        self.assertLess(time.perf_counter() - debut, 1)

    def test_erreur_affichee_en_alert_danger(self):
        from django.contrib import messages
        from django.contrib.messages.storage.base import Message
        self.assertEqual(Message(messages.ERROR, "x").level_tag, "danger error")

    def test_signin_conserve_la_page_demandee(self):
        self.client.logout()
        resp = self.client.get(reverse("login"), {"next": "/historique/"})
        self.assertContains(resp, "/signin/?next=/historique/")
        resp = self.client.get(reverse("signin"), {"next": "/historique/"})
        self.assertContains(resp, 'name="next" value="/historique/"')
        resp = self.client.post(
            reverse("signin"), {"username": "u", "password": "p", "next": "/historique/"},
        )
        self.assertRedirects(resp, "/historique/", fetch_redirect_response=False)

    def test_page_accueil_commence_par_le_doctype(self):
        self.client.logout()
        resp = self.client.get(reverse("login"))
        self.assertTrue(resp.content.lstrip().startswith(b"<!DOCTYPE html>"))


# ---------------------------------------------------------------------------
# Lots de brouillons Gmail (audit, lot 2)
# ---------------------------------------------------------------------------

class LotBrouillonsTests(BaseAuthTestCase):
    def setUp(self):
        super().setUp()
        from core.models import GmailOAuthToken, LettreSecteurTemplate
        GmailOAuthToken.objects.create(utilisateur=self.user, refresh_token="rt", access_token="at")
        profil, _ = ProfilUtilisateur.objects.get_or_create(utilisateur=self.user)
        profil.prenom_lm, profil.nom_lm, profil.email_lm = "Camille", "Rochat", "camille@example.ch"
        profil.onboarding_done, profil.onboarding_secteurs = True, "62"
        profil.save()
        for secteur in ("Email", "Informatique et programmation"):
            LettreSecteurTemplate.objects.create(utilisateur=self.user, secteur_nom=secteur, paragraph_1=f"Texte {secteur}")
        self.cv = DocumentUtilisateur(utilisateur=self.user, type_doc="CV", nom_affichage="CV")
        self.cv.fichier.save("cv.pdf", ContentFile(b"%PDF-1.4"), save=True)
        self.addCleanup(self.cv.fichier.delete, save=False)
        self.cands = [
            self._make_candidature(ref=self._make_ref(email=f"rh{i}@example.ch", nom=f"Societe {i}"))
            for i in range(3)
        ]
        jeton = patch("core.views.gmail._gmail_get_access_token", return_value="jeton")
        jeton.start()
        self.addCleanup(jeton.stop)

    def _lot(self, **champs):
        from core.models import LotBrouillons
        return LotBrouillons.objects.create(utilisateur=self.user, total=len(self.cands), **champs)

    def _traitees(self):
        return Candidature.objects.filter(utilisateur=self.user, brouillon_gmail_cree=True).count()

    def test_lot_complet(self):
        from core.models import LotBrouillons
        from core.views.gmail import _run_brouillons
        lot = self._lot()
        with patch("core.views.gmail._gmail_create_draft") as depot:
            _run_brouillons(lot.pk)
        lot.refresh_from_db()
        self.assertEqual(depot.call_count, 3)
        self.assertEqual((lot.statut, lot.crees, lot.ignores), (LotBrouillons.TERMINE, 3, 0))
        self.assertIsNotNone(lot.date_fin)
        self.assertEqual(self._traitees(), 3)

    def test_arret_brutal_garde_les_brouillons_deja_deposes(self):
        """Le serveur coupé en plein lot : ce qui est déposé reste marqué, le lot suivant ne le refait pas."""
        from core.models import LotBrouillons
        from core.views.gmail import _ouvrir_lot, _run_brouillons
        lot = self._lot()
        with patch("core.views.gmail._gmail_create_draft", side_effect=[None, None, KeyboardInterrupt]):
            with self.assertRaises(KeyboardInterrupt):
                _run_brouillons(lot.pk)
        self.assertEqual(self._traitees(), 2)
        lot.refresh_from_db()
        self.assertEqual((lot.statut, lot.crees), (LotBrouillons.EN_COURS, 2))

        # Le battement s'est arrêté : passé le délai, un nouveau lot reprend la main.
        LotBrouillons.objects.filter(pk=lot.pk).update(date_maj=lot.date_maj - LotBrouillons.DELAI_ABANDON * 2)
        suivant = _ouvrir_lot(self.user, "", None, 1)
        self.assertIsNotNone(suivant)
        lot.refresh_from_db()
        self.assertEqual(lot.statut, LotBrouillons.INTERROMPU)
        with patch("core.views.gmail._gmail_create_draft") as depot:
            _run_brouillons(suivant.pk)
        self.assertEqual(depot.call_count, 1)
        self.assertEqual(self._traitees(), 3)

    def test_un_seul_lot_en_cours_par_utilisateur(self):
        from core.views.gmail import _ouvrir_lot
        self._lot()
        self.assertIsNone(_ouvrir_lot(self.user, "", None, 3))
        autre = User.objects.create_user(username="autre", password="p")
        self.assertIsNotNone(_ouvrir_lot(autre, "", None, 3))

    def test_lancement_refuse_pendant_un_lot(self):
        from core.models import LotBrouillons
        from core.views.gmail import MSG_LOT_DEJA_EN_COURS
        self._lot()
        with patch("core.views.gmail._run_in_background") as fond:
            resp = self.client.post(reverse("creer_brouillons_gmail"), follow=True)
        fond.assert_not_called()
        self.assertIn(MSG_LOT_DEJA_EN_COURS, [str(m) for m in resp.context["messages"]])
        self.assertEqual(LotBrouillons.objects.filter(utilisateur=self.user).count(), 1)

    def test_lancement_cree_le_lot_et_demarre_le_travail(self):
        from core.models import LotBrouillons
        with patch("core.views.gmail._run_in_background") as fond, \
                patch("core.views.gmail.requests.get") as sonde:
            sonde.return_value.status_code = 200
            self.client.post(reverse("creer_brouillons_gmail"), {"secteur": "Informatique et programmation"})
        lot = LotBrouillons.objects.get(utilisateur=self.user)
        self.assertEqual((lot.statut, lot.total, lot.secteur), (LotBrouillons.EN_COURS, 3, "Informatique et programmation"))
        self.assertEqual(fond.call_args.args[1], lot.pk)

    def test_lot_repris_ailleurs_ne_depose_rien(self):
        from core.models import LotBrouillons
        from core.views.gmail import _run_brouillons
        lot = self._lot(statut=LotBrouillons.INTERROMPU)
        with patch("core.views.gmail._gmail_create_draft") as depot:
            _run_brouillons(lot.pk)
        depot.assert_not_called()
        self.assertEqual(self._traitees(), 0)
        lot.refresh_from_db()
        self.assertEqual(lot.statut, LotBrouillons.INTERROMPU)

    def test_lot_repris_en_cours_de_route_s_arrete(self):
        from core.models import LotBrouillons
        from core.views.gmail import _run_brouillons
        lot = self._lot()

        def reprendre_le_lot(*args):
            LotBrouillons.objects.filter(pk=lot.pk).update(statut=LotBrouillons.INTERROMPU, message="repris")

        with patch("core.views.gmail._gmail_create_draft", side_effect=reprendre_le_lot) as depot:
            _run_brouillons(lot.pk)
        self.assertEqual(depot.call_count, 1)
        lot.refresh_from_db()
        self.assertEqual((lot.statut, lot.message), (LotBrouillons.INTERROMPU, "repris"))

    def test_refus_propres_a_une_entreprise_ne_bloquent_pas_le_lot(self):
        from core.models import LotBrouillons
        from core.views.gmail import MAX_ECHECS_DE_SUITE, GmailApiError, _run_brouillons
        for i in range(10, 10 + MAX_ECHECS_DE_SUITE):
            self._make_candidature(ref=self._make_ref(email=f"rh{i}@example.ch", nom=f"Societe {i}"))
        lot = self._lot()
        effets = [GmailApiError(400, "adresse invalide")] * MAX_ECHECS_DE_SUITE + [None, None, None]
        with patch("core.views.gmail._gmail_create_draft", side_effect=effets):
            _run_brouillons(lot.pk)
        lot.refresh_from_db()
        self.assertEqual((lot.statut, lot.crees, lot.ignores), (LotBrouillons.TERMINE, 3, MAX_ECHECS_DE_SUITE))

    def test_gmail_en_panne_arrete_le_lot(self):
        import requests
        from core.models import LotBrouillons
        from core.views.gmail import MAX_ECHECS_DE_SUITE, MSG_GMAIL_INDISPONIBLE, _run_brouillons
        for i in range(10, 10 + MAX_ECHECS_DE_SUITE):
            self._make_candidature(ref=self._make_ref(email=f"rh{i}@example.ch", nom=f"Societe {i}"))
        lot = self._lot()
        # Erreur réseau : jamais retentée (le brouillon a pu être créé), mais comptée comme panne.
        with patch("core.views.gmail._gmail_create_draft", side_effect=requests.ConnectionError("coupé")) as depot, \
                patch("core.views.gmail.time.sleep") as attente:
            _run_brouillons(lot.pk)
        attente.assert_not_called()
        self.assertEqual(depot.call_count, MAX_ECHECS_DE_SUITE)
        lot.refresh_from_db()
        self.assertEqual((lot.statut, lot.message), (LotBrouillons.INTERROMPU, MSG_GMAIL_INDISPONIBLE))

    def test_jeton_expire_en_cours_de_lot_est_rafraichi(self):
        from core.models import LotBrouillons
        from core.views.gmail import GmailApiError, _run_brouillons
        lot = self._lot()
        with patch("core.views.gmail._gmail_create_draft", side_effect=[GmailApiError(401, "expiré"), None, None, None]) as depot:
            _run_brouillons(lot.pk)
        lot.refresh_from_db()
        self.assertEqual(depot.call_count, 4)
        self.assertEqual((lot.statut, lot.crees, lot.ignores), (LotBrouillons.TERMINE, 3, 0))

    def test_jeton_refuse_malgre_le_rafraichissement_arrete_le_lot(self):
        from core.models import LotBrouillons
        from core.views.gmail import MSG_GMAIL_EXPIRE, GmailApiError, _run_brouillons
        lot = self._lot()
        with patch("core.views.gmail._gmail_create_draft", side_effect=GmailApiError(401, "révoqué")) as depot:
            _run_brouillons(lot.pk)
        lot.refresh_from_db()
        self.assertEqual(depot.call_count, 2)
        self.assertEqual((lot.statut, lot.crees, lot.ignores, lot.message), (LotBrouillons.INTERROMPU, 0, 0, MSG_GMAIL_EXPIRE))

    def test_deconnexion_gmail_arrete_le_lot_en_cours(self):
        from core.models import LotBrouillons
        lot = self._lot()
        with patch("core.views.auth.requests.post"):
            self.client.post(reverse("gmail_disconnect"))
        lot.refresh_from_db()
        self.assertEqual(lot.statut, LotBrouillons.INTERROMPU)

    def test_anciens_lots_purges_a_l_ouverture(self):
        from core.models import LotBrouillons
        from core.views.gmail import _ouvrir_lot
        ancien = self._lot(statut=LotBrouillons.TERMINE)
        recent = self._lot(statut=LotBrouillons.TERMINE)
        LotBrouillons.objects.filter(pk=ancien.pk).update(date_maj=ancien.date_maj - LotBrouillons.DUREE_AFFICHAGE * 2)
        nouveau = _ouvrir_lot(self.user, "", None, 1)
        self.assertEqual(
            set(LotBrouillons.objects.values_list("pk", flat=True)), {recent.pk, nouveau.pk},
        )

    def test_numero_de_pack_exotique_ne_plante_pas(self):
        from core.views.gmail import _entier
        self.assertEqual((_entier("12"), _entier("²"), _entier(""), _entier(None), _entier("9" * 12)), (12, None, None, None, None))

    def test_course_au_lancement_refusee_par_la_contrainte(self):
        """Deux requêtes passent le pré-contrôle en même temps : la contrainte d'unicité départage."""
        from core.models import LotBrouillons
        from core.views.gmail import MSG_LOT_DEJA_EN_COURS
        self._lot()
        with patch("core.views.gmail._run_in_background") as fond, \
                patch("core.views.gmail.requests.get") as sonde, \
                patch("core.views.gmail.etat_du_lot", return_value=None):
            sonde.return_value.status_code = 200
            resp = self.client.post(reverse("creer_brouillons_gmail"), follow=True)
        fond.assert_not_called()
        self.assertEqual(LotBrouillons.objects.count(), 1)
        self.assertIn(MSG_LOT_DEJA_EN_COURS, [str(m) for m in resp.context["messages"]])

    def test_refus_temporaire_est_retente(self):
        from core.models import LotBrouillons
        from core.views.gmail import GmailApiError, _run_brouillons
        lot = self._lot()
        effets = [GmailApiError(429, "trop de requêtes"), GmailApiError(503, "indisponible", retry_after=7), None, None, None]
        with patch("core.views.gmail._gmail_create_draft", side_effect=effets), \
                patch("core.views.gmail.time.sleep") as attente:
            _run_brouillons(lot.pk)
        lot.refresh_from_db()
        self.assertEqual((lot.statut, lot.crees, lot.ignores), (LotBrouillons.TERMINE, 3, 0))
        self.assertEqual([a.args[0] for a in attente.call_args_list], [2, 7])

    def test_debit_depasse_arrete_le_lot(self):
        from core.models import LotBrouillons
        from core.views.gmail import DELAIS_REPRISE, MSG_DEBIT, GmailApiError, _run_brouillons
        lot = self._lot()
        with patch("core.views.gmail._gmail_create_draft", side_effect=GmailApiError(429, "quota")) as depot, \
                patch("core.views.gmail.time.sleep"):
            _run_brouillons(lot.pk)
        lot.refresh_from_db()
        # Une seule candidature tentée : le reste du lot n'est pas usé en échecs.
        self.assertEqual(depot.call_count, len(DELAIS_REPRISE) + 1)
        self.assertEqual((lot.statut, lot.crees, lot.message), (LotBrouillons.INTERROMPU, 0, MSG_DEBIT))
        self.assertEqual(self._traitees(), 0)

    def test_refus_definitif_ignore_l_entreprise_sans_attendre(self):
        from core.models import LotBrouillons
        from core.views.gmail import GmailApiError, _run_brouillons
        lot = self._lot()
        with patch("core.views.gmail._gmail_create_draft", side_effect=[GmailApiError(400, "adresse invalide"), None, None]), \
                patch("core.views.gmail.time.sleep") as attente:
            _run_brouillons(lot.pk)
        lot.refresh_from_db()
        attente.assert_not_called()
        self.assertEqual((lot.statut, lot.crees, lot.ignores), (LotBrouillons.TERMINE, 2, 1))

    def test_erreur_gmail_lue_depuis_la_reponse(self):
        from core.views.gmail import GmailApiError, _gmail_create_draft
        with patch("core.views.gmail.requests.post") as post:
            post.return_value.status_code = 429
            post.return_value.text = "{}"
            post.return_value.headers = {"Retry-After": "7"}
            with self.assertRaises(GmailApiError) as ctx:
                _gmail_create_draft("jeton", b"x")
        self.assertEqual((ctx.exception.status, ctx.exception.retry_after), (429, 7))
        self.assertTrue(ctx.exception.temporaire)
        self.assertTrue(GmailApiError(403, '{"reason": "userRateLimitExceeded"}').limite_de_debit)
        self.assertFalse(GmailApiError(403, '{"reason": "insufficientPermissions"}').temporaire)
        self.assertFalse(GmailApiError(400, "").temporaire)

    def test_cv_absent_du_serveur_bloque_le_lancement(self):
        from core.models import LotBrouillons
        from core.views.gmail import MSG_CV_ILLISIBLE
        self.cv.fichier.storage.delete(self.cv.fichier.name)
        with patch("core.views.gmail._run_in_background") as fond:
            resp = self.client.post(reverse("creer_brouillons_gmail"), follow=True)
        fond.assert_not_called()
        self.assertFalse(LotBrouillons.objects.exists())
        self.assertIn(MSG_CV_ILLISIBLE, [str(m) for m in resp.context["messages"]])

    def test_cv_disparu_pendant_l_attente_interrompt_le_lot(self):
        from core.models import LotBrouillons
        from core.views.gmail import MSG_CV_ILLISIBLE, _run_brouillons
        lot = self._lot()
        self.cv.fichier.storage.delete(self.cv.fichier.name)
        with patch("core.views.gmail._gmail_create_draft") as depot:
            _run_brouillons(lot.pk)
        depot.assert_not_called()
        lot.refresh_from_db()
        self.assertEqual((lot.statut, lot.message), (LotBrouillons.INTERROMPU, MSG_CV_ILLISIBLE))

    def test_etat_du_lot_en_json(self):
        from core.models import LotBrouillons
        url = reverse("etat_lot_brouillons")
        self.assertEqual(self.client.get(url).json(), {"en_cours": False, "texte": "", "classe": ""})
        lot = self._lot(crees=1)
        etat = self.client.get(url).json()
        self.assertTrue(etat["en_cours"])
        self.assertIn("1 sur 3", etat["texte"])
        LotBrouillons.objects.filter(pk=lot.pk).update(statut=LotBrouillons.TERMINE, crees=3)
        etat = self.client.get(url).json()
        self.assertEqual((etat["en_cours"], etat["classe"]), (False, "success"))
        self.assertIn("3 brouillon(s) créé(s)", etat["texte"])

    def test_etat_du_lot_reserve_a_son_proprietaire(self):
        self._lot(crees=1)
        User.objects.create_user(username="autre", password="p")
        self.client.login(username="autre", password="p")
        self.assertEqual(self.client.get(reverse("etat_lot_brouillons")).json()["texte"], "")
        self.client.logout()
        self.assertEqual(self.client.get(reverse("etat_lot_brouillons")).status_code, 302)

    def test_lot_sans_battement_affiche_comme_interrompu(self):
        from core.models import LotBrouillons
        from core.views.gmail import MSG_SERVEUR_ARRETE, etat_du_lot
        lot = self._lot(crees=1)
        LotBrouillons.objects.filter(pk=lot.pk).update(date_maj=lot.date_maj - LotBrouillons.DELAI_ABANDON * 2)
        etat = etat_du_lot(self.user)
        self.assertFalse(etat["en_cours"])
        self.assertIn(MSG_SERVEUR_ARRETE, etat["texte"])

    def test_bandeau_affiche_sur_le_tableau_de_bord(self):
        self._lot(crees=2)
        resp = self.client.get(reverse("dashboard"))
        self.assertContains(resp, 'id="lotBrouillons"')
        self.assertContains(resp, "data-en-cours")
        self.assertContains(resp, "2 sur 3")


# ---------------------------------------------------------------------------
# Synchro du registre SITG, purge et scan (audit, lot 3)
# ---------------------------------------------------------------------------

def _faux_registre(lignes, annonce=None, panne_a=None, plafond=1000):
    """Simule l'API du registre : d'abord le nombre de lignes, puis des pages."""
    appels = []

    def repondre(session, url, params, **kwargs):
        appels.append(params)
        if params.get("returnCountOnly"):
            return {"count": len(lignes) if annonce is None else annonce}
        debut = params["resultOffset"]
        if panne_a is not None and debut >= panne_a:
            return None
        page = lignes[debut:debut + min(params["resultRecordCount"], plafond)]
        return {"features": [{"attributes": ligne} for ligne in page]}

    repondre.appels = appels
    return repondre


def _ligne_sitg(i, **champs):
    return {"OBJECTID": i, "RAISON_SOCIALE": f"Societe {i}", "EMAIL": f"rh{i}@example.ch",
            "PHYS_RUE": "Rue du Lac", "PHYS_NUMRUE": str(i), "PHYS_NPA": "1200", "PHYS_LOCALITE": "Genève", **champs}


class FetchSectorTests(SimpleTestCase):
    def _fetch(self, registre, **kwargs):
        from core.management.commands import sync_registre as sr
        with patch.object(sr, "_request_with_retry", side_effect=registre), patch.object(sr.time, "sleep"):
            return sr._fetch_sector("62", session=object(), **kwargs)

    def test_page_plafonnee_par_le_serveur_ne_tronque_pas(self):
        registre = _faux_registre([_ligne_sitg(i) for i in range(5)], plafond=2)
        entreprises, complet = self._fetch(registre, page_size=5)
        self.assertTrue(complet)
        self.assertEqual([e["email"] for e in entreprises], [f"rh{i}@example.ch" for i in range(5)])
        pages = [p for p in registre.appels if "resultOffset" in p]
        self.assertEqual([p["resultOffset"] for p in pages], [0, 2, 4])
        self.assertEqual({p["orderByFields"] for p in pages}, {"objectid"})
        self.assertEqual({p["returnGeometry"] for p in pages}, {"false"})

    def test_page_en_echec_signale_un_secteur_incomplet(self):
        registre = _faux_registre([_ligne_sitg(i) for i in range(5)], plafond=2, panne_a=2)
        entreprises, complet = self._fetch(registre)
        self.assertFalse(complet)
        self.assertEqual(len(entreprises), 2)

    def test_moins_de_lignes_qu_annonce_signale_un_secteur_incomplet(self):
        _, complet = self._fetch(_faux_registre([_ligne_sitg(i) for i in range(3)], annonce=5))
        self.assertFalse(complet)

    def test_nombre_de_lignes_indisponible(self):
        from core.management.commands import sync_registre as sr
        with patch.object(sr, "_request_with_retry", return_value=None):
            self.assertEqual(sr._fetch_sector("62", session=object()), ([], False))

    def test_champs_trop_longs(self):
        lignes = [
            _ligne_sitg(1, RAISON_SOCIALE="N" * 400),
            _ligne_sitg(2, EMAIL="a" * 300 + "@example.ch"),
            _ligne_sitg(3, EMAIL="  RH3@Example.CH "),
        ]
        entreprises, complet = self._fetch(_faux_registre(lignes))
        self.assertTrue(complet)
        self.assertEqual([(len(e["nom"]), e["email"]) for e in entreprises],
                         [(255, "rh1@example.ch"), (9, "rh3@example.ch")])

    def test_valeurs_piegees_du_registre(self):
        lignes = [
            _ligne_sitg(1, EMAIL="info@example.ch; rh@example.ch", RAISON_SOCIALE="Al\x00pha", PHYS_RUE="Rue\x00 X"),
            _ligne_sitg(2, EMAIL="pas-une-adresse"),
            _ligne_sitg(3, EMAIL=None),
            _ligne_sitg(4, EMAIL=12, RAISON_SOCIALE=34),
            _ligne_sitg(5, PHYS_RUE="R" * 400, PHYS_NPA=1200),
        ]
        entreprises, complet = self._fetch(_faux_registre(lignes))
        self.assertTrue(complet)
        self.assertEqual([(e["email"], e["nom"]) for e in entreprises],
                         [("info@example.ch", "Alpha"), ("rh5@example.ch", "Societe 5")])
        self.assertEqual(entreprises[0]["adresse"], "Rue X 1")
        self.assertEqual((len(entreprises[1]["adresse"]), entreprises[1]["npa"]), (255, "1200"))

    def test_nombre_annonce_aberrant(self):
        entreprises, complet = self._fetch(_faux_registre([_ligne_sitg(1)], annonce=10 ** 9))
        self.assertEqual((entreprises, complet), ([], False))


class ValidationEmailsTests(SimpleTestCase):
    def test_un_worker_coince_ne_fait_pas_perdre_les_autres_adresses(self):
        import threading
        from core.management.commands import sync_registre as sr
        from core.management.commands.check_emails import ST_INVALIDE
        liberer = threading.Event()

        def verifier(email, timeout, mx_only=False):
            if email.startswith("coince"):
                liberer.wait(10)
            return (ST_INVALIDE, "") if email.startswith("ko") else ("ok", "")

        candidats = [{"email": e} for e in ("coince1@x.ch", "coince2@x.ch", "a@x.ch", "b@x.ch", "ko@x.ch", "c@x.ch")]
        try:
            with patch.object(sr, "_verifier_email", side_effect=verifier), \
                    patch.object(sr, "_EMAIL_TIMEOUT", 0.3), patch.object(sr, "_WATCHDOG_SLACK", 0), \
                    patch("builtins.print"):
                valides, rejets, non_verifies = sr._validate_emails(candidats, max_workers=2)
        finally:
            liberer.set()
        self.assertEqual(sorted(e["email"] for e in valides), ["a@x.ch", "b@x.ch", "c@x.ch"])
        self.assertEqual(rejets, [("ko@x.ch", ST_INVALIDE)])
        self.assertEqual(sorted(e["email"] for e in non_verifies), ["coince1@x.ch", "coince2@x.ch"])


class SyncRegistreTests(TestCase):
    def _ent(self, i, **champs):
        return {"nom": f"Societe {i}", "email": f"rh{i}@example.ch", "noga_code": "62",
                "adresse": f"Rue du Lac {i}", "npa": "1200", "localite": "Genève", **champs}

    def _ref(self, i, **champs):
        valeurs = {"raison_sociale": f"Societe {i}", "email": f"rh{i}@example.ch", "code_noga": "62",
                   "id_sitg": i, "adresse": f"Rue du Lac {i}", "npa": "1200", "localite": "Genève", **champs}
        return EntrepriseReferentiel.objects.create(**valeurs)

    def _sync(self, registre, *args, verifier=False):
        """Lance la commande ; `registre` associe un code NOGA à (entreprises, complet)."""
        from io import StringIO
        from django.core.management import call_command
        from core.management.commands import sync_registre as sr
        sortie = StringIO()
        if not verifier:
            args = ("--skip_email_check", *args)
        with patch.object(sr, "_fetch_sector", side_effect=lambda code, **kw: registre.get(code, ([], True))):
            call_command("sync_registre", "--inter_sector_delay", "0", *args, stdout=sortie)
        return sortie.getvalue()

    def _vieillir(self, ref):
        """Fait comme si l'absence avait été constatée la veille."""
        from datetime import timedelta
        ref.refresh_from_db()
        EntrepriseReferentiel.objects.filter(pk=ref.pk).update(absente_depuis=ref.absente_depuis - timedelta(days=1))

    def test_cree_les_nouvelles_et_met_a_jour_les_existantes(self):
        ancienne = self._ref(1, raison_sociale="Ancien nom", email_valide=False)
        self._ref(2)
        registre = {"62": ([self._ent(1), self._ent(2), self._ent(3)], True)}
        self.assertIn("1 candidats nouveaux, 1 à mettre à jour", self._sync(registre))
        ancienne.refresh_from_db()
        self.assertEqual(ancienne.raison_sociale, "Societe 1")
        # Une adresse signalée en erreur le reste : la synchro ne la revalide pas.
        self.assertFalse(ancienne.email_valide)
        # L'OBJECTID du registre n'est plus recopié : il change à chaque publication.
        self.assertTrue(EntrepriseReferentiel.objects.filter(email="rh3@example.ch", id_sitg__isnull=True).exists())
        self.assertEqual(EntrepriseReferentiel.objects.count(), 3)
        # Une deuxième synchro identique ne réécrit rien.
        self.assertIn("0 candidats nouveaux, 0 à mettre à jour", self._sync(registre))

    def test_radiee_seulement_apres_deux_synchros_puis_retablie(self):
        absente = self._ref(1)
        passee_dans_un_autre_secteur = self._ref(9)
        hors_carte = self._ref(8)
        registre = {
            "62": ([self._ent(2)], True),
            "70": ([self._ent(9, noga_code="70")], True),
            "99": ([self._ent(8, noga_code="99")], True),
        }
        self._sync(registre)
        absente.refresh_from_db()
        passee_dans_un_autre_secteur.refresh_from_db()
        hors_carte.refresh_from_db()
        self.assertEqual((absente.radiee, absente.absente_depuis is None), (False, False))
        self.assertEqual(
            (passee_dans_un_autre_secteur.absente_depuis, passee_dans_un_autre_secteur.code_noga), (None, "70"),
        )
        # La division 99 n'est pas dans NOGA_MAP, mais l'entreprise y figure : elle n'est pas absente.
        self.assertIsNone(hors_carte.absente_depuis)

        # Relancée aussitôt, la synchro ne vaut pas confirmation.
        self._sync(registre)
        absente.refresh_from_db()
        self.assertFalse(absente.radiee)

        self._vieillir(absente)
        self._sync(registre)
        absente.refresh_from_db()
        self.assertTrue(absente.radiee)

        self._sync({**registre, "62": ([self._ent(1), self._ent(2)], True)})
        absente.refresh_from_db()
        self.assertEqual((absente.radiee, absente.absente_depuis), (False, None))

    def test_reapparue_avant_confirmation_n_est_plus_absente(self):
        ref = self._ref(1)
        self._sync({"62": ([self._ent(2)], True)})
        self._sync({"62": ([self._ent(1), self._ent(2)], True)})
        ref.refresh_from_db()
        self.assertEqual((ref.radiee, ref.absente_depuis), (False, None))

    def test_synchro_d_une_partie_des_secteurs_ne_marque_rien(self):
        absente = self._ref(1)
        self._sync({"62": ([self._ent(2)], True)}, "--secteurs", "62")
        absente.refresh_from_db()
        self.assertEqual((absente.radiee, absente.absente_depuis), (False, None))

    def test_secteur_incomplet_fait_echouer_la_commande_sans_rien_marquer(self):
        from django.core.management.base import CommandError
        absente = self._ref(1)
        with self.assertRaisesMessage(CommandError, "secteurs incomplets : 62"):
            self._sync({"62": ([self._ent(2)], False)})
        absente.refresh_from_db()
        self.assertEqual((absente.radiee, absente.absente_depuis), (False, None))
        # Ce qui a été reçu est tout de même enregistré.
        self.assertTrue(EntrepriseReferentiel.objects.filter(email="rh2@example.ch").exists())

    def test_trop_d_absentes_ne_marque_rien(self):
        from django.core.management.base import CommandError
        EntrepriseReferentiel.objects.bulk_create([
            EntrepriseReferentiel(raison_sociale=f"S{i}", email=f"rh{i}@example.ch", code_noga="62", id_sitg=i)
            for i in range(60)
        ])
        with self.assertRaisesMessage(CommandError, "trop d'entreprises absentes"):
            self._sync({"62": ([self._ent(i) for i in range(10)], True)})
        self.assertFalse(EntrepriseReferentiel.objects.filter(radiee=True).exists())
        self.assertFalse(EntrepriseReferentiel.objects.filter(absente_depuis__isnull=False).exists())

    def test_aucun_resultat_fait_echouer_la_commande(self):
        from django.core.management.base import CommandError
        with self.assertRaisesMessage(CommandError, "aucun secteur"):
            self._sync({})

    def test_since_hours_est_ignore(self):
        sortie = self._sync({"62": ([self._ent(1)], True)}, "--since_hours", "24")
        self.assertIn("--since_hours ignoré", sortie)
        self.assertTrue(EntrepriseReferentiel.objects.filter(email="rh1@example.ch").exists())

    def test_dry_run_n_ecrit_rien(self):
        absente = self._ref(1)
        self._sync({"62": ([self._ent(2)], True)}, "--dry_run")
        absente.refresh_from_db()
        self.assertEqual((absente.radiee, absente.absente_depuis), (False, None))
        self.assertEqual(EntrepriseReferentiel.objects.count(), 1)

    def test_adresse_partagee_garde_l_entreprise_deja_enregistree(self):
        partagee = {"email": "fiduciaire@example.ch"}
        ref = self._ref(1, raison_sociale="Beta SA", **partagee)
        registre = {"62": ([
            self._ent(1, nom="Alpha SA", **partagee),
            self._ent(1, nom="Beta SA", adresse="Quai Neuf 3", **partagee),
        ], True)}
        self._sync(registre)
        ref.refresh_from_db()
        # Même entreprise, nouvelle adresse postale : la ligne suit Beta SA, pas Alpha SA.
        self.assertEqual((ref.raison_sociale, ref.adresse), ("Beta SA", "Quai Neuf 3"))
        self.assertIn("0 à mettre à jour", self._sync(registre))
        # Beta SA quitte le registre : la ligne prend une entreprise qui y figure encore.
        self._sync({"62": ([self._ent(1, nom="Gamma SA", **partagee), self._ent(1, nom="Alpha SA", **partagee)], True)})
        ref.refresh_from_db()
        self.assertEqual((ref.raison_sociale, ref.radiee), ("Alpha SA", False))

    def test_lecture_partielle_ne_remplace_pas_l_entreprise_d_une_adresse_partagee(self):
        partagee = {"email": "fiduciaire@example.ch"}
        ref = self._ref(1, raison_sociale="Beta SA", code_noga="70", **partagee)
        self._sync({"62": ([self._ent(1, nom="Alpha SA", **partagee)], True)}, "--secteurs", "62")
        ref.refresh_from_db()
        self.assertEqual((ref.raison_sociale, ref.code_noga), ("Beta SA", "70"))

    def test_adresse_partagee_nouvelle_donne_une_seule_ligne_stable(self):
        partagee = {"email": "fiduciaire@example.ch"}
        self._sync({
            "62": ([self._ent(1, nom="Zeta SA", **partagee)], True),
            "64": ([self._ent(2, nom="Alpha SA", noga_code="64", **partagee)], True),
        })
        ref = EntrepriseReferentiel.objects.get()
        self.assertEqual((ref.raison_sociale, ref.code_noga), ("Alpha SA", "64"))

    def test_email_en_majuscules_en_base_est_reconnu(self):
        ref = self._ref(1, email="RH1@Example.ch")
        self._sync({"62": ([self._ent(1), self._ent(2)], True)})
        ref.refresh_from_db()
        self.assertEqual((ref.radiee, ref.absente_depuis), (False, None))
        self.assertEqual(EntrepriseReferentiel.objects.count(), 2)

    def test_trop_d_adresses_non_verifiees_fait_echouer_la_commande(self):
        from django.core.management.base import CommandError
        from core.management.commands import sync_registre as sr
        registre = {"62": ([self._ent(i) for i in range(20)], True)}
        with patch.object(sr, "_validate_emails", side_effect=lambda candidats, **kw: ([], [], candidats)):
            with self.assertRaisesMessage(CommandError, "20 adresses sur 20 non vérifiées"):
                self._sync(registre, verifier=True)
        self.assertFalse(EntrepriseReferentiel.objects.exists())


class PurgeBouncesTests(BaseAuthTestCase):
    def test_marque_l_adresse_sans_rien_supprimer(self):
        import os
        import tempfile
        from io import StringIO
        from django.core.management import call_command
        ref = self._make_ref(email="mort@example.ch")
        vivant = self._make_ref(email="vivant@example.ch", nom="Vivant SA")
        boite_pleine = self._make_ref(email="pleine@example.ch", nom="Pleine SA")
        cand = self._make_candidature(ref=ref)
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8", newline="") as f:
            f.write("email,statut,source\nmort@example.ch,invalide,ref\n"
                    "vivant@example.ch,incertain,ref\npleine@example.ch,boite_pleine,ref\n")
        self.addCleanup(os.remove, f.name)
        call_command("purge_bounces", "--csv", f.name, stdout=StringIO())
        ref.refresh_from_db()
        vivant.refresh_from_db()
        self.assertFalse(ref.email_valide)
        self.assertTrue(vivant.email_valide)
        self.assertTrue(Candidature.objects.filter(pk=cand.pk).exists())
        # Le marquage est définitif : une erreur temporaire n'écarte l'adresse que sur demande.
        boite_pleine.refresh_from_db()
        self.assertTrue(boite_pleine.email_valide)
        call_command("purge_bounces", "--csv", f.name, "--statuts", "boite_pleine", stdout=StringIO())
        boite_pleine.refresh_from_db()
        self.assertFalse(boite_pleine.email_valide)


class ScanGroupeTests(BaseAuthTestCase):
    def test_scan_en_lots_numerote_les_packs_et_ignore_les_doublons(self):
        from core.views.scan import _run_scan_for_user
        EntrepriseReferentiel.objects.bulk_create([
            EntrepriseReferentiel(raison_sociale=f"S{i:04d}", email=f"s{i}@example.ch", code_noga="62")
            for i in range(501)
        ])
        _run_scan_for_user(self.user, ["62"])
        _run_scan_for_user(self.user, ["62"])
        cands = Candidature.objects.filter(utilisateur=self.user)
        self.assertEqual(cands.count(), 501)
        self.assertEqual(cands.filter(numero_pack=1).count(), 500)
        # Le pack suit l'ordre alphabétique : la dernière raison sociale ouvre le pack 2.
        self.assertEqual(cands.get(numero_pack=2).entreprise.raison_sociale, "S0500")

    def test_entreprise_radiee_ni_scannee_ni_contactee(self):
        from core.views.gmail import _filtrer_candidatures
        from core.views.scan import _run_scan_for_user
        active = self._make_ref(email="active@example.ch", nom="Active SA")
        radiee = self._make_ref(email="radiee@example.ch", nom="Radiee SA")
        deja_listee = self._make_ref(email="listee@example.ch", nom="Listee SA")
        self._make_candidature(ref=deja_listee)
        EntrepriseReferentiel.objects.filter(pk__in=[radiee.pk, deja_listee.pk]).update(radiee=True)
        _run_scan_for_user(self.user, ["62"])
        cands = Candidature.objects.filter(utilisateur=self.user)
        self.assertEqual(set(cands.values_list("entreprise_id", flat=True)), {active.pk, deja_listee.pk})
        self.assertEqual(
            list(_filtrer_candidatures(cands, "", None).values_list("entreprise_id", flat=True)), [active.pk],
        )


# ---------------------------------------------------------------------------
# Front (audit, lot 4)
# ---------------------------------------------------------------------------

class FrontLot4Tests(BaseAuthTestCase):
    _setup_complete_profil = DashboardTests._setup_complete_profil

    def test_pages_heritent_du_gabarit_de_base(self):
        self._setup_complete_profil()
        for nom in ("dashboard", "historique_scans", "settings_page", "add_secteurs"):
            resp = self.client.get(reverse(nom))
            self.assertEqual(resp.status_code, 200, nom)
            contenu = resp.content.decode()
            self.assertTrue(contenu.startswith("<!DOCTYPE html>"), nom)
            self.assertIn("family=Inter", contenu, nom)
            self.assertIn("css/mx-base.css", contenu, nom)
            self.assertEqual(contenu.count("bootstrap.bundle.min.js"), 1, nom)
            self.assertEqual(contenu.count("</html>"), 1, nom)

    def test_bouton_d_aide_present_une_seule_fois_ou_absent(self):
        self._setup_complete_profil()
        self.assertEqual(self.client.get(reverse("dashboard")).content.decode().count('id="videoAideModal"'), 1)
        self.assertNotContains(self.client.get(reverse("add_secteurs")), "videoAideModal")
        self.client.logout()
        for nom in ("signin", "register"):
            contenu = self.client.get(reverse(nom)).content.decode()
            self.assertNotIn("videoAideModal", contenu, nom)
            self.assertIn("css/login.css", contenu, nom)
            self.assertTrue(contenu.startswith("<!DOCTYPE html>"), nom)
            self.assertEqual(contenu.count("bootstrap.bundle.min.js"), 1, nom)

    def test_detail_d_un_scan_herite_du_gabarit_et_signale_les_radiees(self):
        self._setup_complete_profil()
        radiee = self._make_ref(email="radiee@example.ch", nom="Radiee SA")
        cand = self._make_candidature(ref=radiee)
        self._make_candidature()
        EntrepriseReferentiel.objects.filter(pk=radiee.pk).update(radiee=True)
        contenu = self.client.get(reverse("detail_scan", args=[cand.id])).content.decode()
        self.assertTrue(contenu.startswith("<!DOCTYPE html>"))
        self.assertEqual((contenu.count('id="videoAideModal"'), contenu.count("bootstrap.bundle.min.js")), (1, 1))
        self.assertEqual((contenu.count("Radiée"), contenu.count("En attente")), (1, 1))

    def test_tableau_de_bord_charge_son_script_et_plus_l_ancien(self):
        self._setup_complete_profil()
        resp = self.client.get(reverse("dashboard"))
        self.assertContains(resp, "js/mx-dashboard.js")
        self.assertContains(resp, 'data-filter-url="/entreprises/filtrer-secteur"')
        self.assertNotContains(resp, "scan-history")
        self.assertNotContains(resp, "secteurSelect")

    def test_liste_signale_les_entreprises_qu_on_ne_peut_plus_contacter(self):
        self._setup_complete_profil()
        radiee = self._make_ref(email="radiee@example.ch", nom="Radiee SA")
        invalide = self._make_ref(email="invalide@example.ch", nom="Invalide SA")
        for ref in (radiee, invalide, self._make_ref()):
            self._make_candidature(ref=ref)
        EntrepriseReferentiel.objects.filter(pk=radiee.pk).update(radiee=True)
        EntrepriseReferentiel.objects.filter(pk=invalide.pk).update(email_valide=False)
        resp = self.client.get(
            reverse("entreprises_filtrer_secteur"), {"secteur": "Informatique et programmation"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        lignes = resp.json()["tbody"]
        self.assertEqual((lignes.count("Radiée"), lignes.count("E-mail invalide"), lignes.count("En attente")), (1, 1, 1))

    def test_fichiers_statiques_references_existent(self):
        import re
        from django.contrib.staticfiles import finders
        from django.template.loader import get_template
        gabarits = ["base.html", "core/dashboard.html", "core/historique.html", "core/detail_scan.html",
                    "core/settings.html", "core/onboarding.html", "core/add_secteurs.html",
                    "registration/signin.html", "registration/register.html", "registration/login.html"]
        for nom in gabarits:
            source = open(get_template(nom).origin.name, encoding="utf-8").read()
            for chemin in re.findall(r"{% static '([^']+)' %}", source):
                self.assertIsNotNone(finders.find(chemin), f"{nom} : {chemin}")


# ---------------------------------------------------------------------------
# Accessibilité (audit, lot 5)
# ---------------------------------------------------------------------------

class AccessibiliteLot5Tests(BaseAuthTestCase):
    _setup_complete_profil = DashboardTests._setup_complete_profil

    def _page(self, nom, *args, **params):
        return self.client.get(reverse(nom, args=args), params).content.decode()

    def test_structure_commune_des_pages(self):
        import re
        self._setup_complete_profil()
        cand = self._make_candidature()
        pages = {
            "dashboard": self._page("dashboard"),
            "historique_scans": self._page("historique_scans"),
            "detail_scan": self._page("detail_scan", cand.id),
            "settings_page": self._page("settings_page"),
            "add_secteurs": self._page("add_secteurs"),
        }
        self.client.logout()
        pages["signin"] = self._page("signin")
        pages["register"] = self._page("register")
        for nom, html in pages.items():
            self.assertEqual(len(re.findall(r"<h1[ >]", html)), 1, nom)
            self.assertEqual(html.count('<main id="contenu"'), 1, nom)
            self.assertIn('href="#contenu"', html, nom)
            # Aucune icône de police n'est lue par un lecteur d'écran.
            self.assertEqual(re.findall(r'<i class="bi [^>]*>', re.sub(r'<i class="bi [^>]*aria-hidden="true"[^>]*>', "", html)), [], nom)
            self.assertNotRegex(html, r'<(span|div|tr)[^>]*\sonclick=', nom)
            self.assertNotRegex(html, r'<th(?![^>]*scope=)[ >]', nom)
            self.assertNotRegex(html, r'class="btn-close[^"]*"(?![^>]*aria-label)[^>]*>', nom)

    def test_champs_des_reglages_relies_a_leur_libelle(self):
        import re
        self._setup_complete_profil()
        html = self._page("settings_page")
        champs = set(re.findall(r'<(?:input|textarea)[^>]*\sid="((?:id_|f_)\w+)"', html))
        relies = set(re.findall(r'<label for="(\w+)"', html))
        self.assertEqual(len(champs), 14)
        self.assertEqual(champs - relies, set())
        self.assertIn('autocomplete="given-name"', html)
        for onglet in ("identite", "templates", "connexions"):
            self.assertIn(f'id="onglet-{onglet}" aria-controls="tab-{onglet}"', html)
            self.assertIn(f'id="tab-{onglet}" role="tabpanel" aria-labelledby="onglet-{onglet}"', html)
        self.assertEqual(html.count('aria-selected="true"'), 1)
        self.assertEqual(len(re.findall(r'<button type="button" class="var-badge"', html)), 6)

    def test_tableau_de_bord_nomme_ses_champs_et_ses_boutons(self):
        self._setup_complete_profil()
        doc = DocumentUtilisateur(utilisateur=self.user, type_doc="CV", nom_affichage="Mon CV 2026")
        doc.fichier.save("cv.pdf", ContentFile(b"%PDF-1.4"), save=True)
        self.addCleanup(doc.fichier.delete, save=False)
        html = self._page("dashboard")
        for attendu in ('<label for="id_nom_doc"', '<label for="id_cv_file"', 'aria-label="Supprimer le document Mon CV 2026"',
                        'aria-label="Voir Mon CV 2026 (nouvel onglet)"', 'aria-label="Se déconnecter"'):
            self.assertIn(attendu, html)

    def test_inscription_affiche_les_criteres_avant_l_envoi(self):
        self.client.logout()
        html = self._page("register")
        self.assertIn('id="id_password1_helptext"', html)
        self.assertIn('aria-describedby="id_password1_helptext"', html)

    def test_erreur_de_connexion_annoncee(self):
        self.client.logout()
        resp = self.client.post(reverse("signin"), {"username": "u", "password": "faux"})
        self.assertContains(resp, '<div role="alert">')
        # L'erreur n'apparaît qu'une fois (elle était répétée hors de l'alerte).
        self.assertEqual(resp.content.decode().count('class="errorlist'), 1)

    def test_un_seul_onglet_atteignable_meme_avec_un_onglet_inconnu(self):
        self._setup_complete_profil()
        for tab in ("identite", "templates", "connexions", "inconnu"):
            html = self._page("settings_page", tab=tab)
            self.assertEqual((html.count('tabindex="0"'), html.count('aria-selected="true"')), (1, 1), tab)

    def test_navigation_hors_de_la_zone_principale(self):
        self._setup_complete_profil()
        for nom in ("dashboard", "historique_scans", "settings_page"):
            html = self._page(nom)
            self.assertLess(html.index("</nav>"), html.index('<main id="contenu"'), nom)

    def test_erreur_de_champ_a_l_inscription_annoncee(self):
        self.client.logout()
        resp = self.client.post(reverse("register"), {"username": "nouveau", "password1": "a", "password2": "b"})
        self.assertContains(resp, '<div role="alert">')
        self.assertContains(resp, 'id="id_password2_helptext"')

    def test_page_d_accueil_publique(self):
        import re
        self.client.logout()
        html = self._page("login")
        self.assertEqual(re.findall(r'<i class="bi [^>]*>', re.sub(r'<i class="bi [^>]*aria-hidden="true"[^>]*>', "", html)), [])
        self.assertNotRegex(html, r'class="btn-close[^"]*"(?![^>]*aria-label)[^>]*>')
        self.assertIn('aria-labelledby="videoAideTitre"', html)

    def test_groupes_de_secteurs_intitules(self):
        self._setup_complete_profil()
        html = self._page("add_secteurs")
        self.assertGreaterEqual(html.count("<fieldset>"), 2)
        self.assertEqual(html.count("<fieldset>"), html.count('<legend class="group-title">'))
        self.assertIn('<button type="button" class="select-all-link" id="selectAll">', html)
