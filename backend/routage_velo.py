"""
backend/routage_velo.py

Itinéraires À VÉLO.

Pourquoi un module séparé : la Géoplateforme IGN ne propose AUCUN profil vélo
(https://data.geopf.fr/navigation/getcapabilities : seuls « car » et « pedestrian » existent, sur toutes les
ressources). Router le vélo avec le réseau piéton ou voiture donnerait des tracés faux (escaliers, sentiers
impraticables, nationales dangereuses) : on ne le fait pas.

Moteur utilisé : openrouteservice (HeiGIT), qui route sur OpenStreetMap avec de vrais profils cyclistes
(pistes cyclables, revêtement, dénivelé).

    ORS_API_KEY      clé openrouteservice (gratuite, à créer sur https://openrouteservice.org/dev/#/signup).
                     SANS CLÉ, le vélo est simplement désactivé : les boutons ne s'affichent pas
                     et /api/itineraire?mode=bicycle répond « vélo non activé ».
    ORS_PROFIL_VELO  profil cycliste (défaut : cycling-regular ; autres : cycling-road, cycling-mountain,
                     cycling-electric).
    ORS_DEBIT        requêtes par seconde maximum (défaut : 1.0).

Le résultat a EXACTEMENT la forme de geoplateforme.calculer_itineraire (geometry, distance_metres,
duree_secondes, etapes…), avec en plus « provider », « attribution » et le dénivelé.
Le provider est toujours indiqué : un tracé vélo n'est jamais présenté comme venant de l'IGN.
"""

from __future__ import annotations

import asyncio
import logging
import os

import httpx

from geoplateforme import GeoplateformeError, _Limiteur

logger = logging.getLogger(__name__)

ORS_URL = "https://api.openrouteservice.org/v2/directions"
ATTRIBUTION = "© openrouteservice.org by HeiGIT · données © contributeurs OpenStreetMap"

_TIMEOUT = httpx.Timeout(connect=8.0, read=25.0, write=8.0, pool=8.0)
_CODES_A_REESSAYER = (429, 502, 503, 504)
_LIMITEUR = _Limiteur(float(os.getenv("ORS_DEBIT", "1.0")))


def _cle() -> str:
    return (os.getenv("ORS_API_KEY") or "").strip()


def _profil() -> str:
    return (os.getenv("ORS_PROFIL_VELO") or "cycling-regular").strip()


def est_configure() -> bool:
    """Vrai si une clé openrouteservice est présente : le vélo est alors proposé aux visiteurs."""
    return bool(_cle())


# Types de manœuvre openrouteservice → (type, modifier) dans le vocabulaire déjà utilisé par l'application.
_MANOEUVRES = {
    0: ("turn", "left"),
    1: ("turn", "right"),
    2: ("turn", "sharp left"),
    3: ("turn", "sharp right"),
    4: ("turn", "slight left"),
    5: ("turn", "slight right"),
    6: ("continue", "straight"),
    7: ("roundabout", None),
    8: ("roundabout", None),
    9: ("turn", "uturn"),
    10: ("arrive", None),
    11: ("depart", None),
    12: ("fork", "left"),
    13: ("fork", "right"),
}


async def _requete(corps: dict, profil: str, tentatives: int = 3) -> httpx.Response:
    """POST vers openrouteservice avec limitation de débit et reprises (429, 502, 503, 504, réseau)."""
    url = f"{ORS_URL}/{profil}/geojson"
    entetes = {
        "Authorization": _cle(),
        "Content-Type": "application/json; charset=utf-8",
        "Accept": "application/geo+json, application/json",
    }
    derniere: httpx.Response | Exception | None = None
    for essai in range(tentatives):
        await _LIMITEUR.attendre()
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                r = await client.post(url, json=corps, headers=entetes)
        except (httpx.TimeoutException, httpx.RequestError) as exc:
            derniere = exc
            logger.warning("ORS vélo : %s (essai %d/%d)", type(exc).__name__, essai + 1, tentatives)
            await asyncio.sleep(0.5 * (2 ** essai))
            continue
        if r.status_code in _CODES_A_REESSAYER and essai < tentatives - 1:
            try:
                attente = float(r.headers.get("Retry-After", ""))
            except ValueError:
                attente = 0.8 * (2 ** essai)
            logger.warning("ORS vélo : HTTP %s (essai %d/%d)", r.status_code, essai + 1, tentatives)
            derniere = r
            await asyncio.sleep(min(attente, 3.0))
            continue
        return r
    if isinstance(derniere, httpx.Response):
        return derniere
    raise derniere  # type: ignore[misc]


