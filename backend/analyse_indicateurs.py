"""
backend/analyse_indicateurs.py

Indicateurs de l'observatoire ciné-tourisme.

Sources :
    - lieux_tournage
    - films
    - amenity_stats
    - amenity_cache
    - isochrones

Principes :
    - aucune donnée fictive ;
    - aucun seuil arbitraire pour inventer une accessibilité ;
    - les minutes d'isochrones sont découvertes dynamiquement dans la DB ;
    - les distances DATAtourisme sont utilisées telles quelles ;
    - les durées de trajet ne sont utilisées que lorsqu'elles existent réellement.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from db import fetch_all


# ---------------------------------------------------------------------------
# Helpers généraux
# ---------------------------------------------------------------------------

def _safe_float(value: Any) -> float | None:
    if value is None:
        return None

    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_int(value: Any) -> int | None:
    if value is None:
        return None

    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _round(value: Any, digits: int = 1) -> float | None:
    value = _safe_float(value)

    if value is None:
        return None

    return round(value, digits)


def _pct(numerator: int | float, denominator: int | float) -> float | None:
    if denominator in (None, 0):
        return None

    return round(float(numerator) / float(denominator) * 100.0, 1)


def _normalise_mode(mode: Any) -> str:
    """
    Normalise les modes présents éventuellement dans la DB.

    Valeurs attendues :
        driving-car
        foot-walking

    On accepte aussi quelques variantes afin de rendre
    l'analyse robuste aux anciennes données.
    """

    value = str(mode or "").strip().lower()

    aliases = {
        "car": "driving-car",
        "voiture": "driving-car",
        "driving": "driving-car",
        "driving-car": "driving-car",

        "foot": "foot-walking",
        "walking": "foot-walking",
        "walk": "foot-walking",
        "marche": "foot-walking",
        "a_pied": "foot-walking",
        "à pied": "foot-walking",
        "foot-walking": "foot-walking",
    }

    return aliases.get(value, value)


def _format_date(value: Any) -> str | None:
    """
    Retourne une date ISO lisible sans casser si asyncpg renvoie
    directement un datetime.
    """

    if value is None:
        return None

    if isinstance(value, datetime):
        return value.isoformat()

    return str(value)


def _median(values: list[float]) -> float | None:
    if not values:
        return None

    values = sorted(values)

    n = len(values)

    if n % 2 == 1:
        return values[n // 2]

    return (values[n // 2 - 1] + values[n // 2]) / 2


# ---------------------------------------------------------------------------
# HHI
# ---------------------------------------------------------------------------

def _hhi(parts: list[float]) -> float | None:
    """
    Indice de concentration de Herfindahl-Hirschman.

    Les parts sont exprimées en proportions (0 à 1).

    Exemple :
        [0.5, 0.3, 0.2]
        => 0.38
    """

    if not parts:
        return None

    return round(sum(float(part) ** 2 for part in parts), 4)


# ---------------------------------------------------------------------------
# Equipements touristiques
# ---------------------------------------------------------------------------

async def _charger_equipements(
    lieu_ids: list[int],
) -> dict[int, dict[str, Any]]:
    """
    Charge les indicateurs DATAtourisme pour les lieux concernés.

    Priorité :
        1. amenity_stats.distance_min_m
        2. amenity_cache.distance_metres

    nombre_total vient de amenity_stats.

    On ne transforme jamais une distance en temps de trajet.
    """

    if not lieu_ids:
        return {}

    rows = await fetch_all(
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
        WHERE lieu_tournage_id = ANY(%s)
        """,
        (lieu_ids,),
    )

    result: dict[int, dict[str, Any]] = defaultdict(dict)

    for row in rows:
        lieu_id = _safe_int(row.get("lieu_tournage_id"))

        if lieu_id is None:
            continue

        categorie = str(row.get("categorie") or "").strip().lower()

        if not categorie:
            continue

        result[lieu_id][categorie] = {
            "nombre_total": _safe_int(row.get("nombre_total")) or 0,
            "nombre_500m": _safe_int(row.get("nombre_500m")) or 0,
            "nombre_1000m": _safe_int(row.get("nombre_1000m")) or 0,
            "distance_min_m": _safe_float(row.get("distance_min_m")),
            "distance_moy_top10_m": _safe_float(
                row.get("distance_moy_top10_m")
            ),
        }

    # ------------------------------------------------------------------
    # Fallback amenity_cache pour les distances absentes des stats
    # ------------------------------------------------------------------

    cache_rows = await fetch_all(
        """
        SELECT
            lieu_tournage_id,
            categorie,
            distance_metres
        FROM amenity_cache
        WHERE lieu_tournage_id = ANY(%s)
        """,
        (lieu_ids,),
    )

    cache_distances: dict[int, dict[str, list[float]]] = defaultdict(
        lambda: defaultdict(list)
    )

    for row in cache_rows:
        lieu_id = _safe_int(row.get("lieu_tournage_id"))

        if lieu_id is None:
            continue

        categorie = str(row.get("categorie") or "").strip().lower()

        distance = _safe_float(row.get("distance_metres"))

        if not categorie or distance is None:
            continue

        cache_distances[lieu_id][categorie].append(distance)

    # Compléter les distances manquantes
    for lieu_id, categories in cache_distances.items():

        for categorie, distances in categories.items():

            if not distances:
                continue

            minimum = min(distances)

            if categorie not in result[lieu_id]:
                result[lieu_id][categorie] = {
                    "nombre_total": 0,
                    "nombre_500m": sum(
                        1 for distance in distances
                        if distance <= 500
                    ),
                    "nombre_1000m": sum(
                        1 for distance in distances
                        if distance <= 1000
                    ),
                    "distance_min_m": minimum,
                    "distance_moy_top10_m": sum(
                        distances[:10]
                    ) / min(len(distances), 10),
                }

            elif result[lieu_id][categorie]["distance_min_m"] is None:
                result[lieu_id][categorie]["distance_min_m"] = minimum

    return dict(result)


