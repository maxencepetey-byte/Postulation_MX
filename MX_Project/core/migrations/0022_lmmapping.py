from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0021_gmailoauthtoken"),
    ]

    operations = [
        migrations.CreateModel(
            name="LMMapping",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("email_entreprise", models.EmailField(max_length=254)),
                ("nom_fichier_dans_zip", models.CharField(max_length=255)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "pack_doc",
                    models.ForeignKey(
                        limit_choices_to={"type_doc": "PACK_LM"},
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="lm_mappings",
                        to="core.documentutilisateur",
                    ),
                ),
                (
                    "utilisateur",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="lm_mappings",
                        to="auth.user",
                    ),
                ),
            ],
            options={
                "unique_together": {("pack_doc", "email_entreprise")},
            },
        ),
    ]

