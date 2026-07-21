"""
backend/overpass.py — Recherche des lieux à proximité via Overpass API
(OpenStreetMap), gratuit et sans clé API.

Ce module ne s'exécute JAMAIS sur une requête visiteur. Il alimente
`amenity_cache` en tâche de fond (cron / script manuel), pour que le
site serve uniquement des données déjà en base.

Cela permet de tenir la charge sans dépendre de la disponibilité ou
des limites d'Overpass au moment où les visiteurs utilisent le site.
"""

import asyncio
import math

import httpx


OVERPASS_URL = "https://overpass-api.de/api/interpreter"

# Identification de l'application auprès du serveur Overpass.
OVERPASS_HEADERS = {
    "User-Agent": "Pelify-Tournage/1.0 (https://pelify.app)"
}


# Tags OpenStreetMap correspondant à chaque catégorie affichée.
_CATEGORY_TAGS = {
    "hebergement": [
        # Hébergements classiques
        "tourism=hotel",
        "tourism=motel",
        "tourism=hostel",

        # Chambres d'hôtes, gîtes et locations de vacances
        "tourism=guest_house",
        "tourism=chalet",
        "tourism=apartment",

        # Campings et caravanes
        "tourism=camp_site",
        "tourism=caravan_site",
    ],

    # Refuges et étapes de randonnée
    "refuge": [
        "tourism=alpine_hut",
        "tourism=wilderness_hut",
    ],

    "restaurant": [
        "amenity=restaurant",
        "amenity=cafe",
        "amenity=fast_food",
    ],

    "office_tourisme": [
        "tourism=information",
    ],

    "police": [
        "amenity=police",
    ],

    "hopital": [
        "amenity=hospital",
        "amenity=clinic",
    ],

    "gare": [
        "railway=station",
    ],

    "aeroport": [
        "aeroway=aerodrome",
    ],

    "arret_bus": [
        "highway=bus_stop",
        "public_transport=platform",
    ],

    "parking": [
        "amenity=parking",
    ],

    # Que faire aux alentours
    "activite": [
        # Lieux touristiques
        "tourism=attraction",
        "tourism=museum",
        "tourism=viewpoint",
        "tourism=artwork",
        "tourism=gallery",

        # Patrimoine
        "historic=castle",
        "historic=monument",
        "historic=memorial",
        "historic=ruins",
        "historic=archaeological_site",

        # Nature et loisirs
        "leisure=park",
        "leisure=nature_reserve",
        "leisure=garden",
        "leisure=water_park",
        "leisure=swimming_pool",

        # Randonnée et découverte
        "natural=peak",
        "natural=cave_entrance",
        "natural=waterfall",
        "man_made=observation_tower",
    ],
}


# Rayon maximal utilisé pour chaque catégorie.
RAYON_RECHERCHE_M = {
    "hebergement": 15_000,
    "refuge": 25_000,
    "restaurant": 8_000,
    "office_tourisme": 20_000,
    "police": 20_000,
    "hopital": 25_000,
    "gare": 30_000,
    "aeroport": 60_000,
    "arret_bus": 2_000,
    "parking": 5_000,
    "activite": 15_000,
}


# Icône et couleur par catégorie.
# Elles sont utilisées par le frontend pour les marqueurs et les popups.
ICONES_CATEGORIE = {
    "hebergement": {
        "emoji": "🏨",
        "couleur": "#2a9d8f",
    },

    "refuge": {
        "emoji": "🥾",
        "couleur": "#588157",
    },

    "restaurant": {
        "emoji": "🍽️",
        "couleur": "#e76f51",
    },

    "office_tourisme": {
        "emoji": "ℹ️",
        "couleur": "#264653",
    },

    "police": {
        "emoji": "🚓",
        "couleur": "#023e8a",
    },

    "hopital": {
        "emoji": "🏥",
        "couleur": "#d00000",
    },

    "gare": {
        "emoji": "🚉",
        "couleur": "#6a4c93",
    },

    "aeroport": {
        "emoji": "✈️",
        "couleur": "#4361ee",
    },

    "arret_bus": {
        "emoji": "🚌",
        "couleur": "#f4a261",
    },

    "parking": {
        "emoji": "🅿️",
        "couleur": "#3a3a3a",
    },

    "activite": {
        "emoji": "🎡",
        "couleur": "#9b5de5",
    },
}


