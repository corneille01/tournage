// ═══════════════════════════════════════════════════════════════
// CinéTour — app.js (V2 : filtres, stats, clustering, popup enrichi)
// ═══════════════════════════════════════════════════════════════
const API_BASE = "";
const MON_PARCOURS_STORAGE_KEY = "pelify_mon_parcours_v41";
const MON_PARCOURS_OPTIONS_KEY = "pelify_mon_parcours_options_v43";

// Doit rester synchronisé avec ICONES_CATEGORIE dans backend/overpass.py
// ════ AMAZON PAR MARCHÉ (détection navigateur) ════
const AMAZON_DOMAINS = {
  FR:"www.amazon.fr", BE:"www.amazon.com.be", ES:"www.amazon.es", DE:"www.amazon.de",
  AT:"www.amazon.de", IT:"www.amazon.it", NL:"www.amazon.nl", SE:"www.amazon.se",
  PL:"www.amazon.pl", GB:"www.amazon.co.uk", IE:"www.amazon.co.uk", US:"www.amazon.com",
  CA:"www.amazon.ca", MX:"www.amazon.com.mx", BR:"www.amazon.com.br", JP:"www.amazon.co.jp",
  IN:"www.amazon.in", AU:"www.amazon.com.au", AE:"www.amazon.ae", SA:"www.amazon.sa",
  SG:"www.amazon.sg", TR:"www.amazon.com.tr",
};
function _amazonTag(domain){
  if (domain.endsWith(".com")) return "pelify-20";
  if (domain.endsWith(".ca"))  return "pelify-20";
  return "pelify-21";
}
function _detectCountry(){
  for (const l of (navigator.languages || [navigator.language || ""])) {
    const m = l.toUpperCase().match(/-([A-Z]{2})$/);
    if (m && AMAZON_DOMAINS[m[1]]) return m[1];
  }
  const uiToCC = { fr:"FR", es:"ES", de:"DE", "en-GB":"GB", "en-US":"US", zh:"US" };
  return uiToCC[langueCourante] || "FR";
}
function getAmazonSearch(title){
  const domain = AMAZON_DOMAINS[_detectCountry()] || "www.amazon.fr";
  return `https://${domain}/s?k=${encodeURIComponent(title||"")}&i=instant-video&tag=${_amazonTag(domain)}`;
}

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

  aerodrome: {
    emoji: "🛩️",
    couleur: "#7209b7",
    label: "Aérodrome le plus proche"
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
  marqueursEtapes: [],
  parcoursV4AmenityMarkers: [],
  monParcours: [],
  monParcoursDernierCalcul: null,
  monParcoursOptions: { mode: "driving-car", temps: 240, visite: 45, departType: "premiere", depart: null, retour: false, categories: [], budget: "equilibre", accessibilite: false, optimiser: false, inclureVisitesGuidees: true, heureDepart: "09:00", dateSortie: new Date().toISOString().slice(0,10), budgetMax: null },
};

let map, clusterGroup, clusterActivites;
let coucheIsochrone = null;
let isochronesParLieu = {};
let modeIsochroneCourant = "driving-car";
let minutesIsochroneCourantes = 10;

async function actualiserProfilPelify(){
  try { const r=await fetch(`${API_BASE}/api/me`,{credentials:"include"}); const d=await r.json(); const el=document.getElementById("pelify-historique-compteur"); if(el) el.textContent=String(d.historique_parcours||0); } catch {}
}

