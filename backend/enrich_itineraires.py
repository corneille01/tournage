"""
backend/enrich_itineraires.py

Précalcule la distance ET la durée :
- à pied
- en voiture

entre chaque lieu de tournage et ses commodités déjà présentes
dans amenity_cache.

Les calculs sont effectués via la Géoplateforme IGN.

IMPORTANT :
- aucun appel de navigation n'est effectué par un visiteur ;
- les résultats sont stockés dans amenity_cache ;
- le frontend lit ensuite directement PostgreSQL.

Usage :
    python enrich_itineraires.py
    python enrich_itineraires.py --lieu-id 42
    python enrich_itineraires.py --departement Ariège
"""

from __future__ import annotations

import argparse
import asyncio

from db import init_db_pool, close_db_pool, fetch_all, execute

from geoplateforme import (
    calculer_itineraire,
    GeoplateformeError,
)


# La limite officielle de l'API itinéraire est de 10 req/s par IP.
# On reste volontairement beaucoup plus bas pour ne pas brutaliser
# le service public.
CONCURRENCE_MAX = 4

# Petit délai entre les lots.
DELAI_ENTRE_LOTS = 0.35


# ─────────────────────────────────────────────────────────────
# Géoplateforme : un trajet
# ─────────────────────────────────────────────────────────────

async def _itineraire_geoplateforme(
    depart: tuple[float, float],
    destination: tuple[float, float],
    mode: str,
) -> dict | None:
    """
    Calcule un trajet réel via la Géoplateforme.

    depart/destination :
        (latitude, longitude)

    mode :
        "pied" ou "voiture"
    """

    mode_api = {
        "pied": "foot-walking",
        "voiture": "driving-car",
    }[mode]

    try:
        resultat = await calculer_itineraire(
            depart_lat=depart[0],
            depart_lon=depart[1],
            arrivee_lat=destination[0],
            arrivee_lon=destination[1],
            mode=mode_api,
            avec_etapes=False,
        )

        return resultat

    except GeoplateformeError as exc:
        print(
            f"  ⚠️ Géoplateforme indisponible ({mode}) : {exc}",
            flush=True,
        )
        return None

    except Exception as exc:
        print(
            f"  ⚠️ Erreur itinéraire ({mode}) : {exc}",
            flush=True,
        )
        return None


# ─────────────────────────────────────────────────────────────
# Calcul d'une commodité
# ─────────────────────────────────────────────────────────────

async def _calculer_commodite(
    depart: tuple[float, float],
    item: dict,
    mode: str,
    semaphore: asyncio.Semaphore,
) -> tuple[int, str, dict | None]:

    destination = (
        float(item["latitude"]),
        float(item["longitude"]),
    )

    async with semaphore:
        resultat = await _itineraire_geoplateforme(
            depart,
            destination,
            mode,
        )

        # Petit délai après chaque requête.
        await asyncio.sleep(DELAI_ENTRE_LOTS)

    return item["id"], mode, resultat


# ─────────────────────────────────────────────────────────────
# Traitement lieu × catégorie
# ─────────────────────────────────────────────────────────────

async def traiter_lieu_categorie(
    lieu_id: int,
    lat: float,
    lon: float,
    categorie: str,
    items: list[dict],
) -> None:

    depart = (
        float(lat),
        float(lon),
    )

    semaphore = asyncio.Semaphore(CONCURRENCE_MAX)

    print(
        f"  → Calcul Géoplateforme : "
        f"{len(items)} commodité(s) × 2 modes",
        flush=True,
    )

    # On calcule pied + voiture pour toutes les commodités.
    taches = []

    for item in items:
        for mode in ("pied", "voiture"):
            taches.append(
                _calculer_commodite(
                    depart,
                    item,
                    mode,
                    semaphore,
                )
            )

    resultats = await asyncio.gather(
        *taches,
        return_exceptions=True,
    )

    # Écriture des résultats en base.
    for resultat in resultats:

        if isinstance(resultat, Exception):
            print(
                f"  ⚠️ Calcul ignoré après exception : {resultat}",
                flush=True,
            )
            continue

        item_id, mode, data = resultat

        if not data:
            continue

        distance = data.get("distance_metres")
        duree = data.get("duree_secondes")

        if distance is None:
            print(
                f"  ⚠️ Pas de distance retournée "
                f"pour amenity_cache.id={item_id}",
                flush=True,
            )
            continue

        colonne_distance = f"distance_{mode}_metres"
        colonne_duree = f"duree_{mode}_secondes"

        await execute(
            f"""
            UPDATE amenity_cache
            SET
                {colonne_distance} = %s,
                {colonne_duree} = %s
            WHERE id = %s
            """,
            (
                round(float(distance)),
                round(float(duree)) if duree is not None else None,
                item_id,
            ),
        )


# ─────────────────────────────────────────────────────────────
# Programme principal
# ─────────────────────────────────────────────────────────────

async def main(
    lieu_id: int | None,
    departement: str | None,
):

    await init_db_pool()

    try:

        conditions = """
            distance_pied_metres IS NULL
            OR distance_voiture_metres IS NULL
        """

        params: list = []

        if lieu_id:
            conditions += " AND lt.id = %s"
            params.append(lieu_id)

        elif departement:
            conditions += " AND lt.departement = %s"
            params.append(departement)

        lignes = await fetch_all(
            f"""
            SELECT DISTINCT
                lt.id AS lieu_id,
                lt.latitude,
                lt.longitude,
                ac.categorie

            FROM amenity_cache ac

            JOIN lieux_tournage lt
                ON lt.id = ac.lieu_tournage_id

            WHERE {conditions}

            ORDER BY lt.id, ac.categorie
            """,
            tuple(params),
        )

        print(
            f"{len(lignes)} combinaison(s) "
            f"lieu × catégorie à traiter",
            flush=True,
        )

        for ligne in lignes:

            items = await fetch_all(
                """
                SELECT
                    id,
                    latitude,
                    longitude

                FROM amenity_cache

                WHERE
                    lieu_tournage_id = %s
                    AND categorie = %s
                    AND (
                        distance_pied_metres IS NULL
                        OR distance_voiture_metres IS NULL
                    )

                ORDER BY rang
                """,
                (
                    ligne["lieu_id"],
                    ligne["categorie"],
                ),
            )

            if not items:
                continue

            print(
                f"→ lieu {ligne['lieu_id']} / "
                f"{ligne['categorie']} "
                f"({len(items)} commodités)",
                flush=True,
            )

            try:

                await traiter_lieu_categorie(
                    ligne["lieu_id"],
                    float(ligne["latitude"]),
                    float(ligne["longitude"]),
                    ligne["categorie"],
                    items,
                )

            except Exception as exc:

                print(
                    f"  ⚠️ Erreur imprévue : {exc} "
                    f"— passage au suivant",
                    flush=True,
                )

                continue

    finally:
        await close_db_pool()


# ─────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":

    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--lieu-id",
        type=int,
        default=None,
    )

    parser.add_argument(
        "--departement",
        type=str,
        default=None,
    )

    args = parser.parse_args()

    asyncio.run(
        main(
            args.lieu_id,
            args.departement,
        )
    )