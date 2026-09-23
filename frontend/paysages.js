// ═══════════════════════════════════════════════════════════════
// Landscape Studio — paysages.js
// Branche le frontend sur backend/paysages.py (préfixe /api/paysages)
// ═══════════════════════════════════════════════════════════════
const API_BASE = "";

const esc = v => String(v ?? "").replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
const $ = id => document.getElementById(id);

async function api(path, options = {}) {
  const res = await fetch(`${API_BASE}${path}`, {
    credentials: "include",
    headers: options.body ? { "Content-Type": "application/json" } : {},
    ...options,
  });
  let data = null;
  try { data = await res.json(); } catch {}
  if (!res.ok) throw new Error((data && data.detail) || `Erreur ${res.status}`);
  return data;
}

const MEDIA_LABELS = { photo:"Photo", panorama:"Panorama", "360":"360°", video_360:"Vidéo 360°", a_capturer:"À capturer" };
const DROITS_LABELS = { utilisable:"Utilisable", a_negocier:"À négocier", libre_licence:"Libre de licence", a_capturer:"À capturer", a_verifier:"À vérifier" };
const DROITS_CLASS = { utilisable:"ok", libre_licence:"ok", a_negocier:"warn", a_verifier:"warn", a_capturer:"risk" };
const STATUT_LIEN_LABELS = { candidat:"Candidat", selectionne:"Sélectionné", alternative:"Alternative", rejete:"Rejeté" };





const LICENCE_LABELS = {
  cc0: "CC0",
  pdm: "Domaine public",
  by: "CC BY",
  "by-sa": "CC BY-SA",
  "by-nd": "CC BY-ND",
  "by-nc": "CC BY-NC",
  "by-nc-sa": "CC BY-NC-SA",
  "by-nc-nd": "CC BY-NC-ND",
};

function licenceLabel(code, fallback = "") {
  return LICENCE_LABELS[code] || fallback || "Licence inconnue";
}

function droitsBadge(p) {
  if (p.usage_commercial === true) {
    if (p.verification_statut === "droits_confirmes") {
      return `<span class="ls-badge ok">
        🟢 Droits confirmés
      </span>`;
    }

    return `<span class="ls-badge ok">
      🟢 Compatible commercial
    </span>`;
  }

  if (p.usage_commercial === false) {
    return `<span class="ls-badge risk">
      🔴 Non commercial
    </span>`;
  }

  if (p.statut_droits === "a_negocier") {
    return `<span class="ls-badge warn">
      🟠 À négocier
    </span>`;
  }

  return `<span class="ls-badge warn">
    🟠 Licence à vérifier
  </span>`;
}

function informationsLicence(p) {
  const licence = licenceLabel(
    p.licence_code,
    p.licence
  );

  return `
    <div class="ls-license-box">

      <div>
        <strong>Licence</strong>
        <span>${esc(licence)}</span>
      </div>

      ${p.usage_commercial === true ? `
        <div>💼 Usage commercial : <strong>oui</strong></div>
      ` : ""}

      ${p.usage_commercial === false ? `
        <div>💼 Usage commercial : <strong>non</strong></div>
      ` : ""}

      ${p.modification_autorisee === true ? `
        <div>✏️ Modification : <strong>autorisée</strong></div>
      ` : ""}

      ${p.modification_autorisee === false ? `
        <div>✏️ Modification : <strong>non autorisée</strong></div>
      ` : ""}

      ${p.attribution_requise === true ? `
        <div>👤 Attribution : <strong>requise</strong></div>
      ` : ""}

      ${p.licence_url ? `
        <a
          href="${esc(p.licence_url)}"
          target="_blank"
          rel="noopener noreferrer"
        >
          Voir les conditions de licence
        </a>
      ` : ""}

      ${p.source_url ? `
        <a
          href="${esc(p.source_url)}"
          target="_blank"
          rel="noopener noreferrer"
        >
          Voir la source originale
        </a>
      ` : ""}

      <small>
        Informations issues de la source déclarée.
        Vérifiez les droits avant exploitation commerciale.
      </small>

    </div>
  `;
}
// ─────────────────────────────────────────────────────────────
// Onglets principaux
// ─────────────────────────────────────────────────────────────
document.querySelectorAll(".ls-tab").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".ls-tab").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    document.querySelectorAll(".ls-panel").forEach(p => p.classList.add("hidden"));
    $(`tab-${btn.dataset.tab}`).classList.remove("hidden");
    if (btn.dataset.tab === "bibliotheque") chargerRechercheBibliotheque();
  });
});