// ════ MON PARCOURS V4.1 — stockage local et interface globale ════
function chargerMonParcoursStocke() {
  try {
    const data = JSON.parse(localStorage.getItem(MON_PARCOURS_STORAGE_KEY) || "[]");
    state.monParcours = Array.isArray(data) ? data : [];
  } catch { state.monParcours = []; }
  normaliserMonParcours();
}
function sauvegarderMonParcours() {
  try { localStorage.setItem(MON_PARCOURS_STORAGE_KEY, JSON.stringify(state.monParcours)); } catch (e) { console.warn(e); }
  mettreAJourCompteurMonParcours();
}
function chargerOptionsMonParcours() {
  try {
    const data = JSON.parse(localStorage.getItem(MON_PARCOURS_OPTIONS_KEY) || "null");
    if (data && typeof data === "object") state.monParcoursOptions = { ...state.monParcoursOptions, ...data };
  } catch {}
}
function sauvegarderOptionsMonParcours() {
  try { localStorage.setItem(MON_PARCOURS_OPTIONS_KEY, JSON.stringify(state.monParcoursOptions)); } catch {}
}
function normaliserMonParcours() {
  const vus = new Set();
  state.monParcours = state.monParcours.filter((x) => {
    const id=Number(x?.id), lat=Number(x?.latitude), lon=Number(x?.longitude);
    if (!id || !Number.isFinite(lat) || !Number.isFinite(lon) || vus.has(id)) return false;
    vus.add(id); x.id=id; x.latitude=lat; x.longitude=lon; return true;
  });
}
function mettreAJourCompteurMonParcours() {
  const c=document.getElementById("mon-parcours-compteur"); if(c) c.textContent=String(state.monParcours.length);
  const b=document.getElementById("btn-ajouter-parcours"), id=Number(document.getElementById("popup-overlay")?.dataset?.lieuId);
  if(b && id){ const ok=state.monParcours.some(x=>Number(x.id)===id); b.classList.toggle("ajoute",ok); b.textContent=ok?"✓ Retirer de mon parcours":"＋ Ajouter à mon parcours"; }
}
function lieuPourMonParcours(film, lieu){ return {id:Number(lieu.id),nom:lieu.nom||"Lieu de tournage",commune:lieu.commune||"",departement:lieu.departement||"",latitude:Number(lieu.latitude??lieu.lat),longitude:Number(lieu.longitude??lieu.longitude??lieu.lon??lieu.lng),film_id:Number(film?.id)||null,film_titre:film?.titre||"",media_type:film?.media_type||"",annee:film?.annee||null,poster_url:film?.poster_url||""}; }
function ajouterLieuAuParcours(film,lieu){ const item=lieuPourMonParcours(film,lieu); if(!item.id||!Number.isFinite(item.latitude)||!Number.isFinite(item.longitude))return false; if(!state.monParcours.some(x=>Number(x.id)===item.id)){state.monParcours.push(item);sauvegarderMonParcours();} mettreAJourCompteurMonParcours();return true; }
function retirerLieuDuParcours(id){ const n=state.monParcours.length; state.monParcours=state.monParcours.filter(x=>Number(x.id)!==Number(id)); if(n!==state.monParcours.length)sauvegarderMonParcours(); mettreAJourCompteurMonParcours(); }
function basculerLieuDansMonParcours(film,lieu){ const id=Number(lieu.id); if(state.monParcours.some(x=>Number(x.id)===id))retirerLieuDuParcours(id);else ajouterLieuAuParcours(film,lieu); afficherMonParcoursPanel(); mettreAJourCompteurMonParcours(); }
function deplacerLieuDansMonParcours(i,d){const j=i+d;if(j<0||j>=state.monParcours.length)return;[state.monParcours[i],state.monParcours[j]]=[state.monParcours[j],state.monParcours[i]];sauvegarderMonParcours();afficherMonParcoursPanel();}
function ouvrirMonParcours(){const o=document.getElementById("mon-parcours-overlay");if(!o)return;afficherMonParcoursPanel();o.classList.remove("hidden");o.setAttribute("aria-hidden","false");}
function fermerMonParcours(){const o=document.getElementById("mon-parcours-overlay");if(!o)return;o.classList.add("hidden");o.setAttribute("aria-hidden","true");}
function afficherMonParcoursPanel(){
  const c=document.getElementById("mon-parcours-contenu");if(!c)return;
  mettreAJourCompteurMonParcours();
  const o=state.monParcoursOptions;
  const parcoursVideHtml=!state.monParcours.length?`<div class="mon-parcours-vide"><div class="mon-parcours-vide-icone">🎬</div><h3>Construisez votre sortie autrement</h3><p>Vous pouvez ajouter des lieux depuis la carte, <b>ou partir directement de vos critères</b> pour que Pelify vous propose des possibilités.</p></div>`:"";
  const liste=state.monParcours.map((l,i)=>`<article class="mon-parcours-etape"><div class="mon-parcours-numero">${i+1}</div><div class="mon-parcours-etape-info"><strong>${escapeHtml(l.nom)}</strong><small>${escapeHtml([l.commune,l.departement].filter(Boolean).join(", "))}</small>${l.film_titre?`<small>🎬 ${escapeHtml(l.film_titre)}</small>`:""}</div><div class="mon-parcours-etape-actions"><button type="button" data-action="up" data-index="${i}" title="Monter">↑</button><button type="button" data-action="down" data-index="${i}" title="Descendre">↓</button><button type="button" data-action="remove" data-index="${i}" title="Retirer">✕</button></div></article>`).join("");
  c.innerHTML=`
    ${parcoursVideHtml}
    <div class="mon-parcours-resume-top"><b>${state.monParcours.length} étape${state.monParcours.length>1?"s":""}${state.monParcours.length?" sélectionnée"+(state.monParcours.length>1?"s":""):""}</b>${state.monParcours.length?'<button type="button" id="btn-effacer-mon-parcours" class="btn-texte-danger">Tout effacer</button>':''}</div>
    <div class="mon-parcours-liste">${liste}</div>
    <section class="mon-parcours-criteres">
      <h3>⚙️ Personnalisez votre sortie</h3>
      <label class="mp-label">Point de départ</label>
      <div class="mp-depart-options">
        <label><input type="radio" name="mp-depart-type" value="premiere" ${o.departType==='premiere'?'checked':''}> 📍 Première étape</label>
        <label><input type="radio" name="mp-depart-type" value="position" ${o.departType==='position'?'checked':''}> 📱 Ma position</label>
        <label><input type="radio" name="mp-depart-type" value="adresse" ${o.departType==='adresse'?'checked':''}> 🏠 Une adresse</label>
      </div>
      <div id="mp-adresse-bloc" class="mp-adresse-bloc ${o.departType==='adresse'?'':'hidden'}">
        <div class="mp-adresse-ligne"><input id="mp-adresse-input" type="search" placeholder="Adresse, commune, lieu…" value="${escapeAttr(o.depart?.nom||'')}"><button id="mp-adresse-rechercher" type="button">Rechercher</button></div>
        <div id="mp-adresse-resultats"></div>
      </div>
      <div id="mp-depart-selection" class="mp-selection-info">${o.depart?.nom&&o.departType!=='premiere'?`Départ : <b>${escapeHtml(o.depart.nom)}</b>`:`Départ : <b>${o.departType==='position'?'votre position':(state.monParcours[0]?.nom||'première étape')}</b>`}</div>

      <label class="mp-label">Temps disponible</label>
      <select id="mp-temps"><option value="60" ${o.temps===60?'selected':''}>1 heure</option><option value="120" ${o.temps===120?'selected':''}>2 heures</option><option value="180" ${o.temps===180?'selected':''}>3 heures</option><option value="240" ${o.temps===240?'selected':''}>4 heures</option><option value="360" ${o.temps===360?'selected':''}>6 heures</option><option value="480" ${o.temps===480?'selected':''}>8 heures</option><option value="720" ${o.temps===720?'selected':''}>12 heures</option><option value="1440" ${o.temps===1440?'selected':''}>Toute la journée</option></select>
      <label class="mp-label">Temps de visite moyen par lieu</label>
      <select id="mp-visite"><option value="15" ${o.visite===15?'selected':''}>15 min</option><option value="30" ${o.visite===30?'selected':''}>30 min</option><option value="45" ${o.visite===45?'selected':''}>45 min</option><option value="60" ${o.visite===60?'selected':''}>1 h</option><option value="90" ${o.visite===90?'selected':''}>1 h 30</option></select>
      <label class="mp-label">Date de la sortie</label><input id="mp-date-sortie" type="date" value="${escapeAttr(o.dateSortie||new Date().toISOString().slice(0,10))}" class="mp-time-input"><small class="mp-aide">La date permet à Pelify de vérifier les créneaux publiés des visites guidées.</small><label class="mp-label">Heure de départ</label><input id="mp-heure-depart" type="time" value="${escapeAttr(o.heureDepart||'09:00')}" class="mp-time-input"><label class="mp-label">Budget maximum indicatif</label><select id="mp-budget-max"><option value="" ${!o.budgetMax?'selected':''}>Sans plafond</option><option value="20" ${o.budgetMax===20?'selected':''}>20 €</option><option value="40" ${o.budgetMax===40?'selected':''}>40 €</option><option value="60" ${o.budgetMax===60?'selected':''}>60 €</option><option value="100" ${o.budgetMax===100?'selected':''}>100 €</option></select><label class="mp-label">Moyen de déplacement</label>
      <div class="mp-depart-options"><label><input type="radio" name="mon-parcours-mode" value="driving-car" ${o.mode==='driving-car'?'checked':''}> 🚗 Voiture</label><label><input type="radio" name="mon-parcours-mode" value="foot-walking" ${o.mode==='foot-walking'?'checked':''}> 🚶 À pied</label></div>
      <label class="mp-check"><input id="mp-retour" type="checkbox" ${o.retour?'checked':''}> 🔁 Revenir au point de départ</label>
      <label class="mp-label">Budget souhaité</label>
      <select id="mp-budget"><option value="economique" ${o.budget==='economique'?'selected':''}>💶 Économique</option><option value="equilibre" ${o.budget==='equilibre'?'selected':''}>⚖️ Bon équilibre</option><option value="confort" ${o.budget==='confort'?'selected':''}>✨ Confort</option></select>
      <label class="mp-check"><input id="mp-accessibilite" type="checkbox" ${o.accessibilite?'checked':''}> ♿ Privilégier les offres avec accessibilité renseignée</label>
      <label class="mp-check"><input id="mp-optimiser" type="checkbox" ${o.optimiser?'checked':''}> ✨ Optimiser automatiquement si mon temps est trop court</label>
      <label class="mp-check"><input id="mp-visites-guidees" type="checkbox" ${o.inclureVisitesGuidees!==false?'checked':''}> 🎟️ Intégrer une visite guidée lorsqu’elle est disponible</label>
      <label class="mp-label">Ce que vous souhaitez trouver autour du parcours</label>
      <div class="mp-interets">
        ${[["restaurant","🍽️ Restaurants"],["hebergement","🏨 Hébergements"],["activite","🎟️ Activités"],["office_tourisme","ℹ️ Offices de tourisme"],["parking","🅿️ Parkings"],["gare","🚆 Gares"]].map(([v,l])=>`<label><input class="mp-interet" type="checkbox" value="${v}" ${o.categories.includes(v)?'checked':''}> ${l}</label>`).join('')}
      </div>
    </section>
    <button type="button" id="btn-calculer-mon-parcours" class="btn-calculer-mon-parcours">🗺️ Calculer mon parcours</button><button type="button" id="btn-generer-parcours-ideal" class="btn-generer-parcours-ideal">✨ Générer des possibilités avec mes critères</button><div id="mp-generation-resultat"></div>
    <p class="mon-parcours-note">Le trajet est calculé avec la Géoplateforme IGN. Le temps disponible sert à vérifier si les déplacements + le temps de visite estimé tiennent dans votre créneau.</p>
    <div id="mon-parcours-resultat" class="mon-parcours-resultat"></div>`;

  c.querySelectorAll("[data-action]").forEach(b=>b.addEventListener("click",()=>{const i=Number(b.dataset.index);if(b.dataset.action==="remove")retirerLieuDuParcours(state.monParcours[i]?.id);if(b.dataset.action==="up")deplacerLieuDansMonParcours(i,-1);if(b.dataset.action==="down")deplacerLieuDansMonParcours(i,1);afficherMonParcoursPanel();}));
  document.getElementById("btn-effacer-mon-parcours")?.addEventListener("click",()=>{state.monParcours=[];sauvegarderMonParcours();nettoyerAffichageParcoursGlobal();afficherMonParcoursPanel();});
  c.querySelectorAll('input[name="mp-depart-type"]').forEach(x=>x.addEventListener('change',()=>{o.departType=x.value;if(x.value==='premiere'){o.depart=null;}else if(x.value==='position'){o.depart=null;demanderPositionPourParcours();}document.getElementById('mp-adresse-bloc')?.classList.toggle('hidden',x.value!=='adresse');afficherMonParcoursPanel();}));
  c.querySelectorAll('input[name="mon-parcours-mode"]').forEach(x=>x.addEventListener('change',()=>{o.mode=x.value;sauvegarderOptionsMonParcours();}));
  document.getElementById('mp-temps')?.addEventListener('change',e=>{o.temps=Number(e.target.value);sauvegarderOptionsMonParcours();});
  document.getElementById('mp-visite')?.addEventListener('change',e=>{o.visite=Number(e.target.value);sauvegarderOptionsMonParcours();});
  document.getElementById('mp-date-sortie')?.addEventListener('change',e=>{o.dateSortie=e.target.value;sauvegarderOptionsMonParcours();});
  document.getElementById('mp-heure-depart')?.addEventListener('change',e=>{o.heureDepart=e.target.value;sauvegarderOptionsMonParcours();});
  document.getElementById('mp-budget-max')?.addEventListener('change',e=>{o.budgetMax=e.target.value?Number(e.target.value):null;sauvegarderOptionsMonParcours();});
  document.getElementById('mp-retour')?.addEventListener('change',e=>{o.retour=e.target.checked;sauvegarderOptionsMonParcours();});
  document.getElementById('mp-budget')?.addEventListener('change',e=>{o.budget=e.target.value;sauvegarderOptionsMonParcours();});
  document.getElementById('mp-accessibilite')?.addEventListener('change',e=>{o.accessibilite=e.target.checked;sauvegarderOptionsMonParcours();});
  document.getElementById('mp-optimiser')?.addEventListener('change',e=>{o.optimiser=e.target.checked;sauvegarderOptionsMonParcours();});
  document.getElementById('mp-visites-guidees')?.addEventListener('change',e=>{o.inclureVisitesGuidees=e.target.checked;sauvegarderOptionsMonParcours();});
  c.querySelectorAll('.mp-interet').forEach(x=>x.addEventListener('change',()=>{o.categories=[...c.querySelectorAll('.mp-interet:checked')].map(x=>x.value);sauvegarderOptionsMonParcours();}));
  document.getElementById('mp-adresse-rechercher')?.addEventListener('click', rechercherAdressePourParcours);
  document.getElementById('mp-adresse-input')?.addEventListener('keydown',e=>{if(e.key==='Enter')rechercherAdressePourParcours();});
  document.getElementById("btn-calculer-mon-parcours")?.addEventListener("click",calculerMonParcoursGlobal);
  document.getElementById("btn-generer-parcours-ideal")?.addEventListener("click",genererParcoursIdeal);
}
async function genererParcoursIdeal(){
  const box=document.getElementById("mp-generation-resultat"); if(!box)return;
  const o=state.monParcoursOptions;
  const depart=construireDepartParcours();
  if(!depart){
    if(o.departType==='position'){ demanderPositionPourParcours(); return; }
    if(o.departType==='premiere' && !state.monParcours.length){
      box.innerHTML='<small class="mon-parcours-erreur">Pour générer des possibilités sans lieu sélectionné, choisissez « Ma position » ou « Une adresse » comme point de départ.</small>'; return;
    }
    box.innerHTML='<small class="mon-parcours-erreur">Choisissez un point de départ valide avant la génération automatique.</small>'; return;
  }
  box.innerHTML='<small class="mon-parcours-loading">✨ Pelify recherche des possibilités adaptées à votre temps, votre budget et votre mode de déplacement…</small>';
  try{
    const payload={lieu_ids:state.monParcours.map(x=>Number(x.id)),depart,mode:o.mode,temps_disponible_minutes:o.temps,temps_visite_minutes:o.visite,retour_depart:o.retour,date_sortie:o.dateSortie,budget_level:o.budget,budget_max_euros:o.budgetMax||null,accessibilite:o.accessibilite,categories_interet:o.categories,max_etapes:8};
    const r=await fetch(`${API_BASE}/api/parcours/generer`,{method:'POST',headers:{'Content-Type':'application/json'},credentials:'include',body:JSON.stringify(payload)});
    const d=await r.json(); if(!r.ok)throw new Error(d.detail||'Génération impossible');
    const candidats=Array.isArray(d.candidats_lieux)?d.candidats_lieux:[];
    if(!candidats.length){box.innerHTML='<small class="mon-parcours-note">Aucune possibilité n’a été trouvée autour de votre point de départ avec ces critères.</small>';return;}
    window._pelifyCandidatsGeneration=candidats;
    window._pelifyScenariosGeneration=Array.isArray(d.scenarios)?d.scenarios:[];
    const deja=new Set(state.monParcours.map(x=>Number(x.id)));
    const ajouterLieux=(liste)=>{
      (liste||[]).forEach(x=>{
        if(!x || !x.id || state.monParcours.some(l=>Number(l.id)===Number(x.id))) return;
        state.monParcours.push({id:Number(x.id),nom:x.nom||'Lieu de tournage',commune:x.commune||'',departement:x.departement||'',latitude:Number(x.latitude),longitude:Number(x.longitude),film_id:x.film_id?Number(x.film_id):null,film_titre:x.film_titre||'',media_type:x.media_type||'',annee:x.annee||null,poster_url:x.poster_url||''});
      });
      normaliserMonParcours(); sauvegarderMonParcours(); afficherMonParcoursPanel();
      const b=document.getElementById('mp-generation-resultat'); if(b)b.innerHTML='<small class="mon-parcours-note">✨ Sélection ajoutée. Vous pouvez encore modifier les étapes avant le calcul IGN.</small>';
    };
    const scenarioCards=(d.scenarios||[]).map((sc,i)=>{
      const compat=sc.compatible_temps?'compatible':'depasse';
      const films=(sc.films||[]).slice(0,4).map(f=>`<span class="mp-scenario-film">🎬 ${escapeHtml(f)}</span>`).join('');
      const lieux=(sc.lieux||[]).map(x=>`<span class="mp-scenario-lieu">${escapeHtml(x.nom||'Lieu')}${x.commune?` · ${escapeHtml(x.commune)}`:''}</span>`).join('');
      return `<article class="mp-scenario ${compat}"><div class="mp-scenario-head"><div><span class="mp-scenario-badge">Scénario ${i+1}</span><h5>${escapeHtml(sc.titre||'Parcours proposé')}</h5></div><strong>${sc.nb_etapes||0} lieux</strong></div><p class="mp-scenario-stats">📏 ${formatDistance(sc.distance_approx_metres||0)} · ⏱️ ${formatDuree((sc.temps_approx_minutes||0)*60)} · 📍 ${escapeHtml(String(sc.distance_depuis_depart_km??0))} km du départ</p><div class="mp-scenario-lieux">${lieux}</div><div class="mp-scenario-films">${films}</div><small>${escapeHtml(sc.note||'')}</small><button type="button" class="mp-scenario-choisir" data-scenario-index="${i}">✨ Choisir ce scénario</button></article>`;
    }).join('');
    const dejaTxt=deja.size?` ${deja.size} lieu${deja.size>1?'x':''} déjà dans votre parcours.`:'';
    const candidatsHtml=candidats.map((x,i)=>`<label class="mp-possibilite"><input type="checkbox" class="mp-candidat-check" data-index="${i}" ${deja.has(Number(x.id))?'checked':''}><span><b>${escapeHtml(x.nom||'Lieu de tournage')}</b><small>${escapeHtml([x.commune,x.departement].filter(Boolean).join(', '))}${x.film_titre?` · 🎬 ${escapeHtml(x.film_titre)}`:''}</small></span></label>`).join('');
    box.innerHTML=`<section class="mp-possibilites"><h4>✨ Scénarios compatibles avec vos critères</h4><p class="mp-generation-note">Pelify a trouvé ${candidats.length} lieux candidats.${dejaTxt} Les estimations ci-dessous sont indicatives ; le trajet réel sera recalculé par l'IGN après votre choix.</p><div class="mp-scenarios">${scenarioCards||'<p class="mon-parcours-note">Aucun scénario cohérent n’a pu être constitué avec ces critères.</p>'}</div><details class="mp-tous-candidats"><summary>Voir les ${candidats.length} lieux candidats</summary><div class="mp-candidats-liste">${candidatsHtml}</div><button type="button" id="mp-ajouter-candidats" class="btn-generer-parcours-ideal">＋ Ajouter les possibilités sélectionnées</button></details></section>`;
    box.querySelectorAll('.mp-scenario-choisir').forEach(btn=>btn.addEventListener('click',()=>{const sc=window._pelifyScenariosGeneration?.[Number(btn.dataset.scenarioIndex)];if(sc)ajouterLieux(sc.lieux);}));
    document.getElementById('mp-ajouter-candidats')?.addEventListener('click',()=>{
      const indices=[...document.querySelectorAll('.mp-candidat-check:checked')].map(x=>Number(x.dataset.index));
      ajouterLieux(indices.map(i=>window._pelifyCandidatsGeneration?.[i]).filter(Boolean));
    });
  }catch(e){box.innerHTML=`<small class="mon-parcours-erreur">${escapeHtml(e.message||'Génération impossible')}</small>`;}
}
function demanderPositionPourParcours(){
  if(!navigator.geolocation){alert("La géolocalisation n'est pas disponible dans ce navigateur.");return;}
  navigator.geolocation.getCurrentPosition(pos=>{state.monParcoursOptions.depart={nom:'Ma position',latitude:pos.coords.latitude,longitude:pos.coords.longitude};afficherMonParcoursPanel();},()=>alert("Pelify n'a pas pu accéder à votre position. Vous pouvez choisir une adresse à la place."),{enableHighAccuracy:true,timeout:10000,maximumAge:60000});
}
async function rechercherAdressePourParcours(){
  const input=document.getElementById('mp-adresse-input'), box=document.getElementById('mp-adresse-resultats'); if(!input||!box)return;
  const q=input.value.trim(); if(q.length<3){box.innerHTML='<small>Entrez au moins 3 caractères.</small>';return;}
  box.innerHTML='<small>Recherche de l’adresse…</small>';
  try{const res=await fetch(`${API_BASE}/api/geocodage?q=${encodeURIComponent(q)}`);const data=await res.json();if(!res.ok)throw new Error(data.detail||'Recherche impossible');
    box.innerHTML=(data.resultats||[]).map((x,i)=>`<button type="button" class="mp-adresse-resultat" data-index="${i}">${escapeHtml(x.label)}</button>`).join('')||'<small>Aucune adresse trouvée.</small>';
    box.querySelectorAll('.mp-adresse-resultat').forEach(b=>b.addEventListener('click',()=>{const x=data.resultats[Number(b.dataset.index)];state.monParcoursOptions.depart={nom:x.label,latitude:x.latitude,longitude:x.longitude};afficherMonParcoursPanel();}));
  }catch(e){box.innerHTML=`<small class="mon-parcours-erreur">${escapeHtml(e.message)}</small>`;}
}
function construireDepartParcours(){
  const o=state.monParcoursOptions;
  if(o.departType==='premiere')return null;
  return o.depart;
}
function nettoyerAffichageParcoursGlobal(){if(state.traceLayer){map.removeLayer(state.traceLayer);state.traceLayer=null;} (state.marqueursEtapes||[]).forEach(m=>map.removeLayer(m));state.marqueursEtapes=[];(state.parcoursV4AmenityMarkers||[]).forEach(m=>clusterActivites.removeLayer(m));state.parcoursV4AmenityMarkers=[];}
function construireVisitesGuideesPourParcours(){
  if(state.monParcoursOptions.inclureVisitesGuidees===false) return [];
  const vus=new Set(); const sorties=[];
  state.monParcours.forEach(l=>{
    const filmId=Number(l.film_id); if(!filmId||vus.has(filmId))return;
    const v=(VISITES_PARTENAIRES[filmId]||[]).find(x=>Number(x.duree_minutes)>0);
    if(v){vus.add(filmId);sorties.push({film_id:filmId,nom:v.nom,duree_minutes:Number(v.duree_minutes),lien:v.lien||null});}
  });
  return sorties;
}

