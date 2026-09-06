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

    Service :
        https://data.geopf.fr/navigation/itineraire

    Ressource :
        bdtopo-osrm

    Retour :
        {
            "geometry": GeoJSON,
            "distance_metres": float,
            "duree_secondes": float | None,
            "etapes": list
        }

    Aucun fallback vers un autre moteur de routage.
    """

    mode_mapping = {
        "driving-car": "car",
        "car": "car",
        "foot-walking": "pedestrian",
        "walking": "pedestrian",
        "pedestrian": "pedestrian",
    }

    mode_ign = mode_mapping.get(mode)

    if not mode_ign:
        raise GeoplateformeError(
            f"Mode de déplacement non supporté : {mode}"
        )

    url = f"{BASE_URL}/itineraire"

    params = {
        "resource": RESOURCE_ITINERAIRE,
        "start": f"{depart_lon},{depart_lat}",
        "end": f"{arrivee_lon},{arrivee_lat}",
        "profile": mode_ign,
        "optimization": "fastest",
        "geometryFormat": "geojson",
        "distanceUnit": "meter",
        "timeUnit": "second",
        "crs": "EPSG:4326",
        "getSteps": "true" if avec_etapes else "false",
        "getBbox": "false",
    }

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

    if response.status_code >= 400:
        try:
            detail = response.json()
        except Exception:
            detail = response.text[:1000]

        raise GeoplateformeError(
            f"Erreur HTTP {response.status_code} de la Géoplateforme IGN : {detail}"
        )

    try:
        data = response.json()
    except ValueError as exc:
        raise GeoplateformeError(
            "La Géoplateforme IGN a retourné une réponse JSON invalide."
        ) from exc

    # ---------------------------------------------------------
    # STRUCTURE DE RÉPONSE
    # ---------------------------------------------------------

    if not isinstance(data, dict):
        raise GeoplateformeError(
            "Réponse inattendue de la Géoplateforme IGN."
        )

    # L'API peut retourner directement l'itinéraire.
    route = data

    # Compatibilité si la réponse est enveloppée.
    if isinstance(data.get("route"), dict):
        route = data["route"]

    elif isinstance(data.get("result"), dict):
        route = data["result"]

    # Compatibilité GeoJSON FeatureCollection.
    if data.get("type") == "FeatureCollection":
        features = data.get("features") or []

        if not features:
            raise GeoplateformeError(
                "La Géoplateforme IGN n'a retourné aucun itinéraire."
            )

        feature = features[0]
        properties = feature.get("properties") or {}

        geometry = feature.get("geometry")

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

        raw_steps = (
            properties.get("steps")
            or properties.get("etapes")
            or []
        )

    else:
        geometry = route.get("geometry")

        distance = (
            route.get("distance")
            or route.get("distance_m")
            or route.get("distance_metres")
            or 0
        )

        duree = (
            route.get("duration")
            or route.get("duration_s")
            or route.get("duree_secondes")
        )

        # Selon les versions du service, les étapes peuvent être
        # directement dans la réponse.
        raw_steps = (
            route.get("steps")
            or route.get("etapes")
            or route.get("instructions")
            or []
        )

    if not geometry:
        raise GeoplateformeError(
            "La Géoplateforme IGN n'a retourné aucune géométrie."
        )

    # ---------------------------------------------------------
    # DISTANCE / DURÉE
    # ---------------------------------------------------------

    try:
        distance = float(distance or 0)
    except (TypeError, ValueError):
        distance = 0.0

    if duree is not None:
        try:
            duree = float(duree)
        except (TypeError, ValueError):
            duree = None

     # ---------------------------------------------------------
    # ÉTAPES DE NAVIGATION IGN
    # ---------------------------------------------------------

    etapes = []

    # La réponse Géoplateforme utilise :
    #
    # portions[]
    #   └── steps[]
    #
    # Chaque step contient notamment :
    # geometry, distance, duration, instruction, attributes.

    portions = route.get("portions") or []

    if isinstance(portions, list):

        def _derniere_coordonnee(geometry):
            if not isinstance(geometry, dict):
                return None

            coords = geometry.get("coordinates")

            if not coords:
                return None

            # LineString
            if (
                isinstance(coords, list)
                and coords
                and isinstance(coords[0], (list, tuple))
                and len(coords[0]) >= 2
                and isinstance(coords[0][0], (int, float))
            ):
                return coords[-1]

            # MultiLineString
            if (
                isinstance(coords, list)
                and coords
                and isinstance(coords[0], list)
            ):
                for ligne in reversed(coords):
                    if ligne:
                        return ligne[-1]

            return None

        def _instruction_fr(step):
            instruction = step.get("instruction")

            if not isinstance(instruction, dict):
                return (
                    step.get("message")
                    or step.get("text")
                    or step.get("name")
                    or ""
                )

            type_instruction = (
                instruction.get("type")
                or ""
            ).lower()

            modifier = (
                instruction.get("modifier")
                or ""
            ).lower()

            nom = (
                step.get("name")
                or (step.get("attributes") or {}).get("name")
                or ""
            )

            modificateurs = {
                "left": "à gauche",
                "right": "à droite",
                "slight left": "légèrement à gauche",
                "slight right": "légèrement à droite",
                "straight": "tout droit",
            }

            if type_instruction == "depart":
                texte = "Départ"

            elif type_instruction == "arrive":
                texte = "Vous êtes arrivé à destination"

            elif type_instruction == "turn":
                direction = modificateurs.get(
                    modifier,
                    modifier or ""
                )

                texte = (
                    f"Tourner {direction}".strip()
                )

            elif type_instruction == "fork":
                direction = modificateurs.get(
                    modifier,
                    modifier or ""
                )

                texte = (
                    f"Prendre la bifurcation {direction}".strip()
                )

            elif type_instruction in (
                "continue",
                "new name",
            ):
                if modifier == "straight":
                    texte = "Continuer tout droit"
                else:
                    texte = "Continuer"

            else:
                texte = "Continuer"

            if nom and type_instruction not in ("arrive",):
                texte += f" sur {nom}"

            return texte

        index = 0

        for portion in portions:

            if not isinstance(portion, dict):
                continue

            steps = portion.get("steps") or []

            if not isinstance(steps, list):
                continue

            for step in steps:

                if not isinstance(step, dict):
                    continue

                geometry_step = step.get("geometry")

                coord = _derniere_coordonnee(
                    geometry_step
                )

                if not coord or len(coord) < 2:
                    continue

                try:
                    longitude = float(coord[0])
                    latitude = float(coord[1])
                except (TypeError, ValueError):
                    continue

                step_distance = (
                    step.get("distance")
                    or 0
                )

                step_duration = (
                    step.get("duration")
                )

                try:
                    step_distance = float(
                        step_distance or 0
                    )
                except (TypeError, ValueError):
                    step_distance = 0.0

                if step_duration is not None:
                    try:
                        step_duration = float(
                            step_duration
                        )
                    except (TypeError, ValueError):
                        step_duration = None

                instruction = _instruction_fr(step)

                raw_instruction = step.get(
                    "instruction"
                )

                etapes.append({
                    "index": index,
                    "instruction": instruction,
                    "latitude": latitude,
                    "longitude": longitude,
                    "distance_metres": step_distance,
                    "duree_secondes": step_duration,
                    "geometry": geometry_step,
                    "name": (
                        step.get("name")
                        or (step.get("attributes") or {}).get("name")
                    ),
                    "type": (
                        raw_instruction.get("type")
                        if isinstance(raw_instruction, dict)
                        else None
                    ),
                    "modifier": (
                        raw_instruction.get("modifier")
                        if isinstance(raw_instruction, dict)
                        else None
                    ),
                })

                index += 1

    return {
        "type": "route_reelle",
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