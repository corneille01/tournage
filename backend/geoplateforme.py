"""
backend/geoplateforme.py

Client centralisé des services de navigation de la Géoplateforme IGN.

Services :
    /navigation/itineraire
    /navigation/isochrone
"""

from __future__ import annotations

import httpx


BASE_URL = "https://data.geopf.fr/navigation"

RESOURCE_ITINERAIRE = "bdtopo-osrm"
RESOURCE_ISOCHRONE = "bdtopo-valhalla"


MODE_PROFIL = {
    "foot-walking": "pedestrian",
    "driving-car": "car",
}


class GeoplateformeError(Exception):
    pass


def _profil(mode: str) -> str:
    try:
        return MODE_PROFIL[mode]
    except KeyError:
        raise ValueError(
            "mode doit être 'foot-walking' ou 'driving-car'"
        )


def _extraire_route(data: dict) -> dict:
    """
    Normalise différentes formes possibles de réponse
    de l'API Géoplateforme.
    """

    # Réponse GeoJSON Feature
    if data.get("type") == "Feature":
        return data

    # FeatureCollection
    if data.get("type") == "FeatureCollection":
        features = data.get("features") or []

        if not features:
            raise GeoplateformeError(
                "Réponse GeoJSON sans feature."
            )

        return features[0]

    # Réponse enveloppée
    for cle in ("route", "result"):
        valeur = data.get(cle)

        if isinstance(valeur, dict):
            if valeur.get("type") == "Feature":
                return valeur

            return valeur

    return data


def _extraire_valeur(data: dict, *cles):
    for cle in cles:
        valeur = data.get(cle)

        if valeur is not None:
            return valeur

    properties = data.get("properties")

    if isinstance(properties, dict):
        for cle in cles:
            valeur = properties.get(cle)

            if valeur is not None:
                return valeur

    return None


async def calculer_itineraire(
    depart_lat: float,
    depart_lon: float,
    arrivee_lat: float,
    arrivee_lon: float,
    mode: str = "foot-walking",
    avec_etapes: bool = False,
) -> dict:

    profile = _profil(mode)

    params = {
        "resource": RESOURCE_ITINERAIRE,
        "start": f"{depart_lon},{depart_lat}",
        "end": f"{arrivee_lon},{arrivee_lat}",
        "profile": profile,
        "optimization": "fastest",
        "geometryFormat": "geojson",
        "getSteps": "true" if avec_etapes else "false",
        "getBbox": "true",
        "distanceUnit": "meter",
        "timeUnit": "second",
        "crs": "EPSG:4326",
    }

    try:

        async with httpx.AsyncClient(timeout=30) as client:

            response = await client.get(
                f"{BASE_URL}/itineraire",
                params=params,
            )

            response.raise_for_status()

            data = response.json()

    except Exception as exc:

        raise GeoplateformeError(
            f"Erreur API itinéraire : {exc}"
        ) from exc

    route = _extraire_route(data)

    geometry = route.get("geometry")

    if geometry is None:

        raise GeoplateformeError(
            "Réponse Géoplateforme sans géométrie exploitable."
        )

    distance = _extraire_valeur(
        route,
        "distance",
        "distanceMeters",
    )

    duration = _extraire_valeur(
        route,
        "duration",
        "durationSeconds",
    )

    properties = route.get("properties")

    if isinstance(properties, dict):

        summary = properties.get("summary")

        if isinstance(summary, dict):

            distance = distance or summary.get("distance")
            duration = duration or summary.get("duration")

    resultat = {
        "type": "route_reelle",
        "provider": "geoplateforme",
        "resource": RESOURCE_ITINERAIRE,
        "geometry": geometry,
        "distance_metres": (
            round(float(distance))
            if distance is not None
            else None
        ),
        "duree_secondes": (
            round(float(duration))
            if duration is not None
            else None
        ),
    }

    if avec_etapes:

        resultat["etapes_navigation"] = (
            route.get("steps")
            or route.get("properties", {}).get("steps", [])
            if isinstance(route.get("properties", {}), dict)
            else []
        )

    return resultat


async def calculer_isochrone(
    lat: float,
    lon: float,
    mode: str = "driving-car",
    minutes: int = 30,
) -> dict:

    profile = _profil(mode)

    params = {
        "resource": RESOURCE_ISOCHRONE,
        "point": f"{lon},{lat}",
        "direction": "departure",
        "costType": "time",
        "costValue": str(minutes * 60),
        "profile": profile,
        "timeUnit": "second",
        "distanceUnit": "meter",
        "geometryFormat": "geojson",
        "crs": "EPSG:4326",
    }

    try:

        async with httpx.AsyncClient(timeout=30) as client:

            response = await client.get(
                f"{BASE_URL}/isochrone",
                params=params,
            )

            response.raise_for_status()

            data = response.json()

    except Exception as exc:

        raise GeoplateformeError(
            f"Erreur API isochrone : {exc}"
        ) from exc

    # GeoJSON direct
    if data.get("type") in (
        "Feature",
        "FeatureCollection",
        "Polygon",
        "MultiPolygon",
    ):
        geometry = data

    else:

        geometry = data.get("geometry")

        if geometry is None:

            isochrone = data.get("isochrone")

            if isinstance(isochrone, dict):
                geometry = isochrone.get("geometry")

        if geometry is None:

            result = data.get("result")

            if isinstance(result, dict):
                geometry = result.get("geometry")

    if geometry is None:

        raise GeoplateformeError(
            "Réponse Géoplateforme sans géométrie d'isochrone."
        )

    return {
        "type": "isochrone",
        "provider": "geoplateforme",
        "resource": RESOURCE_ISOCHRONE,
        "profile": profile,
        "minutes": minutes,
        "geometry": geometry,
    }