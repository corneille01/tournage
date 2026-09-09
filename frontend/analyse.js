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

    const container = $("totaux-cartes");

    if (container) {
      const cartes = [
        { valeur: number(totals.films), label: "Œuvres publiées" },
        { valeur: number(totals.lieux), label: "Lieux de tournage" },
        { valeur: number(totals.departements), label: "Départements représentés" },
        { valeur: pct(access.pret_15_pct), label: "Lieux prêts à 15 min" },
        { valeur: pct(access.isoles_45_pct), label: "Lieux isolés (+45 min)" },
        { valeur: pct(iso.couverture_pct), label: "Couverture isochrones IGN" },
      ];

      container.innerHTML = cartes
        .map(
          (c) => `
            <div class="kpi-card">
              <strong>${c.valeur}</strong>
              <span>${c.label}</span>
            </div>
          `
        )
        .join("");
    }

    /*
     * Les IDs ci-dessous sont conservés pour compatibilité avec
     * d'éventuelles variantes de gabarit HTML qui exposeraient ces
     * éléments individuellement (mise en page alternative).
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

    const m = data.metriques || {};

    // On préfère la valeur calculée côté backend (cohérente avec les
    // autres indicateurs de metriques) ; le calcul local ne sert que
    // de filet de sécurité si jamais elle manquait.
    const top3 = isNumber(m.concentration_top3_pct)
      ? m.concentration_top3_pct
      : calculateTop3Share(deps);

    const hhi = isNumber(m.concentration_hhi) ? m.concentration_hhi : null;

    updateText("top3-value", pct(top3));

    updateText("hhi-value", hhi !== null ? number(hhi, 2) : "—");

    // Seuils usuels de lecture d'un indice Herfindahl-Hirschman
    // (échelle 0-1 ici, cf. _hhi() côté backend) :
    //   < 0,15 : répartition diversifiée entre départements
    //   0,15 - 0,25 : concentration modérée
    //   > 0,25 : concentration forte sur peu de départements
    const hhiLabel = $("hhi-label");
    if (hhiLabel) {
      if (hhi === null) {
        hhiLabel.textContent = "";
      } else if (hhi < 0.15) {
        hhiLabel.textContent = "Répartition diversifiée entre départements.";
      } else if (hhi < 0.25) {
        hhiLabel.textContent = "Concentration modérée sur quelques départements.";
      } else {
        hhiLabel.textContent = "Forte concentration sur un petit nombre de départements.";
      }
    }

    const interpretationLieux = $("interpretation-lieux");
    if (interpretationLieux) {
      if (deps.length) {
        const premier = deps[0];
        interpretationLieux.textContent =
          `${esc(premier.departement)} concentre ${number(premier.nb_lieux)} lieux ` +
          `(${pct(premier.part_pct)} du total régional)` +
          (deps.length > 1 ? `, devant ${esc(deps[1].departement)}.` : ".");
      } else {
        interpretationLieux.textContent = "";
      }
    }

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
    const m = data.metriques || {};

    updateText(
      "hebergement-moyen",
      isNumber(e.moy_hebergement) ? number(e.moy_hebergement, 1) : "N/D"
    );

    updateText(
      "restaurant-moyen",
      isNumber(e.moy_restaurant) ? number(e.moy_restaurant, 1) : "N/D"
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
          m.distance_moy_hebergement_m
        )}, contre ${km(
          m.distance_moy_restaurant_m
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

    const topLieux = $("top-lieux");
    if (topLieux) {
      const top5 = rows
        .filter((row) => isNumber(row.score_opportunite))
        .sort((a, b) => Number(b.score_opportunite) - Number(a.score_opportunite))
        .slice(0, 5);

      if (!top5.length) {
        topLieux.innerHTML = `<div class="analyse-empty">Aucun lieu classable pour l'instant.</div>`;
      } else {
        topLieux.innerHTML = top5
          .map(
            (row, index) => `
              <div class="analyse-item">
                <strong>#${index + 1} — ${esc(row.titre || "Œuvre")}</strong>
                <span>${esc(row.commune || "")}${row.departement ? ` · ${esc(row.departement)}` : ""}</span>
                <small>Score : ${number(row.score_opportunite, 1)}/100</small>
              </div>
            `
          )
          .join("");
      }
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

    if (opportunities.length) {
      chart("graphe-opportunites", {
        type: "bar",
        data: {
          labels: opportunities.map((row) => row.titre || "Œuvre"),
          datasets: [
            {
              label: "Score d'opportunité (/100)",
              data: opportunities.map((row) => row.score_opportunite),
            },
          ],
        },
        options: {
          indexAxis: "y",
          responsive: true,
          maintainAspectRatio: false,
          scales: {
            x: { min: 0, max: 100 },
          },
          plugins: {
            legend: { display: false },
          },
        },
      });
    }

    const list = $("liste-opportunites");

    if (!list) {
      return;
    }

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
      .map((row) => {
        const score = isNumber(row.score_investissement)
          ? row.score_investissement
          : null;
        const scoreClass =
          score === null ? "" : score >= 60 ? "score-haut" : score >= 35 ? "score-moyen" : "score-bas";

        const benchmarks = [row.benchmark_hebergement, row.benchmark_enclavement]
          .filter((b) => b && b !== "non comparable")
          .map((b) => esc(b))
          .join(" · ");

        return `
          <article class="carte-departement">

            <div class="dep-head">
              <h3>${esc(row.departement)}</h3>
              <span class="${scoreClass}">${
                score !== null ? `Priorité ${number(score, 0)}/100` : `${number(row.nb_lieux)} lieux`
              }</span>
            </div>

            <div class="dep-grid">
              <div>
                <b>${isNumber(row.moy_hebergement) ? number(row.moy_hebergement, 1) : "N/D"}</b>
                <small>Hébergement moyen</small>
              </div>
              <div>
                <b>${pct(row.pret_15_pct)}</b>
                <small>Prêt à 15 min</small>
              </div>
              <div>
                <b>${isNumber(row.surface_moyenne_15min_km2) ? `${number(row.surface_moyenne_15min_km2, 1)} km²` : "N/D"}</b>
                <small>Surface 15 min voiture</small>
              </div>
            </div>

            <p>
              ${number(row.nb_films)} œuvres · ${pct(row.part_pct)} des lieux régionaux
              ${benchmarks ? ` · ${benchmarks}` : ""}
            </p>

          </article>
        `;
      })
      .join("");
  }

  // ------------------------------------------------------------
  // PRIORISATION DÉPARTEMENTALE — outil d'aide à la décision
  // ------------------------------------------------------------

  function investmentPriority(data) {
    const rows = Array.isArray(data.priorisation_departementale)
      ? data.priorisation_departementale
      : [];

    const listEl = $("liste-priorisation");
    if (listEl) {
      if (!rows.length) {
        listEl.innerHTML = `<div class="analyse-empty">Pas assez de données pour établir un classement de priorité.</div>`;
      } else {
        listEl.innerHTML = rows
          .map((row, index) => {
            const raisons = [];
            if (isNumber(row.popularite_moyenne)) {
              raisons.push(`popularité cinématographique moyenne de ${number(row.popularite_moyenne, 1)}`);
            }
            if (isNumber(row.hebergement_presence_pct) && isNumber(row.restaurant_presence_pct)) {
              raisons.push(
                `hébergement présent pour ${pct(row.hebergement_presence_pct)} des lieux, restauration pour ${pct(row.restaurant_presence_pct)}`
              );
            }
            if (isNumber(row.surface_moyenne_15min_km2)) {
              raisons.push(`${number(row.surface_moyenne_15min_km2, 1)} km² accessibles en 15 min en voiture en moyenne`);
            }

            return `
              <article class="priorite-card">
                <div class="priorite-rang">#${index + 1}</div>
                <div class="priorite-corps">
                  <div class="priorite-head">
                    <h3>${esc(row.departement)}</h3>
                    <strong class="priorite-score">${number(row.score_investissement, 0)}<small>/100</small></strong>
                  </div>
                  <p>${raisons.length ? "Basé sur : " + esc(raisons.join(" — ")) + "." : ""}</p>
                </div>
              </article>
            `;
          })
          .join("");
      }
    }

    chart("graphe-priorisation", {
      type: "bar",
      data: {
        labels: rows.map((row) => row.departement),
        datasets: [
          {
            label: "Score de priorité d'investissement (/100)",
            data: rows.map((row) => row.score_investissement),
          },
        ],
      },
      options: {
        indexAxis: "y",
        responsive: true,
        maintainAspectRatio: false,
        scales: {
          x: { min: 0, max: 100 },
        },
      },
    });
  }

  // ------------------------------------------------------------
  // QUALITÉ / COUVERTURE
  // ------------------------------------------------------------

  function completeness(data) {
    const c =
      data.completude || {};
    const accessRoute =
      (data.accessibilite || {}).route_coverage_pct;

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
            ${pct(c.amenagement_pct)}
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
            ${pct(accessRoute)}
          </strong>
          <span>
            Durées de routage
          </span>
        </div>

      </div>
    `;

    const methodo = $("methodologie");
    if (methodo) {
      methodo.innerHTML = `
        <p>
          Indicateurs recalculés à chaque chargement à partir des données
          actuellement en base : lieux de tournage publiés, équipements
          touristiques DATAtourisme, durées de trajet routières réelles et
          isochrones IGN pré-calculées. Aucune valeur n'est estimée ou
          inventée : un indicateur affiché « N/D » signifie que la donnée
          source correspondante n'est pas encore disponible pour tous les
          lieux concernés.
        </p>
      `;
    }
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

  function calculateRegionalScore(data) {
    const deps = Array.isArray(data.priorisation_departementale)
      ? data.priorisation_departementale
      : [];

    const totalLieux = deps.reduce(
      (sum, d) => sum + (Number(d.nb_lieux) || 0),
      0
    );

    if (!deps.length || !totalLieux) {
      return null;
    }

    const somme = deps.reduce(
      (sum, d) =>
        sum + (Number(d.score_investissement) || 0) * (Number(d.nb_lieux) || 0),
      0
    );

    return somme / totalLieux;
  }

  function diagnostic(data) {
    const totals = data.totaux || {};
    const e = data.equipements || {};
    const a = data.accessibilite || {};
    const c = data.completude || {};

    const score = calculateRegionalScore(data);

    updateText("score-regional", score !== null ? number(score, 0) : "—");

    const titre = $("diagnostic-titre");
    if (titre) {
      if (score === null) {
        titre.textContent = "Analyse en cours…";
      } else if (score >= 60) {
        titre.textContent = "Marge de progression importante pour le ciné-tourisme régional";
      } else if (score >= 35) {
        titre.textContent = "Un potentiel ciné-touristique partiellement exploité";
      } else {
        titre.textContent = "Un territoire déjà globalement bien préparé";
      }
    }

    const texte = $("diagnostic-texte");
    if (texte) {
      texte.textContent =
        `Observation régionale : ${number(totals.films)} œuvres publiées, ` +
        `${number(totals.lieux)} lieux géolocalisés et ${number(totals.departements)} ` +
        `départements représentés.`;
    }

    const tagsEl = $("diagnostic-tags");
    if (tagsEl) {
      const tags = [];

      if (isNumber(e.hebergement_presence_pct)) {
        tags.push(`Hébergement observé : ${pct(e.hebergement_presence_pct)} des lieux`);
      }
      if (isNumber(a.pret_15_pct)) {
        tags.push(`Prêts à 15 min : ${pct(a.pret_15_pct)}`);
      }
      if (isNumber(c.isochrones_pct)) {
        tags.push(`Isochrones calculées : ${pct(c.isochrones_pct)}`);
      }
      if (isNumber(a.route_coverage_pct)) {
        tags.push(`Durées de trajet connues : ${pct(a.route_coverage_pct)}`);
      }

      tagsEl.innerHTML = tags.map((t) => `<span>${esc(t)}</span>`).join("");
    }
  }

  // ------------------------------------------------------------
  // RENDU GLOBAL
  // ------------------------------------------------------------

  // ------------------------------------------------------------
  // OFFRE TOURISTIQUE DÉTAILLÉE (dictionnaire statistique complet)
  // ------------------------------------------------------------

  const LABELS_CATEGORIE_OBSERVATOIRE = {
    hebergement: "Hébergement",
    restaurant: "Restauration",
    activite: "Activités",
    office_tourisme: "Office de tourisme",
    refuge: "Refuge",
    gare: "Gare",
    aeroport: "Aéroport",
    aerodrome: "Aérodrome",
    arret_bus: "Arrêt de bus",
    parking: "Parking",
    distributeur: "Distributeur",
    police: "Police",
    hopital: "Hôpital",
  };

  function libelleCategorie(cle) {
    return LABELS_CATEGORIE_OBSERVATOIRE[cle] || cle;
  }

  function interpretationCategorie(nomCategorie, bloc) {
    const off = bloc.nombre || {};
    const prox = bloc.distance_plus_proche_m || {};
    const phrases = [];

    if (isNumber(off.moyenne) && isNumber(off.mediane)) {
      const ratio = off.mediane > 0 ? off.moyenne / off.mediane : null;
      if (ratio !== null && ratio >= 2) {
        phrases.push(
          `L'offre moyenne (${number(off.moyenne, 1)}) est nettement supérieure à la médiane ` +
          `(${number(off.mediane, 1)}), signe qu'une partie des lieux concentre l'essentiel de l'offre.`
        );
      } else {
        phrases.push(
          `Offre médiane de ${number(off.mediane, 1)} équipement(s) par lieu (moyenne : ${number(off.moyenne, 1)}).`
        );
      }
    }

    if (isNumber(off.cv_pct) && off.cv_pct >= 80) {
      phrases.push(`Dispersion importante entre lieux (CV : ${number(off.cv_pct, 0)} %) : l'offre n'est pas homogène.`);
    }

    if (isNumber(prox.p90)) {
      phrases.push(
        `Pour 90 % des lieux disposant de cet équipement, le plus proche se situe à moins de ${km(prox.p90)}.`
      );
    }

    return phrases.join(" ");
  }

  function renderOffreCategories(equipements) {
    const container = $("offre-categories");
    if (!container) {
      return;
    }

    const cles = Object.keys(equipements || {});

    if (!cles.length) {
      container.innerHTML = `<div class="analyse-empty">Aucune catégorie d'équipement disponible pour l'instant.</div>`;
      return;
    }

    container.innerHTML = cles
      .map((cle) => {
        const bloc = equipements[cle] || {};
        const off = bloc.nombre || {};
        const prox = bloc.distance_plus_proche_m || {};
        const proximite = bloc.proximite || {};
        const carence = bloc.carence || {};

        return `
          <article class="offre-carte">
            <div class="offre-carte-head">
              <h3>${esc(libelleCategorie(cle))}</h3>
              <span>N = ${number(bloc.n)}</span>
            </div>

            <div class="offre-grid">
              <div>
                <b>${isNumber(off.mediane) ? number(off.mediane, 1) : "N/D"}</b>
                <small>Médiane / lieu</small>
              </div>
              <div>
                <b>${isNumber(off.ecart_type) ? number(off.ecart_type, 1) : "N/D"}</b>
                <small>Écart-type</small>
              </div>
              <div>
                <b>${isNumber(prox.mediane) ? km(prox.mediane) : "N/D"}</b>
                <small>Distance médiane</small>
              </div>
              <div>
                <b>${isNumber(prox.p90) ? km(prox.p90) : "N/D"}</b>
                <small>P90</small>
              </div>
              <div>
                <b>${pct(proximite.a_500m_pct)}</b>
                <small>À ≤ 500 m</small>
              </div>
              <div>
                <b>${number(carence.sans_equipement_n)}</b>
                <small>Lieux sans équipement</small>
              </div>
            </div>

            <p>${esc(interpretationCategorie(cle, bloc))}</p>
          </article>
        `;
      })
      .join("");
  }

  function renderOffreCharts(equipements) {
    const cles = Object.keys(equipements || {});

    if (!cles.length) {
      return;
    }

    const labels = cles.map(libelleCategorie);

    chart("graphe-offre-nombre", {
      type: "bar",
      data: {
        labels,
        datasets: [
          {
            label: "Nombre médian d'équipements par lieu",
            data: cles.map((c) => (equipements[c].nombre || {}).mediane),
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { display: false } },
      },
    });

    chart("graphe-offre-distance", {
      type: "bar",
      data: {
        labels,
        datasets: [
          {
            label: "Distance médiane (km)",
            data: cles.map((c) => {
              const v = (equipements[c].distance_plus_proche_m || {}).mediane;
              return isNumber(v) ? Number(v) / 1000 : null;
            }),
          },
          {
            label: "P90 (km)",
            data: cles.map((c) => {
              const v = (equipements[c].distance_plus_proche_m || {}).p90;
              return isNumber(v) ? Number(v) / 1000 : null;
            }),
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
      },
    });
  }

  function renderTableauDepartemental(departements) {
    const table = $("tableau-departemental");
    if (!table) {
      return;
    }

    const rows = Array.isArray(departements) ? departements : [];

    if (!rows.length) {
      table.innerHTML = "";
      return;
    }

    const entetes = [
      "Département", "N lieux", "Moyenne", "Médiane", "Écart-type", "CV",
      "Distance médiane", "P90", "≤ 500 m", "Sans équipement", "Position régionale",
    ];

    table.innerHTML = `
      <thead>
        <tr>${entetes.map((e) => `<th>${esc(e)}</th>`).join("")}</tr>
      </thead>
      <tbody>
        ${rows
          .map(
            (row) => `
              <tr>
                <td>${esc(row.departement)}</td>
                <td>${number(row.n_lieux)}</td>
                <td>${isNumber(row.moyenne) ? number(row.moyenne, 1) : "N/D"}</td>
                <td>${isNumber(row.mediane) ? number(row.mediane, 1) : "N/D"}</td>
                <td>${isNumber(row.ecart_type) ? number(row.ecart_type, 1) : "N/D"}</td>
                <td>${isNumber(row.cv_pct) ? pct(row.cv_pct) : "N/D"}</td>
                <td>${isNumber(row.distance_mediane_m) ? km(row.distance_mediane_m) : "N/D"}</td>
                <td>${isNumber(row.distance_p90_m) ? km(row.distance_p90_m) : "N/D"}</td>
                <td>${pct(row.a_500m_pct)}</td>
                <td>${number(row.sans_equipement_n)}</td>
                <td>${row.position_regionale ? esc(row.position_regionale) : "N/D"}</td>
              </tr>
            `
          )
          .join("")}
      </tbody>
    `;
  }

  function renderObservatoire(obs) {
    if (!obs) {
      return;
    }

    renderOffreCategories(obs.equipements);
    renderOffreCharts(obs.equipements);
    renderTableauDepartemental(obs.departements);

    const avertissements = $("avertissements-methodologiques");
    if (avertissements) {
      const items = Array.isArray(obs.avertissements_methodologiques)
        ? obs.avertissements_methodologiques
        : [];

      avertissements.innerHTML = items.length
        ? `<ul>${items.map((texte) => `<li>${esc(texte)}</li>`).join("")}</ul>`
        : "";
    }
  }

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

    investmentPriority(data);

    films(data);
    territories(data);

    completeness(data);
    addDecisionIndicators(data);
  }

  // ------------------------------------------------------------
  // CHARGEMENT API
  // ------------------------------------------------------------

  async function load() {
    const etat = $("etat-donnees");
    if (etat) {
      etat.textContent = "Chargement…";
    }

    try {
      const [reponseIndicateurs, reponseObservatoire] = await Promise.all([
        fetch(
          "/api/analyse/indicateurs?region=Occitanie",
          { cache: "no-store" }
        ),
        fetch(
          "/api/analyse/observatoire?region=Occitanie",
          { cache: "no-store" }
        ),
      ]);

      if (!reponseIndicateurs.ok) {
        throw new Error(
          `HTTP ${reponseIndicateurs.status}`
        );
      }

      const data =
        await reponseIndicateurs.json();

      render(data);

      // L'observatoire statistique détaillé est traité séparément :
      // une panne ou une lenteur sur ce bloc plus lourd ne doit pas
      // empêcher l'affichage du reste du tableau de bord.
      if (reponseObservatoire.ok) {
        try {
          const obs = await reponseObservatoire.json();
          renderObservatoire(obs);
        } catch (erreurObservatoire) {
          console.error("Erreur observatoire détaillé :", erreurObservatoire);
        }
      } else {
        console.error("Erreur observatoire détaillé : HTTP", reponseObservatoire.status);
      }

      if (etat) {
        etat.textContent = "Données à jour";
      }

      updateText(
        "date-maj",
        `Actualisé à ${new Date().toLocaleTimeString("fr-FR", {
          hour: "2-digit",
          minute: "2-digit",
        })}`
      );

    } catch (error) {

      console.error(
        "Erreur observatoire :",
        error
      );

      if (etat) {
        etat.textContent = "Erreur de chargement";
      }

      const titre = $("diagnostic-titre");
      if (titre) {
        titre.textContent = "Impossible de charger les indicateurs.";
      }

      const texte = $("diagnostic-texte");
      if (texte) {
        texte.textContent =
          "Nouvelle tentative automatique lors du prochain rafraîchissement.";
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

      const btnRefresh = $("btn-refresh");
      if (btnRefresh) {
        btnRefresh.addEventListener("click", load);
      }

    }
  );

})();