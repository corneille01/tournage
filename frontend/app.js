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

  distributeur: {
    emoji: "🏧",
    couleur: "#06923e",
    label: "Distributeur / banque"
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
  filtres: { mediaType: "", annee: "", departement: "", commune: "", nationalite: "", q: "", tri: "titre" },
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
  map = L.map("map", { zoomControl: false }).setView([43.9, 2.2], 8);
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
    remplirSelect("filtre-commune", data.communes, "Commune"); // no-op si l'élément n'existe pas (voir remplirSelect)
    remplirSelect("filtre-nationalite", data.nationalites, "Nationalité");
  } catch (e) { /* champs restent vides, pas bloquant */ }
}

function remplirSelect(id, valeurs, labelDefaut) {
  const select = document.getElementById(id);
  if (!select) return; // filtre désactivé (commenté dans le HTML)
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
  if (state.filtres.nationalite) params.set("nationalite", state.filtres.nationalite);
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
  if (coucheCercleRayon) { map.removeLayer(coucheCercleRayon); coucheCercleRayon = null; }
  if (coucheTraitPlusProche) { map.removeLayer(coucheTraitPlusProche); coucheTraitPlusProche = null; }
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
// Ordre de priorité (partenariats affiliation les plus probables en
// premier) — le reste garde son ordre d'arrivée (déjà trié par TMDB
// par pertinence), tronqué à 5 au total.
const PRIORITE_PLATEFORMES = ["amazon prime", "prime video", "rakuten", "netflix"];

function _trierEtLimiterPlateformes(plateformes) {
  const rang = (nom) => {
    const n = nom.toLowerCase();
    const i = PRIORITE_PLATEFORMES.findIndex((p) => n.includes(p));
    return i === -1 ? PRIORITE_PLATEFORMES.length : i;
  };
  return [...plateformes]
    .sort((a, b) => rang(a.nom) - rang(b.nom))
    .slice(0, 5);
}

// "Film Français" mais "Série Française" — la nationalité est stockée
// au masculin en base, on l'accorde ici selon le type ("tv"/"anime" = féminin).
const NATIONALITE_FEMININ = {
  "Français": "Française", "Belge": "Belge", "Suisse": "Suisse",
  "Canadien": "Canadienne", "Américain": "Américaine", "Britannique": "Britannique",
  "Allemand": "Allemande", "Espagnol": "Espagnole", "Italien": "Italienne",
  "Luxembourgeois": "Luxembourgeoise", "Monégasque": "Monégasque",
};

function _accorderNationalite(nationalite, mediaType) {
  if (mediaType === "movie") return nationalite; // "Film" = masculin, rien à changer
  return nationalite.split(" / ").map((n) => NATIONALITE_FEMININ[n] || n).join(" / ");
}

function _rendreVideo(media, nomLieu) {
  const url = media.url;
  const idYoutube = url.match(/(?:youtube\.com\/watch\?v=|youtu\.be\/)([\w-]+)/);
  if (idYoutube) {
    return `<iframe class="media-video" src="https://www.youtube.com/embed/${idYoutube[1]}" title="${media.legende || nomLieu}" frameborder="0" allowfullscreen loading="lazy"></iframe>`;
  }
  const idVimeo = url.match(/vimeo\.com\/(\d+)/);
  if (idVimeo) {
    return `<iframe class="media-video" src="https://player.vimeo.com/video/${idVimeo[1]}" title="${media.legende || nomLieu}" frameborder="0" allowfullscreen loading="lazy"></iframe>`;
  }
  return `<video class="media-video" src="${url}" controls preload="metadata"></video>`;
}

function ouvrirPopupLieu(film, lieu) {
  document.getElementById("popup-poster").src = film.poster_url || "/placeholder-poster.png";
  document.getElementById("popup-titre").textContent = film.titre;
  document.getElementById("popup-meta").textContent =
    `${labelMediaType(film.media_type)}${film.nationalite ? " " + _accorderNationalite(film.nationalite, film.media_type) : ""} · ${film.annee || "année inconnue"}`;
  document.getElementById("popup-adresse").textContent =
    [lieu.nom, lieu.commune, lieu.departement].filter(Boolean).join(", ");
  document.getElementById("popup-synopsis").textContent =
    lieu.description || film.synopsis || "Aucune description disponible.";

  const conteneurAnecdote = document.getElementById("popup-anecdote");
  conteneurAnecdote.innerHTML = lieu.anecdote
    ? `<p class="anecdote-titre">🎬 Anecdote de tournage</p>
       <div class="anecdote-texte scrollable">${lieu.anecdote}</div>
       ${lieu.source_anecdote ? `<a class="anecdote-source" href="${lieu.source_anecdote}" target="_blank" rel="noopener noreferrer">Source</a>` : ""}`
    : "";

  const conteneurDescriptionLieu = document.getElementById("popup-description-lieu");
  conteneurDescriptionLieu.innerHTML = lieu.description_wikipedia
    ? `<p class="anecdote-titre">📍 À propos de ce lieu</p><p class="anecdote-texte">${lieu.description_wikipedia}</p>`
    : "";

  document.getElementById("popup-resultats").innerHTML = "";
  document.getElementById("popup-overlay").dataset.lieuId = lieu.id;
  document.getElementById("popup-overlay").dataset.filmId = film.id;

  // Galerie photos/vidéos du lieu (nouvelle table lieu_medias) — repli
  // sur la photo unique (photo_url) si aucun média n'a encore été migré.
  const conteneurPhoto = document.getElementById("popup-photos-lieu");
  const medias = lieu.medias && lieu.medias.length
    ? lieu.medias
    : (lieu.photo_url ? [{ type_media: "photo", url: lieu.photo_url, legende: null, source: null }] : []);

  conteneurPhoto.innerHTML = medias.length
    ? `<p class="anecdote-titre">📷 Photos et vidéos du lieu</p>
       <div class="galerie-medias">
         ${medias.map((m) => m.type_media === "video"
           ? _rendreVideo(m, lieu.nom)
           : `<img src="${m.url}" alt="${m.legende || lieu.nom}" loading="lazy">`
         ).join("")}
       </div>`
    : "";

  // Plateformes de streaming — priorité à Amazon Prime / Rakuten /
  // Netflix (partenariats affiliation les plus probables), puis le
  // reste, limité à 5 au total pour ne pas surcharger le popup.
  const conteneurPlateformes = document.getElementById("popup-plateformes");
  const plateformesTriees = _trierEtLimiterPlateformes(state.plateformesCourantes || []);
  conteneurPlateformes.innerHTML = plateformesTriees.length ? (
    `<p class="plateformes-intro">Disponible sur :</p>` +
    plateformesTriees.map((p) => `
      <a class="plateforme-logo" href="${p.lien_affilie || p.lien_repli || '#'}" target="_blank" rel="noopener sponsored">
        <img src="${p.logo_url}" alt="${p.nom}"> ${p.nom}
      </a>
    `).join("")
  ) : "";

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
let modeTriCourant = "pied"; // "pied", "voiture" — plus de vol d'oiseau
let dernieresDonneesAmenities = null; // pour retrier sans refaire l'appel réseau

async function afficherCategorie(categorie) {
  const popupOverlay = document.getElementById("popup-overlay");
  const lieuId = popupOverlay.dataset.lieuId;
  if (!lieuId) return;

  modeTriCourant = "pied"; // repart du mode par défaut à chaque catégorie choisie
  if (coucheItineraireCommodite) { map.removeLayer(coucheItineraireCommodite); coucheItineraireCommodite = null; }

  document.querySelectorAll("#popup-boutons button").forEach((bouton) => {
    bouton.classList.toggle("actif", bouton.dataset.categorie === categorie);
  });

  const conteneur = document.getElementById("popup-resultats");
  conteneur.innerHTML = `<p style="color:#9a9ea8;">Chargement…</p>`;
  conteneur.scrollIntoView({ behavior: "smooth", block: "nearest" });

  const data = await _recupererAmenities(lieuId);
  if (!data) {
    conteneur.innerHTML = `<p style="color:#9a9ea8;">Données indisponibles pour ce lieu.</p>`;
    return;
  }

  const items = data.amenities?.[categorie] || [];
  const stats = data.stats?.[categorie] || null;
  const phrases = data.phrases_pied_voiture?.[categorie] || {};

  if (!items.length) {
    conteneur.innerHTML = `<p style="color:#9a9ea8;">Aucun résultat nommé trouvé à proximité.</p>`;
    return;
  }

  dernieresDonneesAmenities = { categorie, items, stats, phrases };
  _rendreCategorie();
}

function _rendreCategorie() {
  if (!dernieresDonneesAmenities) return;
  const { categorie, items, stats, phrases } = dernieresDonneesAmenities;
  const conteneur = document.getElementById("popup-resultats");
  const infoCategorie = ICONES_CATEGORIE[categorie] || {};
  const couleur = infoCategorie.couleur || "#e63946";
  const resume = creerResumeRecherche(stats, items.length);

  // Les 2 phrases essentielles (à pied / en voiture), toujours visibles
  const blocPhrases = `
    <div class="phrase-recommandation phrases-pied-voiture">
      ${phrases.pied ? `<p><b>🚶 À pied :</b> ${phrases.pied.texte}</p>` : ""}
      ${phrases.voiture ? `<p><b>🚗 En voiture :</b> ${phrases.voiture.texte}</p>` : ""}
    </div>
  `;

  // Boutons de tri groupé, juste après les phrases
  const selecteurTri = `
    <div class="selecteur-mode">
      <button class="mode-btn ${modeTriCourant === "pied" ? "actif" : ""}" data-mode="pied">🚶 Trier à pied</button>
      <button class="mode-btn ${modeTriCourant === "voiture" ? "actif" : ""}" data-mode="voiture">🚗 Trier en voiture</button>
    </div>
  `;

  // Tri selon le mode choisi (données déjà précalculées, aucun appel réseau)
  const cleDistance = modeTriCourant === "pied" ? "distance_pied_metres" : "distance_voiture_metres";
  const itemsTries = [...items].sort((a, b) => {
    const da = a[cleDistance] ?? Infinity;
    const db = b[cleDistance] ?? Infinity;
    return da - db;
  });

  const liste = itemsTries.map((item, index) => {
    const estPlusProche = index === 0;
    return `
      <div class="resultat-item ${estPlusProche ? "plus-proche" : ""}" style="${estPlusProche ? `border-color:${couleur};` : ""}">
        ${item.photo_url ? `<img class="resultat-photo" src="${item.photo_url}" alt="${item.nom}" loading="lazy">` : ""}
        <div class="nom">${estPlusProche ? "⭐ " : ""}${item.nom}</div>
        <div class="distance">${_texteDistanceDynamique(item, modeTriCourant)}</div>
        ${item.adresse ? `<div class="adresse">${item.adresse}</div>` : ""}
        ${item.horaires ? `<div class="horaires">${_texteHoraires(item.horaires)}</div>` : ""}
        ${item.telephone ? `<div class="telephone">📞 ${item.telephone}</div>` : ""}
        ${item.site_web ? `<div class="site-web"><a href="${item.site_web}" target="_blank" rel="noopener noreferrer">Voir le site</a></div>` : ""}
        <div class="boutons-itineraire">
          <button class="btn-itineraire" data-mode="foot-walking" data-lat="${item.latitude}" data-lon="${item.longitude}">🚶 À pied</button>
          <button class="btn-itineraire" data-mode="driving-car" data-lat="${item.latitude}" data-lon="${item.longitude}">🚗 En voiture</button>
        </div>
        <div class="itineraire-resultat"></div>
      </div>
    `;
  }).join("");

  conteneur.innerHTML = resume + blocPhrases + selecteurTri + liste;

  conteneur.querySelectorAll(".btn-itineraire").forEach((btn) => {
    btn.addEventListener("click", () => afficherItineraireVersCommodite(btn));
  });
  conteneur.querySelectorAll(".mode-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      modeTriCourant = btn.dataset.mode;
      _rendreCategorie();
    });
  });

  afficherCommoditesSurCarte(categorie, itemsTries, stats, modeTriCourant);
}

