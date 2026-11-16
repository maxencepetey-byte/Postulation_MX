"""
Commande : python manage.py purge_bounces

Marque comme non valides (email_valide=False) les entreprises dont l'email est classé
Hard bounce (invalide / domaine_ko / pas_de_mx…) selon le fichier CSV produit par check_emails.
Le marquage est définitif : les statuts temporaires (boite_pleine / erreur_temp) ne sont donc
traités que s'ils sont demandés avec --statuts.

Rien n'est supprimé : une ligne supprimée serait recréée par la synchro suivante du registre,
et les candidatures des utilisateurs disparaîtraient avec elle.

Options :
  --csv <chemin>    Fichier CSV source  (défaut : emails_invalides.csv)
  --statuts <list>  Statuts à écarter   (défaut : les hard bounces)
  --dry-run         Simule sans rien modifier
  --source <src>    referentiel | cibles | all  (défaut : all)
"""

import csv
import os

from django.core.management.base import BaseCommand

from core.models import EntrepriseReferentiel

STATUTS_HARD_BOUNCE = {"invalide", "domaine_ko", "pas_de_mx", "compte_desactive", "syntaxe_invalide"}
STATUTS_A_RISQUE    = {"boite_pleine", "erreur_temp"}
# À risque, ip_bloquee et incertain ne sont pas traités par défaut : l'email peut être valide.


class Command(BaseCommand):
    help = "Marque comme non valides les entreprises Hard bounce / À risque listées dans le CSV."

    def add_arguments(self, parser):
        parser.add_argument(
            "--csv", default="emails_invalides.csv",
            help="Chemin du CSV source (défaut : emails_invalides.csv)",
        )
        parser.add_argument(
            "--statuts", nargs="+", default=sorted(STATUTS_HARD_BOUNCE),
            metavar="STATUT",
            help="Statuts à écarter (défaut : les hard bounces ; ajouter boite_pleine erreur_temp au besoin)",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Affiche ce qui serait marqué sans toucher à la base",
        )
        parser.add_argument(
            "--source", default="all", choices=["all", "referentiel", "cibles"],
            help="Lignes du CSV à traiter : all (défaut) | referentiel | cibles",
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
        emails_ref    = set()  # lignes du CSV de source « ref »
        emails_cibles = set()  # les autres lignes du CSV

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
        self.stdout.write(f"  Emails « ref » du CSV    : {len(emails_ref)}")
        self.stdout.write(f"  Emails « cibles » du CSV : {len(emails_cibles)}")
        self.stdout.write(f"{'='*60}\n")

        emails = set()
        if source in ("all", "referentiel"):
            emails |= emails_ref
        if source in ("all", "cibles"):
            emails |= emails_cibles

        qs = EntrepriseReferentiel.objects.filter(email__in=emails, email_valide=True)
        count = qs.count()
        self.stdout.write(f"  EntrepriseReferentiel : {count} ligne(s) à marquer non valides")

        if dry_run:
            self.stdout.write(self.style.WARNING("\n  Mode dry-run — aucune modification effectuée."))
        else:
            marquees = qs.update(email_valide=False)
            self.stdout.write(self.style.SUCCESS(f"\n  ✓ {marquees} entreprise(s) marquée(s) non valides"))

        self.stdout.write(f"{'='*60}\n")
