from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0030_remove_recherche_add_indexes'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='Candidature',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('date_scan', models.DateTimeField(auto_now_add=True)),
                ('secteurs', models.CharField(default='', max_length=255)),
                ('statut', models.CharField(default='À traiter', max_length=50)),
                ('est_dans_paquet', models.BooleanField(default=False)),
                ('numero_pack', models.IntegerField(default=0)),
                ('date_traitement', models.DateTimeField(blank=True, null=True)),
                ('email_valide', models.BooleanField(default=True)),
                ('brouillon_gmail_cree', models.BooleanField(default=False)),
                ('secteur_activite', models.CharField(blank=True, max_length=100, null=True)),
                ('utilisateur', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='candidatures',
                    to=settings.AUTH_USER_MODEL,
                )),
                ('entreprise', models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name='candidatures',
                    to='core.entreprisereferentiel',
                )),
            ],
            options={
                'indexes': [
                    models.Index(fields=['secteur_activite'], name='core_candid_sectact_idx'),
                    models.Index(fields=['est_dans_paquet'], name='core_candid_paquet_idx'),
                    models.Index(fields=['numero_pack'], name='core_candid_pack_idx'),
                    models.Index(fields=['date_scan'], name='core_candid_date_idx'),
                ],
                'unique_together': {('utilisateur', 'entreprise')},
            },
        ),
    ]