let coucheItineraireCommodite = null;

async function afficherItineraireVersCommodite(bouton, idConteneurOverride) {
  const lieuId = document.getElementById("popup-overlay").dataset.lieuId;
  const lieu = state.lieuxCourants.find((l) => l.id === Number(lieuId));
  if (!lieu) return;

  const mode = bouton.dataset.mode;
  const arriveeLat = parseFloat(bouton.dataset.lat);
  const arriveeLon = parseFloat(bouton.dataset.lon);
  const conteneurResultat = idConteneurOverride
    ? document.getElementById(idConteneurOverride)
    : bouton.closest(".resultat-item").querySelector(".itineraire-resultat");
  conteneurResultat.textContent = "Calcul de l'itinéraire…";

  try {
    const params = new URLSearchParams({
      depart_lat: lieu.latitude, depart_lon: lieu.longitude,
      arrivee_lat: arriveeLat, arrivee_lon: arriveeLon, mode,
    });
    const res = await fetch(`${API_BASE}/api/itineraire?${params}`);
    const data = await res.json();

    const distanceTxt = formatDistance(data.distance_metres);
    const modeTexte = mode === "foot-walking" ? "à pied" : "en voiture";
    const precision = data.type === "route_reelle" ? "" : " (estimation à vol d'oiseau)";
    const dureeTxt = data.duree_secondes ? ` — ${formatDuree(data.duree_secondes)}` : "";

    conteneurResultat.innerHTML = `
      <div>Itinéraire ${modeTexte} (${distanceTxt}${dureeTxt})${precision}</div>
      <button class="btn-demarrer-navigation" data-lat="${arriveeLat}" data-lon="${arriveeLon}" data-mode="${mode}">
        🧭 Démarrer la navigation
      </button>
    `;
    conteneurResultat.querySelector(".btn-demarrer-navigation").addEventListener("click", (e) => {
      demarrerNavigation(parseFloat(e.target.dataset.lat), parseFloat(e.target.dataset.lon), e.target.dataset.mode);
    });

    if (coucheItineraireCommodite) map.removeLayer(coucheItineraireCommodite);
    coucheItineraireCommodite = L.geoJSON(data.geometry, {
      style: { color: mode === "foot-walking" ? "#2a9d8f" : "#4361ee", weight: 4, opacity: 0.8 },
    }).addTo(map);
  } catch (e) {
    conteneurResultat.textContent = "Itinéraire indisponible.";
  }
}

