from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0013_entreprisecible_utilisateur_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="profilutilisateur",
            name="onboarding_done",
            field=models.BooleanField(default=False),
        ),
    ]

