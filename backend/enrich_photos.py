"""
backend/enrich_photos.py — Ajoute une photo depuis Wikimedia Commons
(gratuit, légal, pas de clé API), pour deux cibles possibles :

  - "commodites" : le rang 1 (le plus proche) de chaque catégorie
    d'amenity_cache. Volontairement limité à ça (pas les 10 par
    catégorie) pour ne pas aggraver les temps déjà tendus avec
    Overpass ailleurs dans le projet.
  - "lieux" : les lieux_tournage qui n'ont pas de photo — en
    complément de wikidata_ingest.py (propriété P18), qui couvre mal
    les lieux ruraux précis. Sert de repli, pas de remplacement.

Une recherche géographique dans un rayon de 75m autour du point ne
trouve pas toujours de photo (Commons est loin d'être exhaustif) —
c'est normal, pas un bug, ne pas s'attendre à 100% de couverture.

Usage :
    python enrich_photos.py                        # commodités, tous
    python enrich_photos.py --cible lieux           # lieux de tournage, tous
    python enrich_photos.py --cible tous            # les deux
    python enrich_photos.py --lieu-id 42            # un seul (commodités par défaut)
"""

import argparse
import asyncio

import httpx

from db import init_db_pool, close_db_pool, fetch_all, execute

HEADERS = {
    "User-Agent": "CineTourBot/1.0 (https://github.com/corneille01/tournage; contact: [email protected]) httpx",
    "Accept": "application/json",
}
COMMONS_API = "https://commons.wikimedia.org/w/api.php"


async def _chercher_photo_commons(lat: float, lon: float) -> str | None:
    """Cherche une image Commons dans un rayon de 75m — retourne l'URL directe du fichier, ou None."""
    async with httpx.AsyncClient(timeout=15, headers=HEADERS) as client:
        resp = await client.get(COMMONS_API, params={
            "action": "query",
            "generator": "geosearch",
            "ggscoord": f"{lat}|{lon}",
            "ggsradius": 75,
            "ggsnamespace": 6,  # espace de noms "Fichier"
            "ggslimit": 1,
            "prop": "imageinfo",
            "iiprop": "url",
            "format": "json",
        })
        try:
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"  ⚠️ Commons indisponible: {e}", flush=True)
            return None
        pages = data.get("query", {}).get("pages", {})
        if not pages:
            return None
        page = next(iter(pages.values()))
        imageinfo = page.get("imageinfo")
        return imageinfo[0]["url"] if imageinfo else None


async def _traiter(elements: list[dict], table: str) -> int:
    trouvees = 0
    for e in elements:
        try:
            url = await _chercher_photo_commons(float(e["latitude"]), float(e["longitude"]))
        except Exception as ex:
            print(f"  ⚠️ {e['nom']}: {ex}", flush=True)
            await asyncio.sleep(1)
            continue

        if url:
            await execute(f"UPDATE {table} SET photo_url = %s WHERE id = %s", (url, e["id"]))
            trouvees += 1
            print(f"  ✓ {e['nom']}: photo trouvée", flush=True)
        else:
            print(f"  · {e['nom']}: aucune photo à proximité", flush=True)

        await asyncio.sleep(1)  # Wikimedia est un service public partagé
    return trouvees


async def main(lieu_id: int | None, cible: str):
    await init_db_pool()
    try:
        if cible in ("commodites", "tous"):
            conditions = "rang = 1 AND photo_url IS NULL"
            params: tuple = ()
            if lieu_id:
                conditions += " AND lieu_tournage_id = %s"
                params = (lieu_id,)
            commodites = await fetch_all(
                f"SELECT id, nom, latitude, longitude FROM amenity_cache WHERE {conditions}",
                params,
            )
            print(f"{len(commodites)} commodité(s) 'plus proche' sans photo à traiter", flush=True)
            trouvees = await _traiter(commodites, "amenity_cache")
            print(f"Terminé (commodités) : {trouvees}/{len(commodites)} avec une photo trouvée.\n", flush=True)

        if cible in ("lieux", "tous"):
            conditions = "photo_url IS NULL"
            params = ()
            if lieu_id:
                conditions += " AND id = %s"
                params = (lieu_id,)
            lieux = await fetch_all(
                f"SELECT id, nom, latitude, longitude FROM lieux_tournage WHERE {conditions}",
                params,
            )
            print(f"{len(lieux)} lieu(x) de tournage sans photo à traiter", flush=True)
            trouvees = await _traiter(lieux, "lieux_tournage")
            print(f"Terminé (lieux) : {trouvees}/{len(lieux)} avec une photo trouvée.", flush=True)
    finally:
        await close_db_pool()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--lieu-id", type=int, default=None)
    parser.add_argument("--cible", choices=["commodites", "lieux", "tous"], default="commodites")
    args = parser.parse_args()
    asyncio.run(main(args.lieu_id, args.cible))