async function calculerMonParcoursGlobal(){
  const r=document.getElementById("mon-parcours-resultat");if(!r||!state.monParcours.length)return;
  const o=state.monParcoursOptions; o.mode=document.querySelector('input[name="mon-parcours-mode"]:checked')?.value||o.mode;
  if(o.departType==='position'&&!o.depart){demanderPositionPourParcours();return;}
  if(o.departType==='adresse'&&!o.depart){r.innerHTML='<p class="mon-parcours-erreur">Choisissez une adresse de départ avant de calculer.</p>';return;}
  r.innerHTML=`<p class="mon-parcours-loading">Calcul du trajet IGN et des offres touristiques…</p>`;
  try{const body={lieu_ids:state.monParcours.map(x=>Number(x.id)),mode:o.mode,limite_par_categorie:5,depart:construireDepartParcours(),temps_disponible_minutes:o.temps,temps_visite_minutes:o.visite,retour_depart:o.retour,date_sortie:o.dateSortie,categories_interet:o.categories,budget_level:o.budget,budget_max_euros:o.budgetMax||null,accessibilite:o.accessibilite,optimiser:o.optimiser,inclure_visites_guidees:o.inclureVisitesGuidees!==false,heure_depart:o.heureDepart||"09:00",budget_max_euros:o.budgetMax||null};
    const res=await fetch(`${API_BASE}/api/parcours/enrichi`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
    const raw=await res.text();
    let data=null;
    try{data=raw?JSON.parse(raw):null;}catch(parseError){
      console.error("Réponse non JSON de /api/parcours/enrichi :",raw);
      throw new Error(res.ok?"Réponse invalide du serveur.":`Erreur serveur (${res.status}) : ${raw||"Réponse vide"}`);
    }
    if(!res.ok)throw new Error(data?.detail||data?.message||`Erreur serveur (${res.status})`);
    if(!data||typeof data!=="object")throw new Error("Réponse invalide du serveur.");
    state.monParcoursDernierCalcul=data;afficherGeometrieParcoursV4(data,false);afficherResultatMonParcours(data);
  }catch(e){console.error(e);r.innerHTML=`<p class="mon-parcours-erreur">${escapeHtml(e.message||"Erreur lors du calcul du parcours.")}</p>`;}
}
function _minutesDepuisMinuit(hhmm){const [h,m]=String(hhmm||'00:00').split(':').map(Number);return (h||0)*60+(m||0);}
function _hhmmClient(minutes){minutes=Math.round(Number(minutes)||0);minutes=((minutes%(24*60))+24*60)%(24*60);return `${String(Math.floor(minutes/60)).padStart(2,'0')}:${String(minutes%60).padStart(2,'0')}`;}
function construireScenarioCinetouristique(data){
  const planning=data.planning_horaire||[];
  const guidesDisponibles=data.visites_guidees_disponibles||[];
  const guidesPlanifies=data.visites_guidees_planifiees||[];
  const tempsBase=Number(data.temps_disponible_minutes||0);
  const tempsUtilise=Number(data.duree_totale_estimee_secondes||0)/60;
  const scenario=[];
  if(data.date_sortie) scenario.push(`📅 Sortie prévue le ${new Date(data.date_sortie+'T12:00:00').toLocaleDateString('fr-FR')}, à partir de ${data.heure_depart||'09:00'}.`);
  if(data.scenario_recommande?.message) scenario.push(`🎯 ${data.scenario_recommande.message}`);
  if(guidesPlanifies.length){
    const g=guidesPlanifies[0];
    scenario.push(`🎟️ ${g.nom} est le rendez-vous guidé le plus directement intégrable à vos critères pour cette date.`);
    if(g.heure_debut) scenario.push(`Pelify vous conseille de viser le créneau ${g.heure_debut}–${g.heure_fin||''}. L'étape concernée est placée en priorité dans le scénario.`);
    if(g.lien) scenario.push(`Réservation obligatoire ou recommandée : vérifiez le créneau sur la page officielle avant de partir.`);
  } else if(guidesDisponibles.length){
    scenario.push(`🎟️ ${guidesDisponibles.length} créneau(x) de visite guidée sont référencés pour cette date. Pelify peut les afficher, mais ne confirme jamais une disponibilité de réservation.`);
  } else if(data.inclure_visites_guidees!==false){
    scenario.push(`🎟️ Aucune disponibilité datée n'est connue dans le référentiel Pelify pour cette date ; les visites éventuellement proposées restent consultables via leurs pages officielles.`);
  }
  if(tempsBase){
    const marge=Math.round(tempsBase-tempsUtilise);
    scenario.push(marge>=0?`✅ Avec vos critères, il resterait environ ${marge} min de marge estimée.`:`⚠️ Votre sélection dépasse votre créneau d’environ ${Math.abs(marge)} min.`);
  }
  const reco=data.recommandations_par_etape||{};
  for(const etape of (data.etapes||[])){
    const r=(reco[String(etape.id)]||[])[0];
    if(r){scenario.push(`💡 Pour « ${etape.nom||'votre étape'} », Pelify vous suggère ${r.nom||'une offre à proximité'} : ${r.raison||'elle correspond à vos critères'}.`);break;}
  }
  return {guides:guidesDisponibles,texte:scenario,tempsUtilise};
}
function afficherResultatMonParcours(data){
  const c=document.getElementById("mon-parcours-resultat");if(!c)return;
  const distance=Number(data.distance_metres||0);
  const duree=Number(data.duree_secondes||0);
  const total=Number(data.duree_totale_estimee_secondes||0);
  const budget=data.temps_disponible_minutes;
  const visite=data.duree_visite_estimee_secondes||0;
  const cats=data.amenities||{};
  const labels=data.labels_categories||{};
  const icones=data.icones_categories||{};
  const nb=Object.values(cats).reduce((a,x)=>a+(x?.length||0),0);
  const recs=data.recommandations_par_etape||{};
  const scenario=construireScenarioCinetouristique(data);

  const budgetHtml=budget?`<div class="mp-budget ${data.budget_respecte?'ok':'alerte'}"><b>${data.budget_respecte?'✅ Votre parcours tient dans votre créneau':'⚠️ Votre parcours dépasse votre temps disponible'}</b><span>Déplacements : ${duree?formatDuree(duree):'—'} · Visites : ${formatDuree(visite)}${data.duree_visites_guidees_secondes?` · Visites guidées : ${formatDuree(data.duree_visites_guidees_secondes)}`:''}${data.attente_visites_guidees_minutes?` · Attente : ${data.attente_visites_guidees_minutes} min`:''} · Total : ${formatDuree(total)} · Disponible : ${formatDuree(budget*60)}</span>${data.optimiser&&data.etapes_exclues_optimisation?.length?`<small>✨ ${data.etapes_exclues_optimisation.length} étape(s) ont été écartées automatiquement pour respecter vos contraintes.</small>`:''}</div>`:'';

  const ordre=["activite","restaurant","hebergement","office_tourisme","parking","gare","aeroport","refuge","arret_bus"];
  const blocs=ordre.filter(k=>cats[k]?.length).map(k=>{
    const info=icones[k]||ICONES_CATEGORIE[k]||{};
    const offres=(cats[k]||[]).map(x=>{
      const distanceTxt=x.meilleure_distance_metres!=null?`${formatDistance(x.meilleure_distance_metres)} de l'étape la plus proche`:'À proximité';
      const tarif=x.tarif_min!=null?`<small>À partir de ${escapeHtml(String(x.tarif_min))}${x.devise?' '+escapeHtml(x.devise):' €'}</small>`:'';
      const action=x.site_web?`<a class="mp-action" href="${escapeAttr(x.site_web)}" target="_blank" rel="noopener noreferrer">Voir / réserver →</a>`:(x.telephone?`<a class="mp-action" href="tel:${escapeAttr(x.telephone)}">Appeler →</a>`:'');
      return `<div class="mon-parcours-offre"><b>${escapeHtml(info.emoji||'📍')} ${escapeHtml(x.nom||'Offre touristique')}</b><small>${distanceTxt}${x.adresse?` · ${escapeHtml(x.adresse)}`:''}</small>${tarif}${x.description?`<small>${escapeHtml(x.description)}</small>`:''}${action}</div>`;
    }).join('');
    return `<section class="mon-parcours-offres-cat"><h4>${escapeHtml(labels[k]||info.label||k)}</h4>${offres}</section>`;
  }).join('');

  const etapeRecos=(data.etapes||[]).map((etape,i)=>{
    const items=(recs[String(etape.id)]||[]).slice(0,3);
    if(!items.length)return '';
    const cards=items.map(x=>{
      const info=icones[x.categorie]||ICONES_CATEGORIE[x.categorie]||{emoji:'📍'};
      const action=x.action_url?`<a class="mp-reco-action" href="${escapeAttr(x.action_url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(x.action_label||'Voir')} ↗</a>`:(x.telephone?`<a class="mp-reco-action" href="tel:${escapeAttr(x.telephone)}">Appeler ↗</a>`:'');
      return `<article class="mp-reco-card"><div><b>${escapeHtml(info.emoji||'📍')} ${escapeHtml(x.nom||'Suggestion')}</b><small>${escapeHtml(x.raison||'Suggestion personnalisée pour votre parcours.')}</small>${x.adresse?`<small>📍 ${escapeHtml(x.adresse)}</small>`:''}</div>${action}</article>`;
    }).join('');
    return `<section class="mp-reco-etape"><div class="mp-reco-etape-titre"><span class="mon-parcours-numero">${i+1}</span><div><b>${escapeHtml(etape.nom||'Étape')}</b><small>${etape.film_titre?`🎬 ${escapeHtml(etape.film_titre)}${etape.annee?` · ${escapeHtml(etape.annee)}`:''}<br>`:''}${escapeHtml([etape.commune,etape.departement].filter(Boolean).join(', '))}</small></div></div>${cards}</section>`;
  }).join('');

  const visitesGuidees=[];
  (data.visites_guidees_disponibles||[]).forEach(v=>{
    const etape=(data.etapes||[]).find(e=>(v.film_ids||[]).map(Number).includes(Number(e.film_id)));
    visitesGuidees.push({...v,film_titre:etape?.film_titre||'',ordre:etape?((data.etapes||[]).indexOf(etape)+1):null});
  });
  // Si aucune disponibilité datée n'est connue, on garde les visites éditoriales
  // déjà présentes dans le projet afin que l'utilisateur puisse les consulter.
  if(!visitesGuidees.length){
    (data.etapes||[]).forEach((etape,i)=>{
      const partenaires=VISITES_PARTENAIRES[Number(etape.film_id)]||[];
      partenaires.forEach(v=>visitesGuidees.push({...v,film_titre:etape.film_titre||'',ordre:i+1}));
    });
  }
  const visitesGuideesHtml=visitesGuidees.length?`<section class="mp-visites-guidees"><h3>🎟️ Visites guidées pertinentes</h3>${visitesGuidees.map(v=>`<article class="mp-visite-card"><div><b>${escapeHtml(v.nom)}</b><small>${v.ordre?`Étape ${v.ordre}`:''}${v.film_titre?` · ${escapeHtml(v.film_titre)}`:''}${v.duree_minutes?` · ⏱️ ${v.duree_minutes} min`:''}${v.heure_debut?` · 🕘 ${escapeHtml(v.heure_debut)}–${escapeHtml(v.heure_fin||'')}`:''}</small><small>${escapeHtml(v.description||'')}${v.date?` · 📅 ${escapeHtml(v.date)}`:''}</small>${v.heure_debut?`<small class="mp-creneau-confirme">✓ Créneau publié pour la date sélectionnée — réservation à confirmer sur le site officiel.</small>`:''}</div>${v.lien?`<a class="mp-reco-action" href="${escapeAttr(v.lienAffiliation||v.lien)}" target="_blank" rel="noopener noreferrer">📩 Réserver / voir la visite ↗</a>`:''}</article>`).join('')}</section>`:'';

  const conseil=scenario.texte.length?`<section class="mp-scenario"><h3>🎬 Votre scénario conseillé</h3>${scenario.texte.map(x=>`<p>${escapeHtml(x)}</p>`).join('')}</section>`:'';
  const budgetInfo=data.budget_estime_euros!=null?`<div class="mp-budget ${data.budget_max_respecte===false?'alerte':'ok'}"><b>💶 Budget indicatif renseigné : ${Number(data.budget_estime_euros).toFixed(2)} €</b><span>Calculé uniquement à partir des tarifs disponibles ; carburant et dépenses sans tarif renseigné ne sont pas inclus.</span></div>`:'';
  const planning=(data.planning_horaire||[]).map(x=>`<div class="mp-planning-row"><b>${escapeHtml(x.heure_arrivee)}</b><span>${escapeHtml(x.nom||'Étape')}${x.film_titre?` · 🎬 ${escapeHtml(x.film_titre)}`:''} · fin estimée ${escapeHtml(x.heure_fin_visite)}</span></div>`).join('');

  c.innerHTML=`${budgetHtml}${budgetInfo}<div class="mon-parcours-stats"><div><b>${data.nb_etapes||0}</b><span>étapes</span></div><div><b>${distance?formatDistance(distance):'—'}</b><span>trajet</span></div><div><b>${duree?formatDuree(duree):'—'}</b><span>déplacement</span></div><div><b>${nb}</b><span>offres</span></div></div>${planning?`<section class="mp-planning"><h3>🕘 Votre journée cinéma</h3>${planning}</section>`:''}${conseil}${visitesGuideesHtml}${etapeRecos?`<div class="mp-recommandations"><h3>✨ Mes suggestions étape par étape</h3>${etapeRecos}</div>`:''}<div class="mon-parcours-offres"><h3>🎟️ Toutes les offres à proximité</h3>${blocs||`<p class="mon-parcours-note">Aucune offre correspondant à vos critères n'est actuellement en cache.</p>`}</div>`;
  afficherAmenitiesParcoursV4(cats);
}
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
  // Les lieux de tournage ne doivent JAMAIS être masqués dans une bulle
  // de regroupement, même sur petit écran avec beaucoup de commodités
  // autour — layerGroup simple, pas de clustering, contrairement aux
  // commodités.
  clusterGroup = L.layerGroup();
  // Groupe SÉPARÉ pour les activités "que faire aux alentours" : ne
  // doit jamais se mélanger avec les lieux de tournage, sinon cliquer
  // sur un lieu de tournage peut ouvrir un cluster d'activités par erreur.
  clusterActivites = L.markerClusterGroup({ maxClusterRadius: 40, disableClusteringAtZoom: 15 });

  map.addLayer(clusterGroup);
  map.addLayer(clusterActivites);

  // N'importe quel clic sur une bulle de regroupement change la vue —
  // propose de revenir à la vue initiale du film sélectionné.
  clusterActivites.on("clusterclick", afficherBoutonRecentrer);
}
async function recupererIsochrones(lieuId) {

  if (isochronesParLieu[lieuId]) {
    return isochronesParLieu[lieuId];
  }

  const res = await fetch(
    `${API_BASE}/api/lieux/${lieuId}/isochrones`
  );

  if (!res.ok) {
    throw new Error(
      `Impossible de récupérer les isochrones (${res.status})`
    );
  }

  const data = await res.json();

  isochronesParLieu[lieuId] = data;

  return data;
}

