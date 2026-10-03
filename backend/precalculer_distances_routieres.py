"""
backend/precalculer_distances_routieres.py

Précalcule, avec la Géoplateforme IGN, les distances de ROUTE dont le parcours a
besoin et que enrich_itineraires.py (lieu -> commodités) ne couvre pas :

  - lieu  -> lieu : les VOISINS_PAR_LIEU lieux les plus proches de chaque lieu
                    (ordre des étapes, sélection sous contrainte de temps) ;
  - guide -> lieu : les lieux situés dans la zone d'intervention de chaque guide
                    (au moins les NB_LIEUX_MIN_GUIDE plus proches, au plus NB_LIEUX_MAX_GUIDE).

Le vol d'oiseau n'est utilisé ici que pour CHOISIR quels trajets calculer ; la
distance enregistrée est toujours celle de l'itinéraire IGN.

Incrémental : seuls les trajets absents de la table `distances_routieres` sont
calculés. Un lancement quotidien ne traite donc que les nouveaux lieux / guides.
Le script s'arrête proprement au bout de --duree-max-minutes et reprend le
lendemain exactement où il s'est arrêté.

Usage :
    python precalculer_distances_routieres.py                     # tout ce qui manque
    python precalculer_distances_routieres.py --lieu-id 42        # un lieu (et ses voisins)
    python precalculer_distances_routieres.py --guide-id 7        # un guide
    python precalculer_distances_routieres.py --recalculer        # repart de zéro
"""
from __future__ import annotations

import argparse
import asyncio
import math
import time

from db import init_db_pool, close_db_pool, fetch_all, execute, executemany
from geoplateforme import calculer_itineraire, GeoplateformeError

# L'API itinéraire accepte 10 req/s par IP : on reste très en dessous.
CONCURRENCE_MAX = 4
DELAI_APRES_APPEL = 0.35

VOISINS_PAR_LIEU = 12              # lieux les plus proches calculés pour chaque lieu
RAYON_MAX_VOISINS_KM = 150         # au-delà, on ne précalcule pas (repli géré par l'API)
RAYON_PIED_KM = 8                  # trajets à pied seulement pour des lieux proches
NB_LIEUX_MIN_GUIDE = 20
NB_LIEUX_MAX_GUIDE = 150
RAYON_GUIDE_MIN_KM = 30
TAILLE_LOT = 200

MODE_API = {"pied": "foot-walking", "voiture": "driving-car"}


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


# ─────────────────────────────────────────────────────────────
# Quels trajets faut-il ?
# ─────────────────────────────────────────────────────────────

def _trajets_lieux(lieux: list[dict], seulement: int | None) -> list[tuple]:
    """(type, origine_id, origine, dest_id, dest, mode, distance_vol_m)"""
    trajets = []
    for a in lieux:
        if seulement is not None and a["id"] != seulement:
            continue
        voisins = []
        for b in lieux:
            if b["id"] == a["id"]:
                continue
            d = _haversine_m(a["lat"], a["lon"], b["lat"], b["lon"])
            if d <= RAYON_MAX_VOISINS_KM * 1000:
                voisins.append((d, b))
        voisins.sort(key=lambda x: x[0])
        for d, b in voisins[:VOISINS_PAR_LIEU]:
            trajets.append(("lieu", a["id"], (a["lat"], a["lon"]), b["id"], (b["lat"], b["lon"]), "voiture", d))
            if d <= RAYON_PIED_KM * 1000:
                trajets.append(("lieu", a["id"], (a["lat"], a["lon"]), b["id"], (b["lat"], b["lon"]), "pied", d))
    return trajets


def _trajets_guides(guides: list[dict], lieux: list[dict], seulement: int | None) -> list[tuple]:
    trajets = []
    for g in guides:
        if seulement is not None and g["id"] != seulement:
            continue
        rayon_m = max(g["rayon_km"], RAYON_GUIDE_MIN_KM) * 1000
        classes = sorted(
            ((_haversine_m(g["lat"], g["lon"], l["lat"], l["lon"]), l) for l in lieux),
            key=lambda x: x[0],
        )
        retenus = [(d, l) for d, l in classes if d <= rayon_m][:NB_LIEUX_MAX_GUIDE]
        if len(retenus) < NB_LIEUX_MIN_GUIDE:
            retenus = classes[:NB_LIEUX_MIN_GUIDE]
        for d, l in retenus:
            trajets.append(("guide", g["id"], (g["lat"], g["lon"]), l["id"], (l["lat"], l["lon"]), "voiture", d))
    return trajets


# ─────────────────────────────────────────────────────────────
# Calcul IGN
# ─────────────────────────────────────────────────────────────

