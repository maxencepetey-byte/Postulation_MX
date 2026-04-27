"""
Commande : python manage.py check_emails

Teste TOUTES les adresses email en base :
  - EntrepriseReferentiel  (registre global — source des scans futurs)
  - EntrepriseCible         (entreprises ciblées par les utilisateurs)

Détecte :
  - Domaine inexistant
  - Domaine sans serveur MX
  - Boîte email inexistante (hard bounce SMTP 550/551/552/553)
  - Boîte pleine (SMTP 452)
  - Erreur temporaire / serveur indisponible (SMTP 421/450/451)

Options :
  --output <chemin>   Fichier CSV de sortie  (défaut : emails_invalides.csv)
  --workers <n>       Threads parallèles     (défaut : 20)
  --timeout <sec>     Timeout SMTP en sec    (défaut : 8)
  --update-db         Met à jour email_valide=False pour les hard bounces
  --source <src>      referentiel | cibles | all  (défaut : all)
  --user <username>   Restreindre EntrepriseCible à un utilisateur
"""

import csv
import smtplib
import socket
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from threading import Lock

import dns.resolver
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand

from core.models import EntrepriseCible, EntrepriseReferentiel

# ── Statuts ──────────────────────────────────────────────────────────────────
ST_VALIDE      = "valide"
ST_INVALIDE    = "invalide"       # hard bounce : adresse inexistante
ST_DOMAINE_KO  = "domaine_ko"     # NXDOMAIN : domaine inexistant
ST_MX_KO       = "pas_de_mx"      # domaine existe mais aucun MX
ST_PLEIN       = "boite_pleine"   # SMTP 452 / 552 : boîte pleine
ST_TEMP        = "erreur_temp"    # SMTP 421/450/451 : problème temporaire
ST_INCERTAIN   = "incertain"      # SMTP injoignable, port 25 bloqué
ST_DESACTIVE   = "compte_desactive"  # compte désactivé / suspendu (user_disabled)
ST_SYNTAXE     = "syntaxe_invalide"  # adresse mal formée (syntax_error / 5.1.3)
ST_IP_BLOQUEE  = "ip_bloquee"        # notre IP rejetée (Spamhaus…) — email potentiellement valide


import re as _re

# Patterns pour affiner la classification SMTP 5xx
_PAT_IP_BLOQUEE  = _re.compile(
    r'spamhaus|client host.*block|ip.*block|bad reputation|blacklist|'
    r'blocked using|unblock|pbl\b|zen\b.*spamhaus|mimecast|reputation',
    _re.I,
)
_PAT_DESACTIVE   = _re.compile(
    r'user.?disabl|account.?disabl|account.?inactiv|is inactive|'
    r'suspended|deactivated|user_disabled',
    _re.I,
)
_PAT_SYNTAXE     = _re.compile(
    r'syntax|5\.1\.3|not.*valid.*rfc|malformed|invalid.*address|'
    r'address.*invalid|undeliverable.*format',
    _re.I,
)
_PAT_OVER_QUOTA  = _re.compile(
    r'over.?quota|mailbox.*full|out of storage|storage.*full|'
    r'mailbox.*exceeded|quota.*exceeded',
    _re.I,
)


