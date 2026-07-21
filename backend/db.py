"""
backend/db.py — Pool de connexions PostgreSQL asynchrone (Render Postgres).

Utilise asyncpg. Fournit les mêmes fonctions (fetch_all, fetch_one,
execute) qu'avant avec MySQL — le reste du code (main.py,
wikidata_ingest.py, refresh_cache.py) continue d'écrire ses requêtes
avec des placeholders %s comme avant, _convertir_placeholders() les
traduit automatiquement au format $1/$2/... attendu par asyncpg.

Variable d'environnement attendue :
  DATABASE_URL  — fournie automatiquement par Render Postgres
                  (format: postgres://user:password@host:port/dbname)
  À défaut, utilise DB_HOST/DB_PORT/DB_USER/DB_PASSWORD/DB_NAME.
"""

import os
import re
import asyncpg
from dotenv import load_dotenv

load_dotenv()

_pool: asyncpg.Pool | None = None


def _dsn() -> str:
    url = os.getenv("DATABASE_URL")
    if url:
        # asyncpg n'accepte pas le préfixe "postgres://" retourné par
        # certains hébergeurs sous cette forme — on le normalise.
        return url.replace("postgres://", "postgresql://", 1)
    return (
        f"postgresql://{os.getenv('DB_USER')}:{os.getenv('DB_PASSWORD')}"
        f"@{os.getenv('DB_HOST', 'localhost')}:{os.getenv('DB_PORT', '5432')}"
        f"/{os.getenv('DB_NAME', 'cinetour')}"
    )


def _convertir_placeholders(query: str, params: tuple) -> str:
    """'%s' (style MySQL) → '$1', '$2', ... (style asyncpg), dans l'ordre."""
    compteur = [0]

    def _remplace(_match):
        compteur[0] += 1
        return f"${compteur[0]}"

    return re.sub(r"%s", _remplace, query)


async def init_db_pool() -> None:
    global _pool
    dsn = _dsn()
    # Diagnostic sans exposer le mot de passe : montre juste où on essaie
    # de se connecter, pour repérer immédiatement un .env vide/mal rempli.
    host_visible = re.sub(r"://[^@]+@", "://***:***@", dsn)
    print(f"Connexion à : {host_visible}", flush=True)
    _pool = await asyncpg.create_pool(
        dsn=dsn, min_size=2, max_size=15, timeout=15,
        # Neon route les connexions via un pooler façon PgBouncer. Le
        # cache de requêtes préparées d'asyncpg (par connexion) devient
        # invalide dès que le schéma change pendant qu'une connexion du
        # pool est encore vivante — d'où "InvalidCachedStatementError"
        # qui plantait toutes les fiches film après nos migrations.
        # statement_cache_size=0 désactive ce cache : légèrement moins
        # rapide par requête, mais élimine ce plantage définitivement.
        statement_cache_size=0,
    )


async def close_db_pool() -> None:
    if _pool:
        await _pool.close()


async def fetch_all(query: str, params: tuple = ()) -> list[dict]:
    query_pg = _convertir_placeholders(query, params)
    async with _pool.acquire() as conn:
        rows = await conn.fetch(query_pg, *params)
        return [dict(r) for r in rows]


async def fetch_one(query: str, params: tuple = ()) -> dict | None:
    query_pg = _convertir_placeholders(query, params)
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(query_pg, *params)
        return dict(row) if row else None


async def execute(query: str, params: tuple = ()):
    """
    Pour INSERT/UPDATE/DELETE.
    Si la requête se termine par "RETURNING <colonne>" (ajoute-le toi-même
    dans tes INSERT quand tu as besoin de l'id généré — Postgres n'a pas
    d'équivalent implicite à lastrowid de MySQL), renvoie cette valeur.
    Sinon renvoie le statut brut de la commande.
    """
    query_pg = _convertir_placeholders(query, params)
    async with _pool.acquire() as conn:
        if "RETURNING" in query.upper():
            return await conn.fetchval(query_pg, *params)
        return await conn.execute(query_pg, *params)