# ---------------------------------------------------------------------------
# Accessibilité routière réelle
# ---------------------------------------------------------------------------

async def _charger_durees_routieres(
    lieu_ids: list[int],
) -> dict[int, dict[str, int | None]]:
    """
    Cherche les durées routières réellement disponibles.

    Le schéma peut avoir plusieurs noms de colonnes selon les migrations.
    On tente d'abord les colonnes prévues par migration_v10.

    IMPORTANT :
        - aucune valeur n'est inventée ;
        - NULL reste NULL.
    """

    if not lieu_ids:
        return {}

    # La migration v10 prévoit ces colonnes dans amenity_cache.
    # Elles ne sont pas nécessairement présentes dans toutes les lignes.
    try:
        rows = await fetch_all(
            """
            SELECT
                lieu_tournage_id,
                duree_pied_secondes,
                duree_voiture_secondes
            FROM amenity_cache
            WHERE lieu_tournage_id = ANY(%s)
              AND (
                    duree_pied_secondes IS NOT NULL
                 OR duree_voiture_secondes IS NOT NULL
              )
            """,
            (lieu_ids,),
        )
    except Exception:
        # Si la colonne n'est pas disponible sur une ancienne DB,
        # on ne fabrique rien.
        return {}

    result: dict[int, dict[str, int | None]] = defaultdict(
        lambda: {
            "duree_pied_secondes": None,
            "duree_voiture_secondes": None,
        }
    )

    for row in rows:
        lieu_id = _safe_int(row.get("lieu_tournage_id"))

        if lieu_id is None:
            continue

        pied = _safe_int(row.get("duree_pied_secondes"))
        voiture = _safe_int(row.get("duree_voiture_secondes"))

        current = result[lieu_id]

        if pied is not None:
            if (
                current["duree_pied_secondes"] is None
                or pied < current["duree_pied_secondes"]
            ):
                current["duree_pied_secondes"] = pied

        if voiture is not None:
            if (
                current["duree_voiture_secondes"] is None
                or voiture < current["duree_voiture_secondes"]
            ):
                current["duree_voiture_secondes"] = voiture

    return dict(result)


# ---------------------------------------------------------------------------
# Isochrones
# ---------------------------------------------------------------------------