function effacerIsochrone() {

  if (coucheIsochrone) {
    map.removeLayer(coucheIsochrone);
    coucheIsochrone = null;
  }
}

function afficherIsochroneSurCarte(geojson) {
  effacerIsochrone();

  if (!geojson) return;

  coucheIsochrone = L.geoJSON(geojson, {
    style: {
      color: "#3388ff",
      weight: 2,
      opacity: 0.9,
      fillOpacity: 0.16,
      dashArray: "6 4",
    }
  }).addTo(map);
}

function initialiserControlesIsochrone() {

  document
    .querySelectorAll(".iso-mode-btn")
    .forEach((button) => {

      button.addEventListener("click", () => {

        modeIsochroneCourant =
          button.dataset.mode;

        document
          .querySelectorAll(".iso-mode-btn")
          .forEach((b) =>
            b.classList.toggle(
              "actif",
              b === button
            )
          );

        const lieuId =
          document.getElementById(
            "popup-overlay"
          ).dataset.lieuId;

        if (!lieuId) return;

        afficherIsochronePourLieu(
          lieuId,
          modeIsochroneCourant,
          minutesIsochroneCourantes
        );
      });
    });


  document
    .querySelectorAll(".iso-time-btn")
    .forEach((button) => {

      button.addEventListener("click", () => {

        minutesIsochroneCourantes =
          Number(button.dataset.minutes);

        document
          .querySelectorAll(".iso-time-btn")
          .forEach((b) =>
            b.classList.toggle(
              "actif",
              b === button
            )
          );

        const lieuId =
          document.getElementById(
            "popup-overlay"
          ).dataset.lieuId;

        if (!lieuId) return;

        afficherIsochronePourLieu(
          lieuId,
          modeIsochroneCourant,
          minutesIsochroneCourantes
        );
      });
    });
}

