"""
backend/openverse_cache.py

Cache Redis/Upstash pour les résultats de recherche Openverse.

Même principe que navigation_cache.py (module dédié plutôt que
partagé, pour garder des TTL et un rythme de verrou différents —
une recherche d'images n'a pas les mêmes contraintes qu'un calcul
d'itinéraire IGN) :
- une recherche Openverse déjà vue est servie depuis Redis, latence
  quasi nulle, même si des dizaines de milliers d'utilisateurs
  cherchent le même paysage (ex: "Cévennes vallée") en même temps ;
- verrou anti-rafale : si 1 million d'utilisateurs interrogent le
  même terme au même instant sur un cache froid, un seul appel part
  vers Openverse, les autres attendent son résultat au lieu de
  déclencher chacun leur propre requête HTTP ;
- désactivé proprement si Redis n'est pas configuré (l'appel direct à
  Openverse continue de fonctionner, juste sans cache).

Les résultats Openverse (métadonnées d'images sous licence ouverte)
changent rarement : TTL long (7 jours) contrairement au cache de
navigation (24h, données de trajet).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from typing import Any

import httpx

CACHE_TTL_SECONDS = 7 * 24 * 60 * 60

LOCK_TTL_SECONDS = 15
LOCK_ATTEMPTS = 30
LOCK_POLL_SECONDS = 0.25


class OpenverseCache:
    """Petit client Redis REST compatible Upstash, dédié à Openverse."""

    def __init__(self) -> None:
        self.url = os.getenv("UPSTASH_REDIS_REST_URL", "").strip().rstrip("/")
        self.token = os.getenv("UPSTASH_REDIS_REST_TOKEN", "").strip()
        self.enabled = bool(self.url and self.token)

    def construire_cle(self, query: str, page: int, page_size: int) -> str:
        brut = f"openverse-v1|{query.strip().lower()}|{page}|{page_size}"
        digest = hashlib.sha256(brut.encode("utf-8")).hexdigest()
        return f"pelify:openverse:{digest}"

    async def _command(self, *arguments: Any) -> Any | None:
        if not self.enabled:
            return None
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(connect=2.0, read=3.0, write=2.0, pool=2.0)
            ) as client:
                response = await client.post(
                    self.url,
                    headers={
                        "Authorization": f"Bearer {self.token}",
                        "Content-Type": "application/json",
                    },
                    json=list(arguments),
                )
            if response.status_code >= 400:
                print(f"⚠️ Redis openverse : HTTP {response.status_code}", flush=True)
                return None
            return response.json().get("result")
        except Exception as exc:
            # Le cache ne doit jamais casser la recherche d'images.
            print(f"⚠️ Redis openverse indisponible : {exc}", flush=True)
            return None

    async def get(self, key: str) -> dict | None:
        result = await self._command("GET", key)
        if not result:
            return None
        try:
            if isinstance(result, str):
                return json.loads(result)
            if isinstance(result, dict):
                return result
        except (json.JSONDecodeError, TypeError):
            return None
        return None

    async def set(self, key: str, value: dict, ttl: int = CACHE_TTL_SECONDS) -> bool:
        result = await self._command(
            "SET", key, json.dumps(value, ensure_ascii=False, separators=(",", ":")), "EX", ttl,
        )
        return result == "OK"

    async def acquire_lock(self, key: str) -> bool:
        result = await self._command("SET", key, "1", "EX", LOCK_TTL_SECONDS, "NX")
        return result == "OK"

    async def wait_for_result(self, key: str) -> dict | None:
        if not self.enabled:
            return None
        for _ in range(LOCK_ATTEMPTS):
            await asyncio.sleep(LOCK_POLL_SECONDS)
            resultat = await self.get(key)
            if resultat:
                return resultat
        return None

    async def delete(self, key: str) -> None:
        await self._command("DEL", key)


openverse_cache = OpenverseCache()