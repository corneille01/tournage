"""
backend/calculer_amenities_datatourisme.py — Pour chaque lieu de
tournage, trouve les 10 objets DATAtourisme les plus proches (par
catégorie déjà importée) et les écrit dans amenity_cache — même
table, même format que ce que produisait refresh_cache.py avec
Overpass, donc AUCUN changement nécessaire côté frontend/analyse.

Tout se fait en SQL local (formule de Haversine) — aucun appel
réseau, contrairement à Overpass.

Usage :
    python calculer_amenities_datatourisme.py --categorie hebergement
    python calculer_amenities_datatourisme.py --categorie hebergement --lieu-id 42
"""

import argparse
import asyncio

from db import init_db_pool, close_db_pool, fetch_all, execute

RAYON_PAR_DEFAUT_M = {
    "hebergement": 15_000,
    "restaurant": 8_000,
    "activite": 15_000,
    "office_tourisme": 20_000,
}


async def main(categorie: str, lieu_id: int | None):
    await init_db_pool()
    try:
        if lieu_id:
            lieux = await fetch_all(
                "SELECT id, nom, latitude, longitude FROM lieux_tournage WHERE id = %s", (lieu_id,)
            )
        else:
            lieux = await fetch_all("SELECT id, nom, latitude, longitude FROM lieux_tournage")

        rayon = RAYON_PAR_DEFAUT_M.get(categorie, 15_000)
        print(f"{len(lieux)} lieu(x) à traiter (catégorie: {categorie}, rayon: {rayon}m)", flush=True)

        for i, lieu in enumerate(lieux, start=1):
            resultats = await fetch_all(
                """
                SELECT nom, latitude, longitude, site_web, telephone, email,
                       horaires, tarif_min, tarif_max, devise, adresse, distance_metres
                FROM (
                    SELECT
                        nom, latitude, longitude, site_web, telephone, email,
                        horaires, tarif_min, tarif_max, devise, adresse,
                        (
                            6371000 * acos(
                                LEAST(1.0, GREATEST(-1.0,
                                    cos(radians(%s)) * cos(radians(latitude)) *
                                    cos(radians(longitude) - radians(%s)) +
                                    sin(radians(%s)) * sin(radians(latitude))
                                ))
                            )
                        )::int AS distance_metres
                    FROM datatourisme_objets
                    WHERE categorie = %s
                ) AS avec_distance
                WHERE distance_metres <= %s
                ORDER BY distance_metres ASC
                LIMIT 10
                """,
                (
                    float(lieu["latitude"]), float(lieu["longitude"]), float(lieu["latitude"]),
                    categorie, rayon,
                ),
            )

            total = await fetch_all(
                """
                SELECT COUNT(*) AS total FROM (
                    SELECT
                        (
                            6371000 * acos(
                                LEAST(1.0, GREATEST(-1.0,
                                    cos(radians(%s)) * cos(radians(latitude)) *
                                    cos(radians(longitude) - radians(%s)) +
                                    sin(radians(%s)) * sin(radians(latitude))
                                ))
                            )
                        ) AS distance_metres
                    FROM datatourisme_objets
                    WHERE categorie = %s
                ) AS avec_distance
                WHERE distance_metres <= %s
                """,
                (float(lieu["latitude"]), float(lieu["longitude"]), float(lieu["latitude"]), categorie, rayon),
            )
            nombre_total = total[0]["total"] if total else 0

            await execute(
                "DELETE FROM amenity_cache WHERE lieu_tournage_id = %s AND categorie = %s",
                (lieu["id"], categorie),
            )
            for rang, r in enumerate(resultats, start=1):
                await execute(
                    """
                    INSERT INTO amenity_cache
                        (lieu_tournage_id, categorie, nom, latitude, longitude,
                         distance_metres, site_web, telephone, email, horaires,
                         tarif_min, tarif_max, devise, adresse, rang)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        lieu["id"], categorie, r["nom"], r["latitude"], r["longitude"],
                        r["distance_metres"], r["site_web"], r["telephone"], r["email"],
                        r["horaires"], r["tarif_min"], r["tarif_max"], r["devise"],
                        r["adresse"], rang,
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
                    distance_min_m = EXCLUDED.distance_min_m,
                    distance_moy_top10_m = EXCLUDED.distance_moy_top10_m
                """,
                (
                    lieu["id"], categorie, rayon, nombre_total,
                    sum(1 for r in resultats if r["distance_metres"] <= 500),
                    sum(1 for r in resultats if r["distance_metres"] <= 1000),
                    resultats[0]["distance_metres"] if resultats else None,
                    round(sum(r["distance_metres"] for r in resultats) / len(resultats)) if resultats else None,
                ),
            )

            if i % 20 == 0:
                print(f"  … {i}/{len(lieux)} lieux traités", flush=True)

        print(f"\nTerminé : {len(lieux)} lieu(x) traités pour '{categorie}'.", flush=True)
    finally:
        await close_db_pool()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--categorie", required=True)
    parser.add_argument("--lieu-id", type=int, default=None)
    args = parser.parse_args()
    asyncio.run(main(args.categorie, args.lieu_id))
