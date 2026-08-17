"""Modèles ORM : profils, entreprises, candidatures, documents et tokens Gmail chiffrés."""

import base64
import hashlib

from django.conf import settings
from django.db import models
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from cryptography.fernet import Fernet, InvalidToken


import magic


# ---------------------------------------------------------------------------
# Chiffrement symétrique des tokens OAuth (clé dérivée du SECRET_KEY Django)
# ---------------------------------------------------------------------------
def _fernet() -> Fernet:
    key = base64.urlsafe_b64encode(hashlib.sha256(settings.SECRET_KEY.encode()).digest())
    return Fernet(key)


def _encrypt_token(value: str) -> str:
    if not value:
        return value
    if value.startswith("gAAAAA"):  # déjà chiffré Fernet — évite le double chiffrement
        return value
    return _fernet().encrypt(value.encode()).decode()


def _decrypt_token(value: str) -> str:
    if not value:
        return value
    try:
        return _fernet().decrypt(value.encode()).decode()
    except (InvalidToken, Exception):
        return value  # valeur en clair (pré-migration) — retournée telle quelle

MIME_TYPES_AUTORISES = ['application/pdf']


def validate_file(value):
    if value.size > 2 * 1024 * 1024:
        raise ValidationError("Le fichier est trop lourd (max 2 Mo).")
    value.seek(0)
    mime = magic.from_buffer(value.read(2048), mime=True)
    value.seek(0)
    if mime not in MIME_TYPES_AUTORISES:
        raise ValidationError(
            f"Type de fichier non autorisé ({mime}). Seuls les PDF sont acceptés."
        )


class ProfilUtilisateur(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    prenom_lm = models.CharField(max_length=100, blank=True, null=True)
    nom_lm = models.CharField(max_length=100, blank=True, null=True)
    email_lm = models.EmailField(max_length=255, blank=True, null=True)
    telephone = models.CharField(max_length=20, blank=True, null=True)
    rue = models.CharField(max_length=255, blank=True, null=True)
    npa = models.CharField(max_length=10, blank=True, null=True)
    ville = models.CharField(max_length=100, blank=True, null=True)
    onboarding_done = models.BooleanField(default=False)
    onboarding_secteurs = models.CharField(max_length=255, blank=True, default="")

    def __str__(self):
        return f"Profil de {self.user.username}"


class DocumentUtilisateur(models.Model):
    TYPES_DOC = [
        ('CV', 'CV'),
        ('CERTIFICAT', 'Certificat'),
        ('DIPLOME', 'Diplôme'),
        ('GUIDE', 'Guide'),
        ('PACK_LM', 'Pack Lettres de motivation'),
        ('AUTRE', 'Autre'),
    ]
    utilisateur = models.ForeignKey(User, on_delete=models.CASCADE)
    secteur_nom = models.CharField(max_length=100, blank=True)
    nom_affichage = models.CharField(max_length=100, default="Mon CV")
    type_doc = models.CharField(max_length=10, choices=TYPES_DOC, default='CV')
    date_upload = models.DateTimeField(auto_now_add=True)
    fichier = models.FileField(upload_to='cv_storage/', validators=[validate_file])
    used_for_gmail = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.nom_affichage} ({self.utilisateur.username})"



class LettreSecteurTemplate(models.Model):
    """
    Template de lettre (salutation + 4 paragraphes) par secteur.
    """
    utilisateur = models.ForeignKey(User, on_delete=models.CASCADE, related_name="lettres_templates")
    secteur_nom = models.CharField(max_length=100)
    objet = models.CharField(max_length=255, blank=True, default="")
    salutation = models.CharField(max_length=255, blank=True, default="")
    paragraph_1 = models.TextField(blank=True, default="")
    paragraph_2 = models.TextField(blank=True, default="")
    paragraph_3 = models.TextField(blank=True, default="")
    paragraph_4 = models.TextField(blank=True, default="")
    conclusion = models.TextField(blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("utilisateur", "secteur_nom")]

    def __str__(self):
        return f"Template {self.secteur_nom} — {self.utilisateur.username}"


