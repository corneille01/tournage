"""
backend/enrich_traductions_tmdb.py — Récupère le titre et le synopsis
de chaque film publié dans les langues de l'interface (en, es, de, zh),
depuis TMDB — qui les a déjà, comme pour le français.

Ne touche jamais aux colonnes françaises existantes (titre, synopsis)
— tout va dans la colonne i18n (JSONB), une clé par langue, sans
écraser ce qui existe déjà pour une langue si on relance le script.

Usage :
    python enrich_traductions_tmdb.py                  # tous les films publiés
    python enrich_traductions_tmdb.py --film-id 24      # un seul film
"""

import argparse
import asyncio
import json
import os

import httpx

from db import init_db_pool, close_db_pool, fetch_all, execute

TMDB_API_KEY = os.getenv("TMDB_API_KEY", "")
TMDB_BASE = "https://api.themoviedb.org/3"

# Code TMDB (langue-RÉGION) pour chaque langue de l'interface
LANGUES_TMDB = {
    "en": "en-US",
    "es": "es-ES",
    "de": "de-DE",
    "zh": "zh-CN",
}


async def _traduction_tmdb(tmdb_id: int, media_type: str, code_tmdb: str) -> dict | None:
    endpoint = "movie" if media_type == "movie" else "tv"
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            resp = await client.get(
                f"{TMDB_BASE}/{endpoint}/{tmdb_id}",
                params={"api_key": TMDB_API_KEY, "language": code_tmdb},
            )
            resp.raise_for_status()
            d = resp.json()
        except Exception as e:
            print(f"  ⚠️ TMDB {tmdb_id} ({code_tmdb}): {e}", flush=True)
            return None

    titre = d.get("title") or d.get("name")
    synopsis = d.get("overview")
    if not titre and not synopsis:
        return None
    return {"titre": titre, "synopsis": synopsis}


async def main(film_id: int | None):
    if not TMDB_API_KEY:
        print("❌ TMDB_API_KEY manquante.", flush=True)
        return

    await init_db_pool()
    try:
        if film_id:
            films = await fetch_all(
                "SELECT id, tmdb_id, media_type, i18n FROM films WHERE id = %s", (film_id,)
            )
        else:
            films = await fetch_all(
                "SELECT id, tmdb_id, media_type, i18n FROM films "
                "WHERE statut = 'publie' AND tmdb_id IS NOT NULL"
            )

        print(f"{len(films)} film(s) à traiter\n", flush=True)

        for film in films:
            i18n = film.get("i18n") or {}
            if isinstance(i18n, str):  # asyncpg peut renvoyer le JSONB en str selon le codec
                i18n = json.loads(i18n)

            modifie = False
            for langue, code_tmdb in LANGUES_TMDB.items():
                if langue in i18n and i18n[langue].get("titre"):
                    continue  # déjà traduit, ne pas rappeler TMDB pour rien
                traduction = await _traduction_tmdb(film["tmdb_id"], film["media_type"], code_tmdb)
                if traduction:
                    i18n[langue] = traduction
                    modifie = True
                await asyncio.sleep(0.3)  # TMDB tolère largement ce rythme, juste une marge de sécurité

            if modifie:
                await execute(
                    "UPDATE films SET i18n = %s WHERE id = %s",
                    (json.dumps(i18n), film["id"]),
                )
                print(f"✓ film {film['id']} : traductions mises à jour ({', '.join(i18n.keys())})", flush=True)

    finally:
        await close_db_pool()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--film-id", type=int, default=None)
    args = parser.parse_args()
    asyncio.run(main(args.film_id))
