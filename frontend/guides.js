/* guides.js — Annuaire public des guides et médiateurs (bouton « Annuaire des guides »).
 *
 * - Données : GET /api/guides (fiches actives).
 * - Les filtres S'ADAPTENT aux données : un groupe (langues, spécialités, publics, mobilité…)
 *   n'apparaît que si au moins un guide l'a renseigné, avec le nombre de guides concernés.
 * - Position : « Autour de moi » (géolocalisation demandée uniquement au clic) et lien avec
 *   « Mon parcours » (distance aux étapes, « intervient sur mon parcours »).
 * - Tout texte venant de la base est échappé ; les liens sont limités à http(s) et mailto.
 * - Ouverture depuis ailleurs : tout élément portant data-ouvrir-guides (="tous" | "parcours")
 *   ou window.ouvrirAnnuaireGuides({surMonParcours:true}).
 */
(function () {
  "use strict";

  const API = "/api/guides";
  const RAYON_PAR_DEFAUT_KM = 30;
  const DEPARTEMENTS = {
    "09": "Ariège", "11": "Aude", "12": "Aveyron", "30": "Gard", "31": "Haute-Garonne", "32": "Gers",
    "34": "Hérault", "46": "Lot", "48": "Lozère", "65": "Hautes-Pyrénées", "66": "Pyrénées-Orientales",
    "81": "Tarn", "82": "Tarn-et-Garonne",
  };
  const MOBILITES = { pied: "À pied", velo: "À vélo", voiture: "En voiture", transport_collectif: "Transports en commun" };

  // ── utilitaires ───────────────────────────────────────────────
  const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const norm = (v) => String(v ?? "").normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase().trim();
  const liste = (v) => (Array.isArray(v) ? v.filter((x) => x !== null && String(x).trim() !== "") : []);

  /** Lien affichable : http(s) ou mailto uniquement (jamais javascript:, data:…). */
  function lienSur(brut) {
    const t = String(brut ?? "").trim();
    if (!t || /\s/.test(t)) return null;
    if (/^https?:\/\//i.test(t)) return t;
    if (/^mailto:/i.test(t)) return t;
    if (/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(t)) return "mailto:" + t;
    if (/^[\w-]+(\.[\w-]+)+(\/.*)?$/.test(t)) return "https://" + t;
    return null;
  }

  function kmEntre(lat1, lon1, lat2, lon2) {
    const r = Math.PI / 180, R = 6371;
    const a = Math.sin(((lat2 - lat1) * r) / 2) ** 2 + Math.cos(lat1 * r) * Math.cos(lat2 * r) * Math.sin(((lon2 - lon1) * r) / 2) ** 2;
    return 2 * R * Math.asin(Math.sqrt(a));
  }
  const fmtKm = (km) => (km < 10 ? km.toFixed(1) : Math.round(km)).toString().replace(".", ",") + " km";

  function etapesParcours() {
    try {
      // `state` est la constante globale de app.js (chargé avant ce fichier).
      const src = typeof state !== "undefined" && Array.isArray(state.monParcours) ? state.monParcours : [];
      return src
        .map((e) => ({ nom: e.nom, lat: Number(e.latitude), lon: Number(e.longitude) }))
        .filter((e) => Number.isFinite(e.lat) && Number.isFinite(e.lon));
    } catch { return []; }
  }

  // ── état ──────────────────────────────────────────────────────
  let guides = null;       // null = pas encore chargé
  let erreur = null;
  let ouvreur = null;
  const etat = {
    q: "", contact: false, surParcours: false, position: null, rayonMax: 0, tri: "nom",
    f: { fonction: new Set(), departement: new Set(), langue: new Set(), specialite: new Set(), public: new Set(), mobilite: new Set() },
  };

  // Groupes de filtres : comment lire la valeur d'un guide, comment l'afficher.
  const GROUPES = [
    { cle: "fonction", titre: "Type de guide", valeurs: (g) => [g.fonction || "Guide / médiateur"], libelle: (v) => v },
    { cle: "departement", titre: "Département", valeurs: (g) => (g.departement ? [g.departement] : []), libelle: (v) => (DEPARTEMENTS[v] ? `${DEPARTEMENTS[v]} (${v})` : v) },
    { cle: "langue", titre: "Langues", valeurs: (g) => liste(g.langues), libelle: (v) => String(v).toUpperCase() === String(v) ? v : v.charAt(0).toUpperCase() + v.slice(1) },
    { cle: "specialite", titre: "Spécialités", valeurs: (g) => (liste(g.specialites_libelles).length ? liste(g.specialites_libelles) : liste(g.specialites)), libelle: (v) => v },
    { cle: "public", titre: "Publics accueillis", valeurs: (g) => liste(g.publics), libelle: (v) => String(v).replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase()) },
    { cle: "mobilite", titre: "Se déplace", valeurs: (g) => liste(g.mobilite), libelle: (v) => MOBILITES[v] || v },
  ];

  // ── calculs ───────────────────────────────────────────────────
  function enrichir(g, etapes) {
    let dPos = null, dParcours = null;
    const glat = Number(g.latitude), glon = Number(g.longitude);
    const ok = Number.isFinite(glat) && Number.isFinite(glon);
    if (ok && etat.position) dPos = kmEntre(etat.position.lat, etat.position.lon, glat, glon);
    if (ok && etapes.length) dParcours = Math.min(...etapes.map((e) => kmEntre(e.lat, e.lon, glat, glon)));
    const rayon = Number(g.rayon_intervention_km) > 0 ? Number(g.rayon_intervention_km) : RAYON_PAR_DEFAUT_KM;
    return { g, dPos, dParcours, rayon, surParcours: dParcours !== null && dParcours <= rayon };
  }

  function filtrer(etapes) {
    const q = norm(etat.q);
    let res = guides.map((g) => enrichir(g, etapes));
    res = res.filter(({ g, dPos, surParcours }) => {
      if (q && !norm([g.nom, g.commune, g.fonction, g.bio, liste(g.specialites_libelles).join(" ")].join(" ")).includes(q)) return false;
      for (const groupe of GROUPES) {
        const choix = etat.f[groupe.cle];
        if (choix.size && !groupe.valeurs(g).some((v) => choix.has(v))) return false;
      }
      if (etat.contact && !(lienSur(g.site_web) || lienSur(g.lien_contact))) return false;
      if (etat.surParcours && !surParcours) return false;
      if (etat.rayonMax && dPos !== null && dPos > etat.rayonMax) return false;
      return true;
    });
    const parNom = (a, b) => String(a.g.nom).localeCompare(String(b.g.nom), "fr");
    const dist = (x) => (etat.position ? x.dPos : x.dParcours);
    if (etat.tri === "proche") res.sort((a, b) => ((dist(a) ?? Infinity) - (dist(b) ?? Infinity)) || parNom(a, b));
    else if (etat.tri === "recent") res.sort((a, b) => String(b.g.date_creation || "").localeCompare(String(a.g.date_creation || "")) || parNom(a, b));
    else res.sort(parNom);
    return res;
  }

  function facettes() {
    // { cle: Map(valeur -> nombre de guides) } — seuls les groupes réellement renseignés comptent.
    const out = {};
    for (const groupe of GROUPES) {
      const m = new Map();
      for (const g of guides) for (const v of new Set(groupe.valeurs(g))) m.set(v, (m.get(v) || 0) + 1);
      out[groupe.cle] = m;
    }
    return out;
  }

  // ── rendu ─────────────────────────────────────────────────────
  function overlay() {
    let el = document.getElementById("guides-overlay");
    if (el) return el;
    el = document.createElement("section");
    el.id = "guides-overlay";
    el.className = "hidden";
    el.setAttribute("aria-hidden", "true");
    el.innerHTML = `<aside id="guides-panel" role="dialog" aria-modal="true" aria-labelledby="guides-titre">
        <div class="guides-header">
          <div><h2 id="guides-titre"><i class="fa-solid fa-microphone-lines" aria-hidden="true"></i> Annuaire des guides</h2>
          <p>Guides-conférenciers et médiateurs référencés en Occitanie. Pelify ne gère pas les réservations : contactez le guide directement.</p></div>
          <button type="button" id="guides-fermer" aria-label="Fermer l'annuaire des guides">✕</button>
        </div>
        <div id="guides-corps"></div>
      </aside>`;
    document.body.appendChild(el);
    el.addEventListener("click", (e) => { if (e.target === el) fermer(); });
    el.querySelector("#guides-fermer").addEventListener("click", fermer);
    el.querySelector("#guides-corps").addEventListener("click", surClic);
    el.querySelector("#guides-corps").addEventListener("input", surSaisie);
    el.querySelector("#guides-corps").addEventListener("change", surChangement);
    return el;
  }

  function rendreTout() {
    const corps = overlay().querySelector("#guides-corps");
    if (erreur) { corps.innerHTML = `<p class="guides-erreur">${esc(erreur)}</p><button type="button" class="guides-btn" data-act="recharger">Réessayer</button>`; return; }
    if (guides === null) { corps.innerHTML = `<p class="guides-note">Chargement des guides…</p>`; return; }
    if (!guides.length) { corps.innerHTML = `<p class="guides-note">Aucun guide n'est encore référencé.</p>${lienInscription()}`; return; }

    const etapes = etapesParcours();
    const fac = facettes();
    const groupes = GROUPES.filter((gr) => fac[gr.cle].size > (gr.cle === "fonction" || gr.cle === "departement" ? 1 : 0));
    const absents = GROUPES.filter((gr) => !fac[gr.cle].size && ["langue", "specialite"].includes(gr.cle)).map((gr) => gr.titre.toLowerCase());

    const blocs = groupes.map((gr) => `
      <fieldset class="guides-groupe"><legend>${esc(gr.titre)}</legend>
        <div class="guides-chips">${[...fac[gr.cle].entries()].sort((a, b) => b[1] - a[1] || String(a[0]).localeCompare(String(b[0]), "fr")).map(([v, n]) =>
          `<button type="button" class="guides-chip ${etat.f[gr.cle].has(v) ? "actif" : ""}" aria-pressed="${etat.f[gr.cle].has(v)}" data-act="chip" data-groupe="${gr.cle}" data-val="${esc(v)}">${esc(gr.libelle(v))} <small>${n}</small></button>`).join("")}</div>
      </fieldset>`).join("");

    const posBtn = etat.position
      ? `<button type="button" class="guides-chip actif" data-act="position-off"><i class="fa-solid fa-location-crosshairs" aria-hidden="true"></i> Autour de moi ✕</button>
         <label class="guides-inline">Dans un rayon de <select id="guides-rayon"><option value="0">illimité</option>${[10, 30, 50, 100].map((k) => `<option value="${k}" ${etat.rayonMax === k ? "selected" : ""}>${k} km</option>`).join("")}</select></label>`
      : `<button type="button" class="guides-chip" data-act="position"><i class="fa-solid fa-location-crosshairs" aria-hidden="true"></i> Autour de moi</button>`;
    const parcoursBtn = etapes.length
      ? `<button type="button" class="guides-chip ${etat.surParcours ? "actif" : ""}" aria-pressed="${etat.surParcours}" data-act="parcours"><i class="fa-solid fa-route" aria-hidden="true"></i> Interviennent sur mon parcours <small>${etapes.length} étape(s)</small></button>`
      : "";
    const peutProche = !!(etat.position || etapes.length);
    if (etat.tri === "proche" && !peutProche) etat.tri = "nom";

    corps.innerHTML = `
      <div class="guides-barre">
        <input type="search" id="guides-recherche" placeholder="Rechercher un nom, une commune…" value="${esc(etat.q)}" aria-label="Rechercher un guide">
        <label class="guides-inline">Trier
          <select id="guides-tri">
            <option value="nom" ${etat.tri === "nom" ? "selected" : ""}>Nom (A→Z)</option>
            <option value="proche" ${etat.tri === "proche" ? "selected" : ""} ${peutProche ? "" : "disabled"}>Le plus proche${peutProche ? "" : " (position ou parcours requis)"}</option>
            <option value="recent" ${etat.tri === "recent" ? "selected" : ""}>Récemment ajoutés</option>
          </select></label>
      </div>
      <div class="guides-chips guides-rapides">${posBtn}${parcoursBtn}
        <button type="button" class="guides-chip ${etat.contact ? "actif" : ""}" aria-pressed="${etat.contact}" data-act="contact"><i class="fa-solid fa-link" aria-hidden="true"></i> Avec un lien de contact</button></div>
      ${blocs}
      ${absents.length ? `<p class="guides-note">Les filtres « ${esc(absents.join(" » et « "))} » apparaîtront dès que les guides les auront renseignés.</p>` : ""}
      <div class="guides-resume"><span id="guides-compte" role="status" aria-live="polite"></span>
        <button type="button" class="guides-lien" data-act="reset">Réinitialiser les filtres</button></div>
      <div id="guides-liste"></div>
      ${lienInscription()}`;
    rendreListe();
  }

  function lienInscription() {
    return `<p class="guides-inscription"><a href="/devenir-guide.html" target="_blank" rel="noopener noreferrer">Vous êtes guide ou médiateur ? Inscrivez-vous à l'annuaire →</a></p>`;
  }

  function carte({ g, dPos, dParcours, rayon, surParcours }) {
    const specs = liste(g.specialites_libelles).length ? liste(g.specialites_libelles) : liste(g.specialites);
    const chips = [
      ...specs.map((x) => `<span class="guide-tag">${esc(x)}</span>`),
      ...liste(g.langues).map((x) => `<span class="guide-tag langue">${esc(x)}</span>`),
      ...liste(g.publics).map((x) => `<span class="guide-tag">${esc(String(x).replace(/_/g, " "))}</span>`),
      ...liste(g.mobilite).map((x) => `<span class="guide-tag">${esc(MOBILITES[x] || x)}</span>`),
    ].join("");
    const lieu = [g.commune, g.departement ? (DEPARTEMENTS[g.departement] || g.departement) : ""].filter(Boolean).filter((v, i, t) => t.indexOf(v) === i).join(", ");
    const site = lienSur(g.site_web), contact = lienSur(g.lien_contact);
    const dist = [
      dPos !== null ? `<span class="guide-dist"><i class="fa-solid fa-location-crosshairs" aria-hidden="true"></i> À ${fmtKm(dPos)} de vous</span>` : "",
      dParcours !== null ? `<span class="guide-dist ${surParcours ? "ok" : ""}"><i class="fa-solid fa-route" aria-hidden="true"></i> ${surParcours ? "Intervient sur votre parcours" : `À ${fmtKm(dParcours)} de votre parcours (zone : ${rayon} km)`}</span>` : "",
    ].join("");
    const meta = [
      g.tarif_indicatif ? `<span><i class="fa-solid fa-euro-sign" aria-hidden="true"></i> ${esc(g.tarif_indicatif)}</span>` : "",
      Number(g.capacite_max) > 0 ? `<span><i class="fa-solid fa-users" aria-hidden="true"></i> Jusqu'à ${Number(g.capacite_max)} personnes</span>` : "",
      `<span><i class="fa-solid fa-bullseye" aria-hidden="true"></i> Zone d'intervention : ${rayon} km</span>`,
    ].filter(Boolean).join("");
    const actions = [
      site ? `<a class="guides-btn" href="${esc(site)}" target="_blank" rel="noopener noreferrer"><i class="fa-solid fa-globe" aria-hidden="true"></i> Site web</a>` : "",
      contact && contact !== site ? `<a class="guides-btn" href="${esc(contact)}" ${contact.startsWith("mailto:") ? "" : 'target="_blank" rel="noopener noreferrer"'}><i class="fa-solid fa-envelope" aria-hidden="true"></i> Contacter</a>` : "",
    ].join("");
    const photo = /^https?:\/\//i.test(g.photo_url || "") ? `<img class="guide-photo" src="${esc(g.photo_url)}" alt="" loading="lazy" referrerpolicy="no-referrer">` : "";
    return `<article class="guide-card">
      ${photo}
      <div class="guide-corps">
        <h3>${esc(g.nom)}</h3>
        <p class="guide-fonction"><i class="fa-solid fa-user-tie" aria-hidden="true"></i> ${esc(g.fonction || "Guide / médiateur")}</p>
        ${lieu ? `<p class="guide-lieu"><i class="fa-solid fa-location-dot" aria-hidden="true"></i> ${esc(lieu)}</p>` : ""}
        ${dist ? `<p class="guide-dists">${dist}</p>` : ""}
        ${chips ? `<p class="guide-tags">${chips}</p>` : ""}
        ${g.bio ? `<details class="guide-bio"><summary>À propos</summary><p>${esc(g.bio)}</p></details>` : ""}
        <p class="guide-meta">${meta}</p>
        <div class="guide-actions">${actions || `<span class="guides-note">Aucun lien de contact renseigné.</span>`}</div>
      </div></article>`;
  }

  function rendreListe() {
    const zone = document.getElementById("guides-liste");
    if (!zone || guides === null) return;
    const res = filtrer(etapesParcours());
    const compte = document.getElementById("guides-compte");
    if (compte) compte.textContent = `${res.length} guide${res.length > 1 ? "s" : ""} sur ${guides.length}`;
    zone.innerHTML = res.length
      ? res.map(carte).join("")
      : `<p class="guides-note">Aucun guide ne correspond à ces filtres. <button type="button" class="guides-lien" data-act="reset">Réinitialiser</button></p>`;
  }

  function majChips() {
    overlay().querySelectorAll('[data-act="chip"]').forEach((b) => {
      const actif = etat.f[b.dataset.groupe].has(b.dataset.val);
      b.classList.toggle("actif", actif);
      b.setAttribute("aria-pressed", String(actif));
    });
    const bouton = (act, on) => { const b = overlay().querySelector(`[data-act="${act}"]`); if (b) { b.classList.toggle("actif", on); b.setAttribute("aria-pressed", String(on)); } };
    bouton("contact", etat.contact); bouton("parcours", etat.surParcours);
  }

  // ── interactions ──────────────────────────────────────────────
  function surClic(e) {
    const b = e.target.closest("[data-act]");
    if (!b) return;
    const act = b.dataset.act;
    if (act === "chip") {
      const ens = etat.f[b.dataset.groupe];
      ens.has(b.dataset.val) ? ens.delete(b.dataset.val) : ens.add(b.dataset.val);
      majChips(); rendreListe();
    } else if (act === "contact") { etat.contact = !etat.contact; majChips(); rendreListe(); }
    else if (act === "parcours") { etat.surParcours = !etat.surParcours; if (etat.surParcours) etat.tri = "proche"; rendreTout(); }
    else if (act === "position") { demanderPosition(b); }
    else if (act === "position-off") { etat.position = null; etat.rayonMax = 0; rendreTout(); }
    else if (act === "reset") { reinitialiser(); rendreTout(); }
    else if (act === "recharger") { charger(true); }
  }
  function surSaisie(e) { if (e.target.id === "guides-recherche") { etat.q = e.target.value; rendreListe(); } }
  function surChangement(e) {
    if (e.target.id === "guides-tri") { etat.tri = e.target.value; rendreListe(); }
    if (e.target.id === "guides-rayon") { etat.rayonMax = Number(e.target.value) || 0; rendreListe(); }
  }

  function demanderPosition(bouton) {
    if (!navigator.geolocation) { alert("La géolocalisation n'est pas disponible sur cet appareil."); return; }
    bouton.disabled = true;
    navigator.geolocation.getCurrentPosition(
      (pos) => { etat.position = { lat: pos.coords.latitude, lon: pos.coords.longitude }; etat.tri = "proche"; rendreTout(); },
      () => { bouton.disabled = false; alert("Pelify n'a pas pu accéder à votre position. Vous pouvez toujours filtrer par département."); },
      { enableHighAccuracy: false, timeout: 10000, maximumAge: 300000 }
    );
  }

  function reinitialiser() {
    etat.q = ""; etat.contact = false; etat.surParcours = false; etat.rayonMax = 0; etat.tri = (etat.position || etapesParcours().length) ? "proche" : "nom";
    Object.values(etat.f).forEach((s) => s.clear());
  }

  async function charger(force) {
    if (guides !== null && !force) return;
    erreur = null; guides = null; rendreTout();
    try {
      const r = await fetch(API, { headers: { Accept: "application/json" } });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const data = await r.json();
      guides = Array.isArray(data.guides) ? data.guides : [];
    } catch (e) {
      guides = null;
      erreur = "Impossible de charger l'annuaire pour le moment. Vérifiez votre connexion puis réessayez.";
    }
    rendreTout();
  }

  function ouvrir(options = {}) {
    const el = overlay();
    ouvreur = options.opener || document.activeElement;
    if (options.surMonParcours && etapesParcours().length) { etat.surParcours = true; etat.tri = "proche"; }
    el.classList.remove("hidden");
    el.setAttribute("aria-hidden", "false");
    document.addEventListener("keydown", surTouche);
    charger(false).then(() => { rendreTout(); el.querySelector("#guides-fermer")?.focus(); });
    if (guides !== null) { rendreTout(); el.querySelector("#guides-fermer")?.focus(); }
  }

  function fermer() {
    const el = document.getElementById("guides-overlay");
    if (!el) return;
    el.classList.add("hidden");
    el.setAttribute("aria-hidden", "true");
    document.removeEventListener("keydown", surTouche);
    if (ouvreur && typeof ouvreur.focus === "function") ouvreur.focus();
  }
  function surTouche(e) { if (e.key === "Escape") fermer(); }

  document.addEventListener("click", (e) => {
    const b = e.target.closest("[data-ouvrir-guides]");
    if (!b) return;
    e.preventDefault();
    ouvrir({ surMonParcours: b.dataset.ouvrirGuides === "parcours", opener: b });
  });
  window.ouvrirAnnuaireGuides = ouvrir;
  window.fermerAnnuaireGuides = fermer;
})();