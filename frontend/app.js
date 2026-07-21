// ═══════════════════════════════════════════════════════════════
// CinéTour — app.js (V2 : filtres, stats, clustering, popup enrichi)
// ═══════════════════════════════════════════════════════════════
const API_BASE = "";

// Doit rester synchronisé avec ICONES_CATEGORIE dans backend/overpass.py
const ICONES_CATEGORIE = {
  hebergement: {
    emoji: "🏨",
    couleur: "#2a9d8f",
    label: "Où dormir"
  },

  refuge: {
    emoji: "🥾",
    couleur: "#588157",
    label: "Refuges"
  },

  restaurant: {
    emoji: "🍽️",
    couleur: "#e76f51",
    label: "Où manger"
  },

  office_tourisme: {
    emoji: "ℹ️",
    couleur: "#264653",
    label: "Office de tourisme"
  },

  parking: {
    emoji: "🅿️",
    couleur: "#3a3a3a",
    label: "Se garer"
  },

  gare: {
    emoji: "🚉",
    couleur: "#6a4c93",
    label: "Gare la plus proche"
  },

  aeroport: {
    emoji: "✈️",
    couleur: "#4361ee",
    label: "Aéroport le plus proche"
  },

  arret_bus: {
    emoji: "🚌",
    couleur: "#f4a261",
    label: "Arrêt de bus"
  },

  police: {
    emoji: "🚓",
    couleur: "#023e8a",
    label: "Police / gendarmerie"
  },

  hopital: {
    emoji: "🏥",
    couleur: "#d00000",
    label: "Hôpital"
  },

  activite: {
    emoji: "🎡",
    couleur: "#9b5de5",
    label: "Activités à proximité"
  }
};

const state = {
  filtres: { mediaType: "", annee: "", departement: "", commune: "", q: "", tri: "titre" },
  filmSelectionne: null,
  lieuxCourants: [],
  amenitiesParLieu: {},
  traceLayer: null,
  debounceRecherche: null,
  dernierBounds: null,
};

let map, clusterGroup, clusterActivites;

// ── Initialisation carte Leaflet + clustering ────────────────────
function initCarte() {
  map = L.map("map", { zoomControl: false }).setView([43.9, 2.2], 7);
  // Le zoom par défaut est en haut-gauche, comme notre barre de
  // filtres — on le déplace à droite pour ne plus se chevaucher.
  L.control.zoom({ position: "topright" }).addTo(map);
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
  // Groupe SÉPARÉ pour les activités "que faire aux alentours" : ne
  // doit jamais se mélanger dans la même bulle que les lieux de
  // tournage, sinon cliquer sur un lieu de tournage peut ouvrir un
  // cluster d'activités par erreur.
  clusterActivites = L.markerClusterGroup({ maxClusterRadius: 40, disableClusteringAtZoom: 15 });

  map.addLayer(clusterGroup);
  map.addLayer(clusterActivites);

  // N'importe quel clic sur une bulle de regroupement change la vue —
  // propose de revenir à la vue initiale du film sélectionné.
  clusterGroup.on("clusterclick", afficherBoutonRecentrer);
  clusterActivites.on("clusterclick", afficherBoutonRecentrer);
}

function afficherBoutonRecentrer() {
  if (state.dernierBounds) document.getElementById("btn-recentrer").classList.remove("hidden");
}

function recentrerCarte() {
  if (state.dernierBounds) map.fitBounds(state.dernierBounds, { padding: [40, 40], maxZoom: 14 });
  document.getElementById("btn-recentrer").classList.add("hidden");
}

