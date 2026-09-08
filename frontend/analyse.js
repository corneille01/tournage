(() => {
  "use strict";

  const REFRESH_MS = 5 * 60 * 1000;
  const charts = {};

  const $ = (id) => document.getElementById(id);

  // ------------------------------------------------------------
  // UTILITAIRES
  // ------------------------------------------------------------

  const esc = (value) =>
    String(value ?? "").replace(
      /[&<>"']/g,
      (char) =>
        ({
          "&": "&amp;",
          "<": "&lt;",
          ">": "&gt;",
          '"': "&quot;",
          "'": "&#039;",
        })[char]
    );

  const isNumber = (value) =>
    value !== null &&
    value !== undefined &&
    Number.isFinite(Number(value));

  const number = (value, decimals = 0) => {
    if (!isNumber(value)) {
      return "N/D";
    }

    return Number(value).toLocaleString("fr-FR", {
      minimumFractionDigits: decimals,
      maximumFractionDigits: decimals,
    });
  };

  const pct = (value) => {
    if (!isNumber(value)) {
      return "—";
    }

    return `${number(value, 1)} %`;
  };

  const km = (value) => {
    if (!isNumber(value)) {
      return "—";
    }

    return `${number(Number(value) / 1000, 2)} km`;
  };

  const metres = (value) => {
    if (!isNumber(value)) {
      return "—";
    }

    return `${number(value, 0)} m`;
  };

  const destroy = (id) => {
    if (charts[id]) {
      charts[id].destroy();
      delete charts[id];
    }
  };

  const canvas = (id) => {
    const element = $(id);

    if (!element) {
      return null;
    }

    return element.getContext("2d");
  };

  const chart = (id, config) => {
    const ctx = canvas(id);

    if (!ctx || typeof Chart === "undefined") {
      return;
    }

    destroy(id);

    charts[id] = new Chart(ctx, config);
  };

  const updateText = (id, value) => {
    const element = $(id);

    if (element) {
      element.textContent = value;
    }
  };

  // ------------------------------------------------------------
  // KPI PRINCIPAUX
  // ------------------------------------------------------------

  function updateKpis(data) {
    const totals = data.totaux || {};
    const access = data.accessibilite || {};
    const iso = data.isochrones || {};

    /*
     * Les IDs ci-dessous correspondent aux KPI du tableau
     * de bord. Plusieurs variantes sont supportées afin de
     * rester compatible avec l'HTML existant.
     */

    updateText(
      "kpi-films",
      number(totals.films)
    );

    updateText(
      "films",
      number(totals.films)
    );

    updateText(
      "kpi-lieux",
      number(totals.lieux)
    );

    updateText(
      "lieux",
      number(totals.lieux)
    );

    updateText(
      "kpi-departements",
      number(totals.departements)
    );

    updateText(
      "departements",
      number(totals.departements)
    );

    updateText(
      "kpi-pret15",
      pct(access.pret_15_pct)
    );

    updateText(
      "pret15",
      pct(access.pret_15_pct)
    );

    updateText(
      "kpi-isoles45",
      pct(access.isoles_45_pct)
    );

    updateText(
      "isoles45",
      pct(access.isoles_45_pct)
    );

    /*
     * Ici 100 % signifie :
     * tous les lieux disposent des isochrones calculées,
     * et non pas que tous les lieux sont accessibles en
     * 5 minutes.
     */
    updateText(
      "kpi-couverture-ign",
      pct(iso.couverture_pct)
    );

    updateText(
      "couverture-ign",
      pct(iso.couverture_pct)
    );

    updateText(
      "couverture-isochrones",
      pct(iso.couverture_pct)
    );
  }

  // ------------------------------------------------------------
  // STRUCTURE / CONCENTRATION TERRITORIALE
  // ------------------------------------------------------------

  function calculateTop3Share(departements) {
    if (!Array.isArray(departements) || !departements.length) {
      return null;
    }

    return departements
      .slice(0, 3)
      .reduce(
        (total, dep) =>
          total + Number(dep.part_pct || 0),
        0
      );
  }

  function structure(data) {
    const deps = Array.isArray(data.departements)
      ? data.departements
      : [];

    const top3 = calculateTop3Share(deps);

    updateText(
      "concentration-territoriale",
      pct(top3)
    );

    updateText(
      "concentration-top3",
      pct(top3)
    );

    chart("graphe-lieux", {
      type: "bar",
      data: {
        labels: deps.map(
          (item) => item.departement
        ),
        datasets: [
          {
            label: "Lieux de tournage",
            data: deps.map(
              (item) => item.nb_lieux || 0
            ),
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
          y: {
            beginAtZero: true,
          },
        },
      },
    });

    if (top3 !== null) {
      chart("graphe-concentration", {
        type: "doughnut",
        data: {
          labels: [
            "Top 3 départements",
            "Autres",
          ],
          datasets: [
            {
              data: [
                top3,
                Math.max(0, 100 - top3),
              ],
            },
          ],
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
        },
      });
    }
  }

  // ------------------------------------------------------------
  // ÉQUIPEMENT TOURISTIQUE
  // ------------------------------------------------------------

  function access(data) {
    const e = data.equipements || {};
    const a = data.accessibilite || {};

    updateText(
      "hebergement-moyen",
      km(e.moy_hebergement)
    );

    updateText(
      "restaurant-moyen",
      km(e.moy_restaurant)
    );

    updateText(
      "hebergement-presence",
      pct(e.hebergement_presence_pct)
    );

    updateText(
      "restaurant-presence",
      pct(e.restaurant_presence_pct)
    );

    updateText(
      "hebergement-500m",
      pct(e.hebergement_500m_pct)
    );

    updateText(
      "restaurant-500m",
      pct(e.restaurant_500m_pct)
    );

    const interpretation = $("interpretation-equipement");

    if (interpretation) {
      interpretation.textContent =
        `Hébergement : ${pct(
          e.hebergement_presence_pct
        )} des lieux disposent d'au moins un hébergement observé. ` +
        `Restaurant : ${pct(
          e.restaurant_presence_pct
        )}. ` +
        `La distance moyenne du plus proche hébergement est de ${km(
          e.moy_hebergement
        )}, contre ${km(
          e.moy_restaurant
        )} pour le restaurant.`;
    }

    chart("graphe-equipement", {
      type: "bar",
      data: {
        labels: [
          "Hébergement présent",
          "Restaurant présent",
          "Hébergement ≤ 500 m",
          "Restaurant ≤ 500 m",
        ],
        datasets: [
          {
            label: "Part des lieux",
            data: [
              e.hebergement_presence_pct ?? null,
              e.restaurant_presence_pct ?? null,
              e.hebergement_500m_pct ?? null,
              e.restaurant_500m_pct ?? null,
            ],
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        scales: {
          y: {
            beginAtZero: true,
            max: 100,
          },
        },
      },
    });

    /*
     * Les indicateurs 15 / 30 / 45 min restent N/D
     * tant que les durées réelles de routage ne sont
     * pas disponibles pour calculer ces seuils.
     */

    radial(
      "graphe-pret15",
      a.pret_15_pct,
      "Prêts à 15 min"
    );

    radial(
      "graphe-pret30",
      a.pret_30_pct,
      "Prêts à 30 min"
    );

    radial(
      "graphe-isoles",
      a.isoles_45_pct,
      "Isolés > 45 min"
    );

    const routeMessage = isNumber(
      a.route_coverage_pct
    )
      ? `Couverture des durées de routage : ${pct(
          a.route_coverage_pct
        )}.`
      : "Les durées réelles d’itinéraire ne sont pas suffisamment renseignées pour calculer les seuils 15, 30 et 45 minutes.";

    if (interpretation) {
      interpretation.textContent += ` ${routeMessage}`;
    }

    updateText(
      "route-coverage",
      pct(a.route_coverage_pct)
    );
  }

  // ------------------------------------------------------------
  // RADIAL
  // ------------------------------------------------------------

  function radial(id, value, label) {
    const ctx = canvas(id);

    if (!ctx) {
      return;
    }

    destroy(id);

    /*
     * IMPORTANT :
     * null = donnée indisponible.
     * On n'affiche donc pas un faux 0 %.
     */

    if (!isNumber(value)) {
      charts[id] = new Chart(ctx, {
        type: "doughnut",
        data: {
          labels: ["Donnée indisponible"],
          datasets: [
            {
              data: [1],
            },
          ],
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          cutout: "70%",
          plugins: {
            legend: {
              display: false,
            },
          },
        },
      });

      return;
    }

    const numeric = Number(value);

    charts[id] = new Chart(ctx, {
      type: "doughnut",
      data: {
        labels: [
          label,
          "Non couvert",
        ],
        datasets: [
          {
            data: [
              numeric,
              Math.max(0, 100 - numeric),
            ],
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        cutout: "70%",
      },
    });
  }

  // ------------------------------------------------------------
  // MOBILITÉ / ISOCHRONES IGN
  // ------------------------------------------------------------

  function mobility(data) {
    const i = data.isochrones || {};

    const coverage = Array.isArray(
      i.couverture_par_minutes
    )
      ? i.couverture_par_minutes
      : [];

    const minutes = Array.isArray(
      i.minutes_disponibles
    )
      ? i.minutes_disponibles
      : [];

    const labels = coverage.map(
      (item) => `${item.minutes} min`
    );

    chart("graphe-isochrones", {
      type: "line",
      data: {
        labels,
        datasets: [
          {
            label: "Voiture — couverture des calculs",
            data: coverage.map(
              (item) =>
                item.voiture_pct ?? null
            ),
            tension: 0.25,
          },
          {
            label: "À pied — couverture des calculs",
            data: coverage.map(
              (item) =>
                item.pied_pct ?? null
            ),
            tension: 0.25,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        scales: {
          y: {
            min: 0,
            max: 100,
            title: {
              display: true,
              text: "Part des lieux couverts par une isochrone",
            },
          },
        },
      },
    });

    const ratio =
      i.ratio_surface_voiture_marche_15;

    updateText(
      "ratio-mobilite",
      isNumber(ratio)
        ? number(ratio, 2)
        : "—"
    );

    const freshness =
      $("fraicheur-isochrones");

    if (freshness) {
      if (minutes.length) {
        freshness.textContent =
          `Isochrones réellement disponibles : ${minutes.join(
            ", "
          )} minutes.`;
      } else {
        freshness.textContent =
          "Aucune isochrone disponible.";
      }
    }

    updateText(
      "couverture-ign",
      pct(i.couverture_pct)
    );
  }

  // ------------------------------------------------------------
  // POTENTIEL / OPPORTUNITÉS
  // ------------------------------------------------------------

  function potential(data) {
    const rows = Array.isArray(
      data.lieux_potentiel
    )
      ? data.lieux_potentiel
      : [];

    const validRows = rows.filter(
      (row) =>
        isNumber(
          row.maturite_touristique_pct
        ) &&
        isNumber(row.popularite_score)
    );

    chart("graphe-potentiel", {
      type: "scatter",
      data: {
        datasets: [
          {
            label: "Lieux",
            data: validRows.map(
              (row) => ({
                x:
                  row.maturite_touristique_pct,
                y:
                  row.popularite_score,
              })
            ),
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        scales: {
          x: {
            min: 0,
            max: 100,
            title: {
              display: true,
              text:
                "Maturité touristique observée (%)",
            },
          },
          y: {
            min: 0,
            max: 100,
            title: {
              display: true,
              text:
                "Attractivité film",
            },
          },
        },
      },
    });

    const list = $("liste-opportunites");

    if (!list) {
      return;
    }

    const opportunities = rows
      .filter(
        (row) =>
          isNumber(
            row.score_opportunite
          )
      )
      .sort(
        (a, b) =>
          Number(
            b.score_opportunite
          ) -
          Number(
            a.score_opportunite
          )
      )
      .slice(0, 12);

    if (!opportunities.length) {
      list.innerHTML = `
        <div class="analyse-empty">
          Aucune opportunité calculable avec les données disponibles.
        </div>
      `;
      return;
    }

    list.innerHTML = opportunities
      .map(
        (row) => `
          <div class="analyse-item">
            <strong>
              ${esc(row.titre || "Œuvre")}
            </strong>

            <span>
              ${esc(row.commune || "")}
              ${
                row.departement
                  ? ` · ${esc(row.departement)}`
                  : ""
              }
            </span>

            <small>
              Score :
              ${number(
                row.score_opportunite,
                1
              )}/100
              —
              Maturité :
              ${pct(
                row.maturite_touristique_pct
              )}
              —
              Popularité :
              ${
                isNumber(row.popularite)
                  ? number(
                      row.popularite,
                      2
                    )
                  : "N/D"
              }
            </small>
          </div>
        `
      )
      .join("");
  }

  // ------------------------------------------------------------
  // FRAGILITÉS / VIGILANCES
  // ------------------------------------------------------------

  function vigilance(data) {
    const rows = Array.isArray(
      data.lieux_potentiel
    )
      ? data.lieux_potentiel
      : [];

    /*
     * On cherche les lieux ayant la maturité
     * touristique observée la plus faible.
     */

    const fragile = [...rows]
      .filter(
        (row) =>
          isNumber(
            row.maturite_touristique_pct
          )
      )
      .sort(
        (a, b) =>
          Number(
            a.maturite_touristique_pct
          ) -
          Number(
            b.maturite_touristique_pct
          )
      )
      .slice(0, 12);

    chart("graphe-vigilances", {
      type: "bar",
      data: {
        labels: fragile.map(
          (row) =>
            row.commune ||
            row.titre ||
            "Lieu"
        ),
        datasets: [
          {
            label:
              "Maturité touristique observée",
            data: fragile.map(
              (row) =>
                row.maturite_touristique_pct
            ),
          },
        ],
      },
      options: {
        indexAxis: "y",
        responsive: true,
        maintainAspectRatio: false,
        scales: {
          x: {
            min: 0,
            max: 100,
          },
        },
      },
    });

    const list = $("liste-vigilances");

    if (!list) {
      return;
    }

    if (!fragile.length) {
      list.innerHTML = `
        <div class="analyse-empty">
          Aucune fragilité calculable avec les données disponibles.
        </div>
      `;
      return;
    }

    list.innerHTML = fragile
      .map(
        (row) => `
          <div class="analyse-item">
            <strong>
              ${esc(row.titre || "Lieu")}
            </strong>

            <span>
              ${esc(row.commune || "")}
              ${
                row.departement
                  ? ` · ${esc(row.departement)}`
                  : ""
              }
            </span>

            <small>
              Maturité observée :
              ${pct(
                row.maturite_touristique_pct
              )}
              —
              Hébergement :
              ${
                row.hebergement
                  ? "présent"
                  : "non observé"
              }
              —
              Restaurant :
              ${
                row.restaurant
                  ? "présent"
                  : "non observé"
              }
            </small>
          </div>
        `
      )
      .join("");
  }

  // ------------------------------------------------------------
  // FILMOGRAPHIE
  // ------------------------------------------------------------

  function films(data) {
    const rows = Array.isArray(
      data.films_notables
    )
      ? data.films_notables
      : [];

    const container =
      $("films-notables-cartes");

    if (!container) {
      return;
    }

    if (!rows.length) {
      container.innerHTML = `
        <div class="analyse-empty">
          Aucune œuvre notable calculable.
        </div>
      `;
      return;
    }

    container.innerHTML = rows
      .map(
        (film) => `
          <article class="film-card">
            <div class="film-card-content">

              <h3>
                ${esc(
                  film.titre || "Sans titre"
                )}
              </h3>

              <p>
                ${
                  film.annee
                    ? esc(film.annee)
                    : ""
                }

                ${
                  film.media_type
                    ? ` · ${esc(
                        film.media_type
                      )}`
                    : ""
                }
              </p>

              <strong>
                ${number(
                  film.popularite,
                  2
                )}
              </strong>

              <small>
                ${number(
                  film.nb_lieux
                )}
                lieux ·
                ${number(
                  film.nb_departements
                )}
                départements
              </small>

            </div>
          </article>
        `
      )
      .join("");
  }

  // ------------------------------------------------------------
  // TABLEAU DE BORD DÉPARTEMENTAL
  // ------------------------------------------------------------

  function territories(data) {
    const rows = Array.isArray(
      data.departements
    )
      ? data.departements
      : [];

    const container =
      $("cartes-departements");

    if (!container) {
      return;
    }

    if (!rows.length) {
      container.innerHTML = `
        <div class="analyse-empty">
          Aucun territoire représenté.
        </div>
      `;
      return;
    }

    container.innerHTML = rows
      .map(
        (row) => `
          <article class="territoire-card">

            <h3>
              ${esc(
                row.departement
              )}
            </h3>

            <strong>
              ${number(
                row.nb_lieux
              )}
              lieux
            </strong>

            <p>
              ${number(
                row.nb_films
              )}
              œuvres ·
              ${pct(
                row.part_pct
              )}
              des lieux régionaux
            </p>

          </article>
        `
      )
      .join("");
  }

  // ------------------------------------------------------------
  // QUALITÉ / COUVERTURE
  // ------------------------------------------------------------

  function completeness(data) {
    const c =
      data.completude || {};

    const container =
      $("completude-contenu");

    if (!container) {
      return;
    }

    container.innerHTML = `
      <div class="completude-grid">

        <div>
          <strong>
            ${pct(c.coordonnees_pct)}
          </strong>
          <span>
            Coordonnées
          </span>
        </div>

        <div>
          <strong>
            ${pct(c.amenities_pct)}
          </strong>
          <span>
            Équipements touristiques
          </span>
        </div>

        <div>
          <strong>
            ${pct(c.popularite_pct)}
          </strong>
          <span>
            Popularité des œuvres
          </span>
        </div>

        <div>
          <strong>
            ${pct(c.isochrones_pct)}
          </strong>
          <span>
            Isochrones
          </span>
        </div>

        <div>
          <strong>
            ${pct(c.routage_pct)}
          </strong>
          <span>
            Durées de routage
          </span>
        </div>

      </div>
    `;
  }

  // ------------------------------------------------------------
  // INDICATEURS DE DÉCISION
  // ------------------------------------------------------------

  function addDecisionIndicators(data) {
    /*
     * On cherche une zone stable de la page.
     * Priorité à la section "Potentiel" / opportunités.
     */

    const target =
      $("liste-opportunites")?.closest(
        ".analyse-section"
      );

    if (!target) {
      return;
    }

    const old =
      $("observatoire-indicateurs-v2");

    if (old) {
      old.remove();
    }

    const e =
      data.equipements || {};

    const m =
      data.metriques || {};

    const wrapper =
      document.createElement("div");

    wrapper.id =
      "observatoire-indicateurs-v2";

    wrapper.className =
      "insight-grid";

    wrapper.innerHTML = `
      <div class="insight-card">

        <span>
          Maturité touristique observée
        </span>

        <strong>
          ${pct(
            m.maturite_touristique_pct
          )}
        </strong>

        <small>
          Présence observée d'hébergement
          et de restauration autour des
          lieux de tournage.
        </small>

      </div>

      <div class="insight-card">

        <span>
          Hébergements observés
        </span>

        <strong>
          ${pct(
            e.hebergement_presence_pct
          )}
        </strong>

        <small>
          Lieux disposant d'au moins
          un hébergement DATAtourisme.
        </small>

      </div>

      <div class="insight-card">

        <span>
          Restaurants observés
        </span>

        <strong>
          ${pct(
            e.restaurant_presence_pct
          )}
        </strong>

        <small>
          Lieux disposant d'au moins
          un restaurant DATAtourisme.
        </small>

      </div>

      <div class="insight-card">

        <span>
          Distance moyenne hébergement
        </span>

        <strong>
          ${km(
            e.moy_hebergement
          )}
        </strong>

        <small>
          Distance du plus proche
          hébergement observé.
        </small>

      </div>

      <div class="insight-card">

        <span>
          Distance moyenne restaurant
        </span>

        <strong>
          ${km(
            e.moy_restaurant
          )}
        </strong>

        <small>
          Distance du plus proche
          restaurant observé.
        </small>

      </div>
    `;

    target.appendChild(wrapper);
  }

  // ------------------------------------------------------------
  // DIAGNOSTIC
  // ------------------------------------------------------------

  function diagnostic(data) {
    const element =
      $("diagnostic");

    if (!element) {
      return;
    }

    const totals =
      data.totaux || {};

    element.textContent =
      `Observation régionale : ${number(
        totals.films
      )} œuvres publiées, ${number(
        totals.lieux
      )} lieux géolocalisés et ${number(
        totals.departements
      )} départements représentés.`;
  }

  // ------------------------------------------------------------
  // RENDU GLOBAL
  // ------------------------------------------------------------

  function render(data) {
    if (!data) {
      return;
    }

    console.log(
      "Rendu observatoire :",
      data
    );

    diagnostic(data);
    updateKpis(data);

    structure(data);
    access(data);
    mobility(data);

    potential(data);
    vigilance(data);

    films(data);
    territories(data);

    completeness(data);
    addDecisionIndicators(data);
  }

  // ------------------------------------------------------------
  // CHARGEMENT API
  // ------------------------------------------------------------

  async function load() {
    try {
      const response =
        await fetch(
          "/api/analyse/indicateurs?region=Occitanie",
          {
            cache: "no-store",
          }
        );

      if (!response.ok) {
        throw new Error(
          `HTTP ${response.status}`
        );
      }

      const data =
        await response.json();

      render(data);

    } catch (error) {

      console.error(
        "Erreur observatoire :",
        error
      );

      const diagnostic =
        $("diagnostic");

      if (diagnostic) {
        diagnostic.textContent =
          "Impossible de charger les indicateurs.";
      }
    }
  }

  // ------------------------------------------------------------
  // INITIALISATION
  // ------------------------------------------------------------

  document.addEventListener(
    "DOMContentLoaded",
    () => {

      load();

      setInterval(
        load,
        REFRESH_MS
      );

    }
  );

})();