async def _charger_isochrones(
    region: str,
    lieu_ids: list[int],
) -> dict[str, Any]:
    """
    Charge les isochrones uniquement pour les lieux publiés de la région.

    IMPORTANT :
        Le filtre territorial se fait ici par JOIN avec films.

        Sans ce JOIN, on risque de compter des isochrones appartenant
        à d'autres régions et d'obtenir par exemple 113,6 %.
    """

    if not lieu_ids:
        return {
            "couverture_pct": None,
            "minutes_disponibles": [],
            "couverture_par_minutes": [],
            "derniere_date": None,
        }

    rows = await fetch_all(
        """
        SELECT
            i.lieu_tournage_id,
            i.mode,
            i.minutes,
            i.calculated_at
        FROM isochrones i
        JOIN lieux_tournage lt
          ON lt.id = i.lieu_tournage_id
        JOIN films f
          ON f.id = lt.film_id
        WHERE f.region = %s
          AND f.statut = 'publie'
          AND i.lieu_tournage_id = ANY(%s)
        """,
        (region, lieu_ids),
    )

    total_lieux = len(lieu_ids)

    # lieu -> mode -> minutes disponibles
    presence: dict[int, dict[str, set[int]]] = defaultdict(
        lambda: defaultdict(set)
    )

    dates: list[Any] = []

    for row in rows:
        lieu_id = _safe_int(row.get("lieu_tournage_id"))

        minutes = _safe_int(row.get("minutes"))

        mode = _normalise_mode(row.get("mode"))

        if lieu_id is None or minutes is None:
            continue

        if mode not in {
            "driving-car",
            "foot-walking",
        }:
            continue

        if lieu_id not in lieu_ids:
            continue

        presence[lieu_id][mode].add(minutes)

        calculated_at = row.get("calculated_at")

        if calculated_at is not None:
            dates.append(calculated_at)

    # Minutes réellement présentes dans la DB.
    minutes_set: set[int] = set()

    for lieu_data in presence.values():
        for mode_data in lieu_data.values():
            minutes_set.update(mode_data)

    minutes_disponibles = sorted(minutes_set)

    couverture_par_minutes: list[dict[str, Any]] = []

    for minutes in minutes_disponibles:

        voiture_lieux = sum(
            1
            for lieu_id in lieu_ids
            if minutes in presence[lieu_id].get(
                "driving-car",
                set(),
            )
        )

        pied_lieux = sum(
            1
            for lieu_id in lieu_ids
            if minutes in presence[lieu_id].get(
                "foot-walking",
                set(),
            )
        )

        couverture_voiture = _pct(
            voiture_lieux,
            total_lieux,
        )

        couverture_pied = _pct(
            pied_lieux,
            total_lieux,
        )

        couverture_par_minutes.append(
            {
                "minutes": minutes,
                "voiture_pct": couverture_voiture,
                "pied_pct": couverture_pied,
                "voiture_lieux": voiture_lieux,
                "pied_lieux": pied_lieux,
            }
        )

    # ------------------------------------------------------------------
    # Couverture générale
    #
    # Un lieu est considéré couvert si au moins un isochrone existe
    # pour ce lieu, quel que soit le mode.
    # ------------------------------------------------------------------

    lieux_avec_isochrone = sum(
        1
        for lieu_id in lieu_ids
        if presence.get(lieu_id)
    )

    couverture_pct = _pct(
        lieux_avec_isochrone,
        total_lieux,
    )

    derniere_date = None

    if dates:
        try:
            derniere_date = max(dates)
        except Exception:
            derniere_date = None

    return {
        "couverture_pct": couverture_pct,
        "minutes_disponibles": minutes_disponibles,
        "couverture_par_minutes": couverture_par_minutes,
        "ratio_surface_voiture_marche_15": None,
        "derniere_date": _format_date(derniere_date),
    }


# ---------------------------------------------------------------------------
# Construction principale
# ---------------------------------------------------------------------------

