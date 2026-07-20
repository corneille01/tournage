"""
backend/overpass.py — Recherche des lieux à proximité via Overpass API
(OpenStreetMap), gratuit et sans clé API.

Ce module ne s'exécute JAMAIS sur une requête visiteur. Il alimente
`amenity_cache` en tâche de fond (cron / script manuel), pour que le
site serve uniquement des données déjà en base — c'est ce qui permet
de tenir la charge sans dépendre de la disponibilité/rate-limit
d'Overpass au moment où 1M personnes cliquent en même temps.
"""

import math
import httpx

OVERPASS_URL = "https://overpass-api.de/api/interpreter"

# Tags OSM correspondant à chaque catégorie qu'on veut afficher.
# Référence : https://wiki.openstreetmap.org/wiki/Map_features
_CATEGORY_TAGS = {
    "hebergement":      ['tourism=hotel', 'tourism=guest_house', 'tourism=hostel', 'tourism=chalet'],
    "restaurant":       ['amenity=restaurant', 'amenity=cafe'],
    "office_tourisme":  ['tourism=information'],
    "police":           ['amenity=police'],
    "hopital":          ['amenity=hospital'],
    "gare":             ['railway=station'],
    "aeroport":         ['aeroway=aerodrome'],
    "arret_bus":        ['highway=bus_stop'],
    "parking":          ['amenity=parking'],
}

RAYON_RECHERCHE_M = {
    "hebergement": 15000, "restaurant": 8000, "office_tourisme": 20000,
    "police": 20000, "hopital": 25000, "gare": 30000,
    "aeroport": 60000, "arret_bus": 2000, "parking": 5000,
}


def haversine_metres(lat1: float, lon1: float, lat2: float, lon2: float) -> int:
    """Distance à vol d'oiseau entre deux points GPS, en mètres."""
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    return round(2 * R * math.asin(math.sqrt(a)))


def _build_query(lat: float, lon: float, categorie: str) -> str:
    rayon = RAYON_RECHERCHE_M[categorie]
    clauses = []
    for tag in _CATEGORY_TAGS[categorie]:
        k, v = tag.split("=")
        clauses.append(f'node["{k}"="{v}"](around:{rayon},{lat},{lon});')
        clauses.append(f'way["{k}"="{v}"](around:{rayon},{lat},{lon});')
    body = "\n  ".join(clauses)
    return f"""
[out:json][timeout:25];
(
  {body}
);
out center 30;
"""


async def find_nearby(lat: float, lon: float, categorie: str, top_n: int = 10) -> dict:
    """
    Interroge Overpass pour une catégorie donnée autour d'un point.
    Retourne à la fois le top_n le plus proche (pour l'affichage) ET
    des statistiques calculées sur TOUS les résultats trouvés (pour ne
    pas perdre l'info "42 restaurants dans le coin" quand on n'en
    affiche que 10 — deux lieux avec 10 restaurants affichés peuvent
    avoir des niveaux d'équipement réel très différents).
    """
    if categorie not in _CATEGORY_TAGS:
        raise ValueError(f"Catégorie inconnue: {categorie}")

    rayon = RAYON_RECHERCHE_M[categorie]
    query = _build_query(lat, lon, categorie)

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(OVERPASS_URL, data={"data": query})
        resp.raise_for_status()
        data = resp.json()

    tous: list[dict] = []
    for el in data.get("elements", []):
        # Les "way" ont leurs coordonnées dans "center", les "node" directement
        el_lat = el.get("lat") or el.get("center", {}).get("lat")
        el_lon = el.get("lon") or el.get("center", {}).get("lon")
        if el_lat is None or el_lon is None:
            continue

        tags = el.get("tags", {})
        nom = tags.get("name")
        if not nom:
            continue  # on ignore les éléments sans nom, inutilisables pour l'utilisateur

        distance = haversine_metres(lat, lon, el_lat, el_lon)
        tous.append({
            "osm_id":    el.get("id"),
            "nom":       nom,
            "latitude":  el_lat,
            "longitude": el_lon,
            "distance_metres": distance,
            "adresse":   _format_adresse(tags),
            "telephone": tags.get("phone") or tags.get("contact:phone"),
            "site_web":  tags.get("website") or tags.get("contact:website"),
        })

    tous.sort(key=lambda r: r["distance_metres"])
    top = tous[:top_n]
    top10_distances = [r["distance_metres"] for r in tous[:10]]

    stats = {
        "rayon_metres":   rayon,
        "nombre_total":   len(tous),
        "nombre_500m":    sum(1 for r in tous if r["distance_metres"] <= 500),
        "nombre_1000m":   sum(1 for r in tous if r["distance_metres"] <= 1000),
        "distance_min_m": tous[0]["distance_metres"] if tous else None,
        "distance_moy_top10_m": (
            round(sum(top10_distances) / len(top10_distances)) if top10_distances else None
        ),
    }

    return {"top": top, "stats": stats}


def _format_adresse(tags: dict) -> str | None:
    parts = [
        tags.get("addr:housenumber", ""),
        tags.get("addr:street", ""),
        tags.get("addr:postcode", ""),
        tags.get("addr:city", ""),
    ]
    adresse = " ".join(p for p in parts if p).strip()
    return adresse or None


def phrase_recommandation(categorie_label: str, nom: str, distance_metres: int) -> str:
    """
    Génère la phrase demandée : "L'hôtel X est à 2km du lieu de tournage
    et est l'hôtel le plus proche."
    """
    if distance_metres < 1000:
        distance_txt = f"{distance_metres} m"
    else:
        distance_txt = f"{distance_metres / 1000:.1f} km".replace(".0 km", " km")
    return (
        f"{categorie_label} {nom} est à {distance_txt} du lieu de tournage "
        f"et est {categorie_label.lower()} le plus proche."
    )