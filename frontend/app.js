// ═══════════════════════════════════════════════════════════════
// CinéTour — app.js (V2 : filtres, stats, clustering, popup enrichi)
// ═══════════════════════════════════════════════════════════════
const API_BASE = "";

// Doit rester synchronisé avec ICONES_CATEGORIE dans backend/overpass.py
const ICONES_CATEGORIE = {
  hebergement:      { emoji: "🏨", couleur: "#2a9d8f", label: "Où dormir" },
  restaurant:       { emoji: "🍽️", couleur: "#e76f51", label: "Où manger" },
  office_tourisme:  { emoji: "ℹ️", couleur: "#264653", label: "Office de tourisme" },
  parking:          { emoji: "🅿️", couleur: "#3a3a3a", label: "Se garer" },
  gare:             { emoji: "🚉", couleur: "#6a4c93", label: "Gare la plus proche" },
  aeroport:         { emoji: "✈️", couleur: "#4361ee", label: "Aéroport le plus proche" },
  arret_bus:        { emoji: "🚌", couleur: "#f4a261", label: "Arrêt de bus" },
  police:           { emoji: "🚓", couleur: "#023e8a", label: "Police / gendarmerie" },
  hopital:          { emoji: "🏥", couleur: "#d00000", label: "Hôpital" },
  activite:         { emoji: "🎡", couleur: "#9b5de5", label: "Activités à proximité" },
};

const state = {
  filtres: { mediaType: "", annee: "", departement: "", commune: "", q: "" },
  filmSelectionne: null,
  lieuxCourants: [],
  amenitiesParLieu: {},
  traceLayer: null,
  debounceRecherche: null,
};

let map, clusterGroup;

// ── Initialisation carte Leaflet + clustering ────────────────────
function initCarte() {
  map = L.map("map", { zoomControl: true }).setView([43.9, 2.2], 8);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    attribution: "© OpenStreetMap",
    maxZoom: 19,
  }).addTo(map);
  clusterGroup = L.markerClusterGroup({
    maxClusterRadius: 40,
    // Au-delà du zoom 15 (rue/quartier), plus de clustering du tout —
    // à ce niveau de zoom, l'utilisateur veut cliquer un lieu précis,
    // pas encore zoomer sur une bulle de regroupement.
    disableClusteringAtZoom: 15,
  });
  map.addLayer(clusterGroup);
}

// ── Filtres avancés : peupler les menus déroulants depuis l'API ──
async function chargerOptionsFiltres() {
  try {
    const res = await fetch(`${API_BASE}/api/filtres`);
    const data = await res.json();
    remplirSelect("filtre-annee", data.annees, "Année");
    remplirSelect("filtre-departement", data.departements, "Département");
    remplirSelect("filtre-commune", data.communes, "Commune");
  } catch (e) { /* champs restent vides, pas bloquant */ }
}

function remplirSelect(id, valeurs, labelDefaut) {
  const select = document.getElementById(id);
  const valeurCourante = select.value;
  select.innerHTML = `<option value="">${labelDefaut}</option>` +
    valeurs.map((v) => `<option value="${v}">${v}</option>`).join("");
  select.value = valeurCourante;
}

// ── Panneau statistiques ──────────────────────────────────────────
let statsChargees = false;
async function toggleStats() {
  const panel = document.getElementById("stats-panel");
  const btn = document.getElementById("stats-toggle");
  const ouvert = panel.classList.contains("hidden");
  panel.classList.toggle("hidden");
  btn.setAttribute("aria-expanded", String(ouvert));
  if (ouvert && !statsChargees) {
    await chargerStats();
    statsChargees = true;
  }
}

async function chargerStats() {
  const conteneur = document.getElementById("stats-contenu");
  conteneur.innerHTML = "Chargement…";
  try {
    const res = await fetch(`${API_BASE}/api/stats`);
    const data = await res.json();
    const t = data.totaux;
    let html = `
      <div class="stats-ligne"><span>Films/séries publiés</span><b>${t.nb_films}</b></div>
      <div class="stats-ligne"><span>Lieux de tournage</span><b>${t.nb_lieux}</b></div>
      <div class="stats-ligne"><span>En attente de validation</span><b>${t.nb_en_attente}</b></div>
      <div class="stats-ligne-titre">Par département</div>
    `;
    data.par_departement.forEach((d) => {
      html += `<div class="stats-ligne"><span>${d.departement}</span><b>${d.nb_lieux} lieux · ${d.nb_films} œuvres</b></div>`;
    });
    html += `<div class="stats-ligne-titre">Par type</div>`;
    data.par_media_type.forEach((m) => {
      html += `<div class="stats-ligne"><span>${labelMediaType(m.media_type)}</span><b>${m.nb}</b></div>`;
    });
    conteneur.innerHTML = html;
  } catch (e) {
    conteneur.innerHTML = "Statistiques indisponibles.";
  }
}

