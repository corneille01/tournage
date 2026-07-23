"""
backend/enrich_descriptions_lieux.py — Ajoute une courte description
Wikipédia du LIEU lui-même (son histoire, son architecture...), pas ce
qui s'y passe dans le film. C'est le genre d'info que Google Maps ne
propose jamais dans ce contexte précis (lié au tournage).

Recherche directement sur fr.wikipedia.org par le NOM du lieu (pas via
Wikidata — évite l'endpoint le moins fiable, voir historique).

Deux étapes, comme pour les anecdotes :
    1. python export_films_pour_anecdotes.py  (sur GitHub Actions, Neon accessible)
       → réutilise le même JSON, pas besoin d'un export séparé
    2. python enrich_descriptions_lieux.py  (en local, Wikipédia accessible)
       → génère descriptions_lieux.sql à importer dans Neon
"""

import asyncio
import json

import httpx

HEADERS = {
    "User-Agent": "CineTourBot/1.0 (https://github.com/corneille01/tournage; contact: [email protected]) httpx",
    "Accept": "application/json",
}


def _echapper_sql(texte: str) -> str:
    return texte.replace("\\", "\\\\").replace("'", "''")


async def _description_courte(nom_lieu: str) -> str | None:
    async with httpx.AsyncClient(timeout=15, headers=HEADERS) as client:
        # Trouve la meilleure page correspondant au nom du lieu
        resp = await client.get(
            "https://fr.wikipedia.org/w/api.php",
            params={"action": "query", "list": "search", "srsearch": nom_lieu, "srlimit": 1, "format": "json"},
        )
        try:
            resp.raise_for_status()
            resultats = resp.json().get("query", {}).get("search", [])
        except Exception:
            return None
        if not resultats:
            return None
        titre_page = resultats[0]["title"]

        # Récupère un court extrait introductif (avant la 1ère section)
        resp2 = await client.get(
            "https://fr.wikipedia.org/w/api.php",
            params={
                "action": "query", "prop": "extracts", "exintro": 1, "explaintext": 1,
                "exsentences": 3, "titles": titre_page, "format": "json",
            },
        )
        try:
            resp2.raise_for_status()
            pages = resp2.json().get("query", {}).get("pages", {})
        except Exception:
            return None
        extrait = next(iter(pages.values()), {}).get("extract", "").strip()
        return extrait or None


async def main():
    try:
        with open("films_pour_anecdotes.json", encoding="utf-8") as f:
            films = json.load(f)
    except FileNotFoundError:
        print(
            "❌ films_pour_anecdotes.json introuvable. Télécharge-le depuis "
            "l'artefact du workflow GitHub Actions 'Export films pour anecdotes'.",
            flush=True,
        )
        return

    lignes_sql = ["-- Généré par enrich_descriptions_lieux.py — à importer dans Neon"]
    trouvees = 0
    total = 0

    for film in films:
        for lieu in film["lieux"]:
            total += 1
            try:
                description = await _description_courte(lieu["nom"])
            except Exception as e:
                print(f"  ⚠️ {lieu['nom']}: {e}", flush=True)
                await asyncio.sleep(1)
                continue

            if description:
                texte = _echapper_sql(description + "\n\n(Source : Wikipédia, CC BY-SA)")
                lignes_sql.append(
                    f"UPDATE lieux_tournage SET description_wikipedia = '{texte}' WHERE id = {lieu['id']};"
                )
                trouvees += 1
                print(f"✓ {lieu['nom']}: description trouvée", flush=True)
            else:
                print(f"  · {lieu['nom']}: aucune description trouvée", flush=True)

            await asyncio.sleep(1)  # respecte l'API Wikipédia, service public partagé

    with open("descriptions_lieux.sql", "w", encoding="utf-8") as f:
        f.write("\n".join(lignes_sql))

    print(f"\nTerminé : {trouvees}/{total} description(s) trouvée(s), dans descriptions_lieux.sql", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