async def construire_indicateurs_cinetourisme(
    region: str = "Occitanie",
) -> dict[str, Any]:
    """
    Construit l'ensemble des indicateurs de l'observatoire.

    Retour :
        {
            region,
            totaux,
            departements,
            accessibilite,
            equipements,
            isochrones,
            films_notables,
            lieux_potentiel,
            completude,
            metriques
        }
    """

    region = str(region or "Occitanie").strip()

    # ------------------------------------------------------------------
    # 1. Lieux publiés de la région
    # ------------------------------------------------------------------

    lieux = await fetch_all(
        """
        SELECT
            lt.id,
            lt.film_id,
            lt.commune,
            lt.departement,
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

    lieu_ids = [
        int(row["id"])
        for row in lieux
        if row.get("id") is not None
    ]

    # ------------------------------------------------------------------
    # Si aucun lieu
    # ------------------------------------------------------------------

    if not lieux:
        return {
            "region": region,

            "totaux": {
                "films": 0,
                "lieux": 0,
                "departements": 0,
            },

            "departements": [],

            "accessibilite": {
                "pret_15_pct": None,
                "pret_30_pct": None,
                "isoles_45_pct": None,
                "route_coverage_pct": None,
            },

            "equipements": {
                "moy_hebergement": None,
                "moy_restaurant": None,
                "hebergement_presence_pct": None,
                "restaurant_presence_pct": None,
                "hebergement_500m_pct": None,
                "restaurant_500m_pct": None,
            },

            "isochrones": {
                "couverture_pct": None,
                "minutes_disponibles": [],
                "couverture_par_minutes": [],
                "ratio_surface_voiture_marche_15": None,
                "derniere_date": None,
            },

            "films_notables": [],

            "lieux_potentiel": [],

            "completude": {
                "coordonnees_pct": None,
                "popularite_pct": None,
                "amenagement_pct": None,
                "isochrones_pct": None,
            },

            "metriques": {},
        }

    # ------------------------------------------------------------------
    # 2. Equipements
    # ------------------------------------------------------------------

    equipements = await _charger_equipements(lieu_ids)

    # ------------------------------------------------------------------
    # 3. Durées routières réellement disponibles
    # ------------------------------------------------------------------

    durees_routieres = await _charger_durees_routieres(lieu_ids)

    # ------------------------------------------------------------------
    # 4. Isochrones
    # ------------------------------------------------------------------

    isochrones = await _charger_isochrones(
        region,
        lieu_ids,
    )

    # ------------------------------------------------------------------
    # 5. Totaux
    # ------------------------------------------------------------------

    film_ids = {
        _safe_int(row.get("film_id"))
        for row in lieux
        if _safe_int(row.get("film_id")) is not None
    }

    departements = {
        str(row.get("departement")).strip()
        for row in lieux
        if row.get("departement")
        and str(row.get("departement")).strip()
    }

    totaux = {
        "films": len(film_ids),
        "lieux": len(lieux),
        "departements": len(departements),
    }

    total_lieux = len(lieux)

    # ------------------------------------------------------------------
    # 6. Départements
    # ------------------------------------------------------------------

    stats_departements: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "lieux": 0,
            "films": set(),
        }
    )

    for row in lieux:
        dep = row.get("departement")

        if not dep:
            continue

        dep = str(dep).strip()

        stats_departements[dep]["lieux"] += 1

        film_id = _safe_int(row.get("film_id"))

        if film_id is not None:
            stats_departements[dep]["films"].add(film_id)

    departements_result: list[dict[str, Any]] = []

    for dep, values in stats_departements.items():

        nb_lieux = int(values["lieux"])

        nb_films = len(values["films"])

        departements_result.append(
            {
                "departement": dep,
                "nb_lieux": nb_lieux,
                "nb_films": nb_films,
                "part_pct": _pct(
                    nb_lieux,
                    total_lieux,
                ),
            }
        )

    departements_result.sort(
        key=lambda item: (
            item["nb_lieux"],
            item["nb_films"],
        ),
        reverse=True,
    )

    # ------------------------------------------------------------------
    # 7. Equipements touristiques
    # ------------------------------------------------------------------

    hebergement_counts: list[float] = []
    restaurant_counts: list[float] = []

    hebergement_distances: list[float] = []
    restaurant_distances: list[float] = []

    hebergement_presence = 0
    restaurant_presence = 0

    hebergement_500m = 0
    restaurant_500m = 0

    amenity_lieux = 0

    for lieu_id in lieu_ids:

        data = equipements.get(lieu_id, {})

        hebergement = data.get("hebergement")
        restaurant = data.get("restaurant")

        if hebergement is not None or restaurant is not None:
            amenity_lieux += 1

        # --------------------------------------------------------------
        # Hébergement
        # --------------------------------------------------------------

        if hebergement is not None:

            nombre = _safe_int(
                hebergement.get("nombre_total")
            )

            if nombre is not None:
                hebergement_counts.append(
                    float(nombre)
                )

                if nombre > 0:
                    hebergement_presence += 1

            nombre_500 = _safe_int(
                hebergement.get("nombre_500m")
            )

            if nombre_500 is not None and nombre_500 > 0:
                hebergement_500m += 1

            distance = _safe_float(
                hebergement.get("distance_min_m")
            )

            if distance is not None:
                hebergement_distances.append(
                    distance
                )

        # --------------------------------------------------------------
        # Restaurant
        # --------------------------------------------------------------

        if restaurant is not None:

            nombre = _safe_int(
                restaurant.get("nombre_total")
            )

            if nombre is not None:
                restaurant_counts.append(
                    float(nombre)
                )

                if nombre > 0:
                    restaurant_presence += 1

            nombre_500 = _safe_int(
                restaurant.get("nombre_500m")
            )

            if nombre_500 is not None and nombre_500 > 0:
                restaurant_500m += 1

            distance = _safe_float(
                restaurant.get("distance_min_m")
            )

            if distance is not None:
                restaurant_distances.append(
                    distance
                )

    equipements_result = {
        # Moyenne du nombre d'équipements dans les résultats
        # DATAtourisme disponibles.
        "moy_hebergement": _round(
            sum(hebergement_counts)
            / len(hebergement_counts)
            if hebergement_counts
            else None,
            2,
        ),

        "moy_restaurant": _round(
            sum(restaurant_counts)
            / len(restaurant_counts)
            if restaurant_counts
            else None,
            2,
        ),

        "hebergement_presence_pct": _pct(
            hebergement_presence,
            total_lieux,
        ),

        "restaurant_presence_pct": _pct(
            restaurant_presence,
            total_lieux,
        ),

        "hebergement_500m_pct": _pct(
            hebergement_500m,
            total_lieux,
        ),

        "restaurant_500m_pct": _pct(
            restaurant_500m,
            total_lieux,
        ),
    }

    # ------------------------------------------------------------------
    # 8. Accessibilité routière
    # ------------------------------------------------------------------

    lieux_avec_route = 0
    lieux_15 = 0
    lieux_30 = 0

    # IMPORTANT :
    #
    # On ne considère pas "absence de durée" comme "plus de 45 min".
    # Donc isoles_45_pct reste NULL si les données ne permettent pas
    # de le calculer.
    #
    # Ici on compte uniquement les durées réelles disponibles.

    for lieu_id in lieu_ids:

        route = durees_routieres.get(lieu_id)

        if not route:
            continue

        voiture = route.get(
            "duree_voiture_secondes"
        )

        pied = route.get(
            "duree_pied_secondes"
        )

        if voiture is None and pied is None:
            continue

        lieux_avec_route += 1

        # Pour les indicateurs d'accès à un lieu,
        # on utilise une durée réelle voiture OU marche.
        #
        # On ne combine pas les deux pour créer une fausse mesure.
        durees = [
            value
            for value in (
                voiture,
                pied,
            )
            if value is not None
        ]

        if not durees:
            continue

        duree_min = min(durees)

        if duree_min <= 15 * 60:
            lieux_15 += 1

        if duree_min <= 30 * 60:
            lieux_30 += 1

    route_coverage_pct = _pct(
        lieux_avec_route,
        total_lieux,
    )

    # Si toute la population est couverte par des durées réelles,
    # on peut calculer ces indicateurs.
    #
    # Sinon, on ne présente pas une valeur partielle comme une vérité
    # sur toute la région.
    if lieux_avec_route == total_lieux:
        pret_15_pct = _pct(
            lieux_15,
            total_lieux,
        )

        pret_30_pct = _pct(
            lieux_30,
            total_lieux,
        )

        # Il faudrait ici disposer d'une mesure fiable de durée
        # > 45 min pour TOUS les lieux.
        #
        # On ne déduit pas cette valeur de NULL.
        isoles_45_pct = None
    else:
        pret_15_pct = None
        pret_30_pct = None
        isoles_45_pct = None

    accessibilite = {
        "pret_15_pct": pret_15_pct,
        "pret_30_pct": pret_30_pct,
        "isoles_45_pct": isoles_45_pct,
        "route_coverage_pct": route_coverage_pct,
    }

    # ------------------------------------------------------------------
    # 9. Filmographie notable
    # ------------------------------------------------------------------

    films_data: dict[int, dict[str, Any]] = {}

    for row in lieux:

        film_id = _safe_int(row.get("film_id"))

        if film_id is None:
            continue

        if film_id not in films_data:
            films_data[film_id] = {
                "film_id": film_id,
                "titre": row.get("titre"),
                "annee": _safe_int(row.get("annee")),
                "media_type": row.get("media_type"),
                "popularite": _safe_float(
                    row.get("popularite")
                ),
                "lieux": 0,
                "departements": set(),
            }

        data = films_data[film_id]

        data["lieux"] += 1

        dep = row.get("departement")

        if dep:
            data["departements"].add(
                str(dep).strip()
            )

    films_notables: list[dict[str, Any]] = []

    for data in films_data.values():

        films_notables.append(
            {
                "film_id": data["film_id"],
                "titre": data["titre"],
                "annee": data["annee"],
                "media_type": data["media_type"],
                "popularite": data["popularite"],
                "nb_lieux": data["lieux"],
                "nb_departements": len(
                    data["departements"]
                ),
                "departements": sorted(
                    data["departements"]
                ),
            }
        )

    films_notables.sort(
        key=lambda item: (
            item["popularite"]
            if item["popularite"] is not None
            else -1
        ),
        reverse=True,
    )

    films_notables = films_notables[:20]

    # ------------------------------------------------------------------
    # 10. Potentiel des lieux
    # ------------------------------------------------------------------

    # Popularité maximale réelle de la sélection.
    popularites = [
        _safe_float(row.get("popularite"))
        for row in lieux
        if _safe_float(row.get("popularite")) is not None
    ]

    max_popularite = (
        max(popularites)
        if popularites
        else None
    )

    lieux_potentiel: list[dict[str, Any]] = []

    for row in lieux:

        lieu_id = _safe_int(row.get("id"))

        if lieu_id is None:
            continue

        film_id = _safe_int(row.get("film_id"))

        popularite = _safe_float(
            row.get("popularite")
        )

        eq = equipements.get(
            lieu_id,
            {},
        )

        hebergement = eq.get(
            "hebergement"
        )

        restaurant = eq.get(
            "restaurant"
        )

        nb_hebergements = (
            _safe_int(
                hebergement.get("nombre_total")
            )
            if hebergement
            else 0
        )

        nb_restaurants = (
            _safe_int(
                restaurant.get("nombre_total")
            )
            if restaurant
            else 0
        )

        distance_hebergement = (
            _safe_float(
                hebergement.get(
                    "distance_min_m"
                )
            )
            if hebergement
            else None
        )

        distance_restaurant = (
            _safe_float(
                restaurant.get(
                    "distance_min_m"
                )
            )
            if restaurant
            else None
        )

        presence_hebergement = (
            nb_hebergements > 0
        )

        presence_restaurant = (
            nb_restaurants > 0
        )

        # --------------------------------------------------------------
        # Maturité observée
        #
        # 50% présence hébergement
        # 50% présence restaurant
        #
        # Ce n'est PAS un temps de préparation.
        # --------------------------------------------------------------

        maturite = (
            (
                int(presence_hebergement)
                + int(presence_restaurant)
            )
            / 2
            * 100
        )

        # --------------------------------------------------------------
        # Score de popularité
        #
        # Normalisation par rapport au maximum réel de la région.
        # --------------------------------------------------------------

        if (
            popularite is not None
            and max_popularite is not None
            and max_popularite > 0
        ):
            popularite_score = (
                popularite
                / max_popularite
                * 100
            )
        else:
            popularite_score = None

        # --------------------------------------------------------------
        # Score opportunité
        #
        # Moyenne simple entre :
        #     popularité observée
        #     maturité touristique observée
        #
        # Ce score est un indicateur de lecture et non une mesure
        # scientifique de valeur économique.
        # --------------------------------------------------------------

        composants = [
            value
            for value in (
                maturite,
                popularite_score,
            )
            if value is not None
        ]

        score_opportunite = (
            sum(composants)
            / len(composants)
            if composants
            else None
        )

        lieux_potentiel.append(
            {
                "lieu_id": lieu_id,
                "film_id": film_id,
                "titre": row.get("titre"),
                "commune": row.get("commune"),
                "departement": row.get("departement"),

                "popularite": popularite,

                "hebergement": presence_hebergement,
                "restaurant": presence_restaurant,

                "nb_hebergements": nb_hebergements,
                "nb_restaurants": nb_restaurants,

                "distance_hebergement_m": (
                    round(
                        distance_hebergement,
                        1,
                    )
                    if distance_hebergement is not None
                    else None
                ),

                "distance_restaurant_m": (
                    round(
                        distance_restaurant,
                        1,
                    )
                    if distance_restaurant is not None
                    else None
                ),

                "maturite_touristique_pct": round(
                    maturite,
                    1,
                ),

                "popularite_score": (
                    round(
                        popularite_score,
                        1,
                    )
                    if popularite_score is not None
                    else None
                ),

                # Compatibilité avec l'ancien frontend.
                # On conserve le champ mais on lui donne désormais
                # le sens "maturité touristique observée".
                "preparation": round(
                    maturite,
                    1,
                ),

                "score_opportunite": (
                    round(
                        score_opportunite,
                        1,
                    )
                    if score_opportunite is not None
                    else None
                ),
            }
        )

    lieux_potentiel.sort(
        key=lambda item: (
            item["score_opportunite"]
            if item["score_opportunite"] is not None
            else -1
        ),
        reverse=True,
    )

    lieux_potentiel = lieux_potentiel[:50]

    # ------------------------------------------------------------------
    # 11. Complétude des données
    # ------------------------------------------------------------------

    coordonnees = 0
    popularite_count = 0

    for row in lieux:

        latitude = _safe_float(
            row.get("latitude")
        )

        longitude = _safe_float(
            row.get("longitude")
        )

        if (
            latitude is not None
            and longitude is not None
        ):
            coordonnees += 1

        if (
            _safe_float(row.get("popularite"))
            is not None
        ):
            popularite_count += 1

    completude = {
        "coordonnees_pct": _pct(
            coordonnees,
            total_lieux,
        ),

        "popularite_pct": _pct(
            popularite_count,
            total_lieux,
        ),

        "amenagement_pct": _pct(
            amenity_lieux,
            total_lieux,
        ),

        "isochrones_pct": isochrones.get(
            "couverture_pct"
        ),
    }

    # ------------------------------------------------------------------
    # 12. Métriques complémentaires
    # ------------------------------------------------------------------

    # Concentration des lieux par département.
    parts_departements = [
        float(item["nb_lieux"])
        / total_lieux
        for item in departements_result
        if total_lieux > 0
    ]

    concentration_hhi = _hhi(
        parts_departements
    )

    top3_lieux = sum(
        item["nb_lieux"]
        for item in departements_result[:3]
    )

    concentration_top3_pct = _pct(
        top3_lieux,
        total_lieux,
    )

    # Distances moyennes réelles vers le premier équipement.
    distance_moy_hebergement_m = (
        sum(hebergement_distances)
        / len(hebergement_distances)
        if hebergement_distances
        else None
    )

    distance_moy_restaurant_m = (
        sum(restaurant_distances)
        / len(restaurant_distances)
        if restaurant_distances
        else None
    )

    metriques = {
        "concentration_hhi": concentration_hhi,

        "concentration_top3_pct": (
            round(
                concentration_top3_pct,
                1,
            )
            if concentration_top3_pct is not None
            else None
        ),

        "distance_moy_hebergement_m": (
            round(
                distance_moy_hebergement_m,
                1,
            )
            if distance_moy_hebergement_m is not None
            else None
        ),

        "distance_moy_restaurant_m": (
            round(
                distance_moy_restaurant_m,
                1,
            )
            if distance_moy_restaurant_m is not None
            else None
        ),

        "distance_mediane_hebergement_m": (
            round(
                _median(
                    hebergement_distances
                ),
                1,
            )
            if hebergement_distances
            else None
        ),

        "distance_mediane_restaurant_m": (
            round(
                _median(
                    restaurant_distances
                ),
                1,
            )
            if restaurant_distances
            else None
        ),

        "lieux_avec_route": lieux_avec_route,

        "lieux_avec_isochrones": (
            round(
                total_lieux
                * (
                    isochrones.get(
                        "couverture_pct"
                    )
                    or 0
                )
                / 100
            )
            if isochrones.get(
                "couverture_pct"
            ) is not None
            else None
        ),

        "lieux_avec_equipements": amenity_lieux,
    }

    # ------------------------------------------------------------------
    # 13. Résultat final
    # ------------------------------------------------------------------

    return {
        "region": region,

        "totaux": totaux,

        "departements": departements_result,

        "accessibilite": accessibilite,

        "equipements": equipements_result,

        "isochrones": isochrones,

        "films_notables": films_notables,

        "lieux_potentiel": lieux_potentiel,

        "completude": completude,

        "metriques": metriques,
    }