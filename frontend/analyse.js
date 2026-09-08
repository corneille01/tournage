
(() => {
  "use strict";

  const REFRESH_MS = 5 * 60 * 1000;
  const charts = {};

  const $ = (id) => document.getElementById(id);

  const esc = (v) =>
    String(v ?? "").replace(
      /[&<>"']/g,
      (c) =>
        ({
          "&": "&amp;",
          "<": "&lt;",
          ">": "&gt;",
          '"': "&quot;",
          "'": "&#039;",
        })[c]
    );

  const num = (v, d = 0) => {
    if (v == null || Number.isNaN(Number(v))) return "—";

    return Number(v).toLocaleString("fr-FR", {
      maximumFractionDigits: d,
    });
  };

  const pct = (v, d = 0) => `${num(v, d)} %`;

  function draw(id, config) {
    const canvas = $(id);

    if (!canvas || !window.Chart) return;

    if (charts[id]) {
      charts[id].destroy();
    }

    charts[id] = new Chart(canvas, config);
  }

  const scales = {
    x: {
      ticks: {
        color: "#9a9ea8",
      },
      grid: {
        color: "#2a2d35",
      },
    },

    y: {
      ticks: {
        color: "#9a9ea8",
      },
      grid: {
        color: "#2a2d35",
      },
      beginAtZero: true,
    },
  };

  function radial(id, value, label) {
    const x = Math.max(
      0,
      Math.min(100, Number(value) || 0)
    );

    draw(id, {
      type: "doughnut",

      data: {
        labels: [label, "Reste"],

        datasets: [
          {
            data: [x, 100 - x],
            borderWidth: 0,
          },
        ],
      },

      options: {
        responsive: true,
        maintainAspectRatio: false,
        cutout: "76%",

        plugins: {
          legend: {
            display: false,
          },
        },
      },
    });
  }

  function kpis(d) {
    const t = d.totaux || {};
    const a = d.accessibilite || {};

    const element = $("totaux-cartes");

    if (!element) return;

    element.innerHTML = [
      [
        "Films / séries",
        t.nb_films,
        "Œuvres publiées",
      ],

      [
        "Lieux",
        t.nb_lieux,
        "Lieux géolocalisés",
      ],

      [
        "Départements",
        t.nb_departements,
        "Territoires représentés",
      ],

      [
        "Prêts à 15 min",
        pct(a.pret_15_pct),
        "Hébergement + restaurant",
      ],

      [
        "Isolés >45 min",
        pct(a.isoles_45_pct),
        "Signal de fragilité",
      ],

      [
        "Couverture IGN",
        pct(d.isochrones?.couverture_pct),
        "Isochrones disponibles",
      ],
    ]
      .map(
        (x) => `
          <div class="kpi-card">
            <span>${esc(x[0])}</span>
            <strong>${esc(x[1])}</strong>
            <small>${esc(x[2])}</small>
          </div>
        `
      )
      .join("");
  }

  function diagnostic(d) {
    const deps = d.departements || [];
    const f = (d.vigilances || [])[0];

    if ($("score-regional")) {
      $("score-regional").textContent =
        num(d.scores?.opportunite_regionale);
    }

    if ($("diagnostic-titre")) {
      $("diagnostic-titre").textContent = deps[0]
        ? `${esc(
            deps[0].departement
          )} concentre actuellement le plus de lieux recensés.`
        : "Diagnostic territorial disponible.";
    }

    if ($("diagnostic-texte")) {
      $("diagnostic-texte").textContent =
        `${num(
          d.totaux?.nb_lieux
        )} lieux sont observés dans ${num(
          d.totaux?.nb_departements
        )} départements. ${pct(
          d.accessibilite?.pret_15_pct
        )} présentent un environnement hébergement + restaurant accessible en 15 minutes en voiture.` +
        (f
          ? ` ${esc(
              f.departement
            )} présente le principal signal de vigilance.`
          : "");
    }

    if ($("diagnostic-tags")) {
      $("diagnostic-tags").innerHTML = [
        `Concentration : ${esc(
          d.scores?.concentration_label || "—"
        )}`,

        `Préparation 30 min : ${pct(
          d.accessibilite?.pret_30_pct
        )}`,

        `Vigilance : ${esc(
          f?.departement || "—"
        )}`,
      ]
        .map((x) => `<span>${x}</span>`)
        .join("");
    }
  }

  function structure(d) {
    const departments = d.departements || [];
    const labels = departments.map(
      (x) => x.departement
    );

    draw("graphe-lieux", {
      type: "bar",

      data: {
        labels,

        datasets: [
          {
            label: "Lieux",
            data: departments.map(
              (x) => x.nb_lieux
            ),
          },
        ],
      },

      options: {
        responsive: true,
        maintainAspectRatio: false,
        indexAxis: "y",

        plugins: {
          legend: {
            display: false,
          },
        },

        scales,
      },
    });

    if ($("interpretation-lieux")) {
      $("interpretation-lieux").textContent =
        departments[0]
          ? `${departments[0].departement} arrive en tête avec ${num(
              departments[0].nb_lieux
            )} lieux (${pct(
              departments[0].part_pourcentage
            )}). Il s’agit de la base observée, pas d’une mesure exhaustive du potentiel.`
          : "";
    }

    if ($("hhi-value")) {
      $("hhi-value").textContent = num(
        d.concentration?.hhi,
        1
      );
    }

    if ($("top3-value")) {
      $("top3-value").textContent = pct(
        d.concentration?.top3_pct
      );
    }

    if ($("hhi-label")) {
      $("hhi-label").textContent =
        d.scores?.concentration_label || "";
    }

    draw("graphe-concentration", {
      type: "line",

      data: {
        labels,

        datasets: [
          {
            label: "Part cumulée",
            data: departments.map(
              (x) => x.part_cumulee_pct
            ),
            fill: true,
            tension: 0.35,
          },
        ],
      },

      options: {
        responsive: true,
        maintainAspectRatio: false,

        plugins: {
          legend: {
            display: false,
          },
        },

        scales: {
          ...scales,

          y: {
            ...scales.y,
            max: 100,

            ticks: {
              color: "#9a9ea8",
              callback: (v) => `${v}%`,
            },
          },
        },
      },
    });
  }

  function access(d) {
    const a = d.accessibilite || {};
    const deps = d.departements || [];

    radial(
      "graphe-pret15",
      a.pret_15_pct,
      "Prêts"
    );

    radial(
      "graphe-pret30",
      a.pret_30_pct,
      "Prêts"
    );

    radial(
      "graphe-isoles",
      a.isoles_45_pct,
      "Isolés"
    );

    draw("graphe-equipement", {
      type: "bar",

      data: {
        labels: deps.map(
          (x) => x.departement
        ),

        datasets: [
          {
            label: "Hébergements moyens",
            data: deps.map(
              (x) => x.moy_hebergement ?? 0
            ),
          },

          {
            label: "Restaurants moyens",
            data: deps.map(
              (x) => x.moy_restaurant ?? 0
            ),
          },
        ],
      },

      options: {
        responsive: true,
        maintainAspectRatio: false,
        scales,
      },
    });

    const best = [...deps].sort(
      (x, y) =>
        (y.pret_15_pct || 0) -
        (x.pret_15_pct || 0)
    )[0];

    if ($("interpretation-equipement")) {
      $("interpretation-equipement").textContent =
        best
          ? `${best.departement} présente le meilleur taux de préparation à 15 minutes (${pct(
              best.pret_15_pct
            )}).`
          : "";
    }
  }

  function iso(d) {
    const i = d.isochrones || {};
    const rows =
      i.couverture_par_minutes || [];

    if ($("ratio-mobilite")) {
      $("ratio-mobilite").textContent =
        i.ratio_surface_voiture_marche_15 != null
          ? num(
              i.ratio_surface_voiture_marche_15,
              1
            )
          : "—";
    }

    draw("graphe-isochrones", {
      type: "bar",

      data: {
        labels: rows.map(
          (x) => `${x.minutes} min`
        ),

        datasets: [
          {
            label: "Voiture",
            data: rows.map(
              (x) => x.voiture_pct
            ),
          },

          {
            label: "Marche",
            data: rows.map(
              (x) => x.pied_pct
            ),
          },
        ],
      },

      options: {
        responsive: true,
        maintainAspectRatio: false,

        scales: {
          ...scales,

          y: {
            ...scales.y,
            max: 100,

            ticks: {
              color: "#9a9ea8",
              callback: (v) => `${v}%`,
            },
          },
        },
      },
    });

    if ($("fraicheur-isochrones")) {
      if (i.derniere_date) {
        $("fraicheur-isochrones").textContent =
          `Dernier calcul connu : ${new Date(
            i.derniere_date
          ).toLocaleString("fr-FR")}. ` +
          `${
            i.minutes_disponibles?.length
              ? `Minutes disponibles : ${i.minutes_disponibles.join(
                  ", "
                )}.`
              : ""
          }`;
      } else {
        $("fraicheur-isochrones").textContent =
          "Aucun isochrone disponible dans les données observées.";
      }
    }
  }

  function rankings(d) {
    const opportunities =
      d.opportunites || [];

    const warnings =
      d.vigilances || [];

    draw("graphe-opportunites", {
      type: "bar",

      data: {
        labels: opportunities.map(
          (x) => x.departement
        ),

        datasets: [
          {
            label: "Score",
            data: opportunities.map(
              (x) => x.score
            ),
          },
        ],
      },

      options: {
        responsive: true,
        maintainAspectRatio: false,
        indexAxis: "y",

        plugins: {
          legend: {
            display: false,
          },
        },

        scales: {
          ...scales,

          x: {
            ...scales.x,
            max: 100,
          },
        },
      },
    });

    draw("graphe-vigilances", {
      type: "bar",

      data: {
        labels: warnings.map(
          (x) => x.departement
        ),

        datasets: [
          {
            label: "Score",
            data: warnings.map(
              (x) => x.score
            ),
          },
        ],
      },

      options: {
        responsive: true,
        maintainAspectRatio: false,
        indexAxis: "y",

        plugins: {
          legend: {
            display: false,
          },
        },

        scales: {
          ...scales,

          x: {
            ...scales.x,
            max: 100,
          },
        },
      },
    });

    if ($("liste-opportunites")) {
      $("liste-opportunites").innerHTML =
        opportunities
          .slice(0, 5)
          .map(
            (x, i) => `
              <article class="insight-card">
                <span class="rank">#${i + 1}</span>
                <h3>${esc(
                  x.departement
                )}</h3>
                <strong>${num(
                  x.score
                )}/100</strong>
                <p>${esc(
                  x.interpretation
                )}</p>
              </article>
            `
          )
          .join("");
    }

    if ($("liste-vigilances")) {
      $("liste-vigilances").innerHTML =
        warnings
          .slice(0, 5)
          .map(
            (x, i) => `
              <article class="insight-card warning">
                <span class="rank">#${i + 1}</span>
                <h3>${esc(
                  x.departement
                )}</h3>
                <strong>${num(
                  x.score
                )}/100</strong>
                <p>${esc(
                  x.interpretation
                )}</p>
              </article>
            `
          )
          .join("");
    }
  }

  function potential(d) {
    const places =
      d.lieux_potentiel || [];

    draw("graphe-potentiel", {
      type: "scatter",

      data: {
        datasets: [
          {
            label: "Lieux",

            data: places.map(
              (x) => ({
                x: Number(
                  x.preparation || 0
                ),

                y: Number(
                  x.popularite || 0
                ),
              })
            ),

            pointRadius: 5,
          },
        ],
      },

      options: {
        responsive: true,
        maintainAspectRatio: false,

        scales: {
          x: {
            ...scales.x,
            min: 0,
            max: 100,

            title: {
              display: true,
              text: "Préparation touristique (%)",
              color: "#dde4f0",
            },
          },

          y: {
            ...scales.y,

            title: {
              display: true,
              text: "Popularité TMDB",
              color: "#dde4f0",
            },
          },
        },
      },
    });

    if ($("top-lieux")) {
      $("top-lieux").innerHTML =
        places
          .slice(0, 6)
          .map(
            (x, i) => `
              <article class="insight-card">
                <span class="rank">#${i + 1}</span>
                <h3>${esc(
                  x.titre
                )}</h3>
                <p>${esc(
                  x.departement
                )}</p>

                <div class="mini-metrics">
                  <span>
                    Popularité
                    <b>${num(
                      x.popularite,
                      1
                    )}</b>
                  </span>

                  <span>
                    Préparation
                    <b>${pct(
                      x.preparation
                    )}</b>
                  </span>
                </div>
              </article>
            `
          )
          .join("");
    }
  }

  function departments(d) {
    if (!$("cartes-departements"))
      return;

    $("cartes-departements").innerHTML =
      (d.departements || [])
        .map(
          (x) => `
            <article class="carte-departement">

              <div class="dep-head">
                <h3>${esc(
                  x.departement
                )}</h3>

                <span>${pct(
                  x.part_pourcentage
                )}</span>
              </div>

              <div class="dep-grid">

                <div>
                  <b>${num(
                    x.nb_lieux
                  )}</b>
                  <small>lieux</small>
                </div>

                <div>
                  <b>${num(
                    x.nb_films
                  )}</b>
                  <small>œuvres</small>
                </div>

                <div>
                  <b>${pct(
                    x.pret_15_pct
                  )}</b>
                  <small>prêts 15 min</small>
                </div>

                <div>
                  <b>${pct(
                    x.pret_30_pct
                  )}</b>
                  <small>prêts 30 min</small>
                </div>

                <div>
                  <b>${pct(
                    x.isoles_45_pct
                  )}</b>
                  <small>isolés</small>
                </div>

                <div>
                  <b>${num(
                    x.popularite_moyenne,
                    1
                  )}</b>
                  <small>popularité</small>
                </div>

              </div>

              <p>${esc(
                x.recommandation
              )}</p>

            </article>
          `
        )
        .join("");
  }

  function quality(d) {
    const c = d.completude || {};

    if ($("completude-contenu")) {
      $("completude-contenu").innerHTML = [
        [
          "Coordonnées",
          pct(c.coordinates_pct),
        ],

        [
          "Équipements",
          pct(c.amenities_pct),
        ],

        [
          "Isochrones",
          pct(
            d.isochrones?.couverture_pct
          ),
        ],

        [
          "Popularité",
          pct(c.popularite_pct),
        ],
      ]
        .map(
          (x) => `
            <div class="kpi-card">
              <span>${esc(x[0])}</span>
              <strong>${esc(x[1])}</strong>
              <small>Couverture observée</small>
            </div>
          `
        )
        .join("");
    }

    if ($("methodologie")) {
      $("methodologie").innerHTML = `
        <h3>Méthodologie</h3>

        <p>
          ${esc(
            d.methodologie?.texte ||
              "Indicateurs calculés dynamiquement à partir des données publiées."
          )}
        </p>

        <p>
          <b>Important :</b>
          les isochrones mesurent une emprise spatiale
          de déplacement ; ils ne mesurent pas directement
          la population accessible.
        </p>
      `;
    }
  }

  function films(d) {
    if (!$("films-notables-cartes"))
      return;

    $("films-notables-cartes").innerHTML =
      (d.films_notables || [])
        .map(
          (f, i) => `
            <article class="carte-film-notable">

              <div class="rang-notable">
                #${i + 1}
              </div>

              <img
                src="${esc(
                  f.poster_url ||
                    "/icons/placeholder-poster.png"
                )}"
                alt="${esc(
                  f.titre
                )}"
                loading="lazy"
              >

              <h3>${esc(
                f.titre
              )}</h3>

              <small>
                ${num(
                  f.annee
                )} · ${
            f.media_type === "tv"
              ? "Série"
              : "Film"
          }
              </small>

            </article>
          `
        )
        .join("");
  }

  function render(data) {
    kpis(data);
    diagnostic(data);
    structure(data);
    access(data);
    iso(data);
    rankings(data);
    potential(data);
    departments(data);
    quality(data);
    films(data);
  }

  async function load() {
    const state = $("etat-donnees");

    if (state) {
      state.textContent =
        "Actualisation…";
    }

    try {
      const response = await fetch(
        "/api/analyse/indicateurs?region=Occitanie",
        {
          cache: "no-store",
          headers: {
            Accept: "application/json",
          },
        }
      );

      if (!response.ok) {
        const detail =
          await response.text();

        console.error(
          "API /api/analyse/indicateurs :",
          response.status,
          detail
        );

        throw new Error(
          `HTTP ${response.status} — ${detail}`
        );
      }

      const data =
        await response.json();

      render(data);

      if (state) {
        state.textContent =
          "● Données synchronisées";
      }

      if ($("date-maj")) {
        $("date-maj").textContent =
          "Mise à jour : " +
          new Date().toLocaleTimeString(
            "fr-FR"
          );
      }
    } catch (error) {
      console.error(
        "Erreur page Analyse :",
        error
      );

      if (state) {
        state.textContent =
          "⚠ Erreur de chargement";
      }

      if ($("diagnostic-titre")) {
        $("diagnostic-titre").textContent =
          "Impossible de charger les indicateurs";
      }

      if ($("diagnostic-texte")) {
        $("diagnostic-texte").textContent =
          "L'API d'analyse a rencontré une erreur. Consultez la console du navigateur et les logs du serveur pour identifier le problème.";
      }

      if ($("diagnostic-tags")) {
        $("diagnostic-tags").innerHTML =
          `<span>API /api/analyse/indicateurs</span>`;
      }
    }
  }

  document.addEventListener(
    "DOMContentLoaded",
    () => {
      load();

      $("btn-refresh")?.addEventListener(
        "click",
        load
      );

      setInterval(
        load,
        REFRESH_MS
      );
    }
  );
})();
