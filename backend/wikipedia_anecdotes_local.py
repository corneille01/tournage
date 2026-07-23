"""
backend/wikipedia_anecdotes_local.py — Étape 2/2 : à lancer sur ton
PC, PAS besoin de Neon ici (seulement Wikipédia/Wikidata, qui eux
passent bien depuis ton PC).

Lit le fichier exporté par le workflow GitHub Actions
"Export films pour anecdotes", cherche la section "Tournage" sur
Wikipédia pour chaque film, et génère un fichier .sql à importer
toi-même dans le SQL Editor de Neon (comme pour les autres imports
qu'on a déjà faits) — pas de connexion base requise dans ce script.

Pour les films à un seul lieu : le .sql contient directement l'UPDATE.
Pour les films à plusieurs lieux : affiché à l'écran pour que tu
choisisses toi-même le bon lieu (impossible à deviner automatiquement).

Usage :
    1. Télécharge "films_pour_anecdotes.json" depuis l'artefact du
       workflow GitHub Actions "Export films pour anecdotes", mets-le
       dans backend/.
    2. python wikipedia_anecdotes_local.py
    3. Importe le fichier anecdotes.sql généré dans Neon.
"""

import asyncio
import json
import re

import httpx

HEADERS = {
    "User-Agent": "CineTourBot/1.0 (https://github.com/corneille01/tournage; contact: [email protected]) httpx",
    "Accept": "application/json",
}
MENTION_SOURCE = "\n\n(Source : Wikipédia, CC BY-SA)"


def _echapper_sql(texte: str) -> str:
    return texte.replace("\\", "\\\\").replace("'", "''")


async def _chercher_page_wikipedia(titre_film: str) -> str | None:
    """
    Cherche directement sur Wikipédia FR à partir du titre du film
    (déjà connu, pas besoin de Wikidata pour ça) — évite complètement
    www.wikidata.org/w/api.php, qui bloque nos requêtes en 403 alors
    que fr.wikipedia.org/w/api.php (utilisé juste après) ne bloque pas.
    """
    async with httpx.AsyncClient(timeout=15, headers=HEADERS) as client:
        resp = await client.get(
            "https://fr.wikipedia.org/w/api.php",
            params={
                "action": "query", "list": "search", "srsearch": titre_film,
                "srlimit": 1, "format": "json",
            },
        )
        try:
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"  ⚠️ Recherche Wikipédia indisponible pour '{titre_film}': {e} — ignoré", flush=True)
            return None
        resultats = data.get("query", {}).get("search", [])
        return resultats[0]["title"] if resultats else None


async def _extraire_section_tournage(titre_page: str) -> str | None:
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
        r"==\s*(Tournage|Lieux de tournage)\s*==\s*\n(.*?)(?=\n==|\Z)",
        texte, re.DOTALL,
    )
    if not match:
        return None
    return match.group(2).strip()[:1500]


def _ressemble_a_une_liste(texte: str) -> bool:
    """
    Certaines sections 'Tournage' Wikipédia sont une liste brute de
    lieux (un par ligne, style "Commune (détail)") plutôt qu'un texte
    narratif — publier ça tel quel sur un seul lieu de notre base
    serait trompeur (ça mélange des dizaines d'endroits sans rapport
    direct). On détecte ce cas et on le traite comme "à vérifier",
    même si le film n'a qu'un seul lieu chez nous.
    """
    lignes = [l for l in texte.split("\n") if l.strip()]
    if len(lignes) < 5:
        return False
    lignes_courtes_sans_ponctuation = sum(
        1 for l in lignes if len(l) < 80 and not l.strip().endswith((".", "!", "?"))
    )
    return lignes_courtes_sans_ponctuation / len(lignes) > 0.6


async def main():
    try:
        with open("films_pour_anecdotes.json", encoding="utf-8") as f:
            films = json.load(f)
    except FileNotFoundError:
        print(
            "❌ films_pour_anecdotes.json introuvable. Télécharge-le depuis "
            "l'artefact du workflow GitHub Actions 'Export films pour anecdotes' "
            "et place-le dans ce dossier (backend/).",
            flush=True,
        )
        return

    print(f"{len(films)} film(s) à vérifier\n", flush=True)

    lignes_sql = ["-- Généré par wikipedia_anecdotes_local.py — à importer dans Neon"]
    auto_publies = 0
    a_traiter_manuellement = 0

    for film in films:
        try:
            titre_page = await _chercher_page_wikipedia(film["titre"])
            if not titre_page:
                continue

            section = await _extraire_section_tournage(titre_page)
            if not section:
                await asyncio.sleep(1)
                continue

            lieux = film["lieux"]

            if len(lieux) == 1 and not _ressemble_a_une_liste(section):
                texte = _echapper_sql(section + MENTION_SOURCE)
                lignes_sql.append(
                    f"UPDATE lieux_tournage SET anecdote = '{texte}' WHERE id = {lieux[0]['id']};"
                )
                auto_publies += 1
                print(f"✓ {film['titre']} → ajouté au .sql (lieu unique: {lieux[0]['nom']})", flush=True)
            else:
                a_traiter_manuellement += 1
                noms_lieux = ", ".join(f"{l['nom']} (id={l['id']})" for l in lieux)
                print(f"\n═══ {film['titre']} (id={film['id']}) — {len(lieux)} lieux, à répartir toi-même ═══")
                print(section)
                print(f"Lieux disponibles : {noms_lieux}")
                print(
                    "→ UPDATE lieux_tournage SET anecdote = '... (Source : Wikipédia, CC BY-SA)' "
                    "WHERE id = <lieu concerné parmi ceux ci-dessus>;\n"
                )

            await asyncio.sleep(1)  # respecte l'API Wikipédia, service public partagé
        except Exception as e:
            print(f"⚠️ Erreur imprévue sur '{film['titre']}': {e} — passage au suivant", flush=True)
            await asyncio.sleep(1)
            continue

    with open("anecdotes.sql", "w", encoding="utf-8") as f:
        f.write("\n".join(lignes_sql))

    print(
        f"\nTerminé : {auto_publies} anecdote(s) prête(s) dans anecdotes.sql "
        f"(à importer dans Neon), {a_traiter_manuellement} film(s) à répartir toi-même.",
        flush=True,
    )


if __name__ == "__main__":
    asyncio.run(main())
