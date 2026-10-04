"""Import des guides DATAtourisme vers la table guides.

- Une requête par type (ProfessionalTourGuide, TourGuideAgency ; greeters en option).
- Les fiches importées de l'API sont créées en 'actif' (option --statut pour changer).
  Les fiches saisies via le formulaire du site restent 'en_attente' (validation à la main).
  Une fiche déjà importée n'a jamais son statut écrasé (un guide désactivé le reste).
- Toutes les pages sont parcourues en suivant meta.next.
- Aucune photo n'est stockée (droits réservés / CC BY-NC-ND, personnes identifiables).
- L'API n'expose plus les e-mails : contact = site web, sinon téléphone.

Usage :
    python import_datatourisme_guides.py --dry-run     # n'écrit rien, ne touche pas la base
    python import_datatourisme_guides.py               # Occitanie
    python import_datatourisme_guides.py --france --avec-greeters
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin

import httpx
from dotenv import load_dotenv
from shapely.geometry import Point, shape

from db import close_db_pool, execute, init_db_pool

API_URL = "https://api.datatourisme.fr/v1/catalog"
# geo_bounding = haut_gauche_lat,haut_gauche_lon,bas_droite_lat,bas_droite_lon (doc officielle)
OCCITANIE_BBOX = "45.2,-0.5,42.2,4.9"

OCCITANIE_GEOJSON = os.path.join(
    os.path.dirname(__file__),
    "../frontend/contour-occitanie.geojson"
)

PAGE_SIZE = 100
TYPES = {
    "ProfessionalTourGuide": "guide_conferencier",
    "TourGuideAgency": "autre",
    "VolunteerTourGuideOrGreeter": "autre",
}
RESEAUX = ("facebook.", "instagram.", "twitter.", "x.com", "linkedin.", "youtube.", "tiktok.")
STATUT = "actif"
ERREURS: list[tuple[str, str]] = []
FIELDS = "uuid,uri,label,type,isLocatedAt,hasDescription,hasContact,hasBeenCreatedBy,lastUpdate,lastUpdateDatatourisme"


def _text(value: Any) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        for key in ("fr", "@fr", "@value", "value", "label", "en", "@en"):
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






def _charger_polygone_occitanie():
    with open(OCCITANIE_GEOJSON, encoding="utf-8") as f:
        data = json.load(f)

    if data.get("type") == "FeatureCollection":
        features = data.get("features") or []
        if not features:
            raise RuntimeError("Le GeoJSON Occitanie ne contient aucune feature.")
        return shape(features[0]["geometry"])

    if data.get("type") == "Feature":
        return shape(data["geometry"])

    return shape(data)


OCCITANIE_POLYGON = _charger_polygone_occitanie()
def _walk(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _strings(value: Any):
    if isinstance(value, str):
        yield value.strip()
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


def _coords(item: dict) -> tuple[float | None, float | None]:
    for d in _walk(item.get("isLocatedAt")):
        lat, lon = d.get("latitude"), d.get("longitude")
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
            for s in _strings(d.get(key)):
                if s.startswith(("http://", "https://")) and not any(r in s.lower() for r in RESEAUX):
                    return s
    return None


def _phone(item: dict) -> str | None:
    for d in _walk(item.get("hasContact")):
        for s in _strings(d.get("telephone")):
            if s:
                return s
    return None


def _type_name(item: dict) -> str | None:
    typ = item.get("type")
    liste = typ if isinstance(typ, list) else [typ]
    noms = [str(t.get("@id") or t.get("id") or t.get("label")) if isinstance(t, dict) else str(t) for t in liste if t]
    noms = [n.split("/")[-1].split(":")[-1] for n in noms]
    for n in noms:
        if n in TYPES:
            return n
    return None


def extract(item: dict) -> dict | None:
    uuid = item.get("uuid")
    type_name = _type_name(item)
    if not uuid or not type_name:
        return None
    lat, lon = _coords(item)
    if lat is None or lon is None:
        return None
    site, tel = _website(item), _phone(item)
    createur = item.get("hasBeenCreatedBy")
    return {
        "uuid": str(uuid),
        "nom": _cut(_text(item.get("label")) or "Guide DATAtourisme", 255),
        "type_guide": TYPES[type_name],
        "datatourisme_type": type_name,
        "bio": _text(item.get("hasDescription")),
        "latitude": lat,
        "longitude": lon,
        "site_web": _cut(site, 500),
        "lien_contact": None if site else _cut(f"tel:{tel.replace(' ', '')}" if tel else None, 500),
        "createur": _text(createur.get("legalName")) if isinstance(createur, dict) else None,
        "last_update": _date(item.get("lastUpdateDatatourisme") or item.get("lastUpdate")),
    }


def _date(value: Any) -> datetime | None:
    """asyncpg exige un datetime (pas une chaîne) pour une colonne TIMESTAMPTZ."""
    s = _text(value)
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        try:
            d = datetime.fromisoformat(s[:10])
        except ValueError:
            return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _cut(value: str | None, n: int) -> str | None:
    return value[:n] if value else value


def _dans_occitanie(g: dict) -> bool:
    point = Point(g["longitude"], g["latitude"])
    return OCCITANIE_POLYGON.covers(point)


def _objets(data: dict) -> list:
    return data.get("objects") or data.get("results") or []


async def _get(client: httpx.AsyncClient, api_key: str, url: str, params: dict | None) -> httpx.Response:
    for essai in range(3):
        r = await client.get(url, params=params, headers={"X-API-Key": api_key, "Accept": "application/json"},
                             timeout=60, follow_redirects=True)
        if r.status_code in (429, 500, 502, 503, 504) and essai < 2:
            attente = 5 * (essai + 1)
            print(f"  HTTP {r.status_code}, nouvel essai dans {attente}s")
            await asyncio.sleep(attente)
            continue
        break
    if r.status_code >= 400:
        print(f"HTTP {r.status_code} : {r.text[:500]}")
    return r


async def fetch_type(client, api_key, typ, france, limit, dry_run) -> tuple[int, int]:
    """Parcourt TOUTES les pages d'un type en suivant meta.next jusqu'à null."""
    params = {"type": typ, "page_size": PAGE_SIZE, "fields": FIELDS, "lang": "fr,en"}
    if not france:
       params["geo_bounding"] = OCCITANIE_BBOX
    url, page, vus, lus, gardes = API_URL, 1, set(), 0, 0
    while url:
        r = await _get(client, api_key, url, params)
        r.raise_for_status()
        data = r.json()
        objets = _objets(data)
        meta = data.get("meta") or {}
        print(f"[{typ}] page {page} : {len(objets)} fiches (total annoncé : {meta.get('total')}, "
              f"pages : {meta.get('total_pages')})")
        if page == 1 and dry_run and objets:
            print(json.dumps(objets[0], ensure_ascii=False, indent=1)[:1500])
        for item in objets:
            if item.get("uuid") in vus:
                continue
            vus.add(item.get("uuid"))
            lus += 1
            g = extract(item)
            if not g:
                continue
            if not france and not _dans_occitanie(g):
                continue
            gardes += 1
            print(f"  {g['nom']} | {g['uuid']}")
            if not dry_run:
                try:
                    await upsert(g, STATUT)
                except Exception as e:  # une fiche défectueuse ne doit pas bloquer les autres
                    ERREURS.append((g["uuid"], repr(e)))
                    print(f"  !! ERREUR sur {g['uuid']} : {e!r}")
            if limit and gardes >= limit:
                return lus, gardes
        suivant = meta.get("next")
        url = urljoin(API_URL, suivant) if suivant else None
        params = None  # l'URL next contient déjà tous les paramètres
        page += 1
        await asyncio.sleep(0.2)  # quota : ~10 requêtes/s max, 1000/h
    return lus, gardes


async def upsert(g: dict, statut: str = "actif") -> None:
    await execute("""
        INSERT INTO guides (
            nom, type_guide, bio, specialites, langues, publics, mobilite,
            latitude, longitude, rayon_intervention_km, capacite_max,
            site_web, lien_contact, statut, source_donnee, datatourisme_uuid,
            datatourisme_type, datatourisme_creator, datatourisme_last_update
        ) VALUES (
            %s, %s, %s, ARRAY[]::text[], ARRAY[]::text[], ARRAY[]::text[], ARRAY[]::text[],
            %s, %s, 30, NULL, %s, %s, %s, 'datatourisme', %s, %s, %s, %s
        )
        ON CONFLICT (datatourisme_uuid) WHERE datatourisme_uuid IS NOT NULL
        DO UPDATE SET
            nom = EXCLUDED.nom, type_guide = EXCLUDED.type_guide, bio = EXCLUDED.bio,
            latitude = EXCLUDED.latitude, longitude = EXCLUDED.longitude,
            site_web = EXCLUDED.site_web, lien_contact = EXCLUDED.lien_contact,
            datatourisme_type = EXCLUDED.datatourisme_type,
            datatourisme_creator = EXCLUDED.datatourisme_creator,
            datatourisme_last_update = EXCLUDED.datatourisme_last_update
        WHERE guides.source_donnee = 'datatourisme'
    """, (g["nom"], g["type_guide"], g["bio"], g["latitude"], g["longitude"], g["site_web"],
          g["lien_contact"], statut, g["uuid"], g["datatourisme_type"], g["createur"], g["last_update"]))


async def main() -> None:
    load_dotenv()
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true", help="n'écrit rien (base non utilisée)")
    p.add_argument("--france", action="store_true", help="toute la France au lieu de l'Occitanie")
    p.add_argument("--rectangle-seul", action="store_true",
                   help="geo_bounding + filtre local lat/lon, sans filtre département (si le filtre renvoie 0)")
    p.add_argument("--avec-greeters", action="store_true")
    p.add_argument("--statut", choices=["actif", "en_attente", "inactif"], default="actif",
                   help="statut des NOUVELLES fiches importées (défaut : actif)")
    p.add_argument("--limit", type=int, help="nombre max de fiches retenues par type")
    args = p.parse_args()
    global STATUT
    STATUT = args.statut

    api_key = os.getenv("DATATOURISME_API_KEY")
    if not api_key:
        raise SystemExit("DATATOURISME_API_KEY est manquante")

    types = ["ProfessionalTourGuide", "TourGuideAgency"] + (["VolunteerTourGuideOrGreeter"] if args.avec_greeters else [])
    mode = "rectangle" if args.rectangle_seul else "departements"

    if not args.dry_run:
        await init_db_pool()
    try:
        async with httpx.AsyncClient() as client:
            tot_lus = tot_gardes = 0
            for typ in types:
                lus, gardes = await fetch_type(client, api_key, typ, mode, args.france, args.limit, args.dry_run)
                print(f"[{typ}] reçus : {lus} | retenus : {gardes}")
                tot_lus += lus
                tot_gardes += gardes
            print(f"Total reçus : {tot_lus} | retenus : {tot_gardes}{' (simulation)' if args.dry_run else ''}")
            if ERREURS:
                print(f"{len(ERREURS)} fiche(s) en erreur, premières : {ERREURS[:3]}")
                if len(ERREURS) >= tot_gardes:
                    sys.exit(1)
    finally:
        if not args.dry_run:
            await close_db_pool()


if __name__ == "__main__":
    asyncio.run(main())