"""
backend/export_films_pour_anecdotes.py — Étape 1/2 : exporte la liste
des films (id, titre, QID Wikidata, lieux) dans un fichier JSON.

Se lance là où Neon est accessible (GitHub Actions). Ne touche jamais
Wikipédia — sépare le problème réseau en deux morceaux indépendants,
vu que Neon et Wikipédia ne sont jamais bloqués au même endroit en
même temps sur ce projet.

Usage :
    python export_films_pour_anecdotes.py
    → écrit films_pour_anecdotes.json
"""

import asyncio
import json

from db import init_db_pool, close_db_pool, fetch_all


async def main():
    await init_db_pool()
    try:
        films = await fetch_all(
            "SELECT id, titre, wikidata_qid FROM films WHERE statut = 'publie'"
        )
        resultat = []
        for film in films:
            lieux = await fetch_all(
                "SELECT id, nom FROM lieux_tournage WHERE film_id = %s", (film["id"],)
            )
            resultat.append({
                "id": film["id"],
                "titre": film["titre"],
                "wikidata_qid": film["wikidata_qid"],
                "lieux": [{"id": l["id"], "nom": l["nom"]} for l in lieux],
            })

        with open("films_pour_anecdotes.json", "w", encoding="utf-8") as f:
            json.dump(resultat, f, ensure_ascii=False, indent=2)

        print(f"{len(resultat)} film(s) exportés dans films_pour_anecdotes.json", flush=True)
    finally:
        await close_db_pool()


if __name__ == "__main__":
    asyncio.run(main())