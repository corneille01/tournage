"""
backend/overpass.py â€” Recherche des lieux Ã  proximitÃ© via Overpass API
(OpenStreetMap), gratuit et sans clÃ© API.

Ce module ne s'exÃ©cute JAMAIS sur une requÃªte visiteur. Il alimente
`amenity_cache` en tÃ¢che de fond (cron / script manuel), pour que le
site serve uniquement des donnÃ©es dÃ©jÃ  en base.

Cela permet de tenir la charge sans dÃ©pendre de la disponibilitÃ© ou
des limites d'Overpass au moment oÃ¹ les visiteurs utilisent le site.
"""

import asyncio
import math

import httpx


OVERPASS_URL = "https://overpass-api.de/api/interpreter"

# Identification de l'application auprÃ¨s du serveur Overpass.
OVERPASS_HEADERS = {
    "User-Agent": "Pelify-Tournage/1.0 (https://pelify.app)"
}


# Tags OpenStreetMap correspondant Ã  chaque catÃ©gorie affichÃ©e.
# LibellÃ© affichÃ© quand une commoditÃ© n'a pas de nom OSM â€” mieux vaut
# publier le point avec une Ã©tiquette gÃ©nÃ©rique que de le perdre.
_LABEL_GENERIQUE = {
    "hebergement": "HÃ©bergement",
    "refuge": "Refuge",
    "restaurant": "Restaurant",
    "office_tourisme": "Office de tourisme",
    "police": "Police / gendarmerie",
    "hopital": "Ã‰tablissement de santÃ©",
    "gare": "Gare",
    "aeroport": "AÃ©roport",
    "aerodrome": "AÃ©rodrome",
    "arret_bus": "ArrÃªt de transport",
    "parking": "Parking",
    "distributeur": "Distributeur / banque",
    "activite": "Point d'intÃ©rÃªt",
}

