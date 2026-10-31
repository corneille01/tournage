"""
backend/geoplateforme.py

Client centralisé des services de navigation de la Géoplateforme IGN.

Services :
    /navigation/itineraire
    /navigation/isochrone
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

import httpx

logger = logging.getLogger(__name__)


BASE_URL = "https://data.geopf.fr/navigation"

RESOURCE_ITINERAIRE = "bdtopo-osrm"
RESOURCE_ISOCHRONE = "bdtopo-valhalla"


MODE_PROFIL = {
    "foot-walking": "pedestrian",
    "driving-car": "car",
}


class GeoplateformeError(Exception):
    pass


# ─────────────────────────────────────────────────────────────
# Débit et reprises
# ─────────────────────────────────────────────────────────────
# L'IGN limite chaque adresse IP à 5 requêtes par seconde (documentation du service, data.gouv.fr).
# Sur un hébergeur, l'adresse est PARTAGÉE avec d'autres clients : on reste nettement en dessous.
# Deux files d'attente : « interactif » (le visiteur attend : navigation, parcours) est prioritaire
# sur « fond » (précalculs, distances de confort), pour qu'un calcul de masse ne bloque jamais un clic.
# Total par défaut : 4,5 requêtes/s. Réglable : IGN_DEBIT_INTERACTIF, IGN_DEBIT_FOND (requêtes/s).

class _Limiteur:
    """Espace les départs d'appels : au plus `debit` appels par seconde, servis dans l'ordre d'arrivée."""

    def __init__(self, debit: float):
        self.intervalle = 1.0 / max(0.1, debit)
        self._prochain = 0.0
        self._verrou: asyncio.Lock | None = None

    async def attendre(self) -> None:
        if self._verrou is None:
            self._verrou = asyncio.Lock()
        async with self._verrou:
            attente = self._prochain - time.monotonic()
            if attente > 0:
                await asyncio.sleep(attente)
            self._prochain = max(time.monotonic(), self._prochain) + self.intervalle


_LIMITEURS = {
    "interactif": _Limiteur(float(os.getenv("IGN_DEBIT_INTERACTIF", "3.0"))),
    "fond": _Limiteur(float(os.getenv("IGN_DEBIT_FOND", "1.5"))),
}
_TIMEOUT = httpx.Timeout(connect=8.0, read=25.0, write=8.0, pool=8.0)
_CODES_A_REESSAYER = (429, 502, 503, 504)


async def _requete_ign(url: str, params: dict, priorite: str = "interactif", tentatives: int = 3) -> httpx.Response:
    """GET vers l'IGN avec limitation de débit et reprises (429, 502, 503, 504, délais dépassés).
    Renvoie la dernière réponse si elle reste en erreur HTTP (l'appelant la traduit) ; lève l'exception
    réseau si aucun essai n'a abouti."""
    limiteur = _LIMITEURS.get(priorite, _LIMITEURS["interactif"])
    derniere = None
    for essai in range(tentatives):
        await limiteur.attendre()
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                r = await client.get(url, params=params, headers={"Accept": "application/json"})
        except (httpx.TimeoutException, httpx.RequestError) as exc:
            derniere = exc
            logger.warning("IGN %s : %s (essai %d/%d)", url.rsplit("/", 1)[-1], type(exc).__name__, essai + 1, tentatives)
            await asyncio.sleep(0.4 * (2 ** essai))
            continue
        if r.status_code in _CODES_A_REESSAYER and essai < tentatives - 1:
            try:
                attente = float(r.headers.get("Retry-After", ""))
            except ValueError:
                attente = 0.6 * (2 ** essai)
            logger.warning("IGN %s : HTTP %s (essai %d/%d), nouvel essai dans %.1f s",
                           url.rsplit("/", 1)[-1], r.status_code, essai + 1, tentatives, min(attente, 3.0))
            derniere = r
            await asyncio.sleep(min(attente, 3.0))
            continue
        return r
    if isinstance(derniere, httpx.Response):
        return derniere
    raise derniere


def _profil(mode: str) -> str:
    try:
        return MODE_PROFIL[mode]
    except KeyError:
        raise ValueError(
            "mode doit être 'foot-walking' ou 'driving-car'"
        ) from None




