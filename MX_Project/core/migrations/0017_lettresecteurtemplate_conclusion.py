from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0016_lettresecteurtemplate"),
    ]

    operations = [
        migrations.AddField(
            model_name="lettresecteurtemplate",
            name="conclusion",
            field=models.TextField(blank=True, default=""),
        ),
    ]

