// ═══════════════════════════════════════════════════════════════
// Pelify Lieux de tournage — analyse.js — Page d'analyse territoriale
// ═══════════════════════════════════════════════════════════════

async function charger() {
  const [statsRes, analyseRes, accessibiliteRes] = await Promise.all([
    fetch("/api/stats"),
    fetch("/api/analyse"),
    fetch("/api/analyse/accessibilite"),
  ]);
  const stats = await statsRes.json();
  const analyse = await analyseRes.json();
  const accessibilite = await accessibiliteRes.json();

  afficherTotaux(stats.totaux);
  afficherSyntheseComparative(analyse.synthese_comparative);
  afficherFilmsNotables(analyse.films_notables);
  afficherAccessibilite("accessibilite-francais", accessibilite.francais);
  afficherAccessibilite("accessibilite-autres", accessibilite.autres);
  afficherCartesDepartements(analyse.par_departement);
  afficherGrapheLieux(analyse.par_departement);
  afficherGrapheEquipement(analyse.par_departement);
  afficherCompletude(analyse.completude);
}

function afficherAccessibilite(idConteneur, entrees) {
  const conteneur = document.getElementById(idConteneur);
  if (!entrees || !entrees.length) {
    conteneur.innerHTML = `<p class="analyse-note">Pas encore de données d'accessibilité pour cette catégorie.</p>`;
    return;
  }

  conteneur.innerHTML = entrees.map((e) => {
    const couleurHeberg = { "bien desservi": "#00ffcc", "accessibilité modérée": "#ffd700", "isolé": "#ff6b6b", "donnée manquante": "#6b7280" }[e.hebergement.etiquette] || "#6b7280";
    const couleurResto = { "bien desservi": "#00ffcc", "accessibilité modérée": "#ffd700", "isolé": "#ff6b6b", "donnée manquante": "#6b7280" }[e.restaurant.etiquette] || "#6b7280";

    return `
      <div class="carte-accessibilite">
        <div class="entete-accessibilite">
          <img src="${e.poster_url || '/placeholder-poster.png'}" alt="${e.titre}">
          <div>
            <div class="titre-accessibilite">${e.titre}</div>
            <div class="meta-accessibilite">${e.annee || "?"} · ${e.nombre_lieux} lieu${e.nombre_lieux > 1 ? "x" : ""}</div>
          </div>
        </div>
        <div class="indicateur-accessibilite" style="border-color:${couleurHeberg}">
          <b>🏨 Hébergement le plus proche :</b>
          ${e.hebergement.nom ? `${e.hebergement.nom} — ${e.hebergement.duree_minutes} min en voiture (${(e.hebergement.distance_metres / 1000).toFixed(1)} km), ${e.hebergement.nombre_total_rayon} au total dans le rayon` : "Donnée non disponible"}
          <span class="etiquette-accessibilite" style="color:${couleurHeberg}">${e.hebergement.etiquette}</span>
          <p class="action-accessibilite">→ ${e.hebergement.action}</p>
        </div>
        <div class="indicateur-accessibilite" style="border-color:${couleurResto}">
          <b>🍽️ Restaurant le plus proche :</b>
          ${e.restaurant.nom ? `${e.restaurant.nom} — ${e.restaurant.duree_minutes} min en voiture (${(e.restaurant.distance_metres / 1000).toFixed(1)} km), ${e.restaurant.nombre_total_rayon} au total dans le rayon` : "Donnée non disponible"}
          <span class="etiquette-accessibilite" style="color:${couleurResto}">${e.restaurant.etiquette}</span>
          <p class="action-accessibilite">→ ${e.restaurant.action}</p>
        </div>
        <div class="stats-secondaires">ℹ️ ${e.office_tourisme_total} offices de tourisme · 🅿️ ${e.parking_total} parkings dans le rayon</div>
      </div>
    `;
  }).join("");
}

function afficherTotaux(totaux) {
  document.getElementById("totaux-cartes").innerHTML = `
    <div class="totaux-carte"><div class="valeur">${totaux.nb_films}</div><div class="label">Films/séries publiés</div></div>
    <div class="totaux-carte"><div class="valeur">${totaux.nb_lieux}</div><div class="label">Lieux de tournage</div></div>
    <div class="totaux-carte"><div class="valeur">${totaux.nb_en_attente}</div><div class="label">En attente de validation</div></div>
  `;
}

function afficherSyntheseComparative(texte) {
  const conteneur = document.getElementById("synthese-comparative");
  if (!texte) { conteneur.innerHTML = ""; return; }
  conteneur.innerHTML = `
    <p class="synthese-titre">📊 Ce que montrent les chiffres</p>
    <p class="synthese-texte">${texte}</p>
  `;
}

