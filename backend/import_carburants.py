"""Import quotidien des prix des carburants vers la table stations_carburant.

Source : data.economie.gouv.fr, jeu « Prix des carburants en France - Flux
instantané - v2 » (API Opendatasoft Explore v2.1, Licence Ouverte 2.0).
Une ligne = un point de vente, avec tous ses carburants et prix en colonnes.

CORRECTIF (erreur HTTP 400 du run #1)
-------------------------------------
L'ancienne version interrogeait des champs qui n'existent pas dans ce jeu
(geo_point, dep_code, price_gazole, update, name, brand, com_arm_*, epci_*…).
Le jeu v2 utilise notamment : geom, code_departement / departement,
<carburant>_prix, <carburant>_maj, carburants_disponibles, ville, adresse…
Opendatasoft répond 400 dès qu'un champ inconnu apparaît dans « where ».

Cette version :
- détecte les noms de champs à partir d'un enregistrement réel (sonde) et
  accepte les deux vocabulaires (v2 « colonnes par carburant » et l'ancien) ;
- n'utilise plus qu'UN seul champ dans « where » (le département) ;
- filtre les coordonnées côté Python (plus de « geo_point IS NOT NULL ») ;
- ne réessaie plus un 400 (erreur déterministe) et AFFICHE le message de l'API,
  qui nomme le champ fautif ;
- refuse d'écrire en base si aucun prix n'a pu être lu (évite de purger ou
  de remplir la table avec des lignes vides) ;
- déduit la date de mise à jour de la station du max des <carburant>_maj.

Pagination : /records est limité à 100 lignes par requête et à
offset + limit <= 10 000 ; on découpe donc par département.

Usage :
    python import_carburants.py --dry-run
    python import_carburants.py --dry-run --diagnostic   # liste les champs reçus
    python import_carburants.py
    python import_carburants.py --departements 11,30,31,34
    python import_carburants.py --occitanie-seule
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
SEUIL_PURGE_MIN_OCCITANIE = 300
SEUIL_PURGE_MIN_FRANCE = 5_000
SOURCE_FIGEE_JOURS = 7

DEPARTEMENTS_OCCITANIE = ["09", "11", "12", "30", "31", "32", "34", "46", "48", "65", "66", "81", "82"]
# Ardèche, Bouches-du-Rhône, Cantal, Corrèze, Dordogne, Landes, Haute-Loire,
# Lot-et-Garonne, Pyrénées-Atlantiques, Vaucluse.
DEPARTEMENTS_LIMITROPHES = ["07", "13", "15", "19", "24", "40", "43", "47", "64", "84"]
DEPARTEMENTS_PAR_DEFAUT = DEPARTEMENTS_OCCITANIE + DEPARTEMENTS_LIMITROPHES
DEPARTEMENTS_FRANCE = (
    [f"{i:02d}" for i in range(1, 96) if i != 20]
    + ["2A", "2B", "971", "972", "973", "974", "976"]
)

CARBURANTS_CLES = ("gazole", "sp95", "sp98", "e10", "e85", "gplc")
PRIX_MIN, PRIX_MAX = 0.3, 6.0   # bornes de vraisemblance en EUR/L

# Noms de champ possibles, dans l'ordre de préférence (v2 d'abord).
CHAMPS_DEPARTEMENT = ("code_departement", "dep_code")
CHAMPS_GEO = ("geom", "geo_point")


# ─────────────────────────── utilitaires ───────────────────────────

def _premier(rec: dict, *cles: str) -> Any:
    """Première valeur non vide parmi plusieurs noms de champ possibles."""
    for cle in cles:
        v = rec.get(cle)
        if v not in (None, "", [], {}):
            return v
    return None


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
        # Certains exports renvoient « Gazole;SP95 » ou « Gazole,SP95 ».
        for sep in (";", ","):
            if sep in valeur:
                return [x.strip() for x in valeur.split(sep) if x.strip()]
        return [valeur.strip()] if valeur.strip() else []
    return [str(x).strip() for x in valeur if str(x).strip()]


def _date(valeur: Any) -> datetime | None:
    if not valeur:
        return None
    try:
        d = datetime.fromisoformat(str(valeur).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _code_dep(valeur: Any) -> str | None:
    """'9' -> '09', ' 2a ' -> '2A'. Garantit le même format que les listes de départements."""
    if valeur in (None, ""):
        return None
    s = str(valeur).strip().upper()
    return s.zfill(2) if s.isdigit() and len(s) < 2 else s


def _bool_oui_non(valeur: Any) -> bool | None:
    if isinstance(valeur, bool):
        return valeur
    return {"oui": True, "non": False, "1": True, "0": False, "true": True, "false": False}.get(
        str(valeur if valeur is not None else "").strip().lower()
    )


def _dates_par_carburant(rec: dict) -> dict[str, str]:
    """Dates de mise à jour par carburant : champs « <carburant>_maj » (v2).

    On n'accepte QUE les noms finissant par « _maj » : l'ancienne détection
    par sous-chaîne aurait pris « gazole_rupture_debut » pour une date de prix.
    """
    trouvees: dict[str, str] = {}
    for carburant in CARBURANTS_CLES:
        d = _date(rec.get(f"{carburant}_maj"))
        if d:
            trouvees[carburant] = d.isoformat()
    return trouvees


def _horaires(valeur: Any) -> str | None:
    """Le champ horaires arrive sous forme de chaîne JSON (ou déjà de dict)."""
    if not valeur:
        return None
    if isinstance(valeur, str):
        try:
            valeur = json.loads(valeur)
        except ValueError:
            return None
    return json.dumps(valeur, ensure_ascii=False) if isinstance(valeur, dict) else None


def _coordonnees(rec: dict) -> tuple[float, float] | None:
    """(lat, lon) depuis geom / geo_point ; à défaut depuis latitude / longitude."""
    for champ in CHAMPS_GEO:
        geo = rec.get(champ)
        if isinstance(geo, dict):
            try:
                if "lat" in geo and "lon" in geo:
                    return float(geo["lat"]), float(geo["lon"])
                coords = geo.get("coordinates") or (geo.get("geometry") or {}).get("coordinates")
                if coords:                       # GeoJSON : [lon, lat]
                    return float(coords[1]), float(coords[0])
            except (TypeError, ValueError, IndexError):
                pass
    try:
        lat, lon = float(rec["latitude"]), float(rec["longitude"])
    except (KeyError, TypeError, ValueError):
        return None
    # Ancien format : degrés × 100 000 (ex. 4620114 -> 46.20114).
    if abs(lat) > 90 or abs(lon) > 180:
        lat, lon = lat / 100_000, lon / 100_000
    return lat, lon


def normaliser(rec: dict) -> dict | None:
    coord = _coordonnees(rec)
    try:
        ident = int(rec["id"])
    except (KeyError, TypeError, ValueError):
        return None
    if coord is None:
        return None
    lat, lon = coord
    if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
        return None

    par_carburant = _dates_par_carburant(rec)
    maj_station = _date(rec.get("update")) or (
        max((_date(v) for v in par_carburant.values()), default=None)
    )

    prix = {c: _prix(_premier(rec, f"{c}_prix", f"price_{c}")) for c in CARBURANTS_CLES}
    rupture = _liste(_premier(rec, "carburants_rupture_temporaire", "shortage")) + _liste(
        rec.get("carburants_rupture_definitive")
    )
    if not rupture:
        rupture = _liste(rec.get("carburants_indisponibles"))

    return {
        "id": ident, "latitude": lat, "longitude": lon,
        "nom": _premier(rec, "name", "nom"), "marque": _premier(rec, "brand", "marque"),
        "adresse": _premier(rec, "adresse", "address"),
        "code_postal": _premier(rec, "cp"),
        "commune": _premier(rec, "ville", "com_arm_name"),
        "code_insee": _premier(rec, "com_arm_code"),
        "epci_code": _premier(rec, "epci_code"), "epci_nom": _premier(rec, "epci_name"),
        "region_code": _premier(rec, "code_region", "reg_code"),
        "dep_code": _code_dep(_premier(rec, *CHAMPS_DEPARTEMENT)),
        "dep_nom": _premier(rec, "departement", "dep_name"),
        "region": _premier(rec, "region", "reg_name"),
        "type_route": (str(rec.get("pop") or "")[:1].upper() or None),
        "automate": _bool_oui_non(_premier(rec, "horaires_automate_24_24", "automate_24_24")),
        "horaires": _horaires(_premier(rec, "horaires", "timetable")),
        "dispo": _liste(_premier(rec, "carburants_disponibles", "fuel")),
        "rupture": list(dict.fromkeys(rupture)),
        "gazole": prix["gazole"], "sp95": prix["sp95"], "sp98": prix["sp98"],
        "e10": prix["e10"], "e85": prix["e85"], "gplc": prix["gplc"],
        "maj": maj_station,
        "services": _liste(_premier(rec, "services_service", "services")),
        "maj_carburants": json.dumps(par_carburant) if par_carburant else None,
    }


# ─────────────────────────── accès API ───────────────────────────

class ErreurRequete(RuntimeError):
    """Erreur 4xx non répétable : le message de l'API est inclus."""