// ── Chargement de la liste des films (sidebar), avec filtres ─────
async function chargerFilms() {
  document.getElementById("cartes-loading").classList.remove("hidden");
  document.getElementById("cartes-vide").classList.add("hidden");

  const params = new URLSearchParams({ region: "Occitanie" });
  if (state.filtres.mediaType) params.set("media_type", state.filtres.mediaType);
  if (state.filtres.annee) params.set("annee", state.filtres.annee);
  if (state.filtres.departement) params.set("departement", state.filtres.departement);
  if (state.filtres.commune) params.set("commune", state.filtres.commune);
  if (state.filtres.q) params.set("q", state.filtres.q);

  try {
    const res = await fetch(`${API_BASE}/api/films?${params}`);
    const data = await res.json();
    afficherCartesFilms(data.films);
  } catch (e) {
    document.getElementById("cartes-liste").innerHTML =
      `<p style="color:#9a9ea8;padding:10px;">Erreur de chargement.</p>`;
  } finally {
    document.getElementById("cartes-loading").classList.add("hidden");
  }
}

function afficherCartesFilms(films) {
  const conteneur = document.getElementById("cartes-liste");
  conteneur.innerHTML = "";
  document.getElementById("cartes-vide").classList.toggle("hidden", films.length > 0);

  films.forEach((film) => {
    const div = document.createElement("div");
    div.className = "carte-film";
    div.dataset.filmId = film.id;
    div.innerHTML = `
      <img src="${film.poster_url || '/placeholder-poster.png'}" alt="${film.titre}" loading="lazy">
      <div class="infos">
        <h3>${film.titre}</h3>
        <div class="meta">${labelMediaType(film.media_type)} · ${film.annee || "?"}</div>
        <div class="badge-lieux">📍 ${film.nb_lieux} lieu${film.nb_lieux > 1 ? "x" : ""} de tournage</div>
      </div>
      <button class="btn-voir-carte">Voir sur la carte</button>
    `;
    div.querySelector(".btn-voir-carte").addEventListener("click", (e) => {
      e.stopPropagation();
      selectionnerFilm(film.id, div);
      document.getElementById("carte-zone").scrollIntoView({ behavior: "smooth" });
    });
    div.addEventListener("click", () => selectionnerFilm(film.id, div));
    conteneur.appendChild(div);
  });
}

function labelMediaType(type) {
  return { movie: "Film", tv: "Série", anime: "Animé" }[type] || type;
}

// ── Sélection d'un film → charge ses lieux et les affiche sur la carte ──
async function selectionnerFilm(filmId, elementCarte) {
  document.querySelectorAll(".carte-film").forEach((el) => el.classList.remove("selectionnee"));
  if (elementCarte) elementCarte.classList.add("selectionnee");

  const res = await fetch(`${API_BASE}/api/films/${filmId}`);
  if (!res.ok) return;
  const data = await res.json();

  state.filmSelectionne = data.film;
  state.lieuxCourants = data.lieux;
  state.plateformesCourantes = Array.isArray(data.plateformes) ? data.plateformes : [];

  effacerTrace();
  afficherLieuxSurCarte(data.film, data.lieux);
}

function afficherLieuxSurCarte(film, lieux) {
  clusterGroup.clearLayers();
  if (!lieux.length) return;

  const bounds = [];
  lieux.forEach((lieu) => {
    const icone = L.divIcon({
      html: '<div class="icone-tournage">🎬</div>',
      className: "",
      iconSize: [32, 32],
      iconAnchor: [16, 30],
    });
    const marker = L.marker([lieu.latitude, lieu.longitude], { icon: icone });
    marker.on("click", () => ouvrirPopupLieu(film, lieu));
    clusterGroup.addLayer(marker);
    bounds.push([lieu.latitude, lieu.longitude]);
  });

  map.fitBounds(bounds, { padding: [40, 40], maxZoom: 14 });
}