// ── Affiche les points "activité" sur la carte (calque séparé des
// lieux de tournage), avec l'icône propre à la catégorie et un clic
// qui montre les infos du point, comme pour les autres commodités ──
let coucheCercleRayon = null;
let coucheTraitPlusProche = null;
let contexteAudio = null;

function _jouerSon() {
  // Petit "ping" synthétisé (pas de fichier audio à héberger/charger).
  try {
    contexteAudio = contexteAudio || new (window.AudioContext || window.webkitAudioContext)();
    const osc = contexteAudio.createOscillator();
    const gain = contexteAudio.createGain();
    osc.type = "sine";
    osc.frequency.setValueAtTime(880, contexteAudio.currentTime);
    osc.frequency.exponentialRampToValueAtTime(440, contexteAudio.currentTime + 0.15);
    gain.gain.setValueAtTime(0.15, contexteAudio.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.001, contexteAudio.currentTime + 0.2);
    osc.connect(gain).connect(contexteAudio.destination);
    osc.start();
    osc.stop(contexteAudio.currentTime + 0.2);
  } catch (e) { /* audio non disponible (autoplay bloqué, etc.) — silencieux */ }
}

function afficherCommoditesSurCarte(categorie, itemsTries, stats, modeTri) {
  clusterActivites.clearLayers();
  if (coucheCercleRayon) { map.removeLayer(coucheCercleRayon); coucheCercleRayon = null; }

  const lieuActuel = state.lieuxCourants.find(
    (l) => l.id === Number(document.getElementById("popup-overlay").dataset.lieuId)
  );
  const infoCategorie = ICONES_CATEGORIE[categorie] || {};
  const bounds = lieuActuel ? [[lieuActuel.latitude, lieuActuel.longitude]] : [];

  // Cercle Turf.js autour du lieu de tournage, rayon = celui utilisé
  // pour la recherche de cette catégorie (visualise concrètement la
  // zone dans laquelle les commodités ont été cherchées).
  if (lieuActuel && stats?.rayon_metres) {
    const centre = turf.point([lieuActuel.longitude, lieuActuel.latitude]);
    const cercle = turf.circle(centre, stats.rayon_metres / 1000, { units: "kilometers", steps: 64 });
    coucheCercleRayon = L.geoJSON(cercle, {
      style: { color: infoCategorie.couleur || "#e63946", weight: 1, fillOpacity: 0.06, dashArray: "4 4" },
    }).addTo(map);
  }

  itemsTries.forEach((item, index) => {
    const estPlusProche = index === 0;
    const couleurIcone = estPlusProche ? "#ffd60a" : (infoCategorie.couleur || "#e63946");
    const icone = L.divIcon({
      html: `<div class="icone-commodite" style="color:${couleurIcone};${estPlusProche ? "font-size:30px;filter:drop-shadow(0 0 4px #ffd60a);" : ""}">${infoCategorie.emoji || "📍"}</div>`,
      className: "", iconSize: estPlusProche ? [32, 32] : [24, 24], iconAnchor: estPlusProche ? [16, 30] : [12, 22],
    });
    const idPopupItineraire = `itin-carte-${categorie}-${index}`;
    const texteDistance = _texteDistanceDynamique(item, modeTri);
    const marker = L.marker([item.latitude, item.longitude], { icon: icone }).bindPopup(`
      ${item.photo_url ? `<img class="resultat-photo" src="${item.photo_url}" alt="${item.nom}" loading="lazy" style="margin-bottom:6px;">` : ""}
      <b>${estPlusProche ? "⭐ " : ""}${item.nom}</b><br>
      ${texteDistance} du lieu de tournage
      ${item.adresse ? `<br>${item.adresse}` : ""}
      ${item.horaires ? `<br>${_texteHoraires(item.horaires)}` : ""}
      ${item.telephone ? `<br>📞 ${item.telephone}` : ""}
      ${item.site_web ? `<br><a href="${item.site_web}" target="_blank" rel="noopener noreferrer">Voir le site</a>` : ""}
      <div class="boutons-itineraire" style="margin-top:6px;">
        <button class="btn-itineraire" data-mode="foot-walking" data-lat="${item.latitude}" data-lon="${item.longitude}">🚶 À pied</button>
        <button class="btn-itineraire" data-mode="driving-car" data-lat="${item.latitude}" data-lon="${item.longitude}">🚗 En voiture</button>
      </div>
      <div class="itineraire-resultat" id="${idPopupItineraire}"></div>
    `);

    // Leaflet reconstruit le contenu du popup à chaque ouverture — il
    // faut rebrancher les écouteurs à ce moment-là (popupopen), pas à
    // la création du marqueur (le DOM du popup n'existe pas encore).
    marker.on("popupopen", (e) => {
      if (coucheItineraireCommodite) { map.removeLayer(coucheItineraireCommodite); coucheItineraireCommodite = null; }
      e.popup.getElement().querySelectorAll(".btn-itineraire").forEach((btn) => {
        btn.addEventListener("click", () => afficherItineraireVersCommodite(btn, idPopupItineraire));
      });
    });

    // Le plus proche (selon le mode piéton/voiture choisi, plus de vol
    // d'oiseau) a une couleur distincte + un son au clic — plus de
    // trait en pointillés, uniquement demandé pour un lieu précis via
    // les boutons 🚶/🚗 désormais.
    if (estPlusProche) {
      marker.on("click", () => _jouerSon());
    }

    clusterActivites.addLayer(marker);
    bounds.push([item.latitude, item.longitude]);
  });

  if (bounds.length) map.fitBounds(bounds, { padding: [40, 40], maxZoom: 14 });
  if (state.dernierBounds) document.getElementById("btn-recentrer").classList.remove("hidden");
}