def haversine_metres(
    lat1: float,
    lon1: float,
    lat2: float,
    lon2: float,
) -> int:
    """
    Calcule la distance à vol d'oiseau entre deux points GPS.

    La distance retournée est exprimée en mètres.
    """

    rayon_terre = 6_371_000

    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)

    difference_latitude = math.radians(lat2 - lat1)
    difference_longitude = math.radians(lon2 - lon1)

    a = (
        math.sin(difference_latitude / 2) ** 2
        + math.cos(phi1)
        * math.cos(phi2)
        * math.sin(difference_longitude / 2) ** 2
    )

    distance = 2 * rayon_terre * math.asin(math.sqrt(a))

    return round(distance)


def _build_query(
    lat: float,
    lon: float,
    categorie: str,
) -> str:
    """
    Construit la requête Overpass QL pour une catégorie.

    La recherche concerne :
    - les nodes ;
    - les ways ;
    - les relations.
    """

    if categorie not in _CATEGORY_TAGS:
        raise ValueError(f"Catégorie inconnue : {categorie}")

    if categorie not in RAYON_RECHERCHE_M:
        raise ValueError(
            f"Aucun rayon configuré pour la catégorie : {categorie}"
        )

    rayon = RAYON_RECHERCHE_M[categorie]
    clauses = []

    for tag in _CATEGORY_TAGS[categorie]:
        cle, valeur = tag.split("=", maxsplit=1)

        clauses.append(
            f'node["{cle}"="{valeur}"]'
            f"(around:{rayon},{lat},{lon});"
        )

        clauses.append(
            f'way["{cle}"="{valeur}"]'
            f"(around:{rayon},{lat},{lon});"
        )

        clauses.append(
            f'relation["{cle}"="{valeur}"]'
            f"(around:{rayon},{lat},{lon});"
        )

    corps_requete = "\n  ".join(clauses)

    return f"""
[out:json][timeout:60];
(
  {corps_requete}
);
out center;
"""


async def _appeler_overpass_avec_retry(
    query: str,
    timeout: httpx.Timeout,
    tentatives_max: int = 4,
) -> dict:
    """
    Le serveur public overpass-api.de limite le débit (429) et time out
    parfois sous charge (503/504) — normal pour un service public
    gratuit et partagé. On réessaie avec un délai croissant plutôt que
    d'abandonner immédiatement, ce qui évite de perdre des lieux entiers
    juste parce qu'Overpass était temporairement occupé.
    """
    derniere_erreur = None
    for tentative in range(1, tentatives_max + 1):
        try:
            async with httpx.AsyncClient(
                timeout=timeout,
                headers=OVERPASS_HEADERS,
            ) as client:
                response = await client.post(
                    OVERPASS_URL,
                    data={"data": query},
                )
                if response.status_code in (429, 503, 504):
                    raise httpx.HTTPStatusError(
                        f"{response.status_code}", request=response.request, response=response
                    )
                response.raise_for_status()
                return response.json()
        except (httpx.HTTPStatusError, httpx.TimeoutException) as e:
            derniere_erreur = e
            if tentative < tentatives_max:
                attente = 10 * tentative  # 10s, 20s, 30s...
                print(
                    f"  ⏳ Overpass indisponible (tentative {tentative}/{tentatives_max}), "
                    f"nouvelle tentative dans {attente}s…",
                    flush=True,
                )
                await asyncio.sleep(attente)
    raise derniere_erreur


