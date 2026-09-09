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
    - les durées de trajet ne sont utilisées que lorsqu'elles existent réellement ;
    - les statistiques descriptives sont calculées sur les observations
      individuelles disponibles ;
    - voiture et marche restent deux modes distincts ;
    - les indicateurs historiques conservés pour le frontend sont documentés
      lorsqu'ils correspondent désormais à une sémantique plus précise.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import datetime
from typing import Any

from db import fetch_all


# ============================================================================
# HELPERS GENERAUX
# ============================================================================

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


def _pct(
    numerator: int | float | None,
    denominator: int | float | None,
) -> float | None:
    """
    Pourcentage sécurisé.

    Aucun 0 artificiel n'est produit lorsqu'il n'existe pas de
    dénominateur exploitable.
    """

    if numerator is None or denominator in (None, 0):
        return None

    return round(float(numerator) / float(denominator) * 100.0, 1)


def _normalise_mode(mode: Any) -> str:
    """
    Normalise les modes présents éventuellement dans la DB.

    Valeurs canoniques :
        driving-car
        foot-walking
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
    Retourne une date ISO lisible.
    """

    if value is None:
        return None

    if isinstance(value, datetime):
        return value.isoformat()

    return str(value)


def _median(values: list[float]) -> float | None:
    """
    Médiane descriptive simple.

    Cette fonction est utilisée uniquement pour les distributions
    reconstruites explicitement en Python.
    """

    if not values:
        return None

    values = sorted(values)
    n = len(values)

    if n % 2 == 1:
        return values[n // 2]

    return (values[n // 2 - 1] + values[n // 2]) / 2.0


def _percentile_cont(
    values: list[float],
    percentile: float,
) -> float | None:
    """
    Approximation de percentile_cont PostgreSQL pour les distributions
    reconstruites en Python.

    Utilisée uniquement lorsque l'ensemble des observations individuelles
    est déjà récupéré.
    """

    if not values:
        return None

    values = sorted(values)

    if len(values) == 1:
        return values[0]

    position = (len(values) - 1) * percentile

    lower = int(math.floor(position))
    upper = int(math.ceil(position))

    if lower == upper:
        return values[lower]

    fraction = position - lower

    return (
        values[lower]
        + (values[upper] - values[lower]) * fraction
    )


def _stddev_population(values: list[float]) -> float | None:
    """
    Écart-type population.

    L'observatoire décrit les lieux effectivement observés et non
    un échantillon destiné à estimer une population inconnue.
    """

    if not values:
        return None

    moyenne = sum(values) / len(values)

    variance = sum(
        (value - moyenne) ** 2
        for value in values
    ) / len(values)

    return math.sqrt(variance)


def _stats_python(values: list[float]) -> dict[str, Any]:
    """
    Produit le même contrat statistique que les agrégats PostgreSQL :

        moyenne
        mediane
        q1
        q3
        iqr
        ecart_type
        p90
        cv_pct
    """

    if not values:
        return {
            "moyenne": None,
            "mediane": None,
            "q1": None,
            "q3": None,
            "iqr": None,
            "ecart_type": None,
            "p90": None,
            "cv_pct": None,
        }

    moyenne = sum(values) / len(values)
    mediane = _percentile_cont(values, 0.5)
    q1 = _percentile_cont(values, 0.25)
    q3 = _percentile_cont(values, 0.75)
    ecart_type = _stddev_population(values)
    p90 = _percentile_cont(values, 0.9)

    iqr = (
        q3 - q1
        if q1 is not None and q3 is not None
        else None
    )

    cv_pct = (
        ecart_type / moyenne * 100.0
        if ecart_type is not None and moyenne != 0
        else None
    )

    return {
        "moyenne": round(moyenne, 2),
        "mediane": round(mediane, 2) if mediane is not None else None,
        "q1": round(q1, 2) if q1 is not None else None,
        "q3": round(q3, 2) if q3 is not None else None,
        "iqr": round(iqr, 2) if iqr is not None else None,
        "ecart_type": (
            round(ecart_type, 2)
            if ecart_type is not None
            else None
        ),
        "p90": round(p90, 2) if p90 is not None else None,
        "cv_pct": (
            round(cv_pct, 1)
            if cv_pct is not None
            else None
        ),
    }


# ============================================================================
# SURFACE GEOJSON
# ============================================================================

def _aire_km2_geojson(geometry: Any) -> float | None:
    """
    Calcule une surface approximative en km² pour un Polygon ou MultiPolygon
    GeoJSON.

    Projection équirectangulaire locale puis formule du lacet.

    Cette approximation est destinée aux comparaisons de surfaces
    d'isochrones, pas à une mesure cadastrale ou réglementaire.
    """

    if isinstance(geometry, str):
        try:
            geometry = json.loads(geometry)
        except Exception:
            return None

    if not isinstance(geometry, dict):
        return None

    geom_type = geometry.get("type")
    coords = geometry.get("coordinates")

    if not coords:
        return None

    if geom_type == "Polygon":
        polygones = [coords]

    elif geom_type == "MultiPolygon":
        polygones = coords

    else:
        return None

    def _aire_anneau(anneau: list) -> float:
        if not anneau or len(anneau) < 3:
            return 0.0

        try:
            lat_ref = float(anneau[0][1])
        except (TypeError, ValueError, IndexError):
            return 0.0

        m_par_deg_lat = 111_320.0

        m_par_deg_lon = (
            111_320.0
            * math.cos(math.radians(lat_ref))
        )

        pts = []

        for point in anneau:
            if not point or len(point) < 2:
                continue

            try:
                lon = float(point[0])
                lat = float(point[1])
            except (TypeError, ValueError):
                continue

            pts.append(
                (
                    lon * m_par_deg_lon,
                    lat * m_par_deg_lat,
                )
            )

        if len(pts) < 3:
            return 0.0

        aire = 0.0

        for i in range(len(pts) - 1):
            x1, y1 = pts[i]
            x2, y2 = pts[i + 1]

            aire += (
                x1 * y2
                - x2 * y1
            )

        return abs(aire) / 2.0

    total_m2 = 0.0

    for polygone in polygones:
        if not polygone:
            continue

        aire_polygone = _aire_anneau(
            polygone[0]
        )

        for trou in polygone[1:]:
            aire_polygone -= _aire_anneau(trou)

        total_m2 += max(
            aire_polygone,
            0.0,
        )

    return round(
        total_m2 / 1_000_000.0,
        3,
    )


# ============================================================================
# HHI
# ============================================================================

def _hhi(parts: list[float]) -> float | None:
    """
    Indice de concentration de Herfindahl-Hirschman.

    Les parts sont exprimées entre 0 et 1.
    """

    if not parts:
        return None

    return round(
        sum(float(part) ** 2 for part in parts),
        4,
    )


# ============================================================================
# EQUIPMENTS TOURISTIQUES
# ============================================================================

async def _charger_equipements(
    lieu_ids: list[int],
) -> dict[int, dict[str, Any]]:
    """
    Charge les statistiques DATAtourisme.

    Priorité pour les distances :
        1. amenity_stats.distance_min_m
        2. amenity_cache.distance_metres

    Les distances du cache sont triées avant le calcul du top 10.

    Le champ nombre_total reste issu de amenity_stats.
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
        lieu_id = _safe_int(
            row.get("lieu_tournage_id")
        )

        if lieu_id is None:
            continue

        categorie = str(
            row.get("categorie") or ""
        ).strip().lower()

        if not categorie:
            continue

        result[lieu_id][categorie] = {
            "nombre_total": _safe_int(
                row.get("nombre_total")
            ),
            "nombre_500m": _safe_int(
                row.get("nombre_500m")
            ),
            "nombre_1000m": _safe_int(
                row.get("nombre_1000m")
            ),
            "distance_min_m": _safe_float(
                row.get("distance_min_m")
            ),
            "distance_moy_top10_m": _safe_float(
                row.get("distance_moy_top10_m")
            ),
        }

    # ------------------------------------------------------------------
    # Fallback amenity_cache
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

    cache_distances: dict[
        int,
        dict[str, list[float]]
    ] = defaultdict(
        lambda: defaultdict(list)
    )

    for row in cache_rows:
        lieu_id = _safe_int(
            row.get("lieu_tournage_id")
        )

        if lieu_id is None:
            continue

        categorie = str(
            row.get("categorie") or ""
        ).strip().lower()

        distance = _safe_float(
            row.get("distance_metres")
        )

        if not categorie or distance is None:
            continue

        cache_distances[
            lieu_id
        ][categorie].append(distance)

    # ------------------------------------------------------------------
    # Compléter uniquement les informations absentes
    # ------------------------------------------------------------------

    for lieu_id, categories in cache_distances.items():

        for categorie, distances in categories.items():

            if not distances:
                continue

            distances = sorted(distances)

            minimum = distances[0]

            top10 = distances[:10]

            moyenne_top10 = (
                sum(top10) / len(top10)
                if top10
                else None
            )

            if categorie not in result[lieu_id]:

                result[lieu_id][categorie] = {
                    "nombre_total": 0,
                    "nombre_500m": sum(
                        1
                        for distance in distances
                        if distance <= 500
                    ),
                    "nombre_1000m": sum(
                        1
                        for distance in distances
                        if distance <= 1000
                    ),
                    "distance_min_m": minimum,
                    "distance_moy_top10_m": moyenne_top10,
                }

            else:

                current = result[
                    lieu_id
                ][categorie]

                if current.get(
                    "distance_min_m"
                ) is None:
                    current[
                        "distance_min_m"
                    ] = minimum

                if current.get(
                    "distance_moy_top10_m"
                ) is None:
                    current[
                        "distance_moy_top10_m"
                    ] = moyenne_top10

                if current.get(
                    "nombre_500m"
                ) is None:
                    current[
                        "nombre_500m"
                    ] = sum(
                        1
                        for distance in distances
                        if distance <= 500
                    )

                if current.get(
                    "nombre_1000m"
                ) is None:
                    current[
                        "nombre_1000m"
                    ] = sum(
                        1
                        for distance in distances
                        if distance <= 1000
                    )

    return dict(result)


# ============================================================================
# DUREES ROUTIERES
# ============================================================================

async def _charger_durees_routieres(
    lieu_ids: list[int],
) -> dict[int, dict[str, int | None]]:
    """
    Charge les durées réellement disponibles.

    Deux modes distincts :
        - duree_voiture_secondes
        - duree_pied_secondes

    Aucune durée n'est déduite ou interpolée.

    Si plusieurs lignes existent pour un lieu, on conserve la durée
    minimale réellement enregistrée pour chaque mode.
    """

    if not lieu_ids:
        return {}

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
        return {}

    result: dict[
        int,
        dict[str, int | None]
    ] = defaultdict(
        lambda: {
            "duree_pied_secondes": None,
            "duree_voiture_secondes": None,
        }
    )

    for row in rows:

        lieu_id = _safe_int(
            row.get("lieu_tournage_id")
        )

        if lieu_id is None:
            continue

        pied = _safe_int(
            row.get("duree_pied_secondes")
        )

        voiture = _safe_int(
            row.get("duree_voiture_secondes")
        )

        current = result[lieu_id]

        if pied is not None:

            current_pied = current[
                "duree_pied_secondes"
            ]

            if (
                current_pied is None
                or pied < current_pied
            ):
                current[
                    "duree_pied_secondes"
                ] = pied

        if voiture is not None:

            current_voiture = current[
                "duree_voiture_secondes"
            ]

            if (
                current_voiture is None
                or voiture < current_voiture
            ):
                current[
                    "duree_voiture_secondes"
                ] = voiture

    return dict(result)


# ============================================================================
# ISOCHRONES
# ============================================================================

async def _charger_isochrones(
    region: str,
    lieu_ids: list[int],
) -> dict[str, Any]:
    """
    Charge les isochrones réellement présents en DB.

    Les modes sont traités séparément.

    Le ratio de surface voiture/marche à 15 minutes est calculé uniquement
    sur les lieux disposant d'une surface valide pour LES DEUX modes.

    Cela évite de comparer :
        moyenne voiture sur N lieux
        avec
        moyenne marche sur M autres lieux.
    """

    empty_result = {
        "couverture_pct": None,
        "couverture_voiture_pct": None,
        "couverture_pied_pct": None,
        "minutes_disponibles": [],
        "couverture_par_minutes": [],
        "ratio_surface_voiture_marche_15": None,
        "surface_moyenne_voiture_15_km2": None,
        "surface_moyenne_marche_15_km2": None,
        "surface_moyenne_voiture_15_commun_km2": None,
        "surface_moyenne_marche_15_commun_km2": None,
        "nb_lieux_surface_15_commun": 0,
        "surface_voiture_15_par_departement": [],
        "lieu_plus_enclave": None,
        "lieu_plus_dense": None,
        "derniere_date": None,
    }

    if not lieu_ids:
        return empty_result

    rows = await fetch_all(
        """
        SELECT
            i.lieu_tournage_id,
            i.mode,
            i.minutes,
            i.calculated_at,
            i.geometry_geojson,
            lt.nom AS lieu_nom,
            lt.departement AS lieu_departement
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

    # ------------------------------------------------------------------
    # Présence des isochrones
    #
    # lieu -> mode -> minutes
    # ------------------------------------------------------------------

    presence: dict[
        int,
        dict[str, set[int]]
    ] = defaultdict(
        lambda: defaultdict(set)
    )

    dates: list[Any] = []

    # ------------------------------------------------------------------
    # Surfaces à 15 minutes
    # ------------------------------------------------------------------

    surfaces_15: dict[
        int,
        dict[str, float]
    ] = defaultdict(dict)

    aires_lieux_15min_voiture: list[
        dict[str, Any]
    ] = []

    aires_par_departement: dict[
        str,
        list[float]
    ] = defaultdict(list)

    for row in rows:

        lieu_id = _safe_int(
            row.get("lieu_tournage_id")
        )

        minutes = _safe_int(
            row.get("minutes")
        )

        mode = _normalise_mode(
            row.get("mode")
        )

        if lieu_id is None or minutes is None:
            continue

        if mode not in {
            "driving-car",
            "foot-walking",
        }:
            continue

        if lieu_id not in lieu_ids:
            continue

        presence[
            lieu_id
        ][mode].add(minutes)

        calculated_at = row.get(
            "calculated_at"
        )

        if calculated_at is not None:
            dates.append(calculated_at)

        # --------------------------------------------------------------
        # Surface 15 minutes
        # --------------------------------------------------------------

        if minutes == 15:

            aire = _aire_km2_geojson(
                row.get("geometry_geojson")
            )

            if aire is None or aire <= 0:
                continue

            # Si plusieurs géométries existent pour le même lieu/mode,
            # on conserve la dernière valeur parcourue. La requête
            # existante est supposée contenir une géométrie par couple
            # lieu/mode/minute.
            surfaces_15[
                lieu_id
            ][mode] = aire

            if mode == "driving-car":

                aires_lieux_15min_voiture.append(
                    {
                        "lieu_id": lieu_id,
                        "nom": row.get("lieu_nom"),
                        "departement": row.get(
                            "lieu_departement"
                        ),
                        "aire_km2": aire,
                    }
                )

                dep = row.get(
                    "lieu_departement"
                )

                if dep:
                    aires_par_departement[
                        str(dep).strip()
                    ].append(aire)

    # ------------------------------------------------------------------
    # Minutes réellement disponibles
    # ------------------------------------------------------------------

    minutes_set: set[int] = set()

    for lieu_data in presence.values():

        for mode_data in lieu_data.values():
            minutes_set.update(mode_data)

    minutes_disponibles = sorted(
        minutes_set
    )

    # ------------------------------------------------------------------
    # Couverture par minute ET par mode
    # ------------------------------------------------------------------

    couverture_par_minutes = []

    for minutes in minutes_disponibles:

        voiture_lieux = sum(
            1
            for lieu_id in lieu_ids
            if minutes in presence[
                lieu_id
            ].get(
                "driving-car",
                set(),
            )
        )

        pied_lieux = sum(
            1
            for lieu_id in lieu_ids
            if minutes in presence[
                lieu_id
            ].get(
                "foot-walking",
                set(),
            )
        )

        couverture_par_minutes.append(
            {
                "minutes": minutes,
                "voiture_pct": _pct(
                    voiture_lieux,
                    total_lieux,
                ),
                "pied_pct": _pct(
                    pied_lieux,
                    total_lieux,
                ),
                "voiture_lieux": voiture_lieux,
                "pied_lieux": pied_lieux,
            }
        )

    # ------------------------------------------------------------------
    # Couverture générale
    # ------------------------------------------------------------------

    lieux_avec_isochrone = sum(
        1
        for lieu_id in lieu_ids
        if presence.get(lieu_id)
    )

    lieux_avec_voiture = sum(
        1
        for lieu_id in lieu_ids
        if presence[
            lieu_id
        ].get(
            "driving-car"
        )
    )

    lieux_avec_pied = sum(
        1
        for lieu_id in lieu_ids
        if presence[
            lieu_id
        ].get(
            "foot-walking"
        )
    )

    couverture_pct = _pct(
        lieux_avec_isochrone,
        total_lieux,
    )

    couverture_voiture_pct = _pct(
        lieux_avec_voiture,
        total_lieux,
    )

    couverture_pied_pct = _pct(
        lieux_avec_pied,
        total_lieux,
    )

    # ------------------------------------------------------------------
    # Date la plus récente
    # ------------------------------------------------------------------

    derniere_date = None

    if dates:
        try:
            derniere_date = max(dates)
        except Exception:
            derniere_date = None

    # ------------------------------------------------------------------
    # Surfaces moyennes à 15 min
    # ------------------------------------------------------------------

    surfaces_voiture = [
        data["driving-car"]
        for data in surfaces_15.values()
        if "driving-car" in data
    ]

    surfaces_marche = [
        data["foot-walking"]
        for data in surfaces_15.values()
        if "foot-walking" in data
    ]

    surface_moyenne_voiture_15 = (
        round(
            sum(surfaces_voiture)
            / len(surfaces_voiture),
            2,
        )
        if surfaces_voiture
        else None
    )

    surface_moyenne_marche_15 = (
        round(
            sum(surfaces_marche)
            / len(surfaces_marche),
            2,
        )
        if surfaces_marche
        else None
    )

    # ------------------------------------------------------------------
    # Ratio voiture / marche
    #
    # UNIQUEMENT sur les mêmes lieux.
    # ------------------------------------------------------------------

    lieux_communs_15 = [
        lieu_id
        for lieu_id, modes in surfaces_15.items()
        if (
            "driving-car" in modes
            and "foot-walking" in modes
        )
    ]

    surfaces_voiture_communes = [
        surfaces_15[lieu_id]["driving-car"]
        for lieu_id in lieux_communs_15
    ]

    surfaces_marche_communes = [
        surfaces_15[lieu_id]["foot-walking"]
        for lieu_id in lieux_communs_15
    ]

    surface_moyenne_voiture_15_commun = (
        round(
            sum(surfaces_voiture_communes)
            / len(surfaces_voiture_communes),
            2,
        )
        if surfaces_voiture_communes
        else None
    )

    surface_moyenne_marche_15_commun = (
        round(
            sum(surfaces_marche_communes)
            / len(surfaces_marche_communes),
            2,
        )
        if surfaces_marche_communes
        else None
    )

    ratio_surface_voiture_marche_15 = (
        round(
            surface_moyenne_voiture_15_commun
            / surface_moyenne_marche_15_commun,
            1,
        )
        if (
            surface_moyenne_voiture_15_commun is not None
            and surface_moyenne_marche_15_commun is not None
            and surface_moyenne_marche_15_commun > 0
        )
        else None
    )

    # ------------------------------------------------------------------
    # Lieux extrêmes — uniquement descriptifs
    # ------------------------------------------------------------------

    lieu_plus_enclave = None
    lieu_plus_dense = None

    if aires_lieux_15min_voiture:

        lieu_plus_enclave = max(
            aires_lieux_15min_voiture,
            key=lambda item: item["aire_km2"],
        )

        lieu_plus_dense = min(
            aires_lieux_15min_voiture,
            key=lambda item: item["aire_km2"],
        )

    # ------------------------------------------------------------------
    # Surface moyenne voiture 15 min par département
    # ------------------------------------------------------------------

    surface_voiture_15_par_departement = []

    for dep, aires in aires_par_departement.items():

        if not aires:
            continue

        surface_voiture_15_par_departement.append(
            {
                "departement": dep,
                "surface_moyenne_15min_km2": round(
                    sum(aires) / len(aires),
                    1,
                ),
                "nb_lieux_calcules": len(aires),
            }
        )

    surface_voiture_15_par_departement.sort(
        key=lambda item: item[
            "surface_moyenne_15min_km2"
        ],
        reverse=True,
    )

    return {
        "couverture_pct": couverture_pct,
        "couverture_voiture_pct": couverture_voiture_pct,
        "couverture_pied_pct": couverture_pied_pct,

        "minutes_disponibles": minutes_disponibles,

        "couverture_par_minutes": couverture_par_minutes,

        "ratio_surface_voiture_marche_15": (
            ratio_surface_voiture_marche_15
        ),

        "surface_moyenne_voiture_15_km2": (
            surface_moyenne_voiture_15
        ),

        "surface_moyenne_marche_15_km2": (
            surface_moyenne_marche_15
        ),

        "surface_moyenne_voiture_15_commun_km2": (
            surface_moyenne_voiture_15_commun
        ),

        "surface_moyenne_marche_15_commun_km2": (
            surface_moyenne_marche_15_commun
        ),

        "nb_lieux_surface_15_commun": len(
            lieux_communs_15
        ),

        "surface_voiture_15_par_departement": (
            surface_voiture_15_par_departement
        ),

        "lieu_plus_enclave": lieu_plus_enclave,

        "lieu_plus_dense": lieu_plus_dense,

        "derniere_date": _format_date(
            derniere_date
        ),
    }


# ============================================================================
# STATISTIQUES DES CATEGORIES
# ============================================================================

def _bundle_percentiles(
    row: dict,
    prefixe: str,
) -> dict[str, Any]:
    """
    Construit :

        moyenne
        mediane
        q1
        q3
        iqr
        ecart_type
        p90
        cv_pct
    """

    moyenne = _safe_float(
        row.get(f"{prefixe}_moyenne")
    )

    mediane = _safe_float(
        row.get(f"{prefixe}_mediane")
    )

    q1 = _safe_float(
        row.get(f"{prefixe}_q1")
    )

    q3 = _safe_float(
        row.get(f"{prefixe}_q3")
    )

    ecart_type = _safe_float(
        row.get(f"{prefixe}_ecart_type")
    )

    p90 = _safe_float(
        row.get(f"{prefixe}_p90")
    )

    iqr = (
        q3 - q1
        if q1 is not None and q3 is not None
        else None
    )

    cv_pct = (
        ecart_type / moyenne * 100.0
        if (
            ecart_type is not None
            and moyenne is not None
            and moyenne != 0
        )
        else None
    )

    return {
        "moyenne": (
            round(moyenne, 2)
            if moyenne is not None
            else None
        ),

        "mediane": (
            round(mediane, 2)
            if mediane is not None
            else None
        ),

        "q1": (
            round(q1, 2)
            if q1 is not None
            else None
        ),

        "q3": (
            round(q3, 2)
            if q3 is not None
            else None
        ),

        "iqr": (
            round(iqr, 2)
            if iqr is not None
            else None
        ),

        "ecart_type": (
            round(ecart_type, 2)
            if ecart_type is not None
            else None
        ),

        "p90": (
            round(p90, 2)
            if p90 is not None
            else None
        ),

        "cv_pct": (
            round(cv_pct, 1)
            if cv_pct is not None
            else None
        ),
    }


async def _stats_categorie(
    region: str,
    categorie: str,
    departement: str | None = None,
) -> dict[str, Any]:
    """
    Statistiques descriptives d'une catégorie DATAtourisme.

    Trois familles :

        1. nombre_total
           -> nombre d'équipements retournés dans le périmètre
              DATAtourisme de la catégorie ;

        2. nombre_1000m
           -> nombre d'équipements dans un rayon FIXE de 1 km ;
              ce bloc est comparable entre catégories ;

        3. distance_min_m
           -> distance du lieu vers le premier équipement.

    Les dénominateurs des indicateurs de proximité sont séparés :
        - n_nombre pour les données de nombre ;
        - n_distance pour les données de distance.

    Cela évite par exemple de calculer un taux de proximité sur les lieux
    ne possédant aucune distance réellement disponible.
    """

    condition_dep = (
        "AND lt.departement = %s"
        if departement
        else ""
    )

    params: tuple = (
        categorie,
        region,
        *(
            (departement,)
            if departement
            else ()
        ),
    )

    rows = await fetch_all(
        f"""
        SELECT
            COUNT(DISTINCT lt.id)
                FILTER (
                    WHERE ast.nombre_total IS NOT NULL
                ) AS n_nombre,

            AVG(ast.nombre_total)
                AS off_moyenne,

            percentile_cont(0.5)
                WITHIN GROUP (
                    ORDER BY ast.nombre_total
                ) AS off_mediane,

            percentile_cont(0.25)
                WITHIN GROUP (
                    ORDER BY ast.nombre_total
                ) AS off_q1,

            percentile_cont(0.75)
                WITHIN GROUP (
                    ORDER BY ast.nombre_total
                ) AS off_q3,

            stddev_pop(ast.nombre_total)
                AS off_ecart_type,

            percentile_cont(0.9)
                WITHIN GROUP (
                    ORDER BY ast.nombre_total
                ) AS off_p90,

            COUNT(DISTINCT lt.id)
                FILTER (
                    WHERE ast.nombre_1000m IS NOT NULL
                ) AS n_rayon1km,

            AVG(ast.nombre_1000m)
                AS rayon1km_moyenne,

            percentile_cont(0.5)
                WITHIN GROUP (
                    ORDER BY ast.nombre_1000m
                ) AS rayon1km_mediane,

            percentile_cont(0.25)
                WITHIN GROUP (
                    ORDER BY ast.nombre_1000m
                ) AS rayon1km_q1,

            percentile_cont(0.75)
                WITHIN GROUP (
                    ORDER BY ast.nombre_1000m
                ) AS rayon1km_q3,

            stddev_pop(ast.nombre_1000m)
                AS rayon1km_ecart_type,

            percentile_cont(0.9)
                WITHIN GROUP (
                    ORDER BY ast.nombre_1000m
                ) AS rayon1km_p90,

            COUNT(DISTINCT lt.id)
                FILTER (
                    WHERE ast.distance_min_m IS NOT NULL
                ) AS n_distance,

            AVG(ast.distance_min_m)
                AS prox_moyenne,

            percentile_cont(0.5)
                WITHIN GROUP (
                    ORDER BY ast.distance_min_m
                ) AS prox_mediane,

            percentile_cont(0.25)
                WITHIN GROUP (
                    ORDER BY ast.distance_min_m
                ) AS prox_q1,

            percentile_cont(0.75)
                WITHIN GROUP (
                    ORDER BY ast.distance_min_m
                ) AS prox_q3,

            stddev_pop(ast.distance_min_m)
                AS prox_ecart_type,

            percentile_cont(0.9)
                WITHIN GROUP (
                    ORDER BY ast.distance_min_m
                ) AS prox_p90,

            COUNT(DISTINCT lt.id)
                FILTER (
                    WHERE ast.distance_moy_top10_m IS NOT NULL
                ) AS n_top10,

            AVG(ast.distance_moy_top10_m)
                AS top10_moyenne,

            percentile_cont(0.5)
                WITHIN GROUP (
                    ORDER BY ast.distance_moy_top10_m
                ) AS top10_mediane,

            percentile_cont(0.25)
                WITHIN GROUP (
                    ORDER BY ast.distance_moy_top10_m
                ) AS top10_q1,

            percentile_cont(0.75)
                WITHIN GROUP (
                    ORDER BY ast.distance_moy_top10_m
                ) AS top10_q3,

            stddev_pop(ast.distance_moy_top10_m)
                AS top10_ecart_type,

            percentile_cont(0.9)
                WITHIN GROUP (
                    ORDER BY ast.distance_moy_top10_m
                ) AS top10_p90,

            COUNT(DISTINCT lt.id)
                FILTER (
                    WHERE ast.distance_min_m <= 250
                ) AS prox_250,

            COUNT(DISTINCT lt.id)
                FILTER (
                    WHERE ast.distance_min_m <= 500
                ) AS prox_500,

            COUNT(DISTINCT lt.id)
                FILTER (
                    WHERE ast.distance_min_m <= 1000
                ) AS prox_1000,

            COUNT(DISTINCT lt.id)
                FILTER (
                    WHERE ast.distance_min_m <= 2000
                ) AS prox_2000,

            COUNT(DISTINCT lt.id)
                FILTER (
                    WHERE ast.distance_min_m <= 5000
                ) AS prox_5000,

            COUNT(DISTINCT lt.id)
                FILTER (
                    WHERE ast.nombre_total = 0
                ) AS lac_sans_equipement,

            COUNT(DISTINCT lt.id)
                FILTER (
                    WHERE ast.distance_min_m > 500
                ) AS lac_500,

            COUNT(DISTINCT lt.id)
                FILTER (
                    WHERE ast.distance_min_m > 1000
                ) AS lac_1000,

            COUNT(DISTINCT lt.id)
                FILTER (
                    WHERE ast.distance_min_m > 2000
                ) AS lac_2000,

            COUNT(DISTINCT lt.id)
                FILTER (
                    WHERE ast.distance_min_m > 5000
                ) AS lac_5000

        FROM lieux_tournage lt

        JOIN films f
          ON f.id = lt.film_id

        LEFT JOIN amenity_stats ast
          ON ast.lieu_tournage_id = lt.id
         AND ast.categorie = %s

        WHERE f.region = %s
          AND f.statut = 'publie'
          {condition_dep}
        """,
        params,
    )

    if not rows:
        return {}

    row = rows[0]

    n_nombre = (
        _safe_int(row.get("n_nombre"))
        or 0
    )

    n_distance = (
        _safe_int(row.get("n_distance"))
        or 0
    )

    n_top10 = (
        _safe_int(row.get("n_top10"))
        or 0
    )

    n_rayon1km = (
        _safe_int(row.get("n_rayon1km"))
        or 0
    )

    def _pct_count(
        key: str,
        denominator: int,
    ) -> float | None:

        value = _safe_int(
            row.get(key)
        )

        return _pct(
            value,
            denominator,
        )

    return {
        "n": n_nombre,

        "nombre": _bundle_percentiles(
            row,
            "off",
        ),

        # --------------------------------------------------------------
        # Nombre à rayon standard de 1 km.
        #
        # Contrairement à nombre_total, ce bloc repose sur un rayon
        # identique pour toutes les catégories.
        # --------------------------------------------------------------

        "nombre_rayon_standard_1km": (
            _bundle_percentiles(
                row,
                "rayon1km",
            )
            if n_rayon1km > 0
            else {
                "moyenne": None,
                "mediane": None,
                "q1": None,
                "q3": None,
                "iqr": None,
                "ecart_type": None,
                "p90": None,
                "cv_pct": None,
            }
        ),

        "distance_plus_proche_m": (
            _bundle_percentiles(
                row,
                "prox",
            )
            if n_distance > 0
            else {
                "moyenne": None,
                "mediane": None,
                "q1": None,
                "q3": None,
                "iqr": None,
                "ecart_type": None,
                "p90": None,
                "cv_pct": None,
            }
        ),

        "distance_moyenne_10_plus_proches_m": (
            _bundle_percentiles(
                row,
                "top10",
            )
            if n_top10 > 0
            else {
                "moyenne": None,
                "mediane": None,
                "q1": None,
                "q3": None,
                "iqr": None,
                "ecart_type": None,
                "p90": None,
                "cv_pct": None,
            }
        ),

        "proximite": {
            "a_250m_pct": _pct_count(
                "prox_250",
                n_distance,
            ),

            "a_500m_pct": _pct_count(
                "prox_500",
                n_distance,
            ),

            "a_1km_pct": _pct_count(
                "prox_1000",
                n_distance,
            ),

            "a_2km_pct": _pct_count(
                "prox_2000",
                n_distance,
            ),

            "a_5km_pct": _pct_count(
                "prox_5000",
                n_distance,
            ),
        },

        "carence": {
            "sans_equipement_n": _safe_int(
                row.get("lac_sans_equipement")
            ),

            "sans_equipement_pct": _pct_count(
                "lac_sans_equipement",
                n_nombre,
            ),

            "au_dela_500m_n": _safe_int(
                row.get("lac_500")
            ),

            "au_dela_1km_n": _safe_int(
                row.get("lac_1000")
            ),

            "au_dela_2km_n": _safe_int(
                row.get("lac_2000")
            ),

            "au_dela_5km_n": _safe_int(
                row.get("lac_5000")
            ),
        },

        "n_distance": n_distance,
        "n_top10": n_top10,
        "n_rayon_standard_1km": n_rayon1km,
    }


# ============================================================================
# DIVERSITE FONCTIONNELLE
# ============================================================================

async def _diversite_fonctionnelle(
    region: str,
    departement: str | None = None,
) -> dict[str, Any]:
    """
    Nombre de catégories DATAtourisme ayant au moins un équipement
    observé par lieu.

    La distribution est calculée au niveau des lieux.

    Les statistiques utilisent la même convention percentile_cont
    que les autres indicateurs.
    """

    condition_dep = (
        "AND lt.departement = %s"
        if departement
        else ""
    )

    params: tuple = (
        region,
        *(
            (departement,)
            if departement
            else ()
        ),
    )

    rows = await fetch_all(
        f"""
        WITH diversite AS (
            SELECT
                lt.id AS lieu_id,

                COUNT(DISTINCT ast.categorie)
                    FILTER (
                        WHERE ast.nombre_total > 0
                    ) AS nb_fonctions

            FROM lieux_tournage lt

            JOIN films f
              ON f.id = lt.film_id

            LEFT JOIN amenity_stats ast
              ON ast.lieu_tournage_id = lt.id

            WHERE f.region = %s
              AND f.statut = 'publie'
              {condition_dep}

            GROUP BY lt.id
        )

        SELECT
            nb_fonctions,
            COUNT(*) AS n

        FROM diversite

        GROUP BY nb_fonctions

        ORDER BY nb_fonctions
        """,
        params,
    )

    if not rows:
        return {
            "n": 0,
            "moyenne": None,
            "mediane": None,
            "q1": None,
            "q3": None,
            "iqr": None,
            "ecart_type": None,
            "p90": None,
            "cv_pct": None,
            "distribution": [],
        }

    valeurs: list[float] = []

    distribution = []

    for row in rows:

        nb = _safe_int(
            row.get("nb_fonctions")
        )

        n = _safe_int(
            row.get("n")
        )

        if nb is None or n is None or n <= 0:
            continue

        distribution.append(
            {
                "nb_fonctions": nb,
                "n_lieux": n,
            }
        )

        valeurs.extend(
            [float(nb)] * n
        )

    stats = _stats_python(
        valeurs
    )

    return {
        "n": len(valeurs),

        "moyenne": stats[
            "moyenne"
        ],

        "mediane": stats[
            "mediane"
        ],

        "q1": stats[
            "q1"
        ],

        "q3": stats[
            "q3"
        ],

        "iqr": stats[
            "iqr"
        ],

        "ecart_type": stats[
            "ecart_type"
        ],

        "p90": stats[
            "p90"
        ],

        "cv_pct": stats[
            "cv_pct"
        ],

        "distribution": distribution,
    }


# ============================================================================
# OBSERVATOIRE STATISTIQUE
# ============================================================================

async def construire_observatoire_statistique(
    region: str = "Occitanie",
) -> dict[str, Any]:
    """
    Construit le dictionnaire statistique destiné à :

        /api/analyse/observatoire

    Les catégories sont découvertes dynamiquement dans amenity_stats.
    """

    categories_rows = await fetch_all(
        """
        SELECT DISTINCT
            ast.categorie

        FROM amenity_stats ast

        JOIN lieux_tournage lt
          ON lt.id = ast.lieu_tournage_id

        JOIN films f
          ON f.id = lt.film_id

        WHERE f.region = %s
          AND f.statut = 'publie'

        ORDER BY ast.categorie
        """,
        (region,),
    )

    categories = [
        str(row["categorie"]).strip().lower()
        for row in categories_rows
        if row.get("categorie")
    ]

    departements_rows = await fetch_all(
        """
        SELECT DISTINCT
            lt.departement

        FROM lieux_tournage lt

        JOIN films f
          ON f.id = lt.film_id

        WHERE f.region = %s
          AND f.statut = 'publie'
          AND lt.departement IS NOT NULL

        ORDER BY lt.departement
        """,
        (region,),
    )

    departements = [
        str(row["departement"]).strip()
        for row in departements_rows
        if row.get("departement")
    ]

    # ------------------------------------------------------------------
    # Statistiques régionales
    # ------------------------------------------------------------------

    equipements: dict[str, Any] = {}

    for categorie in categories:

        equipements[
            categorie
        ] = await _stats_categorie(
            region,
            categorie,
        )

    diversite_regionale = (
        await _diversite_fonctionnelle(
            region
        )
    )

    # ------------------------------------------------------------------
    # Tableau départemental
    # ------------------------------------------------------------------

    categorie_reference = (
        "hebergement"
        if "hebergement" in categories
        else (
            categories[0]
            if categories
            else None
        )
    )

    tableau_departemental = []

    if categorie_reference:

        ref_regionale = equipements.get(
            categorie_reference,
            {},
        )

        ref_off = ref_regionale.get(
            "nombre",
            {},
        )

        for dep in departements:

            stats_dep = await _stats_categorie(
                region,
                categorie_reference,
                departement=dep,
            )

            diversite_dep = (
                await _diversite_fonctionnelle(
                    region,
                    departement=dep,
                )
            )

            off = stats_dep.get(
                "nombre",
                {},
            )

            prox = stats_dep.get(
                "distance_plus_proche_m",
                {},
            )

            def _ecart(
                dep_val: Any,
                ref_val: Any,
            ) -> tuple[
                float | None,
                float | None,
            ]:

                dep_val = _safe_float(
                    dep_val
                )

                ref_val = _safe_float(
                    ref_val
                )

                if (
                    dep_val is None
                    or ref_val is None
                ):
                    return None, None

                absolu = round(
                    dep_val - ref_val,
                    2,
                )

                relatif = (
                    round(
                        (
                            dep_val - ref_val
                        )
                        / ref_val
                        * 100.0,
                        1,
                    )
                    if ref_val != 0
                    else None
                )

                return (
                    absolu,
                    relatif,
                )

            (
                ecart_off_absolu,
                ecart_off_relatif,
            ) = _ecart(
                off.get("moyenne"),
                ref_off.get("moyenne"),
            )

            tableau_departemental.append(
                {
                    "departement": dep,

                    "n_lieux": stats_dep.get(
                        "n",
                        0,
                    ),

                    "categorie_reference": (
                        categorie_reference
                    ),

                    "moyenne": off.get(
                        "moyenne"
                    ),

                    "mediane": off.get(
                        "mediane"
                    ),

                    "ecart_type": off.get(
                        "ecart_type"
                    ),

                    "cv_pct": off.get(
                        "cv_pct"
                    ),

                    "distance_mediane_m": prox.get(
                        "mediane"
                    ),

                    "distance_p90_m": prox.get(
                        "p90"
                    ),

                    "a_500m_pct": (
                        stats_dep
                        .get("proximite", {})
                        .get("a_500m_pct")
                    ),

                    "sans_equipement_n": (
                        stats_dep
                        .get("carence", {})
                        .get("sans_equipement_n")
                    ),

                    "diversite_moyenne": (
                        diversite_dep.get(
                            "moyenne"
                        )
                    ),

                    "ecart_absolu_regional": (
                        ecart_off_absolu
                    ),

                    "ecart_relatif_regional_pct": (
                        ecart_off_relatif
                    ),

                    "position_regionale": (
                        None
                        if ecart_off_relatif is None
                        else (
                            "au-dessus de la référence régionale"
                            if ecart_off_relatif > 10
                            else (
                                "en-dessous de la référence régionale"
                                if ecart_off_relatif < -10
                                else "proche de la référence régionale"
                            )
                        )
                    ),
                }
            )

    return {
        "region": region,

        "categories_disponibles": categories,

        "equipements": equipements,

        "diversite_fonctionnelle": (
            diversite_regionale
        ),

        "categorie_reference_tableau": (
            categorie_reference
        ),

        "departements": (
            tableau_departemental
        ),

        "avertissements_methodologiques": [
            (
                "Les statistiques « distance_moyenne_10_plus_proches_m » "
                "portent uniquement sur les 10 équipements les plus proches "
                "de chaque lieu lorsqu'une telle donnée est disponible."
            ),

            (
                "Le champ « nombre » utilise le périmètre de recherche "
                "réel de chaque catégorie DATAtourisme. Il ne doit donc "
                "pas être utilisé pour comparer directement deux catégories "
                "dont les rayons de recherche diffèrent."
            ),

            (
                "Le bloc « nombre_rayon_standard_1km » utilise un rayon "
                "fixe de 1 km et constitue le bloc privilégié pour comparer "
                "les volumes observés entre catégories."
            ),

            (
                "Les pourcentages de proximité (250 m, 500 m, 1 km, 2 km, "
                "5 km) utilisent uniquement les lieux pour lesquels une "
                "distance réelle est disponible."
            ),

            (
                "La position régionale est descriptive. L'écart de ±10 % "
                "est un repère de lecture et ne constitue pas un seuil "
                "scientifique universel."
            ),

            (
                "L'écart-type est calculé comme écart-type population "
                "(stddev_pop) puisque l'observatoire décrit les lieux "
                "effectivement présents dans sa base."
            ),

            (
                "Les indicateurs de priorité et d'opportunité sont des "
                "heuristiques de lecture territoriale. Ils ne constituent "
                "pas une estimation de l'impact économique du tourisme."
            ),
        ],
    }


# ============================================================================
# CONSTRUCTION PRINCIPALE
# ============================================================================

async def construire_indicateurs_cinetourisme(
    region: str = "Occitanie",
) -> dict[str, Any]:
    """
    Construit l'ensemble des indicateurs de l'observatoire.

    Contrat principal :

        {
            region,
            totaux,
            departements,
            priorisation_departementale,
            accessibilite,
            equipements,
            isochrones,
            films_notables,
            lieux_potentiel,
            completude,
            metriques
        }
    """

    region = str(
        region or "Occitanie"
    ).strip()

    # =========================================================================
    # 1. LIEUX PUBLIES
    # =========================================================================

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

    lieu_departement_map = {
        int(row["id"]): (
            str(row["departement"]).strip()
            if row.get("departement")
            else None
        )
        for row in lieux
        if row.get("id") is not None
    }

    # =========================================================================
    # 2. CAS VIDE
    # =========================================================================

    if not lieux:

        return {
            "region": region,

            "totaux": {
                "films": 0,
                "lieux": 0,
                "departements": 0,
            },

            "departements": [],

            "priorisation_departementale": [],

            "accessibilite": {
                "pret_15_pct": None,
                "pret_30_pct": None,
                "voiture_15_pct": None,
                "voiture_30_pct": None,
                "pied_15_pct": None,
                "pied_30_pct": None,
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
                "couverture_voiture_pct": None,
                "couverture_pied_pct": None,
                "minutes_disponibles": [],
                "couverture_par_minutes": [],
                "ratio_surface_voiture_marche_15": None,
                "surface_moyenne_voiture_15_km2": None,
                "surface_moyenne_marche_15_km2": None,
                "surface_moyenne_voiture_15_commun_km2": None,
                "surface_moyenne_marche_15_commun_km2": None,
                "nb_lieux_surface_15_commun": 0,
                "surface_voiture_15_par_departement": [],
                "lieu_plus_enclave": None,
                "lieu_plus_dense": None,
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

    # =========================================================================
    # 3. EQUIPEMENTS
    # =========================================================================

    equipements = await _charger_equipements(
        lieu_ids
    )

    # =========================================================================
    # 4. DUREES ROUTIERES
    # =========================================================================

    durees_routieres = (
        await _charger_durees_routieres(
            lieu_ids
        )
    )

    # =========================================================================
    # 5. ISOCHRONES
    # =========================================================================

    isochrones = await _charger_isochrones(
        region,
        lieu_ids,
    )

    # =========================================================================
    # 6. TOTAUX
    # =========================================================================

    film_ids = {
        _safe_int(row.get("film_id"))
        for row in lieux
        if _safe_int(row.get("film_id"))
        is not None
    }

    departements = {
        str(row.get("departement")).strip()
        for row in lieux
        if (
            row.get("departement")
            and str(row.get("departement")).strip()
        )
    }

    totaux = {
        "films": len(film_ids),
        "lieux": len(lieux),
        "departements": len(departements),
    }

    total_lieux = len(lieux)

    # =========================================================================
    # 7. DEPARTEMENTS
    # =========================================================================

    stats_departements = defaultdict(
        lambda: {
            "lieux": 0,
            "films": set(),
        }
    )

    for row in lieux:

        dep = row.get(
            "departement"
        )

        if not dep:
            continue

        dep = str(dep).strip()

        stats_departements[
            dep
        ]["lieux"] += 1

        film_id = _safe_int(
            row.get("film_id")
        )

        if film_id is not None:
            stats_departements[
                dep
            ]["films"].add(
                film_id
            )

    departements_result = []

    for dep, values in stats_departements.items():

        nb_lieux = int(
            values["lieux"]
        )

        nb_films = len(
            values["films"]
        )

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

    # =========================================================================
    # 8. EQUIPEMENTS REGIONAUX
    # =========================================================================

    hebergement_counts = []
    restaurant_counts = []

    hebergement_distances = []
    restaurant_distances = []

    hebergement_presence = 0
    restaurant_presence = 0

    hebergement_500m = 0
    restaurant_500m = 0

    amenity_lieux = 0

    # Ventilation départementale
    dep_equip_acc = defaultdict(
        lambda: {
            "heb": [],
            "rest": [],
            "heb_presence": 0,
            "rest_presence": 0,
            "heb_500m": 0,
            "rest_500m": 0,
            "total": 0,
        }
    )

    for lieu_id in lieu_ids:

        data = equipements.get(
            lieu_id,
            {},
        )

        hebergement = data.get(
            "hebergement"
        )

        restaurant = data.get(
            "restaurant"
        )

        dep = lieu_departement_map.get(
            lieu_id
        )

        if dep:
            dep_equip_acc[
                dep
            ]["total"] += 1

        if (
            hebergement is not None
            or restaurant is not None
        ):
            amenity_lieux += 1

        # ---------------------------------------------------------------------
        # Hébergement
        # ---------------------------------------------------------------------

        if hebergement is not None:

            nombre = _safe_int(
                hebergement.get(
                    "nombre_total"
                )
            )

            if nombre is not None:

                hebergement_counts.append(
                    float(nombre)
                )

                if nombre > 0:
                    hebergement_presence += 1

                if dep:

                    dep_equip_acc[
                        dep
                    ]["heb"].append(
                        float(nombre)
                    )

                    if nombre > 0:
                        dep_equip_acc[
                            dep
                        ]["heb_presence"] += 1

            nombre_500 = _safe_int(
                hebergement.get(
                    "nombre_500m"
                )
            )

            if (
                nombre_500 is not None
                and nombre_500 > 0
            ):

                hebergement_500m += 1

                if dep:
                    dep_equip_acc[
                        dep
                    ]["heb_500m"] += 1

            distance = _safe_float(
                hebergement.get(
                    "distance_min_m"
                )
            )

            if distance is not None:
                hebergement_distances.append(
                    distance
                )

        # ---------------------------------------------------------------------
        # Restaurant
        # ---------------------------------------------------------------------

        if restaurant is not None:

            nombre = _safe_int(
                restaurant.get(
                    "nombre_total"
                )
            )

            if nombre is not None:

                restaurant_counts.append(
                    float(nombre)
                )

                if nombre > 0:
                    restaurant_presence += 1

                if dep:

                    dep_equip_acc[
                        dep
                    ]["rest"].append(
                        float(nombre)
                    )

                    if nombre > 0:
                        dep_equip_acc[
                            dep
                        ]["rest_presence"] += 1

            nombre_500 = _safe_int(
                restaurant.get(
                    "nombre_500m"
                )
            )

            if (
                nombre_500 is not None
                and nombre_500 > 0
            ):

                restaurant_500m += 1

                if dep:
                    dep_equip_acc[
                        dep
                    ]["rest_500m"] += 1

            distance = _safe_float(
                restaurant.get(
                    "distance_min_m"
                )
            )

            if distance is not None:
                restaurant_distances.append(
                    distance
                )

    equipements_result = {
        "moy_hebergement": _round(
            (
                sum(hebergement_counts)
                / len(hebergement_counts)
            )
            if hebergement_counts
            else None,
            2,
        ),

        "moy_restaurant": _round(
            (
                sum(restaurant_counts)
                / len(restaurant_counts)
            )
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

    # =========================================================================
    # 9. ACCESSIBILITE — MODES SEPARES
    # =========================================================================

    voiture_disponible = 0
    pied_disponible = 0

    voiture_15 = 0
    voiture_30 = 0

    pied_15 = 0
    pied_30 = 0

    # Pour les indicateurs "pret_*" historiques :
    # ils restent des alias de la voiture lorsque la couverture voiture
    # est complète. Ils ne mélangent plus voiture et marche.
    for lieu_id in lieu_ids:

        route = durees_routieres.get(
            lieu_id
        )

        if not route:
            continue

        voiture = _safe_int(
            route.get(
                "duree_voiture_secondes"
            )
        )

        pied = _safe_int(
            route.get(
                "duree_pied_secondes"
            )
        )

        if voiture is not None:

            voiture_disponible += 1

            if voiture <= 15 * 60:
                voiture_15 += 1

            if voiture <= 30 * 60:
                voiture_30 += 1

        if pied is not None:

            pied_disponible += 1

            if pied <= 15 * 60:
                pied_15 += 1

            if pied <= 30 * 60:
                pied_30 += 1

    route_coverage_pct = _pct(
        len(
            [
                lieu_id
                for lieu_id in lieu_ids
                if durees_routieres.get(lieu_id)
            ]
        ),
        total_lieux,
    )

    voiture_15_pct = (
        _pct(
            voiture_15,
            total_lieux,
        )
        if voiture_disponible == total_lieux
        else None
    )

    voiture_30_pct = (
        _pct(
            voiture_30,
            total_lieux,
        )
        if voiture_disponible == total_lieux
        else None
    )

    pied_15_pct = (
        _pct(
            pied_15,
            total_lieux,
        )
        if pied_disponible == total_lieux
        else None
    )

    pied_30_pct = (
        _pct(
            pied_30,
            total_lieux,
        )
        if pied_disponible == total_lieux
        else None
    )

    # -------------------------------------------------------------------------
    # Compatibilité frontend
    #
    # IMPORTANT :
    # pret_* ne mélange plus les modes.
    # Ce sont désormais des alias historiques de la couverture voiture
    # lorsque la donnée voiture est complète.
    # -------------------------------------------------------------------------

    pret_15_pct = voiture_15_pct
    pret_30_pct = voiture_30_pct

    # Impossible de calculer honnêtement >45 min sans connaître la durée
    # réelle pour tous les lieux ou une donnée explicitement supérieure
    # à 45 min.
    isoles_45_pct = None

    accessibilite = {
        "pret_15_pct": pret_15_pct,
        "pret_30_pct": pret_30_pct,

        "voiture_15_pct": voiture_15_pct,
        "voiture_30_pct": voiture_30_pct,

        "pied_15_pct": pied_15_pct,
        "pied_30_pct": pied_30_pct,

        "isoles_45_pct": isoles_45_pct,

        "route_coverage_pct": route_coverage_pct,

        "voiture_coverage_pct": _pct(
            voiture_disponible,
            total_lieux,
        ),

        "pied_coverage_pct": _pct(
            pied_disponible,
            total_lieux,
        ),
    }

    # =========================================================================
    # 10. POPULARITE DEPARTEMENTALE
    # =========================================================================

    dep_popularite = defaultdict(list)

    for row in lieux:

        dep = row.get(
            "departement"
        )

        pop = _safe_float(
            row.get("popularite")
        )

        if dep and pop is not None:

            dep_popularite[
                str(dep).strip()
            ].append(pop)

    # =========================================================================
    # 11. SURFACE VOITURE 15 MIN PAR DEPARTEMENT
    # =========================================================================

    surface_par_dep = {
        item["departement"]: item[
            "surface_moyenne_15min_km2"
        ]
        for item in isochrones.get(
            "surface_voiture_15_par_departement",
            [],
        )
    }

    # =========================================================================
    # 12. REFERENCES REGIONALES
    # =========================================================================

    ref_moy_hebergement = (
        equipements_result.get(
            "moy_hebergement"
        )
    )

    ref_surface_15 = (
        isochrones.get(
            "surface_moyenne_voiture_15_km2"
        )
    )

    toutes_popularites_dep = [
        popularity
        for values in dep_popularite.values()
        for popularity in values
    ]

    max_popularite_globale = (
        max(toutes_popularites_dep)
        if toutes_popularites_dep
        else None
    )

    max_surface_15_globale = (
        max(surface_par_dep.values())
        if surface_par_dep
        else None
    )

    # =========================================================================
    # 13. ENRICHISSEMENT DEPARTEMENTAL
    # =========================================================================

    dep_access_acc = defaultdict(
        lambda: {
            "total": 0,
            "voiture": 0,
            "pied": 0,
            "voiture_15": 0,
            "voiture_30": 0,
            "pied_15": 0,
            "pied_30": 0,
        }
    )

    for lieu_id in lieu_ids:

        dep = lieu_departement_map.get(
            lieu_id
        )

        if not dep:
            continue

        dep_access_acc[
            dep
        ]["total"] += 1

        route = durees_routieres.get(
            lieu_id
        )

        if not route:
            continue

        voiture = _safe_int(
            route.get(
                "duree_voiture_secondes"
            )
        )

        pied = _safe_int(
            route.get(
                "duree_pied_secondes"
            )
        )

        if voiture is not None:

            dep_access_acc[
                dep
            ]["voiture"] += 1

            if voiture <= 15 * 60:
                dep_access_acc[
                    dep
                ]["voiture_15"] += 1

            if voiture <= 30 * 60:
                dep_access_acc[
                    dep
                ]["voiture_30"] += 1

        if pied is not None:

            dep_access_acc[
                dep
            ]["pied"] += 1

            if pied <= 15 * 60:
                dep_access_acc[
                    dep
                ]["pied_15"] += 1

            if pied <= 30 * 60:
                dep_access_acc[
                    dep
                ]["pied_30"] += 1

    # =========================================================================
    # 14. CALCUL DES INDICATEURS DEPARTEMENTAUX
    # =========================================================================

    for dep_item in departements_result:

        dep = dep_item[
            "departement"
        ]

        eq = dep_equip_acc.get(
            dep,
            {},
        )

        acc = dep_access_acc.get(
            dep,
            {
                "total": 0,
                "voiture": 0,
                "pied": 0,
                "voiture_15": 0,
                "voiture_30": 0,
                "pied_15": 0,
                "pied_30": 0,
            },
        )

        heb_values = eq.get(
            "heb",
            [],
        )

        rest_values = eq.get(
            "rest",
            [],
        )

        total_dep = eq.get(
            "total",
            0,
        )

        moy_heb = (
            round(
                sum(heb_values)
                / len(heb_values),
                2,
            )
            if heb_values
            else None
        )

        moy_rest = (
            round(
                sum(rest_values)
                / len(rest_values),
                2,
            )
            if rest_values
            else None
        )

        heb_presence_pct = (
            _pct(
                eq.get(
                    "heb_presence",
                    0,
                ),
                total_dep,
            )
            if total_dep
            else None
        )

        rest_presence_pct = (
            _pct(
                eq.get(
                    "rest_presence",
                    0,
                ),
                total_dep,
            )
            if total_dep
            else None
        )

        # ---------------------------------------------------------------------
        # Accessibilité voiture
        # ---------------------------------------------------------------------

        voiture_coverage_pct = _pct(
            acc["voiture"],
            acc["total"],
        )

        voiture_15_pct_dep = (
            _pct(
                acc["voiture_15"],
                acc["total"],
            )
            if (
                acc["total"] > 0
                and acc["voiture"]
                == acc["total"]
            )
            else None
        )

        voiture_30_pct_dep = (
            _pct(
                acc["voiture_30"],
                acc["total"],
            )
            if (
                acc["total"] > 0
                and acc["voiture"]
                == acc["total"]
            )
            else None
        )

        # ---------------------------------------------------------------------
        # Accessibilité piétonne
        # ---------------------------------------------------------------------

        pied_coverage_pct = _pct(
            acc["pied"],
            acc["total"],
        )

        pied_15_pct_dep = (
            _pct(
                acc["pied_15"],
                acc["total"],
            )
            if (
                acc["total"] > 0
                and acc["pied"]
                == acc["total"]
            )
            else None
        )

        pied_30_pct_dep = (
            _pct(
                acc["pied_30"],
                acc["total"],
            )
            if (
                acc["total"] > 0
                and acc["pied"]
                == acc["total"]
            )
            else None
        )

        surface_15 = surface_par_dep.get(
            dep
        )

        pop_list = dep_popularite.get(
            dep,
            [],
        )

        popularite_moyenne = (
            round(
                sum(pop_list)
                / len(pop_list),
                1,
            )
            if pop_list
            else None
        )

        # ---------------------------------------------------------------------
        # Score de priorité
        #
        # HEURISTIQUE :
        #
        #   45 % déficit d'offre
        #   35 % déficit de mobilité
        #   20 % présence cinématographique
        #
        # Ce n'est PAS un indicateur d'impact économique.
        # ---------------------------------------------------------------------

        composantes_priorite = []

        # Déficit d'offre
        if (
            heb_presence_pct is not None
            and rest_presence_pct is not None
        ):

            offre_moyenne_pct = (
                heb_presence_pct
                + rest_presence_pct
            ) / 2.0

            deficit_offre = (
                100.0
                - offre_moyenne_pct
            )

            composantes_priorite.append(
                (
                    "offre",
                    deficit_offre,
                    0.45,
                )
            )

        # Déficit de mobilité
        #
        # Une surface 15 min voiture plus élevée signifie davantage
        # de territoire couvert par le réseau routier depuis les lieux.
        # La composante de déficit est donc calculée inversement.
        if (
            surface_15 is not None
            and max_surface_15_globale is not None
            and max_surface_15_globale > 0
        ):

            accessibilite_surface_pct = min(
                100.0,
                surface_15
                / max_surface_15_globale
                * 100.0,
            )

            deficit_mobilite = (
                100.0
                - accessibilite_surface_pct
            )

            composantes_priorite.append(
                (
                    "mobilite",
                    deficit_mobilite,
                    0.35,
                )
            )

        # Présence cinématographique
        nb_lieux_dep = dep_item[
            "nb_lieux"
        ]

        if total_lieux > 0:

            presence_cinema = (
                nb_lieux_dep
                / total_lieux
                * 100.0
            )

            composantes_priorite.append(
                (
                    "cinema",
                    presence_cinema,
                    0.20,
                )
            )

        # ---------------------------------------------------------------------
        # Renormalisation des poids si une composante manque.
        # ---------------------------------------------------------------------

        if composantes_priorite:

            somme_poids = sum(
                poids
                for _, _, poids
                in composantes_priorite
            )

            score_investissement = round(
                sum(
                    valeur * poids
                    for _, valeur, poids
                    in composantes_priorite
                )
                / somme_poids,
                1,
            )

        else:
            score_investissement = None

        # ---------------------------------------------------------------------
        # Benchmark équipement
        # ---------------------------------------------------------------------

        if (
            moy_heb is not None
            and ref_moy_hebergement is not None
        ):

            if moy_heb > ref_moy_hebergement:
                benchmark_hebergement = (
                    "au-dessus de la moyenne régionale"
                )

            elif moy_heb < ref_moy_hebergement:
                benchmark_hebergement = (
                    "en-dessous de la moyenne régionale"
                )

            else:
                benchmark_hebergement = (
                    "dans la moyenne régionale"
                )

        else:
            benchmark_hebergement = (
                "non comparable"
            )

        # ---------------------------------------------------------------------
        # Benchmark surface
        # ---------------------------------------------------------------------

        if (
            surface_15 is not None
            and ref_surface_15 is not None
        ):

            if surface_15 > ref_surface_15:
                benchmark_enclavement = (
                    "surface accessible supérieure à la moyenne régionale"
                )

            elif surface_15 < ref_surface_15:
                benchmark_enclavement = (
                    "surface accessible inférieure à la moyenne régionale"
                )

            else:
                benchmark_enclavement = (
                    "dans la moyenne régionale"
                )

        else:
            benchmark_enclavement = (
                "non comparable"
            )

        dep_item.update(
            {
                "moy_hebergement": moy_heb,

                "moy_restaurant": moy_rest,

                "hebergement_presence_pct": (
                    heb_presence_pct
                ),

                "restaurant_presence_pct": (
                    rest_presence_pct
                ),

                # Compatibilité historique :
                # ces champs correspondent maintenant explicitement
                # à la voiture et non à min(voiture, marche).
                "pret_15_pct": (
                    voiture_15_pct_dep
                ),

                "pret_30_pct": (
                    voiture_30_pct_dep
                ),

                "voiture_15_pct": (
                    voiture_15_pct_dep
                ),

                "voiture_30_pct": (
                    voiture_30_pct_dep
                ),

                "pied_15_pct": (
                    pied_15_pct_dep
                ),

                "pied_30_pct": (
                    pied_30_pct_dep
                ),

                "voiture_coverage_pct": (
                    voiture_coverage_pct
                ),

                "pied_coverage_pct": (
                    pied_coverage_pct
                ),

                "surface_moyenne_15min_km2": (
                    surface_15
                ),

                "popularite_moyenne": (
                    popularite_moyenne
                ),

                "score_investissement": (
                    score_investissement
                ),

                "benchmark_hebergement": (
                    benchmark_hebergement
                ),

                "benchmark_enclavement": (
                    benchmark_enclavement
                ),
            }
        )

    priorisation_departementale = sorted(
        [
            item
            for item in departements_result
            if item.get(
                "score_investissement"
            ) is not None
        ],
        key=lambda item: item[
            "score_investissement"
        ],
        reverse=True,
    )

    # =========================================================================
    # 15. FILMOGRAPHIE
    # =========================================================================

    films_data = {}

    for row in lieux:

        film_id = _safe_int(
            row.get("film_id")
        )

        if film_id is None:
            continue

        if film_id not in films_data:

            films_data[film_id] = {
                "film_id": film_id,
                "titre": row.get(
                    "titre"
                ),
                "annee": _safe_int(
                    row.get("annee")
                ),
                "media_type": row.get(
                    "media_type"
                ),
                "popularite": _safe_float(
                    row.get("popularite")
                ),
                "lieux": 0,
                "departements": set(),
            }

        data = films_data[
            film_id
        ]

        data["lieux"] += 1

        dep = row.get(
            "departement"
        )

        if dep:
            data[
                "departements"
            ].add(
                str(dep).strip()
            )

    films_notables = []

    for data in films_data.values():

        films_notables.append(
            {
                "film_id": data[
                    "film_id"
                ],

                "titre": data[
                    "titre"
                ],

                "annee": data[
                    "annee"
                ],

                "media_type": data[
                    "media_type"
                ],

                "popularite": data[
                    "popularite"
                ],

                "nb_lieux": data[
                    "lieux"
                ],

                "nb_departements": len(
                    data[
                        "departements"
                    ]
                ),

                "departements": sorted(
                    data[
                        "departements"
                    ]
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

    films_notables = films_notables[
        :20
    ]

    # =========================================================================
    # 16. POTENTIEL DES LIEUX
    # =========================================================================

    popularites = [
        _safe_float(
            row.get("popularite")
        )
        for row in lieux
        if _safe_float(
            row.get("popularite")
        ) is not None
    ]

    max_popularite = (
        max(popularites)
        if popularites
        else None
    )

    lieux_potentiel = []

    for row in lieux:

        lieu_id = _safe_int(
            row.get("id")
        )

        if lieu_id is None:
            continue

        film_id = _safe_int(
            row.get("film_id")
        )

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
                hebergement.get(
                    "nombre_total"
                )
            )
            if hebergement
            else 0
        )

        nb_restaurants = (
            _safe_int(
                restaurant.get(
                    "nombre_total"
                )
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

        # ---------------------------------------------------------------------
        # Maturité touristique observée
        # ---------------------------------------------------------------------

        maturite = (
            (
                int(
                    presence_hebergement
                )
                + int(
                    presence_restaurant
                )
            )
            / 2.0
            * 100.0
        )

        # ---------------------------------------------------------------------
        # Score popularité
        # ---------------------------------------------------------------------

        if (
            popularite is not None
            and max_popularite is not None
            and max_popularite > 0
        ):

            popularite_score = (
                popularite
                / max_popularite
                * 100.0
            )

        else:
            popularite_score = None

        # ---------------------------------------------------------------------
        # Score opportunité
        #
        # Heuristique descriptive.
        # ---------------------------------------------------------------------

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

                "titre": row.get(
                    "titre"
                ),

                "commune": row.get(
                    "commune"
                ),

                "departement": row.get(
                    "departement"
                ),

                "popularite": popularite,

                "hebergement": (
                    presence_hebergement
                ),

                "restaurant": (
                    presence_restaurant
                ),

                "nb_hebergements": (
                    nb_hebergements
                ),

                "nb_restaurants": (
                    nb_restaurants
                ),

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

                "maturite_touristique_pct": (
                    round(
                        maturite,
                        1,
                    )
                ),

                "popularite_score": (
                    round(
                        popularite_score,
                        1,
                    )
                    if popularite_score is not None
                    else None
                ),

                # Compatibilité frontend historique.
                "preparation": (
                    round(
                        maturite,
                        1,
                    )
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

    lieux_potentiel = lieux_potentiel[
        :50
    ]

    # =========================================================================
    # 17. COMPLETUDE
    # =========================================================================

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
            _safe_float(
                row.get("popularite")
            )
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

    # =========================================================================
    # 18. METRIQUES COMPLEMENTAIRES
    # =========================================================================

    parts_departements = [
        float(
            item["nb_lieux"]
        )
        / total_lieux
        for item in departements_result
        if total_lieux > 0
    ]

    concentration_hhi = _hhi(
        parts_departements
    )

    top3_lieux = sum(
        item["nb_lieux"]
        for item in departements_result[
            :3
        ]
    )

    concentration_top3_pct = _pct(
        top3_lieux,
        total_lieux,
    )

    distance_moy_hebergement_m = (
        sum(
            hebergement_distances
        )
        / len(
            hebergement_distances
        )
        if hebergement_distances
        else None
    )

    distance_moy_restaurant_m = (
        sum(
            restaurant_distances
        )
        / len(
            restaurant_distances
        )
        if restaurant_distances
        else None
    )

    mediane_hebergement = (
        _median(
            hebergement_distances
        )
        if hebergement_distances
        else None
    )

    mediane_restaurant = (
        _median(
            restaurant_distances
        )
        if restaurant_distances
        else None
    )

    lieux_avec_isochrones = (
        sum(
            1
            for lieu_id in lieu_ids
            if lieu_id in (
                # Reconstitution à partir du taux disponible.
                # Le taux lui-même provient exclusivement des lieux
                # réellement présents dans isochrones.
                set()
            )
        )
        if False
        else (
            round(
                total_lieux
                * (
                    isochrones.get(
                        "couverture_pct"
                    )
                    or 0
                )
                / 100.0
            )
            if isochrones.get(
                "couverture_pct"
            ) is not None
            else None
        )
    )

    metriques = {
        "concentration_hhi": (
            concentration_hhi
        ),

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
                mediane_hebergement,
                1,
            )
            if mediane_hebergement is not None
            else None
        ),

        "distance_mediane_restaurant_m": (
            round(
                mediane_restaurant,
                1,
            )
            if mediane_restaurant is not None
            else None
        ),

        "lieux_avec_route": (
            len(
                [
                    lieu_id
                    for lieu_id in lieu_ids
                    if durees_routieres.get(
                        lieu_id
                    )
                ]
            )
        ),

        "lieux_avec_isochrones": (
            lieux_avec_isochrones
        ),

        "lieux_avec_equipements": (
            amenity_lieux
        ),

        "couverture_voiture_pct": (
            isochrones.get(
                "couverture_voiture_pct"
            )
        ),

        "couverture_pied_pct": (
            isochrones.get(
                "couverture_pied_pct"
            )
        ),
    }

    # =========================================================================
    # 19. RESULTAT FINAL
    # =========================================================================

    return {
        "region": region,

        "totaux": totaux,

        "departements": (
            departements_result
        ),

        "priorisation_departementale": (
            priorisation_departementale
        ),

        "accessibilite": (
            accessibilite
        ),

        "equipements": (
            equipements_result
        ),

        "isochrones": (
            isochrones
        ),

        "films_notables": (
            films_notables
        ),

        "lieux_potentiel": (
            lieux_potentiel
        ),

        "completude": (
            completude
        ),

        "metriques": (
            metriques
        ),
    }