class GmailOAuthToken(models.Model):
    """
    Tokens OAuth Gmail par utilisateur (refresh token long-terme).
    Les champs refresh_token et access_token sont chiffrés au repos (Fernet/AES-128).
    En mémoire Python ils circulent toujours en clair.
    """
    utilisateur = models.OneToOneField(User, on_delete=models.CASCADE, related_name="gmail_oauth")
    refresh_token = models.TextField()
    access_token = models.TextField(blank=True, default="")
    expires_at = models.DateTimeField(null=True, blank=True)
    scope = models.TextField(blank=True, default="")
    token_type = models.CharField(max_length=40, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    @classmethod
    def from_db(cls, db, field_names, values):
        """Déchiffre les tokens automatiquement à la lecture depuis la DB."""
        instance = super().from_db(db, field_names, values)
        instance.refresh_token = _decrypt_token(instance.refresh_token)
        instance.access_token = _decrypt_token(instance.access_token)
        return instance

    def save(self, *args, **kwargs):
        """Chiffre les tokens avant persistance, restaure les valeurs en clair après."""
        plain_rt = self.refresh_token
        plain_at = self.access_token
        self.refresh_token = _encrypt_token(plain_rt) if plain_rt else plain_rt
        self.access_token = _encrypt_token(plain_at) if plain_at else plain_at
        try:
            super().save(*args, **kwargs)
        finally:
            self.refresh_token = plain_rt
            self.access_token = plain_at

    def __str__(self):
        return f"Gmail OAuth — {self.utilisateur.username}"


class EntrepriseReferentiel(models.Model):
    """
    Référentiel global (cache local) du registre SITG.
    Une entreprise n'existe qu'une seule fois ici, indépendamment des utilisateurs.
    """
    id_sitg = models.BigIntegerField(null=True, blank=True, unique=True)
    raison_sociale = models.CharField(max_length=255)
    email = models.EmailField(unique=True)
    code_noga = models.CharField(max_length=20, blank=True, default="")
    adresse = models.TextField(blank=True, default="")
    date_update = models.DateTimeField(auto_now=True)
    email_valide = models.BooleanField(default=True)

    class Meta:
        indexes = [
            models.Index(fields=["code_noga"]),
            models.Index(fields=["email"]),
        ]

    def __str__(self):
        return f"{self.raison_sociale} <{self.email}>"


class Candidature(models.Model):
    """
    Association User ↔ EntrepriseReferentiel : une candidature spontanée.
    Remplace ScanSession + EntrepriseCible : chaque ligne = une entreprise ciblée
    par un utilisateur, avec le contexte du scan qui l'a générée.
    """
    utilisateur = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name='candidatures'
    )
    entreprise = models.ForeignKey(
        EntrepriseReferentiel, on_delete=models.PROTECT, related_name='candidatures'
    )
    date_scan = models.DateTimeField(auto_now_add=True)
    secteurs = models.CharField(max_length=255, default="")   # secteurs du scan source
    statut = models.CharField(max_length=50, default="À traiter")
    est_dans_paquet = models.BooleanField(default=False)
    numero_pack = models.IntegerField(default=0)
    date_traitement = models.DateTimeField(null=True, blank=True)
    email_valide = models.BooleanField(default=True)
    brouillon_gmail_cree = models.BooleanField(default=False)
    secteur_activite = models.CharField(max_length=100, null=True, blank=True)

    class Meta:
        unique_together = [('utilisateur', 'entreprise')]
        indexes = [
            models.Index(fields=['secteur_activite']),
            models.Index(fields=['est_dans_paquet']),
            models.Index(fields=['numero_pack']),
            models.Index(fields=['date_scan']),
        ]

    def __str__(self):
        return f"{self.entreprise} — {self.utilisateur}"