async function afficherIsochronePourLieu(
  lieuId,
  mode = modeIsochroneCourant,
  minutes = minutesIsochroneCourantes
) {

  const conteneur = document.getElementById(
    "isochrone-legende"
  );

  try {

    conteneur.classList.add("chargement");

    const data = await recupererIsochrones(lieuId);

    const groupe =
      mode === "driving-car"
        ? data.isochrones?.voiture
        : data.isochrones?.pied;

    const isochrone = groupe?.[String(minutes)];

    if (!isochrone) {
      effacerIsochrone();

      conteneur.innerHTML = `
        <span>⚠️ Isochrone non disponible</span>
        <span>Géoplateforme IGN</span>
      `;

      return;
    }

    afficherIsochroneSurCarte(
      isochrone.geometry
    );

    conteneur.innerHTML = `
      <span>
        🟢 Zone accessible en ${minutes} min
      </span>
      <span>
        Géoplateforme IGN
      </span>
    `;

  } catch (error) {

    console.error(
      "Erreur isochrone :",
      error
    );

    effacerIsochrone();

    conteneur.innerHTML = `
      <span>⚠️ Accessibilité indisponible</span>
    `;

  } finally {

    conteneur.classList.remove("chargement");
  }
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
    remplirSelect("filtre-annee", data.annees, t("annee"));
    remplirSelect("filtre-departement", data.departements, t("departement"));
    remplirSelect("filtre-commune", data.communes, "Commune"); // no-op si l'élément n'existe pas (voir remplirSelect)
    remplirSelect("filtre-nationalite", data.nationalites, t("nationalite"));
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
  if (state.filtres.avecVisiteGuidee) {
    // Sans ça, la pagination par défaut (60 résultats, triés par
    // titre) peut exclure des films ayant pourtant une visite guidée
    // — le filtre ne peut agir que sur ce qui a été récupéré. 200 est
    // le maximum accepté par l'API, largement suffisant pour couvrir
    // tout le catalogue actuel.
    params.set("par_page", "200");
  }

  try {
    const res = await fetch(`${API_BASE}/api/films?${params}`);
    const data = await res.json();
    let films = data.films;
    if (state.filtres.avecVisiteGuidee) {
      // Filtrage côté client — VISITES_PARTENAIRES est une donnée du
      // frontend, pas connue du backend. Toute nouvelle entrée ajoutée
      // à cet objet apparaît donc automatiquement ici, sans rien
      // d'autre à faire ni côté base de données ni côté API.
      films = films.filter((f) => VISITES_PARTENAIRES[f.id]?.length > 0);
    }
    afficherCartesFilms(films);
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
    const titreCarte = film.i18n?.[langueCourante]?.titre || film.titre;
    const div = document.createElement("div");
    div.className = "carte-film";
    div.dataset.filmId = film.id;
    div.innerHTML = `
      <img src="${film.poster_url || '/icons/placeholder-poster.png'}" alt="${titreCarte}" loading="lazy">
      <div class="infos">
        <h3>${titreCarte}</h3>
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

function _lienPlateforme(plateforme, titreFilm) {
  const nom = plateforme.nom.toLowerCase();
  if (nom.includes("amazon") || nom.includes("prime video")) {
    return getAmazonSearch(titreFilm);
  }
  return plateforme.lien_affilie || plateforme.lien_repli || "#";
}

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
  // TikTok — lecteur officiel intégré, la vidéo reste hébergée chez
  // TikTok (on ne télécharge/republie jamais le fichier nous-mêmes).
  const idTiktok = url.match(/tiktok\.com\/@[\w.-]+\/video\/(\d+)/);
  if (idTiktok) {
    return `<iframe class="media-video media-tiktok" src="https://www.tiktok.com/embed/v2/${idTiktok[1]}" title="${media.legende || nomLieu}" frameborder="0" allowfullscreen loading="lazy"></iframe>`;
  }
  return `<video class="media-video" src="${url}" controls preload="metadata"></video>`;
}

let dernierLieuOuvertId = null;
let derniereCategorieAffichee = null;

function ouvrirPopupLieu(film, lieu) {
   const overlay =
    document.getElementById("popup-overlay");

  overlay.dataset.lieuId = lieu.id;
  effacerTrace();
  // Toute navigation en cours (tracé, marqueur "ma position", suivi
  // GPS) appartenait au lieu précédemment ouvert — on repart d'un
  // état propre pour ce nouveau lieu.
  nettoyerNavigation();
  document.getElementById("section-reservation").innerHTML = "";

  // Même lieu rouvert : on garde l'état (catégorie sélectionnée,
  // résultats déjà affichés). Lieu différent : on repart à zéro.
  const memeLieu = lieu.id === dernierLieuOuvertId;
  dernierLieuOuvertId = lieu.id;
  if (!memeLieu) derniereCategorieAffichee = null;

  // Traduction disponible pour la langue du navigateur, sinon repli
  // français — jamais de contenu manquant, juste moins traduit.
  const filmTexte = film.i18n?.[langueCourante] || {};
  const lieuTexte = lieu.i18n?.[langueCourante] || {};
  const titreAffiche = filmTexte.titre || film.titre;
  const synopsisAffiche = filmTexte.synopsis || film.synopsis;
  const anecdoteAffichee = lieuTexte.anecdote || lieu.anecdote;
  const descriptionLieuAffichee = lieuTexte.description_wikipedia || lieu.description_wikipedia;

  document.getElementById("popup-poster").src = film.poster_url || "/icons/placeholder-poster.png";
  document.getElementById("popup-titre").textContent = titreAffiche;
  document.getElementById("popup-meta").textContent =
    `${labelMediaType(film.media_type)}${film.nationalite ? " " + _accorderNationalite(film.nationalite, film.media_type) : ""} · ${film.annee || "année inconnue"}`;
  document.getElementById("popup-adresse").textContent =
    [lieu.nom, lieu.commune, lieu.departement].filter(Boolean).join(", ");

  // Itinéraire direct depuis la position actuelle de l'internaute vers
  // CE lieu de tournage — sans passer par une commodité. On réassigne
  // .onclick (plutôt que addEventListener) pour éviter d'empiler des
  // écouteurs obsolètes (avec les anciennes coordonnées) à chaque
  // réouverture du popup pour un autre lieu.
  const lieuLat = Number(lieu.latitude ?? lieu.lat);
  const lieuLon = Number(lieu.longitude ?? lieu.lon ?? lieu.lng);
  const btnNavDirectePied = document.getElementById("btn-nav-directe-pied");
  const btnNavDirecteVoiture = document.getElementById("btn-nav-directe-voiture");
  if (btnNavDirectePied) {
    btnNavDirectePied.onclick = () => demarrerNavigation(lieuLat, lieuLon, "pedestrian");
  }
  if (btnNavDirecteVoiture) {
    btnNavDirecteVoiture.onclick = () => demarrerNavigation(lieuLat, lieuLon, "car");
  }

  const motType = film.media_type === "movie" ? "ce film" : film.media_type === "tv" ? "cette série" : "cet animé";
  document.getElementById("btn-trace").querySelector("span").textContent = `Sur les traces de ${motType}…`;
  document.getElementById("popup-synopsis").textContent =
    lieu.description || synopsisAffiche || "Aucune description disponible.";

  const conteneurAnecdote = document.getElementById("popup-anecdote");
  conteneurAnecdote.innerHTML = anecdoteAffichee
    ? `<p class="anecdote-titre">🎬 Anecdote de tournage</p>
       <div class="anecdote-texte scrollable">${anecdoteAffichee}</div>
       ${lieu.source_anecdote ? `<a class="anecdote-source" href="${lieu.source_anecdote}" target="_blank" rel="noopener noreferrer">Source</a>` : ""}`
    : "";

  const conteneurDescriptionLieu = document.getElementById("popup-description-lieu");
  conteneurDescriptionLieu.innerHTML = descriptionLieuAffichee
    ? `<p class="anecdote-titre">📍 À propos de ce lieu</p><p class="anecdote-texte">${descriptionLieuAffichee}</p>`
    : "";

  if (!memeLieu) document.getElementById("popup-resultats").innerHTML = "";
  document.getElementById("popup-overlay").dataset.lieuId = lieu.id;
  document.getElementById("popup-overlay").dataset.filmId = film.id;
  // Restaurer systématiquement les visites guidées/leurs liens de réservation
  // lorsqu'un lieu est ouvert. Cette fonction avait été conservée mais plus appelée.
  afficherSectionReservation();

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
      <a class="plateforme-logo" href="${_lienPlateforme(p, film.titre)}" target="_blank" rel="noopener sponsored">
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
    if (memeLieu && btn.dataset.categorie === derniereCategorieAffichee) {
      btn.classList.add("actif");
    }
  });

  document.getElementById("popup-overlay").classList.remove("hidden");
  mettreAJourCompteurMonParcours();
  // Affichage automatique de l'isochrone par défaut :
// voiture + 10 minutes.
afficherIsochronePourLieu(
  lieu.id,
  modeIsochroneCourant,
  minutesIsochroneCourantes
);
}

function fermerPopup() {

  document
    .getElementById("popup-overlay")
    .classList.add("hidden");

  
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

  derniereCategorieAffichee = categorie;

  modeTriCourant = "pied"; // repart du mode par défaut à chaque catégorie choisie
  if (coucheItineraireCommodite) { map.removeLayer(coucheItineraireCommodite); coucheItineraireCommodite = null; }
  effacerTrace();

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
        <div class="nom">${estPlusProche ? "⭐ " : ""}${item.nom}${item.note_etoiles ? ` <span class="etoiles">${"⭐".repeat(Math.round(item.note_etoiles))}</span>` : ""}</div>
        <div class="distance">${_texteDistanceDynamique(item, modeTriCourant)}</div>
        ${item.adresse ? `<div class="adresse">${item.adresse}</div>` : ""}
        ${item.horaires ? `<div class="horaires">${_texteHoraires(item.horaires)}</div>` : ""}
        ${item.telephone ? `<div class="telephone">📞 ${item.telephone}</div>` : ""}
        ${item.tarif_min ? `<div class="tarif">💰 ${_texteTarif(item)}</div>` : ""}
        ${item.equipements ? `<div class="equipements">🔧 ${item.equipements}</div>` : ""}
        ${item.langues_parlees ? `<div class="langues">🗣️ ${item.langues_parlees}</div>` : ""}
        ${item.description ? `<div class="description-commodite scrollable">${item.description}</div>` : ""}
        ${item.lien_accessibilite ? `<div class="accessibilite"><a href="${item.lien_accessibilite}" target="_blank" rel="noopener noreferrer">♿ Infos accessibilité</a></div>` : ""}
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

async function afficherItineraireVersCommodite(
  bouton,
  idConteneurOverride = null
) {
  let conteneur = null;
  let texteOriginal = null;

  try {
    if (!bouton || !bouton.dataset) {
      console.error("❌ Bouton d'itinéraire invalide :", bouton);
      return;
    }

    const overlay = document.getElementById("popup-overlay");
    const lieuId = overlay?.dataset?.lieuId;

    const lieu = lieuId && Array.isArray(state.lieuxCourants)
      ? state.lieuxCourants.find(
          (l) => String(l.id) === String(lieuId)
        )
      : null;

    if (!lieu) {
      throw new Error("Lieu de tournage introuvable.");
    }

    // Départ = lieu de tournage
    const departLat = Number(
      lieu.latitude ?? lieu.lat
    );

    const departLon = Number(
      lieu.longitude ??
      lieu.lon ??
      lieu.lng
    );

    // Arrivée = commodité
    const arriveeLat = Number(
      bouton.dataset.lat ??
      bouton.dataset.latitude
    );

    const arriveeLon = Number(
      bouton.dataset.lon ??
      bouton.dataset.longitude ??
      bouton.dataset.lng
    );

    // Conversion mode frontend → backend
    const modeFrontend =
      bouton.dataset.mode || "foot-walking";

    let mode;

    if (
      modeFrontend === "foot-walking" ||
      modeFrontend === "walking" ||
      modeFrontend === "pied" ||
      modeFrontend === "pedestrian"
    ) {
      mode = "pedestrian";
    } else if (
      modeFrontend === "driving-car" ||
      modeFrontend === "voiture" ||
      modeFrontend === "car"
    ) {
      mode = "car";
    } else {
      throw new Error(
        `Mode d'itinéraire inconnu : ${modeFrontend}`
      );
    }

    if (
      !Number.isFinite(departLat) ||
      !Number.isFinite(departLon) ||
      !Number.isFinite(arriveeLat) ||
      !Number.isFinite(arriveeLon)
    ) {
      throw new Error(
        "Coordonnées du lieu ou de la commodité invalides."
      );
    }

    // ---------------------------------------------------------
    // Conteneur du résultat
    // ---------------------------------------------------------

    if (idConteneurOverride) {
      conteneur =
        document.getElementById(idConteneurOverride);
    }

    if (!conteneur) {
      const id =
        bouton.dataset.container ||
        bouton.dataset.target;

      if (id) {
        conteneur =
          document.getElementById(id);
      }
    }

    if (!conteneur) {
      conteneur =
        bouton
          .closest(".resultat-item")
          ?.querySelector(".itineraire-resultat");
    }

    if (!conteneur) {
      conteneur =
        bouton
          .closest(".leaflet-popup-content")
          ?.querySelector(".itineraire-resultat");
    }

    // ---------------------------------------------------------
    // État "calcul en cours"
    // ---------------------------------------------------------

    texteOriginal = bouton.innerHTML;

    bouton.disabled = true;
    bouton.classList.add("calcul-en-cours");

    if (conteneur) {
      conteneur.innerHTML = `
        <div class="itineraire-loading">
          ⏳ Calcul de l’itinéraire
          ${mode === "pedestrian"
            ? "à pied"
            : "en voiture"}…
        </div>
      `;
    }

    console.log(
      "🧭 Calcul itinéraire Géoplateforme IGN :",
      {
        depart: [
          departLat,
          departLon
        ],
        arrivee: [
          arriveeLat,
          arriveeLon
        ],
        mode
      }
    );

    // ---------------------------------------------------------
    // Appel API IGN
    // ---------------------------------------------------------

    const params = new URLSearchParams({
      depart_lat: String(departLat),
      depart_lon: String(departLon),
      arrivee_lat: String(arriveeLat),
      arrivee_lon: String(arriveeLon),
      mode,
      etapes: "true"
    });

    const response = await fetch(
      `${API_BASE}/api/itineraire?${params.toString()}`,
      {
        headers: {
          Accept: "application/json"
        }
      }
    );

    let data;

    try {
      data = await response.json();
    } catch {
      throw new Error(
        `Réponse invalide du serveur (${response.status}).`
      );
    }

    if (!response.ok) {
      throw new Error(
        data?.detail?.message ||
        data?.detail ||
        `Erreur HTTP ${response.status}`
      );
    }

    if (!data?.geometry) {
      throw new Error(
        "La Géoplateforme IGN n'a retourné aucun tracé."
      );
    }

    // ---------------------------------------------------------
    // Supprimer ancien itinéraire
    // ---------------------------------------------------------

    if (coucheItineraireCommodite) {
      map.removeLayer(
        coucheItineraireCommodite
      );

      coucheItineraireCommodite = null;
    }

    // ---------------------------------------------------------
    // Afficher le nouvel itinéraire
    // ---------------------------------------------------------

    coucheItineraireCommodite =
      L.geoJSON(
        data.geometry,
        {
          style: {
            weight: 6,
            opacity: 0.9,
            lineCap: "round",
            lineJoin: "round"
          }
        }
      ).addTo(map);

    const bounds =
      coucheItineraireCommodite.getBounds();

    if (bounds.isValid()) {
      map.fitBounds(
        bounds,
        {
          padding: [40, 40],
          maxZoom: 16
        }
      );
    }

    // ---------------------------------------------------------
    // Distance / durée
    // ---------------------------------------------------------

    const distanceMetres =
      Number(data.distance_metres);

    const dureeSecondes =
      Number(data.duree_secondes);

    // Infobulle "distance/temps restants" au survol du tracé.
    attacherSurvolItineraire(
      coucheItineraireCommodite,
      distanceMetres,
      dureeSecondes
    );

    const distance =
      Number.isFinite(distanceMetres)
        ? formatDistance(distanceMetres)
        : "—";

    const duree =
      Number.isFinite(dureeSecondes)
        ? formatDuree(dureeSecondes)
        : "—";

    const etapes =
      Array.isArray(data.etapes_navigation)
        ? data.etapes_navigation
        : [];

    // ---------------------------------------------------------
    // AFFICHAGE COMPLET DU RÉSULTAT
    // ---------------------------------------------------------

    if (conteneur) {

      conteneur.innerHTML = `
        <div class="itineraire-resultat-ok">

          <div class="itineraire-infos">

            <strong>
              ${
                mode === "pedestrian"
                  ? "🚶 Itinéraire à pied"
                  : "🚗 Itinéraire en voiture"
              }
            </strong>

            <span>
              📏 ${distance}
            </span>

            <span>
              ⏱️ ${duree}
            </span>

          </div>

          <div class="itineraire-source">
            🗺️ Géoplateforme IGN
          </div>

          ${
            etapes.length
              ? `
                <div class="itineraire-etapes-info">
                  🧭
                  ${etapes.length}
                  instruction${etapes.length > 1 ? "s" : ""}
                  de navigation
                </div>
              `
              : ""
          }

          <div class="itineraire-actions">

            <button
              type="button"
              class="btn-demarrer-navigation"
            >
              🧭 Démarrer la navigation
            </button>

            <button
              type="button"
              class="btn-voir-sur-carte"
            >
              🗺️ Voir sur la carte
            </button>

            <button
              type="button"
              class="btn-fermer-itineraire"
            >
              ✕ Fermer
            </button>

          </div>

        </div>
      `;

      // -------------------------------------------------------
      // Démarrer navigation
      // -------------------------------------------------------

      const btnNavigation =
        conteneur.querySelector(
          ".btn-demarrer-navigation"
        );

      if (btnNavigation) {

        btnNavigation.addEventListener(
          "click",
          () => {

            demarrerNavigation(
              arriveeLat,
              arriveeLon,
              mode
            );

          }
        );

      }

      // -------------------------------------------------------
      // Voir sur la carte
      // -------------------------------------------------------

      const btnVoirCarte =
        conteneur.querySelector(
          ".btn-voir-sur-carte"
        );

      if (btnVoirCarte) {

        btnVoirCarte.addEventListener(
          "click",
          () => {

            // Ce résultat peut s'afficher à deux endroits différents :
            // 1. dans le petit popup Leaflet ouvert directement sur la
            //    carte (marqueur de commodité) → il faut fermer CE
            //    popup-là (map.closePopup()), pas le grand panneau du
            //    lieu, sinon rien ne se passe visuellement.
            // 2. dans la liste du grand panneau lieu (#popup-overlay)
            //    → là on ferme bien le grand panneau pour révéler la
            //    carte et le tracé.
            // Dans les deux cas, le tracé lui-même n'est jamais touché.

            const dansPopupCarte =
              btnVoirCarte.closest(
                ".leaflet-popup-content"
              );

            if (dansPopupCarte) {
              map.closePopup();
            } else if (
              typeof fermerPopup ===
              "function"
            ) {
              fermerPopup();
            }

            const bounds =
              coucheItineraireCommodite
                ?.getBounds();

            if (
              bounds &&
              bounds.isValid()
            ) {
              map.fitBounds(
                bounds,
                {
                  padding: [40, 40],
                  maxZoom: 16
                }
              );
            }

          }
        );

      }

      // -------------------------------------------------------
      // Fermer le résultat
      // -------------------------------------------------------

      const btnFermer =
        conteneur.querySelector(
          ".btn-fermer-itineraire"
        );

      if (btnFermer) {

        btnFermer.addEventListener(
          "click",
          () => {

            if (
              coucheItineraireCommodite
            ) {
              map.removeLayer(
                coucheItineraireCommodite
              );

              coucheItineraireCommodite =
                null;
            }

            conteneur.innerHTML = "";

          }
        );

      }

    }

    console.log(
      "✅ Itinéraire IGN affiché :",
      {
        provider: data.provider,
        resource: data.resource,
        mode: data.mode,
        distance_metres:
          data.distance_metres,
        duree_secondes:
          data.duree_secondes,
        cache: data.cache,
        cache_hit:
          data.cache_hit
      }
    );

    return data;

  } catch (error) {

    console.error(
      "❌ Erreur itinéraire :",
      error
    );

    if (conteneur) {

      conteneur.innerHTML = `
        <div class="itineraire-erreur">

          ❌
          ${
            error?.message ||
            "Impossible de calculer l’itinéraire."
          }

          <small>
            Service : Géoplateforme IGN
          </small>

        </div>
      `;

    }

    return null;

  } finally {

    if (bouton) {

      bouton.disabled = false;

      bouton.classList.remove(
        "calcul-en-cours"
      );

      if (
        texteOriginal !== null
      ) {
        bouton.innerHTML =
          texteOriginal;
      }

    }

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
      <b>${estPlusProche ? "⭐ " : ""}${item.nom}${item.note_etoiles ? ` ${"⭐".repeat(Math.round(item.note_etoiles))}` : ""}</b><br>
      ${texteDistance} du lieu de tournage
      ${item.adresse ? `<br>${item.adresse}` : ""}
      ${item.horaires ? `<br>${_texteHoraires(item.horaires)}` : ""}
      ${item.telephone ? `<br>📞 ${item.telephone}` : ""}
      ${item.tarif_min ? `<br>💰 ${_texteTarif(item)}` : ""}
      ${item.equipements ? `<br>🔧 ${item.equipements}` : ""}
      ${item.langues_parlees ? `<br>🗣️ ${item.langues_parlees}` : ""}
      ${item.description ? `<div class="description-commodite scrollable">${item.description}</div>` : ""}
      ${item.lien_accessibilite ? `<br><a href="${item.lien_accessibilite}" target="_blank" rel="noopener noreferrer">♿ Infos accessibilité</a>` : ""}
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

function _texteTarif(item) {
  const devise = item.devise === "EUR" ? "€" : (item.devise || "");
  if (item.tarif_max && item.tarif_max !== item.tarif_min) {
    return `${item.tarif_min} — ${item.tarif_max} ${devise}`;
  }
  return `${item.tarif_min} ${devise}`;
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
  // Repli sur l'AUTRE mode plutôt que le vol d'oiseau, si disponible
  if (item.distance_pied_metres != null) return `${formatDistance(item.distance_pied_metres)} à pied (${formatDuree(item.duree_pied_secondes)})`;
  if (item.distance_voiture_metres != null) return `${formatDistance(item.distance_voiture_metres)} en voiture (${formatDuree(item.duree_voiture_secondes)})`;
  return "Calcul du trajet en cours…";
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
  if (state.marqueursEtapes) {
    state.marqueursEtapes.forEach((m) => map.removeLayer(m));
  }
  state.marqueursEtapes = [];
  if (state.parcoursV4AmenityMarkers) {
    state.parcoursV4AmenityMarkers.forEach((m) => clusterActivites.removeLayer(m));
    state.parcoursV4AmenityMarkers = [];
  }
  const conteneurResultat = document.getElementById("resultat-trace");
  if (conteneurResultat) conteneurResultat.innerHTML = "";
}

async function afficherTraceFilm() {
  const filmId = document.getElementById("popup-overlay").dataset.filmId;
  if (!filmId) return;

  const conteneurResultat = document.getElementById("resultat-trace");
  effacerTrace();

  if (state.lieuxCourants.length === 0) {
    conteneurResultat.innerHTML = `<p style="color:#9a9ea8;">Aucun lieu de tournage recensé.</p>`;
    return;
  }

  // V4 : le parcours historique reste accessible, mais devient
  // personnalisable et enrichissable avec les offres touristiques
  // déjà disponibles autour des lieux.
  conteneurResultat.innerHTML = `<p style="color:#9a9ea8;">Calcul du parcours…</p>`;

  if (state.lieuxCourants.length === 1) {
    afficherTraceEtPanelV4({
      etapes: [state.lieuxCourants[0]],
      nb_etapes: 1,
      mode: "driving-car",
      amenities: {},
      amenities_par_etape: {},
      labels_categories: {},
      icones_categories: {},
    });
    return;
  }

  try {
    const res = await fetch(`${API_BASE}/api/films/${filmId}/trace`);
    if (!res.ok) {
      conteneurResultat.innerHTML = `<p style="color:#9a9ea8;">Tracé impossible (erreur serveur).</p>`;
      return;
    }

    const data = await res.json();
    afficherTraceEtPanelV4(data);
  } catch (e) {
    console.error("Erreur trace film :", e);
    conteneurResultat.innerHTML = `<p style="color:#9a9ea8;">Erreur lors du calcul du tracé.</p>`;
  }
}

function afficherTraceEtPanelV4(data) {
  const conteneurResultat = document.getElementById("resultat-trace");
  const lieux = Array.isArray(data.etapes) ? data.etapes : [];

  if (!lieux.length) {
    conteneurResultat.innerHTML = `<p style="color:#9a9ea8;">Aucune étape disponible.</p>`;
    return;
  }

  // Parcours automatique initial : tous les lieux du film, dans l'ordre
  // calculé par le backend. L'utilisateur pourra ensuite décocher des
  // étapes et construire sa propre version.
  const distanceKm = Number(data.distance_metres || 0) / 1000;
  const dureeTxt = data.duree_secondes ? formatDuree(data.duree_secondes) : null;

  const listeSelection = lieux.map((lieu, index) => {
    const deja = state.monParcours.some(x => Number(x.id) === Number(lieu.id));
    return `<div class="parcours-v4-etape"><input type="checkbox" class="parcours-v4-check" value="${lieu.id}" checked><span class="numero">${index + 1}</span><span class="texte"><b>${escapeHtml(lieu.nom || "Lieu de tournage")}</b><small>${escapeHtml([lieu.commune, lieu.departement].filter(Boolean).join(", "))}</small></span><button type="button" class="btn-parcours-v4-ajouter ${deja?"ajoute":""}" data-lieu-id="${lieu.id}">${deja?"✓":"+"}</button></div>`;
  }).join("");

  conteneurResultat.innerHTML = `
    <p class="trace-intro">
      Parcours automatique : ${lieux.length} lieu${lieux.length > 1 ? "x" : ""}
      ${distanceKm ? `· ${distanceKm.toFixed(1)} km` : ""}
      ${dureeTxt ? `· ${dureeTxt}` : ""}.
    </p>

    <div id="panel-parcours-v4">
      <h3>🗺️ Personnaliser et enrichir le parcours</h3>
      <p class="parcours-v4-intro">
        Sélectionnez les lieux que vous souhaitez visiter. Pelify recalculera
        ensuite le trajet et affichera les hébergements, restaurants,
        activités et offices de tourisme disponibles autour des étapes.
      </p>

      <div class="parcours-v4-etapes">${listeSelection}</div>

      <div class="parcours-v4-options">
        <label><input type="radio" name="parcours-v4-mode" value="driving-car" checked> 🚗 Voiture</label>
        <label><input type="radio" name="parcours-v4-mode" value="foot-walking"> 🚶 À pied</label>
      </div>

      <div class="parcours-v4-actions">
        <button id="btn-calculer-parcours-v4">🗺️ Calculer mon parcours</button>
        <button id="btn-ajouter-tous-parcours-v4" class="secondaire">＋ Ajouter les lieux à Mon parcours</button>
        <button id="btn-tout-selectionner-v4" class="secondaire">☑️ Tout sélectionner</button>
      </div>

      <div id="parcours-v4-resultat" class="parcours-v4-resultat"></div>
    </div>
  `;

  document.getElementById("btn-calculer-parcours-v4")?.addEventListener("click", calculerParcoursV4);
  document.getElementById("btn-tout-selectionner-v4")?.addEventListener("click", () => { document.querySelectorAll(".parcours-v4-check").forEach((input) => { input.checked = true; }); });
  document.getElementById("btn-ajouter-tous-parcours-v4")?.addEventListener("click", () => { if(!state.filmSelectionne)return; lieux.forEach(l=>ajouterLieuAuParcours(state.filmSelectionne,l)); afficherTraceEtPanelV4(data); });
  document.querySelectorAll(".btn-parcours-v4-ajouter").forEach(btn=>btn.addEventListener("click",()=>{const lieu=lieux.find(x=>Number(x.id)===Number(btn.dataset.lieuId));if(!lieu||!state.filmSelectionne)return;basculerLieuDansMonParcours(state.filmSelectionne,lieu);const ok=state.monParcours.some(x=>Number(x.id)===Number(lieu.id));btn.classList.toggle("ajoute",ok);btn.textContent=ok?"✓":"+";}));

  // Affichage immédiat du parcours automatique déjà calculé.
  afficherGeometrieParcoursV4(data, true);
}

async function calculerParcoursV4() {
  const resultat = document.getElementById("parcours-v4-resultat");
  const checks = [...document.querySelectorAll(".parcours-v4-check:checked")];
  const lieuIds = checks.map((input) => Number(input.value));
  const mode = document.querySelector('input[name="parcours-v4-mode"]:checked')?.value || "driving-car";

  if (!lieuIds.length) {
    resultat.innerHTML = `<p style="color:#e76f51;">Sélectionnez au moins un lieu.</p>`;
    return;
  }

  if (lieuIds.length === 1) {
    resultat.innerHTML = `<p class="parcours-v4-loading">Un seul lieu sélectionné : Pelify affiche les offres touristiques autour de cette étape.</p>`;
  } else {
    resultat.innerHTML = `<p class="parcours-v4-loading">Calcul du trajet et recherche des offres touristiques autour des étapes…</p>`;
  }

  try {
    const res = await fetch(`${API_BASE}/api/parcours/enrichi`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        lieu_ids: lieuIds,
        mode,
        limite_par_categorie: 5,
      }),
    });

    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Impossible de calculer le parcours");

    afficherGeometrieParcoursV4(data, false);
    afficherResultatEnrichiV4(data);
  } catch (e) {
    console.error("Erreur parcours V4 :", e);
    resultat.innerHTML = `<p style="color:#e76f51;">${escapeHtml(e.message || "Erreur lors du calcul du parcours.")}</p>`;
  }
}

