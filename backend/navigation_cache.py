"""
backend/navigation_cache.py

Cache Redis/Upstash pour les itinÃ©raires de navigation dynamiques.

Objectifs :
- Ã©viter de recalculer plusieurs fois le mÃªme trajet IGN ;
- regrouper les positions GPS proches ;
- empÃªcher plusieurs requÃªtes identiques simultanÃ©es de frapper IGN ;
- fonctionner sans Redis si les variables d'environnement ne sont pas configurÃ©es.

Le cache est volontairement sÃ©parÃ© du cache PostgreSQL amenity_cache :
- amenity_cache = donnÃ©es durables et prÃ©-calculÃ©es ;
- Redis = cache temporaire des navigations utilisateurs.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from typing import Any

import httpx


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Configuration
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

CACHE_TTL_SECONDS = 24 * 60 * 60

# Les coordonnÃ©es GPS sont regroupÃ©es par tranche d'environ 100 m.
# Cela Ã©vite de crÃ©er une clÃ© diffÃ©rente Ã  chaque mouvement GPS.
PRECISION_COORDONNEES = 4

# DurÃ©e maximale pendant laquelle une requÃªte attend qu'un autre
# utilisateur termine le calcul IGN.
LOCK_TTL_SECONDS = 30

# Nombre maximal de tentatives d'attente sur un calcul dÃ©jÃ  en cours.
LOCK_ATTEMPTS = 40

# Intervalle entre deux vÃ©rifications du cache pendant l'attente.
LOCK_POLL_SECONDS = 0.25


class NavigationCache:
    """
    Petit client Redis REST compatible Upstash.

    Le cache est dÃ©sactivÃ© proprement si :
        UPSTASH_REDIS_REST_URL
        UPSTASH_REDIS_REST_TOKEN

    ne sont pas configurÃ©s.
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

    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # ClÃ©s
    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    @staticmethod
    def _normaliser_coordonnee(valeur: float) -> str:
        """
        Normalise une coordonnÃ©e GPS.

        3 dÃ©cimales â‰ˆ 100 m.
        Cela permet Ã  plusieurs utilisateurs situÃ©s dans la mÃªme
        zone de rÃ©utiliser le mÃªme itinÃ©raire.
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

    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # Appel Redis REST
    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

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
                    "âš ï¸ Redis navigation : "
                    f"HTTP {response.status_code}",
                    flush=True,
                )
                return None

            data = response.json()
            return data.get("result")

        except Exception as exc:
            # Le cache ne doit jamais casser la navigation.
            print(
                f"âš ï¸ Redis navigation indisponible : {exc}",
                flush=True,
            )
            return None

    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # Lecture
    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

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

    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # Ã‰criture
    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

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

    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # Verrou anti-rafale
    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    async def acquire_lock(
        self,
        key: str,
    ) -> bool:
        """
        Tente d'obtenir un verrou exclusif.

        NX = seulement si la clÃ© n'existe pas.

        EX = expiration automatique pour Ã©viter un verrou bloquÃ©
        dÃ©finitivement si le processus plante.
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
