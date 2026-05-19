# Audit du code mort — Postulation_MX

**Date** : 2026-05-19
**Périmètre analysé** : `MX_Project/core/` + `MX_Project/MX_Project/` + `resources/js/` + templates `.html`
**Méthode** : 7 vérifications grep par candidat (Python, urls, templates, JS, migrations, tests, config)

---

## Résumé

| Statut    | Nombre | Action proposée                            |
|-----------|--------|--------------------------------------------|
| CERTAIN   | 3      | Commentage immédiat après validation       |
| PROBABLE  | 2      | Discussion symbole par symbole             |
| DOUTEUX   | 1      | À ne PAS toucher sans confirmation         |

---

## CANDIDATS CERTAINS

### C1 — Variable `secteurs` hardcodée dans la vue `onboarding`
- **Fichier** : `MX_Project/core/views/dashboard.py`
- **Lignes** : 284-293 (définition) + 299, 308 (passées au contexte)
- **Symbole** : variable locale `secteurs` (liste hardcodée de 8 tuples)
- **Raison** : variable construite puis passée au template `core/onboarding.html` (clé `"secteurs"`), mais le template ne contient AUCUNE référence à `{{ secteurs }}`, `{% for s in secteurs %}`, ni `{% if … in secteurs %}`. Les 89 cases à cocher (codes NOGA 01 à 96) sont écrites en HTML brut dans le template. La variable est donc construite, transportée jusqu'au template, puis ignorée.
- **Vérifications 7-points** :
  1. `grep "secteurs"` dans tout le projet : nombreux matches → variable d'usage générique, mais aucun n'utilise CETTE valeur précise (voir points 3-7 ci-dessous).
  2. `urls.py` : aucune référence à la variable locale (normal).
  3. Templates HTML : `grep "{{ secteurs }}"` dans `onboarding.html` → **aucun match**. `grep "{% for .* in secteurs %}"` → aucun match. Le template a sa propre liste statique (lignes 63-163).
  4. Fichiers JS : aucun usage de `secteurs` côté contexte template (les JS lisent `input[name="secteurs"]`, ce qui est le champ POST, indépendant).
  5. Migrations : aucun match.
  6. Tests : aucun test ne vérifie cette variable de contexte.
  7. `admin.py`, `apps.py`, `settings.py`, `render.yaml` : aucun match.
- **Confiance** : **CERTAIN**
- **Note** : la variable contient 8 tuples (`"62"`, `"71"`, `"64"`, `"86"`, `"43"`, `"47"`, `"88"`, `"87"`) — c'est probablement un reliquat d'une ancienne version courte du template. Le template actuel énumère les 89 codes NOGA en dur.

### C2 — Variable de contexte `secteurs_noga` dans la vue `dashboard`
- **Fichier** : `MX_Project/core/views/dashboard.py`
- **Ligne** : 105 (`'secteurs_noga': SECTEURS_NOGA_GROUPS`)
- **Symbole** : clé de contexte `secteurs_noga` passée au template `core/dashboard.html`
- **Raison** : `SECTEURS_NOGA_GROUPS` est légitimement utilisée dans la vue `add_secteurs` (passée sous la clé `"groups"` et consommée par `add_secteurs.html`). Mais dans `dashboard`, elle est passée sous la clé `"secteurs_noga"` au template `dashboard.html` qui n'y fait JAMAIS référence.
- **Vérifications 7-points** :
  1. `grep "secteurs_noga"` dans tout le projet → **un seul match** : la définition à `dashboard.py:105`.
  2. `urls.py` : aucun match.
  3. Templates HTML : `grep "secteurs_noga"` dans tous les `.html` → **aucun match**.
  4. JS : aucun match.
  5. Migrations : aucun match.
  6. Tests : aucun match.
  7. Config : aucun match.
- **Confiance** : **CERTAIN**
- **Note** : la constante `SECTEURS_NOGA_GROUPS` elle-même reste utilisée (par `add_secteurs`), donc on ne touche QUE la ligne du contexte dans `dashboard()`. L'import de la constante reste nécessaire.

### C3 — Fichier `resources/js/app.js` quasi vide et jamais chargé
- **Fichier** : `resources/js/app.js`
- **Contenu** : 1 ligne vide (uniquement un saut de ligne).
- **Raison** : fichier vide qui n'est référencé par aucun template (`grep "app.js"` → 0 match dans `.html`). Son équivalent généré `staticfiles/js/app.js` (hors périmètre) est tout aussi vide.
- **Vérifications 7-points** :
  1-7. `grep "app.js"` dans tout le projet → **aucun match** (hormis le fichier lui-même).
