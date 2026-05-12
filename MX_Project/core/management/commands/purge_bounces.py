"""
Commande : python manage.py purge_bounces

Supprime de la base de données les entreprises dont l'email est classé
Hard bounce (invalide / domaine_ko / pas_de_mx) ou À risque (boite_pleine / erreur_temp)
selon le fichier CSV produit par check_emails.

Options :
  --csv <chemin>    Fichier CSV source  (défaut : emails_invalides.csv)
  --statuts <list>  Statuts à purger    (défaut : invalide domaine_ko pas_de_mx boite_pleine erreur_temp)
  --dry-run         Simule sans supprimer
  --source <src>    referentiel | cibles | all  (défaut : all)
"""

import csv
import os

from django.core.management.base import BaseCommand

from core.models import Candidature, EntrepriseReferentiel

STATUTS_HARD_BOUNCE = {"invalide", "domaine_ko", "pas_de_mx", "compte_desactive", "syntaxe_invalide"}
STATUTS_A_RISQUE    = {"boite_pleine", "erreur_temp"}
STATUTS_PAR_DEFAUT  = STATUTS_HARD_BOUNCE | STATUTS_A_RISQUE
# ip_bloquee et incertain sont exclus par défaut : l'email peut être valide


class Command(BaseCommand):
    help = "Supprime de la BDD les entreprises Hard bounce / À risque listées dans le CSV."

    def add_arguments(self, parser):
        parser.add_argument(
            "--csv", default="emails_invalides.csv",
            help="Chemin du CSV source (défaut : emails_invalides.csv)",
        )
        parser.add_argument(
            "--statuts", nargs="+", default=list(STATUTS_PAR_DEFAUT),
            metavar="STATUT",
            help="Statuts à purger (défaut : tous hard bounces + à risque)",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Affiche ce qui serait supprimé sans toucher à la base",
        )
        parser.add_argument(
            "--source", default="all", choices=["all", "referentiel", "cibles"],
            help="Source à purger : all (défaut) | referentiel | cibles",
        )

    def handle(self, *args, **options):
        csv_path  = options["csv"]
        statuts   = set(options["statuts"])
        dry_run   = options["dry_run"]
        source    = options["source"]

        if not os.path.exists(csv_path):
            self.stderr.write(self.style.ERROR(f"Fichier introuvable : {csv_path}"))
            return

        # ── Lecture du CSV ────────────────────────────────────────────────────
        emails_ref    = set()  # emails issus de EntrepriseReferentiel
        emails_cibles = set()  # emails issus de EntrepriseCible

        with open(csv_path, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                if row.get("statut") not in statuts:
                    continue
                email = row.get("email", "").strip().lower()
                if not email:
                    continue
                if row.get("source") == "ref":
                    emails_ref.add(email)
                else:
                    emails_cibles.add(email)

        self.stdout.write(f"\n{'='*60}")
        self.stdout.write(f"  CSV      : {csv_path}")
        self.stdout.write(f"  Statuts  : {', '.join(sorted(statuts))}")
        self.stdout.write(f"  Dry-run  : {'OUI' if dry_run else 'NON'}")
        self.stdout.write(f"  Source   : {source}")
        self.stdout.write(f"  Emails ref à purger    : {len(emails_ref)}")
        self.stdout.write(f"  Emails cibles à purger : {len(emails_cibles)}")
        self.stdout.write(f"{'='*60}\n")

        total_supprime = 0

        # ── Purge EntrepriseReferentiel ───────────────────────────────────────
        if source in ("all", "referentiel") and emails_ref:
            qs = EntrepriseReferentiel.objects.filter(
                email__in=emails_ref
            )
            count = qs.count()
            self.stdout.write(f"  EntrepriseReferentiel : {count} ligne(s) à supprimer")
            if not dry_run and count:
                deleted, _ = qs.delete()
                self.stdout.write(self.style.SUCCESS(f"  → {deleted} supprimée(s)"))
                total_supprime += deleted

        # ── Purge Candidature (source "cibles") ───────────────────────────────
        if source in ("all", "cibles") and emails_cibles:
            qs = Candidature.objects.filter(
                entreprise__email__in=emails_cibles
            )
            count = qs.count()
            self.stdout.write(f"  Candidature           : {count} ligne(s) à supprimer")
            if not dry_run and count:
                deleted, _ = qs.delete()
                self.stdout.write(self.style.SUCCESS(f"  → {deleted} supprimée(s)"))
                total_supprime += deleted

        if dry_run:
            self.stdout.write(self.style.WARNING("\n  Mode dry-run — aucune suppression effectuée."))
        else:
            self.stdout.write(self.style.SUCCESS(f"\n  ✓ Total supprimé : {total_supprime} entrée(s)"))

        self.stdout.write(f"{'='*60}\n")
