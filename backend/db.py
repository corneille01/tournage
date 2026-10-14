"""
Couche PostgreSQL compatible Render et Cloudflare Workers + Hyperdrive.

- Render : utilise DATABASE_URL / DB_* et un pool asyncpg.
- Cloudflare Workers : utilise la configuration fournie par Hyperdrive.
"""

import os
import re
import asyncpg

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


# ---------------------------------------------------------------------------
# MODE RENDER
# ---------------------------------------------------------------------------

_pool: asyncpg.Pool | None = None


# ---------------------------------------------------------------------------
# MODE CLOUDFLARE / HYPERDRIVE
# ---------------------------------------------------------------------------

_hyperdrive = None


def configure_hyperdrive(hyperdrive) -> None:
    """
    Reçoit le binding env.HYPERDRIVE depuis le Worker Cloudflare.

    Le binding contient notamment :
      - host
      - port
      - user
      - password
      - database
    """
    global _hyperdrive
    _hyperdrive = hyperdrive


def _dsn() -> str:
    url = os.getenv("DATABASE_URL")

    if url:
        return url.replace("postgres://", "postgresql://", 1)

    return (
        f"postgresql://{os.getenv('DB_USER')}:{os.getenv('DB_PASSWORD')}"
        f"@{os.getenv('DB_HOST', 'localhost')}:{os.getenv('DB_PORT', '5432')}"
        f"/{os.getenv('DB_NAME', 'cinetour')}"
    )


def _convertir_placeholders(query: str, params: tuple) -> str:
    """
    Convertit les placeholders %s vers $1, $2, ... pour asyncpg.
    """
    compteur = [0]

    def _remplace(_match):
        compteur[0] += 1
        return f"${compteur[0]}"

    return re.sub(r"%s", _remplace, query)


# ---------------------------------------------------------------------------
# INITIALISATION
# ---------------------------------------------------------------------------

async def init_db_pool() -> None:
    """
    Initialise le pool Render.

    Sur Cloudflare, le Worker utilise Hyperdrive et n'a pas besoin
    d'un pool global ici.
    """
    global _pool

    # Cloudflare / Hyperdrive :
    # aucune connexion persistante globale à créer.
    if _hyperdrive is not None:
        print("PostgreSQL : mode Cloudflare Hyperdrive", flush=True)
        return

    # Render / environnement classique :
    dsn = _dsn()

    host_visible = re.sub(
        r"://[^@]+@",
        "://***:***@",
        dsn
    )

    print(f"Connexion PostgreSQL à : {host_visible}", flush=True)

    _pool = await asyncpg.create_pool(
        dsn=dsn,
        min_size=2,
        max_size=15,
        timeout=15,
        statement_cache_size=0,
    )


async def close_db_pool() -> None:
    global _pool

    if _pool:
        await _pool.close()
        _pool = None


# ---------------------------------------------------------------------------
# CONNEXION CLOUDFLARE
# ---------------------------------------------------------------------------

async def _connect_hyperdrive():
    """
    Ouvre une connexion PostgreSQL via Cloudflare Hyperdrive.

    Hyperdrive fournit déjà le pooling côté Cloudflare.
    """

    if _hyperdrive is None:
        raise RuntimeError(
            "HYPERDRIVE n'est pas configuré. "
            "configure_hyperdrive() doit être appelé par le Worker."
        )

    return await asyncpg.connect(
        host=_hyperdrive.host,
        port=int(_hyperdrive.port),
        user=_hyperdrive.user,
        password=_hyperdrive.password,
        database=_hyperdrive.database,
        ssl=False,
        statement_cache_size=0,
    )


# ---------------------------------------------------------------------------
# FETCH ALL
# ---------------------------------------------------------------------------

async def fetch_all(query: str, params: tuple = ()) -> list[dict]:
    query_pg = _convertir_placeholders(query, params)

    # Cloudflare / Hyperdrive
    if _hyperdrive is not None:
        conn = await _connect_hyperdrive()

        try:
            rows = await conn.fetch(query_pg, *params)
            return [dict(r) for r in rows]
        finally:
            await conn.close()

    # Render
    if _pool is None:
        raise RuntimeError("Pool PostgreSQL non initialisé.")

    async with _pool.acquire() as conn:
        rows = await conn.fetch(query_pg, *params)
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# FETCH ONE
# ---------------------------------------------------------------------------

async def fetch_one(query: str, params: tuple = ()) -> dict | None:
    query_pg = _convertir_placeholders(query, params)

    # Cloudflare / Hyperdrive
    if _hyperdrive is not None:
        conn = await _connect_hyperdrive()

        try:
            row = await conn.fetchrow(query_pg, *params)
            return dict(row) if row else None
        finally:
            await conn.close()

    # Render
    if _pool is None:
        raise RuntimeError("Pool PostgreSQL non initialisé.")

    async with _pool.acquire() as conn:
        row = await conn.fetchrow(query_pg, *params)
        return dict(row) if row else None


# ---------------------------------------------------------------------------
# EXECUTE
# ---------------------------------------------------------------------------

async def execute(query: str, params: tuple = ()):
    """
    Pour INSERT / UPDATE / DELETE.

    Si RETURNING est présent, retourne la valeur retournée.
    Sinon retourne le statut PostgreSQL.
    """

    query_pg = _convertir_placeholders(query, params)

    # Cloudflare / Hyperdrive
    if _hyperdrive is not None:
        conn = await _connect_hyperdrive()

        try:
            if "RETURNING" in query.upper():
                return await conn.fetchval(query_pg, *params)

            return await conn.execute(query_pg, *params)
        finally:
            await conn.close()

    # Render
    if _pool is None:
        raise RuntimeError("Pool PostgreSQL non initialisé.")

    async with _pool.acquire() as conn:
        if "RETURNING" in query.upper():
            return await conn.fetchval(query_pg, *params)

        return await conn.execute(query_pg, *params)


# ---------------------------------------------------------------------------
# EXECUTEMANY
# ---------------------------------------------------------------------------

async def executemany(
    query: str,
    liste_params: list[tuple]
) -> None:

    if not liste_params:
        return

    query_pg = _convertir_placeholders(
        query,
        liste_params[0]
    )

    # Cloudflare / Hyperdrive
    if _hyperdrive is not None:
        conn = await _connect_hyperdrive()

        try:
            await conn.executemany(
                query_pg,
                liste_params
            )
        finally:
            await conn.close()

        return

    # Render
    if _pool is None:
        raise RuntimeError("Pool PostgreSQL non initialisé.")

    async with _pool.acquire() as conn:
        await conn.executemany(
            query_pg,
            liste_params
        )