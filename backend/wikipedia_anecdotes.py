"""
backend/wikipedia_anecdotes.py — Aide à trouver des anecdotes de
tournage, PAS à les publier automatiquement.

Pour chaque film publié, va chercher la page Wikipédia française liée
(via le QID Wikidata déjà en base) et en extrait la section "Tournage"
si elle existe. Affiche le texte brut à l'écran pour que tu le relises,
le reformules avec tes propres mots (droits d'auteur — ne recopie pas
Wikipédia mot pour mot dans le site public) et l'ajoutes toi-même en
base via UPDATE.

Ne modifie JAMAIS la base directement — c'est volontaire.

Usage :
    python wikipedia_anecdotes.py                  # tous les films publiés
    python wikipedia_anecdotes.py --film-id 24      # un seul film
"""

import argparse
import asyncio
import re

import httpx

from db import init_db_pool, close_db_pool, fetch_all

HEADERS = {"User-Agent": "CineTourBot/1.0 (contact: [email protected])"}


async def _qid_vers_titre_wikipedia_fr(qid: str) -> str | None:
    """Trouve le titre de la page Wikipédia FR liée à ce QID Wikidata."""
    async with httpx.AsyncClient(timeout=15, headers=HEADERS) as client:
        resp = await client.get(
            "https://www.wikidata.org/w/api.php",
            params={
                "action": "wbgetentities", "ids": qid, "props": "sitelinks",
                "sitefilter": "frwiki", "format": "json",
            },
        )
        data = resp.json()
        entite = data.get("entities", {}).get(qid, {})
        sitelink = entite.get("sitelinks", {}).get("frwiki")
        return sitelink["title"] if sitelink else None


async def _extraire_section_tournage(titre_page: str) -> str | None:
    """Récupère le texte brut de la page et isole la section 'Tournage'."""
    async with httpx.AsyncClient(timeout=20, headers=HEADERS) as client:
        resp = await client.get(
            "https://fr.wikipedia.org/w/api.php",
            params={
                "action": "query", "prop": "extracts", "explaintext": 1,
                "titles": titre_page, "format": "json",
            },
        )
        data = resp.json()
        pages = data.get("query", {}).get("pages", {})
        texte = next(iter(pages.values()), {}).get("extract", "")

    if not texte:
        return None

    # Cherche une section "Tournage" (ou "Lieux de tournage") dans le texte brut
    match = re.search(
        r"(Tournage|Lieux de tournage)\s*\n(.+?)(?=\n[A-ZÉÈ][a-zéèêàA-Z ]{3,40}\s*\n|\Z)",
        texte, re.DOTALL,
    )
    if not match:
        return None
    return match.group(2).strip()[:1500]


async def main(film_id: int | None):
    await init_db_pool()
    try:
        if film_id:
            films = await fetch_all(
                "SELECT id, titre, wikidata_qid FROM films WHERE id = %s", (film_id,)
            )
        else:
            films = await fetch_all(
                "SELECT id, titre, wikidata_qid FROM films WHERE statut = 'publie' AND wikidata_qid IS NOT NULL"
            )

        print(f"{len(films)} film(s) à vérifier\n", flush=True)

        for film in films:
            titre_page = await _qid_vers_titre_wikipedia_fr(film["wikidata_qid"])
            if not titre_page:
                continue

            section = await _extraire_section_tournage(titre_page)
            if section:
                print(f"═══ {film['titre']} (id={film['id']}) — Wikipédia: {titre_page} ═══")
                print(section)
                print(
                    f"\n→ Si utilisable : reformule avec tes mots, puis en base :\n"
                    f"  UPDATE lieux_tournage SET anecdote = '...' WHERE film_id = {film['id']} AND id = <lieu concerné>;\n"
                )

            await asyncio.sleep(1)  # respecte l'API Wikipédia, service public partagé
    finally:
        await close_db_pool()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--film-id", type=int, default=None)
    args = parser.parse_args()
    asyncio.run(main(args.film_id))