// ── Popup lieu de tournage ────────────────────────────────────────
function ouvrirPopupLieu(film, lieu) {
  document.getElementById("popup-poster").src = film.poster_url || "/placeholder-poster.png";
  document.getElementById("popup-titre").textContent = `${film.titre} — ${lieu.nom}`;
  document.getElementById("popup-synopsis").textContent =
    lieu.description || film.synopsis || "Aucune description disponible.";
  document.getElementById("popup-resultats").innerHTML = "";
  document.getElementById("popup-overlay").dataset.lieuId = lieu.id;
  document.getElementById("popup-overlay").dataset.filmId = film.id;

  // Photo actuelle du lieu (Wikidata P18), si disponible
  const conteneurPhoto = document.getElementById("popup-photos-lieu");
  conteneurPhoto.innerHTML = lieu.photo_url
    ? `<img src="${lieu.photo_url}" alt="Photo actuelle de ${lieu.nom}" loading="lazy">`
    : "";

  // Plateformes de streaming
  const conteneurPlateformes = document.getElementById("popup-plateformes");
  conteneurPlateformes.innerHTML = (state.plateformesCourantes || []).map((p) => `
    <a class="plateforme-logo" href="${p.lien_affilie || '#'}" target="_blank" rel="noopener sponsored">
      <img src="${p.logo_url}" alt="${p.nom}"> ${p.nom}
    </a>
  `).join("");

  // Boutons commodités, générés dynamiquement (icône + couleur par catégorie)
  const conteneurBoutons = document.getElementById("popup-boutons");
  conteneurBoutons.innerHTML = Object.entries(ICONES_CATEGORIE)
    .filter(([cle]) => cle !== "activite") // activité a son propre bouton dédié plus bas
    .map(([cle, info]) => `
      <button data-categorie="${cle}" style="border-color:${info.couleur}">
        <span class="icone-btn">${info.emoji}</span> ${info.label}
      </button>
    `).join("");
  conteneurBoutons.querySelectorAll("button").forEach((btn) => {
    btn.addEventListener("click", () => afficherCategorie(btn.dataset.categorie));
  });

  document.getElementById("popup-overlay").classList.remove("hidden");
}

function fermerPopup() {
  document.getElementById("popup-overlay").classList.add("hidden");
}

async function _recupererAmenities(lieuId) {
  if (!state.amenitiesParLieu[lieuId]) {
    const res = await fetch(`${API_BASE}/api/lieux/${lieuId}/amenities`);
    if (!res.ok) return null;
    state.amenitiesParLieu[lieuId] = await res.json();
  }
  return state.amenitiesParLieu[lieuId];
}

// ── Clic sur un bouton catégorie (hébergement, resto, etc.) ──────
async function afficherCategorie(categorie) {
  const lieuId = document.getElementById("popup-overlay").dataset.lieuId;
  if (!lieuId) return;

  document.querySelectorAll("#popup-boutons button").forEach((b) => {
    b.classList.toggle("actif", b.dataset.categorie === categorie);
  });

  const conteneur = document.getElementById("popup-resultats");
  conteneur.innerHTML = `<p style="color:#9a9ea8;">Chargement…</p>`;

  const data = await _recupererAmenities(lieuId);
  if (!data) {
    conteneur.innerHTML = `<p style="color:#9a9ea8;">Indisponible pour ce lieu.</p>`;
    return;
  }

  const items = data.amenities[categorie] || [];
  const phrase = data.phrases_recommandation[categorie];
  const couleur = (ICONES_CATEGORIE[categorie] || {}).couleur || "#e63946";

  if (!items.length) {
    conteneur.innerHTML = `<p style="color:#9a9ea8;">Aucun résultat trouvé à proximité.</p>`;
    return;
  }

  conteneur.innerHTML =
    (phrase ? `<div class="phrase-recommandation">${phrase}</div>` : "") +
    items.map((item, i) => `
      <div class="resultat-item ${i === 0 ? "plus-proche" : ""}"
           style="${i === 0 ? `border-color:${couleur};` : ""}">
        <div class="nom">${i === 0 ? "⭐ " : ""}${item.nom}</div>
        <div class="distance">${formatDistance(item.distance_metres)}</div>
        ${item.adresse ? `<div class="adresse">${item.adresse}</div>` : ""}
      </div>
    `).join("");
}

