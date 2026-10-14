"""
backend/geoplateforme.py

Client centralisÃ© des services de navigation de la GÃ©oplateforme IGN.

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
            "mode doit Ãªtre 'foot-walking' ou 'driving-car'"
        ) from None




async def calculer_itineraire(
    depart_lat: float,
    depart_lon: float,
    arrivee_lat: float,
    arrivee_lon: float,
    mode: str = "driving-car",
    avec_etapes: bool = False,
) -> dict:
    """
    Calcule un itinÃ©raire routable avec la GÃ©oplateforme IGN.

    Service :
        https://data.geopf.fr/navigation/itineraire

    Ressource :
        bdtopo-osrm

    Retour :
        {
            "type": "route_reelle",
            "geometry": GeoJSON,
            "distance_metres": float,
            "duree_secondes": float | None,
            "etapes": list,
            "provider": "geoplateforme",
            "resource": "bdtopo-osrm",
            "mode": str,
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
            f"Mode de dÃ©placement non supportÃ© : {mode}"
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
            "La GÃ©oplateforme IGN a dÃ©passÃ© le dÃ©lai d'attente pour l'isochrone."
        ) from exc

    except httpx.RequestError as exc:
        raise GeoplateformeError(
            f"Impossible de contacter la GÃ©oplateforme IGN pour l'isochrone : {exc}"
        ) from exc

    except ValueError as exc:
        raise GeoplateformeError(
            "La GÃ©oplateforme IGN a retournÃ© une rÃ©ponse JSON invalide pour l'isochrone."
        ) from exc


    if response.status_code >= 400:
        try:
            detail = response.json()
        except Exception:
            detail = response.text[:1000]

        raise GeoplateformeError(
            f"Erreur HTTP {response.status_code} de la "
            f"GÃ©oplateforme IGN : {detail}"
        )

    try:
        data = response.json()
    except ValueError as exc:
        raise GeoplateformeError(
            "La GÃ©oplateforme IGN a retournÃ© une rÃ©ponse JSON invalide."
        ) from exc

    # ---------------------------------------------------------
    # STRUCTURE DE RÃ‰PONSE
    # ---------------------------------------------------------

    if not isinstance(data, dict):
        raise GeoplateformeError(
            "RÃ©ponse inattendue de la GÃ©oplateforme IGN."
        )

    # L'API peut retourner directement l'itinÃ©raire.
    route = data

    # CompatibilitÃ© si la rÃ©ponse est enveloppÃ©e.
    if isinstance(data.get("route"), dict):
        route = data["route"]

    elif isinstance(data.get("result"), dict):
        route = data["result"]

    # CompatibilitÃ© GeoJSON FeatureCollection.
    if data.get("type") == "FeatureCollection":
        features = data.get("features") or []

        if not features:
            raise GeoplateformeError(
                "La GÃ©oplateforme IGN n'a retournÃ© aucun itinÃ©raire."
            )

        feature = features[0]

        if not isinstance(feature, dict):
            raise GeoplateformeError(
                "La GÃ©oplateforme IGN a retournÃ© une feature invalide."
            )

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

        # Selon les versions du service, les Ã©tapes peuvent Ãªtre
        # directement dans la rÃ©ponse.
        raw_steps = (
            route.get("steps")
            or route.get("etapes")
            or route.get("instructions")
            or []
        )

    if not geometry:
        raise GeoplateformeError(
            "La GÃ©oplateforme IGN n'a retournÃ© aucune gÃ©omÃ©trie."
        )

    # ---------------------------------------------------------
    # DISTANCE / DURÃ‰E
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
    # Ã‰TAPES DE NAVIGATION IGN
    # ---------------------------------------------------------

    etapes = []

    # La rÃ©ponse GÃ©oplateforme utilise gÃ©nÃ©ralement :
    #
    # portions[]
    #     â””â”€â”€ steps[]
    #
    # Chaque step contient notamment :
    # geometry, distance, duration, instruction, attributes.

    portions = route.get("portions") or []

    def _derniere_coordonnee(geometry_step):
        """
        Retourne la derniÃ¨re coordonnÃ©e d'une gÃ©omÃ©trie GeoJSON.
        Compatible LineString et MultiLineString.
        """

        if not isinstance(geometry_step, dict):
            return None

        coords = geometry_step.get("coordinates")

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
        """
        Transforme une instruction IGN en phrase franÃ§aise.

        Cette fonction protÃ¨ge notamment contre le cas oÃ¹
        attributes["name"] est un dictionnaire au lieu d'une chaÃ®ne.
        """

        instruction = step.get("instruction")

        if not isinstance(instruction, dict):
            instruction = {}

        type_instruction = str(
            instruction.get("type") or ""
        ).lower()

        modifier = str(
            instruction.get("modifier") or ""
        ).lower()

        # -----------------------------------------------------
        # NOM DE LA VOIE
        # -----------------------------------------------------

        attributes = step.get("attributes")

        if not isinstance(attributes, dict):
            attributes = {}

        candidats_nom = [
            step.get("name"),
            attributes.get("name"),
            attributes.get("cpx_toponyme_route_nommee"),
            attributes.get("cpx_toponyme"),
            attributes.get("cpx_numero"),
            attributes.get("nom_1_droite"),
            attributes.get("nom_1_gauche"),
        ]

        nom = ""

        for candidat in candidats_nom:
            if isinstance(candidat, str) and candidat.strip():
                nom = candidat.strip()
                break

        # -----------------------------------------------------
        # TRADUCTION DES DIRECTIONS
        # -----------------------------------------------------

        modificateurs = {
            "left": "Ã  gauche",
            "right": "Ã  droite",
            "slight left": "lÃ©gÃ¨rement Ã  gauche",
            "slight right": "lÃ©gÃ¨rement Ã  droite",
            "sharp left": "fortement Ã  gauche",
            "sharp right": "fortement Ã  droite",
            "straight": "tout droit",
            "uturn": "faites demi-tour",
            "u-turn": "faites demi-tour",
        }

        direction = modificateurs.get(
            modifier,
            modifier,
        )

        # -----------------------------------------------------
        # INSTRUCTION
        # -----------------------------------------------------

        if type_instruction in (
            "depart",
            "start",
        ):
            texte = "DÃ©part"

        elif type_instruction in (
            "arrive",
            "destination",
            "end",
        ):
            texte = "Vous Ãªtes arrivÃ© Ã  destination"

        elif type_instruction in (
            "turn",
            "turn-left",
            "turn-right",
        ):
            if direction:
                texte = f"Tourner {direction}"
            else:
                texte = "Tourner"

        elif type_instruction == "fork":
            if direction:
                texte = f"Prendre la bifurcation {direction}"
            else:
                texte = "Prendre la bifurcation"

        elif type_instruction in (
            "continue",
            "new name",
            "new_name",
        ):
            if direction == "tout droit":
                texte = "Continuer tout droit"
            elif direction:
                texte = f"Continuer {direction}"
            else:
                texte = "Continuer"

        elif type_instruction in (
            "merge",
            "on ramp",
            "on_ramp",
        ):
            if direction:
                texte = f"Rejoindre {direction}"
            else:
                texte = "Rejoindre la voie"

        elif type_instruction in (
            "off ramp",
            "off_ramp",
        ):
            if direction:
                texte = f"Prendre la sortie {direction}"
            else:
                texte = "Prendre la sortie"

        elif type_instruction in (
            "roundabout",
            "rotary",
        ):
            texte = "Prendre le rond-point"

        else:
            # On Ã©vite absolument de fabriquer une phrase
            # avec un objet/dictionnaire Python.
            texte = "Continuer"

        # -----------------------------------------------------
        # AJOUT DU NOM DE LA VOIE
        # -----------------------------------------------------

        if nom and type_instruction not in (
            "arrive",
            "destination",
            "end",
        ):
            texte += f" sur {nom}"

        return texte

    # ---------------------------------------------------------
    # EXTRACTION DES Ã‰TAPES
    # ---------------------------------------------------------

    index = 0

    if isinstance(portions, list):
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

                step_duration = step.get("duration")

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

                # RÃ©cupÃ©ration propre du nom de voie.
                attributes = step.get("attributes")

                if not isinstance(attributes, dict):
                    attributes = {}

                nom_etape = ""

                candidats_nom_etape = [
                    step.get("name"),
                    attributes.get("name"),
                    attributes.get(
                        "cpx_toponyme_route_nommee"
                    ),
                    attributes.get("cpx_toponyme"),
                    attributes.get("cpx_numero"),
                    attributes.get("nom_1_droite"),
                    attributes.get("nom_1_gauche"),
                ]

                for candidat in candidats_nom_etape:
                    if (
                        isinstance(candidat, str)
                        and candidat.strip()
                    ):
                        nom_etape = candidat.strip()
                        break

                etapes.append(
                    {
                        "index": index,
                        "instruction": instruction,
                        "latitude": latitude,
                        "longitude": longitude,
                        "distance_metres": step_distance,
                        "duree_secondes": step_duration,
                        "geometry": geometry_step,
                        "name": nom_etape,
                        "type": (
                            raw_instruction.get("type")
                            if isinstance(
                                raw_instruction,
                                dict,
                            )
                            else None
                        ),
                        "modifier": (
                            raw_instruction.get("modifier")
                            if isinstance(
                                raw_instruction,
                                dict,
                            )
                            else None
                        ),
                    }
                )

                index += 1

    # ---------------------------------------------------------
    # RETOUR FINAL
    # ---------------------------------------------------------

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

    except httpx.TimeoutException as exc:
            raise GeoplateformeError(
                "La GÃ©oplateforme IGN a dÃ©passÃ© le dÃ©lai d'attente pour l'isochrone."
            ) from exc

    except httpx.RequestError as exc:
            raise GeoplateformeError(
                f"Impossible de contacter la GÃ©oplateforme IGN pour l'isochrone : {exc}"
            ) from exc

    except ValueError as exc:
            raise GeoplateformeError(
                "La GÃ©oplateforme IGN a retournÃ© une rÃ©ponse JSON invalide pour l'isochrone."
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
            "RÃ©ponse GÃ©oplateforme sans gÃ©omÃ©trie d'isochrone."
        )

    return {
        "type": "isochrone",
        "provider": "geoplateforme",
        "resource": RESOURCE_ISOCHRONE,
        "profile": profile,
        "minutes": minutes,
        "geometry": geometry,
    }