def _verifier_email(email: str, timeout: int) -> tuple[str, str]:
    """
    Vérifie une adresse email en 2 étapes : MX puis SMTP RCPT TO.
    Retourne (statut, raison).
    """
    if not email or "@" not in email:
        return ST_INVALIDE, "format invalide"

    domaine = email.split("@")[1]

    # ── Étape 1 : MX records ─────────────────────────────────────────────────
    try:
        mx_records = sorted(
            dns.resolver.resolve(domaine, "MX"),
            key=lambda r: r.preference,
        )
    except dns.resolver.NXDOMAIN:
        return ST_DOMAINE_KO, "domaine inexistant (NXDOMAIN)"
    except dns.resolver.NoAnswer:
        return ST_MX_KO, "aucun enregistrement MX"
    except dns.exception.Timeout:
        return ST_INCERTAIN, "timeout DNS"
    except Exception as e:
        return ST_INCERTAIN, f"erreur DNS: {e}"

    if not mx_records:
        return ST_MX_KO, "aucun enregistrement MX"

    # ── Étape 2 : SMTP RCPT TO sur les 2 premiers MX ────────────────────────
    last_err = ""
    for mx in mx_records[:2]:
        mx_host = str(mx.exchange).rstrip(".")
        try:
            with smtplib.SMTP(timeout=timeout) as smtp:
                smtp.connect(mx_host, 25)
                smtp.ehlo_or_helo_if_needed()
                smtp.mail("")
                code, raw = smtp.rcpt(email)
                detail = (raw.decode(errors="replace") if isinstance(raw, bytes) else str(raw))[:100]

                if code == 250:
                    return ST_VALIDE, "ok"
                elif code == 452 or (code in (550, 551, 552, 553, 554) and _PAT_OVER_QUOTA.search(detail)):
                    return ST_PLEIN, f"boîte pleine (SMTP {code} : {detail})"
                elif code in (550, 551, 552, 553, 554):
                    if _PAT_IP_BLOQUEE.search(detail):
                        return ST_IP_BLOQUEE, f"IP bloquée (SMTP {code} : {detail})"
                    if _PAT_DESACTIVE.search(detail):
                        return ST_DESACTIVE, f"compte désactivé (SMTP {code} : {detail})"
                    if _PAT_SYNTAXE.search(detail):
                        return ST_SYNTAXE, f"syntaxe invalide (SMTP {code} : {detail})"
                    return ST_INVALIDE, f"adresse inexistante (SMTP {code} : {detail})"
                elif code in (421, 450, 451):
                    return ST_TEMP, f"erreur temporaire (SMTP {code} : {detail})"
                else:
                    return ST_INCERTAIN, f"code SMTP {code} (non conclusif)"

        except (ConnectionRefusedError, socket.timeout,
                smtplib.SMTPConnectError, smtplib.SMTPServerDisconnected, OSError) as e:
            last_err = str(e)
            continue
        except Exception as e:
            last_err = str(e)
            continue

    # MX existe mais SMTP injoignable (port 25 souvent bloqué par les FAI/hébergeurs)
    return ST_INCERTAIN, f"SMTP injoignable ({last_err[:80]})" if last_err else "SMTP injoignable"


# Couleur console par statut
_STYLE_MAP = {
    ST_INVALIDE:   "ERROR",
    ST_DOMAINE_KO: "ERROR",
    ST_MX_KO:      "ERROR",
    ST_DESACTIVE:  "ERROR",
    ST_SYNTAXE:    "ERROR",
    ST_PLEIN:      "WARNING",
    ST_TEMP:       "WARNING",
    ST_IP_BLOQUEE: "NOTICE",
    ST_INCERTAIN:  "NOTICE",
    ST_VALIDE:     "SUCCESS",
}

_LABEL_MAP = {
    ST_INVALIDE:   "INVALIDE  ",
    ST_DOMAINE_KO: "DOMAINE ✗ ",
    ST_MX_KO:      "PAS DE MX ",
    ST_DESACTIVE:  "DÉSACTIVÉ ",
    ST_SYNTAXE:    "SYNTAXE ✗ ",
    ST_PLEIN:      "PLEIN     ",
    ST_TEMP:       "TEMP      ",
    ST_IP_BLOQUEE: "IP BLOQUÉE",
    ST_INCERTAIN:  "INCERTAIN ",
    ST_VALIDE:     "OK        ",
}

# Statuts considérés comme "problématiques" → écrits dans le CSV
STATUTS_PROBLEMATIQUES = {
    ST_INVALIDE, ST_DOMAINE_KO, ST_MX_KO,
    ST_DESACTIVE, ST_SYNTAXE,
    ST_PLEIN, ST_TEMP,
    ST_IP_BLOQUEE, ST_INCERTAIN,
}

# Statuts qui méritent email_valide=False en base (hard bounces confirmés)
STATUTS_HARD_KO = {ST_INVALIDE, ST_DOMAINE_KO, ST_MX_KO, ST_DESACTIVE, ST_SYNTAXE}


