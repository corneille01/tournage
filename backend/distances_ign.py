"""
backend/distances_ign.py

Distances et durées PAR LA ROUTE (Géoplateforme IGN) pour le parcours.

Ordre de recherche, du plus fiable au moins fiable :
  1. la table `distances_routieres` (précalculée chaque jour par
     precalculer_distances_routieres.py) : aucun appel IGN pour le visiteur ;
  2. un appel IGN en direct, mis en cache 24 h : uniquement pour ce qui n'est pas
     précalculé (point de départ saisi par le visiteur, paires de lieux lointaines) ;
  3. une estimation, toujours signalée comme telle.

Principes :
- La distance « à vol d'oiseau » (haversine) ne sert JAMAIS à afficher ni à
  classer : uniquement à présélectionner quelques candidats avant d'interroger
  l'IGN, pour ne pas multiplier les appels.
- Chaque trajet est mis en cache 24 h (mémoire du processus + Redis/Upstash si
  configuré), par tranche de ~10 m.
- Débit limité (4 appels simultanés + petite pause) : l'API IGN accepte 10 req/s
  par IP.
- Si l'IGN ne répond pas, on renvoie None : l'appelant décide du repli et doit
  l'indiquer à l'utilisateur (distance « estimée »).
"""
from __future__ import annotations

import asyncio
import logging
import time

from db import fetch_all
from geoplateforme import GeoplateformeError, calculer_itineraire
from navigation_cache import navigation_cache
from overpass import haversine_metres

logger = logging.getLogger(__name__)

CONCURRENCE_MAX = 4
PAUSE_ENTRE_APPELS_S = 0.25
TTL_S = 24 * 60 * 60
MEMOIRE_MAX = 5000

# Utilisés UNIQUEMENT pour estimer quand l'IGN est indisponible.
FACTEUR_ROUTE = {"driving-car": 1.30, "foot-walking": 1.15}
VITESSE_KMH = {"driving-car": 45.0, "foot-walking": 4.5}

_memoire: dict[str, tuple[float, dict]] = {}
_semaphore: asyncio.Semaphore | None = None


def _sem() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(CONCURRENCE_MAX)
    return _semaphore


def _cle(origine: tuple[float, float], destination: tuple[float, float], mode: str) -> str:
    n = navigation_cache._normaliser_coordonnee
    return (
        f"pelify:distance-route:v1:{mode}:"
        f"{n(origine[0])},{n(origine[1])}:{n(destination[0])},{n(destination[1])}"
    )


def _memoriser(cle: str, valeur: dict) -> None:
    if len(_memoire) >= MEMOIRE_MAX:
        # Purge simple : on retire les entrées expirées, puis les plus anciennes.
        maintenant = time.monotonic()
        for k in [k for k, (exp, _) in _memoire.items() if exp <= maintenant]:
            _memoire.pop(k, None)
        while len(_memoire) >= MEMOIRE_MAX:
            _memoire.pop(next(iter(_memoire)), None)
    _memoire[cle] = (time.monotonic() + TTL_S, valeur)


def estimation(origine: tuple[float, float], destination: tuple[float, float], mode: str = "driving-car") -> dict:
    """Repli quand l'IGN est indisponible. Toujours marqué source = « estimee »."""
    d = haversine_metres(origine[0], origine[1], destination[0], destination[1]) * FACTEUR_ROUTE.get(mode, 1.3)
    duree = d / 1000.0 / VITESSE_KMH.get(mode, 45.0) * 3600.0
    return {"distance_metres": round(d), "duree_secondes": round(duree), "source": "estimee"}


async def distance_routiere(
    origine: tuple[float, float], destination: tuple[float, float], mode: str = "driving-car"
) -> dict | None:
    """{'distance_metres', 'duree_secondes', 'source': 'ign'} ou None si l'IGN échoue."""
    cle = _cle(origine, destination, mode)
    hit = _memoire.get(cle)
    if hit and hit[0] > time.monotonic():
        return hit[1]

    if navigation_cache.enabled:
        try:
            en_cache = await navigation_cache.get(cle)
        except Exception:  # Redis indisponible : on calcule quand même
            en_cache = None
        if en_cache and en_cache.get("distance_metres") is not None:
            _memoriser(cle, en_cache)
            return en_cache

    async with _sem():
        try:
            res = await calculer_itineraire(
                depart_lat=origine[0], depart_lon=origine[1],
                arrivee_lat=destination[0], arrivee_lon=destination[1],
                mode=mode, avec_etapes=False,
            )
        except (GeoplateformeError, ValueError) as exc:
            logger.warning("Distance IGN indisponible (%s) : %s", mode, exc)
            return None
        except Exception:
            logger.exception("Erreur imprévue pendant un calcul de distance IGN")
            return None
        await asyncio.sleep(PAUSE_ENTRE_APPELS_S)

    if res.get("distance_metres") is None:
        return None
    distance = round(float(res["distance_metres"]))
    duree = res.get("duree_secondes")
    if duree is None:  # distance IGN conservée ; seule la durée est déduite
        duree = distance / 1000.0 / VITESSE_KMH.get(mode, 45.0) * 3600.0
    out = {"distance_metres": distance, "duree_secondes": round(float(duree)), "source": "ign"}
    _memoriser(cle, out)
    if navigation_cache.enabled:
        try:
            await navigation_cache.set(cle, out)
        except Exception:
            pass
    return out


