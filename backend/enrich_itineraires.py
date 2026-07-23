"""
backend/enrich_itineraires.py — Précalcule la distance ET la durée
piéton/voiture entre chaque lieu de tournage et ses commodités déjà
en cache (amenity_cache), via le service "table" d'OSRM (une requête
par lieu×catégorie×mode, pas une par commodité — jusqu'à 10 en un
appel).

Volontairement séparé et exécuté en tâche de fond : avec potentiellement
beaucoup de visiteurs simultanés, il est hors de question d'appeler
OSRM à chaque affichage de popup. Le site sert uniquement ces valeurs
déjà en base — voir /api/lieux/{id}/amenities.

Usage :
    python enrich_itineraires.py                  # tout ce qui manque
    python enrich_itineraires.py --lieu-id 42      # un seul lieu
    python enrich_itineraires.py --departement X   # un département
"""

import argparse
import asyncio

import httpx

from db import init_db_pool, close_db_pool, fetch_all, execute

OSRM_URL = "https://router.project-osrm.org"
_PROFILS = {"pied": "foot", "voiture": "driving"}
VITESSE_MARCHE_MS = 5000 / 3600  # 5 km/h — voir note dans main.py


async def _table_osrm(depart: tuple[float, float], destinations: list[tuple[float, float]], mode: str) -> list[dict | None]:
    profil = _PROFILS[mode]
    tous_points = [depart] + destinations
    coords = ";".join(f"{lon},{lat}" for lat, lon in tous_points)
    n = len(destinations)
    destinations_idx = ";".join(str(i) for i in range(1, n + 1))

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(
                f"{OSRM_URL}/table/v1/{profil}/{coords}",
                params={"sources": "0", "destinations": destinations_idx, "annotations": "distance,duration"},
            )
            resp.raise_for_status()
            data = resp.json()
        distances = data["distances"][0]
        durees = data["durations"][0]
    except Exception as e:
        print(f"  ⚠️ Table OSRM indisponible ({mode}): {e}", flush=True)
        return [None] * n

    resultats = []
    for i in range(n):
        if distances[i] is None:
            resultats.append(None)
            continue
        duree = distances[i] / VITESSE_MARCHE_MS if mode == "pied" else durees[i]
        resultats.append({"distance": round(distances[i]), "duree": round(duree)})
    return resultats


async def traiter_lieu_categorie(lieu_id: int, lat: float, lon: float, categorie: str, items: list[dict]) -> None:
    destinations = [(float(i["latitude"]), float(i["longitude"])) for i in items]

    for mode in ("pied", "voiture"):
        resultats = await _table_osrm((lat, lon), destinations, mode)
        for item, resultat in zip(items, resultats):
            if not resultat:
                continue
            colonne_distance = f"distance_{mode}_metres"
            colonne_duree = f"duree_{mode}_secondes"
            await execute(
                f"UPDATE amenity_cache SET {colonne_distance} = %s, {colonne_duree} = %s WHERE id = %s",
                (resultat["distance"], resultat["duree"], item["id"]),
            )
        await asyncio.sleep(1.1)  # OSRM public : limite à 1 req/s


async def main(lieu_id: int | None, departement: str | None):
    await init_db_pool()
    try:
        conditions = "distance_pied_metres IS NULL"
        params: list = []
        if lieu_id:
            conditions += " AND lt.id = %s"
            params.append(lieu_id)
        elif departement:
            conditions += " AND lt.departement = %s"
            params.append(departement)

        # Un lieu×catégorie à la fois (pas une ligne par commodité)
        lignes = await fetch_all(
            f"""
            SELECT DISTINCT lt.id AS lieu_id, lt.latitude, lt.longitude, ac.categorie
            FROM amenity_cache ac
            JOIN lieux_tournage lt ON lt.id = ac.lieu_tournage_id
            WHERE {conditions}
            """,
            tuple(params),
        )
        print(f"{len(lignes)} combinaison(s) lieu×catégorie à traiter", flush=True)

        for ligne in lignes:
            items = await fetch_all(
                "SELECT id, latitude, longitude FROM amenity_cache "
                "WHERE lieu_tournage_id = %s AND categorie = %s AND distance_pied_metres IS NULL",
                (ligne["lieu_id"], ligne["categorie"]),
            )
            if not items:
                continue
            print(f"→ lieu {ligne['lieu_id']} / {ligne['categorie']} ({len(items)} commodités)", flush=True)
            try:
                await traiter_lieu_categorie(
                    ligne["lieu_id"], float(ligne["latitude"]), float(ligne["longitude"]),
                    ligne["categorie"], items,
                )
            except Exception as e:
                print(f"  ⚠️ Erreur imprévue: {e} — passage au suivant", flush=True)
                continue
    finally:
        await close_db_pool()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--lieu-id", type=int, default=None)
    parser.add_argument("--departement", type=str, default=None)
    args = parser.parse_args()
    asyncio.run(main(args.lieu_id, args.departement))
