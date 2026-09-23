"""
backend/openverse.py — Client pour l'API Openverse (images sous
licences ouvertes / domaine public), utilisé par le module Landscape
Studio pour la recherche de paysages de référence.

Openverse ne garantit pas lui-même l'exactitude de ses métadonnées de
licence — on les conserve telles quelles et on affiche toujours le
lien vers la source d'origine plutôt que de prétendre à une garantie
qu'on n'a pas.

Passe par openverse_cache (Redis/Upstash) exactement comme
navigation_cache est utilisé dans main.py pour les itinéraires : une
recherche déjà vue est servie sans latence, et si un très grand
nombre d'utilisateurs interrogent le même terme au même instant sur
un cache froid, un seul appel part réellement vers Openverse — les
autres attendent son résultat via le verrou anti-rafale.
"""

import os

import httpx

from openverse_cache import openverse_cache

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

    Même séquence que navigation_cache dans main.py : cache_key pour la
    valeur, lock_key = cache_key + ":lock" pour le verrou (deux clés
    distinctes — sinon le verrou et le résultat s'écrasent l'un
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
        # Un autre appel est déjà en train de récupérer ce même résultat —
        # on attend le sien plutôt que de dupliquer la requête HTTP.
        resultat = await openverse_cache.wait_for_result(cache_key)
        if resultat:
            resultat["cache_hit"] = True
            resultat["cache_deduplicated"] = True
            return resultat
        # Personne n'a livré à temps (verrou expiré/plantage) — on
        # retente d'obtenir le verrou avant de partir nous-mêmes.
        verrou_obtenu = await openverse_cache.acquire_lock(lock_key)

    try:
        resultat = await _appeler_openverse(query, page, page_size)
        await openverse_cache.set(cache_key, resultat)
        return resultat
    finally:
        if verrou_obtenu:
            await openverse_cache.delete(lock_key)