async def calculer_itineraire(
    depart_lat: float,
    depart_lon: float,
    arrivee_lat: float,
    arrivee_lon: float,
    mode: str = "driving-car",
    avec_etapes: bool = False,
    priorite: str = "interactif",
) -> dict:
    """
    Calcule un itinéraire routable avec la Géoplateforme IGN.

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
        response = await _requete_ign(url, params, priorite)

    except httpx.TimeoutException as exc:
        raise GeoplateformeError(
            "La Géoplateforme IGN a dépassé le délai d'attente pour l'itinéraire."
        ) from exc

    except httpx.RequestError as exc:
        raise GeoplateformeError(
            f"Impossible de contacter la Géoplateforme IGN pour l'itinéraire : {exc}"
        ) from exc

    if response.status_code >= 400:
        try:
            detail = response.json()
        except Exception:
            detail = response.text[:1000]

        raise GeoplateformeError(
            f"Erreur HTTP {response.status_code} de la "
            f"Géoplateforme IGN : {detail}"
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

        if not isinstance(feature, dict):
            raise GeoplateformeError(
                "La Géoplateforme IGN a retourné une feature invalide."
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

    # La réponse Géoplateforme utilise généralement :
    #
    # portions[]
    #     └── steps[]
    #
    # Chaque step contient notamment :
    # geometry, distance, duration, instruction, attributes.

    portions = route.get("portions") or []

    def _derniere_coordonnee(geometry_step):
        """
        Retourne la dernière coordonnée d'une géométrie GeoJSON.
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
        Transforme une instruction IGN en phrase française.

        Cette fonction protège notamment contre le cas où
        attributes["name"] est un dictionnaire au lieu d'une chaîne.
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
            "left": "à gauche",
            "right": "à droite",
            "slight left": "légèrement à gauche",
            "slight right": "légèrement à droite",
            "sharp left": "fortement à gauche",
            "sharp right": "fortement à droite",
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
            texte = "Départ"

        elif type_instruction in (
            "arrive",
            "destination",
            "end",
        ):
            texte = "Vous êtes arrivé à destination"

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
            # On évite absolument de fabriquer une phrase
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
    # EXTRACTION DES ÉTAPES
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

                # Récupération propre du nom de voie.
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
        response = await _requete_ign(f"{BASE_URL}/isochrone", params, "interactif")
        if response.status_code >= 400:
            raise GeoplateformeError(
                f"Erreur HTTP {response.status_code} de la Géoplateforme IGN pour l'isochrone : {response.text[:300]}"
            )
        data = response.json()

    except httpx.TimeoutException as exc:
        raise GeoplateformeError(
            "La Géoplateforme IGN a dépassé le délai d'attente pour l'isochrone."
        ) from exc

    except httpx.RequestError as exc:
        raise GeoplateformeError(
            f"Impossible de contacter la Géoplateforme IGN pour l'isochrone : {exc}"
        ) from exc

    except ValueError as exc:
        raise GeoplateformeError(
            "La Géoplateforme IGN a retourné une réponse JSON invalide pour l'isochrone."
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


async def diagnostic() -> dict:
    """Appel de test (Foix → Pamiers, avec instructions) : ce que répond réellement l'IGN depuis CE serveur."""
    params = {"resource": RESOURCE_ITINERAIRE, "start": "1.6054,42.9639", "end": "1.6076,43.1161", "profile": "car",
              "optimization": "fastest", "geometryFormat": "geojson", "distanceUnit": "meter", "timeUnit": "second",
              "crs": "EPSG:4326", "getSteps": "true", "getBbox": "false"}
    debut = time.monotonic()
    out: dict = {"url": f"{BASE_URL}/itineraire", "debit_interactif_par_s": round(1 / _LIMITEURS["interactif"].intervalle, 2),
                 "debit_fond_par_s": round(1 / _LIMITEURS["fond"].intervalle, 2)}
    try:
        r = await _requete_ign(f"{BASE_URL}/itineraire", params, "interactif", tentatives=1)
    except Exception as exc:
        return {**out, "ok": False, "erreur": f"{type(exc).__name__}: {exc}", "duree_ms": round((time.monotonic() - debut) * 1000)}
    out.update(statut_http=r.status_code, duree_ms=round((time.monotonic() - debut) * 1000),
               en_tetes_utiles={k: v for k, v in r.headers.items() if k.lower().startswith(("retry-after", "x-ratelimit", "ratelimit", "server", "via"))})
    try:
        j = r.json()
    except ValueError:
        return {**out, "ok": False, "corps_debut": r.text[:300]}
    if r.status_code >= 400:
        return {**out, "ok": False, "reponse": j}
    portions = j.get("portions") or []
    out.update(ok=True, cles_reponse=sorted(j.keys()), nb_portions=len(portions),
               nb_etapes=sum(len(p.get("steps") or []) for p in portions if isinstance(p, dict)),
               distance=j.get("distance"), duree=j.get("duration"))
    if not out["nb_etapes"]:
        out["avertissement"] = "Itinéraire reçu mais SANS instructions : la structure de réponse a peut-être changé (cles_reponse)."
    return out