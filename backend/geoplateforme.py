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
import math
import os
import time

import httpx

logger = logging.getLogger(__name__)


BASE_URL = "https://data.geopf.fr/navigation"

# ─────────────────────────────────────────────────────────────
# Ressources IGN (liste officielle : https://data.geopf.fr/navigation/getcapabilities)
# ─────────────────────────────────────────────────────────────
# « bdtopo-osrm » a été retirée du service par l'IGN (HTTP 400 « Parameter 'resource' is invalid »).
# Les ressources de la Géoplateforme peuvent changer : elles se règlent donc SANS nouveau push,
# par variables d'environnement Render (liste séparée par des virgules, essayées dans l'ordre) :
#     IGN_RESSOURCES_VOITURE   (défaut : bdtopo-valhalla)
#     IGN_RESSOURCES_PIETON    (défaut : bdtopo-pgr,bdtopo-valhalla)
#     IGN_RESSOURCE_ISOCHRONE  (défaut : bdtopo-valhalla)
# Toutes restent des ressources de la Géoplateforme IGN : aucun moteur extérieur n'est utilisé.
# Une ressource n'est suivie d'une autre que si elle échoue OU renvoie un tracé jugé incohérent
# (voir _verifier_route).

def _liste_env(nom: str, defaut: str) -> list[str]:
    brut = os.getenv(nom)
    if brut is None:
        brut = defaut
    return [r.strip() for r in brut.split(",") if r.strip()]


RESSOURCES_ITINERAIRE = {
    "car": _liste_env("IGN_RESSOURCES_VOITURE", "bdtopo-valhalla"),
    "pedestrian": _liste_env("IGN_RESSOURCES_PIETON", "bdtopo-pgr,bdtopo-valhalla"),
}

# Conservée pour main.py (messages d'erreur) : ressource principale pour la voiture.
RESOURCE_ITINERAIRE = (RESSOURCES_ITINERAIRE["car"] or ["bdtopo-valhalla"])[0]
RESOURCE_ISOCHRONE = (_liste_env("IGN_RESSOURCE_ISOCHRONE", "bdtopo-valhalla") or ["bdtopo-valhalla"])[0]

# Écart maximal (mètres) accepté entre le point demandé et l'extrémité du tracé renvoyé
# (le routeur « accroche » le point au réseau routier le plus proche).
TOLERANCE_EXTREMITES_M = float(os.getenv("IGN_TOLERANCE_EXTREMITES_M", "1500"))


MODE_PROFIL = {
    "foot-walking": "pedestrian",
    "driving-car": "car",
}

# Tous les noms de mode acceptés par l'application → profil IGN.
_MODES_ITINERAIRE = {
    "driving-car": "car",
    "car": "car",
    "foot-walking": "pedestrian",
    "walking": "pedestrian",
    "pedestrian": "pedestrian",
}


# Modes vélo : l'IGN n'a pas de profil vélo → routage_velo.py (openrouteservice), activé par ORS_API_KEY.
MODES_VELO = {"bicycle", "cycling", "cycling-regular", "velo", "vélo"}


class GeoplateformeError(Exception):
    """Erreur du service IGN. `code` précise la cause :
    INDISPONIBLE (réseau, délai, 5xx, limite de débit), RESSOURCE_INTROUVABLE (la ressource n'existe plus
    ou n'est pas configurée : vérifier getcapabilities), GEOMETRIE_INCOHERENTE (tracé refusé par nos contrôles),
    et pour le vélo : VELO_NON_CONFIGURE, VELO_CLE_INVALIDE, AUCUN_ITINERAIRE."""

    def __init__(self, message: str = "", code: str = "INDISPONIBLE"):
        super().__init__(message)
        self.code = code


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