// ── "Que faire aux alentours" : affiche les points d'activité sur la carte ──
async function afficherActivites() {
  const lieuId = document.getElementById("popup-overlay").dataset.lieuId;
  if (!lieuId) return;
  const lieu = state.lieuxCourants.find((l) => l.id === Number(lieuId));
  const data = await _recupererAmenities(lieuId);
  if (!data) return;

  const items = data.amenities["activite"] || [];
  fermerPopup();

  clusterGroup.clearLayers();
  const bounds = [];
  if (lieu) bounds.push([lieu.latitude, lieu.longitude]);
  items.forEach((item) => {
    const icone = L.divIcon({
      html: `<div class="icone-tournage" style="color:${ICONES_CATEGORIE.activite.couleur}">🎡</div>`,
      className: "", iconSize: [28, 28], iconAnchor: [14, 26],
    });
    const marker = L.marker([item.latitude, item.longitude], { icon: icone })
      .bindPopup(`<b>${item.nom}</b><br>${formatDistance(item.distance_metres)} du lieu de tournage`);
    clusterGroup.addLayer(marker);
    bounds.push([item.latitude, item.longitude]);
  });
  if (bounds.length) map.fitBounds(bounds, { padding: [40, 40] });
}

// ── "Sur les traces de {film}" : itinéraire réel entre tous les lieux ──
function effacerTrace() {
  if (state.traceLayer) {
    map.removeLayer(state.traceLayer);
    state.traceLayer = null;
  }
}

async function afficherTraceFilm() {
  const filmId = document.getElementById("popup-overlay").dataset.filmId;
  if (!filmId) return;

  fermerPopup();
  effacerTrace();

  try {
    const res = await fetch(`${API_BASE}/api/films/${filmId}/trace`);
    if (!res.ok) {
      alert("Tracé impossible pour ce film (un seul lieu recensé, ou erreur serveur).");
      return;
    }
    const data = await res.json();

    state.traceLayer = L.geoJSON(data.geometry, {
      style: { color: "#e63946", weight: 4, opacity: 0.8 },
    }).addTo(map);

    const distanceKm = (data.distance_metres / 1000).toFixed(1);
    const typeTexte = data.type === "route_reelle"
      ? `Itinéraire routier réel : ${distanceKm} km`
      : `Estimation à vol d'oiseau : ${distanceKm} km (itinéraire routier indisponible)`;

    L.popup()
      .setLatLng(state.traceLayer.getBounds().getCenter())
      .setContent(`<b>${typeTexte}</b>`)
      .openOn(map);

    map.fitBounds(state.traceLayer.getBounds(), { padding: [40, 40] });
  } catch (e) {
    alert("Erreur lors du calcul du tracé.");
  }
}

function formatDistance(m) {
  return m < 1000 ? `${m} m` : `${(m / 1000).toFixed(1)} km`;
}

// ── Écouteurs d'événements ────────────────────────────────────────
document.addEventListener("DOMContentLoaded", () => {
  initCarte();
  chargerOptionsFiltres();
  chargerFilms();

  document.querySelectorAll(".filtre-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".filtre-btn").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      state.filtres.mediaType = btn.dataset.type;
      chargerFilms();
    });
  });

  ["filtre-annee", "filtre-departement", "filtre-commune"].forEach((id) => {
    document.getElementById(id).addEventListener("change", (e) => {
      const cle = { "filtre-annee": "annee", "filtre-departement": "departement", "filtre-commune": "commune" }[id];
      state.filtres[cle] = e.target.value;
      chargerFilms();
    });
  });

  document.getElementById("recherche-input").addEventListener("input", (e) => {
    clearTimeout(state.debounceRecherche);
    state.debounceRecherche = setTimeout(() => {
      state.filtres.q = e.target.value.trim();
      chargerFilms();
    }, 350); // évite un appel API à chaque frappe
  });

  document.getElementById("stats-toggle").addEventListener("click", toggleStats);

  document.getElementById("popup-fermer").addEventListener("click", fermerPopup);
  document.getElementById("popup-overlay").addEventListener("click", (e) => {
    if (e.target.id === "popup-overlay") fermerPopup();
  });

  document.getElementById("btn-activites").addEventListener("click", afficherActivites);
  document.getElementById("btn-trace").addEventListener("click", afficherTraceFilm);

  const params = new URLSearchParams(window.location.search);
  const filmParam = params.get("film");
  if (filmParam) selectionnerFilm(Number(filmParam));

  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/sw.js").catch(() => {});
  }
});