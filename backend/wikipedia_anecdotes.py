"""
backend/wikipedia_anecdotes.py — Anecdotes de tournage automatiques,
depuis la section "Tournage" de Wikipédia (licence CC BY-SA, réemploi
autorisé avec attribution — pas un souci de droit d'auteur).

Automatisation complète pour les films à UN SEUL lieu de tournage (pas
d'ambiguïté possible). Pour les films à PLUSIEURS lieux, affiche le
texte pour attribution manuelle — la section Wikipédia parle souvent
de plusieurs lieux mélangés dans un seul paragraphe, impossible à
répartir automatiquement entre les bonnes lignes de la base.

Chaque anecdote publiée porte la mention "Source : Wikipédia" en fin
de texte, comme l'exige la licence CC BY-SA.

Usage :
    python wikipedia_anecdotes.py                  # tous les films publiés
    python wikipedia_anecdotes.py --film-id 24      # un seul film
"""

import argparse
import asyncio
import re

import httpx

from db import init_db_pool, close_db_pool, fetch_all, execute

HEADERS = {"User-Agent": "CineTourBot/1.0 (contact: [email protected])"}
MENTION_SOURCE = "\n\n(Source : Wikipédia, CC BY-SA)"


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
        try:
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"  ⚠️ Wikidata indisponible pour {qid}: {e} — ignoré", flush=True)
            return None
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
        try:
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"  ⚠️ Wikipédia indisponible pour '{titre_page}': {e} — ignoré", flush=True)
            return None
        pages = data.get("query", {}).get("pages", {})
        texte = next(iter(pages.values()), {}).get("extract", "")

    if not texte:
        return None

    match = re.search(
        r"(Tournage|Lieux de tournage)\s*\n(.+?)(?=\n[A-ZÉÈ][a-zéèêàA-Z ]{3,40}\s*\n|\Z)",
        texte, re.DOTALL,
    )
    if not match:
        return None
    return match.group(2).strip()[:1500]


async def main(film_id: int | None):
    await init_db_pool()
    auto_publies = 0
    a_traiter_manuellement = 0
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
            try:
                titre_page = await _qid_vers_titre_wikipedia_fr(film["wikidata_qid"])
                if not titre_page:
                    continue

                section = await _extraire_section_tournage(titre_page)
                if not section:
                    await asyncio.sleep(1)
                    continue

                lieux = await fetch_all(
                    "SELECT id, nom FROM lieux_tournage WHERE film_id = %s", (film["id"],)
                )

                if len(lieux) == 1:
                    # Aucune ambiguïté possible : publication automatique.
                    await execute(
                        "UPDATE lieux_tournage SET anecdote = %s WHERE id = %s",
                        (section + MENTION_SOURCE, lieux[0]["id"]),
                    )
                    auto_publies += 1
                    print(f"✓ {film['titre']} → publié automatiquement (lieu unique: {lieux[0]['nom']})", flush=True)
                else:
                    a_traiter_manuellement += 1
                    print(f"\n═══ {film['titre']} (id={film['id']}) — {len(lieux)} lieux, à répartir toi-même ═══")
                    print(section)
                    noms_lieux = ", ".join(f"{l['nom']} (id={l['id']})" for l in lieux)
                    print(f"Lieux disponibles : {noms_lieux}")
                    print(
                        f"→ UPDATE lieux_tournage SET anecdote = '... (Source : Wikipédia, CC BY-SA)' "
                        f"WHERE id = <lieu concerné parmi ceux ci-dessus>;\n"
                    )

                await asyncio.sleep(1)  # respecte l'API Wikipédia, service public partagé
            except Exception as e:
                print(f"⚠️ Erreur imprévue sur '{film['titre']}': {e} — passage au suivant", flush=True)
                await asyncio.sleep(1)
                continue

        print(f"\nTerminé : {auto_publies} publiée(s) automatiquement, {a_traiter_manuellement} à répartir toi-même.", flush=True)
    finally:
        await close_db_pool()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--film-id", type=int, default=None)
    args = parser.parse_args()
    asyncio.run(main(args.film_id))