function afficherFilmsNotables(films) {
  const conteneur = document.getElementById("films-notables-cartes");
  if (!films || !films.length) { conteneur.innerHTML = "<p class=\"analyse-note\">Pas encore assez de données de popularité.</p>"; return; }

  conteneur.innerHTML = films.map((f, i) => `
    <div class="carte-film-notable">
      <div class="rang-notable">#${i + 1}</div>
      <img src="${f.poster_url || '/placeholder-poster.png'}" alt="${f.titre}" loading="lazy">
      <div class="infos-notable">
        <div class="titre-notable">${f.titre}</div>
        <div class="meta-notable">${f.annee || "?"} · ${f.media_type === "movie" ? "Film" : f.media_type === "tv" ? "Série" : "Animé"}</div>
      </div>
    </div>
  `).join("");
}

function afficherCartesDepartements(parDepartement) {
  const conteneur = document.getElementById("cartes-departements");
  conteneur.innerHTML = parDepartement.map((d) => `
    <div class="carte-departement">
      <div class="entete-departement">
        <h3>${d.departement}</h3>
        <span class="part-departement">${d.part_pourcentage}% du total régional</span>
      </div>
      <div class="chiffres-departement">
        <div><span class="chiffre">${d.nb_films}</span><span class="libelle-chiffre">films/séries</span></div>
        <div><span class="chiffre">${d.nb_lieux}</span><span class="libelle-chiffre">lieux</span></div>
        <div><span class="chiffre">${d.moy_hebergement ?? "—"}</span><span class="libelle-chiffre">hébergements (moy.)</span></div>
        <div><span class="chiffre">${d.moy_restaurant ?? "—"}</span><span class="libelle-chiffre">restaurants (moy.)</span></div>
        <div><span class="chiffre">${d.lieux_sans_hebergement_5km ?? 0}</span><span class="libelle-chiffre">lieux isolés</span></div>
      </div>
      <p class="interpretation-departement">${d.recommandation}</p>
    </div>
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
        backgroundColor: "#ff007f",
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

  const premier = parDepartement[0];
  const dernier = parDepartement[parDepartement.length - 1];
  document.getElementById("interpretation-lieux").textContent = premier && dernier
    ? `Lecture objective : ${premier.departement} arrive en tête avec ${premier.nb_lieux} lieux recensés (${premier.part_pourcentage}% du total), ${dernier.departement} ferme la marche avec ${dernier.nb_lieux} (${dernier.part_pourcentage}%). Cet écart reflète la donnée actuellement en base, pas nécessairement le potentiel réel de chaque territoire — certains départements sont probablement sous-représentés faute de données Wikidata complètes.`
    : "";
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
          backgroundColor: "#00ffcc",
        },
        {
          label: "Restaurants (moy.)",
          data: parDepartement.map((d) => d.moy_restaurant || 0),
          backgroundColor: "#ffd700",
        },
      ],
    },
    options: {
      responsive: true,
      plugins: { legend: { labels: { color: "#dde4f0" } } },
      scales: {
        x: { ticks: { color: "#9a9ea8" }, grid: { color: "#2a2d35" } },
        y: { ticks: { color: "#9a9ea8" }, grid: { color: "#2a2d35" } },
      },
    },
  });

  const mieuxEquipe = [...parDepartement].filter((d) => d.moy_hebergement != null)
    .sort((a, b) => b.moy_hebergement - a.moy_hebergement)[0];
  const moinsEquipe = [...parDepartement].filter((d) => d.moy_hebergement != null)
    .sort((a, b) => a.moy_hebergement - b.moy_hebergement)[0];
  document.getElementById("interpretation-equipement").textContent = mieuxEquipe && moinsEquipe
    ? `Lecture objective : ${mieuxEquipe.departement} offre le plus d'hébergements à proximité de ses lieux de tournage (${mieuxEquipe.moy_hebergement} en moyenne), contre ${moinsEquipe.moy_hebergement} pour ${moinsEquipe.departement} — un département avec peu de lieux mais bien équipés (comme des zones urbaines) peut être plus facile à valoriser rapidement qu'un département avec beaucoup de lieux mal desservis.`
    : "";
}

function afficherCompletude(c) {
  document.getElementById("completude-contenu").innerHTML = `
    <div class="totaux-carte"><div class="valeur">${c.brouillons}</div><div class="label">Films en brouillon</div></div>
    <div class="totaux-carte"><div class="valeur">${c.sans_poster}</div><div class="label">Publiés sans affiche</div></div>
    <div class="totaux-carte"><div class="valeur">${c.lieux_sans_photo}</div><div class="label">Lieux sans photo</div></div>
  `;
}

document.addEventListener("DOMContentLoaded", charger);
