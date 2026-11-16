// Formulaires : confirmation (data-confirm) et blocage du double envoi (data-once).
// Délégation sur document : les cartes de packs sont injectées en AJAX après le chargement.
(function () {
  "use strict";

  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (e.defaultPrevented || !(form instanceof HTMLFormElement)) return;

    if (form.dataset.sending) {
      e.preventDefault();
      return;
    }
    if (form.dataset.confirm && !window.confirm(form.dataset.confirm)) {
      e.preventDefault();
      return;
    }
    if (!("once" in form.dataset)) return;

    form.dataset.sending = "1";
    // Classe « disabled » plutôt que la propriété : Firefox garde un bouton désactivé après F5,
    // et un bouton réellement désactivé est exclu des données envoyées.
    form.querySelectorAll('button[type="submit"]').forEach(function (btn) {
      btn.dataset.label = btn.innerHTML;
      btn.classList.add("disabled");
      btn.setAttribute("aria-disabled", "true");
      btn.innerHTML =
        '<span class="spinner-border spinner-border-sm me-1" aria-hidden="true"></span>Traitement…';
    });
  });

  // Retour arrière du navigateur : la page revient du cache avec ses boutons encore bloqués.
  window.addEventListener("pageshow", function (e) {
    if (!e.persisted) return;
    document.querySelectorAll("form[data-sending]").forEach(function (form) {
      delete form.dataset.sending;
      form.querySelectorAll('button[type="submit"]').forEach(function (btn) {
        btn.classList.remove("disabled");
        btn.removeAttribute("aria-disabled");
        if (btn.dataset.label) btn.innerHTML = btn.dataset.label;
      });
    });
  });
})();
