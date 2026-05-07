import base64
import hashlib

from django.db import migrations


def encrypt_existing_tokens(apps, schema_editor):
    try:
        from cryptography.fernet import Fernet
        from django.conf import settings

        key = base64.urlsafe_b64encode(hashlib.sha256(settings.SECRET_KEY.encode()).digest())
        f = Fernet(key)

        GmailOAuthToken = apps.get_model("core", "GmailOAuthToken")
        for tok in GmailOAuthToken.objects.all():
            update = {}
            # Fernet tokens always start with "gAAAAA" — skip already-encrypted values
            if tok.refresh_token and not tok.refresh_token.startswith("gAAAAA"):
                update["refresh_token"] = f.encrypt(tok.refresh_token.encode()).decode()
            if tok.access_token and not tok.access_token.startswith("gAAAAA"):
                update["access_token"] = f.encrypt(tok.access_token.encode()).decode()
            if update:
                GmailOAuthToken.objects.filter(pk=tok.pk).update(**update)
    except Exception:
        pass  # Si cryptography absent ou SECRET_KEY indisponible, skip silencieusement


def noop_reverse(apps, schema_editor):
    pass  # Rollback sans déchiffrement (tokens restent chiffrés)


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0028_remove_unused_fields"),
    ]

    operations = [
        migrations.RunPython(encrypt_existing_tokens, noop_reverse),
    ]
