from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0014_profilutilisateur_onboarding_done"),
    ]

    operations = [
        migrations.AddField(
            model_name="profilutilisateur",
            name="onboarding_secteurs",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
    ]

