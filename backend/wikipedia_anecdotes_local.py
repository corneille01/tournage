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

HEADERS = {"User-Agent": "CineTourBot/1.0 (contact: [email protected])"}
MENTION_SOURCE = "\n\n(Source : Wikipédia, CC BY-SA)"


def _echapper_sql(texte: str) -> str:
    return texte.replace("\\", "\\\\").replace("'", "''")


async def _qid_vers_titre_wikipedia_fr(qid: str) -> str | None:
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
            titre_page = await _qid_vers_titre_wikipedia_fr(film["wikidata_qid"])
            if not titre_page:
                continue

            section = await _extraire_section_tournage(titre_page)
            if not section:
                await asyncio.sleep(1)
                continue

            lieux = film["lieux"]

            if len(lieux) == 1:
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