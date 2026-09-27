"""
backend/import_datatourisme_api.py — Synchronise la catégorie
"fêtes et manifestations" via l'API REST DATAtourisme
(https://api.datatourisme.fr/v1/docs), en remplacement du diffuseur
tant que celui-ci reste inaccessible.

Respecte les quotas documentés (20-30 requêtes concurrentes, ~10/s en
continu, 1000/h) via un simple espacement séquentiel — largement
suffisant vu le volume attendu pour l'Occitanie (pas besoin de
parallélisme ici, la pagination via meta.next est intrinsèquement
séquentielle).

Reprend automatiquement là où une exécution précédente s'est arrêtée
(URL "next" sauvegardée dans sync_state), utile si le run est
interrompu par le plafond horaire ou un timeout GitHub Actions.

Usage :
    python import_datatourisme_api.py
"""

from __future__ import annotations

import argparse
import asyncio
import os
import time

import httpx

from db import init_db_pool, close_db_pool, execute, fetch_one

API_BASE = os.environ.get("DATATOURISME_API_BASE", "https://api.datatourisme.fr/v1")
API_KEY = os.environ["DATATOURISME_API_KEY"]

CATEGORIE = "fetes_manifestations"

# Départements couverts par l'app (mêmes codes INSEE que dans
# enrich-itineraires.yml).
DEPARTEMENTS_INSEE = "09,11,12,30,31,32,34,46,48,65,66,81,82"

# Marge de sécurité, volontairement bien en dessous des limites
# officielles (10 req/s, 1000/h).
DELAI_ENTRE_REQUETES_S = 0.35
PLAFOND_REQUETES_PAR_RUN = 900  # sécurité : on s'arrête avant 1000/h et on reprendra au run suivant

CLE_CURSEUR = "datatourisme_api_fetes_next_url"


async def _lire_curseur() -> str | None:
    row = await fetch_one("SELECT valeur FROM sync_state WHERE cle = %s", (CLE_CURSEUR,))
    return row["valeur"] if row else None


async def _sauver_curseur(valeur: str | None) -> None:
    await execute(
        """
        INSERT INTO sync_state (cle, valeur, maj) VALUES (%s, %s, NOW())
        ON CONFLICT (cle) DO UPDATE SET valeur = EXCLUDED.valeur, maj = NOW()
        """,
        (CLE_CURSEUR, valeur),
    )


def _texte(champ, lang: str = "fr"):
    """Les champs multilingues sont de la forme {"@fr": "...", "@en": "..."}
    (confirmé sur les payloads réels observés)."""
    if champ is None:
        return None
    if isinstance(champ, dict):
        cle_lang = f"@{lang}"
        if cle_lang in champ:
            return champ[cle_lang]
        if lang in champ:
            return champ[lang]
        return next(iter(champ.values()), None)
    if isinstance(champ, list):
        return _texte(champ[0], lang) if champ else None
    return champ


def _texte_liste(champ) -> str | None:
    """Aplatit un champ qui peut être une chaîne, une liste de chaînes,
    ou une liste de dicts multilingues, en une seule chaîne (les valeurs
    multiples sont jointes par ', ').

    Nécessaire car dans les payloads réels de l'API DATAtourisme, des
    champs comme isLocatedAt.address.streetAddress ou
    hasContact.telephone / hasContact.homepage sont systématiquement des
    listes, même quand ils ne contiennent qu'une seule valeur — contrairement
    à d'autres champs "simples" comme postalCode."""
    if champ is None:
        return None
    if isinstance(champ, str):
        return champ or None
    if isinstance(champ, list):
        valeurs = [
            v for v in (_texte(x) if isinstance(x, dict) else x for x in champ)
            if v
        ]
        return ", ".join(valeurs) if valeurs else None
    return None


def _chemin(objet: dict, *cles, defaut=None):
    """Descend dans un dict imbriqué, clé par clé, sans planter si un
    niveau est absent. Déballe automatiquement les listes à un seul
    élément (convention JSON-LD de l'API DATAtourisme : isLocatedAt,
    hasAddressCity, etc. sont presque toujours des tableaux)."""
    courant = objet
    for cle in cles:
        if isinstance(courant, list):
            courant = courant[0] if courant else None
        if not isinstance(courant, dict):
            return defaut
        courant = courant.get(cle)
    if isinstance(courant, list):
        courant = courant[0] if courant else None
    return courant if courant is not None else defaut


def _extraire_description(poi: dict) -> str | None:
    descriptions = poi.get("hasDescription")
    if not descriptions:
        return None
    if not isinstance(descriptions, list):
        descriptions = [descriptions]
    for d in descriptions:
        for cle in ("description", "longDescription", "shortDescription"):
            texte = _texte(d.get(cle) if isinstance(d, dict) else None)
            if texte:
                return texte[:1990]
    return None


def _extraire_photo(poi: dict) -> str | None:
    repr_ = poi.get("hasMainRepresentation")
    if isinstance(repr_, list):
        repr_ = repr_[0] if repr_ else None
    if not isinstance(repr_, dict):
        return None
    return repr_.get("url") or _chemin(repr_, "hasRelatedResource", "locator")


