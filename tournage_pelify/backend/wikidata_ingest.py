"""
backend/wikidata_ingest.py — Récupère les films/séries tournés en
Occitanie depuis Wikidata (propriété P915 "filming location"),
enrichit avec TMDB (poster, synopsis, type exact), et insère en base
avec le statut 'brouillon' (à valider manuellement avant publication
— voir README, section validation).

Ne remplace pas un travail éditorial : Wikidata est incomplet pour les
lieux de tournage régionaux. Ce script donne un premier socle, pas un
catalogue exhaustif. Complète à la main les films emblématiques
manquants directement en base (statut='publie' une fois vérifiés).

Usage :
    python wikidata_ingest.py --dry-run     # affiche sans toucher la BDD
    python wikidata_ingest.py               # ingère réellement
"""

import argparse
import asyncio
import os
import re

import httpx

from db import init_db_pool, close_db_pool, fetch_one, execute

WIKIDATA_SPARQL_URL = "https://query.wikidata.org/sparql"
TMDB_API_KEY = os.getenv("TMDB_API_KEY", "")
TMDB_BASE = "https://api.themoviedb.org/3"

# Occitanie (région administrative) — vérifié via recherche, à
# reconfirmer sur wikidata.org si le script ne remonte aucun résultat.
QID_OCCITANIE = "Q18678265"

# Obligatoire par la politique d'usage de Wikidata Query Service —
# remplace [email protected] par une vraie adresse avant usage en
# production (un User-Agent trop générique se fait bloquer par leur
# pare-feu avec un 403, même pour une requête légitime).
HEADERS = {
    "User-Agent": "CineTourBot/1.0 (https://tonsite.fr; contact: [email protected]) httpx",
    "Accept": "application/sparql-results+json",
}

SPARQL_QUERY = f"""
SELECT DISTINCT ?film ?filmLabel ?filmDescription ?tmdbMovie ?tmdbTv
       ?location ?locationLabel ?coord ?communeLabel
WHERE {{
  ?film wdt:P915 ?location .
  ?location wdt:P131* wd:{QID_OCCITANIE} .
  OPTIONAL {{ ?film wdt:P4947 ?tmdbMovie . }}
  OPTIONAL {{ ?film wdt:P4983 ?tmdbTv . }}
  OPTIONAL {{ ?location wdt:P625 ?coord . }}
  OPTIONAL {{ ?location wdt:P131 ?commune . }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "fr,en". }}
}}
LIMIT 2000
"""

_COORD_RE = re.compile(r"Point\(([\-0-9.]+) ([\-0-9.]+)\)")


async def query_wikidata() -> list[dict]:
    async with httpx.AsyncClient(timeout=60, headers=HEADERS) as client:
        resp = await client.post(
            WIKIDATA_SPARQL_URL,
            data={"query": SPARQL_QUERY, "format": "json"},
        )
        if resp.status_code == 403:
            print(
                "⚠️ 403 de Wikidata malgré le POST + User-Agent. Teste la requête "
                "manuellement sur https://query.wikidata.org/sparql en collant "
                "SPARQL_QUERY (au-dessus dans ce fichier), pour voir si c'est la "
                "requête elle-même ou un blocage réseau/IP (proxy universitaire ?).",
                flush=True,
            )
        resp.raise_for_status()
        data = resp.json()

    lignes = []
    for b in data["results"]["bindings"]:
        coord_raw = b.get("coord", {}).get("value")
        match = _COORD_RE.match(coord_raw) if coord_raw else None
        if not match:
            continue  # sans coordonnées, inutilisable pour la carte

        lon, lat = float(match.group(1)), float(match.group(2))

        lignes.append({
            "wikidata_qid": b["film"]["value"].rsplit("/", 1)[-1],
            "titre_wikidata": b.get("filmLabel", {}).get("value", "Sans titre"),
            "description": b.get("filmDescription", {}).get("value"),
            "tmdb_movie_id": b.get("tmdbMovie", {}).get("value"),
            "tmdb_tv_id": b.get("tmdbTv", {}).get("value"),
            "lieu_nom": b.get("locationLabel", {}).get("value", "Lieu inconnu"),
            "commune": b.get("communeLabel", {}).get("value"),
            "latitude": lat,
            "longitude": lon,
        })
    return lignes


