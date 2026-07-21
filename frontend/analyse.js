// ═══════════════════════════════════════════════════════════════
// CinéTour — analyse.js — Page d'analyse territoriale
// ═══════════════════════════════════════════════════════════════

async function charger() {
  const [statsRes, analyseRes] = await Promise.all([
    fetch("/api/stats"),
    fetch("/api/analyse"),
  ]);
  const stats = await statsRes.json();
  const analyse = await analyseRes.json();

  afficherTotaux(stats.totaux);
  afficherTableau(analyse.par_departement);
  afficherGrapheLieux(analyse.par_departement);
  afficherGrapheEquipement(analyse.par_departement);
  afficherCompletude(analyse.completude);
}

function afficherTotaux(totaux) {
  document.getElementById("totaux-cartes").innerHTML = `
    <div class="totaux-carte"><div class="valeur">${totaux.nb_films}</div><div class="label">Films/séries publiés</div></div>
    <div class="totaux-carte"><div class="valeur">${totaux.nb_lieux}</div><div class="label">Lieux de tournage</div></div>
    <div class="totaux-carte"><div class="valeur">${totaux.nb_en_attente}</div><div class="label">En attente de validation</div></div>
  `;
}

function afficherTableau(parDepartement) {
  const tbody = document.querySelector("#table-departements tbody");
  tbody.innerHTML = parDepartement.map((d) => `
    <tr>
      <td><b>${d.departement}</b></td>
      <td>${d.nb_films}</td>
      <td>${d.nb_lieux}</td>
      <td>${d.moy_hebergement ?? "—"}</td>
      <td>${d.moy_restaurant ?? "—"}</td>
      <td>${d.lieux_sans_hebergement_5km ?? 0}</td>
      <td class="recommandation">${d.recommandation}</td>
    </tr>
  `).join("");
}

function afficherGrapheLieux(parDepartement) {
  const ctx = document.getElementById("graphe-lieux");
  new Chart(ctx, {
    type: "bar",
    data: {
      labels: parDepartement.map((d) => d.departement),
      datasets: [{
        label: "Lieux de tournage",
        data: parDepartement.map((d) => d.nb_lieux),
        backgroundColor: "#e63946",
      }],
    },
    options: {
      responsive: true,
      plugins: { legend: { display: false } },
      scales: {
        x: { ticks: { color: "#9a9ea8" }, grid: { color: "#2a2d35" } },
        y: { ticks: { color: "#9a9ea8" }, grid: { color: "#2a2d35" } },
      },
    },
  });
}

function afficherGrapheEquipement(parDepartement) {
  const ctx = document.getElementById("graphe-equipement");
  new Chart(ctx, {
    type: "bar",
    data: {
      labels: parDepartement.map((d) => d.departement),
      datasets: [
        {
          label: "Hébergements (moy.)",
          data: parDepartement.map((d) => d.moy_hebergement || 0),
          backgroundColor: "#2a9d8f",
        },
        {
          label: "Restaurants (moy.)",
          data: parDepartement.map((d) => d.moy_restaurant || 0),
          backgroundColor: "#e76f51",
        },
      ],
    },
    options: {
      responsive: true,
      plugins: { legend: { labels: { color: "#f2f2f2" } } },
      scales: {
        x: { ticks: { color: "#9a9ea8" }, grid: { color: "#2a2d35" } },
        y: { ticks: { color: "#9a9ea8" }, grid: { color: "#2a2d35" } },
      },
    },
  });
}

function afficherCompletude(c) {
  document.getElementById("completude-contenu").innerHTML = `
    <div class="totaux-carte"><div class="valeur">${c.brouillons}</div><div class="label">Films en brouillon</div></div>
    <div class="totaux-carte"><div class="valeur">${c.sans_poster}</div><div class="label">Publiés sans affiche</div></div>
    <div class="totaux-carte"><div class="valeur">${c.lieux_sans_photo}</div><div class="label">Lieux sans photo</div></div>
  `;
}

document.addEventListener("DOMContentLoaded", charger);
