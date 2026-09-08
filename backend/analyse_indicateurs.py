
"""
Indicateurs de l'observatoire ciné-tourisme.

Sources utilisées :
- lieux_tournage / films
- amenity_stats / amenity_cache pour DATAtourisme
- isochrones pour les zones calculées par Géoplateforme

Principes :
- aucune donnée fictive ;
- aucune conversion distance -> temps ;
- minutes d'isochrones découvertes dynamiquement en base ;
- les données absentes restent NULL / N/D ;
- les distances d'équipements sont des distances réelles DATAtourisme ;
- les durées ne sont utilisées que lorsqu'elles existent réellement.
"""

from collections import defaultdict
from statistics import mean

from db import fetch_all, fetch_one


def _pct(numerator, denominator):
    if denominator is None or denominator == 0:
        return None
    return round((numerator / denominator) * 100, 1)


def _avg(values):
    values = [float(v) for v in values if v is not None]
    if not values:
        return None
    return round(mean(values), 2)


def _safe_float(value, default=0.0):
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value, default=0):
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _film_popularity(row):
    for key in (
        "popularite",
        "popularity",
        "tmdb_popularity",
    ):
        if key in row and row[key] is not None:
            return _safe_float(row[key])
    return 0.0


def _film_year(row):
    for key in (
        "annee",
        "annee_sortie",
        "release_year",
        "year",
    ):
        if key in row and row[key] is not None:
            return row[key]
    return None


def _normalise_mode(mode):
    """
    Normalise les modes réellement utilisés par isochrones.py.
    """
    mode = str(mode or "").lower()

    if mode in (
        "driving-car",
        "voiture",
        "car",
    ):
        return "voiture"

    if mode in (
        "foot-walking",
        "pied",
        "walking",
        "foot",
    ):
        return "pied"

    return None


