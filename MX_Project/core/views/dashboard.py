import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, Max
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.template.loader import render_to_string
from django.urls import reverse

from ..constants import NOGA_MAP, SECTEURS_NOGA_GROUPS
from ..forms import ProfilForm
from ..models import (
    Candidature,
    DocumentUtilisateur,
    GmailOAuthToken,
    LettreSecteurTemplate,
    ProfilUtilisateur,
)
from ._utils import _STATIC_VERSION
from .scan import _run_scan_for_user
from ._utils import _run_in_background

logger = logging.getLogger(__name__)


def _get_setup_status(user):
    profil, _ = ProfilUtilisateur.objects.get_or_create(user=user)
    profil_ok = bool(profil.prenom_lm and profil.nom_lm and profil.email_lm)

    secteurs_profil = set(
        NOGA_MAP.get(c.strip()[:2], c.strip())
        for c in profil.onboarding_secteurs.split(",")
        if c.strip()
    )
    secteurs_cibles = set(
        Candidature.objects
        .filter(utilisateur=user)
        .exclude(secteur_activite__isnull=True)
        .exclude(secteur_activite="")
        .values_list("secteur_activite", flat=True)
        .distinct()
    )
    secteurs_requis = {"Email"} | secteurs_profil | secteurs_cibles

    templates_ok = set(
        LettreSecteurTemplate.objects
        .filter(utilisateur=user)
        .exclude(paragraph_1="")
        .values_list("secteur_nom", flat=True)
    )
    secteurs_manquants = secteurs_requis - templates_ok
    gmail_connected = GmailOAuthToken.objects.filter(utilisateur=user).exists()

    return {
        "profil_ok": profil_ok,
        "secteurs_manquants": secteurs_manquants,
        "secteurs_requis": secteurs_requis,
        "gmail_connected": gmail_connected,
        "setup_complete": profil_ok and not secteurs_manquants and gmail_connected,
    }


@login_required
def dashboard(request):
    profil, _ = ProfilUtilisateur.objects.get_or_create(user=request.user)

    if not profil.onboarding_done:
        return redirect("onboarding")

    status = _get_setup_status(request.user)
    if not status["setup_complete"]:
        return redirect("settings_page")

    entreprises_list = (
        Candidature.objects
        .filter(utilisateur=request.user)
        .select_related('entreprise')
        .order_by('-id')
    )
    nb_restants = request.user.candidatures.filter(est_dans_paquet=False).count()

    paginator = Paginator(entreprises_list, 50)
    page_obj = paginator.get_page(request.GET.get('page'))

    secteurs_uniques = list(
        Candidature.objects
        .filter(utilisateur=request.user)
        .exclude(secteur_activite__isnull=True)
        .exclude(secteur_activite="")
        .values_list("secteur_activite", flat=True)
        .distinct()
        .order_by("secteur_activite")
    )

    tous_les_docs = list(DocumentUtilisateur.objects.filter(utilisateur=request.user).order_by("-date_upload"))
    packs_actifs_secteurs = {d.secteur_nom for d in tous_les_docs if d.type_doc == 'PACK_LM' and d.secteur_nom}

    return render(request, 'core/dashboard.html', {
        'entreprises': page_obj,
        'total_entreprises': paginator.count,
        'tous_les_docs': tous_les_docs,
        'secteurs_uniques': secteurs_uniques,
        'secteurs_noga': SECTEURS_NOGA_GROUPS,
        'gmail_connected': status["gmail_connected"],
        'static_version': _STATIC_VERSION,
        'nb_restants': nb_restants,
        'packs_actifs_secteurs': packs_actifs_secteurs,
    })


