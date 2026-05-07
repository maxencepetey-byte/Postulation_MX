from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0029_encrypt_gmail_tokens'),
    ]

    operations = [
        # 1. Supprimer la FK recherche de EntrepriseCible
        migrations.RemoveField(
            model_name='entreprisecible',
            name='recherche',
        ),
        # 2. Supprimer la table Recherche devenue orpheline
        migrations.DeleteModel(
            name='Recherche',
        ),
        # 3. Ajouter les index de performance sur EntrepriseCible
        migrations.AddIndex(
            model_name='entreprisecible',
            index=models.Index(fields=['secteur_activite'], name='core_entrep_secteur_idx'),
        ),
        migrations.AddIndex(
            model_name='entreprisecible',
            index=models.Index(fields=['est_dans_paquet'], name='core_entrep_paquet_idx'),
        ),
        migrations.AddIndex(
            model_name='entreprisecible',
            index=models.Index(fields=['numero_pack'], name='core_entrep_pack_idx'),
        ),
    ]