def _message_erreur(r: httpx.Response) -> tuple[str, str]:
    """(code, message) d'une réponse d'erreur openrouteservice."""
    try:
        corps = r.json()
    except ValueError:
        corps = {}
    erreur = corps.get("error") if isinstance(corps, dict) else None
    if isinstance(erreur, dict):
        detail = f"{erreur.get('code', '')} {erreur.get('message', '')}".strip()
        code_ors = erreur.get("code")
    else:
        detail = str(erreur or r.text[:300])
        code_ors = None

    if r.status_code in (401, 403):
        return "VELO_CLE_INVALIDE", f"Clé openrouteservice refusée (HTTP {r.status_code}) : {detail}"
    if r.status_code == 404 or code_ors == 2010:
        # 2010 : « Could not find routable point within a radius of … » (départ ou arrivée hors réseau cyclable)
        return "AUCUN_ITINERAIRE", f"Aucun itinéraire cyclable trouvé : {detail}"
    if code_ors in (2004, 2003):
        return "AUCUN_ITINERAIRE", f"Trajet trop long pour le service vélo : {detail}"
    return "INDISPONIBLE", f"Erreur HTTP {r.status_code} de openrouteservice : {detail}"


async def calculer_itineraire_velo(
    depart_lat: float,
    depart_lon: float,
    arrivee_lat: float,
    arrivee_lon: float,
    avec_etapes: bool = False,
    priorite: str = "interactif",  # signature identique à l'IGN ; le débit est géré par ORS_DEBIT
) -> dict:
    if not est_configure():
        raise GeoplateformeError(
            "Itinéraire à vélo non activé sur ce serveur (variable ORS_API_KEY absente).",
            "VELO_NON_CONFIGURE",
        )

    profil = _profil()
    corps = {
        "coordinates": [[depart_lon, depart_lat], [arrivee_lon, arrivee_lat]],
        "instructions": bool(avec_etapes),
        "instructions_format": "text",
        "language": "fr",
        "units": "m",
        "elevation": True,
        "preference": "recommended",
    }

    try:
        r = await _requete(corps, profil)
    except httpx.TimeoutException as exc:
        raise GeoplateformeError("Le service d'itinéraire vélo a dépassé le délai d'attente.") from exc
    except httpx.RequestError as exc:
        raise GeoplateformeError(f"Impossible de contacter le service d'itinéraire vélo : {exc}") from exc

    if r.status_code >= 400:
        code, message = _message_erreur(r)
        raise GeoplateformeError(message, code)

    try:
        data = r.json()
    except ValueError as exc:
        raise GeoplateformeError("Le service vélo a retourné une réponse JSON invalide.") from exc

    features = data.get("features") if isinstance(data, dict) else None
    if not features or not isinstance(features[0], dict):
        raise GeoplateformeError("Le service vélo n'a retourné aucun itinéraire.", "AUCUN_ITINERAIRE")

    feature = features[0]
    geometry = feature.get("geometry")
    props = feature.get("properties") or {}
    if not isinstance(geometry, dict) or not geometry.get("coordinates"):
        raise GeoplateformeError("Le service vélo n'a retourné aucune géométrie.", "GEOMETRIE_INCOHERENTE")
    coords = geometry["coordinates"]

    resume = props.get("summary") or {}
    try:
        distance = float(resume.get("distance") or 0)
    except (TypeError, ValueError):
        distance = 0.0
    try:
        duree = float(resume["duration"]) if resume.get("duration") is not None else None
    except (TypeError, ValueError):
        duree = None

    segments = [s for s in (props.get("segments") or []) if isinstance(s, dict)]

    def _total(cle: str) -> float | None:
        valeurs = [s.get(cle) for s in segments if isinstance(s.get(cle), (int, float))]
        if isinstance(props.get(cle), (int, float)):
            return float(props[cle])
        return float(sum(valeurs)) if valeurs else None

    denivele_positif = _total("ascent")
    denivele_negatif = _total("descent")

    # Étapes : même convention que l'IGN — chaque étape porte l'instruction ET les coordonnées de sa FIN
    # (= début de l'étape suivante) : l'appli annonce l'instruction suivante en arrivant à ce point.
    etapes: list[dict] = []
    if avec_etapes:
        index = 0
        for segment in segments:
            for step in segment.get("steps") or []:
                if not isinstance(step, dict):
                    continue
                wp = step.get("way_points") or []
                if len(wp) < 2:
                    continue
                try:
                    fin = coords[int(wp[1])]
                    debut = coords[int(wp[0])]
                except (IndexError, TypeError, ValueError):
                    continue
                type_ors = step.get("type")
                type_app, modifier = _MANOEUVRES.get(type_ors, ("continue", None))
                instruction = step.get("instruction")
                if not isinstance(instruction, str) or not instruction.strip():
                    instruction = "Continuer"
                nom = step.get("name")
                nom = nom.strip() if isinstance(nom, str) and nom.strip() and nom.strip() != "-" else ""
                etapes.append({
                    "index": index,
                    "instruction": instruction.strip(),
                    "latitude": float(fin[1]),
                    "longitude": float(fin[0]),
                    "distance_metres": float(step.get("distance") or 0),
                    "duree_secondes": float(step["duration"]) if isinstance(step.get("duration"), (int, float)) else None,
                    "geometry": {
                        "type": "LineString",
                        "coordinates": coords[int(wp[0]): int(wp[1]) + 1] or [debut, fin],
                    },
                    "name": nom,
                    "type": type_app,
                    "modifier": modifier,
                })
                index += 1

    return {
        "type": "route_reelle",
        "geometry": geometry,
        "distance_metres": distance,
        "duree_secondes": duree,
        "etapes": etapes,
        "provider": "openrouteservice",
        "resource": profil,
        "mode": "bicycle",
        "denivele_positif_m": round(denivele_positif) if denivele_positif is not None else None,
        "denivele_negatif_m": round(denivele_negatif) if denivele_negatif is not None else None,
        "attribution": ATTRIBUTION,
    }