async def _get(client: httpx.AsyncClient, params: dict) -> dict:
    derniere = None
    for tentative in range(5):
        try:
            r = await client.get(API_URL, params=params)
            if 400 <= r.status_code < 500 and r.status_code != 429:
                # Erreur de requête : inutile de réessayer, et le corps de la
                # réponse dit précisément ce qui est refusé (champ inconnu…).
                raise ErreurRequete(
                    f"HTTP {r.status_code} sur {r.request.url}\n  Réponse de l'API : {r.text[:600]}"
                )
            if r.status_code in (429, 500, 502, 503, 504):
                raise httpx.HTTPStatusError(f"HTTP {r.status_code}", request=r.request, response=r)
            r.raise_for_status()
            return r.json()
        except ErreurRequete:
            raise
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


async def fetch_departement(client: httpx.AsyncClient, champ: str, code: str) -> list[dict]:
    """Essaie « 09 » puis « 9 » : selon le jeu, le code n'est pas toujours zéro-complété."""
    variantes = [code] + ([code.lstrip("0")] if code.startswith("0") and code.lstrip("0") else [])
    for valeur in variantes:
        lignes = await fetch_partition(client, f'{champ}="{valeur}"')
        if lignes:
            return lignes
    return []


# (colonne SQL, clé du dict normalisé, conversion SQL éventuelle). Une seule liste
# alimente l'INSERT, le UPDATE et les paramètres : impossible de les désaligner.
CHAMPS = [
    ("id", "id", ""), ("latitude", "latitude", ""), ("longitude", "longitude", ""),
    ("nom", "nom", ""), ("marque", "marque", ""), ("adresse", "adresse", ""),
    ("code_postal", "code_postal", ""), ("commune", "commune", ""), ("code_insee", "code_insee", ""),
    ("dep_code", "dep_code", ""), ("dep_nom", "dep_nom", ""),
    ("region", "region", ""), ("region_code", "region_code", ""),
    ("epci_code", "epci_code", ""), ("epci_nom", "epci_nom", ""),
    ("type_route", "type_route", ""), ("automate_24_24", "automate", ""),
    ("horaires", "horaires", "::jsonb"),
    ("carburants_dispo", "dispo", ""), ("carburants_rupture", "rupture", ""),
    ("prix_gazole", "gazole", ""), ("prix_sp95", "sp95", ""), ("prix_sp98", "sp98", ""),
    ("prix_e10", "e10", ""), ("prix_e85", "e85", ""), ("prix_gplc", "gplc", ""),
    ("maj_prix", "maj", ""), ("maj_carburants", "maj_carburants", "::jsonb"),
    ("services", "services", ""),
]
_COLONNES_SQL = ", ".join(c for c, _, _ in CHAMPS) + ", synchro_at"
_VALEURS_SQL = ", ".join("%s" + cast for _, _, cast in CHAMPS) + ", %s"
_MAJ_SQL = ",\n    ".join(f"{c} = EXCLUDED.{c}" for c, _, _ in CHAMPS if c != "id") + ",\n    synchro_at = EXCLUDED.synchro_at"
UPSERT = (
    f"INSERT INTO stations_carburant ({_COLONNES_SQL})\nVALUES ({_VALEURS_SQL})\n"
    f"ON CONFLICT (id) DO UPDATE SET\n    {_MAJ_SQL}"
)