document.querySelectorAll(".ls-subtab").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".ls-subtab").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    document.querySelectorAll(".ls-sub").forEach(p => p.classList.add("hidden"));
    $(`sub-${btn.dataset.subtab}`).classList.remove("hidden");
    if (btn.dataset.subtab === "carte") chargerCarte();
  });
});

// ─────────────────────────────────────────────────────────────
// MODALE — édition d'un paysage existant (PATCH /api/paysages/{id})
// ─────────────────────────────────────────────────────────────
function fermerModal() {
  const m = $("ls-modal");
  if (m) m.remove();
}

function ouvrirModal(html) {
  fermerModal();
  const overlay = document.createElement("div");
  overlay.id = "ls-modal";
  overlay.className = "ls-modal-overlay";
  overlay.innerHTML = `<div class="ls-modal">${html}</div>`;
  document.body.appendChild(overlay);
  overlay.addEventListener("click", ev => { if (ev.target === overlay) fermerModal(); });
  return overlay;
}

async function ouvrirEditionPaysage(paysageId, onSauvegarde) {
  let p;
  try {
    p = await api(`/api/paysages/${paysageId}`);
  } catch (e) { return alert("Erreur : " + e.message); }

  ouvrirModal(`
    <h3>✏️ Modifier « ${esc(p.nom)} »</h3>
    <div class="ls-form" style="max-width:none;border:none;padding:0;margin:0">
      <input type="text" id="e-nom" placeholder="Nom" value="${esc(p.nom || "")}">
      <div class="ls-form-row">
        <input type="text" id="e-type" placeholder="Type" value="${esc(p.type || "")}">
        <input type="text" id="e-environnement" placeholder="Environnement" value="${esc(p.environnement || "")}">
      </div>
      <div class="ls-form-row">
        <input type="text" id="e-ambiance" placeholder="Ambiance" value="${esc(p.ambiance || "")}">
        <input type="text" id="e-elements" placeholder="Éléments (virgules)" value="${esc((p.elements || []).join(", "))}">
      </div>
      <div class="ls-form-row">
        <input type="number" step="any" id="e-lat" placeholder="Latitude" value="${p.latitude ?? ""}">
        <input type="number" step="any" id="e-lon" placeholder="Longitude" value="${p.longitude ?? ""}">
      </div>
      <div class="ls-form-row">
        <select id="e-media-type">
          ${Object.entries(MEDIA_LABELS).map(([v, l]) => `<option value="${v}" ${v === p.media_type ? "selected" : ""}>${l}</option>`).join("")}
        </select>
        <select id="e-statut-droits">
          ${Object.entries(DROITS_LABELS).map(([v, l]) => `<option value="${v}" ${v === p.statut_droits ? "selected" : ""}>${l}</option>`).join("")}
        </select>
      </div>
      <p class="ls-modal-note">Sans latitude/longitude, ce paysage n'apparaît pas sur la carte.</p>
      <div class="ls-form-actions">
        <button class="ls-btn ls-btn-accent" id="e-valider">Enregistrer</button>
        <button class="ls-btn" id="e-annuler">Annuler</button>
      </div>
    </div>
  `);

  $("e-annuler").addEventListener("click", fermerModal);
  $("e-valider").addEventListener("click", async () => {
    const nom = $("e-nom").value.trim();
    if (!nom) return alert("Le nom est requis.");
    const payload = {
      nom,
      type: $("e-type").value.trim() || null,
      environnement: $("e-environnement").value.trim() || null,
      ambiance: $("e-ambiance").value.trim() || null,
      elements: $("e-elements").value.trim().split(",").map(s => s.trim()).filter(Boolean),
      latitude: $("e-lat").value ? Number($("e-lat").value) : null,
      longitude: $("e-lon").value ? Number($("e-lon").value) : null,
      media_type: $("e-media-type").value,
      statut_droits: $("e-statut-droits").value,
    };
    try {
      await api(`/api/paysages/${paysageId}`, { method: "PATCH", body: JSON.stringify(payload) });
      fermerModal();
      if (onSauvegarde) onSauvegarde();
    } catch (e) { alert("Erreur : " + e.message); }
  });
}

