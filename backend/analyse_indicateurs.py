
"""
backend/analyse_indicateurs.py

Indicateurs dynamiques de l'observatoire ciné-touristique.

Principes :
- uniquement les données réellement présentes en base ;
- aucun appel externe pendant le chargement de la page ;
- aucune minute d'isochrone supposée ;
- les minutes réellement présentes dans `isochrones` sont découvertes
  dynamiquement ;
- une erreur sur les isochrones ne doit pas rendre tout l'endpoint
  indisponible ;
- compatible avec backend/db.py et ses placeholders %s.
"""

from collections import defaultdict
from statistics import mean

from db import fetch_all, fetch_one


def _pct(a, b):
    """Pourcentage sécurisé."""
    return round(100 * a / b, 1) if b else 0.0


def _mode_normalise(mode):
    """Normalise les noms de modes utilisés par les différents calculs."""
    return str(mode or "").strip().lower()


async def construire_indicateurs_cinetourisme(region="Occitanie"):
    # ------------------------------------------------------------------
    # 1. LIEUX + FILMS
    # ------------------------------------------------------------------
    lieux = await fetch_all(
        """
        SELECT
            lt.id,
            lt.departement,
            lt.commune,
            lt.latitude,
            lt.longitude,
            f.id AS film_id,
            f.titre,
            f.annee,
            f.media_type,
            COALESCE(f.popularity, 0) AS popularite
        FROM lieux_tournage lt
        JOIN films f ON f.id = lt.film_id
        WHERE f.region=%s
          AND f.statut='publie'
          AND lt.latitude IS NOT NULL
          AND lt.longitude IS NOT NULL
        ORDER BY lt.id
        """,
        (region,),
    )

    # ------------------------------------------------------------------
    # Cas aucune donnée
    # ------------------------------------------------------------------
    if not lieux:
        return {
            "region": region,
            "totaux": {
                "nb_films": 0,
                "nb_lieux": 0,
                "nb_departements": 0,
            },
            "departements": [],
            "concentration": {
                "hhi": 0,
                "top3_pct": 0,
            },
            "accessibilite": {
                "pret_15_pct": 0,
                "pret_30_pct": 0,
                "isoles_45_pct": 0,
            },
            "isochrones": {
                "couverture_pct": 0,
                "couverture_par_minutes": [],
                "minutes_disponibles": [],
                "ratio_surface_voiture_marche_15": None,
                "derniere_date": None,
            },
            "opportunites": [],
            "vigilances": [],
            "lieux_potentiel": [],
            "films_notables": [],
            "completude": {
                "coordinates_pct": 0,
                "amenities_pct": 0,
                "popularite_pct": 0,
            },
            "scores": {
                "opportunite_regionale": 0,
                "concentration_label": "aucune donnée",
            },
            "methodologie": {
                "texte": (
                    "Aucune donnée publiée géolocalisée disponible "
                    "pour cette région."
                )
            },
        }

    ids = [x["id"] for x in lieux]

    # ------------------------------------------------------------------
    # 2. AMENITIES PRÉCALCULÉES
    # ------------------------------------------------------------------
    access = defaultdict(dict)

    try:
        amen = await fetch_all(
            """
            SELECT
                lieu_tournage_id,
                categorie,
                MIN(duree_voiture_secondes) AS voiture_s,
                MIN(duree_pied_secondes) AS pied_s
            FROM amenity_cache
            WHERE lieu_tournage_id=ANY(%s)
              AND categorie IN ('hebergement', 'restaurant')
            GROUP BY lieu_tournage_id, categorie
            """,
            (ids,),
        )
    except Exception as exc:
        # L'analyse doit continuer même si amenity_cache est momentanément
        # indisponible/incomplet.
        print(
            f"[analyse] erreur amenity_cache : {exc}",
            flush=True,
        )
        amen = []

    for a in amen:
        access[a["lieu_tournage_id"]][a["categorie"]] = a

    # ------------------------------------------------------------------
    # 3. REGROUPEMENT PAR DÉPARTEMENT
    # ------------------------------------------------------------------
    deps = defaultdict(
        lambda: {
            "lieux": [],
            "films": set(),
            "pop": [],
        }
    )

    for lieu in lieux:
        dep = lieu["departement"] or "Non renseigné"

        deps[dep]["lieux"].append(lieu)
        deps[dep]["films"].add(lieu["film_id"])
        deps[dep]["pop"].append(
            float(lieu["popularite"] or 0)
        )

    total = len(lieux)
    rows = []

    # ------------------------------------------------------------------
    # 4. INDICATEURS DÉPARTEMENTAUX
    # ------------------------------------------------------------------
    for dep, value in deps.items():

        ready15 = 0
        ready30 = 0
        isolated45 = 0

        heures = []
        restaurants = []

        for lieu in value["lieux"]:

            a = access.get(lieu["id"], {})

            hebergement = a.get("hebergement")
            restaurant = a.get("restaurant")

            hv = (
                hebergement.get("voiture_s")
                if hebergement
                else None
            )

            rv = (
                restaurant.get("voiture_s")
                if restaurant
                else None
            )

            if hv is not None:
                heures.append(hv / 60)

            if rv is not None:
                restaurants.append(rv / 60)

            # Prêt à 15 minutes :
            # hébergement ET restaurant disponibles à <= 15 min.
            if hv is not None and rv is not None:
                if hv <= 900 and rv <= 900:
                    ready15 += 1

                if hv <= 1800 and rv <= 1800:
                    ready30 += 1

            # Isolé :
            # aucun hébergement <= 45 minutes.
            if hv is None or hv > 2700:
                isolated45 += 1

        rows.append(
            {
                "departement": dep,
                "nb_lieux": len(value["lieux"]),
                "nb_films": len(value["films"]),
                "part_pourcentage": _pct(
                    len(value["lieux"]),
                    total,
                ),
                "part_brute": (
                    len(value["lieux"]) / total
                ),
                "popularite_moyenne": (
                    round(mean(value["pop"]), 1)
                    if value["pop"]
                    else 0
                ),
                "moy_hebergement": (
                    round(mean(heures), 1)
                    if heures
                    else None
                ),
                "moy_restaurant": (
                    round(mean(restaurants), 1)
                    if restaurants
                    else None
                ),
                "pret_15_pct": _pct(
                    ready15,
                    len(value["lieux"]),
                ),
                "pret_30_pct": _pct(
                    ready30,
                    len(value["lieux"]),
                ),
                "isoles_45_pct": _pct(
                    isolated45,
                    len(value["lieux"]),
                ),
            }
        )

    rows.sort(
        key=lambda x: x["nb_lieux"],
        reverse=True,
    )

    # ------------------------------------------------------------------
    # 5. PART CUMULÉE + HHI
    # ------------------------------------------------------------------
    cumul = 0

    for dep in rows:
        cumul += dep["part_brute"]

        dep["part_cumulee_pct"] = round(
            cumul * 100,
            1,
        )

    hhi = 10000 * sum(
        dep["part_brute"] ** 2
        for dep in rows
    )

    top3 = 100 * sum(
        dep["part_brute"]
        for dep in rows[:3]
    )

    # ------------------------------------------------------------------
    # 6. SCORES
    # ------------------------------------------------------------------
    maxpop = max(
        (
            dep["popularite_moyenne"]
            for dep in rows
        ),
        default=1,
    )

    for dep in rows:

        concentration = min(
            100,
            dep["part_pourcentage"] * 5,
        )

        readiness = (
            0.65 * dep["pret_15_pct"]
            + 0.35 * dep["pret_30_pct"]
        )

        pop = min(
            100,
            (
                100
                * dep["popularite_moyenne"]
                / maxpop
            )
            if maxpop
            else 0,
        )

        dep["score_opportunite"] = round(
            0.40 * concentration
            + 0.35 * readiness
            + 0.25 * pop,
            1,
        )

        dep["score_vigilance"] = round(
            0.45 * concentration
            + 0.55 * dep["isoles_45_pct"],
            1,
        )

        if dep["score_opportunite"] >= 60:
            dep["recommandation"] = (
                "Priorité de valorisation : forte présence "
                "et environnement relativement prêt."
            )
        elif dep["score_vigilance"] >= 55:
            dep["recommandation"] = (
                "Territoire à renforcer : présence observée "
                "mais accessibilité touristique à améliorer."
            )
        else:
            dep["recommandation"] = (
                "Potentiel à structurer : présence intéressante, "
                "préparation intermédiaire."
            )

    # ------------------------------------------------------------------
    # 7. INDICATEURS RÉGIONAUX D'ACCESSIBILITÉ
    # ------------------------------------------------------------------
    ready15_total = 0
    ready30_total = 0
    isolated_total = 0

    for lieu in lieux:

        a = access.get(lieu["id"], {})

        h = a.get("hebergement", {})
        r = a.get("restaurant", {})

        hv = h.get("voiture_s")
        rv = r.get("voiture_s")

        if (
            hv is not None
            and rv is not None
            and hv <= 900
            and rv <= 900
        ):
            ready15_total += 1

        if (
            hv is not None
            and rv is not None
            and hv <= 1800
            and rv <= 1800
        ):
            ready30_total += 1

        if hv is None or hv > 2700:
            isolated_total += 1

    # ------------------------------------------------------------------
    # 8. ISOCHRONES
    #
    # IMPORTANT :
    # On ne suppose plus 5/10/15/30.
    # On récupère les minutes réellement présentes en base.
    # ------------------------------------------------------------------
    iso_rows = []

    try:
        iso_rows = await fetch_all(
            """
            SELECT
                lieu_tournage_id,
                mode,
                minutes,
                geometry_geojson,
                calculated_at
            FROM isochrones
            WHERE lieu_tournage_id=ANY(%s)
            """,
            (ids,),
        )
    except Exception as exc:
        print(
            f"[analyse] erreur lecture isochrones : {exc}",
            flush=True,
        )
        iso_rows = []

    # Minutes réellement disponibles.
    minutes_disponibles = sorted(
        {
            int(row["minutes"])
            for row in iso_rows
            if row.get("minutes") is not None
        }
    )

    # Index rapide.
    iso_index = {
        (
            row["lieu_tournage_id"],
            _mode_normalise(row["mode"]),
            int(row["minutes"]),
        ): row
        for row in iso_rows
        if row.get("minutes") is not None
    }

    couverture = []

    for minute in minutes_disponibles:

        voitures = sum(
            (
                lieu_id,
                "driving-car",
                minute,
            )
            in iso_index
            for lieu_id in ids
        )

        pieds = sum(
            (
                lieu_id,
                "foot-walking",
                minute,
            )
            in iso_index
            for lieu_id in ids
        )

        couverture.append(
            {
                "minutes": minute,
                "voiture_pct": _pct(
                    voitures,
                    total,
                ),
                "pied_pct": _pct(
                    pieds,
                    total,
                ),
            }
        )

    # Couverture générale :
    # on prend, pour chaque minute réelle, le meilleur taux voiture/pied.
    if couverture:
        couverture_pct = round(
            mean(
                max(
                    row["voiture_pct"],
                    row["pied_pct"],
                )
                for row in couverture
            ),
            1,
        )
    else:
        couverture_pct = 0.0

    # ------------------------------------------------------------------
    # 9. RATIO SURFACE VOITURE / MARCHE À 15 MIN
    #
    # On ne lance le calcul que si 15 min existe réellement.
    # Et surtout : une erreur PostGIS ne fait plus tomber l'endpoint.
    # ------------------------------------------------------------------
    ratio = None

    if 15 in minutes_disponibles:

        try:
            ratio_rows = await fetch_all(
                """
                SELECT AVG(
                    ST_Area(
                        ST_SetSRID(
                            ST_GeomFromGeoJSON(
                                c.geometry_geojson::text
                            ),
                            4326
                        )::geography
                    )
                    /
                    NULLIF(
                        ST_Area(
                            ST_SetSRID(
                                ST_GeomFromGeoJSON(
                                    p.geometry_geojson::text
                                ),
                                4326
                            )::geography
                        ),
                        0
                    )
                ) AS ratio
                FROM isochrones c
                JOIN isochrones p
                  ON p.lieu_tournage_id =
                     c.lieu_tournage_id
                 AND p.minutes = c.minutes
                WHERE c.lieu_tournage_id=ANY(%s)
                  AND c.mode='driving-car'
                  AND p.mode='foot-walking'
                  AND c.minutes=15
                  AND c.geometry_geojson IS NOT NULL
                  AND p.geometry_geojson IS NOT NULL
                """,
                (ids,),
            )

            if ratio_rows:
                ratio_value = ratio_rows[0].get("ratio")

                if ratio_value is not None:
                    ratio = round(
                        float(ratio_value),
                        2,
                    )

        except Exception as exc:
            print(
                f"[analyse] ratio isochrones indisponible : {exc}",
                flush=True,
            )
            ratio = None

    # ------------------------------------------------------------------
    # 10. DATE DU DERNIER CALCUL
    # ------------------------------------------------------------------
    dates = [
        row["calculated_at"]
        for row in iso_rows
        if row.get("calculated_at") is not None
    ]

    derniere_date = max(dates) if dates else None

    # ------------------------------------------------------------------
    # 11. LIEUX À POTENTIEL
    # ------------------------------------------------------------------
    points = []

    for lieu in sorted(
        lieux,
        key=lambda x: float(
            x["popularite"] or 0
        ),
        reverse=True,
    )[:100]:

        a = access.get(lieu["id"], {})

        h = a.get("hebergement", {})
        r = a.get("restaurant", {})

        hv = h.get("voiture_s")
        rv = r.get("voiture_s")

        temps = max(
            hv or 99999,
            rv or 99999,
        )

        preparation = max(
            0,
            100 - min(
                100,
                temps / 1800 * 100,
            ),
        )

        points.append(
            {
                "titre": lieu["titre"],
                "departement": lieu["departement"],
                "popularite": round(
                    float(lieu["popularite"] or 0),
                    1,
                ),
                "preparation": round(
                    preparation,
                    1,
                ),
            }
        )

    # ------------------------------------------------------------------
    # 12. COMPLETUDE
    # ------------------------------------------------------------------
    popularite_pct = _pct(
        sum(
            float(lieu["popularite"] or 0) > 0
            for lieu in lieux
        ),
        total,
    )

    amenities_pct = _pct(
        len(
            {
                row["lieu_tournage_id"]
                for row in amen
            }
        ),
        total,
    )

    # ------------------------------------------------------------------
    # 13. FILMS NOTABLES
    #
    # Le frontend accepte une liste vide.
    # On conserve donc le comportement actuel sans inventer
    # une seconde structure de données.
    # ------------------------------------------------------------------
    films_notables = []

    # ------------------------------------------------------------------
    # 14. SCORE RÉGIONAL
    # ------------------------------------------------------------------
    score_regional = round(
        mean(
            row["score_opportunite"]
            for row in rows
        ),
        1,
    ) if rows else 0

    if hhi < 1500:
        concentration_label = "répartition dispersée"
    elif hhi < 2500:
        concentration_label = "concentration intermédiaire"
    else:
        concentration_label = "forte concentration"

    # ------------------------------------------------------------------
    # 15. JSON FINAL
    # ------------------------------------------------------------------
    return {
        "region": region,

        "totaux": {
            "nb_films": len(
                {
                    lieu["film_id"]
                    for lieu in lieux
                }
            ),
            "nb_lieux": total,
            "nb_departements": len(rows),
        },

        "departements": rows,

        "concentration": {
            "hhi": round(hhi, 1),
            "top3_pct": round(top3, 1),
        },

        "accessibilite": {
            "pret_15_pct": _pct(
                ready15_total,
                total,
            ),
            "pret_30_pct": _pct(
                ready30_total,
                total,
            ),
            "isoles_45_pct": _pct(
                isolated_total,
                total,
            ),
        },

        "isochrones": {
            "couverture_pct": couverture_pct,
            "couverture_par_minutes": couverture,
            "minutes_disponibles": minutes_disponibles,
            "ratio_surface_voiture_marche_15": ratio,
            "derniere_date": derniere_date,
        },

        "opportunites": [
            {
                "departement": row["departement"],
                "score": row["score_opportunite"],
                "interpretation": row["recommandation"],
            }
            for row in sorted(
                rows,
                key=lambda x: x["score_opportunite"],
                reverse=True,
            )
        ],

        "vigilances": [
            {
                "departement": row["departement"],
                "score": row["score_vigilance"],
                "interpretation": row["recommandation"],
            }
            for row in sorted(
                rows,
                key=lambda x: x["score_vigilance"],
                reverse=True,
            )
        ],

        "lieux_potentiel": points,

        "films_notables": films_notables,

        "completude": {
            "coordinates_pct": 100.0,
            "amenities_pct": round(
                amenities_pct,
                1,
            ),
            "popularite_pct": round(
                popularite_pct,
                1,
            ),
        },

        "scores": {
            "opportunite_regionale": score_regional,
            "concentration_label": concentration_label,
        },

        "methodologie": {
            "texte": (
                "Indicateurs calculés dynamiquement depuis les lieux "
                "de tournage publiés et géolocalisés, les équipements "
                "précalculés de amenity_cache et les isochrones IGN "
                "réellement présents dans la table isochrones. "
                "Les minutes d'isochrones sont découvertes "
                "dynamiquement et aucune minute absente n'est inventée."
            )
        },
    }
