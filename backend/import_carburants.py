"""Import quotidien des prix des carburants vers la table stations_carburant.

Source : data.economie.gouv.fr, jeu « Prix des carburants en France - Flux
instantané - v2 » (API Opendatasoft Explore v2.1, Licence Ouverte 2.0).
Une ligne = un point de vente, avec tous ses carburants et prix en colonnes.

Périmètre : Occitanie par défaut (mêmes 13 départements que l'import des
guides). L'option --france élargit à tout le territoire.

Dates de mise à jour : chaque station porte la date de dernière mise à jour de
ses prix (champ « update »). Elle est stockée dans maj_prix. Si l'API expose en
plus une date par carburant (champ contenant le nom du carburant et une date),
elle est stockée dans maj_carburants. Le serveur s'en sert pour écarter les prix
périmés et afficher l'âge de chaque prix. L'import signale aussi une source
figée (aucune mise à jour récente).

Pagination :
- l'endpoint /records est limité à 100 lignes par requête (limit=100) et à
  offset + limit <= 10 000 ;
- on découpe donc par département (dep_code), chacun tenant largement sous
  10 000 lignes, puis on pagine par pas de 100 ;
- les stations sans coordonnées sont ignorées (inutilisables pour une
  recherche de proximité) ;
- aucun « select » : on récupère tous les champs, ce qui évite de deviner leurs
  noms exacts et permet de repérer les dates par carburant si elles existent.

Sécurités :
- si le jeu interrogé dépasse 60 000 lignes, il s'agit d'un historique et non
  du flux instantané : l'import s'arrête (option --force pour passer outre) ;
- les stations absentes de la synchro ne sont supprimées que si l'import couvre
  tout le périmètre (pas d'option --departements) et atteint un volume minimal ;
  seuls les départements du périmètre sont concernés.

Usage :
    python import_carburants.py --dry-run
    python import_carburants.py
    python import_carburants.py --departements 11,30,31,34
    python import_carburants.py --france
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any

import httpx
from dotenv import load_dotenv

from db import close_db_pool, execute, executemany, init_db_pool

DATASET = os.getenv("CARBURANTS_DATASET", "prix-des-carburants-en-france-flux-instantane-v2")
API_URL = f"https://data.economie.gouv.fr/api/explore/v2.1/catalog/datasets/{DATASET}/records"
PAGE_SIZE = 100            # maximum autorisé par l'API sur /records
OFFSET_MAX = 10_000        # offset + limit doit rester sous cette borne
PAUSE_ENTRE_REQUETES_S = 0.15
SEUIL_HISTORIQUE = 60_000  # au-delà : ce n'est pas le flux instantané
SEUIL_PURGE_MIN_OCCITANIE = 300   # l'Occitanie compte environ 1 000 stations
SEUIL_PURGE_MIN_FRANCE = 5_000
SOURCE_FIGEE_JOURS = 7

DEPARTEMENTS_OCCITANIE = ["09", "11", "12", "30", "31", "32", "34", "46", "48", "65", "66", "81", "82"]
DEPARTEMENTS_FRANCE = (
    [f"{i:02d}" for i in range(1, 96) if i != 20]
    + ["2A", "2B", "971", "972", "973", "974", "976"]
)

CARBURANTS_CLES = ("gazole", "sp95", "sp98", "e10", "e85", "gplc")
PRIX_MIN, PRIX_MAX = 0.3, 6.0   # bornes de vraisemblance en EUR/L


def _prix(valeur: Any) -> float | None:
    try:
        p = float(valeur)
    except (TypeError, ValueError):
        return None
    return round(p, 3) if PRIX_MIN <= p <= PRIX_MAX else None


def _liste(valeur: Any) -> list[str]:
    if not valeur:
        return []
    if isinstance(valeur, str):
        return [valeur]
    return [str(x).strip() for x in valeur if str(x).strip()]


def _date(valeur: Any) -> datetime | None:
    if not valeur:
        return None
    try:
        d = datetime.fromisoformat(str(valeur).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _dates_par_carburant(rec: dict) -> dict[str, str]:
    """Dates de mise à jour propres à un carburant, si l'API en fournit
    (champ texte dont le nom contient un carburant et dont la valeur est une
    date, hors champs de prix). Renvoie {carburant: date ISO}."""
    trouvees: dict[str, str] = {}
    for cle, valeur in rec.items():
        nom = str(cle).lower()
        if nom.startswith("price_") or not isinstance(valeur, str):
            continue
        for carburant in CARBURANTS_CLES:
            if carburant in nom:
                d = _date(valeur)
                if d:
                    trouvees[carburant] = d.isoformat()
    return trouvees


def _horaires(valeur: Any) -> str | None:
    """Le champ timetable arrive sous forme de chaîne JSON (ou déjà de dict)."""
    if not valeur:
        return None
    if isinstance(valeur, str):
        try:
            valeur = json.loads(valeur)
        except ValueError:
            return None
    return json.dumps(valeur, ensure_ascii=False) if isinstance(valeur, dict) else None


def normaliser(rec: dict) -> dict | None:
    geo = rec.get("geo_point") or {}
    try:
        lat, lon = float(geo["lat"]), float(geo["lon"])
        ident = int(rec["id"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    automate = {"oui": True, "non": False}.get(str(rec.get("automate_24_24") or "").strip().lower())
    return {
        "id": ident, "latitude": lat, "longitude": lon,
        "nom": (rec.get("name") or None), "marque": (rec.get("brand") or None),
        "adresse": (rec.get("address") or None), "code_postal": (rec.get("cp") or None),
        "commune": (rec.get("com_arm_name") or None),
        "dep_code": (rec.get("dep_code") or None), "dep_nom": (rec.get("dep_name") or None),
        "region": (rec.get("reg_name") or None),
        "type_route": (str(rec.get("pop") or "")[:1].upper() or None),
        "automate": automate, "horaires": _horaires(rec.get("timetable")),
        "dispo": _liste(rec.get("fuel")), "rupture": _liste(rec.get("shortage")),
        "gazole": _prix(rec.get("price_gazole")), "sp95": _prix(rec.get("price_sp95")),
        "sp98": _prix(rec.get("price_sp98")), "e10": _prix(rec.get("price_e10")),
        "e85": _prix(rec.get("price_e85")), "gplc": _prix(rec.get("price_gplc")),
        "maj": _date(rec.get("update")), "services": _liste(rec.get("services")),
        "maj_carburants": json.dumps(_dates_par_carburant(rec)) if _dates_par_carburant(rec) else None,
    }


async def _get(client: httpx.AsyncClient, params: dict) -> dict:
    derniere = None
    for tentative in range(5):
        try:
            r = await client.get(API_URL, params=params)
            if r.status_code in (429, 500, 502, 503, 504):
                raise httpx.HTTPStatusError(f"HTTP {r.status_code}", request=r.request, response=r)
            r.raise_for_status()
            return r.json()
        except (httpx.HTTPError, ValueError) as exc:
            derniere = exc
            await asyncio.sleep(2 ** tentative)
    raise RuntimeError(f"API carburants injoignable après 5 essais : {derniere}")


async def fetch_partition(client: httpx.AsyncClient, where: str) -> list[dict]:
    """Récupère toutes les lignes d'une partition, 100 par 100."""
    lignes: list[dict] = []
    offset = 0
    while True:
        data = await _get(client, {
            "where": where, "order_by": "id",
            "limit": PAGE_SIZE, "offset": offset,
        })
        page = data.get("results") or []
        lignes.extend(page)
        total = int(data.get("total_count") or 0)
        offset += PAGE_SIZE
        if len(page) < PAGE_SIZE or offset >= total:
            break
        if offset + PAGE_SIZE > OFFSET_MAX:
            print(f"  ! partition tronquée à {offset} lignes sur {total} : {where}")
            break
        await asyncio.sleep(PAUSE_ENTRE_REQUETES_S)
    return lignes