_CATEGORY_TAGS = {
    # hebergement, restaurant, office_tourisme et activite sont repris
    # par DATAtourisme (donnÃ©es officielles, plus complÃ¨tes) depuis
    # juillet 2026 â€” retirÃ©s d'ici pour ne plus solliciter Overpass
    # inutilement sur ces catÃ©gories. Les dicts ci-dessous (labels,
    # rayons, phrases) gardent leurs entrÃ©es pour ces 4 catÃ©gories â€”
    # inoffensif, elles ne sont juste plus jamais consultÃ©es puisque
    # la boucle dans refresh_cache.py itÃ¨re sur _CATEGORY_TAGS.

    # Refuges et Ã©tapes de randonnÃ©e
    "refuge": [
        "tourism=alpine_hut",
        "tourism=wilderness_hut",
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

    "aerodrome": [
        "aeroway=aerodrome",
    ],

    "arret_bus": [
        "highway=bus_stop",
        "public_transport=platform",
    ],

    "parking": [
        "amenity=parking",
    ],

    "distributeur": [
        "amenity=atm",
        "amenity=bank",
    ],
}


# Rayon maximal utilisÃ© pour chaque catÃ©gorie.
RAYON_RECHERCHE_M = {
    "hebergement": 15_000,
    "refuge": 25_000,
    "restaurant": 8_000,
    "office_tourisme": 20_000,
    "police": 20_000,
    "hopital": 25_000,
    "gare": 30_000,
    "aeroport": 60_000,
    "aerodrome": 30_000,
    "arret_bus": 2_000,
    "parking": 5_000,
    "distributeur": 3_000,
    "activite": 15_000,
}


# IcÃ´ne et couleur par catÃ©gorie.
# Elles sont utilisÃ©es par le frontend pour les marqueurs et les popups.
ICONES_CATEGORIE = {
    "hebergement": {
        "emoji": "ðŸ¨",
        "couleur": "#2a9d8f",
    },

    "refuge": {
        "emoji": "ðŸ¥¾",
        "couleur": "#588157",
    },

    "restaurant": {
        "emoji": "ðŸ½ï¸",
        "couleur": "#e76f51",
    },

    "office_tourisme": {
        "emoji": "â„¹ï¸",
        "couleur": "#264653",
    },

    "police": {
        "emoji": "ðŸš“",
        "couleur": "#023e8a",
    },

    "hopital": {
        "emoji": "ðŸ¥",
        "couleur": "#d00000",
    },

    "gare": {
        "emoji": "ðŸš‰",
        "couleur": "#6a4c93",
    },

    "aeroport": {
        "emoji": "âœˆï¸",
        "couleur": "#4361ee",
    },

    "aerodrome": {
        "emoji": "ðŸ›©ï¸",
        "couleur": "#7209b7",
    },

    "arret_bus": {
        "emoji": "ðŸšŒ",
        "couleur": "#f4a261",
    },

    "parking": {
        "emoji": "ðŸ…¿ï¸",
        "couleur": "#3a3a3a",
    },

    "distributeur": {
        "emoji": "ðŸ§",
        "couleur": "#06923e",
    },

     "activite": {
        "emoji": "ðŸŽ¡",
        "couleur": "#9b5de5",
    },

    "fetes_manifestations": {
        "emoji": "ðŸŽ‰",
        "couleur": "#f15bb5",
    },

    "station_service": {
        "emoji": "â›½",
        "couleur": "#f97316",
    },

}


def haversine_metres(
    lat1: float,
    lon1: float,
    lat2: float,
    lon2: float,
) -> int:
    """
    Calcule la distance Ã  vol d'oiseau entre deux points GPS.

    La distance retournÃ©e est exprimÃ©e en mÃ¨tres.
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
    Construit la requÃªte Overpass QL pour une catÃ©gorie.

    La recherche concerne :
    - les nodes ;
    - les ways ;
    - les relations.
    """

    if categorie not in _CATEGORY_TAGS:
        raise ValueError(f"CatÃ©gorie inconnue : {categorie}")

    if categorie not in RAYON_RECHERCHE_M:
        raise ValueError(
            f"Aucun rayon configurÃ© pour la catÃ©gorie : {categorie}"
        )

    rayon = RAYON_RECHERCHE_M[categorie]
    # Format dÃ©cimal fixe : Ã©vite la notation scientifique Python sur
    # les coordonnÃ©es trÃ¨s proches de 0 (l'Occitanie est proche du
    # mÃ©ridien de Greenwich), qu'Overpass ne saurait pas interprÃ©ter.
    lat = f"{lat:.7f}"
    lon = f"{lon:.7f}"
    clauses = []

    # AÃ©roport et aÃ©rodrome utilisent le mÃªme tag OSM de base
    # (aeroway=aerodrome) â€” la prÃ©sence d'un code IATA distingue un
    # vrai aÃ©roport commercial d'un simple terrain d'aviation lÃ©gÃ¨re.
    filtre_supplementaire = ""
    if categorie == "aeroport":
        filtre_supplementaire = '["iata"]'
    elif categorie == "aerodrome":
        filtre_supplementaire = '["iata"!~"."]'

    for tag in _CATEGORY_TAGS[categorie]:
        cle, valeur = tag.split("=", maxsplit=1)

        clauses.append(
            f'node["{cle}"="{valeur}"]{filtre_supplementaire}'
            f"(around:{rayon},{lat},{lon});"
        )

        clauses.append(
            f'way["{cle}"="{valeur}"]{filtre_supplementaire}'
            f"(around:{rayon},{lat},{lon});"
        )

        clauses.append(
            f'relation["{cle}"="{valeur}"]{filtre_supplementaire}'
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
    Le serveur public overpass-api.de limite le dÃ©bit (429) et time out
    parfois sous charge (503/504) â€” normal pour un service public
    gratuit et partagÃ©. On rÃ©essaie avec un dÃ©lai croissant plutÃ´t que
    d'abandonner immÃ©diatement, ce qui Ã©vite de perdre des lieux entiers
    juste parce qu'Overpass Ã©tait temporairement occupÃ©.
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
                    f"  â³ Overpass indisponible (tentative {tentative}/{tentatives_max}), "
                    f"nouvelle tentative dans {attente}sâ€¦",
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
    Recherche les lieux d'une catÃ©gorie autour d'un point GPS.

    La fonction retourne :

    - les `top_n` lieux nommÃ©s les plus proches ;
    - les statistiques calculÃ©es sur tous les rÃ©sultats nommÃ©s et
      exploitables retournÃ©s par Overpass.

    Exemple :

        resultat = await find_nearby(
            lat=42.965,
            lon=1.607,
            categorie="refuge",
            top_n=10,
        )
    """

    if categorie not in _CATEGORY_TAGS:
        raise ValueError(f"CatÃ©gorie inconnue : {categorie}")

    if categorie not in RAYON_RECHERCHE_M:
        raise ValueError(
            f"Aucun rayon dÃ©fini pour la catÃ©gorie : {categorie}"
        )

    if top_n < 1:
        raise ValueError("top_n doit Ãªtre supÃ©rieur ou Ã©gal Ã  1")

    # Postgres (via asyncpg) renvoie les colonnes DECIMAL sous forme de
    # decimal.Decimal, pas de float â€” soustraire un Decimal et un float
    # (coordonnÃ©es venant d'Overpass, en JSON donc dÃ©jÃ  float) lÃ¨ve une
    # TypeError. On normalise ici, Ã  l'entrÃ©e, une fois pour toutes.
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

    # EmpÃªche qu'un mÃªme objet OSM soit ajoutÃ© plusieurs fois s'il
    # correspond Ã  plusieurs clauses de la requÃªte.
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

        # Les nodes possÃ¨dent directement lat/lon.
        # Les ways et relations utilisent gÃ©nÃ©ralement center.
        element_latitude = element.get("lat")
        element_longitude = element.get("lon")

        if element_latitude is None:
            element_latitude = element.get("center", {}).get("lat")

        if element_longitude is None:
            element_longitude = element.get("center", {}).get("lon")

        if element_latitude is None or element_longitude is None:
            continue

        tags = element.get("tags", {})

        # On privilÃ©gie le nom franÃ§ais lorsqu'il est disponible.
        nom = (
            tags.get("name:fr")
            or tags.get("name")
            or tags.get("official_name")
            or tags.get("brand")
        )

        # Un lieu sans nom reste utile (au moins sa position) â€” on lui
        # donne un intitulÃ© gÃ©nÃ©rique plutÃ´t que de l'exclure, comme
        # demandÃ© : mieux vaut afficher "Distributeur" sans nom propre
        # que de perdre le point entiÃ¨rement.
        if not nom:
            nom = f"{_LABEL_GENERIQUE.get(categorie, 'Point')} (sans nom)"

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
            "horaires": tags.get("opening_hours"),
            "capacite": int(tags["capacity"]) if tags.get("capacity", "").isdigit() else None,
        })

    # Classement du plus proche au plus Ã©loignÃ©.
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

        # Nombre total de lieux nommÃ©s et exploitables.
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
    Construit une adresse lisible Ã  partir des tags OSM.
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
    GÃ©nÃ¨re une phrase prÃ©sentant le lieu le plus proche.

    Exemples :

    - L'hÃ´tel X est Ã  2 km du lieu de tournage et est l'hÃ´tel
      le plus proche.

    - Le refuge Y est Ã  5,2 km du lieu de tournage et est le
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
        f"{categorie_label} {nom} est Ã  {distance_txt} "
        f"du lieu de tournage et est "
        f"{categorie_label.lower()} le plus proche."
    )