async def find_nearby(
    lat: float,
    lon: float,
    categorie: str,
    top_n: int = 10,
) -> dict:
    """
    Recherche les lieux d'une catégorie autour d'un point GPS.

    La fonction retourne :

    - les `top_n` lieux nommés les plus proches ;
    - les statistiques calculées sur tous les résultats nommés et
      exploitables retournés par Overpass.

    Exemple :

        resultat = await find_nearby(
            lat=42.965,
            lon=1.607,
            categorie="refuge",
            top_n=10,
        )
    """

    if categorie not in _CATEGORY_TAGS:
        raise ValueError(f"Catégorie inconnue : {categorie}")

    if categorie not in RAYON_RECHERCHE_M:
        raise ValueError(
            f"Aucun rayon défini pour la catégorie : {categorie}"
        )

    if top_n < 1:
        raise ValueError("top_n doit être supérieur ou égal à 1")

    # Postgres (via asyncpg) renvoie les colonnes DECIMAL sous forme de
    # decimal.Decimal, pas de float — soustraire un Decimal et un float
    # (coordonnées venant d'Overpass, en JSON donc déjà float) lève une
    # TypeError. On normalise ici, à l'entrée, une fois pour toutes.
    lat = float(lat)
    lon = float(lon)

    rayon = RAYON_RECHERCHE_M[categorie]
    query = _build_query(lat, lon, categorie)

    timeout = httpx.Timeout(
        timeout=75.0,
        connect=15.0,
    )

    data = await _appeler_overpass_avec_retry(query, timeout)

    tous: list[dict] = []

    # Empêche qu'un même objet OSM soit ajouté plusieurs fois s'il
    # correspond à plusieurs clauses de la requête.
    objets_deja_vus: set[tuple[str, int]] = set()

    for element in data.get("elements", []):
        osm_type = element.get("type")
        osm_id = element.get("id")

        if not osm_type or osm_id is None:
            continue

        cle_unique = (osm_type, osm_id)

        if cle_unique in objets_deja_vus:
            continue

        objets_deja_vus.add(cle_unique)

        # Les nodes possèdent directement lat/lon.
        # Les ways et relations utilisent généralement center.
        element_latitude = element.get("lat")
        element_longitude = element.get("lon")

        if element_latitude is None:
            element_latitude = element.get("center", {}).get("lat")

        if element_longitude is None:
            element_longitude = element.get("center", {}).get("lon")

        if element_latitude is None or element_longitude is None:
            continue

        tags = element.get("tags", {})

        # On privilégie le nom français lorsqu'il est disponible.
        nom = (
            tags.get("name:fr")
            or tags.get("name")
            or tags.get("official_name")
        )

        # Un lieu sans nom est difficilement exploitable dans l'interface.
        if not nom:
            continue

        distance = haversine_metres(
            lat,
            lon,
            element_latitude,
            element_longitude,
        )

        tous.append({
            "osm_id": osm_id,
            "osm_type": osm_type,
            "nom": nom,
            "latitude": element_latitude,
            "longitude": element_longitude,
            "distance_metres": distance,
            "adresse": _format_adresse(tags),
            "telephone": (
                tags.get("phone")
                or tags.get("contact:phone")
            ),
            "site_web": (
                tags.get("website")
                or tags.get("contact:website")
                or tags.get("url")
            ),
        })

    # Classement du plus proche au plus éloigné.
    tous.sort(
        key=lambda resultat: resultat["distance_metres"]
    )

    top = tous[:top_n]

    top10_distances = [
        resultat["distance_metres"]
        for resultat in tous[:10]
    ]

    stats = {
        "rayon_metres": rayon,

        # Nombre total de lieux nommés et exploitables.
        "nombre_total": len(tous),

        "nombre_500m": sum(
            1
            for resultat in tous
            if resultat["distance_metres"] <= 500
        ),

        "nombre_1000m": sum(
            1
            for resultat in tous
            if resultat["distance_metres"] <= 1_000
        ),

        "distance_min_m": (
            tous[0]["distance_metres"]
            if tous
            else None
        ),

        "distance_moy_top10_m": (
            round(
                sum(top10_distances)
                / len(top10_distances)
            )
            if top10_distances
            else None
        ),
    }

    return {
        "top": top,
        "stats": stats,
    }


def _format_adresse(tags: dict) -> str | None:
    """
    Construit une adresse lisible à partir des tags OSM.
    """

    numero = tags.get("addr:housenumber", "")
    rue = tags.get("addr:street", "")
    code_postal = tags.get("addr:postcode", "")
    ville = (
        tags.get("addr:city")
        or tags.get("addr:town")
        or tags.get("addr:village")
        or ""
    )

    parties = [
        numero,
        rue,
        code_postal,
        ville,
    ]

    adresse = " ".join(
        partie
        for partie in parties
        if partie
    ).strip()

    return adresse or None


def phrase_recommandation(
    categorie_label: str,
    nom: str,
    distance_metres: int,
) -> str:
    """
    Génère une phrase présentant le lieu le plus proche.

    Exemples :

    - L'hôtel X est à 2 km du lieu de tournage et est l'hôtel
      le plus proche.

    - Le refuge Y est à 5,2 km du lieu de tournage et est le
      refuge le plus proche.
    """

    if distance_metres < 1_000:
        distance_txt = f"{distance_metres} m"
    else:
        distance_km = distance_metres / 1_000

        if distance_km.is_integer():
            distance_txt = f"{int(distance_km)} km"
        else:
            distance_txt = (
                f"{distance_km:.1f} km"
                .replace(".", ",")
            )

    return (
        f"{categorie_label} {nom} est à {distance_txt} "
        f"du lieu de tournage et est "
        f"{categorie_label.lower()} le plus proche."
    )