async def diagnostic() -> dict:
    """Test du vélo depuis CE serveur (Foix → Pamiers) : clé présente ? réponse ? étapes ? dénivelé ?"""
    out: dict = {"configure": est_configure(), "profil": _profil(), "debit_par_s": float(os.getenv("ORS_DEBIT", "1.0"))}
    if not out["configure"]:
        out["ok"] = False
        out["information"] = "Vélo désactivé : définir ORS_API_KEY sur Render pour l'activer."
        return out
    try:
        res = await calculer_itineraire_velo(42.9639, 1.6054, 43.1161, 1.6076, avec_etapes=True)
    except GeoplateformeError as exc:
        return {**out, "ok": False, "code": exc.code, "erreur": str(exc)[:400]}
    except Exception as exc:  # noqa: BLE001
        return {**out, "ok": False, "erreur": f"{type(exc).__name__}: {exc}"[:400]}
    out.update(ok=True, distance_m=round(res["distance_metres"]), duree_s=res["duree_secondes"],
               nb_etapes=len(res["etapes"]), denivele_positif_m=res["denivele_positif_m"],
               denivele_negatif_m=res["denivele_negatif_m"])
    if not out["nb_etapes"]:
        out["avertissement"] = "Itinéraire reçu mais SANS instructions : la structure de réponse a peut-être changé."
    return out