async function chargerContourOccitanie() {
  try {
    const response = await fetch("/contour-occitanie.geojson");

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
  if (state.filtres.tri) params.set("tri", state.filtres.tri);

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
  clusterActivites.clearLayers();
  document.getElementById("btn-recentrer").classList.add("hidden");
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

  state.dernierBounds = bounds;
  map.fitBounds(bounds, { padding: [40, 40], maxZoom: 14 });
}

// ── Popup lieu de tournage ────────────────────────────────────────
function ouvrirPopupLieu(film, lieu) {
  document.getElementById("popup-poster").src = film.poster_url || "/placeholder-poster.png";
  document.getElementById("popup-titre").textContent = film.titre;
  document.getElementById("popup-meta").textContent =
    `${labelMediaType(film.media_type)} · ${film.annee || "année inconnue"}`;
  document.getElementById("popup-adresse").textContent =
    [lieu.nom, lieu.commune, lieu.departement].filter(Boolean).join(", ");
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

  // Boutons commodités, générés dynamiquement (icône + couleur par
  // catégorie) — "activite" suit exactement les mêmes règles que les
  // autres (liste, plus proche en évidence, total dans le rayon) ; en
  // plus de la liste, elle affiche aussi les points sur la carte.
  const conteneurBoutons = document.getElementById("popup-boutons");
  conteneurBoutons.innerHTML = Object.entries(ICONES_CATEGORIE)
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
  const popupOverlay = document.getElementById("popup-overlay");
  const lieuId = popupOverlay.dataset.lieuId;

  if (!lieuId) {
    return;
  }

  document.querySelectorAll("#popup-boutons button").forEach((bouton) => {
    bouton.classList.toggle(
      "actif",
      bouton.dataset.categorie === categorie
    );
  });

  const conteneur = document.getElementById("popup-resultats");

  conteneur.innerHTML = `
    <p style="color:#9a9ea8;">
      Chargement…
    </p>
  `;

  const data = await _recupererAmenities(lieuId);

  if (!data) {
    conteneur.innerHTML = `
      <p style="color:#9a9ea8;">
        Données indisponibles pour ce lieu.
      </p>
    `;
    return;
  }

  const items = data.amenities?.[categorie] || [];
  const stats = data.stats?.[categorie] || null;
  const phrase = data.phrases_recommandation?.[categorie];

  const informationsCategorie =
    ICONES_CATEGORIE[categorie] || {};

  const couleur =
    informationsCategorie.couleur || "#e63946";

  const resume = creerResumeRecherche(
    stats,
    items.length
  );

  if (!items.length) {
    conteneur.innerHTML =
      resume +
      `
        <p style="color:#9a9ea8;">
          Aucun résultat nommé trouvé à proximité.
        </p>
      `;

    return;
  }

  const liste = items.map((item, index) => {
    const estPlusProche = index === 0;

    return `
      <div
        class="resultat-item ${estPlusProche ? "plus-proche" : ""}"
        style="${estPlusProche ? `border-color:${couleur};` : ""}"
      >
        <div class="nom">
          ${estPlusProche ? "⭐ " : ""}
          ${item.nom}
        </div>

        <div class="distance">
          ${formatDistance(item.distance_metres)}
        </div>

        ${
          item.adresse
            ? `<div class="adresse">${item.adresse}</div>`
            : ""
        }

        ${
          item.telephone
            ? `
              <div class="telephone">
                📞 ${item.telephone}
              </div>
            `
            : ""
        }

        ${
          item.site_web
            ? `
              <div class="site-web">
                <a
                  href="${item.site_web}"
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  Voir le site
                </a>
              </div>
            `
            : ""
        }
      </div>
    `;
  }).join("");

  conteneur.innerHTML =
    resume +
    (
      phrase
        ? `<div class="phrase-recommandation">${phrase}</div>`
        : ""
    ) +
    liste;

  if (categorie === "activite") {
    afficherActivitesSurCarte(items);
  }
}

// ── Affiche les points "activité" sur la carte (calque séparé des
// lieux de tournage), avec l'icône propre à la catégorie et un clic
// qui montre les infos du point, comme pour les autres commodités ──
function afficherActivitesSurCarte(items) {
  clusterActivites.clearLayers();
  const lieuActuel = state.lieuxCourants.find(
    (l) => l.id === Number(document.getElementById("popup-overlay").dataset.lieuId)
  );

  const bounds = lieuActuel ? [[lieuActuel.latitude, lieuActuel.longitude]] : [];

  items.forEach((item) => {
    const icone = L.divIcon({
      html: `<div class="icone-commodite" style="color:${ICONES_CATEGORIE.activite.couleur}">🎡</div>`,
      className: "", iconSize: [26, 26], iconAnchor: [13, 24],
    });
    const marker = L.marker([item.latitude, item.longitude], { icon: icone }).bindPopup(`
      <b>${item.nom}</b><br>
      ${formatDistance(item.distance_metres)} du lieu de tournage
      ${item.adresse ? `<br>${item.adresse}` : ""}
      ${item.telephone ? `<br>📞 ${item.telephone}` : ""}
      ${item.site_web ? `<br><a href="${item.site_web}" target="_blank" rel="noopener noreferrer">Voir le site</a>` : ""}
    `);
    clusterActivites.addLayer(marker);
    bounds.push([item.latitude, item.longitude]);
  });

  if (bounds.length) map.fitBounds(bounds, { padding: [40, 40], maxZoom: 14 });
  if (state.dernierBounds) document.getElementById("btn-recentrer").classList.remove("hidden");
}

function creerResumeRecherche(stats, nombreAffiche) {
  if (!stats) {
    return "";
  }

  const total = Number(stats.nombre_total || 0);
  const rayon = formatDistance(stats.rayon_metres || 0);
  const reellementAffiches = Math.min(nombreAffiche, total);

  return `
    <div class="resume-recherche">
      <strong>${reellementAffiches}</strong>
      lieu${reellementAffiches > 1 ? "x" : ""} affiché${reellementAffiches > 1 ? "s" : ""}
      sur
      <strong>${total}</strong>
      trouvé${total > 1 ? "s" : ""}
      dans un rayon de
      <strong>${rayon}</strong>.
    </div>
  `;
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

// ── Choroplèthe départements + carte de chaleur ──────────────────
let coucheChoroplethe = null;
let coucheChaleur = null;
let analyseCache = null;

function _echelleCouleur(valeur, max) {
  // 5 paliers, du plus clair (peu de lieux) au plus intense (le plus sollicité)
  const ratio = max > 0 ? valeur / max : 0;
  if (ratio > 0.8) return "#7f1d1d";
  if (ratio > 0.6) return "#c1272d";
  if (ratio > 0.4) return "#e63946";
  if (ratio > 0.2) return "#f4a3a8";
  return "#3a3d45";
}

async function _recupererAnalyse() {
  if (!analyseCache) {
    const res = await fetch(`${API_BASE}/api/analyse`);
    analyseCache = await res.json();
  }
  return analyseCache;
}

async function toggleChoroplethe() {
  const btn = document.getElementById("btn-choroplethe");
  if (coucheChoroplethe) {
    map.removeLayer(coucheChoroplethe);
    coucheChoroplethe = null;
    btn.dataset.actif = "false";
    return;
  }

  const [geojsonRes, analyse] = await Promise.all([
    fetch("/departements-occitanie.geojson").then((r) => r.json()),
    _recupererAnalyse(),
  ]);

  const statsParDept = {};
  analyse.par_departement.forEach((d) => { statsParDept[d.departement] = d; });
  const maxLieux = Math.max(...analyse.par_departement.map((d) => d.nb_lieux), 1);

  coucheChoroplethe = L.geoJSON(geojsonRes, {
    style: (feature) => {
      const nom = feature.properties.dep_name?.[0];
      const stat = statsParDept[nom];
      return {
        color: "#0f1115",
        weight: 1,
        fillColor: _echelleCouleur(stat ? stat.nb_lieux : 0, maxLieux),
        fillOpacity: 0.55,
      };
    },
    onEachFeature: (feature, layer) => {
      const nom = feature.properties.dep_name?.[0];
      const stat = statsParDept[nom];
      layer.on("click", () => {
        const contenu = stat ? `
          <div class="popup-departement">
            <h3>${nom}</h3>
            <div class="ligne"><span>Films/séries</span><b>${stat.nb_films}</b></div>
            <div class="ligne"><span>Lieux de tournage</span><b>${stat.nb_lieux}</b></div>
            <div class="ligne"><span>Hébergements (moy.)</span><b>${stat.moy_hebergement ?? "—"}</b></div>
            <div class="ligne"><span>Restaurants (moy.)</span><b>${stat.moy_restaurant ?? "—"}</b></div>
            <div class="ligne"><span>Lieux isolés</span><b>${stat.lieux_sans_hebergement_5km ?? 0}</b></div>
            <div class="recommandation">${stat.recommandation}</div>
          </div>
        ` : `<div class="popup-departement"><h3>${nom}</h3>Aucune donnée pour ce département.</div>`;
        L.popup().setLatLng(layer.getBounds().getCenter()).setContent(contenu).openOn(map);
      });
    },
  }).addTo(map);

  document.getElementById("btn-choroplethe").dataset.actif = "true";
}

async function toggleChaleur() {
  const btn = document.getElementById("btn-chaleur");
  if (coucheChaleur) {
    map.removeLayer(coucheChaleur);
    coucheChaleur = null;
    btn.dataset.actif = "false";
    return;
  }

  const res = await fetch(`${API_BASE}/api/lieux/tous-points`);
  const data = await res.json();
  coucheChaleur = L.heatLayer(data.points, { radius: 22, blur: 18, maxZoom: 10 }).addTo(map);
  btn.dataset.actif = "true";
}

// ── Écouteurs d'événements ────────────────────────────────────────
document.addEventListener("DOMContentLoaded", () => {
  initCarte();
  chargerContourOccitanie();
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

  document.getElementById("popup-fermer").addEventListener("click", fermerPopup);
  document.getElementById("popup-overlay").addEventListener("click", (e) => {
    if (e.target.id === "popup-overlay") fermerPopup();
  });

  document.getElementById("btn-trace").addEventListener("click", afficherTraceFilm);
  document.getElementById("btn-recentrer").addEventListener("click", recentrerCarte);
  document.getElementById("btn-choroplethe").addEventListener("click", toggleChoroplethe);
  document.getElementById("btn-chaleur").addEventListener("click", toggleChaleur);

  document.getElementById("filtre-notoriete").addEventListener("click", (e) => {
    const actif = e.target.classList.toggle("active");
    state.filtres.tri = actif ? "popularite" : "titre";
    chargerFilms();
  });

  const params = new URLSearchParams(window.location.search);
  const filmParam = params.get("film");
  if (filmParam) selectionnerFilm(Number(filmParam));

  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/sw.js").catch(() => {});
  }
});