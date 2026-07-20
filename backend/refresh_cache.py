"""
backend/refresh_cache.py — Remplit amenity_cache pour tous les lieux
de tournage qui n'ont pas encore de cache (ou dont le cache est vieux).

À lancer manuellement ou via un cron mensuel — PAS à chaque requête
visiteur. Les lieux de tournage bougent rarement, les commerces
autour changent peu d'un mois à l'autre : un rafraîchissement mensuel
est largement suffisant et évite de surcharger Overpass.

Usage :
    python refresh_cache.py                  # tous les lieux
    python refresh_cache.py --lieu-id 42     # un seul lieu (tests)
"""

import argparse
import asyncio

from db import init_db_pool, close_db_pool, fetch_all, execute
from overpass import find_nearby, _CATEGORY_TAGS


async def refresh_lieu(lieu: dict) -> None:
    print(f"→ {lieu['nom']} ({lieu['id']})", flush=True)
    for categorie in _CATEGORY_TAGS:
        try:
            resultat = await find_nearby(
                lieu["latitude"], lieu["longitude"], categorie, top_n=10
            )
        except Exception as e:
            print(f"  ⚠️ {categorie}: {e}", flush=True)
            continue

        resultats = resultat["top"]
        stats = resultat["stats"]

        # On repart de zéro pour cette catégorie à chaque rafraîchissement
        await execute(
            "DELETE FROM amenity_cache WHERE lieu_tournage_id = %s AND categorie = %s",
            (lieu["id"], categorie),
        )

        for rang, item in enumerate(resultats, start=1):
            await execute(
                """
                INSERT INTO amenity_cache
                    (lieu_tournage_id, categorie, nom, latitude, longitude,
                     distance_metres, osm_id, adresse, telephone, site_web, rang)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    lieu["id"], categorie, item["nom"],
                    item["latitude"], item["longitude"], item["distance_metres"],
                    item["osm_id"], item["adresse"], item["telephone"], item["site_web"],
                    rang,
                ),
            )

        await execute(
            """
            INSERT INTO amenity_stats
                (lieu_tournage_id, categorie, rayon_metres, nombre_total,
                 nombre_500m, nombre_1000m, distance_min_m, distance_moy_top10_m)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (lieu_tournage_id, categorie) DO UPDATE SET
                rayon_metres = EXCLUDED.rayon_metres,
                nombre_total = EXCLUDED.nombre_total,
                nombre_500m = EXCLUDED.nombre_500m,
                nombre_1000m = EXCLUDED.nombre_1000m,
                distance_min_m = EXCLUDED.distance_min_m,
                distance_moy_top10_m = EXCLUDED.distance_moy_top10_m
            """,
            (
                lieu["id"], categorie, stats["rayon_metres"], stats["nombre_total"],
                stats["nombre_500m"], stats["nombre_1000m"],
                stats["distance_min_m"], stats["distance_moy_top10_m"],
            ),
        )

        print(
            f"  ✓ {categorie}: {stats['nombre_total']} trouvés au total, "
            f"{len(resultats)} conservés, le plus proche à {stats['distance_min_m']}m",
            flush=True,
        )

        # Overpass est un service public partagé — on ne le sature pas
        await asyncio.sleep(1)


async def main(lieu_id: int | None):
    await init_db_pool()
    try:
        if lieu_id:
            lieux = await fetch_all(
                "SELECT id, nom, latitude, longitude FROM lieux_tournage WHERE id = %s",
                (lieu_id,),
            )
        else:
            lieux = await fetch_all(
                "SELECT id, nom, latitude, longitude FROM lieux_tournage"
            )

        print(f"{len(lieux)} lieu(x) à traiter", flush=True)
        for lieu in lieux:
            await refresh_lieu(lieu)
    finally:
        await close_db_pool()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--lieu-id", type=int, default=None)
    args = parser.parse_args()
    asyncio.run(main(args.lieu_id))