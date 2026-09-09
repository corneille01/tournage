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

import json
import math
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


def _aire_km2_geojson(geometry: Any) -> float | None:
    """
    Surface approximative (km²) d'une géométrie GeoJSON d'isochrone
    (Polygon ou MultiPolygon), par projection équirectangulaire locale
    (centrée sur la latitude du premier point) puis formule du lacet.

    Pas de dépendance SIG lourde (shapely n'est pas dans
    requirements.txt) — l'imprécision de cette approximation est
    négligeable à l'échelle d'une isochrone de quelques dizaines de km,
    largement suffisante pour un ratio comparatif voiture/marche.
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

    polygones = coords if geom_type == "MultiPolygon" else [coords]

    def _aire_anneau(anneau: list) -> float:
        if len(anneau) < 3:
            return 0.0

        lat_ref = anneau[0][1]
        m_par_deg_lat = 111_320.0
        m_par_deg_lon = 111_320.0 * math.cos(math.radians(lat_ref))

        pts = [
            (lon * m_par_deg_lon, lat * m_par_deg_lat)
            for lon, lat in anneau
        ]

        aire = 0.0
        for i in range(len(pts) - 1):
            x1, y1 = pts[i]
            x2, y2 = pts[i + 1]
            aire += x1 * y2 - x2 * y1

        return abs(aire) / 2.0

    total_m2 = 0.0
    for polygone in polygones:
        if not polygone:
            continue

        aire_polygone = _aire_anneau(polygone[0])
        for trou in polygone[1:]:
            aire_polygone -= _aire_anneau(trou)

        total_m2 += max(aire_polygone, 0.0)

    return round(total_m2 / 1_000_000, 3)


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

    # lieu -> mode -> minutes disponibles
    presence: dict[int, dict[str, set[int]]] = defaultdict(
        lambda: defaultdict(set)
    )

    dates: list[Any] = []

    # Surfaces (km²) des isochrones à 15 min, par mode — c'est ce qui
    # permet le ratio d'emprise spatiale voiture/marche (section
    # "Mobilité") : à durée égale, plus la surface accessible en
    # voiture est grande devant celle accessible à pied, plus le
    # territoire dépend de la voiture pour être exploré (signal
    # d'enclavement, pas juste une curiosité géométrique).
    aires_15min: dict[str, list[float]] = {"driving-car": [], "foot-walking": []}
    aires_lieux_15min_voiture: list[dict[str, Any]] = []
    aires_par_departement: dict[str, list[float]] = defaultdict(list)

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

        if minutes == 15:
            aire = _aire_km2_geojson(row.get("geometry_geojson"))

            if aire is not None and aire > 0:
                aires_15min[mode].append(aire)

                if mode == "driving-car":
                    aires_lieux_15min_voiture.append({
                        "lieu_id": lieu_id,
                        "nom": row.get("lieu_nom"),
                        "departement": row.get("lieu_departement"),
                        "aire_km2": aire,
                    })
                    dep = row.get("lieu_departement")
                    if dep:
                        aires_par_departement[str(dep).strip()].append(aire)

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

    # ── Ratio d'emprise spatiale voiture / marche à 15 min ──
    # Moyenne des surfaces sur tous les lieux disposant des DEUX modes
    # à 15 min (comparaison à territoire égal, pas de biais si un
    # lieu n'a que la voiture calculée et un autre que la marche).
    surface_moyenne_voiture_15 = (
        round(sum(aires_15min["driving-car"]) / len(aires_15min["driving-car"]), 2)
        if aires_15min["driving-car"] else None
    )
    surface_moyenne_marche_15 = (
        round(sum(aires_15min["foot-walking"]) / len(aires_15min["foot-walking"]), 2)
        if aires_15min["foot-walking"] else None
    )
    ratio_surface_voiture_marche_15 = (
        round(surface_moyenne_voiture_15 / surface_moyenne_marche_15, 1)
        if surface_moyenne_voiture_15 and surface_moyenne_marche_15
        else None
    )

    # Lieu le plus enclavé (grande surface à parcourir en voiture pour
    # 15 min = réseau routier lâche) et le plus dense (petite surface
    # = réseau routier serré, contexte urbain).
    lieu_plus_enclave = None
    lieu_plus_dense = None
    if aires_lieux_15min_voiture:
        lieu_plus_enclave = max(aires_lieux_15min_voiture, key=lambda l: l["aire_km2"])
        lieu_plus_dense = min(aires_lieux_15min_voiture, key=lambda l: l["aire_km2"])

    surface_voiture_15_par_departement = [
        {
            "departement": dep,
            "surface_moyenne_15min_km2": round(sum(aires) / len(aires), 1),
            "nb_lieux_calcules": len(aires),
        }
        for dep, aires in aires_par_departement.items()
    ]
    surface_voiture_15_par_departement.sort(key=lambda d: d["surface_moyenne_15min_km2"], reverse=True)

    return {
        "couverture_pct": couverture_pct,
        "minutes_disponibles": minutes_disponibles,
        "couverture_par_minutes": couverture_par_minutes,
        "ratio_surface_voiture_marche_15": ratio_surface_voiture_marche_15,
        "surface_moyenne_voiture_15_km2": surface_moyenne_voiture_15,
        "surface_moyenne_marche_15_km2": surface_moyenne_marche_15,
        "surface_voiture_15_par_departement": surface_voiture_15_par_departement,
        "lieu_plus_enclave": lieu_plus_enclave,
        "lieu_plus_dense": lieu_plus_dense,
        "derniere_date": _format_date(derniere_date),
    }


# ---------------------------------------------------------------------------
# Observatoire statistique — dictionnaire complet (OFF/PROX/LAC/DIV/DIS)
#
# Convention statistique (documentée, cf. audit) :
#   - moyenne  : avg()
#   - médiane / Q1 / Q3 / P90 : percentile_cont() — interpolation continue
#   - écart-type : stddev_pop() — on décrit LA POPULATION des lieux de
#     l'observatoire, pas un échantillon aléatoire d'une population plus
#     large, donc population et non échantillon (stddev_samp aurait été
#     le mauvais choix ici).
#   - CV = écart-type / moyenne × 100, NULL si moyenne = 0 (jamais 0
#     silencieusement, jamais une exception qui casse tout l'endpoint).
#
# N = nombre de LIEUX disposant de la donnée nécessaire (pas de films).
# Toute valeur non calculable reste None — jamais remplacée par 0.
# ---------------------------------------------------------------------------

def _bundle_percentiles(row: dict, prefixe: str) -> dict[str, Any]:
    """
    Construit le bloc {moyenne, mediane, q1, q3, iqr, ecart_type, p90, cv_pct}
    à partir d'une ligne SQL contenant déjà les agrégats {prefixe}_moyenne,
    {prefixe}_mediane, etc. (calculés côté PostgreSQL par percentile_cont
    et stddev_pop — jamais recalculés côté Python sur un échantillon
    partiel, cf. section 31/32 de la spec).
    """

    moyenne = _safe_float(row.get(f"{prefixe}_moyenne"))
    mediane = _safe_float(row.get(f"{prefixe}_mediane"))
    q1 = _safe_float(row.get(f"{prefixe}_q1"))
    q3 = _safe_float(row.get(f"{prefixe}_q3"))
    ecart_type = _safe_float(row.get(f"{prefixe}_ecart_type"))
    p90 = _safe_float(row.get(f"{prefixe}_p90"))

    iqr = round(q3 - q1, 2) if (q1 is not None and q3 is not None) else None

    cv_pct = (
        round(ecart_type / moyenne * 100, 1)
        if (ecart_type is not None and moyenne not in (None, 0))
        else None
    )

    return {
        "moyenne": round(moyenne, 2) if moyenne is not None else None,
        "mediane": round(mediane, 2) if mediane is not None else None,
        "q1": round(q1, 2) if q1 is not None else None,
        "q3": round(q3, 2) if q3 is not None else None,
        "iqr": iqr,
        "ecart_type": round(ecart_type, 2) if ecart_type is not None else None,
        "p90": round(p90, 2) if p90 is not None else None,
        "cv_pct": cv_pct,
    }


async def _stats_categorie(
    region: str,
    categorie: str,
    departement: str | None = None,
) -> dict[str, Any]:
    """
    Bloc statistique complet pour UNE catégorie DATAtourisme, à l'échelle
    régionale (departement=None) ou pour un seul département.

    Calcule tout en une requête SQL agrégée (percentile_cont/stddev_pop
    côté PostgreSQL) : aucune boucle Python par lieu, cf. section 31 de
    la spec (performance).
    """

    condition_dep = "AND lt.departement = %s" if departement else ""
    params: tuple = (categorie, region) + ((departement,) if departement else ())

    row = await fetch_all(
        f"""
        SELECT
            COUNT(ast.id) AS n,

            AVG(ast.nombre_total) AS off_moyenne,
            percentile_cont(0.5) WITHIN GROUP (ORDER BY ast.nombre_total) AS off_mediane,
            percentile_cont(0.25) WITHIN GROUP (ORDER BY ast.nombre_total) AS off_q1,
            percentile_cont(0.75) WITHIN GROUP (ORDER BY ast.nombre_total) AS off_q3,
            stddev_pop(ast.nombre_total) AS off_ecart_type,
            percentile_cont(0.9) WITHIN GROUP (ORDER BY ast.nombre_total) AS off_p90,

            COUNT(ast.distance_min_m) AS n_distance,
            AVG(ast.distance_min_m) AS prox_moyenne,
            percentile_cont(0.5) WITHIN GROUP (ORDER BY ast.distance_min_m) AS prox_mediane,
            percentile_cont(0.25) WITHIN GROUP (ORDER BY ast.distance_min_m) AS prox_q1,
            percentile_cont(0.75) WITHIN GROUP (ORDER BY ast.distance_min_m) AS prox_q3,
            stddev_pop(ast.distance_min_m) AS prox_ecart_type,
            percentile_cont(0.9) WITHIN GROUP (ORDER BY ast.distance_min_m) AS prox_p90,

            AVG(ast.distance_moy_top10_m) AS top10_moyenne,
            percentile_cont(0.5) WITHIN GROUP (ORDER BY ast.distance_moy_top10_m) AS top10_mediane,
            stddev_pop(ast.distance_moy_top10_m) AS top10_ecart_type,

            COUNT(*) FILTER (WHERE ast.distance_min_m <= 250)  AS prox_250,
            COUNT(*) FILTER (WHERE ast.distance_min_m <= 500)  AS prox_500,
            COUNT(*) FILTER (WHERE ast.distance_min_m <= 1000) AS prox_1000,
            COUNT(*) FILTER (WHERE ast.distance_min_m <= 2000) AS prox_2000,
            COUNT(*) FILTER (WHERE ast.distance_min_m <= 5000) AS prox_5000,

            COUNT(*) FILTER (WHERE ast.nombre_total = 0) AS lac_sans_equipement,
            COUNT(*) FILTER (WHERE ast.distance_min_m > 500)  AS lac_500,
            COUNT(*) FILTER (WHERE ast.distance_min_m > 1000) AS lac_1000,
            COUNT(*) FILTER (WHERE ast.distance_min_m > 2000) AS lac_2000,
            COUNT(*) FILTER (WHERE ast.distance_min_m > 5000) AS lac_5000

        FROM lieux_tournage lt
        JOIN films f ON f.id = lt.film_id
        LEFT JOIN amenity_stats ast
               ON ast.lieu_tournage_id = lt.id
              AND ast.categorie = %s
        WHERE f.region = %s
          AND f.statut = 'publie'
          {condition_dep}
        """,
        params,
    )

    if not row:
        return {}

    r = row[0]
    n = _safe_int(r.get("n")) or 0

    def _pct_n(cle: str) -> float | None:
        valeur = _safe_int(r.get(cle))
        return round(valeur / n * 100, 1) if (n > 0 and valeur is not None) else None

    return {
        "n": n,
        "nombre": _bundle_percentiles(r, "off"),
        "distance_plus_proche_m": _bundle_percentiles(r, "prox"),
        # Rappel explicite : porte sur les 10 objets les plus proches
        # uniquement — jamais sur l'ensemble de la catégorie (cf. section 20).
        "distance_moyenne_10_plus_proches_m": _bundle_percentiles(r, "top10"),
        "proximite": {
            "a_250m_pct": _pct_n("prox_250"),
            "a_500m_pct": _pct_n("prox_500"),
            "a_1km_pct": _pct_n("prox_1000"),
            "a_2km_pct": _pct_n("prox_2000"),
            "a_5km_pct": _pct_n("prox_5000"),
        },
        "carence": {
            "sans_equipement_n": _safe_int(r.get("lac_sans_equipement")),
            "sans_equipement_pct": _pct_n("lac_sans_equipement"),
            "au_dela_500m_n": _safe_int(r.get("lac_500")),
            "au_dela_1km_n": _safe_int(r.get("lac_1000")),
            "au_dela_2km_n": _safe_int(r.get("lac_2000")),
            "au_dela_5km_n": _safe_int(r.get("lac_5000")),
        },
    }


async def _diversite_fonctionnelle(
    region: str,
    departement: str | None = None,
) -> dict[str, Any]:
    """
    Nombre de fonctions DATAtourisme (catégories avec au moins un
    équipement observé) réellement présentes par lieu — cf. Groupe F.

    Pas de seuil X arbitraire (DIV06) : on fournit la distribution
    complète, au lecteur (ou au frontend) d'en tirer ses propres seuils.
    """

    condition_dep = "AND lt.departement = %s" if departement else ""
    params: tuple = (region,) + ((departement,) if departement else ())

    rows = await fetch_all(
        f"""
        WITH diversite AS (
            SELECT
                lt.id AS lieu_id,
                COUNT(DISTINCT ast.categorie) FILTER (WHERE ast.nombre_total > 0) AS nb_fonctions
            FROM lieux_tournage lt
            JOIN films f ON f.id = lt.film_id
            LEFT JOIN amenity_stats ast ON ast.lieu_tournage_id = lt.id
            WHERE f.region = %s
              AND f.statut = 'publie'
              {condition_dep}
            GROUP BY lt.id
        )
        SELECT nb_fonctions, COUNT(*) AS n
        FROM diversite
        GROUP BY nb_fonctions
        ORDER BY nb_fonctions
        """,
        params,
    )

    if not rows:
        return {"n": 0, "moyenne": None, "mediane": None, "q1": None, "q3": None, "ecart_type": None, "distribution": []}

    valeurs: list[int] = []
    distribution = []
    for row in rows:
        nb = _safe_int(row.get("nb_fonctions")) or 0
        n = _safe_int(row.get("n")) or 0
        distribution.append({"nb_fonctions": nb, "n_lieux": n})
        valeurs.extend([nb] * n)

    n_total = len(valeurs)
    if not n_total:
        return {"n": 0, "moyenne": None, "mediane": None, "q1": None, "q3": None, "ecart_type": None, "distribution": distribution}

    valeurs.sort()
    moyenne = sum(valeurs) / n_total
    mediane = _median(valeurs)
    q1 = _median(valeurs[: n_total // 2]) if n_total >= 4 else None
    q3 = _median(valeurs[(n_total + 1) // 2 :]) if n_total >= 4 else None
    variance = sum((v - moyenne) ** 2 for v in valeurs) / n_total
    ecart_type = math.sqrt(variance)

    return {
        "n": n_total,
        "moyenne": round(moyenne, 2),
        "mediane": mediane,
        "q1": q1,
        "q3": q3,
        "ecart_type": round(ecart_type, 2),
        "distribution": distribution,
    }


async def construire_observatoire_statistique(
    region: str = "Occitanie",
) -> dict[str, Any]:
    """
    Dictionnaire statistique complet (Groupes A à I de la spec), calculé
    directement à partir des observations individuelles des lieux —
    jamais par moyenne des indicateurs départementaux (cf. section 4).

    Catégories couvertes : découvertes dynamiquement dans amenity_stats
    (jamais une liste codée en dur qui pourrait diverger du schéma réel).
    """

    categories_rows = await fetch_all(
        """
        SELECT DISTINCT ast.categorie
        FROM amenity_stats ast
        JOIN lieux_tournage lt ON lt.id = ast.lieu_tournage_id
        JOIN films f ON f.id = lt.film_id
        WHERE f.region = %s AND f.statut = 'publie'
        ORDER BY ast.categorie
        """,
        (region,),
    )
    categories = [row["categorie"] for row in categories_rows if row.get("categorie")]

    departements_rows = await fetch_all(
        """
        SELECT DISTINCT lt.departement
        FROM lieux_tournage lt
        JOIN films f ON f.id = lt.film_id
        WHERE f.region = %s AND f.statut = 'publie' AND lt.departement IS NOT NULL
        ORDER BY lt.departement
        """,
        (region,),
    )
    departements = [row["departement"] for row in departements_rows if row.get("departement")]

    equipements: dict[str, Any] = {}
    for categorie in categories:
        equipements[categorie] = await _stats_categorie(region, categorie)

    diversite_regionale = await _diversite_fonctionnelle(region)

    # ── Tableau départemental (Groupe G) ──
    # Une catégorie de référence est nécessaire pour un tableau lisible
    # par un élu ; on retient "hebergement" si elle existe (c'est la
    # catégorie la plus directement actionnable pour la valorisation
    # touristique), sinon la première catégorie disponible.
    categorie_reference = "hebergement" if "hebergement" in categories else (categories[0] if categories else None)

    tableau_departemental = []
    if categorie_reference:
        ref_regionale = equipements.get(categorie_reference, {})
        ref_off = ref_regionale.get("nombre", {})
        ref_prox = ref_regionale.get("distance_plus_proche_m", {})

        for dep in departements:
            stats_dep = await _stats_categorie(region, categorie_reference, departement=dep)
            diversite_dep = await _diversite_fonctionnelle(region, departement=dep)

            off = stats_dep.get("nombre", {})
            prox = stats_dep.get("distance_plus_proche_m", {})

            # DIS01/DIS02 : écart à la référence régionale, jamais de
            # classement "bon/mauvais" arbitraire (DIS03) — seulement
            # l'écart numérique et un intitulé neutre.
            def _ecart(dep_val, ref_val):
                if dep_val is None or ref_val is None:
                    return None, None
                absolu = round(dep_val - ref_val, 2)
                relatif = round((dep_val - ref_val) / ref_val * 100, 1) if ref_val != 0 else None
                return absolu, relatif

            ecart_off_absolu, ecart_off_relatif = _ecart(off.get("moyenne"), ref_off.get("moyenne"))

            tableau_departemental.append({
                "departement": dep,
                "n_lieux": stats_dep.get("n", 0),
                "categorie_reference": categorie_reference,
                "moyenne": off.get("moyenne"),
                "mediane": off.get("mediane"),
                "ecart_type": off.get("ecart_type"),
                "cv_pct": off.get("cv_pct"),
                "distance_mediane_m": prox.get("mediane"),
                "distance_p90_m": prox.get("p90"),
                "a_500m_pct": stats_dep.get("proximite", {}).get("a_500m_pct"),
                "sans_equipement_n": stats_dep.get("carence", {}).get("sans_equipement_n"),
                "diversite_moyenne": diversite_dep.get("moyenne"),
                "ecart_absolu_regional": ecart_off_absolu,
                "ecart_relatif_regional_pct": ecart_off_relatif,
                "position_regionale": (
                    None if ecart_off_relatif is None
                    else "au-dessus de la référence régionale" if ecart_off_relatif > 10
                    else "en-dessous de la référence régionale" if ecart_off_relatif < -10
                    else "proche de la référence régionale"
                ),
            })

    return {
        "region": region,
        "categories_disponibles": categories,
        "equipements": equipements,
        "diversite_fonctionnelle": diversite_regionale,
        "categorie_reference_tableau": categorie_reference,
        "departements": tableau_departemental,
        "avertissements_methodologiques": [
            "Les statistiques « distance_moyenne_10_plus_proches_m » portent "
            "uniquement sur les 10 équipements les plus proches de chaque lieu, "
            "jamais sur l'ensemble des équipements de la catégorie.",
            "Le rayon de recherche varie selon la catégorie (hébergement/activité "
            ": 15 km, restaurant : 8 km, office de tourisme : 20 km) — les "
            "comparaisons intercatégories de « nombre » ne sont donc pas directement "
            "comparables ; utiliser les taux de proximité (250 m/500 m/1 km) pour comparer.",
            "Le classement « position régionale » est purement descriptif : un écart "
            ">10% ou <-10% par rapport à la référence régionale, sans seuil "
            "scientifique universel — à interpréter avec prudence.",
            "L'écart-type est calculé sur la population des lieux observés "
            "(stddev_pop), pas sur un échantillon.",
        ],
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

    lieu_departement_map: dict[int, str | None] = {
        int(row["id"]): (str(row["departement"]).strip() if row.get("departement") else None)
        for row in lieux
        if row.get("id") is not None
    }

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

            "priorisation_departementale": [],

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

    # Mêmes compteurs mais ventilés par département — permet à un élu
    # de voir la situation de SON territoire, pas seulement la moyenne
    # régionale qui peut masquer de fortes disparités locales.
    dep_equip_acc: dict[str, dict[str, list]] = defaultdict(
        lambda: {"heb": [], "rest": [], "heb_presence": 0, "rest_presence": 0, "total": 0}
    )

    for lieu_id in lieu_ids:

        data = equipements.get(lieu_id, {})

        hebergement = data.get("hebergement")
        restaurant = data.get("restaurant")

        dep = lieu_departement_map.get(lieu_id)
        if dep:
            dep_equip_acc[dep]["total"] += 1

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

                if dep:
                    dep_equip_acc[dep]["heb"].append(float(nombre))
                    if nombre > 0:
                        dep_equip_acc[dep]["heb_presence"] += 1

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

                if dep:
                    dep_equip_acc[dep]["rest"].append(float(nombre))
                    if nombre > 0:
                        dep_equip_acc[dep]["rest_presence"] += 1

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

    dep_access_acc: dict[str, dict[str, int]] = defaultdict(
        lambda: {"total": 0, "avec_route": 0, "lieux_15": 0, "lieux_30": 0}
    )

    # IMPORTANT :
    #
    # On ne considère pas "absence de durée" comme "plus de 45 min".
    # Donc isoles_45_pct reste NULL si les données ne permettent pas
    # de le calculer.
    #
    # Ici on compte uniquement les durées réelles disponibles.

    for lieu_id in lieu_ids:

        dep = lieu_departement_map.get(lieu_id)
        if dep:
            dep_access_acc[dep]["total"] += 1

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
        if dep:
            dep_access_acc[dep]["avec_route"] += 1

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
            if dep:
                dep_access_acc[dep]["lieux_15"] += 1

        if duree_min <= 30 * 60:
            lieux_30 += 1
            if dep:
                dep_access_acc[dep]["lieux_30"] += 1

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
    # 8bis. Enrichissement départemental — équipement, accessibilité,
    # enclavement et score de priorité d'investissement.
    #
    # C'est le cœur de l'outil d'aide à la décision : un élu doit
    # pouvoir situer SON département, pas seulement lire une moyenne
    # régionale qui peut masquer de fortes disparités locales.
    # ------------------------------------------------------------------

    dep_popularite: dict[str, list[float]] = defaultdict(list)
    for row in lieux:
        dep = row.get("departement")
        pop = _safe_float(row.get("popularite"))
        if dep and pop is not None:
            dep_popularite[str(dep).strip()].append(pop)

    surface_par_dep = {
        item["departement"]: item["surface_moyenne_15min_km2"]
        for item in isochrones.get("surface_voiture_15_par_departement", [])
    }

    # Références régionales, utilisées comme repère de benchmark pour
    # chaque département (au-dessus / en-dessous de la moyenne).
    ref_moy_hebergement = equipements_result.get("moy_hebergement")
    ref_surface_15 = isochrones.get("surface_moyenne_voiture_15_km2")

    toutes_popularites_dep = [p for valeurs in dep_popularite.values() for p in valeurs]
    max_popularite_globale = max(toutes_popularites_dep) if toutes_popularites_dep else None
    max_surface_15_globale = max(surface_par_dep.values()) if surface_par_dep else None

    for dep_item in departements_result:
        dep = dep_item["departement"]
        eq = dep_equip_acc.get(dep, {})
        acc = dep_access_acc.get(
            dep, {"total": 0, "avec_route": 0, "lieux_15": 0, "lieux_30": 0}
        )

        moy_heb = round(sum(eq.get("heb", [])) / len(eq["heb"]), 2) if eq.get("heb") else None
        moy_rest = round(sum(eq.get("rest", [])) / len(eq["rest"]), 2) if eq.get("rest") else None

        heb_presence_pct = _pct(eq.get("heb_presence", 0), eq.get("total") or 0) if eq.get("total") else None
        rest_presence_pct = _pct(eq.get("rest_presence", 0), eq.get("total") or 0) if eq.get("total") else None

        # Même règle de rigueur que le calcul régional : pret_15/30 par
        # département n'est publié que si TOUS ses lieux ont une durée
        # réelle disponible — sinon NULL plutôt qu'un chiffre partiel
        # présenté comme une vérité territoriale complète.
        if acc["total"] > 0 and acc["avec_route"] == acc["total"]:
            dep_pret_15_pct = _pct(acc["lieux_15"], acc["total"])
            dep_pret_30_pct = _pct(acc["lieux_30"], acc["total"])
        else:
            dep_pret_15_pct = None
            dep_pret_30_pct = None

        surface_15 = surface_par_dep.get(dep)

        pop_list = dep_popularite.get(dep, [])
        popularite_moyenne = round(sum(pop_list) / len(pop_list), 1) if pop_list else None

        # ── Score de priorité d'investissement (0-100) ──
        # Moyenne de 3 composantes normalisées, calculée uniquement sur
        # celles réellement disponibles pour ce département (on ne
        # pénalise pas un département simplement parce qu'une donnée
        # n'a pas encore été précalculée pour lui) :
        #   - attractivité cinématographique (popularité moyenne / max régional)
        #   - retard d'équipement touristique (100 - taux de présence hébergement+restaurant)
        #   - enclavement routier (surface 15 min du département / surface max régionale)
        composantes = []

        if popularite_moyenne is not None and max_popularite_globale:
            composantes.append(min(100.0, popularite_moyenne / max_popularite_globale * 100))

        if heb_presence_pct is not None and rest_presence_pct is not None:
            equipement_moyen_pct = (heb_presence_pct + rest_presence_pct) / 2
            composantes.append(100 - equipement_moyen_pct)  # retard d'équipement = priorité

        if surface_15 is not None and max_surface_15_globale:
            composantes.append(min(100.0, surface_15 / max_surface_15_globale * 100))

        score_investissement = round(sum(composantes) / len(composantes), 1) if composantes else None

        dep_item.update({
            "moy_hebergement": moy_heb,
            "moy_restaurant": moy_rest,
            "hebergement_presence_pct": heb_presence_pct,
            "restaurant_presence_pct": rest_presence_pct,
            "pret_15_pct": dep_pret_15_pct,
            "pret_30_pct": dep_pret_30_pct,
            "surface_moyenne_15min_km2": surface_15,
            "popularite_moyenne": popularite_moyenne,
            "score_investissement": score_investissement,
            "benchmark_hebergement": (
                "au-dessus de la moyenne régionale"
                if (moy_heb is not None and ref_moy_hebergement is not None and moy_heb > ref_moy_hebergement)
                else "en-dessous de la moyenne régionale"
                if (moy_heb is not None and ref_moy_hebergement is not None and moy_heb < ref_moy_hebergement)
                else "dans la moyenne régionale"
                if (moy_heb is not None and ref_moy_hebergement is not None)
                else "non comparable"
            ),
            "benchmark_enclavement": (
                "plus enclavé que la moyenne régionale"
                if (surface_15 is not None and ref_surface_15 is not None and surface_15 > ref_surface_15)
                else "moins enclavé que la moyenne régionale"
                if (surface_15 is not None and ref_surface_15 is not None and surface_15 < ref_surface_15)
                else "dans la moyenne régionale"
                if (surface_15 is not None and ref_surface_15 is not None)
                else "non comparable"
            ),
        })

    priorisation_departementale = sorted(
        [d for d in departements_result if d.get("score_investissement") is not None],
        key=lambda d: d["score_investissement"],
        reverse=True,
    )

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

        "priorisation_departementale": priorisation_departementale,

        "accessibilite": accessibilite,

        "equipements": equipements_result,

        "isochrones": isochrones,

        "films_notables": films_notables,

        "lieux_potentiel": lieux_potentiel,

        "completude": completude,

        "metriques": metriques,
    }