def _params(s: dict, synchro: datetime) -> tuple:
    return tuple(s[cle] for _, cle, _ in CHAMPS) + (synchro,)


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="n'écrit rien en base")
    parser.add_argument("--diagnostic", action="store_true", help="affiche les champs d'un enregistrement réel puis s'arrête")
    parser.add_argument("--departements", default="", help="liste séparée par des virgules (ex. 11,34)")
    parser.add_argument("--france", action="store_true", help="toute la France au lieu de l'Occitanie et de ses limitrophes")
    parser.add_argument("--occitanie-seule", action="store_true", help="les 13 départements d'Occitanie, sans les limitrophes")
    parser.add_argument("--force", action="store_true", help="ignore le garde-fou « jeu trop volumineux »")
    args = parser.parse_args()
    load_dotenv()

    debut = datetime.now(timezone.utc)
    demandes = [_code_dep(d) for d in args.departements.split(",") if d.strip()]
    perimetre = (
        DEPARTEMENTS_FRANCE if args.france
        else DEPARTEMENTS_OCCITANIE if args.occitanie_seule
        else DEPARTEMENTS_PAR_DEFAUT
    )
    cibles = demandes or perimetre
    if demandes:
        libelle_perimetre = "départements " + ",".join(demandes)
    elif args.france:
        libelle_perimetre = "France entière"
    elif args.occitanie_seule:
        libelle_perimetre = "Occitanie"
    else:
        libelle_perimetre = "Occitanie + départements limitrophes"
    print(f"Périmètre : {libelle_perimetre}")

    async with httpx.AsyncClient(timeout=60, headers={"User-Agent": "Pelify/1.0 (import carburants)"}) as client:
        # Sonde : UN enregistrement réel, sans « select » ni « where » → aucun risque de 400
        # lié à un nom de champ, et on apprend le vrai vocabulaire du jeu.
        sonde = await _get(client, {"limit": 1})
        total_jeu = int(sonde.get("total_count") or 0)
        print(f"Jeu {DATASET} : {total_jeu} lignes")
        exemples = sonde.get("results") or []
        if not exemples:
            print("Arrêt : le jeu ne renvoie aucun enregistrement.")
            return 1
        champs_recus = sorted(exemples[0].keys())
        print(f"Champs reçus ({len(champs_recus)}) : {', '.join(champs_recus)}")
        if args.diagnostic:
            print(json.dumps(exemples[0], ensure_ascii=False, indent=2, default=str)[:4000])
            return 0

        champ_dep = next((c for c in CHAMPS_DEPARTEMENT if c in champs_recus), None)
        if champ_dep is None:
            print(
                "Arrêt : aucun champ département reconnu "
                f"(attendu : {', '.join(CHAMPS_DEPARTEMENT)}). Relancez avec --diagnostic "
                "et ajoutez le bon nom dans CHAMPS_DEPARTEMENT."
            )
            return 3
        print(f"Champ département utilisé : {champ_dep}")

        if total_jeu > SEUIL_HISTORIQUE and not args.force:
            print(
                f"Arrêt : {total_jeu} lignes, c'est un historique et non le flux instantané "
                "(environ 10 000 stations). Vérifiez CARBURANTS_DATASET ou utilisez --force."
            )
            return 2

        stations: dict[int, dict] = {}
        ignorees = 0
        for code in cibles:
            lignes = await fetch_departement(client, champ_dep, code)
            for rec in lignes:
                s = normaliser(rec)
                if s is None:
                    ignorees += 1
                else:
                    stations[s["id"]] = s     # dédoublonnage par id
            print(f"  {code:>16} : {len(lignes)} lignes")
            await asyncio.sleep(PAUSE_ENTRE_REQUETES_S)

    print(f"Total : {len(stations)} stations retenues, {ignorees} ignorées (sans coordonnées valides).")

    avec_prix = sum(1 for s in stations.values() if any(s[c] is not None for c in CARBURANTS_CLES))
    print(f"Stations avec au moins un prix lisible : {avec_prix}")
    if stations and avec_prix == 0:
        print("::error::Aucun prix lisible : les champs de prix ne sont pas reconnus "
              "(attendu : <carburant>_prix). Relancez avec --diagnostic. Aucune écriture.")
        return 4

    dates = sorted(s["maj"] for s in stations.values() if s["maj"])
    plus_recente = dates[-1] if dates else None
    print(f"Mise à jour la plus récente côté source : {plus_recente}")
    if dates:
        mediane = dates[len(dates) // 2]
        vieux = sum(1 for d in dates if (debut - d).days > 30)
        print(f"Date médiane des prix : {mediane:%Y-%m-%d} ; {vieux} station(s) avec des prix de plus de 30 jours.")
    if plus_recente is None or (debut - plus_recente).days > SOURCE_FIGEE_JOURS:
        print(f"::warning::Source possiblement figée ou dates non lues : aucune mise à jour depuis plus de "
              f"{SOURCE_FIGEE_JOURS} jours (dernière : {plus_recente}). Vérifiez les champs <carburant>_maj.")
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