function _texteHoraires(horaires) {
  if (typeof opening_hours === "undefined") return `🕒 ${horaires}`;
  try {
    const oh = new opening_hours(horaires, { lat: 43.9, lon: 2.2 }, { locale: "fr" });
    const maintenant = new Date();
    const ouvert = oh.getState(maintenant);
    const prochainChangement = oh.getNextChange(maintenant);
    const minutesAvant = prochainChangement
      ? Math.round((prochainChangement - maintenant) / 60000)
      : null;

    if (ouvert) {
      if (minutesAvant !== null && minutesAvant <= 60) {
        return `🕒 ${horaires} · <span class="statut-ouvert">Ferme dans ${minutesAvant} min</span>`;
      }
      return `🕒 ${horaires} · <span class="statut-ouvert">Ouvert</span>`;
    }
    if (minutesAvant !== null && minutesAvant <= 60) {
      return `🕒 ${horaires} · <span class="statut-bientot">Ouvre dans ${minutesAvant} min</span>`;
    }
    return `🕒 ${horaires} · <span class="statut-ferme">Fermé</span>`;
  } catch (e) {
    // Format d'horaires OSM non standard ou non reconnu — on affiche
    // juste le texte brut plutôt que de planter.
    return `🕒 ${horaires}`;
  }
}

