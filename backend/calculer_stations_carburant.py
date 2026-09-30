"""
backend/calculer_stations_carburant.py - Pour chaque lieu de tournage, retient les
stations-service les plus proches et les écrit dans amenity_cache
(categorie = 'station_service'), exactement comme le font
calculer_amenities_datatourisme.py et refresh_cache.py pour les autres commodités.

Sélection, pour chaque lieu :
  1. les 10 stations les plus proches, dans un rayon de 30 km ;
  2. en complément, pour chaque carburant que AUCUNE de ces 10 stations ne vend
     (typiquement E85 ou GPLc), la station la plus proche qui le vend, jusqu'à
     60 km. Un conducteur au GPLc préfère savoir qu'il y en a une à 45 km plutôt
     que de croire qu'il n'y en a aucune.

Ce script ne stocke que le précalcul (rang, distance à vol d'oiseau, lien vers la
station). Prix, horaires, services, ruptures... restent dans stations_carburant
(mise à jour chaque jour par import_carburants.py) et sont joints à la lecture par
/api/lieux/{id}/amenities : le visiteur voit donc toujours les derniers prix, sans
avoir à relancer ce calcul.

Les distances à pied et en voiture (Géoplateforme IGN) sont ensuite calculées par
enrich_itineraires.py, qui traite automatiquement toute ligne d'amenity_cache dont
les distances sont vides : aucune modification n'est nécessaire de son côté.
Si une station change de coordonnées, ses anciennes distances sont remises à NULL
ici pour être recalculées.

100 % local (SQL + Python), aucun appel réseau.

Sécurité : si la table stations_carburant contient moins de 300 stations (import
raté ou incomplet), le script s'arrête sans rien modifier, pour ne pas vider les
stations déjà précalculées. Option --force pour passer outre.

Usage :
    python calculer_stations_carburant.py
    python calculer_stations_carburant.py --lieu-id 42
"""
from __future__ import annotations

import argparse
import asyncio
import math
import sys
from decimal import Decimal

from db import close_db_pool, execute, executemany, fetch_all, fetch_one, init_db_pool

CATEGORIE = "station_service"
RAYON_PROCHES_M = 30_000
NB_PROCHES = 10
RAYON_COMPLEMENT_M = 60_000
STATIONS_MIN = 300

# (code utilisé par la source, colonne de prix)
CARBURANTS = (
    ("Gazole", "prix_gazole"),
    ("E10", "prix_e10"),
    ("SP95", "prix_sp95"),
    ("SP98", "prix_sp98"),
    ("E85", "prix_e85"),
    ("GPLc", "prix_gplc"),
)


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> int:
    r = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlam / 2) ** 2
    return int(round(2 * r * math.asin(min(1.0, math.sqrt(a)))))


def _carburants_vendus(station: dict) -> set[str]:
    """Carburants réellement achetables : un prix connu et pas de rupture signalée."""
    ruptures = set(station.get("carburants_rupture") or [])
    return {code for code, col in CARBURANTS if station.get(col) is not None and code not in ruptures}


def _nom_station(s: dict) -> str:
    nom = (s.get("nom") or s.get("marque") or "Station-service").strip()
    return (nom.title() if nom.isupper() else nom)[:255]


def _adresse(s: dict) -> str | None:
    fin = " ".join(x for x in (s.get("code_postal"), (s.get("commune") or "").title()) if x)
    texte = ", ".join(x for x in (s.get("adresse"), fin) if x)
    return texte[:500] or None


def choisir(lat: float, lon: float, stations: list[dict]) -> list[tuple[int, dict]]:
    """Renvoie [(distance_m, station)] triées par distance croissante."""
    dlat = RAYON_COMPLEMENT_M / 111_320.0
    dlon = RAYON_COMPLEMENT_M / (111_320.0 * max(0.2, math.cos(math.radians(lat))))
    avec_distance = []
    for s in stations:
        slat, slon = float(s["latitude"]), float(s["longitude"])
        if abs(slat - lat) > dlat or abs(slon - lon) > dlon:
            continue
        d = _haversine_m(lat, lon, slat, slon)
        if d <= RAYON_COMPLEMENT_M:
            avec_distance.append((d, s))
    avec_distance.sort(key=lambda t: t[0])

    retenues: dict[int, tuple[int, dict]] = {}
    for d, s in avec_distance:
        if d > RAYON_PROCHES_M or len(retenues) >= NB_PROCHES:
            break
        retenues[s["id"]] = (d, s)

    couverts: set[str] = set()
    for _, s in retenues.values():
        couverts |= _carburants_vendus(s)
    for code, _ in CARBURANTS:
        if code in couverts:
            continue
        for d, s in avec_distance:
            if s["id"] in retenues:
                continue
            if code in _carburants_vendus(s):
                retenues[s["id"]] = (d, s)
                couverts |= _carburants_vendus(s)
                break

    return sorted(retenues.values(), key=lambda t: t[0])


def _decimal7(valeur: float) -> Decimal:
    # Les colonnes sont en DECIMAL(10,7) : on envoie exactement 7 décimales.
    return Decimal(f"{float(valeur):.7f}")


