// Tableau de bord : filtre par secteur, liste des entreprises, packs et documents.
(function () {
  "use strict";

  var tbody = document.getElementById("entreprisesTbody");
  if (!tbody) return;

  var filterUrl = tbody.dataset.filterUrl;
  var pager = document.getElementById("paginationBar");
  var packsSection = document.getElementById("packsSection");
  var packsGrid = document.getElementById("packsGrid");
  var packsLabel = document.getElementById("packsSecteurLabel");
  var gmailField = document.getElementById("gmailSecteurField");
  var gmailForm = document.getElementById("gmailForm");
  var pills = Array.prototype.slice.call(document.querySelectorAll(".secteur-filter-btn"));
  var docs = Array.prototype.slice.call(document.querySelectorAll(".doc-item"));
  var currentSecteur = "";
  var requete = null;

  function ligne(contenu) {
    tbody.innerHTML = '<tr><td colspan="5" class="text-center py-5 text-muted">' + contenu + "</td></tr>";
  }

  function filterDocs(secteur) {
    // Les documents sans secteur (CV…) restent visibles ; un pack ne l'est que pour son secteur.
    docs.forEach(function (item) {
      var docSecteur = item.dataset.secteur;
      item.style.display = !secteur || !docSecteur || docSecteur === secteur ? "" : "none";
    });
  }

  function choisir(secteur) {
    currentSecteur = secteur;
    if (gmailField) gmailField.value = secteur;
    var btn = gmailForm && gmailForm.querySelector('button[type="submit"]');
    if (btn) btn.title = secteur ? "" : "Sélectionne un secteur pour créer des brouillons";
    filterDocs(secteur);
    fetchSecteur(secteur);
  }

  function fetchSecteur(secteur, page) {
    var params = new URLSearchParams();
    if (secteur) params.set("secteur", secteur);
    if (page) params.set("page", page);
    var url = params.toString() ? filterUrl + "?" + params : filterUrl;

    // Une réponse en retard ne doit pas écraser celle du dernier secteur cliqué.
    if (requete) requete.abort();
    requete = new AbortController();
    var signal = requete.signal;

    tbody.setAttribute("aria-busy", "true");
    ligne('<span class="spinner-border spinner-border-sm me-2" aria-hidden="true"></span>Chargement…');

    fetch(url, { headers: { "X-Requested-With": "XMLHttpRequest" }, signal: signal })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (data) {
        tbody.innerHTML = data.tbody || "";
        if (pager) pager.innerHTML = data.pagination || "";
        if (secteur && data.packs) {
          packsGrid.innerHTML = data.packs;
          packsLabel.textContent = "— " + secteur;
          packsSection.style.display = "";
        } else {
          packsSection.style.display = "none";
          packsGrid.innerHTML = "";
          packsLabel.textContent = "";
        }
      })
      .catch(function (err) {
        if (err && err.name === "AbortError") return;
        ligne(
          '<span class="text-danger d-block mb-2"><i class="bi bi-exclamation-triangle-fill me-1"></i>' +
            "Impossible de charger les entreprises.</span>" +
            '<button type="button" class="btn btn-sm btn-outline-primary" data-reessayer>Réessayer</button>'
        );
        tbody.querySelector("[data-reessayer]").addEventListener("click", function () {
          fetchSecteur(secteur, page);
        });
      })
      .finally(function () {
        if (!signal.aborted) tbody.removeAttribute("aria-busy");
      });
  }

  // Sans secteur choisi, le formulaire Gmail ne part pas.
  if (gmailForm) {
    gmailForm.addEventListener("submit", function (e) {
      var secteur = (gmailField && gmailField.value) || "";
      if (!secteur) {
        e.preventDefault();
        alert("Sélectionne un secteur avant de créer des brouillons.");
        return;
      }
      e.currentTarget.dataset.confirm = "Créer les brouillons Gmail pour le secteur « " + secteur + " » ?";
    });
  }

  pills.forEach(function (btn) {
    btn.addEventListener("click", function () {
      pills.forEach(function (b) {
        if (b.disabled) return;
        b.classList.remove("active", "btn-primary");
        b.classList.add("btn-outline-primary");
        b.setAttribute("aria-pressed", "false");
      });
      btn.classList.add("active", "btn-primary");
      btn.classList.remove("btn-outline-primary");
      btn.setAttribute("aria-pressed", "true");
      choisir(btn.dataset.secteur);
    });
  });

  if (pager) {
    pager.addEventListener("click", function (e) {
      var link = e.target.closest("a[data-page]");
      if (!link) return;
      e.preventDefault();
      fetchSecteur(currentSecteur, link.dataset.page);
      tbody.closest(".card").scrollIntoView({ behavior: "smooth" });
    });
  }

  // Clic sur un pack de la liste des documents : affiche son secteur.
  docs.forEach(function (item) {
    var secteur = item.dataset.secteur;
    var nom = item.querySelector(".doc-name-btn");
    if (!secteur || !nom) return;
    nom.addEventListener("click", function () {
      docs.forEach(function (d) {
        d.classList.remove("doc-selected");
      });
      item.classList.add("doc-selected");
      var pill = pills.find(function (b) {
        return b.dataset.secteur === secteur;
      });
      if (pill && !pill.disabled) pill.click();
      else choisir(secteur);
    });
  });

  // Au chargement : le secteur de l'URL (?secteur=…), sinon le premier secteur accessible.
  var demande = new URLSearchParams(window.location.search).get("secteur") || "";
  var depart =
    pills.find(function (b) {
      return demande && !b.disabled && b.dataset.secteur === demande;
    }) ||
    pills.find(function (b) {
      return !b.disabled && b.dataset.secteur;
    });
  if (depart) depart.click();
})();
