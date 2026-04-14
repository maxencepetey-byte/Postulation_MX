(function () {
  function ready(fn) {
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", fn);
    else fn();
  }

  ready(() => {
    const select = document.getElementById("secteurSelect");
    const tbody = document.getElementById("entreprisesTbody");
    const packsSection = document.getElementById("packsSection");
    const packsGrid = document.getElementById("packsGrid");
    const hiddenGmailSecteur = document.getElementById("gmailSecteurHidden");

    if (!select || !tbody) return;

    const url = select.dataset.url;
    if (!url) return;

    const presetSecteur = new URLSearchParams(window.location.search).get("secteur");
    if (presetSecteur) {
      const hasOption = Array.from(select.options).some((o) => o.value === presetSecteur);
      if (hasOption) select.value = presetSecteur;
    }

    let abortController = null;

    function syncHidden() {
      const v = select.value || "";
      if (hiddenGmailSecteur) hiddenGmailSecteur.value = v;
      const gmailSecteurField = document.getElementById("gmailSecteurField");
      if (gmailSecteurField) gmailSecteurField.value = v;
    }

    async function refresh() {
      syncHidden();

      const secteur = select.value || "";
      const targetUrl = `${url}?secteur=${encodeURIComponent(secteur)}`;

      if (abortController) abortController.abort();
      abortController = new AbortController();

      try {
        tbody.innerHTML =
          '<tr><td colspan="5" class="text-center py-4 text-muted">Chargement…</td></tr>';

        const res = await fetch(targetUrl, {
          headers: {
            "X-Requested-With": "XMLHttpRequest",
            Accept: "application/json",
          },
          signal: abortController.signal,
        });

        if (!res.ok) throw new Error(`HTTP ${res.status}`);

        const data = await res.json();
        if (typeof data.tbody === "string") tbody.innerHTML = data.tbody;
        if (typeof data.packs === "string") {
          const html = data.packs.trim();
          if (packsGrid) packsGrid.innerHTML = html;
          if (packsSection) packsSection.style.display = html ? "" : "none";
        }
      } catch (e) {
        if (e && e.name === "AbortError") return;
        tbody.innerHTML =
          '<tr><td colspan="5" class="text-center py-4 text-danger">Erreur de chargement.</td></tr>';
        if (packsGrid) packsGrid.innerHTML = "";
        if (packsSection) packsSection.style.display = "none";
      }
    }

    syncHidden();
    select.addEventListener("change", refresh);
    if (select.value) refresh();

    // ── Polling progression Gmail ──────────────────────────────────────
    const progressBar = document.getElementById("gmailProgressBar");
    const progressWrap = document.getElementById("gmailProgressWrap");
    const progressText = document.getElementById("gmailProgressText");
    const progressUrl = document.getElementById("gmailProgressWrap")
      ? document.getElementById("gmailProgressWrap").dataset.url
      : null;

    if (!progressUrl) return;

    let pollInterval = null;
    let lastDone = -1;

    async function pollProgress() {
      const secteur = select ? select.value || "" : "";
      try {
        const res = await fetch(`${progressUrl}?secteur=${encodeURIComponent(secteur)}`, {
          headers: { "X-Requested-With": "XMLHttpRequest" },
        });
        if (!res.ok) return;
        const d = await res.json();

        const { total, done, remaining, percent } = d;

        if (progressWrap) progressWrap.style.display = remaining > 0 ? "block" : "none";
        if (progressBar) {
          progressBar.style.width = percent + "%";
          progressBar.setAttribute("aria-valuenow", percent);
        }
        if (progressText) {
          progressText.textContent = remaining > 0
            ? `${done} / ${total} brouillons créés… (${remaining} restants)`
            : `✅ ${done} brouillons créés`;
        }

        // Rafraîchit le tableau quand des lignes passent à "Traité"
        if (done !== lastDone && done > 0) {
          lastDone = done;
          refresh();
        }

        // Stop polling quand tout est traité
        if (remaining === 0 && pollInterval) {
          clearInterval(pollInterval);
          pollInterval = null;
        }
      } catch (_) {}
    }

    // Démarre le polling si la bannière est visible au chargement (job en cours)
    if (progressWrap && progressWrap.dataset.active === "1") {
      pollInterval = setInterval(pollProgress, 3000);
      pollProgress();
    }

    // Démarre le polling quand l'utilisateur clique sur "Créer Brouillons"
    const gmailForm = document.getElementById("gmailForm");
    if (gmailForm) {
      gmailForm.addEventListener("submit", () => {
        setTimeout(() => {
          if (!pollInterval) {
            pollInterval = setInterval(pollProgress, 3000);
            pollProgress();
          }
        }, 1500);
      });
    }
  });
})();