"""
Supprime les tables ScanSession et EntrepriseCible désormais remplacées par Candidature.
Cette migration est irréversible : les données ont été migrées en 0032.
"""

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0032_migrate_candidatures'),
    ]

    operations = [
        migrations.DeleteModel(name='EntrepriseCible'),
        migrations.DeleteModel(name='ScanSession'),
    ]
