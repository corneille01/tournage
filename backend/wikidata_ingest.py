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
import csv
import os
import re
import sys

import httpx

# Bug connu : asyncpg se bloque jusqu'au timeout sur Windows avec la
# boucle d'événements par défaut (ProactorEventLoop) dès qu'une
# connexion SSL est impliquée (Neon impose SSL). La SelectorEventLoop
# n'a pas ce problème.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

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
       ?location ?locationLabel ?coord ?communeLabel ?departementLabel ?photo
WHERE {{
  ?film wdt:P915 ?location .
  ?location wdt:P131* wd:{QID_OCCITANIE} .
  OPTIONAL {{ ?film wdt:P4947 ?tmdbMovie . }}
  OPTIONAL {{ ?film wdt:P4983 ?tmdbTv . }}
  OPTIONAL {{ ?location wdt:P625 ?coord . }}
  OPTIONAL {{ ?location wdt:P131 ?commune . }}
  OPTIONAL {{ ?location wdt:P18 ?photo . }}
  # Remonte la hiérarchie administrative jusqu'à trouver le département
  # (type Q6465 dans Wikidata) — un même lieu a plusieurs parents P131
  # (canton, arrondissement...), on veut spécifiquement celui-là.
  OPTIONAL {{
    ?location wdt:P131* ?departement .
    ?departement wdt:P31 wd:Q6465 .
  }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "fr,en". }}
}}
LIMIT 2000
"""

_COORD_RE = re.compile(r"Point\(([\-0-9.]+) ([\-0-9.]+)\)")


def charger_depuis_csv(chemin: str) -> list[dict]:
    """
    Charge les résultats depuis un export CSV téléchargé manuellement
    sur query.wikidata.org (bouton 'Fichier CSV' sous les résultats de
    la requête SPARQL_QUERY ci-dessus, à coller telle quelle sur
    https://query.wikidata.org/).

    Utile si l'exécution automatique échoue (403, réseau universitaire
    bloqué, etc.) — tu lances la requête toi-même dans ton navigateur,
    tu télécharges le CSV, et ce script fait le reste (enrichissement
    TMDB + génération du fichier SQL) sans dépendre du réseau pour la
    partie Wikidata.

    Colonnes attendues (mêmes noms que les variables SELECT) :
    film, filmLabel, filmDescription, tmdbMovie, tmdbTv, location,
    locationLabel, coord, communeLabel
    """
    lignes = []
    with open(chemin, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            coord_raw = row.get("coord", "")
            match = _COORD_RE.match(coord_raw) if coord_raw else None
            if not match:
                continue

            lon, lat = float(match.group(1)), float(match.group(2))
            lignes.append({
                "wikidata_qid": row.get("film", "").rsplit("/", 1)[-1],
                "titre_wikidata": row.get("filmLabel") or "Sans titre",
                "description": row.get("filmDescription") or None,
                "tmdb_movie_id": row.get("tmdbMovie") or None,
                "tmdb_tv_id": row.get("tmdbTv") or None,
                "lieu_nom": row.get("locationLabel") or "Lieu inconnu",
                "lieu_qid": (row.get("location") or "").rsplit("/", 1)[-1] or None,
                "photo_url": row.get("photo") or None,
                "commune": row.get("communeLabel") or None,
                "departement": row.get("departementLabel") or None,
                "latitude": lat,
                "longitude": lon,
            })
    print(f"{len(lignes)} lignes chargées depuis {chemin}", flush=True)
    return lignes


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
            "lieu_qid": b.get("location", {}).get("value", "").rsplit("/", 1)[-1] or None,
            "photo_url": b.get("photo", {}).get("value"),
            "commune": b.get("communeLabel", {}).get("value"),
            "departement": b.get("departementLabel", {}).get("value"),
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
    ligne["popularite"] = d.get("popularity")
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
                 annee, synopsis, poster_url, popularite, region, source_donnee, statut)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'Occitanie', 'wikidata', 'brouillon')
            RETURNING id
            """,
            (
                ligne["tmdb_id"], ligne["wikidata_qid"], ligne["titre"],
                ligne["titre_original"], ligne["media_type"], ligne["annee"],
                ligne["synopsis"], ligne["poster_url"], ligne.get("popularite"),
            ),
        )
        print(f"  + Film créé: {ligne['titre']} (id={film_id})", flush=True)

    existe = await fetch_one(
        """
        SELECT id, wikidata_qid, photo_url FROM lieux_tournage
        WHERE film_id = %s AND ABS(latitude - %s) < 0.0001 AND ABS(longitude - %s) < 0.0001
        """,
        (film_id, ligne["latitude"], ligne["longitude"]),
    )
    if not existe:
        await execute(
            """
            INSERT INTO lieux_tournage
                (film_id, nom, commune, departement, latitude, longitude,
                 wikidata_qid, photo_url)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (film_id, ligne["lieu_nom"], ligne.get("commune"), ligne.get("departement"),
             ligne["latitude"], ligne["longitude"],
             ligne.get("lieu_qid"), ligne.get("photo_url")),
        )
        print(f"    ↳ Lieu ajouté: {ligne['lieu_nom']}", flush=True)
    elif not existe.get("wikidata_qid") and ligne.get("lieu_qid"):
        # Lieu déjà importé avant qu'on capture QID/photo (rétro-complétion,
        # ne modifie rien d'autre — ne touche pas nom/commune déjà validés).
        await execute(
            "UPDATE lieux_tournage SET wikidata_qid = %s, photo_url = COALESCE(photo_url, %s) WHERE id = %s",
            (ligne.get("lieu_qid"), ligne.get("photo_url"), existe["id"]),
        )


def _sql_echap(valeur) -> str:
    """Échappe une valeur Python pour l'insérer littéralement dans un fichier .sql."""
    if valeur is None:
        return "NULL"
    if isinstance(valeur, (int, float)):
        return str(valeur)
    texte = str(valeur).replace("\\", "\\\\").replace("'", "\\'")
    return f"'{texte}'"


async def generer_fichier_sql(lignes: list[dict], chemin: str) -> None:
    """
    ⚠️ Génère de la syntaxe MySQL (LAST_INSERT_ID()), pas Postgres.
    Utile uniquement si tu veux encore archiver une copie sur ta base
    universitaire (phpMyAdmin) en plus de la base live. Pour la base
    live (Render Postgres), utilise le mode par défaut sans --sql-file
    — la connexion directe fonctionne maintenant (Postgres accepte les
    connexions externes, contrairement à l'université).

    Alternative à l'insertion directe en base : produit un fichier .sql
    à importer soi-même via phpMyAdmin (bouton "Importer"). Pas de
    vérification de doublons ici (pas de connexion à la base pour
    comparer) : à utiliser pour un premier import sur une base vide.
    """
    lignes_sql = [
        "-- Généré par wikidata_ingest.py --sql-file",
        "-- À importer via phpMyAdmin : onglet 'Importer' > choisir ce fichier",
        "SET NAMES utf8mb4;",
        "",
    ]

    for ligne in lignes:
        ligne.setdefault("tmdb_id", None)
        ligne.setdefault("media_type", "movie")
        ligne.setdefault("titre", ligne["titre_wikidata"])
        ligne.setdefault("titre_original", None)
        ligne.setdefault("synopsis", ligne.get("description"))
        ligne.setdefault("annee", None)
        ligne.setdefault("poster_url", None)

        lignes_sql.append(
            "INSERT INTO films "
            "(tmdb_id, wikidata_qid, titre, titre_original, media_type, annee, "
            "synopsis, poster_url, region, source_donnee, statut) VALUES ("
            f"{_sql_echap(ligne['tmdb_id'])}, {_sql_echap(ligne['wikidata_qid'])}, "
            f"{_sql_echap(ligne['titre'])}, {_sql_echap(ligne['titre_original'])}, "
            f"{_sql_echap(ligne['media_type'])}, {_sql_echap(ligne['annee'])}, "
            f"{_sql_echap(ligne['synopsis'])}, {_sql_echap(ligne['poster_url'])}, "
            "'Occitanie', 'wikidata', 'brouillon');"
        )
        lignes_sql.append("SET @dernier_film_id = LAST_INSERT_ID();")
        lignes_sql.append(
            "INSERT INTO lieux_tournage (film_id, nom, commune, departement, latitude, longitude) "
            "VALUES (@dernier_film_id, "
            f"{_sql_echap(ligne['lieu_nom'])}, {_sql_echap(ligne.get('commune'))}, "
            f"{_sql_echap(ligne.get('departement'))}, "
            f"{ligne['latitude']}, {ligne['longitude']});"
        )

    with open(chemin, "w", encoding="utf-8") as f:
        f.write("\n".join(lignes_sql))

    print(f"\n✓ Fichier généré : {chemin}", flush=True)
    print(
        "  → Dans phpMyAdmin, sélectionne la base 'cinetour', onglet "
        "'Importer', choisis ce fichier, puis 'Exécuter'.",
        flush=True,
    )


async def main(dry_run: bool, sql_file: str | None, from_csv: str | None):
    if from_csv:
        lignes = charger_depuis_csv(from_csv)
    else:
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

    if sql_file:
        print("Enrichissement TMDB (peut prendre quelques minutes)…", flush=True)
        lignes_enrichies = []
        for i, ligne in enumerate(lignes, 1):
            lignes_enrichies.append(await enrichir_tmdb(ligne))
            if i % 50 == 0:
                print(f"  … {i}/{len(lignes)} enrichis", flush=True)
        await generer_fichier_sql(lignes_enrichies, sql_file)
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
        "vérifie-les dans l'éditeur SQL de Neon (dashboard Neon > SQL Editor) "
        "ou avec un client Postgres (poster/synopsis présents, "
        "media_type correct) avant de passer statut='publie'.",
        flush=True,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--sql-file", type=str, default=None,
        help="Génère un fichier .sql à importer via phpMyAdmin, au lieu de se "
             "connecter directement à la base (utile si l'accès MySQL distant "
             "est bloqué par l'hébergement).",
    )
    parser.add_argument(
        "--from-csv", type=str, default=None,
        help="Charge les résultats depuis un CSV exporté manuellement sur "
             "query.wikidata.org au lieu d'interroger Wikidata en direct.",
    )
    args = parser.parse_args()
    asyncio.run(main(args.dry_run, args.sql_file, args.from_csv))
