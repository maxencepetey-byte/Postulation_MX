from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0034_remove_profilutilisateur_phrases'),
    ]

    operations = [
        migrations.RenameField(
            model_name='profilutilisateur',
            old_name='user',
            new_name='utilisateur',
        ),
    ]
