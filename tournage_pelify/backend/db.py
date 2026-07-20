"""
backend/db.py — Pool de connexions MySQL asynchrone.

Utilise aiomysql (compatible avec ta base MySQL universitaire /
phpMyAdmin, pas besoin de Postgres). Le pool est créé une fois au
démarrage de l'app et réutilisé pour toutes les requêtes — c'est ce
qui permet de tenir la charge sans ouvrir une connexion par requête.

Variables d'environnement attendues :
  DB_HOST, DB_PORT (def 3306), DB_USER, DB_PASSWORD, DB_NAME
"""

import os
import aiomysql

_pool: aiomysql.Pool | None = None


async def init_db_pool() -> None:
    global _pool
    _pool = await aiomysql.create_pool(
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", "3306")),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        db=os.getenv("DB_NAME", "ehou_db"),
        charset="utf8mb4",
        autocommit=True,
        minsize=5,
        maxsize=20,   # ajuster selon les limites de ta BDD universitaire
    )


async def close_db_pool() -> None:
    if _pool:
        _pool.close()
        await _pool.wait_closed()


async def fetch_all(query: str, params: tuple = ()) -> list[dict]:
    async with _pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(query, params)
            return await cur.fetchall()


async def fetch_one(query: str, params: tuple = ()) -> dict | None:
    async with _pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(query, params)
            return await cur.fetchone()


async def execute(query: str, params: tuple = ()) -> int:
    """Pour INSERT/UPDATE/DELETE. Retourne l'id inséré si applicable."""
    async with _pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(query, params)
            return cur.lastrowid