function _texteDistanceDynamique(item, modeTri) {
  if (modeTri === "pied" && item.distance_pied_metres != null) {
    return `${formatDistance(item.distance_pied_metres)} à pied (${formatDuree(item.duree_pied_secondes)})`;
  }
  if (modeTri === "voiture" && item.distance_voiture_metres != null) {
    return `${formatDistance(item.distance_voiture_metres)} en voiture (${formatDuree(item.duree_voiture_secondes)})`;
  }
  return formatDistance(item.distance_metres);
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

  const conteneurResultat = document.getElementById("resultat-trace");
  conteneurResultat.innerHTML = `<p style="color:#9a9ea8;">Calcul du trajet le plus optimisé…</p>`;
  effacerTrace();

  try {
    const res = await fetch(`${API_BASE}/api/films/${filmId}/trace`);
    if (!res.ok) {
      conteneurResultat.innerHTML = `<p style="color:#9a9ea8;">Tracé impossible (un seul lieu recensé, ou erreur serveur).</p>`;
      return;
    }
    const data = await res.json();

    state.traceLayer = L.geoJSON(data.geometry, {
      style: { color: "#e63946", weight: 4, opacity: 0.8 },
    }).addTo(map);

    // Un marqueur numéroté (1, 2, 3…) par étape, dans l'ordre du trajet optimisé
    data.etapes.forEach((etape, index) => {
      const icone = L.divIcon({
        html: `<div class="numero-etape">${index + 1}</div>`,
        className: "", iconSize: [28, 28], iconAnchor: [14, 14],
      });
      L.marker([etape.latitude, etape.longitude], { icon: icone }).addTo(map);
    });

    const distanceKm = (data.distance_metres / 1000).toFixed(1);
    const dureeTxt = data.duree_secondes ? formatDuree(data.duree_secondes) : null;
    const typeTexte = data.type === "route_reelle"
      ? `Voici le trajet le plus optimisé en temps et en distance en voiture (${distanceKm} km, environ ${dureeTxt}) :`
      : `Estimation à vol d'oiseau (${distanceKm} km, itinéraire routier indisponible) :`;

    const listeAdresses = data.adresses.map((adresse, i) => `<li>${adresse}</li>`).join("");
    conteneurResultat.innerHTML = `
      <p class="trace-intro">${typeTexte}</p>
      <ol class="trace-liste">${listeAdresses}</ol>
    `;

    map.fitBounds(state.traceLayer.getBounds(), { padding: [40, 40] });
  } catch (e) {
    conteneurResultat.innerHTML = `<p style="color:#9a9ea8;">Erreur lors du calcul du tracé.</p>`;
  }
}