async def distances_routieres(
    origine: tuple[float, float],
    destinations: list[tuple[float, float]],
    mode: str = "driving-car",
    delai_max_s: float = 8.0,
) -> list[dict | None]:
    """Un résultat par destination, dans le même ordre ; None si échec ou délai dépassé."""
    if not destinations:
        return []
    taches = [asyncio.ensure_future(distance_routiere(origine, d, mode)) for d in destinations]
    _, en_attente = await asyncio.wait(taches, timeout=delai_max_s)
    for t in en_attente:
        t.cancel()
    if en_attente:
        await asyncio.gather(*en_attente, return_exceptions=True)
        logger.warning("Délai dépassé pour %d calcul(s) de distance IGN", len(en_attente))
    sortie: list[dict | None] = []
    for t in taches:
        if t.cancelled():
            sortie.append(None)
        elif t.exception() is not None:
            sortie.append(None)
        else:
            sortie.append(t.result())
    return sortie


def _coord(x: dict) -> tuple[float, float]:
    return float(x["latitude"]), float(x["longitude"])


async def mesure_ou_estimation(
    origine: tuple[float, float], destination: tuple[float, float], mode: str = "driving-car"
) -> dict:
    """Distance IGN si possible, sinon estimation clairement marquée."""
    return (await distance_routiere(origine, destination, mode)) or estimation(origine, destination, mode)


MODE_BASE = {"driving-car": "voiture", "foot-walking": "pied"}


async def charger_distances_lieux(ids: list, mode: str = "driving-car") -> dict[tuple[int, int], dict]:
    """Trajets lieu -> lieu précalculés entre les lieux donnés. Le sens inverse sert à
    défaut (la distance de route A→B et B→A est quasi identique)."""
    ids = sorted({int(i) for i in ids if i is not None})
    if len(ids) < 2:
        return {}
    try:
        lignes = await fetch_all(
            """
            SELECT origine_id, lieu_destination_id, distance_metres, duree_secondes
            FROM distances_routieres
            WHERE type_origine = 'lieu' AND mode = %s
              AND origine_id = ANY(%s) AND lieu_destination_id = ANY(%s)
            """,
            (MODE_BASE.get(mode, "voiture"), ids, ids),
        )
    except Exception:
        logger.warning("Table distances_routieres indisponible : repli sur l'IGN en direct")
        return {}
    direct = {
        (int(x["origine_id"]), int(x["lieu_destination_id"])): {
            "distance_metres": int(x["distance_metres"]),
            "duree_secondes": x["duree_secondes"],
            "source": "ign",
        }
        for x in lignes
    }
    complet = dict(direct)
    for (a, b), m in direct.items():
        complet.setdefault((b, a), m)
    return complet


async def charger_distances_guides(guide_ids: list, lieu_ids: list) -> dict[tuple[int, int], dict]:
    """Trajets guide -> lieu précalculés (voiture)."""
    guide_ids = sorted({int(i) for i in guide_ids if i is not None})
    lieu_ids = sorted({int(i) for i in lieu_ids if i is not None})
    if not guide_ids or not lieu_ids:
        return {}
    try:
        lignes = await fetch_all(
            """
            SELECT origine_id, lieu_destination_id, distance_metres, duree_secondes
            FROM distances_routieres
            WHERE type_origine = 'guide' AND mode = 'voiture'
              AND origine_id = ANY(%s) AND lieu_destination_id = ANY(%s)
            """,
            (guide_ids, lieu_ids),
        )
    except Exception:
        logger.warning("Table distances_routieres indisponible : repli sur l'IGN en direct")
        return {}
    return {
        (int(x["origine_id"]), int(x["lieu_destination_id"])): {
            "distance_metres": int(x["distance_metres"]),
            "duree_secondes": x["duree_secondes"],
            "source": "ign",
        }
        for x in lignes
    }


def _id(x: dict):
    v = x.get("id")
    return int(v) if v is not None else None


async def _mesures_vers(origine: dict, candidats: list[dict], mode: str, base: dict) -> list[dict | None]:
    """Base précalculée d'abord ; appel IGN en direct seulement pour les paires absentes."""
    resultat: list[dict | None] = [None] * len(candidats)
    manquants = []
    for i, c in enumerate(candidats):
        o, d = _id(origine), _id(c)
        if o is not None and d is not None and (o, d) in base:
            resultat[i] = base[(o, d)]
        else:
            manquants.append(i)
    if manquants:
        live = await distances_routieres(_coord(origine), [_coord(candidats[i]) for i in manquants], mode)
        for i, m in zip(manquants, live):
            resultat[i] = m
    return resultat