// ─────────────────────────────────────────────────────────────
// PROJETS — liste
// ─────────────────────────────────────────────────────────────
function afficherVueListeProjets() {
  $("vue-projet-detail").classList.add("hidden");
  $("vue-scene-detail").classList.add("hidden");
  $("vue-liste-projets").classList.remove("hidden");
  chargerProjets();
}

async function chargerProjets() {
  const el = $("liste-projets");
  el.innerHTML = `<p class="ls-loading">Chargement…</p>`;
  try {
    const projets = await api("/api/paysages/projets");
    if (!projets.length) { el.innerHTML = `<p class="ls-empty">Aucun projet pour l'instant. Créez-en un pour commencer.</p>`; return; }
    el.innerHTML = projets.map(p => `
      <div class="ls-card" data-id="${p.id}">
        <div class="ls-card-noimg">📁</div>
        <div class="ls-card-body">
          <b>${esc(p.nom)}</b>
          <span>${esc(p.description || "Pas de description")}</span>
          <span>${new Date(p.created_at).toLocaleDateString("fr-FR")}</span>
        </div>
      </div>`).join("");
    el.querySelectorAll(".ls-card").forEach(c => c.addEventListener("click", () => ouvrirProjet(c.dataset.id)));
  } catch (e) {
    el.innerHTML = `<p class="ls-empty">Impossible de charger vos projets : ${esc(e.message)}</p>`;
  }
}

$("btn-nouveau-projet").addEventListener("click", () => $("form-nouveau-projet").classList.toggle("hidden"));
$("np-annuler").addEventListener("click", () => $("form-nouveau-projet").classList.add("hidden"));
$("np-valider").addEventListener("click", async () => {
  const nom = $("np-nom").value.trim();
  if (!nom) return alert("Le nom du projet est requis.");
  try {
    await api("/api/paysages/projets", { method: "POST", body: JSON.stringify({ nom, description: $("np-description").value.trim() || null }) });
    $("np-nom").value = ""; $("np-description").value = "";
    $("form-nouveau-projet").classList.add("hidden");
    chargerProjets();
  } catch (e) { alert("Erreur : " + e.message); }
});

$("btn-retour-projets").addEventListener("click", afficherVueListeProjets);

// ─────────────────────────────────────────────────────────────
// PROJET — détail + scènes
// ─────────────────────────────────────────────────────────────
let projetCourantId = null;

async function ouvrirProjet(id) {
  projetCourantId = id;
  $("vue-liste-projets").classList.add("hidden");
  $("vue-scene-detail").classList.add("hidden");
  $("vue-projet-detail").classList.remove("hidden");
  $("form-nouvelle-scene").classList.add("hidden");
  await chargerProjetDetail();
}