async def _itineraire_ressource(
    depart_lat: float,
    depart_lon: float,
    arrivee_lat: float,
    arrivee_lon: float,
    mode: str,
    avec_etapes: bool,
    priorite: str,
    resource: str,
) -> dict:
    """
    Calcule un itinéraire avec UNE ressource précise de la Géoplateforme IGN
    (la sélection de la ressource et les contrôles sont faits par calculer_itineraire).

    Service :
        https://data.geopf.fr/navigation/itineraire

    Retour :
        {
            "type": "route_reelle",
            "geometry": GeoJSON,
            "distance_metres": float,
            "duree_secondes": float | None,
            "etapes": list,
            "provider": "geoplateforme",
            "resource": resource,
            "mode": str,
        }

    Aucun fallback vers un moteur extérieur à la Géoplateforme.
    """

    mode_ign = _MODES_ITINERAIRE.get(mode)

    if not mode_ign:
        raise GeoplateformeError(
            f"Mode de déplacement non supporté : {mode}"
        )

    url = f"{BASE_URL}/itineraire"

    params = {
        "resource": resource,
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

        # HTTP 400 « Parameter 'resource' is invalid: it does not exist on this service » :
        # la ressource n'existe plus côté IGN → ce n'est PAS une panne temporaire.
        texte_detail = str(detail).lower()
        code_erreur = (
            "RESSOURCE_INTROUVABLE"
            if response.status_code in (400, 404) and "resource" in texte_detail
            and ("invalid" in texte_detail or "does not exist" in texte_detail or "not found" in texte_detail)
            else "INDISPONIBLE"
        )

        raise GeoplateformeError(
            f"Erreur HTTP {response.status_code} de la "
            f"Géoplateforme IGN (ressource {resource}) : {detail}",
            code_erreur,
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
        "resource": resource,
        "mode": mode,
    }


# ─────────────────────────────────────────────────────────────
# Contrôle des tracés et choix de la ressource
# ─────────────────────────────────────────────────────────────

def _distance_vol_oiseau_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def _extremites_geometrie(geometry) -> tuple | None:
    """Premier et dernier point (lon, lat) d'une LineString ou MultiLineString GeoJSON."""
    if not isinstance(geometry, dict):
        return None
    coords = geometry.get("coordinates")
    if not isinstance(coords, list) or not coords:
        return None
    if isinstance(coords[0], (list, tuple)) and coords[0] and isinstance(coords[0][0], (int, float)):
        premier, dernier = coords[0], coords[-1]
    else:
        lignes = [ligne for ligne in coords if isinstance(ligne, list) and ligne]
        if not lignes:
            return None
        premier, dernier = lignes[0][0], lignes[-1][-1]
    if len(premier) < 2 or len(dernier) < 2:
        return None
    return premier, dernier


def _verifier_route(res: dict, depart_lat: float, depart_lon: float, arrivee_lat: float, arrivee_lon: float) -> dict:
    """Refuse un tracé manifestement faux avant de le montrer ou de l'enregistrer en base :
    extrémités éloignées des points demandés, distance impossible (plus courte que la ligne droite
    ou démesurément longue). Renvoie les écarts mesurés (utile au diagnostic)."""
    ext = _extremites_geometrie(res.get("geometry"))
    if not ext:
        raise GeoplateformeError("Tracé IGN sans coordonnées exploitables.", "GEOMETRIE_INCOHERENTE")

    ecart_depart = _distance_vol_oiseau_m(depart_lat, depart_lon, float(ext[0][1]), float(ext[0][0]))
    ecart_arrivee = _distance_vol_oiseau_m(arrivee_lat, arrivee_lon, float(ext[1][1]), float(ext[1][0]))
    if ecart_depart > TOLERANCE_EXTREMITES_M or ecart_arrivee > TOLERANCE_EXTREMITES_M:
        raise GeoplateformeError(
            f"Tracé IGN incohérent : extrémités à {ecart_depart:.0f} m (départ) et {ecart_arrivee:.0f} m "
            f"(arrivée) des points demandés (tolérance {TOLERANCE_EXTREMITES_M:.0f} m).",
            "GEOMETRIE_INCOHERENTE",
        )

    vol = _distance_vol_oiseau_m(depart_lat, depart_lon, arrivee_lat, arrivee_lon)
    distance = float(res.get("distance_metres") or 0)
    if vol > 100:
        if distance <= 0 or distance < 0.8 * vol - 2 * TOLERANCE_EXTREMITES_M:
            raise GeoplateformeError(
                f"Tracé IGN incohérent : {distance:.0f} m annoncés pour {vol:.0f} m à vol d'oiseau.",
                "GEOMETRIE_INCOHERENTE",
            )
        if distance > 8 * vol + 5000:
            raise GeoplateformeError(
                f"Tracé IGN incohérent : {distance:.0f} m annoncés pour {vol:.0f} m à vol d'oiseau (détour démesuré).",
                "GEOMETRIE_INCOHERENTE",
            )
    return {"ecart_depart_m": round(ecart_depart), "ecart_arrivee_m": round(ecart_arrivee), "vol_oiseau_m": round(vol)}


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
    Calcule un itinéraire avec la Géoplateforme IGN.

    Les ressources sont essayées dans l'ordre configuré pour le mode (voir RESSOURCES_ITINERAIRE).
    Une ressource est écartée si elle échoue ou si son tracé est incohérent. Si toutes échouent,
    la dernière erreur est levée : aucun itinéraire inventé, aucun moteur extérieur à l'IGN.
    La clé « resource » du résultat indique la ressource réellement utilisée.
    """
    if mode in MODES_VELO:
        # Import différé : routage_velo importe lui-même ce module.
        from routage_velo import calculer_itineraire_velo

        res = await calculer_itineraire_velo(
            depart_lat, depart_lon, arrivee_lat, arrivee_lon, avec_etapes, priorite
        )
        _verifier_route(res, depart_lat, depart_lon, arrivee_lat, arrivee_lon)
        return res

    profil = _MODES_ITINERAIRE.get(mode)
    if not profil:
        raise GeoplateformeError(f"Mode de déplacement non supporté : {mode}")

    ressources = RESSOURCES_ITINERAIRE.get(profil) or []
    if not ressources:
        raise GeoplateformeError(
            f"Aucune ressource IGN configurée pour le profil « {profil} » "
            "(variables IGN_RESSOURCES_VOITURE / IGN_RESSOURCES_PIETON).",
            "RESSOURCE_INTROUVABLE",
        )

    derniere: GeoplateformeError | None = None
    for ressource in ressources:
        try:
            res = await _itineraire_ressource(
                depart_lat, depart_lon, arrivee_lat, arrivee_lon, mode, avec_etapes, priorite, ressource
            )
            _verifier_route(res, depart_lat, depart_lon, arrivee_lat, arrivee_lon)
            return res
        except GeoplateformeError as exc:
            derniere = exc
            logger.warning("IGN itinéraire : ressource %s écartée (%s) : %s", ressource, exc.code, exc)
            if len(ressources) > 1:
                print(f"⚠️ IGN ressource {ressource} écartée ({exc.code}) : {exc}", flush=True)

    assert derniere is not None
    raise derniere


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
    """Teste CHAQUE ressource configurée, en voiture et à pied (Foix → Pamiers, avec instructions) :
    ce que répond réellement l'IGN depuis CE serveur, et si le tracé passe nos contrôles.
    Liste officielle des ressources : https://data.geopf.fr/navigation/getcapabilities"""
    dep_lat, dep_lon, arr_lat, arr_lon = 42.9639, 1.6054, 43.1161, 1.6076
    out: dict = {
        "url": f"{BASE_URL}/itineraire",
        "debit_interactif_par_s": round(1 / _LIMITEURS["interactif"].intervalle, 2),
        "debit_fond_par_s": round(1 / _LIMITEURS["fond"].intervalle, 2),
        "ressources_configurees": RESSOURCES_ITINERAIRE,
        "ressource_isochrone": RESOURCE_ISOCHRONE,
        "tolerance_extremites_m": TOLERANCE_EXTREMITES_M,
        "essais": [],
    }
    for profil, mode in (("car", "driving-car"), ("pedestrian", "foot-walking")):
        for ressource in RESSOURCES_ITINERAIRE.get(profil, []):
            essai: dict = {"profil": profil, "ressource": ressource}
            debut = time.monotonic()
            try:
                res = await _itineraire_ressource(dep_lat, dep_lon, arr_lat, arr_lon, mode, True, "interactif", ressource)
                essai.update(ok=True, distance_m=round(res["distance_metres"]), duree_s=res["duree_secondes"],
                             nb_etapes=len(res["etapes"]))
                try:
                    essai["controles"] = _verifier_route(res, dep_lat, dep_lon, arr_lat, arr_lon)
                except GeoplateformeError as exc:
                    essai.update(ok=False, code=exc.code, erreur=str(exc))
                if essai.get("ok") and not essai["nb_etapes"]:
                    essai["avertissement"] = "Itinéraire reçu mais SANS instructions : la structure de réponse a peut-être changé."
            except Exception as exc:
                essai.update(ok=False, code=getattr(exc, "code", type(exc).__name__), erreur=str(exc)[:400])
            essai["duree_ms"] = round((time.monotonic() - debut) * 1000)
            out["essais"].append(essai)
    out["ok"] = all(
        any(e["ok"] for e in out["essais"] if e["profil"] == profil)
        for profil in ("car", "pedestrian")
    )
    return out