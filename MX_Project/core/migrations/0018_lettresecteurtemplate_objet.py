from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0017_lettresecteurtemplate_conclusion"),
    ]

    operations = [
        migrations.AddField(
            model_name="lettresecteurtemplate",
            name="objet",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
    ]