def _extraire_objet(poi: dict) -> dict | None:
    uuid = poi.get("uuid")
    nom = _texte(poi.get("label"))
    geo = _chemin(poi, "isLocatedAt", "geo", defaut={})
    lat, lon = geo.get("latitude"), geo.get("longitude")

    if not (uuid and nom and lat and lon):
        return None

    adresse_obj = _chemin(poi, "isLocatedAt", "address", defaut={})
    commune = _texte(_chemin(adresse_obj, "hasAddressCity", "label"))
    departement = _texte(_chemin(adresse_obj, "hasAddressCity", "isPartOfDepartment", "label"))
    rue = _texte_liste(adresse_obj.get("streetAddress"))
    cp = adresse_obj.get("postalCode")
    adresse_complete = ", ".join(p for p in (rue, cp, commune) if p) or None

    contact = poi.get("hasContact") or {}
    if isinstance(contact, list):
        contact = contact[0] if contact else {}

    return {
        "identifiant_dt": str(uuid)[:95],
        "nom": nom[:250],
        "commune": (commune or "")[:250] or None,
        "departement": (departement or "")[:95] or None,
        "latitude": float(lat),
        "longitude": float(lon),
        "adresse": (adresse_complete or "")[:495] or None,
        "telephone": (_texte_liste(contact.get("telephone")) or "")[:45] or None,
        "site_web": (_texte_liste(contact.get("homepage")) or "")[:495] or None,
        "description": _extraire_description(poi),
        "photo_url": (_extraire_photo(poi) or "")[:495] or None,
    }


async def main():
    await init_db_pool()
    try:
        url = await _lire_curseur()
        if url:
            print(f"Reprise à partir du curseur sauvegardé : {url[:80]}…", flush=True)
        else:
            url = (
                f"{API_BASE}/entertainmentAndEvent"
                f"?page_size=100&lang=fr"
                f"&filters=isLocatedAt.address.hasAddressCity.isPartOfDepartment.insee[in]={DEPARTEMENTS_INSEE}"
            )

        importes = 0
        ignores = 0
        requetes = 0
        premier_log_fait = False

        async with httpx.AsyncClient(timeout=30) as client:
            while url and requetes < PLAFOND_REQUETES_PAR_RUN:
                debut = time.monotonic()

                resp = await client.get(url, headers={"X-API-Key": API_KEY})
                requetes += 1

                if resp.status_code == 429:
                    attente = float(resp.headers.get("Retry-After", "5"))
                    print(f"  ⚠️ 429 reçu, pause {attente}s", flush=True)
                    await asyncio.sleep(attente)
                    continue

                resp.raise_for_status()
                data = resp.json()

                if not premier_log_fait and data.get("objects"):
                    print("── Premier objet brut reçu (à comparer aux extractions) ──", flush=True)
                    print(data["objects"][0], flush=True)
                    premier_log_fait = True

                for poi in data.get("objects", []):
                    try:
                        objet = _extraire_objet(poi)
                    except Exception as exc:
                        # Un POI malformé ne doit jamais interrompre toute
                        # la synchro : on le logue et on continue.
                        ignores += 1
                        print(
                            f"  ⚠️ POI ignoré (erreur d'extraction: {exc}) — uuid={poi.get('uuid')!r}",
                            flush=True,
                        )
                        continue

                    if not objet:
                        ignores += 1
                        continue

                    await execute(
                        """
                        INSERT INTO datatourisme_objets
                            (identifiant_dt, nom, categorie, commune, departement,
                             latitude, longitude, adresse, telephone, site_web,
                             description, photo_url)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (identifiant_dt) DO UPDATE SET
                            nom = EXCLUDED.nom, commune = EXCLUDED.commune,
                            departement = EXCLUDED.departement,
                            latitude = EXCLUDED.latitude, longitude = EXCLUDED.longitude,
                            adresse = EXCLUDED.adresse, telephone = EXCLUDED.telephone,
                            site_web = EXCLUDED.site_web, description = EXCLUDED.description,
                            photo_url = EXCLUDED.photo_url
                        """,
                        (
                            objet["identifiant_dt"], objet["nom"], CATEGORIE,
                            objet["commune"], objet["departement"],
                            objet["latitude"], objet["longitude"], objet["adresse"],
                            objet["telephone"], objet["site_web"],
                            objet["description"], objet["photo_url"],
                        ),
                    )
                    importes += 1

                url = _chemin(data, "meta", "next")

                # Sauvegarde le curseur à CHAQUE page, pas seulement à la
                # fin — si le run est interrompu, on ne repart pas de zéro.
                await _sauver_curseur(url)

                if importes % 200 == 0 and importes:
                    print(f"  … {importes} importés (+ {ignores} ignorés), {requetes} requêtes", flush=True)

                ecoule = time.monotonic() - debut
                await asyncio.sleep(max(0.0, DELAI_ENTRE_REQUETES_S - ecoule))

        if url:
            print(
                f"\nPlafond de {PLAFOND_REQUETES_PAR_RUN} requêtes atteint pour ce run — "
                f"reprise automatique au prochain run. {importes} importé(s) jusqu'ici.",
                flush=True,
            )
        else:
            await _sauver_curseur(None)  # synchro complète, on repart de la page 1 au prochain cron
            print(f"\nSynchro complète : {importes} importé(s), {ignores} ignoré(s).", flush=True)

    finally:
        await close_db_pool()


if __name__ == "__main__":
    asyncio.run(main())