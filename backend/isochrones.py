"""
backend/isochrones.py

Précalcule les zones accessibles autour des lieux de tournage.

Voiture :
    10 / 20 / 30 minutes

À pied :
    10 / 20 minutes

Les résultats sont stockés dans PostgreSQL.
"""

from __future__ import annotations

import argparse
import asyncio
import json

from db import (
    init_db_pool,
    close_db_pool,
    fetch_all,
    execute,
)

from geoplateforme import (
    calculer_isochrone,
    GeoplateformeError,
)


DELAI = 0.25


ISOCHRONES = {
    "driving-car": (10, 20, 30),
    "foot-walking": (10, 20),
}


async def calculer_pour_lieu(
    lieu: dict,
) -> None:

    lieu_id = lieu["id"]

    lat = float(lieu["latitude"])
    lon = float(lieu["longitude"])

    for mode, durees in ISOCHRONES.items():

        for minutes in durees:

            print(
                f"→ lieu {lieu_id} / "
                f"{mode} / {minutes} min",
                flush=True,
            )

            try:

                resultat = await calculer_isochrone(
                    lat=lat,
                    lon=lon,
                    mode=mode,
                    minutes=minutes,
                )

                geometry = resultat["geometry"]

                if geometry.get("type") == "Feature":
                    geometry = geometry["geometry"]

                elif geometry.get("type") == "FeatureCollection":

                    features = geometry.get("features") or []

                    if not features:
                        raise ValueError(
                            "FeatureCollection vide"
                        )

                    geometry = features[0]["geometry"]

                await execute(
                    """
                    INSERT INTO isochrones (
                        lieu_tournage_id,
                        mode,
                        minutes,
                        geometry_geojson,
                        provider,
                        calculated_at
                    )
                    VALUES (
                        %s,
                        %s,
                        %s,
                        %s::jsonb,
                        %s,
                        NOW()
                    )
                    ON CONFLICT (
                        lieu_tournage_id,
                        mode,
                        minutes
                    )
                    DO UPDATE SET
                        geometry_geojson = EXCLUDED.geometry_geojson,
                        provider = EXCLUDED.provider,
                        calculated_at = NOW()
                    """,
                    (
                        lieu_id,
                        mode,
                        minutes,
                        json.dumps(geometry),
                        "geoplateforme",
                    ),
                )

                await asyncio.sleep(DELAI)

            except GeoplateformeError as exc:

                print(
                    f"  ⚠️ Géoplateforme : {exc}",
                    flush=True,
                )

            except Exception as exc:

                print(
                    f"  ⚠️ Erreur isochrone : {exc}",
                    flush=True,
                )


async def main(lieu_id: int | None = None):

    await init_db_pool()

    try:

        if lieu_id:

            lieux = await fetch_all(
                """
                SELECT
                    id,
                    latitude,
                    longitude
                FROM lieux_tournage
                WHERE id = %s
                """,
                (lieu_id,),
            )

        else:

            lieux = await fetch_all(
                """
                SELECT
                    id,
                    latitude,
                    longitude
                FROM lieux_tournage
                WHERE latitude IS NOT NULL
                  AND longitude IS NOT NULL
                ORDER BY id
                """
            )

        print(
            f"{len(lieux)} lieu(x) à traiter.",
            flush=True,
        )

        for lieu in lieux:

            await calculer_pour_lieu(lieu)

    finally:

        await close_db_pool()


if __name__ == "__main__":

    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--lieu-id",
        type=int,
        default=None,
    )

    args = parser.parse_args()

    asyncio.run(
        main(args.lieu_id)
    )