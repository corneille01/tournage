"""
backend/enrich_photos.py — Ajoute une photo à certaines commodités,
depuis Wikimedia Commons (gratuit, légal, pas de clé API).

Volontairement SÉPARÉ de refresh_cache.py : ajouter un appel photo
pour chacune des ~10 commodités × 12 catégories aurait multiplié par
10 le nombre d'appels réseau déjà tendu avec Overpass (c'est ce qui
causait les timeouts sur les gros départements). Ici, on ne cherche
une photo QUE pour le rang 1 (le plus proche) de chaque catégorie —
c'est lui qui est mis en avant dans l'interface, donc le plus rentable
à illustrer.

Une recherche géographique dans un rayon de 75m autour du point ne
trouve pas toujours de photo (Commons est loin d'être exhaustif sur
les petits commerces) — c'est normal, pas un bug, ne pas s'attendre à
100% de couverture.

Usage :
    python enrich_photos.py                       # tous les "rang 1" sans photo
    python enrich_photos.py --lieu-id 42           # un seul lieu
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


async def main(lieu_id: int | None):
    await init_db_pool()
    try:
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

        trouvees = 0
        for c in commodites:
            try:
                url = await _chercher_photo_commons(float(c["latitude"]), float(c["longitude"]))
            except Exception as e:
                print(f"  ⚠️ {c['nom']}: {e}", flush=True)
                await asyncio.sleep(1)
                continue

            if url:
                await execute(
                    "UPDATE amenity_cache SET photo_url = %s WHERE id = %s",
                    (url, c["id"]),
                )
                trouvees += 1
                print(f"  ✓ {c['nom']}: photo trouvée", flush=True)
            else:
                print(f"  · {c['nom']}: aucune photo à proximité", flush=True)

            await asyncio.sleep(1)  # Wikimedia est un service public partagé

        print(f"\nTerminé : {trouvees}/{len(commodites)} avec une photo trouvée.", flush=True)
    finally:
        await close_db_pool()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--lieu-id", type=int, default=None)
    args = parser.parse_args()
    asyncio.run(main(args.lieu_id))