@login_required
def entreprises_filtrer_secteur(request):
    secteur = (request.GET.get("secteur") or "").strip()
    qs = (
        Candidature.objects
        .filter(utilisateur=request.user)
        .select_related('entreprise')
        .order_by("-id")
    )

    if secteur:
        qs = qs.filter(secteur_activite=secteur)

    paginator = Paginator(qs, 50)
    page_obj = paginator.get_page(request.GET.get("page"))
    tbody_html = render_to_string(
        "partials/entreprises_table.html",
        {"entreprises": page_obj},
        request=request,
    )

    pack_infos = []
    if secteur:
        qs_pack_all = Candidature.objects.filter(
            utilisateur=request.user, secteur_activite=secteur,
        )
        qs_pack_remaining = qs_pack_all.filter(est_dans_paquet=False)
        max_pack = qs_pack_all.aggregate(m=Max("numero_pack")).get("m") or 0
        secteur_clean = secteur.replace(" ", "_").replace("/", "-")
        docs = {
            d.nom_affichage: d
            for d in DocumentUtilisateur.objects.filter(
                utilisateur=request.user, type_doc="PACK_LM", secteur_nom=secteur,
            )
        }
        all_counts = dict(
            qs_pack_all.values("numero_pack").annotate(n=Count("id")).values_list("numero_pack", "n")
        )
        remaining_counts = dict(
            qs_pack_remaining.values("numero_pack").annotate(n=Count("id")).values_list("numero_pack", "n")
        )
        for i in range(1, int(max_pack) + 1):
            nom_base = f"MX_SCAN_{secteur_clean}_PACK_{i}"
            doc = docs.get(nom_base)
            total_cnt = all_counts.get(i, 0)
            remaining_cnt = remaining_counts.get(i, 0)
            if not total_cnt and not doc:
                continue
            pack_infos.append({
                "pack_num": i,
                "count": total_cnt,
                "remaining": remaining_cnt,
                "secteur": secteur,
                "doc_url": (doc.fichier.url if doc else ""),
                "is_used": bool(getattr(doc, "used_for_gmail", False)) if doc else False,
            })

    packs_html = render_to_string(
        "partials/packs_cards.html",
        {"pack_infos": pack_infos},
        request=request,
    )

    is_ajax = (
        request.headers.get("X-Requested-With") == "XMLHttpRequest"
        or "application/json" in request.headers.get("Accept", "")
    )
    if is_ajax:
        return JsonResponse({"tbody": tbody_html, "packs": packs_html})

    return render(request, "partials/entreprises_table.html", {"entreprises": page_obj})


@login_required
def settings_page(request):
    profil, _ = ProfilUtilisateur.objects.get_or_create(user=request.user)
    required_fields = ["prenom_lm", "nom_lm", "email_lm"]

    secteurs_profil = [
        NOGA_MAP.get(c.strip()[:2], c.strip())
        for c in profil.onboarding_secteurs.split(",")
        if c.strip()
    ]
    secteurs_cibles = list(
        Candidature.objects
        .filter(utilisateur=request.user)
        .exclude(secteur_activite__isnull=True)
        .exclude(secteur_activite="")
        .values_list("secteur_activite", flat=True)
        .distinct()
        .order_by("secteur_activite")
    )
    secteurs_requis_set = {"Email"} | set(secteurs_profil) | set(secteurs_cibles)
    secteurs_requis_list = ["Email"] + sorted(secteurs_requis_set - {"Email"})

    templates_qs = LettreSecteurTemplate.objects.filter(utilisateur=request.user)
    templates_data = {
        t.secteur_nom: {
            "objet":       t.objet or "",
            "salutation":  t.salutation or "",
            "paragraph_1": t.paragraph_1 or "",
            "paragraph_2": t.paragraph_2 or "",
            "paragraph_3": t.paragraph_3 or "",
            "paragraph_4": t.paragraph_4 or "",
            "conclusion":  t.conclusion or "",
        }
        for t in templates_qs
    }

    if request.method == 'POST':
        action = request.POST.get("action", "")

        if action == "save_profil":
            form = ProfilForm(request.POST, instance=profil, required_fields=required_fields)
            if form.is_valid():
                form.save()
                messages.success(request, "✅ Informations personnelles sauvegardées.")
            else:
                for field, errors in form.errors.items():
                    for error in errors:
                        messages.error(request, f"Erreur — {field} : {error}")
            return redirect('settings_page')

        elif action == "save_template":
            secteur_tpl = (request.POST.get("template_secteur") or "Email").strip()[:100] or "Email"
            p1 = (request.POST.get("paragraph_1") or "").strip()
            if not p1:
                messages.error(request, f"⚠ Le Paragraphe 1 est obligatoire pour le template « {secteur_tpl} ».")
                return redirect('/settings/?tab=templates')

            tpl, _ = LettreSecteurTemplate.objects.get_or_create(
                utilisateur=request.user, secteur_nom=secteur_tpl,
            )
            tpl.objet       = (request.POST.get("objet")       or "").strip()
            tpl.salutation  = (request.POST.get("introduction") or "").strip()
            tpl.paragraph_1 = p1
            tpl.paragraph_2 = (request.POST.get("paragraph_2") or "").strip()
            tpl.paragraph_3 = (request.POST.get("paragraph_3") or "").strip()
            tpl.paragraph_4 = (request.POST.get("paragraph_4") or "").strip()
            tpl.conclusion  = (request.POST.get("conclusion")   or "").strip()
            tpl.save()

            messages.success(request, f"✅ Template « {secteur_tpl} » sauvegardé.")
            return redirect(f"/settings/?tab=templates&secteur={secteur_tpl}")

        return redirect('settings_page')

    form = ProfilForm(instance=profil, required_fields=required_fields)
    status = _get_setup_status(request.user)

    return render(request, 'core/settings.html', {
        'form':                  form,
        'profil':                profil,
        'secteurs_requis_list':  secteurs_requis_list,
        'templates_data':        templates_data,
        'templates_by_secteur':  {t.secteur_nom: t for t in templates_qs},
        'gmail_connected':       status["gmail_connected"],
        'secteurs_manquants':    sorted(status["secteurs_manquants"]),
        'profil_ok':             status["profil_ok"],
        'setup_complete':        status["setup_complete"],
        'active_tab':            request.GET.get("tab", "identite"),
        'active_secteur':        request.GET.get("secteur", "Email"),
    })


