from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("auth", "0012_alter_user_first_name_max_length"),
        ("core", "0020_remove_lettresecteurtemplate_email_fields"),
    ]

    operations = [
        migrations.CreateModel(
            name="GmailOAuthToken",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("refresh_token", models.TextField()),
                ("access_token", models.TextField(blank=True, default="")),
                ("expires_at", models.DateTimeField(blank=True, null=True)),
                ("scope", models.TextField(blank=True, default="")),
                ("token_type", models.CharField(blank=True, default="", max_length=40)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "utilisateur",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="gmail_oauth",
                        to="auth.user",
                    ),
                ),
            ],
        ),
    ]