_BADGE_COLOR = {
    ST_INVALIDE:   "#dc3545",
    ST_DOMAINE_KO: "#dc3545",
    ST_MX_KO:      "#fd7e14",
    ST_DESACTIVE:  "#dc3545",
    ST_SYNTAXE:    "#dc3545",
    ST_PLEIN:      "#ffc107",
    ST_TEMP:       "#6f42c1",
    ST_IP_BLOQUEE: "#0dcaf0",
    ST_INCERTAIN:  "#6c757d",
    ST_VALIDE:     "#198754",
}

_BADGE_LABEL = {
    ST_INVALIDE:   "Invalide",
    ST_DOMAINE_KO: "Domaine ✗",
    ST_MX_KO:      "Pas de MX",
    ST_DESACTIVE:  "Désactivé",
    ST_SYNTAXE:    "Syntaxe ✗",
    ST_PLEIN:      "Boîte pleine",
    ST_TEMP:       "Erreur temp.",
    ST_IP_BLOQUEE: "IP bloquée",
    ST_INCERTAIN:  "Incertain",
    ST_VALIDE:     "Valide",
}


def _ecrire_html(path: str, results: list, compteurs: dict, total: int, dt) -> None:
    nb_hard   = (compteurs[ST_INVALIDE] + compteurs[ST_DOMAINE_KO] + compteurs[ST_MX_KO]
                 + compteurs[ST_DESACTIVE] + compteurs[ST_SYNTAXE])
    nb_risque = compteurs[ST_PLEIN] + compteurs[ST_TEMP]
    nb_ip     = compteurs[ST_IP_BLOQUEE]

    lignes = ""
    for r in results:
        couleur = _BADGE_COLOR.get(r["statut"], "#6c757d")
        label   = _BADGE_LABEL.get(r["statut"], r["statut"])
        raison  = r["raison"].replace("<", "&lt;").replace(">", "&gt;")
        lignes += f"""
        <tr data-statut="{r['statut']}">
          <td>{r['entreprise']}</td>
          <td><a href="mailto:{r['email']}">{r['email']}</a></td>
          <td>{r['secteur']}</td>
          <td>{r['utilisateur']}</td>
          <td><span class="badge" style="background:{couleur}">{label}</span></td>
          <td class="raison">{raison}</td>
        </tr>"""

    html = f"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<title>Rapport emails — {dt.strftime('%d/%m/%Y')}</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f4f6f9;color:#212529;padding:24px}}
  h1{{font-size:1.4rem;font-weight:700;margin-bottom:6px}}
  .sub{{color:#6c757d;font-size:.85rem;margin-bottom:20px}}
  .stats{{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:20px}}
  .stat-card{{background:#fff;border-radius:10px;padding:14px 20px;min-width:130px;box-shadow:0 1px 4px rgba(0,0,0,.08)}}
  .stat-card .val{{font-size:1.6rem;font-weight:800}}
  .stat-card .lbl{{font-size:.72rem;color:#6c757d;text-transform:uppercase;letter-spacing:.5px}}
  .filters{{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px;align-items:center}}
  .filters label{{font-size:.82rem;color:#6c757d;font-weight:600}}
  .btn-filter{{border:1.5px solid #dee2e6;background:#fff;border-radius:20px;padding:4px 14px;
               font-size:.8rem;cursor:pointer;transition:all .15s}}
  .btn-filter:hover,.btn-filter.active{{border-color:#0d6efd;background:#0d6efd;color:#fff}}
  input[type=search]{{border:1.5px solid #dee2e6;border-radius:8px;padding:6px 12px;
                      font-size:.85rem;outline:none;width:280px}}
  input[type=search]:focus{{border-color:#0d6efd}}
  table{{width:100%;border-collapse:collapse;background:#fff;border-radius:10px;
         overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,.08)}}
  thead tr{{background:#f8f9fa}}
  th{{padding:11px 14px;font-size:.72rem;text-transform:uppercase;letter-spacing:.5px;
      color:#6c757d;font-weight:700;text-align:left;cursor:pointer;user-select:none}}
  th:hover{{color:#0d6efd}}
  td{{padding:10px 14px;font-size:.83rem;border-top:1px solid #f0f0f0}}
  tr:hover td{{background:#f8fbff}}
  .badge{{display:inline-block;border-radius:20px;padding:3px 10px;font-size:.72rem;
           font-weight:700;color:#fff}}
  .raison{{color:#6c757d;font-size:.78rem;max-width:320px}}
  a{{color:#0d6efd;text-decoration:none}}
  a:hover{{text-decoration:underline}}
  .hidden{{display:none}}
  #counter{{font-size:.8rem;color:#6c757d;margin-bottom:8px}}
</style>
</head>
<body>
<h1>Rapport de validation email</h1>
<div class="sub">Généré le {dt.strftime('%d/%m/%Y à %H:%M')} — {total} adresses vérifiées</div>

<div class="stats">
  <div class="stat-card">
    <div class="val" style="color:#198754">{compteurs[ST_VALIDE]}</div>
    <div class="lbl">Valides</div>
  </div>
  <div class="stat-card">
    <div class="val" style="color:#dc3545">{nb_hard}</div>
    <div class="lbl">Hard bounces</div>
  </div>
  <div class="stat-card">
    <div class="val" style="color:#ffc107">{nb_risque}</div>
    <div class="lbl">À risque</div>
  </div>
  <div class="stat-card">
    <div class="val" style="color:#0dcaf0">{nb_ip}</div>
    <div class="lbl">IP bloquée</div>
  </div>
  <div class="stat-card">
    <div class="val" style="color:#6c757d">{compteurs[ST_INCERTAIN]}</div>
    <div class="lbl">Incertains</div>
  </div>
  <div class="stat-card">
    <div class="val">{total}</div>
    <div class="lbl">Total</div>
  </div>
</div>

<div class="filters">
  <label>Filtrer :</label>
  <button class="btn-filter active" data-filter="">Tous ({len(results)})</button>
  <button class="btn-filter" data-filter="{ST_INVALIDE}" style="border-color:#dc3545">
    Invalide ({compteurs[ST_INVALIDE]})
  </button>
  <button class="btn-filter" data-filter="{ST_DOMAINE_KO}" style="border-color:#dc3545">
    Domaine ✗ ({compteurs[ST_DOMAINE_KO]})
  </button>
  <button class="btn-filter" data-filter="{ST_MX_KO}" style="border-color:#fd7e14">
    Pas de MX ({compteurs[ST_MX_KO]})
  </button>
  <button class="btn-filter" data-filter="{ST_DESACTIVE}" style="border-color:#dc3545">
    Désactivé ({compteurs[ST_DESACTIVE]})
  </button>
  <button class="btn-filter" data-filter="{ST_SYNTAXE}" style="border-color:#dc3545">
    Syntaxe ✗ ({compteurs[ST_SYNTAXE]})
  </button>
  <button class="btn-filter" data-filter="{ST_PLEIN}" style="border-color:#ffc107">
    Boîte pleine ({compteurs[ST_PLEIN]})
  </button>
  <button class="btn-filter" data-filter="{ST_TEMP}" style="border-color:#6f42c1">
    Temp. ({compteurs[ST_TEMP]})
  </button>
  <button class="btn-filter" data-filter="{ST_IP_BLOQUEE}" style="border-color:#0dcaf0">
    IP bloquée ({compteurs[ST_IP_BLOQUEE]})
  </button>
  <button class="btn-filter" data-filter="{ST_INCERTAIN}" style="border-color:#6c757d">
    Incertain ({compteurs[ST_INCERTAIN]})
  </button>
  <input type="search" id="search" placeholder="Rechercher entreprise / email…">
</div>

<div id="counter"></div>

<table id="tbl">
  <thead>
    <tr>
      <th onclick="sortTable(0)">Entreprise ↕</th>
      <th onclick="sortTable(1)">Email ↕</th>
      <th onclick="sortTable(2)">Secteur ↕</th>
      <th onclick="sortTable(3)">Utilisateur ↕</th>
      <th onclick="sortTable(4)">Statut ↕</th>
      <th>Raison</th>
    </tr>
  </thead>
  <tbody>{lignes}
  </tbody>
</table>

<script>
let currentFilter = '';
let sortDir = {{}};

function applyFilters() {{
  const q = document.getElementById('search').value.toLowerCase();
  let visible = 0;
  document.querySelectorAll('#tbl tbody tr').forEach(tr => {{
    const statut = tr.dataset.statut;
    const text   = tr.textContent.toLowerCase();
    const show   = (!currentFilter || statut === currentFilter) && (!q || text.includes(q));
    tr.classList.toggle('hidden', !show);
    if (show) visible++;
  }});
  document.getElementById('counter').textContent = visible + ' ligne(s) affichée(s)';
}}

document.querySelectorAll('.btn-filter').forEach(btn => {{
  btn.addEventListener('click', () => {{
    document.querySelectorAll('.btn-filter').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    currentFilter = btn.dataset.filter;
    applyFilters();
  }});
}});

document.getElementById('search').addEventListener('input', applyFilters);

function sortTable(col) {{
  const tbody = document.querySelector('#tbl tbody');
  const rows  = Array.from(tbody.querySelectorAll('tr'));
  sortDir[col] = !sortDir[col];
  rows.sort((a, b) => {{
    const va = a.cells[col].textContent.trim();
    const vb = b.cells[col].textContent.trim();
    return sortDir[col] ? va.localeCompare(vb) : vb.localeCompare(va);
  }});
  rows.forEach(r => tbody.appendChild(r));
}}

applyFilters();
</script>
</body>
</html>"""

    with open(path, "w", encoding="utf-8") as f:
        f.write(html)


class Command(BaseCommand):
    help = "Vérifie tous les emails en base (MX + SMTP) et exporte les problématiques dans un CSV."

    def add_arguments(self, parser):
        parser.add_argument("--output",  default="emails_invalides.csv",
                            help="Fichier CSV de sortie (défaut : emails_invalides.csv)")
        parser.add_argument("--workers", type=int, default=20,
                            help="Nombre de threads parallèles (défaut : 20)")
        parser.add_argument("--timeout", type=int, default=8,
                            help="Timeout SMTP en secondes (défaut : 8)")
        parser.add_argument("--update-db", action="store_true",
                            help="Met à jour email_valide=False pour les hard bounces")
        parser.add_argument("--source", default="all",
                            choices=["all", "referentiel", "cibles"],
                            help="Source : all (défaut) | referentiel | cibles")
        parser.add_argument("--user", default=None,
                            help="Restreindre EntrepriseCible à un utilisateur (username)")

    def handle(self, *args, **options):
        output_path = options["output"]
        workers     = options["workers"]
        timeout     = options["timeout"]
        update_db   = options["update_db"]
        source      = options["source"]
        username    = options["user"]

        # ── Collecte des entrées à tester ─────────────────────────────────────
        # Format : (source, id, email, nom, secteur, utilisateur)
        items = []

        if source in ("all", "referentiel"):
            ref_qs = EntrepriseReferentiel.objects.exclude(email="").values_list(
                "id", "email", "raison_sociale", "code_noga"
            )
            for row in ref_qs.iterator(chunk_size=2000):
                items.append(("ref", row[0], row[1], row[2] or "", row[3] or "", "—"))

        if source in ("all", "cibles"):
            cible_qs = EntrepriseCible.objects.exclude(email="").select_related("utilisateur")
            if username:
                try:
                    user_obj = User.objects.get(username=username)
                    cible_qs = cible_qs.filter(utilisateur=user_obj)
                except User.DoesNotExist:
                    self.stderr.write(self.style.ERROR(f"Utilisateur '{username}' introuvable."))
                    sys.exit(1)
            for row in cible_qs.values_list("id", "email", "nom", "secteur_activite",
                                             "utilisateur__username").iterator(chunk_size=2000):
                items.append(("cible", row[0], row[1], row[2] or "", row[3] or "", row[4] or ""))

        total = len(items)
        if total == 0:
            self.stdout.write(self.style.WARNING("Aucune entreprise en base."))
            return

        self.stdout.write(f"\n{'='*60}")
        self.stdout.write(f"  Source   : {source}")
        self.stdout.write(f"  Emails   : {total}")
        self.stdout.write(f"  Threads  : {workers}  |  Timeout SMTP : {timeout}s")
        self.stdout.write(f"  Fichier  : {output_path}")
        self.stdout.write(f"{'='*60}\n")

        results = []
        lock = Lock()
        done_count = [0]
        compteurs = {s: 0 for s in _LABEL_MAP}

        def _task(row):
            src, ent_id, email, nom, secteur, uname = row
            statut, raison = _verifier_email(email, timeout)
            return src, ent_id, email, nom, secteur, uname, statut, raison

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_task, row): row for row in items}
            for fut in as_completed(futures):
                src, ent_id, email, nom, secteur, uname, statut, raison = fut.result()

                with lock:
                    done_count[0] += 1
                    compteurs[statut] += 1
                    done = done_count[0]

                    if statut != ST_VALIDE:
                        results.append({
                            "source":      src,
                            "entreprise":  nom,
                            "email":       email,
                            "secteur":     secteur,
                            "utilisateur": uname,
                            "statut":      statut,
                            "raison":      raison,
                        })
                        label  = _LABEL_MAP[statut]
                        line   = f"  [{label}] {email:<45} {raison}"
                        styled = getattr(self.style, _STYLE_MAP[statut], self.style.NOTICE)(line)
                        self.stdout.write(styled)

                    if update_db and statut in STATUTS_HARD_KO:
                        if src == "ref":
                            EntrepriseReferentiel.objects.filter(id=ent_id).update(email_valide=False)
                        else:
                            EntrepriseCible.objects.filter(id=ent_id).update(email_valide=False)

                    pct = round(done / total * 100)
                    self.stdout.write(
                        f"  {done}/{total} ({pct}%)  "
                        f"| invalides: {compteurs[ST_INVALIDE]+compteurs[ST_DOMAINE_KO]+compteurs[ST_MX_KO]}  "
                        f"| pleins: {compteurs[ST_PLEIN]}  "
                        f"| temp: {compteurs[ST_TEMP]}",
                        ending="\r",
                    )
                    self.stdout.flush()

        self.stdout.write("")

        # ── Écriture CSV ─────────────────────────────────────────────────────
        results.sort(key=lambda r: (r["statut"], r["email"]))
        with open(output_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=["source", "entreprise", "email", "secteur", "utilisateur", "statut", "raison"],
            )
            writer.writeheader()
            writer.writerows(results)

        # ── Écriture HTML ─────────────────────────────────────────────────────
        html_path = output_path.replace(".csv", ".html")
        _ecrire_html(html_path, results, compteurs, total, datetime.now())

        # ── Résumé final ─────────────────────────────────────────────────────
        self.stdout.write(f"\n{'='*60}")
        self.stdout.write(self.style.SUCCESS(f"  ✓  {total} emails vérifiés"))
        self.stdout.write(self.style.SUCCESS(f"  ✓  {compteurs[ST_VALIDE]} valides"))
        nb_hard_final = (compteurs[ST_INVALIDE] + compteurs[ST_DOMAINE_KO]
                         + compteurs[ST_MX_KO] + compteurs[ST_DESACTIVE] + compteurs[ST_SYNTAXE])
        self.stdout.write(self.style.ERROR(
            f"  ✗  {nb_hard_final} hard bounces  "
            f"(invalide:{compteurs[ST_INVALIDE]}  domaine:{compteurs[ST_DOMAINE_KO]}  "
            f"no-mx:{compteurs[ST_MX_KO]}  désactivé:{compteurs[ST_DESACTIVE]}  "
            f"syntaxe:{compteurs[ST_SYNTAXE]})"
        ))
        self.stdout.write(self.style.WARNING(f"  ⚠  {compteurs[ST_PLEIN]} boîtes pleines"))
        self.stdout.write(self.style.WARNING(f"  ⚠  {compteurs[ST_TEMP]} erreurs temporaires"))
        self.stdout.write(self.style.NOTICE(f"  ~  {compteurs[ST_IP_BLOQUEE]} IP bloquées (email potentiellement valide)"))
        self.stdout.write(self.style.NOTICE(f"  ?  {compteurs[ST_INCERTAIN]} incertains (SMTP injoignable)"))
        self.stdout.write(f"  CSV  : {output_path}")
        self.stdout.write(f"  HTML : {html_path}")
        self.stdout.write(f"  Date : {datetime.now().strftime('%d/%m/%Y %H:%M')}")
        self.stdout.write(f"{'='*60}\n")
