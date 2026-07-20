// ═══════════════════════════════════════════════════════════════
// CinéTour — app.js
// ═══════════════════════════════════════════════════════════════
const API_BASE = ""; // même domaine ; sinon mettre l'URL du backend

const state = {
  filtreMediaType: "",
  filmSelectionne: null,
  lieuxCourants: [],
  amenitiesParLieu: {},   // cache local pendant la session : { lieuId: {amenities, phrases} }
};


let map, markersLayer;

// ── Initialisation carte Leaflet ─────────────────────────────────
function initCarte() {
  map = L.map("map", { zoomControl: true }).setView([43.9, 2.2], 8); // centré Occitanie
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    attribution: "© OpenStreetMap",
    maxZoom: 19,
  }).addTo(map);
  markersLayer = L.layerGroup().addTo(map);
}

async function chargerContourOccitanie() {
  try {
    const response = await fetch("/departements-occitanie.geojson");

    if (!response.ok) {
      throw new Error(
        `Erreur HTTP ${response.status} : impossible de charger le fichier GeoJSON`
      );
    }

    const occitanie = await response.json();

    L.geoJSON(occitanie, {
      style: {
        color: "#3388ff",
        weight: 3,
        opacity: 1,
        fillOpacity: 0
      }
    }).addTo(map);

  } catch (error) {
    console.error(
      "Erreur lors du chargement du contour de l’Occitanie :",
      error
    );
  }
}

chargerContourOccitanie();






// ── Chargement de la liste des films (sidebar) ───────────────────
async function chargerFilms() {
  document.getElementById("cartes-loading").classList.remove("hidden");
  const url = `${API_BASE}/api/films?region=Occitanie` +
              (state.filtreMediaType ? `&media_type=${state.filtreMediaType}` : "");
  try {
    const res = await fetch(url);
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
  films.forEach((film) => {
    const div = document.createElement("div");
    div.className = "carte-film";
    div.dataset.filmId = film.id;
    div.innerHTML = `
      <img src="${film.poster_url || '/placeholder-poster.png'}" alt="${film.titre}" loading="lazy">
      <div class="infos">
        <h3>${film.titre}</h3>
        <div class="meta">${labelMediaType(film.media_type)} · ${film.annee || "?"}</div>
      </div>
    `;
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

  afficherLieuxSurCarte(data.film, data.lieux);
}

function afficherLieuxSurCarte(film, lieux) {
  markersLayer.clearLayers();
  if (!lieux.length) return;

  const bounds = [];
  lieux.forEach((lieu) => {
    const icone = L.divIcon({
      html: '<div class="icone-tournage">🎬</div>',
      className: "",
      iconSize: [32, 32],
      iconAnchor: [16, 30],
    });
    const marker = L.marker([lieu.latitude, lieu.longitude], { icon: icone }).addTo(markersLayer);
    marker.on("click", () => ouvrirPopupLieu(film, lieu));
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
  document.querySelectorAll("#popup-boutons button").forEach((b) => b.classList.remove("actif"));

  document.getElementById("popup-overlay").dataset.lieuId = lieu.id;
  document.getElementById("popup-overlay").classList.remove("hidden");
}

function fermerPopup() {
  document.getElementById("popup-overlay").classList.add("hidden");
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

  // Cache session : on ne refetch pas si déjà chargé pour ce lieu
  if (!state.amenitiesParLieu[lieuId]) {
    const res = await fetch(`${API_BASE}/api/lieux/${lieuId}/amenities`);
    if (!res.ok) {
      conteneur.innerHTML = `<p style="color:#9a9ea8;">Indisponible pour ce lieu.</p>`;
      return;
    }
    state.amenitiesParLieu[lieuId] = await res.json();
  }

  const data = state.amenitiesParLieu[lieuId];
  const items = data.amenities[categorie] || [];
  const phrase = data.phrases_recommandation[categorie];

  if (!items.length) {
    conteneur.innerHTML = `<p style="color:#9a9ea8;">Aucun résultat trouvé à proximité.</p>`;
    return;
  }

  conteneur.innerHTML =
    (phrase ? `<div class="phrase-recommandation">${phrase}</div>` : "") +
    items.map((item, i) => `
      <div class="resultat-item ${i === 0 ? "plus-proche" : ""}">
        <div class="nom">${i === 0 ? "⭐ " : ""}${item.nom}</div>
        <div class="distance">${formatDistance(item.distance_metres)}</div>
        ${item.adresse ? `<div class="adresse">${item.adresse}</div>` : ""}
      </div>
    `).join("");
}

function formatDistance(m) {
  return m < 1000 ? `${m} m` : `${(m / 1000).toFixed(1)} km`;
}

// ── Écouteurs d'événements ────────────────────────────────────────
document.addEventListener("DOMContentLoaded", () => {
  initCarte();
  chargerFilms();

  document.querySelectorAll(".filtre-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".filtre-btn").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      state.filtreMediaType = btn.dataset.type;
      chargerFilms();
    });
  });

  document.getElementById("popup-fermer").addEventListener("click", fermerPopup);
  document.getElementById("popup-overlay").addEventListener("click", (e) => {
    if (e.target.id === "popup-overlay") fermerPopup();
  });

  document.querySelectorAll("#popup-boutons button").forEach((btn) => {
    btn.addEventListener("click", () => afficherCategorie(btn.dataset.categorie));
  });

  // Support lien direct depuis pelify : ?film=123
  const params = new URLSearchParams(window.location.search);
  const filmParam = params.get("film");
  if (filmParam) {
    // TODO : mapper l'id TMDB transmis par pelify vers l'id interne CinéTour
    // une fois le champ tmdb_id renseigné en base (voir schema.sql).
  }

  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/sw.js").catch(() => {});
  }
});
