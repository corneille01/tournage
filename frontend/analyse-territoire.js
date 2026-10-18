/* analyse-territoire.js — page « Analyse territoriale » : du global au précis, expliquée simplement.
   Les textes et les chiffres viennent de /api/analyse/territoire (calculés sur la base, jamais saisis ici). */
(function () {
  "use strict";
  const racine = document.getElementById("at-contenu");
  const COULEURS = { accent: "#ff007f", gris: "rgba(139,147,163,.55)",
    q: { etoiles: "#2a9d8f", a_renforcer: "#e63946", atouts_discrets: "#00ffcc", a_surveiller: "#8d99ae" } };
  const LIBELLE_Q = { etoiles: "Vitrines (célèbre et bien équipé)", a_renforcer: "À renforcer (célèbre, peu équipé)",
    atouts_discrets: "Atouts discrets (équipé, peu connu)", a_surveiller: "À surveiller" };
  const graphiques = [];

  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const md = (s) => esc(s).replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");

  const carteChiffre = (c) => `<div class="at-chiffre"><div class="val">${esc(c.valeur)}</div><div class="lib">${esc(c.label)}</div>${c.aide ? `<div class="aide">${md(c.aide)}</div>` : ""}</div>`;

  const tableau = (t, id) => `<div class="at-tableau"><h3><span>${esc(t.titre)}</span><button type="button" class="at-btn at-csv" data-csv="${esc(id)}">Télécharger (CSV)</button></h3>
      <table><thead><tr>${t.colonnes.map((c) => `<th scope="col">${esc(c)}</th>`).join("")}</tr></thead>
      <tbody>${t.lignes.map((l) => `<tr>${l.map((x) => `<td>${esc(x)}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;

  function chapitre(c) {
    const curieux = (c.pour_les_curieux || []).length
      ? `<details class="at-curieux" open><summary>Pour les curieux : les outils statistiques utilisés</summary>${c.pour_les_curieux.map((t) =>
          `<div class="at-terme"><b>${esc(t.terme)}</b>${t.valeur ? `<span class="v">${esc(t.valeur)}</span>` : ""}<br>${md(t.explication)}</div>`).join("")}</details>` : "";
    return `<article class="at-chapitre" id="ch-${esc(c.id)}">
      <p class="at-eyebrow">Chapitre ${c.numero} sur 9</p>
      <h2>${esc(c.titre)}</h2>
      ${c.question ? `<p class="at-question">${esc(c.question)}</p>` : ""}
      <div class="at-bref"><strong class="etiq">En bref</strong>${md(c.en_bref)}</div>
      ${(c.chiffres || []).length ? `<div class="at-chiffres">${c.chiffres.map(carteChiffre).join("")}</div>` : ""}
      ${(c.graphiques || []).length ? `<div class="at-graphes">${c.graphiques.map((g) =>
        `<figure class="at-graphe ${(g.etiquettes || []).length > 9 ? "haut" : ""}"><h3>${esc(g.titre)}</h3><div class="cadre"><canvas id="${esc(c.id)}-${esc(g.id)}" role="img" aria-label="${esc(g.titre)}"></canvas></div></figure>`).join("")}</div>` : ""}
      ${(c.tableaux || []).map((t, i) => tableau(t, `${c.id}:${i}`)).join("")}
      ${(c.lecture || []).filter(Boolean).length ? `<div class="at-bloc at-lecture"><h3><i class="fa-solid fa-lightbulb" aria-hidden="true"></i> Comment lire ces résultats</h3>${c.lecture.filter(Boolean).map((p) => `<p>${md(p)}</p>`).join("")}</div>` : ""}
      ${(c.attention || []).length ? `<div class="at-bloc at-attention"><h3><i class="fa-solid fa-triangle-exclamation" aria-hidden="true"></i> À garder en tête</h3><ul>${c.attention.map((a) => `<li>${md(a)}</li>`).join("")}</ul></div>` : ""}
      ${curieux}
    </article>`;
  }

  const base = { responsive: true, maintainAspectRatio: false, animation: { duration: 500 }, plugins: { legend: { labels: { color: "#dde4f0" } } } };
  const axes = (horizontal) => ({ x: { ticks: { color: "#aab2c2" }, grid: { color: "rgba(255,255,255,.06)" }, beginAtZero: true },
    y: { ticks: { color: "#aab2c2", autoSkip: false }, grid: { color: "rgba(255,255,255,.04)" }, beginAtZero: !horizontal } });
  const axe = (titre, extra = {}) => ({ min: 0, max: 100, title: { display: true, text: titre, color: "#aab2c2" }, ticks: { color: "#aab2c2" }, grid: { color: "rgba(255,255,255,.06)" }, ...extra });

  function dessiner(cid, g) {
    const el = document.getElementById(`${cid}-${g.id}`);
    if (!el || typeof Chart === "undefined") return;
    let cfg;
    if (g.type === "barres_h") {
      const datasets = [{ label: "Valeur observée", data: g.valeurs, backgroundColor: "rgba(0,255,204,.55)", borderRadius: 4 }];
      if (g.bas && g.haut) datasets.push({ label: "Fourchette plausible (95 %)", data: g.bas.map((b, i) => (b == null ? null : [b, g.haut[i]])),
        backgroundColor: "rgba(255,215,0,.9)", barPercentage: .25, grouped: false, borderRadius: 2 });
      cfg = { type: "bar", data: { labels: g.etiquettes, datasets }, options: { ...base, indexAxis: "y", scales: axes(true) } };
    } else if (g.type === "barres") {
      const datasets = [{ label: "Œuvres", data: g.valeurs, backgroundColor: "rgba(0,255,204,.55)", borderRadius: 4 }];
      if (g.valeurs2) datasets.push({ label: g.legende2 || "dont", data: g.valeurs2, backgroundColor: "rgba(255,0,127,.75)", borderRadius: 4 });
      cfg = { type: "bar", data: { labels: g.etiquettes, datasets }, options: { ...base, scales: axes(false) } };
    } else if (g.type === "histogramme_groupe") {
      const pal = ["rgba(0,255,204,.65)", "rgba(255,215,0,.7)", "rgba(255,0,127,.7)", "rgba(141,153,174,.7)"];
      cfg = { type: "bar", data: { labels: g.etiquettes, datasets: g.series.map((s, i) => ({ label: s.nom, data: s.valeurs, backgroundColor: pal[i % pal.length], borderRadius: 3 })) },
        options: { ...base, scales: axes(false) } };
    } else if (g.type === "lorenz") {
      cfg = { type: "line", data: { datasets: [
        { label: "Répartition observée", data: g.points.map(([x, y]) => ({ x: 100 * x, y: 100 * y })), borderColor: COULEURS.accent, backgroundColor: "rgba(255,0,127,.18)", fill: "origin", tension: .1, pointRadius: 3 },
        { label: "Répartition parfaitement égale", data: [{ x: 0, y: 0 }, { x: 100, y: 100 }], borderColor: COULEURS.gris, borderDash: [6, 4], pointRadius: 0 }] },
        options: { ...base, scales: { x: { type: "linear", ...axe("% des départements (du moins doté au plus doté)") }, y: axe("% cumulé des lieux") } } };
    } else if (g.type === "nuage") {
      const groupes = {};
      g.points.forEach((p) => { (groupes[p.q] = groupes[p.q] || []).push(p); });
      cfg = { type: "scatter", data: { datasets: [
        ...Object.entries(groupes).map(([q, pts]) => ({ label: LIBELLE_Q[q] || q, data: pts, backgroundColor: COULEURS.q[q] || "#fff", pointRadius: 4 })),
        { label: "Seuil d'équipement (66)", data: [{ x: 66, y: 0 }, { x: 66, y: 100 }], type: "line", borderColor: COULEURS.gris, borderDash: [5, 4], pointRadius: 0 },
        { label: "Notoriété médiane", data: [{ x: 0, y: 50 }, { x: 100, y: 50 }], type: "line", borderColor: COULEURS.gris, borderDash: [5, 4], pointRadius: 0 }] },
        options: { ...base, plugins: { ...base.plugins, tooltip: { callbacks: { label: (c) => c.raw.nom ? `${c.raw.nom} · équipement ${c.raw.x}, notoriété ${c.raw.y}` : c.dataset.label } } },
          scales: { x: axe("Équipement du lieu (/100)"), y: axe("Notoriété de l'œuvre (0-100)") } } };
    }
    if (cfg) graphiques.push(new Chart(el.getContext("2d"), cfg));
  }

  function exporterCSV(t, nom) {
    const ligne = (cells) => cells.map((c) => `"${String(c).replace(/"/g, '""')}"`).join(";");
    const contenu = "\ufeff" + [ligne(t.colonnes), ...t.lignes.map(ligne)].join("\n");
    const url = URL.createObjectURL(new Blob([contenu], { type: "text/csv;charset=utf-8" }));
    const a = Object.assign(document.createElement("a"), { href: url, download: `pelify-${nom}.csv` });
    document.body.appendChild(a); a.click(); a.remove(); URL.revokeObjectURL(url);
  }

  function afficher(data) {
    const resume = `<section class="at-resume" aria-labelledby="at-resume-t"><h2 id="at-resume-t">L'essentiel en 30 secondes</h2>
      <div class="at-messages">${data.messages_cles.map((m) => `<a class="at-message" href="#ch-${esc(data.chapitres.find((c) => c.numero === m.chapitre)?.id || "")}"><b>${esc(m.titre)}</b>${md(m.texte)}<br><small>Voir le chapitre ${m.chapitre} →</small></a>`).join("")}</div>
      <p class="at-question" style="margin-top:.7rem">Analyse calculée sur ${esc(data.perimetre.lieux)} lieux de tournage et ${esc(data.perimetre.oeuvres)} œuvres · mise à jour du ${esc(new Date(data.genere_le).toLocaleString("fr-FR"))}.</p></section>`;
    const nav = `<nav class="at-nav" aria-label="Chapitres">${data.chapitres.map((c) => `<a href="#ch-${esc(c.id)}" data-ch="ch-${esc(c.id)}">${c.numero}. ${esc(c.titre)}</a>`).join("")}</nav>`;
    racine.innerHTML = resume + nav + data.chapitres.map(chapitre).join("") +
      `<footer class="at-pied">Sources : Wikidata, TMDB, DATAtourisme, OpenStreetMap, IGN Géoplateforme (itinéraires et temps de trajet). Les textes sont générés automatiquement à partir des chiffres, selon des règles publiques et reproductibles. Cette analyse aide à décider ; elle ne remplace pas une étude de terrain.</footer>`;
    graphiques.forEach((g) => g.destroy());
    graphiques.length = 0;
    data.chapitres.forEach((c) => (c.graphiques || []).forEach((g) => dessiner(c.id, g)));
    racine.querySelectorAll("[data-csv]").forEach((b) => b.addEventListener("click", () => {
      const [cid, i] = b.dataset.csv.split(":");
      exporterCSV(data.chapitres.find((c) => c.id === cid).tableaux[Number(i)], `${cid}-${i}`);
    }));
    if ("IntersectionObserver" in window) {
      const liens = [...racine.querySelectorAll(".at-nav a")];
      const obs = new IntersectionObserver((entrees) => entrees.forEach((e) => {
        if (e.isIntersecting) liens.forEach((l) => l.classList.toggle("actif", l.dataset.ch === e.target.id));
      }), { rootMargin: "-30% 0px -60% 0px" });
      racine.querySelectorAll(".at-chapitre").forEach((c) => obs.observe(c));
    }
  }

  document.getElementById("at-imprimer")?.addEventListener("click", () => window.print());
  const btnExpert = document.getElementById("at-expert");
  document.body.classList.add("at-mode-expert");   // détails visibles par défaut ; le bouton les masque
  btnExpert?.addEventListener("click", () => {
    const actif = document.body.classList.toggle("at-mode-expert");
    btnExpert.setAttribute("aria-pressed", String(actif));
  });

  fetch("/api/analyse/territoire", { cache: "no-store" })
    .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
    .then(afficher)
    .catch((e) => { racine.innerHTML = `<div class="at-erreur"><b>L'analyse est momentanément indisponible.</b><br>${esc(e.message)}. Réessayez dans un instant ou consultez le <a href="/analyse-detaillee.html">tableau de bord expert</a>.</div>`; });
})();