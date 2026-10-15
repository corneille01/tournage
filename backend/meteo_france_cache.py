"""
backend/meteo_france_cache.py

Petit cache en mémoire avec durée de vie, partagé par les modules météo.
Anti-« cohue » : si plusieurs visiteurs demandent la même donnée au même moment, un seul appel
part vers le service externe (verrou par clé), les autres attendent son résultat.
"""
from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable, Hashable


class CacheTTL:
    def __init__(self, ttl_s: float, taille_max: int = 2000):
        self.ttl_s = ttl_s
        self.taille_max = taille_max
        self._donnees: dict[Hashable, tuple[float, object]] = {}
        self._verrous: dict[Hashable, asyncio.Lock] = {}

    def lire(self, cle: Hashable):
        entree = self._donnees.get(cle)
        if entree and entree[0] > time.monotonic():
            return entree[1]
        return None

    def ecrire(self, cle: Hashable, valeur, ttl_s: float | None = None) -> None:
        if len(self._donnees) >= self.taille_max:
            maintenant = time.monotonic()
            for k in [k for k, (exp, _) in self._donnees.items() if exp <= maintenant]:
                self._donnees.pop(k, None)
            while len(self._donnees) >= self.taille_max:
                self._donnees.pop(next(iter(self._donnees)), None)
        self._donnees[cle] = (time.monotonic() + (ttl_s if ttl_s is not None else self.ttl_s), valeur)

    async def obtenir(self, cle: Hashable, fabrique: Callable[[], Awaitable[object]], ttl_echec_s: float = 60.0):
        """Valeur en cache, sinon `await fabrique()`. Un échec (None) est mémorisé `ttl_echec_s`
        secondes pour ne pas marteler un service en panne."""
        valeur = self.lire(cle)
        if valeur is not None:
            return None if valeur is _ECHEC else valeur
        verrou = self._verrous.setdefault(cle, asyncio.Lock())
        async with verrou:
            valeur = self.lire(cle)
            if valeur is not None:
                return None if valeur is _ECHEC else valeur
            resultat = await fabrique()
            if resultat is None:
                self.ecrire(cle, _ECHEC, ttl_echec_s)
            else:
                self.ecrire(cle, resultat)
            return resultat


_ECHEC = object()