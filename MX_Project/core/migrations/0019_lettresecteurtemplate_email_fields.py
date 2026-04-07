from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0018_lettresecteurtemplate_objet"),
    ]

    operations = [
        migrations.AddField(
            model_name="lettresecteurtemplate",
            name="email_subject",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AddField(
            model_name="lettresecteurtemplate",
            name="email_body",
            field=models.TextField(blank=True, default=""),
        ),
    ]