async def construire_indicateurs_cinetourisme(
    region="Occitanie",
):

    # ==============================================================
    # 1. FILMS + LIEUX
    # ==============================================================

    lieux = await fetch_all(
        """
        SELECT
            lt.id AS lieu_id,
            lt.film_id,
            lt.departement,
            lt.commune,
            lt.latitude,
            lt.longitude,
            f.titre,
            f.annee,
            f.media_type,
            f.popularite
        FROM lieux_tournage lt
        JOIN films f
          ON f.id = lt.film_id
        WHERE f.region = %s
          AND f.statut = 'publie'
        ORDER BY lt.id
        """,
        (region,),
    )

    total_lieux = len(lieux)

    # ==============================================================
    # 2. FILMS
    # ==============================================================

    film_map = {}

    for row in lieux:
        film_id = row["film_id"]

        if film_id not in film_map:
            film_map[film_id] = {
                "film_id": film_id,
                "titre": row["titre"],
                "annee": _film_year(row),
                "media_type": row.get("media_type"),
                "popularite": _film_popularity(row),
                "lieux": [],
                "departements": set(),
            }

        film_map[film_id]["lieux"].append(
            row["lieu_id"]
        )

        if row.get("departement"):
            film_map[film_id]["departements"].add(
                row["departement"]
            )

    total_films = len(film_map)

    # ==============================================================
    # 3. COORDONNEES
    #
    # Calcul sur tous les lieux publiés de la région.
    # ==============================================================

    coord_stats = await fetch_one(
        """
        SELECT
            COUNT(*) AS total,
            COUNT(*) FILTER (
                WHERE lt.latitude IS NOT NULL
                  AND lt.longitude IS NOT NULL
            ) AS geolocalises
        FROM lieux_tournage lt
        JOIN films f
          ON f.id = lt.film_id
        WHERE f.region = %s
          AND f.statut = 'publie'
        """,
        (region,),
    )

    total_coord = _safe_int(
        coord_stats.get("total")
    )

    geolocalises = _safe_int(
        coord_stats.get("geolocalises")
    )

    coordinates_pct = _pct(
        geolocalises,
        total_coord,
    )

    # ==============================================================
    # 4. DEPARTEMENTS
    # ==============================================================

    departements = defaultdict(
        lambda: {
            "nb_lieux": 0,
            "films": set(),
        }
    )

    for row in lieux:
        dep = row.get("departement")

        if not dep:
            continue

        departements[dep]["nb_lieux"] += 1

        if row.get("film_id") is not None:
            departements[dep]["films"].add(
                row["film_id"]
            )

    departements_result = []

    for dep, values in departements.items():
        departements_result.append(
            {
                "departement": dep,
                "nb_lieux": values["nb_lieux"],
                "nb_films": len(values["films"]),
                "part_pct": _pct(
                    values["nb_lieux"],
                    total_lieux,
                ),
            }
        )

    departements_result.sort(
        key=lambda x: x["nb_lieux"],
        reverse=True,
    )

    total_dep = len(departements_result)

    # ==============================================================
    # 5. DATATOURISME : STATISTIQUES
    # ==============================================================

    amenity_stats_rows = await fetch_all(
        """
        SELECT
            lieu_tournage_id,
            categorie,
            rayon_metres,
            nombre_total,
            nombre_500m,
            nombre_1000m,
            distance_min_m,
            distance_moy_top10_m
        FROM amenity_stats
        """
    )

    amenity_cache_rows = await fetch_all(
        """
        SELECT
            lieu_tournage_id,
            categorie,
            distance_metres
        FROM amenity_cache
        """
    )

    stats_by_lieu = defaultdict(dict)
    cache_by_lieu = defaultdict(dict)

    for row in amenity_stats_rows:
        stats_by_lieu[
            row["lieu_tournage_id"]
        ][row["categorie"]] = row

    for row in amenity_cache_rows:
        cache_by_lieu[
            row["lieu_tournage_id"]
        ][row["categorie"]] = row

    # ==============================================================
    # 6. ISOCHRONES
    #
    # IMPORTANT :
    # geometry_geojson est du JSONB.
    # On ne suppose aucune colonne geom/PostGIS.
    #
    # Les minutes disponibles sont découvertes dans la base.
    # ==============================================================

    isochrone_rows = await fetch_all(
        """
        SELECT
            lieu_tournage_id,
            mode,
            minutes,
            calculated_at
        FROM isochrones
        """
    )

    minutes_disponibles = sorted(
        {
            _safe_int(row.get("minutes"))
            for row in isochrone_rows
            if row.get("minutes") is not None
        }
    )

    iso_by_minute = defaultdict(
        lambda: {
            "voiture": set(),
            "pied": set(),
        }
    )

    iso_dates = []

    for row in isochrone_rows:

        minutes = row.get("minutes")

        if minutes is None:
            continue

        minutes = _safe_int(minutes)

        mode = _normalise_mode(
            row.get("mode")
        )

        if mode is None:
            continue

        lieu_id = row.get(
            "lieu_tournage_id"
        )

        if lieu_id is not None:
            iso_by_minute[
                minutes
            ][mode].add(lieu_id)

        if row.get("calculated_at"):
            iso_dates.append(
                row["calculated_at"]
            )

    couverture_par_minutes = []

    for minutes in minutes_disponibles:

        voiture_ids = iso_by_minute[
            minutes
        ]["voiture"]

        pied_ids = iso_by_minute[
            minutes
        ]["pied"]

        couverture_par_minutes.append(
            {
                "minutes": minutes,

                "voiture_pct": _pct(
                    len(voiture_ids),
                    total_lieux,
                ),

                "pied_pct": _pct(
                    len(pied_ids),
                    total_lieux,
                ),

                "voiture_lieux": len(
                    voiture_ids
                ),

                "pied_lieux": len(
                    pied_ids
                ),
            }
        )

    derniere_date = None

    if iso_dates:
        derniere_date = max(
            iso_dates
        ).isoformat()

    iso_lieux = {
        row["lieu_tournage_id"]
        for row in isochrone_rows
        if row.get(
            "lieu_tournage_id"
        ) is not None
    }

    iso_lieux_region = iso_lieux.intersection(
        {
            row["lieu_id"]
            for row in lieux
        }
    )

    iso_coverage_pct = _pct(
        len(iso_lieux_region),
        total_lieux,
    )

    # ==============================================================
    # 7. ROUTAGE RÉEL DISPONIBLE
    #
    # Ces colonnes existent dans amenity_cache après migration_v10.
    #
    # Si elles sont NULL, on ne déduit aucune durée.
    # ==============================================================

    route_rows = await fetch_all(
        """
        SELECT
            lieu_tournage_id,
            MAX(duree_pied_secondes)
                AS duree_pied_secondes,
            MAX(duree_voiture_secondes)
                AS duree_voiture_secondes
        FROM amenity_cache
        GROUP BY lieu_tournage_id
        """
    )

    routes_by_lieu = {
        row["lieu_tournage_id"]: row
        for row in route_rows
    }

    route_complete = 0
    ready15 = 0
    ready30 = 0
    isolated45 = 0

    for row in lieux:

        route = routes_by_lieu.get(
            row["lieu_id"]
        )

        if not route:
            continue

        pied = route.get(
            "duree_pied_secondes"
        )

        voiture = route.get(
            "duree_voiture_secondes"
        )

        if pied is None or voiture is None:
            continue

        route_complete += 1

        if (
            pied <= 15 * 60
            and voiture <= 15 * 60
        ):
            ready15 += 1

        if (
            pied <= 30 * 60
            and voiture <= 30 * 60
        ):
            ready30 += 1

        if (
            pied > 45 * 60
            and voiture > 45 * 60
        ):
            isolated45 += 1

    route_coverage_pct = _pct(
        route_complete,
        total_lieux,
    )

    # Si tous les lieux n'ont pas de durée réelle,
    # le pourcentage régional n'est pas affiché.
    if route_complete == total_lieux:
        pret15_pct = _pct(
            ready15,
            total_lieux,
        )

        pret30_pct = _pct(
            ready30,
            total_lieux,
        )

        isoles45_pct = _pct(
            isolated45,
            total_lieux,
        )
    else:
        pret15_pct = None
        pret30_pct = None
        isoles45_pct = None

    # ==============================================================
    # 8. INDICATEURS PAR LIEU
    # ==============================================================

    lieu_indicators = []

    hebergement_distances = []
    restaurant_distances = []

    hebergement_presence = 0
    restaurant_presence = 0

    lieu_ids = {
        row["lieu_id"]
        for row in lieux
    }

    for row in lieux:

        lieu_id = row["lieu_id"]

        local_stats = stats_by_lieu.get(
            lieu_id,
            {},
        )

        local_cache = cache_by_lieu.get(
            lieu_id,
            {},
        )

        # ----------------------------------------------------------
        # Hébergement
        # ----------------------------------------------------------

        h = local_stats.get(
            "hebergement"
        )

        if h:

            h_nombre = _safe_int(
                h.get("nombre_total")
            )

            h_distance = h.get(
                "distance_min_m"
            )

            if h_distance is None:

                cached = local_cache.get(
                    "hebergement"
                )

                if cached:
                    h_distance = cached.get(
                        "distance_metres"
                    )

        else:

            cached = local_cache.get(
                "hebergement"
            )

            h_nombre = (
                1
                if cached
                else 0
            )

            h_distance = (
                cached.get(
                    "distance_metres"
                )
                if cached
                else None
            )

        # ----------------------------------------------------------
        # Restaurant
        # ----------------------------------------------------------

        r = local_stats.get(
            "restaurant"
        )

        if r:

            r_nombre = _safe_int(
                r.get("nombre_total")
            )

            r_distance = r.get(
                "distance_min_m"
            )

            if r_distance is None:

                cached = local_cache.get(
                    "restaurant"
                )

                if cached:
                    r_distance = cached.get(
                        "distance_metres"
                    )

        else:

            cached = local_cache.get(
                "restaurant"
            )

            r_nombre = (
                1
                if cached
                else 0
            )

            r_distance = (
                cached.get(
                    "distance_metres"
                )
                if cached
                else None
            )

        has_h = h_nombre > 0
        has_r = r_nombre > 0

        if has_h:
            hebergement_presence += 1

        if has_r:
            restaurant_presence += 1

        if h_distance is not None:
            hebergement_distances.append(
                float(h_distance)
            )

        if r_distance is not None:
            restaurant_distances.append(
                float(r_distance)
            )

        # ----------------------------------------------------------
        # Maturité touristique observée
        #
        # 50 points : hébergement présent
        # 50 points : restaurant présent
        #
        # Ce score ne représente PAS un temps de trajet.
        # ----------------------------------------------------------

        maturite = (
            (50 if has_h else 0)
            + (50 if has_r else 0)
        )

        popularite = 0

        film = film_map.get(
            row["film_id"]
        )

        if film:
            popularite = film[
                "popularite"
            ]

        lieu_indicators.append(
            {
                "lieu_id": lieu_id,
                "film_id": row["film_id"],
                "titre": row["titre"],
                "commune": row.get(
                    "commune"
                ),
                "departement": row.get(
                    "departement"
                ),
                "popularite": popularite,

                "hebergement": has_h,
                "restaurant": has_r,

                "nb_hebergements": h_nombre,
                "nb_restaurants": r_nombre,

                "distance_hebergement_m": (
                    round(
                        float(
                            h_distance
                        ),
                        1,
                    )
                    if h_distance is not None
                    else None
                ),

                "distance_restaurant_m": (
                    round(
                        float(
                            r_distance
                        ),
                        1,
                    )
                    if r_distance is not None
                    else None
                ),

                "maturite_touristique_pct":
                    maturite,
            }
        )

    # ==============================================================
    # 9. CAPACITÉ TOURISTIQUE
    # ==============================================================

    moyenne_h_km = (
        round(
            mean(
                hebergement_distances
            ) / 1000,
            2,
        )
        if hebergement_distances
        else None
    )

    moyenne_r_km = (
        round(
            mean(
                restaurant_distances
            ) / 1000,
            2,
        )
        if restaurant_distances
        else None
    )

    hebergement_500m = sum(
        1
        for x in lieu_indicators
        if (
            x[
                "distance_hebergement_m"
            ]
            is not None
            and x[
                "distance_hebergement_m"
            ] <= 500
        )
    )

    restaurant_500m = sum(
        1
        for x in lieu_indicators
        if (
            x[
                "distance_restaurant_m"
            ]
            is not None
            and x[
                "distance_restaurant_m"
            ] <= 500
        )
    )

    # ==============================================================
    # 10. FILMS NOTABLES
    # ==============================================================

    films_sorted = sorted(
        film_map.values(),
        key=lambda x: (
            x["popularite"],
            len(x["lieux"]),
        ),
        reverse=True,
    )

    films_notables = []

    for film in films_sorted[:10]:

        films_notables.append(
            {
                "film_id": film["film_id"],
                "titre": film["titre"],
                "annee": film["annee"],
                "media_type": film[
                    "media_type"
                ],
                "popularite": film[
                    "popularite"
                ],
                "nb_lieux": len(
                    film["lieux"]
                ),
                "nb_departements": len(
                    film[
                        "departements"
                    ]
                ),
                "departements": sorted(
                    film[
                        "departements"
                    ]
                ),
            }
        )

    # ==============================================================
    # 11. POTENTIEL PAR LIEU
    # ==============================================================

    popularites = [
        _safe_float(
            x["popularite"]
        )
        for x in lieu_indicators
    ]

    max_pop = max(
        popularites,
        default=0,
    )

    potentiel = []

    for item in lieu_indicators:

        if max_pop > 0:

            popularite_score = round(
                (
                    item["popularite"]
                    / max_pop
                )
                * 100,
                1,
            )

        else:
            popularite_score = 0

        maturite = item[
            "maturite_touristique_pct"
        ]

        score_opportunite = round(
            (
                maturite * 0.55
                + popularite_score * 0.45
            ),
            1,
        )

        potentiel.append(
            {
                **item,

                "popularite_score":
                    popularite_score,

                "preparation":
                    maturite,

                "score_opportunite":
                    score_opportunite,
            }
        )

    potentiel.sort(
        key=lambda x:
            x["score_opportunite"],
        reverse=True,
    )

    # ==============================================================
    # 12. CONCENTRATION TERRITORIALE
    # ==============================================================

    parts = [
        _safe_float(
            x["part_pct"]
        )
        for x in departements_result
    ]

    hhi = sum(
        (part / 100) ** 2
        for part in parts
    ) * 10000

    top3_pct = sum(
        parts[:3]
    )

    if hhi < 1500:
        concentration_label = "faible"
    elif hhi < 2500:
        concentration_label = "intermédiaire"
    else:
        concentration_label = "forte"

    # ==============================================================
    # 13. SCORE RÉGIONAL
    # ==============================================================

    maturity_regionale = (
        _avg(
            [
                item[
                    "maturite_touristique_pct"
                ]
                for item in lieu_indicators
            ]
        )
        or 0
    )

    popularite_regionale = (
        _avg(
            [
                item[
                    "popularite"
                ]
                for item in lieu_indicators
            ]
        )
        or 0
    )

    if max_pop > 0:

        popularite_score_regionale = (
            min(
                100,
                (
                    popularite_regionale
                    / max_pop
                )
                * 100,
            )
        )

    else:
        popularite_score_regionale = 0

    opportunite_regionale = round(
        (
            maturity_regionale * 0.45
            + popularite_score_regionale
            * 0.25
            + min(
                100,
                top3_pct,
            )
            * 0.30
        ),
        1,
    )

    # ==============================================================
    # 14. QUALITÉ DES DONNÉES
    # ==============================================================

    amenity_lieux = {
        row["lieu_tournage_id"]
        for row in amenity_stats_rows
        if row.get(
            "lieu_tournage_id"
        ) is not None
    }

    amenity_coverage_pct = _pct(
        len(
            amenity_lieux.intersection(
                lieu_ids
            )
        ),
        total_lieux,
    )

    popularity_complete = sum(
        1
        for film in film_map.values()
        if film["popularite"] > 0
    )

    popularity_pct = _pct(
        popularity_complete,
        total_films,
    )

    # ==============================================================
    # 15. RÉSULTAT
    # ==============================================================

    return {
        "region": region,

        "totaux": {
            "films": total_films,
            "lieux": total_lieux,
            "departements": total_dep,
        },

        "departements":
            departements_result,

        "accessibilite": {
            "pret_15_pct":
                pret15_pct,

            "pret_30_pct":
                pret30_pct,

            "isoles_45_pct":
                isoles45_pct,

            "route_coverage_pct":
                route_coverage_pct,
        },

        "equipements": {
            "moy_hebergement":
                moyenne_h_km,

            "moy_restaurant":
                moyenne_r_km,

            "hebergement_presence_pct":
                _pct(
                    hebergement_presence,
                    total_lieux,
                ),

            "restaurant_presence_pct":
                _pct(
                    restaurant_presence,
                    total_lieux,
                ),

            "hebergement_500m_pct":
                _pct(
                    hebergement_500m,
                    total_lieux,
                ),

            "restaurant_500m_pct":
                _pct(
                    restaurant_500m,
                    total_lieux,
                ),
        },

        "isochrones": {
            "couverture_pct":
                iso_coverage_pct,

            "minutes_disponibles":
                minutes_disponibles,

            "couverture_par_minutes":
                couverture_par_minutes,

            # Impossible à calculer honnêtement
            # sans géométrie PostGIS exploitable.
            "ratio_surface_voiture_marche_15":
                None,

            "derniere_date":
                derniere_date,
        },

        "films_notables":
            films_notables,

        "lieux_potentiel":
            potentiel,

        "completude": {
            "coordonnees_pct":
                coordinates_pct,

            "amenities_pct":
                amenity_coverage_pct,

            "popularite_pct":
                popularity_pct,

            "isochrones_pct":
                iso_coverage_pct,

            "routage_pct":
                route_coverage_pct,
        },

        "scores": {
            "opportunite_regionale":
                opportunite_regionale,

            "concentration_hhi":
                round(hhi, 1),

            "concentration_top3_pct":
                round(
                    top3_pct,
                    1,
                ),

            "concentration_label":
                concentration_label,
        },

        "metriques": {
            "maturite_touristique_pct":
                round(
                    maturity_regionale,
                    1,
                ),

            "popularite_moyenne":
                round(
                    popularite_regionale,
                    2,
                ),

            "nb_lieux_avec_hebergement":
                hebergement_presence,

            "nb_lieux_avec_restaurant":
                restaurant_presence,

            "nb_lieux_routage_complet":
                route_complete,

            "nb_isochrones":
                len(
                    isochrone_rows
                ),

            "minutes_isochrones":
                minutes_disponibles,
        },
    }