async def _mesure(origine: dict, destination: dict, mode: str, base: dict) -> dict:
    m = (await _mesures_vers(origine, [destination], mode, base))[0]
    return m or estimation(_coord(origine), _coord(destination), mode)


def _candidats(courant: dict, restants: list[dict], base: dict, preselection: int) -> list[dict]:
    """Tous les lieux dont le trajet est déjà en base (gratuit), complétés par les plus
    proches à vol d'oiseau (présélection seulement) jusqu'à `preselection` candidats."""
    o = _id(courant)
    connus = [c for c in restants if o is not None and _id(c) is not None and (o, _id(c)) in base]
    if len(connus) >= preselection:
        return connus
    origine = _coord(courant)
    autres = sorted((c for c in restants if c not in connus),
                    key=lambda x: haversine_metres(*origine, *_coord(x)))
    return connus + autres[: preselection - len(connus)]


async def ordre_plus_proche_voisin_ign(
    lieux: list[dict], mode: str = "driving-car", preselection: int = 4
) -> list[dict]:
    """Ordonne les lieux : à chaque pas, on va vers le lieu le plus proche PAR LA ROUTE.

    Distances lues dans `distances_routieres` (précalcul IGN) ; appel IGN en direct
    seulement pour les paires absentes. Le premier lieu reste le premier.
    """
    if len(lieux) <= 2:
        return list(lieux)
    base = await charger_distances_lieux([_id(x) for x in lieux], mode)
    restants = list(lieux)
    ordre = [restants.pop(0)]
    while restants:
        courant = ordre[-1]
        candidats = _candidats(courant, restants, base, preselection)
        mesures = await _mesures_vers(courant, candidats, mode, base)
        meilleur, meilleure_cle = None, None
        for cand, m in zip(candidats, mesures):
            m = m or estimation(_coord(courant), _coord(cand), mode)
            cle_tri = m["duree_secondes"] if m.get("duree_secondes") is not None else m["distance_metres"]
            if meilleure_cle is None or cle_tri < meilleure_cle:
                meilleur, meilleure_cle = cand, cle_tri
        ordre.append(meilleur)
        restants.remove(meilleur)
    return ordre


async def optimiser_etapes_ign(
    etapes: list[dict], depart: dict | None, mode: str,
    temps_disponible_minutes, temps_visite_minutes, retour_depart: bool,
) -> tuple[list[dict], list[dict]]:
    """Choisit et ordonne les étapes qui tiennent dans le temps disponible, avec les
    durées de route IGN (précalculées si possible)."""
    if not temps_disponible_minutes or len(etapes) <= 1:
        return list(etapes), []

    budget_h = max(0.25, float(temps_disponible_minutes) / 60.0)
    temps_visites_h = len(etapes) * float(temps_visite_minutes) / 60.0
    if budget_h - temps_visites_h <= 0:
        return ([etapes[0]] if etapes else []), [x for x in etapes[1:]]

    base_db = await charger_distances_lieux(
        [_id(x) for x in etapes] + ([_id(depart)] if depart else []), mode
    )
    restants = list(etapes)
    if depart:
        choisi: list[dict] = []
        courant = depart
    else:
        choisi = [restants.pop(0)]
        courant = choisi[0]
    base = depart if depart else (choisi[0] if choisi else etapes[0])

    cumul_s = 0.0
    while restants:
        candidats = _candidats(courant, restants, base_db, 4)
        mesures = await _mesures_vers(courant, candidats, mode, base_db)
        meilleur, meilleure_mesure = None, None
        for cand, m in zip(candidats, mesures):
            m = m or estimation(_coord(courant), _coord(cand), mode)
            if meilleure_mesure is None or m["duree_secondes"] < meilleure_mesure["duree_secondes"]:
                meilleur, meilleure_mesure = cand, m
        prochain_s = cumul_s + float(meilleure_mesure["duree_secondes"])
        retour_s = 0.0
        if retour_depart:
            r = await _mesure(meilleur, base, mode, base_db)
            retour_s = float(r["duree_secondes"])
        heures = (prochain_s + retour_s) / 3600.0
        visites_h = (len(choisi) + 1) * float(temps_visite_minutes) / 60.0
        if heures + visites_h > budget_h and choisi:
            break
        choisi.append(meilleur)
        restants.remove(meilleur)
        cumul_s = prochain_s
        courant = meilleur

    ids_choisis = {int(x["id"]) for x in choisi if x.get("id") is not None}
    exclus = [x for x in etapes if x.get("id") is not None and int(x["id"]) not in ids_choisis]
    return choisi, exclus