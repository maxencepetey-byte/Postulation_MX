from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0033_drop_scansession_entreprisecible'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='profilutilisateur',
            name='phrase_banque',
        ),
        migrations.RemoveField(
            model_name='profilutilisateur',
            name='phrase_generale',
        ),
        migrations.RemoveField(
            model_name='profilutilisateur',
            name='phrase_informatique',
        ),
        migrations.RemoveField(
            model_name='profilutilisateur',
            name='phrase_luxe',
        ),
    ]
