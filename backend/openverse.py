"""
backend/openverse.py â€” Client pour l'API Openverse (images sous
licences ouvertes / domaine public), utilisÃ© par le module Landscape
Studio pour la recherche de paysages de rÃ©fÃ©rence.

Openverse ne garantit pas lui-mÃªme l'exactitude de ses mÃ©tadonnÃ©es de
licence â€” on les conserve telles quelles et on affiche toujours le
lien vers la source d'origine plutÃ´t que de prÃ©tendre Ã  une garantie
qu'on n'a pas.

Passe par openverse_cache (Redis/Upstash) exactement comme
navigation_cache est utilisÃ© dans main.py pour les itinÃ©raires : une
recherche dÃ©jÃ  vue est servie sans latence, et si un trÃ¨s grand
nombre d'utilisateurs interrogent le mÃªme terme au mÃªme instant sur
un cache froid, un seul appel part rÃ©ellement vers Openverse â€” les
autres attendent son rÃ©sultat via le verrou anti-rafale.
"""

import os

import httpx

from .openverse_cache import openverse_cache

OPENVERSE_API_URL = os.getenv(
    "OPENVERSE_API_URL", "https://api.openverse.org/v1/images/"
)


async def _appeler_openverse(query: str, page: int, page_size: int) -> dict:
    params = {"q": query, "page": page, "page_size": page_size}

    async with httpx.AsyncClient(timeout=15) as client:
        reponse = await client.get(OPENVERSE_API_URL, params=params)
        reponse.raise_for_status()
        data = reponse.json()

    resultats = []
    for image in data.get("results", []):
        resultats.append({
            "id_openverse": image.get("id"),
            "titre": image.get("title"),
            "image_url": image.get("url"),
            "thumbnail_url": image.get("thumbnail"),
            "auteur": image.get("creator"),
            "auteur_url": image.get("creator_url"),
            "licence": image.get("license"),
            "licence_url": image.get("license_url"),
            "source_nom": image.get("source") or image.get("provider"),
            "source_url": image.get("foreign_landing_url"),
            "tags": [t.get("name") for t in (image.get("tags") or []) if t.get("name")],
        })
    return {
        "page": data.get("page", page),
        "page_count": data.get("page_count", 0),
        "result_count": data.get("result_count", 0),
        "results": resultats,
    }


async def rechercher_openverse(query: str, page: int = 1, page_size: int = 20) -> dict:
    """Recherche des images Openverse, en passant par le cache Redis.

    MÃªme sÃ©quence que navigation_cache dans main.py : cache_key pour la
    valeur, lock_key = cache_key + ":lock" pour le verrou (deux clÃ©s
    distinctes â€” sinon le verrou et le rÃ©sultat s'Ã©crasent l'un
    l'autre). Si Redis est indisponible, on part directement vers
    Openverse sans jamais bloquer l'utilisateur.
    """
    cache_key = openverse_cache.construire_cle(query, page, page_size)
    lock_key = f"{cache_key}:lock"

    if openverse_cache.enabled:
        resultat = await openverse_cache.get(cache_key)
        if resultat:
            resultat["cache_hit"] = True
            return resultat

    if not openverse_cache.enabled:
        return await _appeler_openverse(query, page, page_size)

    verrou_obtenu = await openverse_cache.acquire_lock(lock_key)

    if not verrou_obtenu:
        # Un autre appel est dÃ©jÃ  en train de rÃ©cupÃ©rer ce mÃªme rÃ©sultat â€”
        # on attend le sien plutÃ´t que de dupliquer la requÃªte HTTP.
        resultat = await openverse_cache.wait_for_result(cache_key)
        if resultat:
            resultat["cache_hit"] = True
            resultat["cache_deduplicated"] = True
            return resultat
        # Personne n'a livrÃ© Ã  temps (verrou expirÃ©/plantage) â€” on
        # retente d'obtenir le verrou avant de partir nous-mÃªmes.
        verrou_obtenu = await openverse_cache.acquire_lock(lock_key)

    try:
        resultat = await _appeler_openverse(query, page, page_size)
        await openverse_cache.set(cache_key, resultat)
        return resultat
    finally:
        if verrou_obtenu:
            await openverse_cache.delete(lock_key)