function afficherGeometrieParcoursV4(data, parcoursInitial = false) {
  // Nettoyage de l'ancien parcours V4.
  if (state.traceLayer) {
    map.removeLayer(state.traceLayer);
    state.traceLayer = null;
  }
  if (state.marqueursEtapes) {
    state.marqueursEtapes.forEach((m) => map.removeLayer(m));
  }
  state.marqueursEtapes = [];

  if (state.parcoursV4AmenityMarkers) {
    state.parcoursV4AmenityMarkers.forEach((m) => clusterActivites.removeLayer(m));
  }
  state.parcoursV4AmenityMarkers = [];

  if (!data.geometry || !data.etapes?.length) return;

  state.traceLayer = L.geoJSON(data.geometry, {
    style: { color: "#e63946", weight: 4, opacity: 0.82 },
  }).addTo(map);

  state.marqueursEtapes = data.etapes.map((etape, index) => {
    const icone = L.divIcon({
      html: `<div class="numero-etape">${index + 1}</div>`,
      className: "", iconSize: [28, 28], iconAnchor: [14, 14],
    });
    const marker = L.marker([etape.latitude, etape.longitude], { icon: icone });
    const mediaLabel = etape.media_type === "tv" ? "Série" : etape.media_type === "movie" ? "Film" : etape.media_type === "anime" ? "Animé" : "Œuvre";
    marker.bindPopup(`<b>Étape ${index + 1}</b><br><strong>${escapeHtml(etape.film_titre || "Lieu de tournage")}</strong><br><small>${escapeHtml(mediaLabel)}${etape.annee ? ` · ${escapeHtml(etape.annee)}` : ""}</small><br>${escapeHtml(_adresseCompleteFrontend(etape))}`);
    marker.addTo(map);
    return marker;
  });

  if (Number.isFinite(Number(data.distance_metres))) {
    attacherSurvolItineraire(state.traceLayer, data.distance_metres, data.duree_secondes);
  }

  map.fitBounds(state.traceLayer.getBounds(), { padding: [40, 40] });
}