function formatDistance(m) {
  return m < 1000 ? `${m} m` : `${(m / 1000).toFixed(1)} km`;
}

function formatDuree(secondes) {
  const min = Math.round(secondes / 60);
  if (min < 60) return `${min} min`;
  const h = Math.floor(min / 60);
  const reste = min % 60;
  return `${h}h${reste > 0 ? reste.toString().padStart(2, "0") : ""}`;
}

// ── Navigation guidée : géolocalisation réelle + instructions vocales ──
// Limites honnêtes : pas de recalcul automatique d'itinéraire en cas
// d'écart important (juste un avertissement), et les instructions
// viennent d'OSRM (simples : gauche/droite/tout droit), pas aussi
// riches qu'un GPS dédié. Fonctionne néanmoins pour un usage réel.
let suiviPositionId = null;
let etapesNavigationCourantes = [];
let indexEtapeCourante = 0;

function _parler(texte) {
  if (!("speechSynthesis" in window)) return;
  const enonce = new SpeechSynthesisUtterance(texte);
  enonce.lang = "fr-FR";
  window.speechSynthesis.speak(enonce);
}

async function demarrerNavigation(destLat, destLon, mode) {
  if (!("geolocation" in navigator)) {
    alert("La géolocalisation n'est pas disponible sur cet appareil.");
    return;
  }

  const panneau = document.getElementById("panneau-navigation");
  panneau.classList.remove("hidden");
  panneau.querySelector(".nav-instruction").textContent = "Localisation en cours…";

  navigator.geolocation.getCurrentPosition(async (position) => {
    const departLat = position.coords.latitude;
    const departLon = position.coords.longitude;

    const params = new URLSearchParams({
      depart_lat: departLat, depart_lon: departLon,
      arrivee_lat: destLat, arrivee_lon: destLon, mode, etapes: "true",
    });
    const res = await fetch(`${API_BASE}/api/itineraire?${params}`);
    const data = await res.json();

    if (!data.etapes_navigation || !data.etapes_navigation.length) {
      panneau.querySelector(".nav-instruction").textContent =
        "Navigation détaillée indisponible (itinéraire en ligne droite uniquement).";
      return;
    }

    etapesNavigationCourantes = data.etapes_navigation;
    indexEtapeCourante = 0;

    if (coucheItineraireCommodite) map.removeLayer(coucheItineraireCommodite);
    coucheItineraireCommodite = L.geoJSON(data.geometry, {
      style: { color: "#ffd60a", weight: 5, opacity: 0.9 },
    }).addTo(map);

    _parler(etapesNavigationCourantes[0].instruction);
    panneau.querySelector(".nav-instruction").textContent = etapesNavigationCourantes[0].instruction;

    if (suiviPositionId) navigator.geolocation.clearWatch(suiviPositionId);
    suiviPositionId = navigator.geolocation.watchPosition(_surNouvellePosition, null, {
      enableHighAccuracy: true, maximumAge: 2000, timeout: 10000,
    });
  }, () => {
    panneau.querySelector(".nav-instruction").textContent = "Impossible d'obtenir ta position (autorisation refusée ?).";
  }, { enableHighAccuracy: true });
}

