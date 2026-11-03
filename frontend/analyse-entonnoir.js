/* analyse-entonnoir.js — page « Analyse territoriale » : affiche /api/analyse/entonnoir.
   Tous les textes et chiffres viennent du serveur (calculés sur la base) ; ce fichier ne fait que les mettre en page. */
(function () {
  "use strict";
  const racine = document.getElementById("en-contenu");
  const nav = document.getElementById("en-nav");
  const TXT = "#aab2c2", GRILLE = "rgba(255,255,255,.07)";

  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const md = (s) => esc(s).replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
  const chaud = (v) => `hsl(${Math.max(0, Math.min(100, v)) * 1.35}, 62%, 55%)`;   // 0 = rouge … 100 = vert

  // ───────── tableaux ─────────
  function tableau(t, id) {
    const depuis = t.chaleur_colonnes_depuis ?? 1;
    const tete = t.colonnes.map((c) => `<th scope="col">${esc(c)}</th>`).join("");
    const corps = t.lignes.map((l, i) => `<tr>${l.map((x, j) => {
      let cls = "", style = "";
      if (t.chaleur && j >= depuis) {
        const v = (t.chaleur[i] || [])[j - depuis];
        if (v !== null && v !== undefined) { cls = "chaud"; style = ` style="background:${chaud(v)}"`; }
      }
      if (t.feux && j === l.length - 1) cls = `feu-${t.feux[i]}`;
      return `<td${cls ? ` class="${cls}"` : ""}${style}>${esc(x)}</td>`;
    }).join("")}</tr>`).join("");
    return `<div class="en-tableau"><h4><span>${esc(t.titre)}</span><button type="button" class="en-btn en-csv" data-csv="${esc(id)}"><i class="fa-solid fa-download" aria-hidden="true"></i> CSV</button></h4>
      ${t.masque ? `<p class="en-question">Tableau complet disponible en téléchargement (${t.lignes.length} lignes).</p>` :
        `<div class="en-defile"><table><thead><tr>${tete}</tr></thead><tbody>${corps}</tbody></table></div>`}</div>`;
  }

  function telecharger(t, nom) {
    const q = (x) => `"${String(x ?? "").replace(/"/g, '""')}"`;
    const lignes = [t.colonnes, ...t.lignes].map((l) => l.map(q).join(";")).join("\r\n");
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob(["\ufeff" + lignes], { type: "text/csv;charset=utf-8" }));
    a.download = `${nom.replace(/[^a-z0-9]+/gi, "-").toLowerCase()}.csv`;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 2000);
  }

  // ───────── section ─────────
  function section(s) {
    const detail = (s.detail || []).length
      ? `<details class="en-detail" open><summary>Détails statistiques de cette section</summary>${s.detail.map((t) =>
          `<div class="en-terme"><b>${esc(t.terme)}</b>${t.valeur ? `<span class="v">${esc(t.valeur)}</span>` : ""}<br>${md(t.explication)}</div>`).join("")}</details>` : "";
    return `<article class="en-section" id="s-${esc(s.id)}">
      <h3><span class="en-num">${s.numero}.</span>${esc(s.titre)}</h3>
      ${s.question ? `<p class="en-question">${esc(s.question)}</p>` : ""}
      <div class="en-bref"><strong class="etiq">En bref</strong>${md(s.en_bref)}</div>
      ${s.chiffres.length ? `<div class="en-chiffres">${s.chiffres.map((c) => `<div class="en-chiffre"><div class="val">${esc(c.valeur)}</div><div class="lib">${esc(c.label)}</div>${c.aide ? `<div class="aide">${esc(c.aide)}</div>` : ""}</div>`).join("")}</div>` : ""}
      ${s.graphiques.length ? `<div class="en-graphes">${s.graphiques.map((g) => `<figure class="en-graphe ${(g.etiquettes || []).length > 9 ? "haut" : ""}"><h4>${esc(g.titre)}</h4><div class="en-cadre"><canvas id="${esc(s.id)}-${esc(g.id)}" role="img" aria-label="${esc(g.titre)}"></canvas></div></figure>`).join("")}</div>` : ""}
      ${s.tableaux.map((t, i) => tableau(t, `${s.id}:${i}`)).join("")}
      ${s.lecture.length ? `<div class="en-bloc en-lecture"><h4><i class="fa-solid fa-lightbulb" aria-hidden="true"></i> Comment lire ces résultats</h4>${s.lecture.map((p) => `<p>${md(p)}</p>`).join("")}</div>` : ""}
      ${s.attention.length ? `<div class="en-bloc en-attention"><h4><i class="fa-solid fa-triangle-exclamation" aria-hidden="true"></i> À garder en tête</h4><ul>${s.attention.map((a) => `<li>${md(a)}</li>`).join("")}</ul></div>` : ""}
      ${detail}
    </article>`;
  }

  // ───────── graphiques ─────────
  const base = { responsive: true, maintainAspectRatio: false, animation: { duration: 400 }, plugins: { legend: { display: false } } };
  const axe = (extra = {}) => ({ ticks: { color: TXT }, grid: { color: GRILLE }, beginAtZero: true, ...extra });
  const axeCat = () => ({ ticks: { color: TXT, autoSkip: false }, grid: { display: false } });
  const COUL = { cible: "#ff007f", vitrine: "#2a9d8f", discret: "#ffd700", autre: "rgba(139,147,163,.45)" };
  const LIB_Q = { cible: "Priorités (célèbre, peu équipé)", vitrine: "Vitrines (célèbre, bien équipé)", discret: "Atouts discrets (équipé, peu connu)", autre: "Autres lieux" };

  const pluginSeuils = (sx, sy) => ({ id: "seuils", afterDraw(c) {
    const { ctx, chartArea: a, scales: { x, y } } = c;
    ctx.save(); ctx.strokeStyle = "rgba(255,255,255,.35)"; ctx.setLineDash([5, 5]);
    ctx.beginPath(); ctx.moveTo(x.getPixelForValue(sx), a.top); ctx.lineTo(x.getPixelForValue(sx), a.bottom);
    ctx.moveTo(a.left, y.getPixelForValue(sy)); ctx.lineTo(a.right, y.getPixelForValue(sy)); ctx.stroke(); ctx.restore();
  } });

  function dessiner(cid, g) {
    const el = document.getElementById(`${cid}-${g.id}`);
    if (!el || typeof Chart === "undefined") return;
    const u = g.unite ? ` ${g.unite}` : "";
    let cfg;
    if (g.type === "barres_h") {
      const ds = [{ label: "Valeur observée", data: g.valeurs, backgroundColor: "rgba(0,255,204,.6)", borderRadius: 4 }];
      if (g.bas && g.haut) ds.push({ label: "Fourchette plausible (95 %)", data: g.bas.map((b, i) => (b == null ? null : [b, g.haut[i]])),
        backgroundColor: "rgba(255,215,0,.95)", barPercentage: .22, grouped: false, borderRadius: 2 });
      cfg = { type: "bar", data: { labels: g.etiquettes, datasets: ds },
        options: { ...base, indexAxis: "y", plugins: { legend: { display: ds.length > 1, labels: { color: TXT } }, tooltip: { callbacks: { label: (c) => `${c.dataset.label} : ${Array.isArray(c.raw) ? c.raw.join(" à ") : c.raw}${u}` } } },
          scales: { x: axe(g.max ? { max: g.max } : {}), y: axeCat() } } };
    } else if (g.type === "barres" || g.type === "classes") {
      cfg = { type: "bar", data: { labels: g.etiquettes, datasets: [{ data: g.valeurs, backgroundColor: g.couleurs || "rgba(0,255,204,.6)", borderRadius: 4 }] },
        options: { ...base, scales: { x: { ticks: { color: TXT, autoSkip: false, maxRotation: 40 }, grid: { display: false } }, y: axe() } } };
    } else if (g.type === "empile_h") {
      cfg = { type: "bar", data: { labels: g.etiquettes, datasets: g.series.map((s) => ({ label: s.nom, data: s.valeurs, backgroundColor: s.couleur })) },
        options: { ...base, indexAxis: "y", plugins: { legend: { display: true, labels: { color: TXT, boxWidth: 12 } }, tooltip: { callbacks: { label: (c) => `${c.dataset.label} : ${c.raw} %` } } },
          scales: { x: { ...axe({ max: 100 }), stacked: true }, y: { ...axeCat(), stacked: true } } } };
    } else if (g.type === "entonnoir") {
      const max = Math.max(...g.valeurs, 1), tot = g.valeurs[0] || 1;
      cfg = { type: "bar", data: { labels: g.etiquettes.map((e, i) => `${e} — ${g.valeurs[i].toLocaleString("fr-FR")} (${Math.round(100 * g.valeurs[i] / tot)} %)`),
          datasets: [{ data: g.valeurs.map((v) => [-v / 2, v / 2]), backgroundColor: ["#00ffcc", "#2fd3b8", "#7fa6c8", "#ff007f"], borderRadius: 3, barPercentage: .92, categoryPercentage: .95 }] },
        options: { ...base, indexAxis: "y", plugins: { legend: { display: false }, tooltip: { callbacks: { label: (c) => `${g.valeurs[c.dataIndex].toLocaleString("fr-FR")} lieux` } } },
          scales: { x: { min: -max / 2, max: max / 2, display: false, grid: { display: false } }, y: axeCat() } } };
    } else if (g.type === "ecarts") {
      cfg = { type: "bar", data: { labels: g.etiquettes, datasets: [{ data: g.valeurs, backgroundColor: g.valeurs.map((v) => (v < 0 ? "#e63946" : "#2a9d8f")), borderRadius: 3 }] },
        options: { ...base, indexAxis: "y", plugins: { legend: { display: false }, tooltip: { callbacks: { label: (c) => `${c.raw > 0 ? "+" : ""}${c.raw} points` } } },
          scales: { x: axe({ beginAtZero: false }), y: axeCat() } } };
    } else if (g.type === "nuage") {
      const groupes = ["cible", "vitrine", "discret", "autre"].map((q) => ({ q, pts: g.points.filter((p) => p.q === q) })).filter((x) => x.pts.length);
      cfg = { type: "scatter", data: { datasets: groupes.map(({ q, pts }) => ({ label: LIB_Q[q], data: pts.map((p) => ({ x: p.x, y: p.y, nom: p.nom })), backgroundColor: COUL[q], pointRadius: q === "autre" ? 3 : 5 })) },
        options: { ...base, plugins: { legend: { display: true, labels: { color: TXT, boxWidth: 12 } }, tooltip: { callbacks: { label: (c) => `${c.raw.nom} — notoriété ${c.raw.x}, note ${c.raw.y}` } } },
          scales: { x: { ...axe({ min: 0, max: 100 }), title: { display: true, text: g.axe_x, color: TXT } }, y: { ...axe({ min: 0, max: 100 }), title: { display: true, text: g.axe_y, color: TXT } } } },
        plugins: [pluginSeuils(g.seuil_x, g.seuil_y)] };
    }
    if (cfg) new Chart(el, cfg);
  }

  // ───────── plan en entonnoir ─────────
  function construireNav(parties) {
    nav.innerHTML = parties.map((p, i) => {
      const largeur = 100 - i * (34 / Math.max(1, parties.length - 1));   // de 100 % à 66 % : l'entonnoir se resserre
      return `<div class="en-nav-partie" data-partie="${esc(p.id)}" style="width:${largeur.toFixed(0)}%"><h2>${esc(p.id)} · ${esc(p.titre)}</h2><ol>${p.sections.map((s) => `<li><a href="#s-${esc(s.id)}" data-sec="s-${esc(s.id)}">${s.numero}. ${esc(s.titre.replace(/^Indicateur \d+ sur 8 : /, ""))}</a></li>`).join("")}</ol></div>`;
    }).join("");
    if (!("IntersectionObserver" in window)) return;
    const liens = new Map([...nav.querySelectorAll("a")].map((a) => [a.dataset.sec, a]));
    const obs = new IntersectionObserver((entrees) => {
      entrees.filter((e) => e.isIntersecting).forEach((e) => {
        liens.forEach((a) => a.classList.remove("actif"));
        nav.querySelectorAll(".en-nav-partie").forEach((n) => n.classList.remove("actif"));
        const a = liens.get(e.target.id);
        if (a) { a.classList.add("actif"); a.closest(".en-nav-partie").classList.add("actif"); }
      });
    }, { rootMargin: "-20% 0px -70% 0px" });
    racine.querySelectorAll(".en-section").forEach((s) => obs.observe(s));
  }

  // ───────── chargement ─────────
  function afficher(d) {
    const tablesCsv = {};
    racine.innerHTML = d.parties.map((p) => `<section class="en-partie" id="p-${esc(p.id)}">
        <div class="en-partie-tete"><span class="en-partie-lettre">${esc(p.id)}</span><h2>${esc(p.titre)}</h2><p>${esc(p.intro)}</p></div>
        ${p.sections.map(section).join("")}</section>`).join("") +
      `<p class="en-pied">Analyse calculée le ${esc(new Date(d.genere_le).toLocaleString("fr-FR"))} sur ${d.nb_lieux.toLocaleString("fr-FR")} lieux de tournage (${d.nb_notes.toLocaleString("fr-FR")} notés). Les textes sont générés par des règles publiques à partir des chiffres : aucune saisie manuelle, aucune intelligence artificielle.</p>`;
    d.parties.forEach((p) => p.sections.forEach((s) => {
      s.tableaux.forEach((t, i) => { tablesCsv[`${s.id}:${i}`] = t; });
      s.graphiques.forEach((g) => dessiner(s.id, g));
    }));
    racine.querySelectorAll("[data-csv]").forEach((b) => b.addEventListener("click", () => telecharger(tablesCsv[b.dataset.csv], b.dataset.csv)));
    construireNav(d.parties);
    if (location.hash) document.getElementById(location.hash.slice(1))?.scrollIntoView();
  }

  document.getElementById("en-imprimer").addEventListener("click", () => window.print());
  const bd = document.getElementById("en-details");
  bd.addEventListener("click", () => { const on = document.body.classList.toggle("avec-details"); bd.setAttribute("aria-pressed", String(on)); });

  fetch("/api/analyse/entonnoir")
    .then((r) => (r.ok ? r.json() : r.json().then((j) => Promise.reject(new Error(j.detail || `Erreur ${r.status}`)))))
    .then(afficher)
    .catch((e) => { racine.innerHTML = `<div class="en-erreur"><b>L'analyse est momentanément indisponible.</b><br>${esc(e.message)}. Réessayez dans un instant, ou consultez le <a href="/analyse-detaillee.html">tableau de bord expert</a>.</div>`; });
})();