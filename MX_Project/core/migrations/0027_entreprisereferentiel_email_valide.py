from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0026_entreprisecible_brouillon_gmail_cree'),
    ]

    operations = [
        migrations.AddField(
            model_name='entreprisereferentiel',
            name='email_valide',
            field=models.BooleanField(default=True),
        ),
    ]