- **Confiance** : **CERTAIN** (fichier mort)
- **Note** : **Rien à commenter** — le fichier est vide. Je propose **soit** de le laisser tel quel (zéro impact), **soit** d'y ajouter un commentaire d'audit `// DEAD CODE — fichier vide, jamais chargé`. Pas de suppression sans confirmation.

---

## CANDIDATS PROBABLES

### P1 — Référence JS à l'élément inexistant `#helpIframe` (6 templates)
- **Fichiers concernés** :
  - `MX_Project/core/templates/core/dashboard.html:343-346`
  - `MX_Project/core/templates/core/detail_scan.html:172-176`
  - `MX_Project/core/templates/core/historique.html:121-125`
  - `MX_Project/core/templates/core/onboarding.html:195-198`
  - `MX_Project/core/templates/core/settings.html:669-672`
  - `MX_Project/core/templates/registration/login.html:377-380`
- **Symbole** : ID DOM `helpIframe`
- **Bloc concerné** (identique dans les 6 templates) :
  ```js
  document.getElementById('videoAideModal')?.addEventListener('hide.bs.modal', () => {
      const f = document.getElementById('helpIframe');
      if (f) { const s = f.src; f.src = ''; f.src = s; }
  });
  ```
- **Raison** : le code cherche un élément `#helpIframe` pour réinitialiser son `src` à la fermeture du modal vidéo. Or AUCUN template ne définit cet ID — le modal contient une `<img>` + une `<a>` vers YouTube, pas d'iframe. Le `if (f)` est donc **toujours faux** → le bloc ne s'exécute jamais.
- **Vérifications 7-points** :
  1-2-3. `grep "helpIframe"` → 6 matches uniques (tous les 6 lecteurs ci-dessus). **Aucun `id="helpIframe"`** nulle part.
  4-7. JS / migrations / tests / config : aucun usage.
- **Confiance** : **PROBABLE**
- **Pourquoi pas CERTAIN** : le code est techniquement « défensif » (`if (f)` protège correctement). Les règles d'audit disent : *« un cas-limite explicitement commenté n'est PAS mort même s'il n'a jamais été déclenché »*. Ce n'est pas commenté ici, mais c'est défensif. C'est vraisemblablement un vestige d'une ancienne version qui utilisait `<iframe>` pour la vidéo YouTube (au lieu de `<a>`). À confirmer avant commentage.

### P2 — Référence JS à l'élément inexistant `#gmailSecteurHidden`
- **Fichier** : `resources/js/scan-history.js:12, 27-29`
- **Symbole** : ID DOM `gmailSecteurHidden`
- **Bloc concerné** :
  ```js
  const hiddenGmailSecteur = document.getElementById("gmailSecteurHidden");
  …
  function syncHidden() {
      const v = select.value || "";
      if (hiddenGmailSecteur) hiddenGmailSecteur.value = v;
      …
  }
  ```
