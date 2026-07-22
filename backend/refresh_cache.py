"""
backend/refresh_cache.py — Remplit amenity_cache pour tous les lieux
de tournage qui n'ont pas encore de cache (ou dont le cache est vieux).

À lancer manuellement ou via un cron mensuel — PAS à chaque requête
visiteur. Les lieux de tournage bougent rarement, les commerces
autour changent peu d'un mois à l'autre : un rafraîchissement mensuel
est largement suffisant et évite de surcharger Overpass.

Optimisation importante : plusieurs films peuvent avoir été tournés
exactement au même endroit physique (ex: dix films différents avec
une scène à "Toulouse, place du Capitole"). Sans regroupement, on
interrogerait Overpass dix fois pour calculer exactement le même
résultat — d'où le regroupement par coordonnées ci-dessous, qui
réduit considérablement le nombre d'appels réels et donc le temps
total et la pression sur le serveur public.

Usage :
    python refresh_cache.py                          # tous les lieux
    python refresh_cache.py --lieu-id 42              # un seul lieu (tests)
    python refresh_cache.py --departement "Aude"      # un département
"""

import argparse
import asyncio
from collections import defaultdict

from db import init_db_pool, close_db_pool, fetch_all, execute
from overpass import find_nearby, _CATEGORY_TAGS


async def refresh_groupe(lieu_ids: list[int], lat: float, lon: float, nom: str) -> None:
    """
    Calcule les commodités UNE SEULE FOIS pour ces coordonnées, puis
    applique le même résultat à tous les lieu_tournage_id qui partagent
    ce point (plusieurs films tournés au même endroit physique).
    """
    suffixe = f" — appliqué à {len(lieu_ids)} lieux (mêmes coordonnées)" if len(lieu_ids) > 1 else ""
    print(f"→ {nom} ({', '.join(map(str, lieu_ids))}){suffixe}", flush=True)

    for categorie in _CATEGORY_TAGS:
        try:
            resultat = await find_nearby(lat, lon, categorie, top_n=10)
        except Exception as e:
            print(f"  ⚠️ {categorie}: {e}", flush=True)
            continue

        resultats = resultat["top"]
        stats = resultat["stats"]

        for lieu_id in lieu_ids:
            await execute(
                "DELETE FROM amenity_cache WHERE lieu_tournage_id = %s AND categorie = %s",
                (lieu_id, categorie),
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
                        lieu_id, categorie, item["nom"],
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
                    lieu_id, categorie, stats["rayon_metres"], stats["nombre_total"],
                    stats["nombre_500m"], stats["nombre_1000m"],
                    stats["distance_min_m"], stats["distance_moy_top10_m"],
                ),
            )

        print(
            f"  ✓ {categorie}: {stats['nombre_total']} trouvés au total, "
            f"{len(resultats)} conservés, le plus proche à {stats['distance_min_m']}m",
            flush=True,
        )

        # Overpass est un service public partagé, et on a déjà des
        # tentatives automatiques sur 429/503/504 — mais mieux vaut
        # aussi ralentir le rythme de base pour déclencher ces erreurs
        # moins souvent (5s entre chaque catégorie).
        await asyncio.sleep(5)


def _grouper_par_coordonnees(lieux: list[dict]) -> list[tuple[list[int], float, float, str]]:
    """
    Regroupe les lieux qui partagent EXACTEMENT les mêmes coordonnées
    (arrondies à ~10m près) — typiquement plusieurs films tournés au
    même endroit physique. Retourne une liste de
    (liste_d_ids, latitude, longitude, nom_pour_affichage).
    """
    groupes: dict[tuple, dict] = defaultdict(lambda: {"ids": [], "nom": None})
    for lieu in lieux:
        cle = (round(float(lieu["latitude"]), 4), round(float(lieu["longitude"]), 4))
        groupes[cle]["ids"].append(lieu["id"])
        if groupes[cle]["nom"] is None:
            groupes[cle]["nom"] = lieu["nom"]

    return [
        (donnees["ids"], lat, lon, donnees["nom"])
        for (lat, lon), donnees in groupes.items()
    ]


async def main(lieu_id: int | None, departement: str | None):
    await init_db_pool()
    try:
        if lieu_id:
            lieux = await fetch_all(
                "SELECT id, nom, latitude, longitude FROM lieux_tournage WHERE id = %s",
                (lieu_id,),
            )
        elif departement:
            lieux = await fetch_all(
                "SELECT id, nom, latitude, longitude FROM lieux_tournage WHERE departement = %s",
                (departement,),
            )
        else:
            lieux = await fetch_all(
                "SELECT id, nom, latitude, longitude FROM lieux_tournage"
            )

        groupes = _grouper_par_coordonnees(lieux)
        economie = len(lieux) - len(groupes)
        print(
            f"{len(lieux)} lieu(x) → {len(groupes)} coordonnée(s) unique(s) à interroger"
            + (f" ({economie} doublon(s) de coordonnées évités)" if economie > 0 else ""),
            flush=True,
        )

        for lieu_ids, lat, lon, nom in groupes:
            await refresh_groupe(lieu_ids, lat, lon, nom)
    finally:
        await close_db_pool()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--lieu-id", type=int, default=None)
    parser.add_argument("--departement", type=str, default=None)
    args = parser.parse_args()
    asyncio.run(main(args.lieu_id, args.departement))