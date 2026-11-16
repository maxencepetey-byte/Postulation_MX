// Bandeau du lot de brouillons Gmail : suit l'avancement tant que le lot tourne.
// Ces appels réguliers gardent aussi le service Render éveillé pendant le lot.
(function () {
  "use strict";

  var bandeau = document.getElementById("lotBrouillons");
  if (!bandeau || !("enCours" in bandeau.dataset)) return;

  var texte = bandeau.querySelector("[data-lot-texte]");
  var roue = bandeau.querySelector(".spinner-border");
  var lien = bandeau.querySelector("[data-lot-actualiser]");
  var INTERVALLE = 4000;
  var echecs = 0;

  function afficher(etat) {
    if (!etat.texte) return;
    texte.textContent = etat.texte;
    bandeau.className = bandeau.className.replace(/\balert-(info|success|warning|danger)\b/, "alert-" + etat.classe);
  }

  function arreter() {
    roue.classList.add("d-none");
    lien.classList.remove("d-none");
  }

  function suivre() {
    fetch(bandeau.dataset.url, { headers: { Accept: "application/json" }, credentials: "same-origin" })
      .then(function (r) {
        // Session expirée : la requête est renvoyée vers la page de connexion, inutile d'insister.
        if (r.redirected) {
          echecs = 10;
          throw new Error("redirection");
        }
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (etat) {
        echecs = 0;
        afficher(etat);
        if (etat.en_cours) {
          setTimeout(suivre, INTERVALLE);
          return;
        }
        // Lot fini : la liste et les compteurs affichés datent d'avant, d'où le lien d'actualisation.
        arreter();
      })
      .catch(function () {
        // Coupure réseau ou service qui se réveille : on réessaie plus tard, sans alarmer.
        echecs += 1;
        if (echecs < 10) setTimeout(suivre, INTERVALLE * 3);
        else arreter();
      });
  }

  setTimeout(suivre, INTERVALLE);
})();