UPSERT = """
INSERT INTO stations_carburant (
    id, latitude, longitude, nom, marque, adresse, code_postal, commune,
    dep_code, dep_nom, region, type_route, automate_24_24, horaires,
    carburants_dispo, carburants_rupture,
    prix_gazole, prix_sp95, prix_sp98, prix_e10, prix_e85, prix_gplc,
    maj_prix, maj_carburants, services, synchro_at
) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s)
ON CONFLICT (id) DO UPDATE SET
    latitude = EXCLUDED.latitude, longitude = EXCLUDED.longitude,
    nom = EXCLUDED.nom, marque = EXCLUDED.marque, adresse = EXCLUDED.adresse,
    code_postal = EXCLUDED.code_postal, commune = EXCLUDED.commune,
    dep_code = EXCLUDED.dep_code, dep_nom = EXCLUDED.dep_nom, region = EXCLUDED.region,
    type_route = EXCLUDED.type_route, automate_24_24 = EXCLUDED.automate_24_24,
    horaires = EXCLUDED.horaires,
    carburants_dispo = EXCLUDED.carburants_dispo, carburants_rupture = EXCLUDED.carburants_rupture,
    prix_gazole = EXCLUDED.prix_gazole, prix_sp95 = EXCLUDED.prix_sp95,
    prix_sp98 = EXCLUDED.prix_sp98, prix_e10 = EXCLUDED.prix_e10,
    prix_e85 = EXCLUDED.prix_e85, prix_gplc = EXCLUDED.prix_gplc,
    maj_prix = EXCLUDED.maj_prix, maj_carburants = EXCLUDED.maj_carburants,
    services = EXCLUDED.services,
    synchro_at = EXCLUDED.synchro_at
"""