UPSERT = """
INSERT INTO amenity_cache
    (lieu_tournage_id, categorie, nom, latitude, longitude, distance_metres,
     adresse, station_id, rang)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (lieu_tournage_id, station_id) WHERE station_id IS NOT NULL
DO UPDATE SET
    -- Coordonnées modifiées : les anciennes distances IGN ne valent plus rien,
    -- on les remet à NULL pour que enrich_itineraires.py les recalcule.
    distance_pied_metres = CASE
        WHEN (amenity_cache.latitude, amenity_cache.longitude) IS DISTINCT FROM (EXCLUDED.latitude, EXCLUDED.longitude)
        THEN NULL ELSE amenity_cache.distance_pied_metres END,
    duree_pied_secondes = CASE
        WHEN (amenity_cache.latitude, amenity_cache.longitude) IS DISTINCT FROM (EXCLUDED.latitude, EXCLUDED.longitude)
        THEN NULL ELSE amenity_cache.duree_pied_secondes END,
    distance_voiture_metres = CASE
        WHEN (amenity_cache.latitude, amenity_cache.longitude) IS DISTINCT FROM (EXCLUDED.latitude, EXCLUDED.longitude)
        THEN NULL ELSE amenity_cache.distance_voiture_metres END,
    duree_voiture_secondes = CASE
        WHEN (amenity_cache.latitude, amenity_cache.longitude) IS DISTINCT FROM (EXCLUDED.latitude, EXCLUDED.longitude)
        THEN NULL ELSE amenity_cache.duree_voiture_secondes END,
    nom = EXCLUDED.nom,
    latitude = EXCLUDED.latitude,
    longitude = EXCLUDED.longitude,
    distance_metres = EXCLUDED.distance_metres,
    adresse = EXCLUDED.adresse,
    rang = EXCLUDED.rang,
    date_maj = CURRENT_TIMESTAMP
"""


async def main(lieu_id: int | None, force: bool) -> int:
    await init_db_pool()
    try:
        n = await fetch_one("SELECT count(*) AS n FROM stations_carburant")
        nb_stations = int(n["n"]) if n else 0
        if nb_stations < STATIONS_MIN and not force:
            print(
                f"Arrêt : seulement {nb_stations} station(s) en base (minimum {STATIONS_MIN}). "
                "Lancez d'abord import_carburants.py, ou utilisez --force.",
                flush=True,
            )
            return 1

        stations = await fetch_all(
            """
            SELECT id, nom, marque, adresse, code_postal, commune, latitude, longitude,
                   prix_gazole, prix_e10, prix_sp95, prix_sp98, prix_e85, prix_gplc,
                   carburants_rupture
            FROM stations_carburant
            """
        )

        if lieu_id:
            lieux = await fetch_all(
                "SELECT id, latitude, longitude FROM lieux_tournage WHERE id = %s", (lieu_id,)
            )
        else:
            lieux = await fetch_all("SELECT id, latitude, longitude FROM lieux_tournage")

        print(f"{len(lieux)} lieu(x) à traiter, {len(stations)} stations en base.", flush=True)

        sans_station = 0
        total_lignes = 0
        for i, lieu in enumerate(lieux, start=1):
            choisies = choisir(float(lieu["latitude"]), float(lieu["longitude"]), stations)

            lignes, vus = [], set()
            for d, s in choisies:
                nom = _nom_station(s)
                cle = (nom, f"{float(s['latitude']):.7f}", f"{float(s['longitude']):.7f}")
                if cle in vus:          # deux fiches identiques au même endroit : on n'en garde qu'une
                    continue
                vus.add(cle)
                lignes.append((
                    lieu["id"], CATEGORIE, nom,
                    _decimal7(s["latitude"]), _decimal7(s["longitude"]),
                    d, _adresse(s), s["id"], len(lignes) + 1,
                ))
            if not lignes:
                sans_station += 1

            # On supprime uniquement les stations qui ne font plus partie de la sélection :
            # jamais de DELETE global, pour conserver les distances IGN déjà calculées.
            ids_conserves = [l[7] for l in lignes]
            await execute(
                "DELETE FROM amenity_cache WHERE lieu_tournage_id = %s AND categorie = %s "
                "AND (station_id IS NULL OR NOT (station_id = ANY(%s)))",
                (lieu["id"], CATEGORIE, ids_conserves),
            )
            await executemany(UPSERT, lignes)
            total_lignes += len(lignes)

            if i % 50 == 0:
                print(f"  ... {i}/{len(lieux)} lieux traités", flush=True)

        print(
            f"\nTerminé : {len(lieux)} lieu(x), {total_lignes} station(s) associées, "
            f"{sans_station} lieu(x) sans aucune station à moins de {RAYON_PROCHES_M // 1000} km.",
            flush=True,
        )
        return 0
    finally:
        await close_db_pool()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--lieu-id", type=int, default=None)
    parser.add_argument("--force", action="store_true", help="ignore le garde-fou « pas assez de stations en base »")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.lieu_id, args.force)))
