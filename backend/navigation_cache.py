"""
backend/navigation_cache.py

Cache Redis/Upstash pour les itinéraires de navigation dynamiques.

Objectifs :
- éviter de recalculer plusieurs fois le même trajet IGN ;
- regrouper les positions GPS proches ;
- empêcher plusieurs requêtes identiques simultanées de frapper IGN ;
- fonctionner sans Redis si les variables d'environnement ne sont pas configurées.

Le cache est volontairement séparé du cache PostgreSQL amenity_cache :
- amenity_cache = données durables et pré-calculées ;
- Redis = cache temporaire des navigations utilisateurs.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from typing import Any

import httpx


# ─────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────

CACHE_TTL_SECONDS = 24 * 60 * 60

# Les coordonnées GPS sont regroupées par tranche d'environ 100 m.
# Cela évite de créer une clé différente à chaque mouvement GPS.
PRECISION_COORDONNEES = 4

# Durée maximale pendant laquelle une requête attend qu'un autre
# utilisateur termine le calcul IGN.
LOCK_TTL_SECONDS = 30

# Nombre maximal de tentatives d'attente sur un calcul déjà en cours.
LOCK_ATTEMPTS = 40

# Intervalle entre deux vérifications du cache pendant l'attente.
LOCK_POLL_SECONDS = 0.25


class NavigationCache:
    """
    Petit client Redis REST compatible Upstash.

    Le cache est désactivé proprement si :
        UPSTASH_REDIS_REST_URL
        UPSTASH_REDIS_REST_TOKEN

    ne sont pas configurés.
    """

    def __init__(self) -> None:
        self.url = (
            os.getenv("UPSTASH_REDIS_REST_URL", "")
            .strip()
            .rstrip("/")
        )
        self.token = os.getenv(
            "UPSTASH_REDIS_REST_TOKEN",
            "",
        ).strip()

        self.enabled = bool(self.url and self.token)

    # ─────────────────────────────────────────────────────────
    # Clés
    # ─────────────────────────────────────────────────────────

    @staticmethod
    def _normaliser_coordonnee(valeur: float) -> str:
        """
        Normalise une coordonnée GPS.

        3 décimales ≈ 100 m.
        Cela permet à plusieurs utilisateurs situés dans la même
        zone de réutiliser le même itinéraire.
        """
        return f"{round(float(valeur), PRECISION_COORDONNEES):.{PRECISION_COORDONNEES}f}"

    def construire_cle(
        self,
        depart_lat: float,
        depart_lon: float,
        arrivee_lat: float,
        arrivee_lon: float,
        mode: str,
        etapes: bool,
    ) -> str:
        depart = (
            self._normaliser_coordonnee(depart_lat),
            self._normaliser_coordonnee(depart_lon),
        )

        arrivee = (
            self._normaliser_coordonnee(arrivee_lat),
            self._normaliser_coordonnee(arrivee_lon),
        )

        brut = "|".join(
            [
                "navigation-v2",
                mode,
                "steps" if etapes else "route",
                depart[0],
                depart[1],
                arrivee[0],
                arrivee[1],
            ]
        )

        digest = hashlib.sha256(
            brut.encode("utf-8")
        ).hexdigest()

        return f"pelify:navigation:{digest}"

    # ─────────────────────────────────────────────────────────
    # Appel Redis REST
    # ─────────────────────────────────────────────────────────

    async def _command(
        self,
        *arguments: Any,
    ) -> Any | None:
        if not self.enabled:
            return None

        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(
                    connect=2.0,
                    read=3.0,
                    write=2.0,
                    pool=2.0,
                )
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
                print(
                    "⚠️ Redis navigation : "
                    f"HTTP {response.status_code}",
                    flush=True,
                )
                return None

            data = response.json()
            return data.get("result")

        except Exception as exc:
            # Le cache ne doit jamais casser la navigation.
            print(
                f"⚠️ Redis navigation indisponible : {exc}",
                flush=True,
            )
            return None

    # ─────────────────────────────────────────────────────────
    # Lecture
    # ─────────────────────────────────────────────────────────

    async def get(
        self,
        key: str,
    ) -> dict | None:
        result = await self._command(
            "GET",
            key,
        )

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

    # ─────────────────────────────────────────────────────────
    # Écriture
    # ─────────────────────────────────────────────────────────

    async def set(
        self,
        key: str,
        value: dict,
        ttl: int = CACHE_TTL_SECONDS,
    ) -> bool:
        result = await self._command(
            "SET",
            key,
            json.dumps(
                value,
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            "EX",
            ttl,
        )

        return result == "OK"

    # ─────────────────────────────────────────────────────────
    # Verrou anti-rafale
    # ─────────────────────────────────────────────────────────

    async def acquire_lock(
        self,
        key: str,
    ) -> bool:
        """
        Tente d'obtenir un verrou exclusif.

        NX = seulement si la clé n'existe pas.

        EX = expiration automatique pour éviter un verrou bloqué
        définitivement si le processus plante.
        """
        result = await self._command(
            "SET",
            key,
            "1",
            "EX",
            LOCK_TTL_SECONDS,
            "NX",
        )

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
    
    async def delete(
        self,
        key: str,
    ) -> None:
        await self._command(
            "DEL",
            key,
        )


navigation_cache = NavigationCache()