function _surNouvellePosition(position) {
  const { latitude, longitude } = position.coords;
  const panneau = document.getElementById("panneau-navigation");
  const etape = etapesNavigationCourantes[indexEtapeCourante];
  if (!etape) return;

  const distanceEtape = haversineApprox(latitude, longitude, etape.latitude, etape.longitude);
  panneau.querySelector(".nav-distance").textContent = `Dans ${formatDistance(Math.round(distanceEtape))}`;

  // Sous 25m de la manœuvre : on l'annonce et on passe à la suivante.
  if (distanceEtape < 25 && indexEtapeCourante < etapesNavigationCourantes.length - 1) {
    indexEtapeCourante += 1;
    const suivante = etapesNavigationCourantes[indexEtapeCourante];
    _parler(suivante.instruction);
    panneau.querySelector(".nav-instruction").textContent = suivante.instruction;
  } else if (distanceEtape < 15 && indexEtapeCourante === etapesNavigationCourantes.length - 1) {
    _parler("Vous êtes arrivé à destination.");
    panneau.querySelector(".nav-instruction").textContent = "Vous êtes arrivé à destination 🎉";
    arreterNavigation();
  }
}

function haversineApprox(lat1, lon1, lat2, lon2) {
  const R = 6371000;
  const toRad = (d) => (d * Math.PI) / 180;
  const dphi = toRad(lat2 - lat1);
  const dlambda = toRad(lon2 - lon1);
  const a = Math.sin(dphi / 2) ** 2 + Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) * Math.sin(dlambda / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(a));
}

function arreterNavigation() {
  if (suiviPositionId) navigator.geolocation.clearWatch(suiviPositionId);
  suiviPositionId = null;
  setTimeout(() => document.getElementById("panneau-navigation").classList.add("hidden"), 3000);
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

  ["filtre-annee", "filtre-departement", "filtre-commune", "filtre-nationalite"].forEach((id) => {
    const element = document.getElementById(id);
    if (!element) return; // filtre désactivé (commenté dans le HTML)
    element.addEventListener("change", (e) => {
      const cle = { "filtre-annee": "annee", "filtre-departement": "departement", "filtre-commune": "commune", "filtre-nationalite": "nationalite" }[id];
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
  document.getElementById("btn-arreter-navigation").addEventListener("click", () => {
    arreterNavigation();
    document.getElementById("panneau-navigation").classList.add("hidden");
  });
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