function afficherResultatEnrichiV4(data) {
  const conteneur = document.getElementById("parcours-v4-resultat");
  if (!conteneur) return;

  const distance = Number(data.distance_metres || 0);
  const duree = Number(data.duree_secondes || 0);
  const categories = data.amenities || {};
  const labels = data.labels_categories || {};
  const icones = data.icones_categories || {};

  const nbOffres = Object.values(categories).reduce((total, items) => total + (items?.length || 0), 0);

  const resume = `
    <div class="parcours-v4-resume">
      <div class="parcours-v4-stat"><b>${data.nb_etapes}</b><span>étape${data.nb_etapes > 1 ? "s" : ""}</span></div>
      <div class="parcours-v4-stat"><b>${distance ? formatDistance(distance) : "—"}</b><span>trajet</span></div>
      <div class="parcours-v4-stat"><b>${duree ? formatDuree(duree) : "—"}</b><span>durée</span></div>
      <div class="parcours-v4-stat"><b>${nbOffres}</b><span>offres proches</span></div>
    </div>
  `;

  const ordreCategories = ["activite", "restaurant", "hebergement", "office_tourisme", "parking", "gare", "aeroport", "refuge"];
  const blocs = ordreCategories
    .filter((categorie) => Array.isArray(categories[categorie]) && categories[categorie].length)
    .map((categorie) => {
      const info = icones[categorie] || {};
      const label = labels[categorie] || categorie;
      const items = categories[categorie].map((item) => `
        <div class="parcours-v4-amenity">
          <b>${escapeHtml(info.emoji || "📍")} ${escapeHtml(item.nom || "Offre touristique")}</b>
          <small>
            ${item.meilleure_distance_metres != null ? `${formatDistance(item.meilleure_distance_metres)} de l'étape la plus proche` : "À proximité d'une étape"}
            ${item.adresse ? ` · ${escapeHtml(item.adresse)}` : ""}
          </small>
          ${item.description ? `<small>${escapeHtml(item.description)}</small>` : ""}
          ${item.site_web ? `<a href="${escapeAttr(item.site_web)}" target="_blank" rel="noopener noreferrer">Voir le site →</a>` : ""}
        </div>
      `).join("");
      return `<section class="parcours-v4-cat"><h4>${escapeHtml(label)}</h4>${items}</section>`;
    }).join("");

  conteneur.innerHTML = resume + (blocs || `<p class="parcours-v4-loading">Aucune offre touristique en cache n'est actuellement référencée autour des étapes sélectionnées.</p>`);

  afficherAmenitiesParcoursV4(categories);
}

function afficherAmenitiesParcoursV4(categories) {
  const toutes = Object.entries(categories || {}).flatMap(([categorie, items]) =>
    (items || []).map((item) => ({ categorie, item }))
  );

  const bounds = [];
  toutes.forEach(({ categorie, item }) => {
    const lat = Number(item.latitude);
    const lon = Number(item.longitude);
    if (!Number.isFinite(lat) || !Number.isFinite(lon)) return;

    const info = ICONES_CATEGORIE[categorie] || {};
    const marker = L.marker([lat, lon], {
      icon: L.divIcon({
        html: `<div class="icone-commodite" style="color:${info.couleur || "#e63946"};">${info.emoji || "📍"}</div>`,
        className: "", iconSize: [26, 26], iconAnchor: [13, 24],
      }),
    }).bindPopup(`
      <b>${escapeHtml(info.emoji || "📍")} ${escapeHtml(item.nom || "")}</b><br>
      ${item.adresse ? `${escapeHtml(item.adresse)}<br>` : ""}
      ${item.site_web ? `<a href="${escapeAttr(item.site_web)}" target="_blank" rel="noopener noreferrer">Voir le site</a>` : ""}
    `);

    clusterActivites.addLayer(marker);
    state.parcoursV4AmenityMarkers.push(marker);
    bounds.push([lat, lon]);
  });
}

function _adresseCompleteFrontend(lieu) {
  return [lieu.nom, lieu.commune, lieu.departement].filter(Boolean).join(", ");
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function escapeAttr(value) {
  const s = String(value ?? "");
  // Les URLs de données viennent de sources externes ; on évite les
  // schémas javascript/data dans l'interface générée.
  if (!/^https?:\/\//i.test(s)) return "#";
  return escapeHtml(s);
}

// ── Réglage caché : contrôle l'affichage de la section réservation
// depuis le code (pas de bouton visible pour l'utilisateur final) —
// change juste cette valeur pour masquer toute la section avant de
// partager un lien avec quelqu'un.
const AFFICHER_SECTION_RESERVATION = true;

// ── Visites déjà organisées par de vraies structures (offices de
// tourisme, guides locaux...) pour certains films/séries précis —
// un film peut avoir PLUSIEURS visites possibles (ex: Un si grand
// soleil, proposé par deux offices différents). Vide pour l'instant :
// à remplir au fur et à mesure des partenariats confirmés. Clé = id
// du film. "lienAffiliation" reste null tant qu'aucun contrat n'est
// signé avec la structure ; une fois signé, remplace juste cette
// valeur, rien d'autre à changer dans le code.
const VISITES_PARTENAIRES = {
  // Le Petit Baigneur — ciné-balade le jeudi
  15: [
    { nom: "Office de tourisme de Collioure", description: "Ciné-balade sur les traces des films tournés à Collioure, dont Le Petit Baigneur — durée publiée : 180 min pour l’édition documentée.", duree_minutes: 180, lien: "https://boutique.tourisme-collioure.com/cine-balades/cine-balades", lienAffiliation: null },
  ],
  // Les Visiteurs — visite gratuite, réservation obligatoire par mail
  24: [
    { nom: "Office de tourisme de Carcassonne", description: "Visite « La Cité Star de Cinéma » sur les lieux de tournage, dont Les Visiteurs. Durée annoncée : 60 min. Réservation obligatoire.", duree_minutes: 60, lien: "https://www.tourisme-carcassonne.fr/preparer/visites/visites-guidees/", lienAffiliation: null },
  ],
  // Demain nous appartient — deux formules de Cinétour
  128: [
    { nom: "Office de tourisme Archipel de Thau — Cinétour à pied", description: "Balade immersive depuis Le Spoon, le long des canaux jusqu'à la Pointe Courte — plus de 20 lieux emblématiques de la série. Durée annoncée : 120 min.", duree_minutes: 120, lien: "https://billetterie.archipel-thau.com/loisirs/visites-guidees-a-pied/cinetour-pedestre-dna-aujourdhui-vous-appartient", lienAffiliation: null },
    { nom: "Office de tourisme Archipel de Thau — Cinétour en bateau", description: "Balade en bateau (Canauxrama) commentée par une comédienne professionnelle, sur les traces des lieux de tournage vus à l'écran. Durée à confirmer selon la formule/date.", duree_minutes: null, lien: "https://billetterie.archipel-thau.com/loisirs/excursions-et-promenades-en-bateau/cinetour-bateau-dna-lequipage-vous-appartient", lienAffiliation: null },
  ],
  // Un si grand soleil — visite Montpellier confirmée avec page dédiée ;
  // Palavas-les-Flots propose une visite cinéma plus générale (plusieurs
  // films/séries tournés sur place), pas un circuit dédié à cette seule
  // série — honnêteté avant tout, pas de lien inventé.
  131: [
    { nom: "Office de tourisme Montpellier — Au cœur de la série (centre historique)", description: "Visite guidée dans le centre historique, avec anecdotes de tournage issues directement des plateaux. Durée annoncée : 120 min. Réservation obligatoire.", duree_minutes: 120, lien: "https://book.montpellier-tourisme.fr/fr/voir-faire/1999897/au-c%C5%93ur-de-la-s%C3%A9rie-un-si-grand-soleil-centre-historique/afficher-les-details", lienAffiliation: null },
    { nom: "Office de tourisme de Palavas-les-Flots — Visite spéciale cinéma", description: "Partez à la découverte des icônes du 7ème art qui ont foulé le sol palavasien, dont Un si grand soleil. Durée annoncée : 120 min. Rendez-vous au pied du Phare de la Méditerranée. Réservation en ligne.", duree_minutes: 120, lien: "https://billetterie.palavas-tourisme.com/fr/produit/le-cinema-a-palavas", lienAffiliation: null },
  ],
  // Killer Coaster (Palavas-les-Flots) — même visite que Un si grand
  // soleil, l'office de tourisme couvre les deux dans la même visite
  // thématique "Le cinéma à Palavas".
  136: [
    { nom: "Office de tourisme de Palavas-les-Flots — Visite spéciale cinéma", description: "Partez à la découverte des icônes du 7ème art qui ont foulé le sol palavasien, dont Killer Coaster. Durée annoncée : 120 min. Rendez-vous au pied du Phare de la Méditerranée. Réservation en ligne.", duree_minutes: 120, lien: "https://billetterie.palavas-tourisme.com/fr/produit/le-cinema-a-palavas", lienAffiliation: null },
  ],
  // L'Homme qui a vu l'ours qui a vu l'homme — pas de réservation en
  // ligne directe trouvée pour "Filets Gourmands" (inscription à
  // l'office de tourisme uniquement), lien vers leur page qui décrit
  // précisément cette visite plutôt qu'un lien de billetterie inventé
  135: [
    { nom: "Office de tourisme de Gruissan — Filets Gourmands", description: "Balade en petit train jusqu'aux cabanes de pêcheurs de l'Ayrolle, rencontre avec les pêcheurs et ostréiculteurs locaux, dégustation. Durée annoncée : 210 min (9h–12h30). Inscription à l'office de tourisme.", duree_minutes: 210, lien: "https://www.gruissan-mediterranee.com/top5-des-visites-guidees/", lienAffiliation: null },
  ],
  // D'Artagnan (Auch) — 2 visites guidées + 1 escape game, signalés
  // par Séverine Teulières (chargée de mission tourisme) en commentaire
  // du post LinkedIn du 17/08/2026 — merci à elle !
  3: [
    { nom: "Office de tourisme du Grand Auch — Visite guidée d'Artagnan", description: "Sur les traces de d'Artagnan à Auch, lieux et anecdotes liés au tournage et à l'histoire du personnage. Durée annoncée : 90 min.", duree_minutes: 90, lien: "https://www.mysportsession.com/activites/search/activity/3110/2112", lienAffiliation: null },
    { nom: "Office de tourisme du Grand Auch — Visite guidée d'Artagnan (2)", description: "Seconde formule de visite guidée autour de d'Artagnan à Auch. Durée à confirmer selon la formule/date.", duree_minutes: null, lien: "https://www.mysportsession.com/activites/search/details/activity/3108/3272", lienAffiliation: null },
    { nom: "Office de tourisme du Grand Auch — Escape game d'Artagnan", description: "Un escape game pour découvrir l'histoire de d'Artagnan à Auch, en famille ou entre amis.", lien: "https://www.mysportsession.com/activites/search/activity/3140/2129", lienAffiliation: null },
  ],
  // Candice Renoir (44) volontairement laissé vide — le Cinétour de
  // Sète trouvé concerne "Demain nous appartient", pas cette série.
  // À compléter si un vrai circuit Candice Renoir existe, ou si
  // "Demain nous appartient" rejoint la base de films.
};

function afficherSectionReservation() {
  const conteneur = document.getElementById("section-reservation");
  if (!AFFICHER_SECTION_RESERVATION) { conteneur.innerHTML = ""; return; }

  const filmId = Number(document.getElementById("popup-overlay").dataset.filmId);
  const titre = state.filmSelectionne?.titre || "ce parcours";
  const partenaires = VISITES_PARTENAIRES[filmId];

  if (partenaires && partenaires.length) {
    // Une ou plusieurs structures organisent déjà une visite pour ce
    // film — on renvoie vers elles plutôt que de proposer nos propres
    // tarifs.
    const blocsPartenaires = partenaires.map((partenaire) => {
      const lien = partenaire.lienAffiliation || partenaire.lien;
      return `
        <div class="partenaire-visite">
          <p class="reservation-intro">
            <strong>${partenaire.nom}</strong> propose une visite guidée sur les lieux de tournage de "${titre}".
            ${partenaire.description || ""}
            ${partenaire.duree_minutes ? `<br><strong>⏱️ Durée de la visite : ${partenaire.duree_minutes} min</strong>` : ""}
          </p>
          <a class="btn-reserver" href="${lien}" target="_blank" rel="noopener noreferrer" onclick="_trackerClic('visite_partenaire', {nom_partenaire: '${partenaire.nom.replace(/'/g, "")}'})">
            📩 Voir cette visite chez ${partenaire.nom}
          </a>
        </div>
      `;
    }).join("");

    conteneur.innerHTML = `
      <p class="anecdote-titre">🎟️ ${partenaires.length > 1 ? "Des visites existent déjà" : "Une visite existe déjà"} pour ce parcours</p>
      ${blocsPartenaires}
    `;
    return;
  }

  // Aucune visite organisée connue pour ce film — message honnête,
  // pas de tarifs inventés tant qu'aucune structure réelle n'a été
  // identifiée pour ce parcours précis.
  conteneur.innerHTML = `
    <p class="anecdote-titre">🎟️ Envie de visiter ce lieu accompagné ?</p>
    <p class="reservation-intro">
      Aucune visite guidée organisée n'est référencée pour "${titre}" pour l'instant.
      L'office de tourisme le plus proche du lieu peut avoir plus d'informations sur les visites disponibles.
    </p>
  `;
}



// ── Infobulle "à la Google Maps" au survol d'un tracé : distance et
// durée restantes jusqu'à l'arrivée, en fonction de la position du
// curseur sur la ligne. S'appuie sur turf.js (déjà chargé) pour
// projeter le curseur sur le tracé et mesurer la distance parcourue.

function _ligneUnique(geojson) {
  try {
    const features =
      geojson.type === "FeatureCollection"
        ? geojson.features
        : geojson.type === "Feature"
        ? [geojson]
        : [{ type: "Feature", properties: {}, geometry: geojson }];

    const coords = [];

    features.forEach((f) => {
      const geom = f?.geometry;
      if (!geom) return;
      if (geom.type === "LineString") {
        coords.push(...geom.coordinates);
      } else if (geom.type === "MultiLineString") {
        geom.coordinates.forEach((segment) => coords.push(...segment));
      }
    });

    return coords.length >= 2 ? turf.lineString(coords) : null;
  } catch (e) {
    console.error("Erreur de fusion de la géométrie du tracé :", e);
    return null;
  }
}

function attacherSurvolItineraire(coucheGeoJSON, distanceTotaleMetres, dureeTotaleSecondes) {
  if (!coucheGeoJSON || typeof turf === "undefined") return;
  if (!Number.isFinite(Number(distanceTotaleMetres))) return;

  const distanceTotale = Number(distanceTotaleMetres);
  const dureeTotale = Number(dureeTotaleSecondes);
  const ligne = _ligneUnique(coucheGeoJSON.toGeoJSON());
  if (!ligne) return;

  const infobulle = L.tooltip({
    sticky: true,
    direction: "top",
    offset: [0, -8],
    className: "tooltip-itineraire",
  });

  coucheGeoJSON.eachLayer((sousCouche) => {
    if (typeof sousCouche.on !== "function") return;

    sousCouche.on("mousemove", (e) => {
      const curseur = turf.point([e.latlng.lng, e.latlng.lat]);
      const surLaLigne = turf.nearestPointOnLine(ligne, curseur, { units: "kilometers" });
      const parcourueMetres = (surLaLigne.properties.location || 0) * 1000;
      const restantMetres = Math.max(0, distanceTotale - parcourueMetres);
      const restantSecondes =
        Number.isFinite(dureeTotale) && distanceTotale > 0
          ? dureeTotale * (restantMetres / distanceTotale)
          : null;

      const texte =
        `📍 ${formatDistance(restantMetres)} restants` +
        (restantSecondes !== null ? ` · ⏱️ ${formatDuree(restantSecondes)}` : "");

      infobulle.setLatLng(e.latlng).setContent(texte);
      if (!map.hasLayer(infobulle)) infobulle.addTo(map);
    });

    sousCouche.on("mouseout", () => {
      if (map.hasLayer(infobulle)) map.removeLayer(infobulle);
    });
  });
}

function formatDistance(m) {
    const valeur = Number(m);

    if (!Number.isFinite(valeur)) {
        return "distance indisponible";
    }

    if (valeur < 1000) {
        return `${Math.round(valeur)} m`;
    }

    return `${(valeur / 1000).toFixed(1)} km`;
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
let coucheMarqueurDepart = null;
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

  // Repart toujours d'un état propre : évite qu'un tracé, un marqueur
  // "ma position" ou un suivi GPS d'une navigation précédente (vers
  // un autre lieu ou une autre commodité) ne reste affiché ou
  // n'entre en conflit avec cette nouvelle navigation.
  nettoyerNavigation();

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
        "⚠️ Itinéraire calculé par la Géoplateforme IGN, mais les instructions détaillées ne sont pas disponibles.";
    return;
}

etapesNavigationCourantes = data.etapes_navigation;
indexEtapeCourante = 0;

   

    coucheItineraireCommodite = L.geoJSON(data.geometry, {
      style: { color: "#00ffcc", weight: 5, opacity: 0.9 },
    }).addTo(map);

    // Infobulle "distance/temps restants" au survol du tracé de
    // navigation guidée.
    attacherSurvolItineraire(
      coucheItineraireCommodite,
      data.distance_metres,
      data.duree_secondes
    );

    // Marqueur du point de départ réel de l'utilisateur — distinct des
    // icônes de lieu de tournage et de commodité.
    coucheMarqueurDepart = L.marker([departLat, departLon], {
      icon: L.divIcon({
        html: '<div class="marqueur-depart">📍</div>',
        className: "", iconSize: [30, 30], iconAnchor: [15, 28],
      }),
    }).bindPopup("Votre point de départ").addTo(map);

    _parler(etapesNavigationCourantes[0].instruction);
    panneau.querySelector(".nav-instruction").textContent = etapesNavigationCourantes[0].instruction;

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

function nettoyerNavigation() {
  // Tout ce qui appartient à UNE navigation guidée en cours : le
  // suivi GPS, le tracé sur la carte et le marqueur "ma position".
  // Appelée à l'arrêt explicite, à l'arrivée, ou avant d'en démarrer
  // une nouvelle (vers un autre lieu/une autre commodité) — pour ne
  // jamais laisser de résidu visuel de l'ancienne navigation.
  if (suiviPositionId) {
    navigator.geolocation.clearWatch(suiviPositionId);
    suiviPositionId = null;
  }
  if (coucheItineraireCommodite) {
    map.removeLayer(coucheItineraireCommodite);
    coucheItineraireCommodite = null;
  }
  if (coucheMarqueurDepart) {
    map.removeLayer(coucheMarqueurDepart);
    coucheMarqueurDepart = null;
  }
  etapesNavigationCourantes = [];
  indexEtapeCourante = 0;
}

function arreterNavigation() {
  nettoyerNavigation();
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

let _chargementChoroplethe = false;
let _chargementChaleur = false;

async function toggleChoroplethe() {
  const btn = document.getElementById("btn-choroplethe");
  if (coucheChoroplethe) {
    map.removeLayer(coucheChoroplethe);
    coucheChoroplethe = null;
    btn.dataset.actif = "false";
    return;
  }
  if (_chargementChoroplethe) return; // un clic est déjà en cours de traitement
  _chargementChoroplethe = true;

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
            <div class="ligne"><span>Lieux isolés</span><b>${stat.lieux_sans_hebergement_15km ?? 0}</b></div>
            <div class="recommandation">${stat.recommandation}</div>
          </div>
        ` : `<div class="popup-departement"><h3>${nom}</h3>Aucune donnée pour ce département.</div>`;
        L.popup().setLatLng(layer.getBounds().getCenter()).setContent(contenu).openOn(map);
      });
    },
  }).addTo(map);

  document.getElementById("btn-choroplethe").dataset.actif = "true";
  _chargementChoroplethe = false;
}

async function toggleChaleur() {
  const btn = document.getElementById("btn-chaleur");
  if (coucheChaleur) {
    map.removeLayer(coucheChaleur);
    coucheChaleur = null;
    btn.dataset.actif = "false";
    return;
  }
  if (_chargementChaleur) return;
  _chargementChaleur = true;

  const res = await fetch(`${API_BASE}/api/lieux/tous-points`);
  const data = await res.json();
  coucheChaleur = L.heatLayer(data.points, { radius: 22, blur: 18, maxZoom: 10 }).addTo(map);
  btn.dataset.actif = "true";
  _chargementChaleur = false;
}

// ── Écouteurs d'événements ────────────────────────────────────────
// Sur mobile, les filtres avancés (année/département/nationalité) sont
// déplacés dans la sidebar, à côté de Tout/Films/Séries — ça libère de
// la hauteur pour la carte, qui devenait trop petite. Sur desktop, ils
// restent au-dessus de la carte (position d'origine).
// Menu hamburger mobile — la sidebar reste ouverte par défaut au
// chargement, l'internaute peut l'ouvrir/fermer ensuite à volonté.
function _initialiserMenuHamburger() {
  const sidebar = document.getElementById("sidebar");
  const btnOuvrir = document.getElementById("btn-hamburger");
  const btnFermer = document.getElementById("btn-fermer-sidebar");

  btnOuvrir.addEventListener("click", () => sidebar.classList.remove("fermee"));
  btnFermer.addEventListener("click", () => sidebar.classList.add("fermee"));

  // En sélectionnant un film sur mobile, on referme le menu pour
  // libérer la carte — sans forcer sur desktop où il n'y a pas d'overlay.
  document.getElementById("cartes-liste").addEventListener("click", (e) => {
    if (window.matchMedia("(max-width: 600px)").matches && e.target.closest(".carte-film")) {
      sidebar.classList.add("fermee");
    }
  });
}

function _gererPositionFiltresAvances() {
  const filtresCarte = document.getElementById("filtres-carte");
  const emplacementMobile = document.getElementById("filtres");
  const emplacementDesktop = document.getElementById("carte-zone");
  const media = window.matchMedia("(max-width: 600px)");

  function repositionner() {
    if (media.matches) {
      emplacementMobile.insertAdjacentElement("afterend", filtresCarte);
    } else {
      emplacementDesktop.insertBefore(filtresCarte, emplacementDesktop.firstChild);
    }
  }

  repositionner();
  media.addEventListener("change", repositionner);
}

// ════ PUBLICITÉS — filtrées pour n'être pertinentes qu'au parcours
// touristique (transport, hébergement, logistique de voyage) — pas
// l'intégralité du catalogue Awin de Pelify, qui contient surtout du
// mobilier/déco/bien-être sans rapport avec un déplacement touristique.
// Chaque offre porte ses zones de vente réelles (Awin) — n'apparaît
// que si le pays détecté du visiteur en fait partie. ════
const PUBS_PELIFY = [
  
  { icon: "avion", titre: "Evago", desc: "Plateforme IA pour réserver vols, hôtels et locations de voiture", cta: "Découvrir →", url: "https://www.awin1.com/cread.php?s=4809237&v=127793&q=607479&r=2932851", image: "https://www.awin1.com/cshow.php?s=4809237&v=127793&q=607479&r=2932851", zones: ["US"] },
  { icon: "voiture", titre: "Allycar", desc: "Location de voiture familiale haut de gamme", cta: "Découvrir →", url: "https://www.awin1.com/cread.php?s=4721783&v=122406&q=599904&r=2932851", image: "https://www.awin1.com/cshow.php?s=4721783&v=122406&q=599904&r=2932851", zones: ["US"] },
  { icon: "parking", titre: "Purple Parking", desc: "Parking et services aéroport au Royaume-Uni", cta: "Découvrir →", url: "https://www.awin1.com/cread.php?s=2261337&v=12028&q=348193&r=2932851", image: "https://www.awin1.com/cshow.php?s=2261337&v=12028&q=348193&r=2932851", zones: ["GB"] },
  { icon: "bouclier", titre: "FastestVPN", desc: "Navigation privée et sécurisée en déplacement", cta: "Découvrir →", url: "https://www.awin1.com/cread.php?s=4590561&v=90211&q=566685&r=2932851", image: "https://www.awin1.com/cshow.php?s=4590561&v=90211&q=566685&r=2932851", zones: ["FR"] },
  { icon: "casque", titre: "EarFun", desc: "Écouteurs et enceintes sans fil pour vos trajets", cta: "Découvrir →", url: "https://www.awin1.com/cread.php?s=3996847&v=61233&q=525399&r=2932851", image: "https://www.awin1.com/cshow.php?s=3996847&v=61233&q=525399&r=2932851", zones: ["FR"] },
  { icon: "billet", titre: "Fnac Spectacles", desc: "Billetterie spectacles, concerts et activités en France, Belgique et Suisse", cta: "Découvrir →", url: "https://www.awin1.com/cread.php?s=4859292&v=12494&q=595681&r=2932851", image: "https://www.awin1.com/cshow.php?s=4859292&v=12494&q=595681&r=2932851", zones: ["FR", "BE", "CH"] },
  { icon: "hotel", titre: "Hotels.com", desc: "Réservation d'hôtels dans le monde entier, programme de fidélité inclus", cta: "Découvrir →", url: "https://www.awin1.com/cread.php?s=4665023&v=121722&q=594874&r=2932851", image: "https://www.awin1.com/cshow.php?s=4665023&v=121722&q=594874&r=2932851", zones: ["NZ"] },
  { icon: "navette", titre: "Flibco", desc: "Navettes aéroport écoresponsables entre villes et aéroports en Europe", cta: "Découvrir →", url: "https://www.awin1.com/cread.php?s=3412832&v=53945&q=467508&r=2932851", image: "https://www.awin1.com/cshow.php?s=3412832&v=53945&q=467508&r=2932851", zones: ["FR", "BE", "DE", "IT", "NL", "GB"] },
  { icon: "sim", titre: "Saily", desc: "eSIM voyage, data mobile dans plus de 200 destinations, sans frais d'itinérance", cta: "Découvrir →", url: "https://www.awin1.com/cread.php?s=4853773&v=128609&q=611399&r=2932851", image: "https://www.awin1.com/cshow.php?s=4853773&v=128609&q=611399&r=2932851", zones: ["US"] },
  { icon: "camping", titre: "Flextail", desc: "Accessoires de camping ultralégers pour explorer les lieux les plus reculés", cta: "Découvrir →", url: "https://www.awin1.com/cread.php?s=4722075&v=60295&q=539696&r=2932851", image: "https://www.awin1.com/cshow.php?s=4722075&v=60295&q=539696&r=2932851", zones: ["US"] },
  { icon: "deco", titre: "Éclairage Déco", desc: "Luminaires et éclairage décoratif haut de gamme pour la maison", cta: "Découvrir →", url: "https://www.awin1.com/cread.php?s=4826404&v=128237&q=608878&r=2932851", image: "https://www.awin1.com/cshow.php?s=4826404&v=128237&q=608878&r=2932851", zones: ["FR"] },
];

const _etatsCarrousels = {}; // { "1": { index, intervalId }, "2": {...} }

function _pubsPourVisiteur() {
  const pays = _detectCountry();
  const pubsCorrespondantes = PUBS_PELIFY.filter((p) => p.zones.includes(pays));
  // Repli : si aucune pub ne cible ce pays précis, montrer celles ciblant la France
  return pubsCorrespondantes.length ? pubsCorrespondantes : PUBS_PELIFY.filter((p) => p.zones.includes("FR"));
}

function _rendreOffrePub(offre) {
  return `
    <div class="pub-carte">
      <img src="${offre.image}" alt="${offre.titre}"
           onerror="this.replaceWith(Object.assign(document.createElement('div'), {className:'pub-icone-repli', innerHTML: PUB_SVG_FALLBACK['${offre.icon}'] || ''}))">
      <a href="${offre.url}" target="_blank" rel="sponsored noopener" class="pub-cta">${offre.cta}</a>
    </div>
  `;
}

function _initialiserCarrousel(idCarrousel) {
  const conteneur = document.getElementById(`carrousel-pub-${idCarrousel}`);
  if (!conteneur) return;
  const zoneContenu = conteneur.querySelector(".carrousel-contenu");
  const pubs = _pubsPourVisiteur();
  if (!pubs.length) { conteneur.classList.add("hidden"); return; }

  _etatsCarrousels[idCarrousel] = { index: 0, intervalId: null, pubs };

  function afficher(index) {
    const etat = _etatsCarrousels[idCarrousel];
    etat.index = ((index % etat.pubs.length) + etat.pubs.length) % etat.pubs.length;
    zoneContenu.innerHTML = _rendreOffrePub(etat.pubs[etat.index]);
  }

  function suivant() { afficher(_etatsCarrousels[idCarrousel].index + 1); _redemarrerAuto(); }
  function precedent() { afficher(_etatsCarrousels[idCarrousel].index - 1); _redemarrerAuto(); }

  function _redemarrerAuto() {
    clearInterval(_etatsCarrousels[idCarrousel].intervalId);
    _etatsCarrousels[idCarrousel].intervalId = setInterval(() => afficher(_etatsCarrousels[idCarrousel].index + 1), 4000);
  }

  conteneur.querySelector(".carrousel-gauche").addEventListener("click", precedent);
  conteneur.querySelector(".carrousel-droite").addEventListener("click", suivant);

  afficher(0);
  _redemarrerAuto();
}

function initialiserPublicites() {
  _initialiserCarrousel(1);
  _initialiserCarrousel(2);
}

document.addEventListener("DOMContentLoaded", () => {
  initCarte();
  chargerMonParcoursStocke();
  actualiserProfilPelify();
  chargerOptionsMonParcours();
  mettreAJourCompteurMonParcours();
  initialiserControlesIsochrone();
  chargerContourOccitanie();
  chargerOptionsFiltres();
  chargerFilms();
  _gererPositionFiltresAvances();
  _initialiserMenuHamburger();
  initialiserPublicites();

  document.querySelectorAll(".filtre-btn:not(.filtre-independant)").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".filtre-btn:not(.filtre-independant)").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      state.filtres.mediaType = btn.dataset.type;
      chargerFilms();
    });
  });

  document.getElementById("filtre-visites-toggle").addEventListener("click", (e) => {
    // Bascule indépendante — se combine avec Tout/Films/Séries plutôt
    // que de les remplacer, contrairement aux 3 boutons ci-dessus.
    state.filtres.avecVisiteGuidee = !state.filtres.avecVisiteGuidee;
    e.currentTarget.classList.toggle("active", state.filtres.avecVisiteGuidee);
    chargerFilms();
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

  document.getElementById("btn-mon-parcours")?.addEventListener("click", ouvrirMonParcours);
  document.getElementById("btn-fermer-mon-parcours")?.addEventListener("click", fermerMonParcours);
  document.getElementById("mon-parcours-overlay")?.addEventListener("click", e => { if(e.target.id === "mon-parcours-overlay") fermerMonParcours(); });
  document.addEventListener("keydown", e => { if(e.key === "Escape") fermerMonParcours(); });
  document.getElementById("btn-ajouter-parcours")?.addEventListener("click", () => { const id=Number(document.getElementById("popup-overlay")?.dataset?.lieuId); const lieu=state.lieuxCourants.find(x=>Number(x.id)===id); if(lieu) basculerLieuDansMonParcours(state.filmSelectionne,lieu); });

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
    const actif = e.currentTarget.classList.toggle("active");
    state.filtres.tri = actif ? "popularite" : "titre";
    chargerFilms();
  });

  const params = new URLSearchParams(window.location.search);
  const filmParam = params.get("film");
  if (filmParam) selectionnerFilm(Number(filmParam));

  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/sw.js").catch(() => {});
  }
})
;