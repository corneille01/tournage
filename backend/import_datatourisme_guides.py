"""Import des guides DATAtourisme vers la table guides.
Les fiches importées restent en_attente et nécessitent une validation humaine.
"""
from __future__ import annotations

import argparse
import asyncio
import os
from typing import Any

import httpx
from dotenv import load_dotenv

from db import close_db_pool, execute, init_db_pool

API_URL = "https://api.datatourisme.fr/v1/catalog"
OCCITANIE_BBOX = "45.2,-0.5,42.2,4.9"
TYPES = {
    "ProfessionalTourGuide": "guide_conferencier",
    "TourGuideAgency": "autre",
    "VolunteerTourGuideOrGreeter": "autre",
}
FIELDS = "uuid,uri,label,type,isLocatedAt,hasDescription,hasContact,hasBeenCreatedBy,lastUpdate,lastUpdateDatatourisme"


def _text(value: Any) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        for key in ("fr", "@value", "value", "label"):
            if key in value:
                found = _text(value[key])
                if found:
                    return found
    if isinstance(value, list):
        for item in value:
            found = _text(item)
            if found:
                return found
    return None


def _walk(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _coords(item: dict) -> tuple[float | None, float | None]:
    for d in _walk(item.get("isLocatedAt")):
        lat = d.get("latitude")
        lon = d.get("longitude")
        if lat is not None and lon is not None:
            try:
                return float(lat), float(lon)
            except (TypeError, ValueError):
                pass
        coords = d.get("coordinates")
        if isinstance(coords, list) and len(coords) >= 2:
            try:
                return float(coords[1]), float(coords[0])
            except (TypeError, ValueError):
                pass
    return None, None


def _website(item: dict) -> str | None:
    for d in _walk(item.get("hasContact")):
        for key in ("homepage", "website", "url", "uri"):
            value = d.get(key)
            if isinstance(value, str) and value.startswith(("http://", "https://")):
                return value
    return None


def extract(item: dict) -> dict | None:
    uuid = item.get("uuid")
    if not uuid:
        return None
    lat, lon = _coords(item)
    if lat is None or lon is None:
        return None
    typ = item.get("type")
    if isinstance(typ, list):
        typ = typ[0] if typ else None
    if isinstance(typ, dict):
        typ = typ.get("@id") or typ.get("id") or typ.get("label")
    type_name = str(typ or "TourGuideAgency").split("/")[-1]
    return {
        "uuid": str(uuid),
        "nom": _text(item.get("label")) or "Guide DATAtourisme",
        "type_guide": TYPES.get(type_name, "autre"),
        "datatourisme_type": type_name,
        "bio": _text(item.get("hasDescription")),
        "latitude": lat,
        "longitude": lon,
        "site_web": _website(item),
        "last_update": item.get("lastUpdateDatatourisme") or item.get("lastUpdate"),
    }


async def fetch_type(client: httpx.AsyncClient, api_key: str, typ: str, bbox: str | None, limit: int | None, dry_run: bool) -> int:
    params = {"type": typ, "page_size": 100, "fields": FIELDS, "lang": "fr"}
    if bbox:
        params["bbox"] = bbox
    headers = {"X-API-Key": api_key}
    url = API_URL
    count = 0
    while url:
        response = await client.get(url, params=params if url == API_URL else None, headers=headers, timeout=60)
        response.raise_for_status()
        data = response.json()
        for item in data.get("results", []):
            guide = extract(item)
            if not guide:
                continue
            count += 1
            print(f"{guide['nom']} | {guide['uuid']}")
            if not dry_run:
                await upsert(guide)
            if limit and count >= limit:
                return count
        url = (data.get("meta") or {}).get("next")
        params = None
    return count


async def upsert(g: dict) -> None:
    await execute("""
        INSERT INTO guides (
            nom, type_guide, bio, specialites, langues, publics, mobilite,
            latitude, longitude, rayon_intervention_km, capacite_max,
            site_web, statut, source_donnee, datatourisme_uuid,
            datatourisme_type, datatourisme_last_update
        ) VALUES (
            %s, %s, %s, ARRAY[]::text[], ARRAY[]::text[], ARRAY[]::text[], ARRAY[]::text[],
            %s, %s, 30, NULL, %s, 'en_attente', 'datatourisme', %s, %s, %s
        )
        ON CONFLICT (datatourisme_uuid) WHERE datatourisme_uuid IS NOT NULL
        DO UPDATE SET
            nom = EXCLUDED.nom,
            type_guide = EXCLUDED.type_guide,
            bio = EXCLUDED.bio,
            latitude = EXCLUDED.latitude,
            longitude = EXCLUDED.longitude,
            site_web = EXCLUDED.site_web,
            datatourisme_type = EXCLUDED.datatourisme_type,
            datatourisme_last_update = EXCLUDED.datatourisme_last_update,
            source_donnee = 'datatourisme'
    """, g["nom"], g["type_guide"], g["bio"], g["latitude"], g["longitude"], g["site_web"], g["uuid"], g["datatourisme_type"], g["last_update"])


async def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--france", action="store_true")
    parser.add_argument("--avec-greeters", action="store_true")
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()

    api_key = os.getenv("DATATOURISME_API_KEY")
    if not api_key:
        raise SystemExit("DATATOURISME_API_KEY est manquante")

    types = ["ProfessionalTourGuide", "TourGuideAgency"]
    if args.avec_greeters:
        types.append("VolunteerTourGuideOrGreeter")
    bbox = None if args.france else OCCITANIE_BBOX

    await init_db_pool()
    try:
        async with httpx.AsyncClient() as client:
            total = 0
            for typ in types:
                total += await fetch_type(client, api_key, typ, bbox, args.limit, args.dry_run)
            print(f"Total importé/analysé : {total}")
    finally:
        await close_db_pool()


if __name__ == "__main__":
    asyncio.run(main())