async def enrichir_tmdb(ligne: dict) -> dict:
    """
    Complète avec les vraies données TMDB (poster, synopsis, année,
    type exact movie/tv) quand un identifiant TMDB est disponible.
    Sans TMDB_API_KEY ou sans ID trouvé, le film reste utilisable mais
    avec les seules infos Wikidata (à compléter manuellement).
    """
    if not TMDB_API_KEY:
        return ligne

    tmdb_id, media_type = None, None
    if ligne["tmdb_movie_id"]:
        tmdb_id, media_type = ligne["tmdb_movie_id"], "movie"
    elif ligne["tmdb_tv_id"]:
        tmdb_id, media_type = ligne["tmdb_tv_id"], "tv"

    if not tmdb_id:
        return ligne

    endpoint = "movie" if media_type == "movie" else "tv"
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            resp = await client.get(
                f"{TMDB_BASE}/{endpoint}/{tmdb_id}",
                params={"api_key": TMDB_API_KEY, "language": "fr-FR"},
            )
            resp.raise_for_status()
            d = resp.json()
        except Exception as e:
            print(f"  ⚠️ TMDB {tmdb_id} indisponible: {e}", flush=True)
            return ligne

    ligne["tmdb_id"] = int(tmdb_id)
    ligne["media_type"] = media_type
    ligne["titre"] = d.get("title") or d.get("name") or ligne["titre_wikidata"]
    ligne["titre_original"] = d.get("original_title") or d.get("original_name")
    ligne["synopsis"] = d.get("overview") or ligne.get("description")
    date = d.get("release_date") or d.get("first_air_date") or ""
    ligne["annee"] = int(date[:4]) if date[:4].isdigit() else None
    poster = d.get("poster_path")
    ligne["poster_url"] = f"https://image.tmdb.org/t/p/w500{poster}" if poster else None
    return ligne


async def inserer_en_base(ligne: dict) -> None:
    ligne.setdefault("tmdb_id", None)
    ligne.setdefault("media_type", "movie")  # à défaut de confirmation TMDB, à corriger manuellement
    ligne.setdefault("titre", ligne["titre_wikidata"])
    ligne.setdefault("titre_original", None)
    ligne.setdefault("synopsis", ligne.get("description"))
    ligne.setdefault("annee", None)
    ligne.setdefault("poster_url", None)

    film = await fetch_one(
        "SELECT id FROM films WHERE wikidata_qid = %s", (ligne["wikidata_qid"],)
    )
    if film:
        film_id = film["id"]
    else:
        film_id = await execute(
            """
            INSERT INTO films
                (tmdb_id, wikidata_qid, titre, titre_original, media_type,
                 annee, synopsis, poster_url, region, source_donnee, statut)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'Occitanie', 'wikidata', 'brouillon')
            """,
            (
                ligne["tmdb_id"], ligne["wikidata_qid"], ligne["titre"],
                ligne["titre_original"], ligne["media_type"], ligne["annee"],
                ligne["synopsis"], ligne["poster_url"],
            ),
        )
        print(f"  + Film créé: {ligne['titre']} (id={film_id})", flush=True)

    existe = await fetch_one(
        """
        SELECT id FROM lieux_tournage
        WHERE film_id = %s AND ABS(latitude - %s) < 0.0001 AND ABS(longitude - %s) < 0.0001
        """,
        (film_id, ligne["latitude"], ligne["longitude"]),
    )
    if not existe:
        await execute(
            """
            INSERT INTO lieux_tournage
                (film_id, nom, commune, latitude, longitude)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (film_id, ligne["lieu_nom"], ligne.get("commune"),
             ligne["latitude"], ligne["longitude"]),
        )
        print(f"    ↳ Lieu ajouté: {ligne['lieu_nom']}", flush=True)


async def main(dry_run: bool):
    print("Interrogation de Wikidata…", flush=True)
    lignes = await query_wikidata()
    print(f"{len(lignes)} lignes (film × lieu) trouvées pour l'Occitanie", flush=True)

    if not lignes:
        print(
            "⚠️ Aucun résultat. Vérifie le QID d'Occitanie sur wikidata.org, "
            "ou teste la requête directement sur query.wikidata.org/sparql "
            "en collant SPARQL_QUERY.",
            flush=True,
        )
        return

    if dry_run:
        for l in lignes[:20]:
            print(f"  - {l['titre_wikidata']} → {l['lieu_nom']} ({l['commune']})", flush=True)
        if len(lignes) > 20:
            print(f"  … et {len(lignes) - 20} de plus", flush=True)
        print("\n(dry-run : rien n'a été écrit en base)", flush=True)
        return

    await init_db_pool()
    try:
        for i, ligne in enumerate(lignes, 1):
            ligne = await enrichir_tmdb(ligne)
            await inserer_en_base(ligne)
            if i % 20 == 0:
                print(f"… {i}/{len(lignes)} traités", flush=True)
    finally:
        await close_db_pool()

    print(
        "\nTerminé. Tous les films sont en statut 'brouillon' — "
        "vérifie-les dans phpMyAdmin (poster/synopsis présents, "
        "media_type correct) avant de passer statut='publie'.",
        flush=True,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(args.dry_run))