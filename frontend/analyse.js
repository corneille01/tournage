(() => {
  "use strict";

  const REFRESH_MS = 5 * 60 * 1000;
  const charts = {};
  const $ = (id) => document.getElementById(id);

  const ESC = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({
    "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#039;"
  }[c]));

  const NUM = (v) => v !== null && v !== undefined && Number.isFinite(Number(v));
  const N = (v, d = 0) => NUM(v)
    ? Number(v).toLocaleString("fr-FR", {minimumFractionDigits:d, maximumFractionDigits:d})
    : "N/D";
  const P = (v, d = 1) => NUM(v) ? `${N(v,d)} %` : "N/D";
  const KM = (v, d = 2) => NUM(v) ? `${N(Number(v)/1000,d)} km` : "N/D";

  const CAT = {
    hebergement:"Hébergement",
    restaurant:"Restauration",
    activite:"Activités",
    office_tourisme:"Office de tourisme",
    arret_bus:"Arrêts de bus",
    aeroport:"Aéroports",
    aerodrome:"Aérodromes",
    hopital:"Hôpitaux",
    gare:"Gares",
    parking:"Parkings",
    refuge:"Refuges",
    distributeur:"Distributeurs",
    police:"Police"
  };

  function destroy(id) {
    if (charts[id]) {
      charts[id].destroy();
      delete charts[id];
    }
  }

  function chart(id, config) {
    const el = $(id);
    if (!el || typeof Chart === "undefined") return;
    destroy(id);

    const valueLabels = {
      id: "valueLabels",
      afterDatasetsDraw(chartInstance) {
        const {ctx} = chartInstance;
        chartInstance.data.datasets.forEach((dataset, di) => {
          const meta = chartInstance.getDatasetMeta(di);
          meta.data.forEach((element, i) => {
            const value = dataset.data[i];
            if (value === null || value === undefined || Number.isNaN(Number(value))) return;
            ctx.save();
            ctx.font = "600 11px Rajdhani, sans-serif";
            ctx.fillStyle = getComputedStyle(document.body).getPropertyValue("--couleur-texte") || "#fff";
            ctx.textAlign = "center";
            ctx.textBaseline = "bottom";
            if (chartInstance.config.type === "bar") {
              const pos = element.tooltipPosition();
              ctx.fillText(typeof value === "number" ? (Number.isInteger(value) ? value : value.toFixed(1)) : value, pos.x, pos.y - 5);
            }
            ctx.restore();
          });
        });
      }
    };

    const plugins = Array.isArray(config.plugins) ? [...config.plugins, valueLabels] : [valueLabels];
    charts[id] = new Chart(el.getContext("2d"), {...config, plugins});
  }

  function setText(id, value) {
    const el = $(id);
    if (el) el.textContent = value;
  }

  function ensureBlock(id, html, className = "panel") {
    let el = $(id);
    if (!el) {
      el = document.createElement("article");
      el.id = id;
      el.className = className;
    }
    el.innerHTML = html;
    return el;
  }

  function statTooltip(label) {
    const defs = {
      moyenne: "Moyenne arithmétique. Formule : Σx / N. Sensible aux valeurs extrêmes.",
      mediane: "Médiane (P50). Valeur qui partage les observations en deux moitiés.",
      q1: "Q1 = 25e percentile. 25 % des observations sont inférieures ou égales à cette valeur.",
      q3: "Q3 = 75e percentile. 75 % des observations sont inférieures ou égales à cette valeur.",
      iqr: "IQR = Q3 − Q1. Dispersion du cœur de 50 % des observations.",
      ecart_type: "Écart-type population. Racine de la variance calculée sur tous les lieux observés.",
      p90: "P90 = 90e percentile. 90 % des observations sont inférieures ou égales à cette valeur.",
      cv_pct: "Coefficient de variation. Formule : (écart-type / moyenne) × 100. Plus il est élevé, plus l'offre est hétérogène."
    };
    return defs[label] || "";
  }

  function statsRows(stat, unit = "nombre") {
    if (!stat) return "";
    const items = [
      ["moyenne", "Moyenne"],
      ["mediane", "Médiane"],
      ["q1", "Q1"],
      ["q3", "Q3"],
      ["iqr", "IQR"],
      ["ecart_type", "Écart-type"],
      ["p90", "P90"],
      ["cv_pct", "CV"]
    ];
    return items.map(([key, label]) => {
      let value = stat[key];
      let formatted = "N/D";
      if (NUM(value)) {
        formatted = key === "cv_pct" ? `${N(value,1)} %` :
          unit === "distance" ? KM(value,2) : N(value,2);
      }
      return `<tr>
        <th title="${ESC(statTooltip(key))}">${label}</th>
        <td>${formatted}</td>
      </tr>`;
    }).join("");
  }

  function addStatsGlossary() {
    const main = $("analyse-contenu");
    if (!main || $("stat-glossaire")) return;
    const sec = document.createElement("section");
    sec.className = "analyse-section";
    sec.id = "stat-glossaire";
    sec.innerHTML = `
      <div class="section-heading">
        <div>
          <span class="eyebrow">LECTURE STATISTIQUE</span>
          <h2>Comment lire les indicateurs ?</h2>
        </div>
      </div>
      <article class="panel stats-glossaire-grid">
        ${[
          ["Moyenne","Σx / N","Niveau moyen observé, mais sensible aux valeurs extrêmes."],
          ["Médiane","P50","Valeur centrale : 50 % des lieux sont en dessous et 50 % au-dessus."],
          ["Q1","P25","Repère du quart inférieur."],
          ["Q3","P75","Repère du quart supérieur."],
          ["IQR","Q3 − Q1","Mesure de dispersion du cœur de 50 % des lieux."],
          ["Écart-type","√variance","Dispersion absolue autour de la moyenne."],
          ["P90","90e percentile","Repère des situations les plus éloignées / fortement équipées."],
          ["CV","σ / moyenne × 100","Dispersion relative : utile pour repérer l’hétérogénéité."]
        ].map(([a,b,c]) => `<div class="stat-glossary-item" title="${ESC(c)}">
          <strong>${a}</strong><code>${b}</code><span>${c}</span>
        </div>`).join("")}
      </article>
    `;
    main.appendChild(sec);
  }

  function moveSections() {
    const main = $("analyse-contenu");
    if (!main) return;
    const sections = [...main.querySelectorAll(":scope > section.analyse-section")];

    const find = (text) => sections.find(s => s.textContent.toLowerCase().includes(text));

    const structure = find("où se concentre");
    const capacite = find("les lieux sont-ils prêts");
    const mobilite = find("quel rôle joue");
    const opportunites = find("où concentrer les efforts");
    const fragilites = find("quels territoires");
    const potentiel = find("popularité cinématographique");
    const priorite = find("où un élu devrait-il agir");
    const territoires = find("tableau de bord départemental");
    const offre = find("offre touristique autour");
    const qualite = find("qualité et couverture");
    const filmographie = find("œuvres les plus reconnues");

    // We keep the existing sections but append the decision section last.
    const ordered = [
      ...[structure, offre, capacite, mobilite, territoires, filmographie, qualite, potentiel, opportunites, fragilites]
        .filter(Boolean),
    ];

    ordered.forEach(s => main.appendChild(s));
    if (priorite) main.appendChild(priorite);
    addStatsGlossary();
  }

  function improveExistingLabels(data) {
    const c = data.accessibilite || {};
    const e = data.equipements || {};
    const secCap = [...document.querySelectorAll(".analyse-section")].find(s =>
      s.textContent.includes("Les lieux sont-ils prêts à être valorisés")
    );
    if (secCap) {
      const ps = secCap.querySelectorAll(".radial-panel p");
      if (ps[0]) ps[0].textContent = NUM(c.pret_15_pct)
        ? `Part des lieux dont les durées routières réelles permettent le seuil de 15 min : ${P(c.pret_15_pct)}.`
        : "Donnée insuffisante : toutes les observations ne disposent pas d'une durée routière réelle complète.";
      if (ps[1]) ps[1].textContent = NUM(c.pret_30_pct)
        ? `Part des lieux sous 30 min selon les durées routières réellement disponibles : ${P(c.pret_30_pct)}.`
        : "Donnée insuffisante : le seuil 30 min n'est pas présenté sur une base partielle.";
      if (ps[2]) ps[2].textContent = NUM(c.isoles_45_pct)
        ? `Part des lieux à plus de 45 min.`
        : "Non calculé : l'absence de durée n'est jamais assimilée à plus de 45 min.";
    }

    const secPot = [...document.querySelectorAll(".analyse-section")].find(s =>
      s.textContent.includes("Popularité cinématographique × préparation touristique")
    );
    if (secPot) {
      const note = secPot.querySelector(".analyse-note");
      if (note) note.textContent =
        "Ce croisement n'est utilisé que lorsque les deux variables sont réellement disponibles. La popularité TMDB est un proxy numérique de notoriété, pas une fréquentation touristique ni un impact économique.";
    }

    // Remove ambiguous old score language.
    document.querySelectorAll("[title]").forEach(el => {
      if ((el.title || "").includes("score composite")) {
        el.title = "Voir la formule détaillée dans la section Priorité d'investissement.";
      }
    });
  }

  function renderStructure(data) {
    const deps = Array.isArray(data.departements) ? [...data.departements] : [];
    const hhi = data.metriques?.concentration_hhi;
    const top3 = data.metriques?.concentration_top3_pct ?? deps.slice(0,3).reduce((s,d)=>s+Number(d.part_pct||0),0);

    chart("graphe-lieux", {
      type:"bar",
      data:{labels:deps.map(d=>d.departement),datasets:[{label:"Lieux",data:deps.map(d=>d.nb_lieux||0)}]},
      options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false}},scales:{y:{beginAtZero:true}}}
    });

    chart("graphe-concentration", {
      type:"doughnut",
      data:{labels:["3 premiers départements","Autres"],datasets:[{data:[top3,Math.max(0,100-top3)]}]},
      options:{responsive:true,maintainAspectRatio:false}
    });

    setText("top3-value", NUM(top3) ? P(top3) : "N/D");
    setText("hhi-value", NUM(hhi) ? N(hhi,2) : "N/D");
    const i = $("interpretation-lieux");
    if (i && deps.length) {
      const d = deps[0];
      i.innerHTML = `<strong>${ESC(d.departement)}</strong> concentre <strong>${N(d.nb_lieux)}</strong> lieux (${P(d.part_pct)} du total). Cette concentration indique où l'écosystème cinéma est le plus visible, mais ne prouve pas à elle seule un potentiel touristique supérieur.`;
    }
  }

  function renderMobility(data) {
    const iso = data.isochrones || {};
    const cov = Array.isArray(iso.couverture_par_minutes) ? iso.couverture_par_minutes : [];
    chart("graphe-isochrones", {
      type:"line",
      data:{
        labels:cov.map(x=>`${x.minutes} min`),
        datasets:[
          {label:"Voiture",data:cov.map(x=>x.voiture_pct ?? null),tension:.25},
          {label:"À pied",data:cov.map(x=>x.pied_pct ?? null),tension:.25}
        ]
      },
      options:{responsive:true,maintainAspectRatio:false,scales:{y:{min:0,max:100,title:{display:true,text:"Part des lieux avec isochrone disponible"}}}}
    });
    setText("ratio-mobilite", NUM(iso.ratio_surface_voiture_marche_15) ? N(iso.ratio_surface_voiture_marche_15,2) : "N/D");
    const txt = $("fraicheur-isochrones");
    if (txt) txt.innerHTML = `Une surface d'isochrone mesure une <strong>emprise spatiale</strong>. Elle ne mesure ni population, ni emplois, ni fréquentation accessibles. À 15 min, un rapport de surface de ${NUM(iso.ratio_surface_voiture_marche_15) ? N(iso.ratio_surface_voiture_marche_15,2)+"×" : "N/D"} signifie que la voiture couvre une emprise spatiale plus large que la marche, pas qu'elle transporte ${N(iso.ratio_surface_voiture_marche_15,2)} fois plus de personnes.`;
  }

  function renderRadials(data) {
    const a = data.accessibilite || {};
    const radial = (id,val,label) => {
      const el = $(id);
      if (!el || typeof Chart === "undefined") return;
      destroy(id);
      const known = NUM(val);
      new Chart(el.getContext("2d"), {
        type:"doughnut",
        data:{labels:[label,known?"Non couvert":"Donnée indisponible"],datasets:[{data:known?[Number(val),Math.max(0,100-Number(val))]:[1]}]},
        options:{responsive:true,maintainAspectRatio:false,cutout:"70%",plugins:{legend:{display:false}}}
      });
      if (!known) {
        const p = el.closest(".radial-panel")?.querySelector("p");
        if (p) p.classList.add("data-unavailable");
      }
    };
    radial("graphe-pret15", a.pret_15_pct, "15 min");
    radial("graphe-pret30", a.pret_30_pct, "30 min");
    radial("graphe-isoles", a.isoles_45_pct, "45 min");
  }

  function normalized(v, min, max) {
    if (!NUM(v) || !NUM(min) || !NUM(max) || max === min) return null;
    return ((Number(v)-Number(min))/(Number(max)-Number(min)))*100;
  }

  function computePriority(rows) {
    const valid = rows.filter(r => NUM(r.nb_lieux));
    if (!valid.length) return [];

    const offers = valid
      .map(r => NUM(r.hebergement_presence_pct) && NUM(r.restaurant_presence_pct)
        ? (Number(r.hebergement_presence_pct)+Number(r.restaurant_presence_pct))/2 : null)
      .filter(NUM);

    const surfaces = valid.map(r => NUM(r.surface_moyenne_15min_km2) ? Number(r.surface_moyenne_15min_km2) : null).filter(NUM);
    const minOffer = offers.length ? Math.min(...offers) : null;
    const maxOffer = offers.length ? Math.max(...offers) : null;
    const maxSurface = surfaces.length ? Math.max(...surfaces) : null;
    const minSurface = surfaces.length ? Math.min(...surfaces) : null;
    const maxPlaces = Math.max(...valid.map(r=>Number(r.nb_lieux)||0));

    return valid.map(r => {
      const offer = NUM(r.hebergement_presence_pct) && NUM(r.restaurant_presence_pct)
        ? (Number(r.hebergement_presence_pct)+Number(r.restaurant_presence_pct))/2 : null;

      // Offre faible => besoin d'action plus fort.
      const deficitOffer = NUM(offer) && NUM(minOffer) && NUM(maxOffer)
        ? 100 - normalized(offer,minOffer,maxOffer) : null;

      // Surface faible => accessibilité spatiale plus faible.
      const deficitMobility = NUM(r.surface_moyenne_15min_km2) && NUM(minSurface) && NUM(maxSurface)
        ? 100 - normalized(r.surface_moyenne_15min_km2,minSurface,maxSurface) : null;

      // Plus de lieux => plus d'enjeu pour une politique de valorisation,
      // sans prétendre mesurer l'impact économique.
      const cinemaPresence = maxPlaces ? (Number(r.nb_lieux)/maxPlaces)*100 : null;

      const parts = [
        [deficitOffer,.45],
        [deficitMobility,.35],
        [cinemaPresence,.20]
      ].filter(([v])=>NUM(v));

      const score = parts.length
        ? parts.reduce((s,[v,w])=>s+v*w,0)/parts.reduce((s,[,w])=>s+w,0)
        : null;

      return {...r, score_priorite:score, deficit_offre:deficitOffer, deficit_mobilite:deficitMobility, presence_cinema:cinemaPresence};
    }).sort((a,b)=>(b.score_priorite??-1)-(a.score_priorite??-1));
  }

  function renderPriority(data) {
    const host = $("liste-priorisation");
    const rows = computePriority(Array.isArray(data.departements)?data.departements:[]);
    const section = $("liste-priorisation")?.closest(".analyse-section");
    if (section) {
      const note = section.querySelector(".analyse-note");
      if (note) note.innerHTML = `
        <strong>Pourquoi agir en premier ?</strong><br>
        Le score est un <strong>outil de décision</strong>, pas une mesure statistique universelle :
        <code>Priorité = 45 % × déficit d'offre + 35 % × déficit de mobilité + 20 % × présence cinématographique</code>.
        Le déficit d'offre repose sur la moyenne des taux de présence hébergement + restaurant ; le déficit de mobilité repose sur la surface moyenne accessible en voiture à 15 min ; la présence cinématographique est le nombre de lieux rapporté au département qui en compte le plus.
        Les pondérations sont explicites et modifiables. Une donnée manquante n'améliore jamais le score.
      `;
    }

    chart("graphe-priorisation", {
      type:"bar",
      data:{labels:rows.map(r=>r.departement),datasets:[{label:"Besoin d'action (/100)",data:rows.map(r=>r.score_priorite)}]},
      options:{indexAxis:"y",responsive:true,maintainAspectRatio:false,scales:{x:{min:0,max:100}},plugins:{legend:{display:false}}}
    });

    if (host) {
      host.innerHTML = rows.length ? rows.slice(0,8).map((r,i)=>`
        <article class="priorite-card">
          <div class="priorite-rang">#${i+1}</div>
          <div class="priorite-corps">
            <div class="priorite-head"><h3>${ESC(r.departement)}</h3><strong class="priorite-score">${N(r.score_priorite,1)}<small>/100</small></strong></div>
            <p>
              Déficit d'offre : ${NUM(r.deficit_offre)?P(r.deficit_offre,1):"N/D"} ·
              Déficit mobilité : ${NUM(r.deficit_mobilite)?P(r.deficit_mobilite,1):"N/D"} ·
              Présence cinéma : ${P(r.presence_cinema,1)}.
            </p>
          </div>
        </article>
      `).join("") : `<div class="analyse-empty">Données insuffisantes pour classer les départements.</div>`;
    }

    return rows;
  }

  function renderOffer(obs) {
    const host = $("offre-categories");
    const eq = obs?.equipements || {};
    const keys = Object.keys(eq);
    if (!host) return;

    host.innerHTML = keys.length ? keys.map(key => {
      const b = eq[key] || {};
      const n = b.n;
      return `
        <article class="offre-carte">
          <div class="offre-carte-head">
            <h3>${ESC(CAT[key] || key)}</h3><span>N = ${N(n)}</span>
          </div>
          <div class="offre-stat-table">
            <table>
              <thead><tr><th>Indicateur</th><th>Valeur</th></tr></thead>
              <tbody>${statsRows(b.nombre,"nombre")}</tbody>
            </table>
          </div>
          <div class="offre-stat-table">
            <table>
              <thead><tr><th>Distance au plus proche</th><th>Valeur</th></tr></thead>
              <tbody>${statsRows(b.distance_plus_proche_m,"distance")}</tbody>
            </table>
          </div>
          <div class="offre-card-foot">
            <span>≤500 m : ${P(b.proximite?.a_500m_pct)}</span>
            <span>≤1 km : ${P(b.proximite?.a_1km_pct)}</span>
            <span>≤2 km : ${P(b.proximite?.a_2km_pct)}</span>
          </div>
          <p class="offre-interpretation">
            ${offerInterpretation(key,b)}
          </p>
        </article>
      `;
    }).join("") : `<div class="analyse-empty">Aucune catégorie statistique disponible.</div>`;

    const common = keys.map(k => {
      const stat = eq[k]?.nombre_rayon_standard_1km || eq[k]?.nombre_1km;
      return {key:k,stat};
    }).filter(x => x.stat && NUM(x.stat.mediane));

    // The current backend may not yet expose the 1-km bundle.
    const commonPanel = document.createElement("div");
    commonPanel.className = "panel common-radius-panel";
    commonPanel.innerHTML = common.length ? `
      <h3>Comparaison objective à rayon commun : 1 km</h3>
      <p class="analyse-note">Toutes les catégories sont comparées à <strong>1 000 m autour de chaque lieu</strong>. Cela permet une comparaison intercatégories qui n'est pas possible avec les rayons de recherche propres à DATAtourisme.</p>
      <div class="chart-wrap"><canvas id="graphe-offre-1km"></canvas></div>
    ` : `
      <h3>Comparaison objective à rayon commun : 1 km</h3>
      <p class="analyse-note data-unavailable">Le backend actuel n'expose pas encore le bloc statistique agrégé <code>nombre_rayon_standard_1km</code>. Ne pas comparer les nombres bruts des catégories tant que ce champ n'est pas disponible.</p>
    `;
    const sec = host.closest(".analyse-section");
    if (sec) {
      const old = $("offre-common-radius");
      if (old) old.remove();
      commonPanel.id = "offre-common-radius";
      sec.insertBefore(commonPanel, sec.querySelector(".dashboard-grid"));
    }

    if (common.length) {
      chart("graphe-offre-1km", {
        type:"bar",
        data:{labels:common.map(x=>CAT[x.key]||x.key),datasets:[{label:"Médiane du nombre d'équipements à 1 km",data:common.map(x=>x.stat.mediane)}]},
        options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false}},scales:{y:{beginAtZero:true}}}
      });
    }

    chart("graphe-offre-nombre", {
      type:"bar",
      data:{labels:keys.map(k=>CAT[k]||k),datasets:[{label:"Médiane",data:keys.map(k=>eq[k]?.nombre?.mediane ?? null)}]},
      options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false}},scales:{y:{beginAtZero:true}}}
    });

    chart("graphe-offre-distance", {
      type:"bar",
      data:{labels:keys.map(k=>CAT[k]||k),datasets:[
        {label:"Médiane (km)",data:keys.map(k=>NUM(eq[k]?.distance_plus_proche_m?.mediane)?Number(eq[k].distance_plus_proche_m.mediane)/1000:null)},
        {label:"P90 (km)",data:keys.map(k=>NUM(eq[k]?.distance_plus_proche_m?.p90)?Number(eq[k].distance_plus_proche_m.p90)/1000:null)}
      ]},
      options:{responsive:true,maintainAspectRatio:false}
    });
  }

  function offerInterpretation(key,b) {
    const o=b.nombre||{}, d=b.distance_plus_proche_m||{};
    const phrases=[];
    if (NUM(o.mediane)) phrases.push(`Pour un lieu médian, on observe ${N(o.mediane,1)} équipement(s).`);
    if (NUM(o.q1)&&NUM(o.q3)) phrases.push(`Le cœur de 50 % des lieux se situe entre ${N(o.q1,1)} et ${N(o.q3,1)} équipement(s) (IQR ${N(o.iqr,1)}).`);
    if (NUM(o.cv_pct)) phrases.push(`Le CV de ${N(o.cv_pct,1)} % mesure l'hétérogénéité entre lieux.`);
    if (NUM(d.mediane)) phrases.push(`L'équipement le plus proche est à ${KM(d.mediane)} pour le lieu médian ; P90 = ${KM(d.p90)}.`);
    return phrases.join(" ");
  }

  function renderDepartmentDashboard(obs, data) {
    const container = $("cartes-departements");
    const rows = Array.isArray(obs?.departements) ? obs.departements : [];
    if (!container) return;

    container.innerHTML = rows.length ? `
      <div class="department-graphs dashboard-grid two">
        <article class="panel"><h3>Lieux de tournage par département</h3><div class="chart-wrap"><canvas id="graphe-dep-lieux"></canvas></div><p class="interpretation-graphe">La taille de la présence cinématographique est exprimée en nombre de lieux. Elle indique l'activité observée, pas l'impact économique.</p></article>
        <article class="panel"><h3>Distance médiane à l'hébergement le plus proche</h3><div class="chart-wrap"><canvas id="graphe-dep-distance"></canvas></div><p class="interpretation-graphe">Une distance médiane élevée signale une offre d'hébergement plus éloignée autour du lieu médian du département.</p></article>
      </div>
      <div class="table-scroll"><table class="tableau-stats" id="tableau-departemental-v2"></table></div>
    ` : `<div class="analyse-empty">Aucun département statistiquement comparable.</div>`;

    const globalDeps = Array.isArray(data.departements) ? data.departements : [];
    chart("graphe-dep-lieux", {
      type:"bar",
      data:{labels:globalDeps.map(x=>x.departement),datasets:[{label:"Lieux",data:globalDeps.map(x=>x.nb_lieux)}]},
      options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false}},scales:{y:{beginAtZero:true}}}
    });

    chart("graphe-dep-distance", {
      type:"bar",
      data:{labels:rows.map(x=>x.departement),datasets:[{label:"Distance médiane (km)",data:rows.map(x=>NUM(x.distance_mediane_m)?Number(x.distance_mediane_m)/1000:null)}]},
      options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false}},scales:{y:{beginAtZero:true}}}
    });

    const table = $("tableau-departemental-v2");
    if (table) {
      const head = [
        ["Département","Territoire comparé"],
        ["N lieux","Nombre de lieux servant de base au calcul"],
        ["Moyenne","Σx / N"],
        ["Médiane","P50"],
        ["Écart-type","Dispersion population"],
        ["CV","σ / moyenne × 100"],
        ["Distance médiane","P50 de la distance au plus proche"],
        ["P90","90e percentile de la distance"],
        ["≤500 m","Part des lieux à ≤500 m"],
        ["Sans équipement","Nombre de lieux avec 0 équipement"]
      ];
      table.innerHTML = `<thead><tr>${head.map(([a,b])=>`<th title="${ESC(b)}">${a}</th>`).join("")}</tr></thead><tbody>${
        rows.map(r=>`<tr>
          <td>${ESC(r.departement)}</td><td>${N(r.n_lieux)}</td><td>${N(r.moyenne,1)}</td><td>${N(r.mediane,1)}</td>
          <td>${N(r.ecart_type,1)}</td><td>${P(r.cv_pct)}</td><td>${KM(r.distance_mediane_m)}</td>
          <td>${KM(r.distance_p90_m)}</td><td>${P(r.a_500m_pct)}</td><td>${N(r.sans_equipement_n)}</td>
        </tr>`).join("")
      }</tbody>`;
    }
  }

  function renderFilmography(data) {
    const rows = Array.isArray(data.films_notables) ? data.films_notables : [];
    const host = $("films-notables-cartes");
    if (!host) return;
    host.innerHTML = rows.length ? rows.map((f,i)=>`
      <article class="film-card">
        <div class="film-card-content">
          <span class="rang-notable">#${i+1}</span>
          <h3>${ESC(f.titre||"Sans titre")}</h3>
          <p>${f.annee ? ESC(f.annee) : ""}</p>
          <strong>${NUM(f.popularite)?N(f.popularite,2):"N/D"}</strong>
          <small>Popularité TMDB · ${N(f.nb_lieux)} lieux · ${N(f.nb_departements)} départements</small>
        </div>
      </article>
    `).join("") : `<div class="analyse-empty">Aucune œuvre classable.</div>`;

    const sec = host.closest(".analyse-section");
    if (sec) {
      let note = sec.querySelector(".filmography-note");
      if (!note) {
        note = document.createElement("p");
        note.className = "analyse-note filmography-note";
        sec.appendChild(note);
      }
      note.innerHTML = "<strong>Interprétation :</strong> la popularité TMDB est un indicateur de notoriété / engagement numérique. Elle ne mesure ni les entrées en salle, ni la fréquentation des lieux, ni les retombées économiques. Elle peut néanmoins aider à repérer quelles œuvres sont les plus susceptibles d'être mises en avant dans une stratégie éditoriale, un parcours ou une campagne.";
    }
  }

  function renderQuality(data) {
    const c = data.completude||{};
    const route = data.accessibilite?.route_coverage_pct;
    const host = $("completude-contenu");
    if (!host) return;
    host.classList.add("quality-flex");
    host.innerHTML = `
      <div><strong>${P(c.coordonnees_pct)}</strong><span>Coordonnées</span></div>
      <div><strong>${P(c.amenagement_pct)}</strong><span>Équipements touristiques</span></div>
      <div><strong>${P(c.popularite_pct)}</strong><span>Popularité des œuvres</span></div>
      <div><strong>${P(c.isochrones_pct)}</strong><span>Isochrones</span></div>
      <div><strong>${P(route)}</strong><span>Durées de routage</span></div>
    `;
    const methodo = $("methodologie");
    if (methodo) methodo.innerHTML = `
      <p><strong>Règle de qualité :</strong> une donnée manquante reste N/D. Elle n'est jamais transformée en zéro et ne fait jamais baisser artificiellement un score.</p>
      <p><strong>Population statistique :</strong> les calculs décrivent les lieux de tournage géolocalisés observés. N correspond au nombre de lieux effectivement disponibles pour le calcul.</p>
      <p><strong>Attention :</strong> la distance moyenne des 10 plus proches reste une statistique descriptive sur les 10 objets les plus proches ; elle ne doit pas être interprétée comme une moyenne de toute l'offre.</p>
    `;
  }

  function renderInfrastructure(data) {
    const infra = data.equipements?.infrastructures;
    if (!infra || typeof infra !== "object") return;

    const existing = document.getElementById("infrastructures-observees");
    if (existing) existing.remove();

    const sec = [...document.querySelectorAll(".analyse-section")].find(s=>s.textContent.includes("Les lieux sont-ils prêts à être valorisés"));
    if (!sec) return;

    const panel = document.createElement("article");
    panel.id = "infrastructures-observees";
    panel.className = "panel";
    const keys = Object.keys(infra);
    panel.innerHTML = `
      <h3>Infrastructures et services complémentaires observés</h3>
      <div class="infra-grid">
        ${keys.map(k=>{
          const x=infra[k]||{};
          const value = NUM(x.presence_pct) ? P(x.presence_pct) :
            NUM(x.a_500m_pct) ? P(x.a_500m_pct) :
            NUM(x.nombre_total) ? N(x.nombre_total) : "N/D";
          const label = CAT[k]||k;
          return `<div class="infra-card"><strong>${value}</strong><span>${ESC(label)}</span><small>Ne pas interpréter comme capacité totale du territoire : il s'agit des équipements observés autour des lieux recensés.</small></div>`;
        }).join("")}
      </div>
    `;
    sec.appendChild(panel);
  }

  function renderSynthesis(data, priorityRows) {
    const main = $("analyse-contenu");
    if (!main) return;
    const old = $("synthese-decision");
    if (old) old.remove();

    const top = priorityRows?.[0];
    const sec = document.createElement("section");
    sec.className = "analyse-section";
    sec.id = "synthese-decision";
    sec.innerHTML = `
      <div class="section-heading">
        <div><span class="eyebrow">SYNTHÈSE DÉCISIONNELLE</span><h2>Ce que l'élu doit retenir</h2></div>
      </div>
      <article class="diagnostic-card decision-synthesis">
        <h3>Lecture en trois niveaux</h3>
        <p><strong>1. Où est l'activité ?</strong> ${N(data.totaux?.lieux)} lieux géolocalisés, répartis sur ${N(data.totaux?.departements)} départements. La concentration indique les espaces où la présence cinématographique est déjà visible.</p>
        <p><strong>2. Sont-ils prêts à être valorisés ?</strong> La réponse doit être lue catégorie par catégorie avec la médiane, l'IQR, le P90 et les taux de proximité. Une moyenne seule masquerait les écarts entre lieux.</p>
        <p><strong>3. Où agir en premier ?</strong> ${top ? `<strong>${ESC(top.departement)}</strong> arrive en tête du score de besoin d'action (${N(top.score_priorite,1)}/100) selon la formule explicite affichée dans la section précédente.` : "Le classement n'est pas calculable avec les données actuellement disponibles."}</p>
        <p class="analyse-note">Ce tableau de bord est un outil d'aide à la décision. Il ne démontre pas un effet causal des tournages sur l'économie touristique et ne remplace pas une étude de fréquentation, de dépenses ou d'impact local.</p>
      </article>
    `;
    main.appendChild(sec);
  }

  function render(data, obs) {
    improveExistingLabels(data);
    renderStructure(data);
    renderRadials(data);
    renderMobility(data);
    renderOffer(obs);
    renderDepartmentDashboard(obs, data);
    renderFilmography(data);
    renderQuality(data);
    renderInfrastructure(data);

    // Replaces opaque existing score presentation without inventing a zero.
    const priorityRows = renderPriority(data);

    // Hide old opportunity / fragility / potential sections if their score language
    // conflicts with the transparent decision framework.
    document.querySelectorAll(".analyse-section").forEach(section => {
      const text = section.textContent || "";
      if (text.includes("Score de priorisation transparent") ||
          text.includes("Un score élevé signale un décalage entre présence des tournages") ||
          (text.includes("Popularité cinématographique × préparation touristique") && !data.lieux_potentiel?.length)) {
        if (!section.querySelector("#graphe-priorisation") && !section.querySelector("#films-notables-cartes")) {
          section.classList.add("legacy-ambiguous");
        }
      }
    });

    renderSynthesis(data, priorityRows);
    addStatsGlossary();
    moveSections();

    // Regional score: weighted mean of the transparent department score when available.
    const weighted = priorityRows.length
      ? priorityRows.reduce((s,r)=>s+(Number(r.score_priorite)||0)*(Number(r.nb_lieux)||0),0) /
        Math.max(1,priorityRows.reduce((s,r)=>s+(Number(r.nb_lieux)||0),0))
      : null;
    setText("score-regional", weighted !== null && Number.isFinite(weighted) ? N(weighted,0) : "—");

    const diag = $("diagnostic-titre");
    if (diag) diag.textContent = weighted === null
      ? "Diagnostic non calculable avec les données disponibles"
      : weighted >= 60 ? "Des écarts territoriaux justifient des actions ciblées"
      : weighted >= 35 ? "Une partie du potentiel reste à structurer"
      : "Les écarts observés sont relativement modérés";

    const txt = $("diagnostic-texte");
    if (txt) txt.textContent =
      `${N(data.totaux?.films)} œuvres, ${N(data.totaux?.lieux)} lieux géolocalisés et ${N(data.totaux?.departements)} départements observés. Les statistiques détaillées sont calculées au niveau des lieux, puis agrégées sans moyenner des écarts-types départementaux.`;
  }

  async function load() {
    setText("etat-donnees","Chargement…");
    try {
      const [ri,ro] = await Promise.all([
        fetch("/api/analyse/indicateurs?region=Occitanie",{cache:"no-store"}),
        fetch("/api/analyse/observatoire?region=Occitanie",{cache:"no-store"})
      ]);
      if (!ri.ok) throw new Error(`indicateurs HTTP ${ri.status}`);
      const data = await ri.json();
      let obs = null;
      if (ro.ok) obs = await ro.json();
      render(data,obs||{});
      setText("etat-donnees","Données à jour");
      setText("date-maj",`Actualisé à ${new Date().toLocaleTimeString("fr-FR",{hour:"2-digit",minute:"2-digit"})}`);
    } catch (e) {
      console.error("Erreur observatoire",e);
      setText("etat-donnees","Erreur de chargement");
      const t=$("diagnostic-titre"); if(t)t.textContent="Impossible de charger l'observatoire";
      const p=$("diagnostic-texte"); if(p)p.textContent="Vérifiez les endpoints /api/analyse/indicateurs et /api/analyse/observatoire.";
    }
  }

  document.addEventListener("DOMContentLoaded",()=>{
    load();
    setInterval(load,REFRESH_MS);
    const btn=$("btn-refresh");
    if(btn)btn.addEventListener("click",load);
  });
})();
