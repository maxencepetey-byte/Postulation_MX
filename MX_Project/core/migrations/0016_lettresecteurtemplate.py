from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("auth", "0012_alter_user_first_name_max_length"),
        ("core", "0015_profilutilisateur_onboarding_secteurs"),
    ]

    operations = [
        migrations.CreateModel(
            name="LettreSecteurTemplate",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("secteur_nom", models.CharField(max_length=100)),
                ("salutation", models.CharField(blank=True, default="", max_length=255)),
                ("paragraph_1", models.TextField(blank=True, default="")),
                ("paragraph_2", models.TextField(blank=True, default="")),
                ("paragraph_3", models.TextField(blank=True, default="")),
                ("paragraph_4", models.TextField(blank=True, default="")),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "utilisateur",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="lettres_templates",
                        to="auth.user",
                    ),
                ),
            ],
            options={
                "unique_together": {("utilisateur", "secteur_nom")},
            },
        ),
    ]

