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
    mode: str = "driving-car",
    avec_etapes: bool = False,
) -> dict:
    """
    Calcule un itinéraire routable avec la Géoplateforme IGN.

    Ressource utilisée :
        bdtopo-osrm

    Retour :
        {
            "geometry": GeoJSON,
            "distance_metres": float,
            "duree_secondes": float | None,
            "etapes": list
        }

    En cas d'échec, une GeoplateformeError est levée.
    Aucun fallback externe n'est effectué ici.
    """

    # Correspondance des modes de ton application
    mode_mapping = {
        "driving-car": "car",
        "car": "car",
        "foot-walking": "pedestrian",
        "walking": "pedestrian",
        "pedestrian": "pedestrian",
        "cycling-regular": "bike",
        "cycling": "bike",
        "bike": "bike",
    }

    mode_ign = mode_mapping.get(mode)

    if not mode_ign:
        raise GeoplateformeError(
            f"Mode de déplacement non supporté : {mode}"
        )

    # Endpoint officiel de navigation Géoplateforme
    url = f"{BASE_URL}/itineraire"

    params = {
        "resource": RESOURCE_ITINERAIRE,
        "start": f"{depart_lon},{depart_lat}",
        "end": f"{arrivee_lon},{arrivee_lat}",
        "profile": mode_ign,
    }

    if avec_etapes:
        params["geometry"] = "geojson"
        params["instructions"] = "true"
    else:
        params["geometry"] = "geojson"

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=10.0,
                read=60.0,
                write=10.0,
                pool=10.0,
            )
        ) as client:

            response = await client.get(
                url,
                params=params,
                headers={
                    "Accept": "application/json",
                },
            )

    except httpx.TimeoutException as exc:
        raise GeoplateformeError(
            "La Géoplateforme IGN a dépassé le délai d'attente."
        ) from exc

    except httpx.RequestError as exc:
        raise GeoplateformeError(
            f"Impossible de contacter la Géoplateforme IGN : {exc}"
        ) from exc

    # Erreurs HTTP
    if response.status_code >= 400:
        try:
            detail = response.json()
        except Exception:
            detail = response.text[:500]

        raise GeoplateformeError(
            f"Erreur HTTP {response.status_code} de la "
            f"Géoplateforme IGN : {detail}"
        )

    # Décodage JSON
    try:
        data = response.json()

    except ValueError as exc:
        raise GeoplateformeError(
            "La Géoplateforme IGN a retourné une réponse "
            "qui n'est pas un JSON valide."
        ) from exc

    # ---------------------------------------------------------
    # Extraction du résultat
    # ---------------------------------------------------------

    features = data.get("features")

    if not features:
        raise GeoplateformeError(
            "La Géoplateforme IGN n'a retourné aucun itinéraire."
        )

    feature = features[0]

    geometry = feature.get("geometry")

    if not geometry:
        raise GeoplateformeError(
            "La Géoplateforme IGN n'a retourné aucune géométrie."
        )

    properties = feature.get("properties") or {}

    # Les noms peuvent varier selon la réponse du service.
    distance = (
        properties.get("distance")
        or properties.get("distance_m")
        or properties.get("distance_metres")
        or 0
    )

    duree = (
        properties.get("duration")
        or properties.get("duration_s")
        or properties.get("duree_secondes")
    )

    try:
        distance = float(distance)
    except (TypeError, ValueError):
        distance = 0.0

    if duree is not None:
        try:
            duree = float(duree)
        except (TypeError, ValueError):
            duree = None

    # ---------------------------------------------------------
    # Étapes de navigation
    # ---------------------------------------------------------

    etapes = []

    if avec_etapes:

        # Selon la structure retournée par le service
        raw_steps = (
            properties.get("steps")
            or properties.get("etapes")
            or []
        )

        if isinstance(raw_steps, list):

            for step in raw_steps:

                if not isinstance(step, dict):
                    continue

                instruction = (
                    step.get("instruction")
                    or step.get("message")
                    or step.get("text")
                    or ""
                )

                step_distance = (
                    step.get("distance")
                    or step.get("distance_m")
                    or 0
                )

                step_duration = (
                    step.get("duration")
                    or step.get("duration_s")
                )

                etapes.append({
                    "instruction": str(instruction),
                    "distance_metres": (
                        float(step_distance)
                        if step_distance is not None
                        else 0
                    ),
                    "duree_secondes": (
                        float(step_duration)
                        if step_duration is not None
                        else None
                    ),
                    "geometry": step.get("geometry"),
                })

    return {
        "geometry": geometry,
        "distance_metres": distance,
        "duree_secondes": duree,
        "etapes": etapes,
        "provider": "geoplateforme",
        "resource": RESOURCE_ITINERAIRE,
        "mode": mode,
    }


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