- **Raison** : ID `gmailSecteurHidden` jamais défini dans aucun template. Le code défensif `if (hiddenGmailSecteur)` rend la branche morte (l'autre branche `gmailSecteurField` est, elle, bien utilisée).
- **Vérifications 7-points** :
  1-3. `grep "gmailSecteurHidden"` → 2 matches dans `resources/js/scan-history.js` + 1 dans `staticfiles/js/scan-history.min.js` (généré, hors périmètre). **Aucun `id="gmailSecteurHidden"`** dans les `.html`.
  4-7. Aucun autre usage.
- **Confiance** : **PROBABLE**
- **Pourquoi pas CERTAIN** : code défensif (`if (hiddenGmailSecteur)`). Vestige d'une ancienne version. Note : si on commente ce bloc, il faudra aussi **regénérer `scan-history.min.js`** (sinon le code resté dans la version minifiée serait incohérent). Ne pas toucher sans plan de regénération.

---

## CANDIDATS DOUTEUX

### D1 — Fonction `verifier_email_existence` exportée mais non appelée en production
- **Fichier** : `MX_Project/core/views/_utils.py:36-44`
- **Symbole** : fonction `verifier_email_existence(email: str) -> bool`
- **Exportation** : importée dans `MX_Project/core/views/__init__.py:41` (réexport explicite).
- **Raison potentielle de mort** : appelée UNIQUEMENT dans `core/tests.py` (lignes 23-33). Aucune vue, aucune commande de gestion, aucun template, aucun cron ne l'utilise. La validation d'emails réelle passe par `_verifier_email()` dans `core/management/commands/check_emails.py` (fonction différente, plus complète).
- **Vérifications 7-points** :
  1. `grep "verifier_email_existence"` → 8 matches : 4 dans `tests.py`, 1 dans `_utils.py` (définition), 1 dans `views/__init__.py` (réexport). **Aucun appel en code de production**.
  2-7. Aucun autre usage.
- **Confiance** : **DOUTEUX**
- **Pourquoi DOUTEUX et non CERTAIN** :
  - **Règle explicite** : *« Les fonctions utilitaires exportées dans `__init__.py` ou via `__all__` »* sont à laisser intactes.
  - La fonction est réexportée comme API publique du package `core.views` et fait l'objet de 3 tests dédiés.
  - Pourrait être réutilisée à terme (validation rapide MX-only sans appel SMTP).
- **Recommandation** : **NE PAS TOUCHER** sans confirmation explicite.

---

## ÉLÉMENTS EXAMINÉS PUIS ÉCARTÉS (NON MORTS)

Pour transparence, voici les candidats initialement suspects mais qui se sont avérés vivants après vérification :

| Élément | Pourquoi vivant |
|---------|-----------------|
| `_compute_static_version`, `_STATIC_VERSION` (`_utils.py`) | Utilisée par `dashboard.py:107` (`static_version`), affichée dans `dashboard.html` (`?v=…`). |
| `MIME_TYPES_AUTORISES`, `validate_file` (`models.py`) | `validate_file` est un validator du FileField `DocumentUtilisateur.fichier`. |
| `_VirtualSession.nb_doublons_evites` (`scan.py:36`) | Affiché dans `historique.html:55` et `detail_scan.html:36` (toujours à 0 après migration — comportement intentionnel). |
| `_fernet`, `_encrypt_token`, `_decrypt_token` (`models.py`) | Utilisés par `GmailOAuthToken.save()` / `from_db()` (chiffrement Fernet). |
| `phrase_informatique`, `phrase_banque`, `phrase_luxe`, `phrase_generale` (`models.py`) | Utilisés par `get_accroche()` dans `_utils.py:49-58`. |
| `UserFilteredAdmin.save_model` (`admin.py`) | Hook Django admin standard. |
| `_BG_SEMAPHORE` (`_utils.py:9`) | Utilisé par `_run_in_background`. |
| `_get_token_lock`, `_TOKEN_REFRESH_LOCKS` (`auth.py`) | Mutex anti-race pour le refresh token Gmail — utilisé par `_gmail_get_access_token`. |
| `_b64url`, `_build_mime_message`, `_gmail_create_draft` (`gmail.py`) | Tous utilisés dans le job background `_run_brouillons`. |
| `_slugify_loose`, `_email_to_pdf_name`, `_safe_format`, `_read_filefield_bytes` (`_utils.py`) | Tous utilisés dans `_pdf.py`, `gmail.py`, `downloads.py`. |
| `get_accroche` (`_utils.py`) | Utilisée dans `_pdf.py:50` et `gmail.py:163`. |
| `_run_in_background` (`_utils.py`) | Utilisée 4× dans `scan.py`, `gmail.py`, `dashboard.py`. |
| `settings_test.py` | Utilisable via `DJANGO_SETTINGS_MODULE=MX_Project.settings_test`. Considéré comme API publique de tests. |
| `MX_Project/emails_invalides.html` (racine) | Artefact généré par `check_emails`, présent dans `.gitignore`. Pas du code source. |
| Management commands `check_emails.py`, `purge_bounces.py` | Lancées manuellement par admin (règle explicite : *« même si non appelées en cron, un admin peut les lancer à la main »*). |
| Toutes les `TYPES_DOC` (`CERTIFICAT`, `DIPLOME`, `GUIDE`, `AUTRE`) | Choices Django stockés en DB, affichés par `get_type_doc_display()`. |
| Tests : `test_all.py` | Bridge officiel pour `python manage.py test` (commentaire interne le documente). |

---

## CAS SPÉCIFIQUE — variable `secteurs` dans `dashboard.py` (rappel)

Le cas que vous m'avez explicitement demandé de vérifier est **C1** ci-dessus.

**Confirmation après les 7 vérifications** : la variable est bien **CERTAINE** d'être du code mort.

Détails techniques :
- La variable existe dans la fonction `onboarding(request)` (et NON dans `dashboard(request)` comme le suggérait l'énoncé — vérifié en lisant le fichier).
- Lignes 284-293 : déclaration.
- Lignes 299 et 308 : passées au template via `render(..., {"secteurs": secteurs, …})`.
- Aucun usage côté template `onboarding.html` — celui-ci possède sa propre liste complète des 89 codes NOGA en HTML brut.

---

## Prochaine étape

Conformément aux règles, je m'arrête ici et attends ta validation.

**Commenter les blocs CERTAIN** (C1, C2, C3) ? Ou veux-tu d'abord discuter des PROBABLE / DOUTEUX ?

Pour rappel :
- **CERTAIN → action immédiate** sur validation globale.
- **PROBABLE → confirmation symbole par symbole** requise.
- **DOUTEUX → on ne touche pas** sans confirmation explicite.
