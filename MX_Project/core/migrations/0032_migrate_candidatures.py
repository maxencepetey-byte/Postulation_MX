"""
Migration de données : EntrepriseCible → Candidature.

Pour chaque EntrepriseCible :
 1. Trouve ou crée l'EntrepriseReferentiel par email (source de vérité unique).
 2. Crée la Candidature en copiant tous les champs métier.
 3. Override date_scan via .update() pour préserver la date historique du scan.

Les doublons (même utilisateur + même entreprise) sont ignorés silencieusement
car unique_together = ('utilisateur', 'entreprise') est respecté.
"""

from django.db import migrations


def migrate_entrepises_vers_candidatures(apps, schema_editor):
    EntrepriseCible = apps.get_model('core', 'EntrepriseCible')
    EntrepriseReferentiel = apps.get_model('core', 'EntrepriseReferentiel')
    Candidature = apps.get_model('core', 'Candidature')

    seen_pairs = set()  # éviter les doublons en mémoire avant le hit DB

    qs = (
        EntrepriseCible.objects
        .select_related('scan_session', 'utilisateur')
        .order_by('id')
        .iterator(chunk_size=500)
    )

    to_create = []
    date_overrides = []  # [(index_in_to_create, date_scan_value)]

    for ec in qs:
        if not ec.utilisateur_id or not ec.email:
            continue

        # Trouve ou crée l'EntrepriseReferentiel par email
        ref, created = EntrepriseReferentiel.objects.get_or_create(
            email=ec.email,
            defaults={
                'raison_sociale': ec.nom or '',
                'adresse': ec.adresse or '',
                'email_valide': ec.email_valide,
            },
        )
        # Complète l'adresse si le référentiel l'avait vide
        if not created and not ref.adresse and ec.adresse:
            EntrepriseReferentiel.objects.filter(pk=ref.pk).update(adresse=ec.adresse)

        pair = (ec.utilisateur_id, ref.pk)
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)

        # Récupère le contexte de la ScanSession source
        secteurs_val = ec.scan_session.secteurs if ec.scan_session else ''
        date_scan_val = ec.scan_session.date_scan if ec.scan_session else None

        idx = len(to_create)
        to_create.append(Candidature(
            utilisateur_id=ec.utilisateur_id,
            entreprise=ref,
            secteurs=secteurs_val,
            statut=ec.statut,
            est_dans_paquet=ec.est_dans_paquet,
            numero_pack=ec.numero_pack,
            date_traitement=ec.date_traitement,
            email_valide=ec.email_valide,
            brouillon_gmail_cree=ec.brouillon_gmail_cree,
            secteur_activite=ec.secteur_activite,
        ))
        if date_scan_val:
            date_overrides.append((idx, date_scan_val))

        # Flush par batch de 500 pour ne pas tout charger en RAM
        if len(to_create) >= 500:
            _flush(Candidature, to_create, date_overrides)
            to_create.clear()
            date_overrides.clear()

    if to_create:
        _flush(Candidature, to_create, date_overrides)


def _flush(Candidature, to_create, date_overrides):
    """Insère un batch et corrige les dates auto_now_add via UPDATE."""
    # ignore_conflicts=True : si un doublon existe déjà (ex. 2e migration accidentelle)
    created_objs = Candidature.objects.bulk_create(to_create, ignore_conflicts=True)

    # Récupère les IDs réellement insérés pour corriger les dates
    # bulk_create avec ignore_conflicts ne retourne pas les PKs sur SQLite < 3.35,
    # donc on re-fetche par (utilisateur_id, entreprise_id).
    for idx, date_val in date_overrides:
        obj = to_create[idx]
        Candidature.objects.filter(
            utilisateur_id=obj.utilisateur_id,
            entreprise_id=obj.entreprise_id,
        ).update(date_scan=date_val)


def reverse_migration(apps, schema_editor):
    # Non réversible : on ne peut pas recréer les ScanSessions depuis les Candidatures
    Candidature = apps.get_model('core', 'Candidature')
    Candidature.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0031_create_candidature'),
    ]

    operations = [
        migrations.RunPython(
            migrate_entrepises_vers_candidatures,
            reverse_code=reverse_migration,
        ),
    ]