def _params(s: dict, synchro: datetime) -> tuple:
    return (
        s["id"], s["latitude"], s["longitude"], s["nom"], s["marque"], s["adresse"],
        s["code_postal"], s["commune"], s["dep_code"], s["dep_nom"], s["region"],
        s["type_route"], s["automate"], s["horaires"], s["dispo"], s["rupture"],
        s["gazole"], s["sp95"], s["sp98"], s["e10"], s["e85"], s["gplc"],
        s["maj"], s["maj_carburants"], s["services"], synchro,
    )


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="n'écrit rien en base")
    parser.add_argument("--departements", default="", help="liste séparée par des virgules (ex. 11,34)")
    parser.add_argument("--france", action="store_true", help="toute la France au lieu de l'Occitanie")
    parser.add_argument("--force", action="store_true", help="ignore le garde-fou « jeu trop volumineux »")
    args = parser.parse_args()
    load_dotenv()

    debut = datetime.now(timezone.utc)
    demandes = [d.strip().upper() for d in args.departements.split(",") if d.strip()]
    perimetre = DEPARTEMENTS_FRANCE if args.france else DEPARTEMENTS_OCCITANIE
    cibles = demandes or perimetre
    partitions = [(d, f'dep_code="{d}" AND geo_point IS NOT NULL') for d in cibles]
    print(f"Périmètre : {'France entière' if args.france and not demandes else 'Occitanie' if not demandes else 'départements ' + ','.join(demandes)}")

    async with httpx.AsyncClient(timeout=60, headers={"User-Agent": "Pelify/1.0 (import carburants)"}) as client:
        sonde = await _get(client, {"limit": 1, "select": "id"})
        total_jeu = int(sonde.get("total_count") or 0)
        print(f"Jeu {DATASET} : {total_jeu} lignes")
        if total_jeu > SEUIL_HISTORIQUE and not args.force:
            print(
                f"Arrêt : {total_jeu} lignes, c'est un historique et non le flux instantané "
                "(environ 10 000 stations). Vérifiez CARBURANTS_DATASET ou utilisez --force."
            )
            return 2

        stations: dict[int, dict] = {}
        ignorees = 0
        for code, where in partitions:
            lignes = await fetch_partition(client, where)
            for rec in lignes:
                s = normaliser(rec)
                if s is None:
                    ignorees += 1
                else:
                    stations[s["id"]] = s     # dédoublonnage par id
            print(f"  {code:>16} : {len(lignes)} lignes")
            await asyncio.sleep(PAUSE_ENTRE_REQUETES_S)

    print(f"Total : {len(stations)} stations retenues, {ignorees} ignorées (sans coordonnées valides).")
    dates = sorted(s["maj"] for s in stations.values() if s["maj"])
    plus_recente = dates[-1] if dates else None
    print(f"Mise à jour la plus récente côté source : {plus_recente}")
    if dates:
        mediane = dates[len(dates) // 2]
        vieux = sum(1 for d in dates if (debut - d).days > 30)
        print(f"Date médiane des prix : {mediane:%Y-%m-%d} ; {vieux} station(s) avec des prix de plus de 30 jours.")
    if plus_recente is None or (debut - plus_recente).days > SOURCE_FIGEE_JOURS:
        print(f"::warning::Source possiblement figée : aucune mise à jour depuis plus de {SOURCE_FIGEE_JOURS} jours "
              f"(dernière : {plus_recente}). Vérifiez le jeu de données interrogé.")
    if args.dry_run or not stations:
        print("Simulation : aucune écriture." if args.dry_run else "Rien à écrire.")
        return 0 if stations else 1

    await init_db_pool()
    try:
        lot = list(stations.values())
        for i in range(0, len(lot), 500):
            await executemany(UPSERT, [_params(s, debut) for s in lot[i:i + 500]])
        print(f"{len(lot)} stations enregistrées.")
        seuil = SEUIL_PURGE_MIN_FRANCE if args.france else SEUIL_PURGE_MIN_OCCITANIE
        if not demandes and len(lot) >= seuil:
            await execute(
                "DELETE FROM stations_carburant WHERE synchro_at < %s AND dep_code = ANY(%s)",
                (debut, list(perimetre)),
            )
            print("Stations disparues de la source (dans le périmètre) : supprimées.")
        elif not demandes:
            print(f"Purge ignorée : {len(lot)} stations seulement (minimum {seuil}).")
    finally:
        await close_db_pool()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))