async def _calculer(trajet: tuple, semaphore: asyncio.Semaphore):
    type_o, origine_id, origine, dest_id, dest, mode, d_vol = trajet
    if d_vol < 5:  # même position : pas la peine d'interroger l'IGN
        return (type_o, origine_id, dest_id, mode, 0, 0)
    async with semaphore:
        try:
            res = await calculer_itineraire(
                depart_lat=origine[0], depart_lon=origine[1],
                arrivee_lat=dest[0], arrivee_lon=dest[1],
                mode=MODE_API[mode], avec_etapes=False,
            )
        except GeoplateformeError as exc:
            print(f"  ⚠️ IGN indisponible ({type_o} {origine_id} → {dest_id}, {mode}) : {exc}", flush=True)
            return None
        except Exception as exc:
            print(f"  ⚠️ Erreur ({type_o} {origine_id} → {dest_id}, {mode}) : {exc}", flush=True)
            return None
        finally:
            await asyncio.sleep(DELAI_APRES_APPEL)
    if not res or res.get("distance_metres") is None:
        return None
    duree = res.get("duree_secondes")
    return (type_o, origine_id, dest_id, mode,
            round(float(res["distance_metres"])), round(float(duree)) if duree is not None else None)


async def _enregistrer(lignes: list[tuple]) -> None:
    await executemany(
        """
        INSERT INTO distances_routieres
            (type_origine, origine_id, lieu_destination_id, mode, distance_metres, duree_secondes, calcule_le)
        VALUES (%s, %s, %s, %s, %s, %s, now())
        ON CONFLICT (type_origine, origine_id, lieu_destination_id, mode)
        DO UPDATE SET distance_metres = EXCLUDED.distance_metres,
                      duree_secondes  = EXCLUDED.duree_secondes,
                      calcule_le      = now()
        """,
        lignes,
    )


# ─────────────────────────────────────────────────────────────
# Programme principal
# ─────────────────────────────────────────────────────────────

async def main(lieu_id: int | None, guide_id: int | None, duree_max_min: float, recalculer: bool) -> None:
    await init_db_pool()
    debut = time.monotonic()
    try:
        if recalculer:
            await execute("DELETE FROM distances_routieres")
            print("Table vidée : tout sera recalculé.", flush=True)

        # Nettoyage : guides supprimés (pas de clé étrangère sur origine_id).
        await execute(
            "DELETE FROM distances_routieres WHERE type_origine = 'guide' "
            "AND origine_id NOT IN (SELECT id FROM guides)"
        )

        lieux = [
            {"id": int(r["id"]), "lat": float(r["latitude"]), "lon": float(r["longitude"])}
            for r in await fetch_all("SELECT id, latitude, longitude FROM lieux_tournage")
        ]
        guides = [
            {"id": int(r["id"]), "lat": float(r["latitude"]), "lon": float(r["longitude"]),
             "rayon_km": int(r["rayon_intervention_km"] or 30)}
            for r in await fetch_all(
                "SELECT id, latitude, longitude, rayon_intervention_km FROM guides WHERE statut = 'actif'"
            )
        ]
        print(f"{len(lieux)} lieu(x), {len(guides)} guide(s) actif(s)", flush=True)

        souhaites = []
        if guide_id is None:
            souhaites += _trajets_lieux(lieux, lieu_id)
        if lieu_id is None:
            souhaites += _trajets_guides(guides, lieux, guide_id)

        deja = {
            (r["type_origine"], int(r["origine_id"]), int(r["lieu_destination_id"]), r["mode"])
            for r in await fetch_all(
                "SELECT type_origine, origine_id, lieu_destination_id, mode FROM distances_routieres"
            )
        }
        a_faire = [t for t in souhaites if (t[0], t[1], t[3], t[5]) not in deja]
        # Les plus proches d'abord : ce sont les plus utiles si le budget de temps s'épuise.
        a_faire.sort(key=lambda t: t[6])
        print(f"{len(souhaites)} trajet(s) souhaité(s), {len(a_faire)} à calculer", flush=True)

        semaphore = asyncio.Semaphore(CONCURRENCE_MAX)
        ok = echecs = 0
        for i in range(0, len(a_faire), TAILLE_LOT):
            if (time.monotonic() - debut) / 60.0 >= duree_max_min:
                print(f"⏱️ Budget de {duree_max_min:.0f} min atteint : reprise au prochain lancement.", flush=True)
                break
            lot = a_faire[i:i + TAILLE_LOT]
            resultats = await asyncio.gather(*(_calculer(t, semaphore) for t in lot))
            lignes = [r for r in resultats if r]
            echecs += len(resultats) - len(lignes)
            ok += len(lignes)
            if lignes:
                await _enregistrer(lignes)
            print(f"  … {min(i + TAILLE_LOT, len(a_faire))}/{len(a_faire)} (ok {ok}, échecs {echecs})", flush=True)

        reste = len(a_faire) - ok
        print(f"Terminé : {ok} trajet(s) enregistré(s), {reste} restant(s) (repris au prochain lancement).", flush=True)
    finally:
        await close_db_pool()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Précalcul des distances de route IGN pour le parcours")
    ap.add_argument("--lieu-id", type=int, default=None, help="Ne traiter que ce lieu comme origine")
    ap.add_argument("--guide-id", type=int, default=None, help="Ne traiter que ce guide")
    ap.add_argument("--duree-max-minutes", type=float, default=330.0,
                    help="Arrêt propre après cette durée (reprise au lancement suivant)")
    ap.add_argument("--recalculer", action="store_true", help="Vider la table et tout recalculer")
    a = ap.parse_args()
    asyncio.run(main(a.lieu_id, a.guide_id, a.duree_max_minutes, a.recalculer))