@login_required
def onboarding(request):
    profil, _ = ProfilUtilisateur.objects.get_or_create(user=request.user)
    if profil.onboarding_done:
        return redirect("settings_page")

    secteurs = [
        ("62", "Informatique (62)"),
        ("71", "Architecture (71)"),
        ("64", "Banque (64)"),
        ("86", "Santé (86)"),
        ("43", "Construction (43)"),
        ("47", "Horlogerie/Luxe (47)"),
        ("88", "Social (88)"),
        ("87", "Hébergement (87)"),
    ]

    if request.method == "POST":
        choix = request.POST.getlist("secteurs")
        if not choix:
            return render(request, "core/onboarding.html", {
                "secteurs": secteurs,
                "erreur": "Coche au moins un secteur pour continuer.",
            })
        profil.onboarding_done = True
        profil.onboarding_secteurs = ",".join(choix)
        profil.save(update_fields=["onboarding_done", "onboarding_secteurs"])
        _run_in_background(_run_scan_for_user, request.user, choix)
        return redirect("settings_page")

    return render(request, "core/onboarding.html", {"secteurs": secteurs})


@login_required
def add_secteurs(request):
    profil, _ = ProfilUtilisateur.objects.get_or_create(user=request.user)
    existing_codes = set(c.strip() for c in profil.onboarding_secteurs.split(",") if c.strip())

    if request.method == "POST":
        submitted_codes = set(request.POST.getlist("secteurs"))

        if not submitted_codes:
            return render(request, "core/add_secteurs.html", {
                "existing_codes": existing_codes,
                "groups": SECTEURS_NOGA_GROUPS,
                "erreur": "Coche au moins un secteur pour continuer.",
            })

        new_codes = submitted_codes - existing_codes
        profil.onboarding_secteurs = ",".join(sorted(existing_codes | submitted_codes))
        profil.save(update_fields=["onboarding_secteurs"])

        if new_codes:
            _run_in_background(_run_scan_for_user, request.user, list(new_codes))
            messages.success(
                request,
                f"✅ {len(new_codes)} nouveau(x) secteur(s) ajouté(s). Configure les templates LM associés pour débloquer le dashboard."
            )
        else:
            messages.info(request, "Aucun nouveau secteur — ta sélection est déjà active.")

        return redirect(reverse('settings_page') + '?tab=templates')

    return render(request, "core/add_secteurs.html", {
        "existing_codes": existing_codes,
        "groups": SECTEURS_NOGA_GROUPS,
    })