async function chargerProjetDetail() {
  $("projet-detail-head").innerHTML = `<p class="ls-loading">Chargement…</p>`;
  $("liste-scenes").innerHTML = "";
  try {
    const p = await api(`/api/paysages/projets/${projetCourantId}`);
    $("projet-detail-head").innerHTML = `
      <div class="ls-detail-head">
        <h2>📁 ${esc(p.nom)}</h2>
        <p>${esc(p.description || "Pas de description")}</p>
      </div>`;
    if (!p.scenes.length) {
      $("liste-scenes").innerHTML = `<p class="ls-empty">Aucune scène. Ajoutez-en une.</p>`;
      return;
    }
    $("liste-scenes").innerHTML = p.scenes.map(s => `
      <div class="ls-card" data-id="${s.id}">
        <div class="ls-card-noimg">🎬</div>
        <div class="ls-card-body">
          <b>${s.numero != null ? `#${s.numero} — ` : ""}${esc(s.titre || "Scène sans titre")}</b>
          <span>${esc(s.ambiance || "")}</span>
          <span>Format : ${MEDIA_LABELS[s.format_souhaite] || s.format_souhaite || "peu importe"}</span>
        </div>
      </div>`).join("");
    $("liste-scenes").querySelectorAll(".ls-card").forEach(c => c.addEventListener("click", () => ouvrirScene(c.dataset.id)));
  } catch (e) {
    $("projet-detail-head").innerHTML = `<p class="ls-empty">Erreur : ${esc(e.message)}</p>`;
  }
}

$("btn-nouvelle-scene").addEventListener("click", () => $("form-nouvelle-scene").classList.toggle("hidden"));
$("sc-annuler").addEventListener("click", () => $("form-nouvelle-scene").classList.add("hidden"));
$("sc-valider").addEventListener("click", async () => {
  const payload = {
    titre: $("sc-titre").value.trim() || null,
    numero: $("sc-numero").value ? Number($("sc-numero").value) : null,
    description: $("sc-description").value.trim() || null,
    ambiance: $("sc-ambiance").value.trim() || null,
    environnement: $("sc-environnement").value.trim() || null,
    epoque: $("sc-epoque").value.trim() || null,
    region: $("sc-region").value.trim() || null,
    ville_reference: $("sc-ville").value.trim() || null,
    distance_max_km: $("sc-distance").value ? Number($("sc-distance").value) : null,
    intention_artistique: $("sc-intention").value.trim() || null,
    format_souhaite: $("sc-format").value,
    besoin_elements: [],
  };
  try {
    await api(`/api/paysages/projets/${projetCourantId}/scenes`, { method: "POST", body: JSON.stringify(payload) });
    ["sc-titre","sc-numero","sc-description","sc-ambiance","sc-environnement","sc-epoque","sc-region","sc-ville","sc-distance","sc-intention"]
      .forEach(id => $(id).value = "");
    $("form-nouvelle-scene").classList.add("hidden");
    chargerProjetDetail();
  } catch (e) { alert("Erreur : " + e.message); }
});

// ─────────────────────────────────────────────────────────────
// SCÈNE — détail, paysages liés, liaison
// ─────────────────────────────────────────────────────────────
let sceneCouranteId = null;

async function ouvrirScene(id) {
  sceneCouranteId = id;
  $("vue-projet-detail").classList.add("hidden");
  $("vue-scene-detail").classList.remove("hidden");
  $("lier-resultats").innerHTML = "";
  $("lier-recherche").value = "";
  await chargerSceneDetail();
}

$("btn-retour-scenes").addEventListener("click", () => {
  $("vue-scene-detail").classList.add("hidden");
  $("vue-projet-detail").classList.remove("hidden");
  chargerProjetDetail();
});

function carteLienPaysage(l) {
  const badgeClass = DROITS_CLASS[l.statut_droits] || "";
  return `
    <div class="ls-card" data-paysage-id="${l.id}">
      ${l.thumbnail_url ? `<img src="${esc(l.thumbnail_url)}" alt="${esc(l.nom)}">` : `<div class="ls-card-noimg">🖼️</div>`}
      <div class="ls-card-body">
        <b>${esc(l.nom)}</b>
        <span>${esc(l.ambiance || "")}</span>
        <div class="ls-badges">
          <span class="ls-badge">${MEDIA_LABELS[l.media_type] || l.media_type}</span>
          <span class="ls-badge ${badgeClass}">${DROITS_LABELS[l.statut_droits] || l.statut_droits}</span>
        </div>
        <select class="ls-statut-select" data-lien-paysage="${l.id}">
          ${Object.entries(STATUT_LIEN_LABELS).map(([v, label]) =>
            `<option value="${v}" ${v === l.statut ? "selected" : ""}>${label}</option>`).join("")}
        </select>
        <button class="ls-card-edit-btn" data-editer="${l.id}">✏️ Modifier ce paysage</button>
      </div>
    </div>`;
}

async function chargerSceneDetail() {
  $("scene-detail-head").innerHTML = `<p class="ls-loading">Chargement…</p>`;
  $("liste-liens").innerHTML = "";
  try {
    const s = await api(`/api/paysages/scenes/${sceneCouranteId}`);
    $("scene-detail-head").innerHTML = `
      <div class="ls-detail-head">
        <h2>🎬 ${s.numero != null ? `#${s.numero} — ` : ""}${esc(s.titre || "Scène sans titre")}</h2>
        <p>${esc(s.description || "")}</p>
        <p>${esc(s.ambiance || "")}${s.environnement ? " · " + esc(s.environnement) : ""}${s.epoque ? " · " + esc(s.epoque) : ""}</p>
        <p>${esc(s.intention_artistique || "")}</p>
      </div>`;
    $("liste-liens").innerHTML = s.paysages.length
      ? s.paysages.map(carteLienPaysage).join("")
      : `<p class="ls-empty">Aucun paysage lié pour l'instant.</p>`;
    $("liste-liens").querySelectorAll("[data-lien-paysage]").forEach(sel => {
      sel.addEventListener("change", async () => {
        try {
          await api(`/api/paysages/scenes/${sceneCouranteId}/lier`, {
            method: "POST",
            body: JSON.stringify({ paysage_id: Number(sel.dataset.lienPaysage), statut: sel.value }),
          });
        } catch (e) { alert("Erreur : " + e.message); }
      });
    });
    $("liste-liens").querySelectorAll("[data-editer]").forEach(btn => {
      btn.addEventListener("click", ev => {
        ev.stopPropagation();
        ouvrirEditionPaysage(btn.dataset.editer, chargerSceneDetail);
      });
    });
  } catch (e) {
    $("scene-detail-head").innerHTML = `<p class="ls-empty">Erreur : ${esc(e.message)}</p>`;
  }
}

$("lier-rechercher-btn").addEventListener("click", async () => {
  const q = $("lier-recherche").value.trim();
  const el = $("lier-resultats");
  el.innerHTML = `<p class="ls-loading">Recherche…</p>`;
  try {
    const params = new URLSearchParams();
    if (q) params.set("ambiance", q);
    const resultats = await api(`/api/paysages?${params.toString()}`);
    if (!resultats.length) { el.innerHTML = `<p class="ls-empty">Aucun résultat. Essayez un autre terme, ou ajoutez un paysage dans la bibliothèque.</p>`; return; }
    el.innerHTML = resultats.map(p => `
      <div class="ls-card" data-id="${p.id}">
        ${p.thumbnail_url ? `<img src="${esc(p.thumbnail_url)}" alt="${esc(p.nom)}">` : `<div class="ls-card-noimg">🖼️</div>`}
        <div class="ls-card-body">
          <b>${esc(p.nom)}</b>
          <span>${esc(p.ambiance || p.type || "")}</span>
          <button class="ls-btn ls-btn-accent" data-lier="${p.id}">Lier cette scène</button>
        </div>
      </div>`).join("");
    el.querySelectorAll("[data-lier]").forEach(btn => {
      btn.addEventListener("click", async ev => {
        ev.stopPropagation();
        try {
          await api(`/api/paysages/scenes/${sceneCouranteId}/lier`, {
            method: "POST",
            body: JSON.stringify({ paysage_id: Number(btn.dataset.lier), statut: "candidat" }),
          });
          chargerSceneDetail();
        } catch (e) { alert("Erreur : " + e.message); }
      });
    });
  } catch (e) {
    el.innerHTML = `<p class="ls-empty">Erreur : ${esc(e.message)}</p>`;
  }
});

// ─────────────────────────────────────────────────────────────
// BIBLIOTHÈQUE — recherche multicritère
// ─────────────────────────────────────────────────────────────
function carteResultatPaysage(p) {
  return `
    <div class="ls-card" data-id="${p.id}">

      ${
        p.thumbnail_url
          ? `
            <img
              src="${esc(p.thumbnail_url)}"
              alt="${esc(p.nom)}"
              loading="lazy"
            >
          `
          : `
            <div class="ls-card-noimg">🖼️</div>
          `
      }

      <div class="ls-card-body">

        <b>${esc(p.nom)}</b>

        <span>
          ${esc(p.type || "")}
          ${p.environnement ? " · " + esc(p.environnement) : ""}
        </span>

        ${
          p.auteur
            ? `<span>📷 ${esc(p.auteur)}</span>`
            : ""
        }

        ${
          p.distance_km != null
            ? `<span>📍 ${p.distance_km} km</span>`
            : ""
        }

        <div class="ls-badges">

          <span class="ls-badge">
            ${MEDIA_LABELS[p.media_type] || p.media_type}
          </span>

          ${droitsBadge(p)}

        </div>

        ${informationsLicence(p)}

        ${
          p.statut_droits === "utilisable"
            ? `
              <button
                class="ls-btn ls-btn-accent"
                data-ajouter-projet="${p.id}"
              >
                Ajouter à mon projet
              </button>
            `
            : ""
        }

        ${
          p.statut_droits === "a_negocier"
            ? `
              <button
                class="ls-btn"
                data-demander-droits="${p.id}"
              >
                🔐 Demander les droits
              </button>
            `
            : ""
        }

        <button
          class="ls-card-edit-btn"
          data-editer="${p.id}"
        >
          ✏️ Modifier
        </button>

      </div>
    </div>
  `;
}

async function chargerRechercheBibliotheque() {
  if ($("resultats-recherche").dataset.charge) return;
  await lancerRechercheBibliotheque();
  $("resultats-recherche").dataset.charge = "1";
}

async function lancerRechercheBibliotheque() {
  const el = $("resultats-recherche");
  el.innerHTML = `<p class="ls-loading">Chargement…</p>`;
  const params = new URLSearchParams();
  if ($("f-type").value.trim()) params.set("type", $("f-type").value.trim());
  if ($("f-environnement").value.trim()) params.set("environnement", $("f-environnement").value.trim());
  if ($("f-ambiance").value.trim()) params.set("ambiance", $("f-ambiance").value.trim());
  if ($("f-media-type").value) params.set("media_type", $("f-media-type").value);
  if ($("f-statut-droits").value) params.set("statut_droits", $("f-statut-droits").value);
  $("f-elements").value.trim().split(",").map(s => s.trim()).filter(Boolean).forEach(e => params.append("elements", e));
  try {
    const resultats = await api(`/api/paysages?${params.toString()}`);
    el.innerHTML = resultats.length ? resultats.map(carteResultatPaysage).join("") : `<p class="ls-empty">Aucun paysage ne correspond à ces critères.</p>`;
    el.querySelectorAll("[data-editer]").forEach(btn => {
      btn.addEventListener("click", ev => {
        ev.stopPropagation();
        ouvrirEditionPaysage(btn.dataset.editer, lancerRechercheBibliotheque);
      });
    });
  } catch (e) {
    el.innerHTML = `<p class="ls-empty">Erreur : ${esc(e.message)}</p>`;
  }
}
$("btn-filtrer").addEventListener("click", lancerRechercheBibliotheque);

// ─────────────────────────────────────────────────────────────
// BIBLIOTHÈQUE — carte Leaflet
// ─────────────────────────────────────────────────────────────
let carteLeaflet = null;
async function chargerCarte() {
  if (!carteLeaflet) {
    carteLeaflet = L.map("carte-paysages").setView([43.6, 1.44], 7); // Occitanie par défaut
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", { attribution: "© OpenStreetMap" }).addTo(carteLeaflet);
  }
  try {
    const points = await api("/api/paysages/carte");
    carteLeaflet.eachLayer(l => { if (l instanceof L.Marker) carteLeaflet.removeLayer(l); });
    if (!points.length) return;
    points.forEach(p => {
      const m = L.marker([p.latitude, p.longitude]).addTo(carteLeaflet);
      m.bindPopup(`
        ${p.thumbnail_url ? `<img src="${esc(p.thumbnail_url)}" style="width:100%;max-width:180px;border-radius:6px;margin-bottom:4px">` : ""}
        <b>${esc(p.nom)}</b><br>${esc(p.type || "")}${p.ambiance ? " · " + esc(p.ambiance) : ""}
      `);
    });
    carteLeaflet.fitBounds(points.map(p => [p.latitude, p.longitude]), { maxZoom: 10 });
  } catch (e) {
    console.error("Carte paysages :", e.message);
  }
}

// ─────────────────────────────────────────────────────────────
// BIBLIOTHÈQUE — import Openverse (formulaire complet, pas un prompt())
// ─────────────────────────────────────────────────────────────
$("ov-rechercher").addEventListener("click", async () => {
  const q = $("ov-q").value.trim();
  const el = $("ov-resultats");
  if (q.length < 2) return alert("Tapez au moins 2 caractères.");
  el.innerHTML = `<p class="ls-loading">Recherche sur Openverse…</p>`;
  try {
    const data = await api(`/api/paysages/images/recherche?q=${encodeURIComponent(q)}`);
    const resultats = data.results || [];
    if (!resultats.length) { el.innerHTML = `<p class="ls-empty">Aucun résultat Openverse pour « ${esc(q)} ».</p>`; return; }
    el.innerHTML = resultats.map(r => `
      <div class="ls-card" data-id="${esc(r.id_openverse)}">
        <img src="${esc(r.thumbnail_url || r.image_url)}" alt="${esc(r.titre || '')}">
        <div class="ls-card-body">
          <b>${esc(r.titre || "Sans titre")}</b>
          <span>${esc(r.auteur || "Auteur inconnu")} · ${esc(r.licence || "")}</span>
          <button class="ls-btn ls-btn-accent" data-importer="${esc(r.id_openverse)}">Importer</button>
        </div>
      </div>`).join("");
    el.querySelectorAll("[data-importer]").forEach(btn => {
      btn.addEventListener("click", () => ouvrirFormulaireImportOpenverse(resultats.find(r => String(r.id_openverse) === btn.dataset.importer)));
    });
  } catch (e) {
    el.innerHTML = `<p class="ls-empty">Erreur : ${esc(e.message)}</p>`;
  }
});

function ouvrirFormulaireImportOpenverse(r) {
  if (!r) return;
  ouvrirModal(`
    <h3>Importer « ${esc(r.titre || "ce paysage")} »</h3>
    <div class="ls-form" style="max-width:none;border:none;padding:0;margin:0">
      <input type="text" id="i-nom" placeholder="Nom" value="${esc(r.titre || 'Paysage sans titre')}">
      <div class="ls-form-row">
        <input type="text" id="i-type" placeholder="Type (ex : montagne, urbain...)">
        <input type="text" id="i-environnement" placeholder="Environnement">
      </div>
      <div class="ls-form-row">
        <input type="text" id="i-ambiance" placeholder="Ambiance">
        <input type="text" id="i-elements" placeholder="Éléments (virgules)">
      </div>
      <div class="ls-form-row">
        <input type="number" step="any" id="i-lat" placeholder="Latitude">
        <input type="number" step="any" id="i-lon" placeholder="Longitude">
      </div>
      <select id="i-media-type">
        <option value="photo" selected>Photo</option>
        <option value="panorama">Panorama</option>
        <option value="360">360°</option>
        <option value="video_360">Vidéo 360°</option>
      </select>
      <p class="ls-modal-note">Sans latitude/longitude, ce paysage n'apparaîtra pas sur la carte — vous pourrez toujours les compléter plus tard via « Modifier ».</p>
      <div class="ls-form-actions">
        <button class="ls-btn ls-btn-accent" id="i-valider">Importer</button>
        <button class="ls-btn" id="i-annuler">Annuler</button>
      </div>
    </div>
  `);

  $("i-annuler").addEventListener("click", fermerModal);
  $("i-valider").addEventListener("click", async () => {
    const nom = $("i-nom").value.trim();
    if (!nom) return alert("Le nom est requis.");
    try {
      await api("/api/paysages/depuis-openverse", {
        method: "POST",
        body: JSON.stringify({
          id_openverse: String(r.id_openverse),
          titre: r.titre || null,
          image_url: r.image_url,
          thumbnail_url: r.thumbnail_url || null,
          auteur: r.auteur || null,
          auteur_url: r.auteur_url || null,
          licence: r.licence || null,
          licence_url: r.licence_url || null,
          source_nom: r.source_nom || "Openverse",
          source_url: r.source_url || null,
          nom,
          type: $("i-type").value.trim() || null,
          environnement: $("i-environnement").value.trim() || null,
          ambiance: $("i-ambiance").value.trim() || null,
          elements: $("i-elements").value.trim().split(",").map(s => s.trim()).filter(Boolean),
          latitude: $("i-lat").value ? Number($("i-lat").value) : null,
          longitude: $("i-lon").value ? Number($("i-lon").value) : null,
          media_type: $("i-media-type").value,
        }),
      });
      fermerModal();
      $("resultats-recherche").dataset.charge = "";
      alert("Paysage importé et ajouté à la bibliothèque.");
    } catch (e) { alert("Erreur : " + e.message); }
  });
}

// ─────────────────────────────────────────────────────────────
// BIBLIOTHÈQUE — ajout manuel
// ─────────────────────────────────────────────────────────────
$("m-valider").addEventListener("click", async () => {
  const nom = $("m-nom").value.trim();
  if (!nom) return alert("Le nom du paysage est requis.");
  const payload = {
    nom,
    description: $("m-description").value.trim() || null,
    type: $("m-type").value.trim() || null,
    environnement: $("m-environnement").value.trim() || null,
    ambiance: $("m-ambiance").value.trim() || null,
    elements: $("m-elements").value.trim().split(",").map(s => s.trim()).filter(Boolean),
    latitude: $("m-lat").value ? Number($("m-lat").value) : null,
    longitude: $("m-lon").value ? Number($("m-lon").value) : null,
    image_url: $("m-image-url").value.trim() || null,
    thumbnail_url: $("m-thumbnail-url").value.trim() || null,
    auteur: $("m-auteur").value.trim() || null,
    licence: $("m-licence").value.trim() || null,
    source_type: "user",
    media_type: $("m-media-type").value,
    type_reference: $("m-type-reference").value,
  };
  try {
    await api("/api/paysages", { method: "POST", body: JSON.stringify(payload) });
    ["m-nom","m-description","m-type","m-environnement","m-ambiance","m-elements","m-lat","m-lon","m-image-url","m-thumbnail-url","m-auteur","m-licence"]
      .forEach(id => $(id).value = "");
    $("resultats-recherche").dataset.charge = "";
    alert("Paysage ajouté à la bibliothèque.");
  } catch (e) { alert("Erreur : " + e.message); }
});

// ─────────────────────────────────────────────────────────────
// Démarrage
// ─────────────────────────────────────────────────────────────
chargerProjets();