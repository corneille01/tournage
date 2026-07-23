"""
backend/main.py — API CinéTour.

Tous les endpoints lisent des données déjà en base (jamais d'appel
Overpass en direct sur une requête visiteur) — voir refresh_cache.py
pour le remplissage du cache.
"""

import os
import json
import asyncio
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from contextlib import asynccontextmanager

from db import init_db_pool, close_db_pool, fetch_all, fetch_one, execute
from overpass import phrase_recommandation, ICONES_CATEGORIE, haversine_metres
from seo import slugify, url_film, json_ld_film, meta_description

templates = Jinja2Templates(directory="templates")
BASE_URL = "https://tonsite.fr"  # à remplacer par le vrai domaine en prod

_LABELS_CATEGORIE = {
    "hebergement":     "L'hébergement",
    "restaurant":      "Le restaurant",
    "office_tourisme": "L'office de tourisme",
    "police":          "Le commissariat/gendarmerie",
    "hopital":         "L'hôpital",
    "gare":            "La gare",
    "aeroport":        "L'aéroport",
    "arret_bus":       "L'arrêt de bus",
    "parking":         "Le parking",
    "refuge":          "Le refuge",
    "distributeur":    "Le distributeur",
    "activite":        "L'activité",
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db_pool()
    yield
    await close_db_pool()


app = FastAPI(title="CinéTour API", lifespan=lifespan)

app.add_middleware(GZipMiddleware, minimum_size=500)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # à restreindre au domaine réel en prod
    allow_methods=["GET"],
)


# ── Liste des films (barre latérale) ─────────────────────────────
@app.get("/api/films")
async def liste_films(
    region: str = Query("Occitanie"),
    media_type: str | None = Query(None, description="movie, tv ou anime"),
    annee: int | None = Query(None),
    departement: str | None = Query(None),
    commune: str | None = Query(None),
    q: str | None = Query(None, description="Recherche par titre"),
    tri: str = Query("titre", description="titre ou popularite"),
    page: int = Query(1, ge=1),
    par_page: int = Query(60, le=200),
):
    """
    Liste des films/séries pour la sidebar, avec le nombre de lieux de
    tournage en Occitanie inclus directement (affiché sur la carte
    avant même de cliquer) — évite un aller-retour supplémentaire par
    film juste pour ce chiffre.
    """
    offset = (page - 1) * par_page
    conditions = ["f.region = %s", "f.statut = 'publie'"]
    params: list = [region]

    if media_type:
        conditions.append("f.media_type = %s")
        params.append(media_type)
    if annee:
        conditions.append("f.annee = %s")
        params.append(annee)
    if q:
        conditions.append("f.titre ILIKE %s")
        params.append(f"%{q}%")
    if departement or commune:
        conditions.append(
            "EXISTS (SELECT 1 FROM lieux_tournage lt WHERE lt.film_id = f.id"
            + (" AND lt.departement = %s" if departement else "")
            + (" AND lt.commune = %s" if commune else "")
            + ")"
        )
        if departement:
            params.append(departement)
        if commune:
            params.append(commune)

    where = " AND ".join(conditions)
    # Whitelist stricte : on n'insère jamais le paramètre "tri" tel
    # quel dans le SQL (protection contre l'injection).
    ordre_sql = "f.popularite DESC NULLS LAST, f.titre ASC" if tri == "popularite" else "f.titre ASC"
    films = await fetch_all(
        f"""
        SELECT f.id, f.titre, f.titre_original, f.media_type, f.annee, f.poster_url,
               f.popularite, COUNT(lt.id) AS nb_lieux
        FROM films f
        LEFT JOIN lieux_tournage lt ON lt.film_id = f.id
        WHERE {where}
        GROUP BY f.id
        ORDER BY {ordre_sql}
        LIMIT %s OFFSET %s
        """,
        (*params, par_page, offset),
    )
    return {"films": films, "page": page}


@app.get("/api/filtres")
async def options_filtres(region: str = Query("Occitanie")):
    """
    Valeurs disponibles pour peupler les menus déroulants (année,
    département, commune) — évite de coder ces listes en dur côté
    frontend, elles reflètent toujours les données réelles en base.
    """
    annees = await fetch_all(
        """
        SELECT DISTINCT annee FROM films
        WHERE region = %s AND statut = 'publie' AND annee IS NOT NULL
        ORDER BY annee DESC
        """,
        (region,),
    )
    departements = await fetch_all(
        """
        SELECT DISTINCT lt.departement FROM lieux_tournage lt
        JOIN films f ON f.id = lt.film_id
        WHERE f.region = %s AND f.statut = 'publie' AND lt.departement IS NOT NULL
        ORDER BY lt.departement ASC
        """,
        (region,),
    )
    communes = await fetch_all(
        """
        SELECT DISTINCT lt.commune FROM lieux_tournage lt
        JOIN films f ON f.id = lt.film_id
        WHERE f.region = %s AND f.statut = 'publie' AND lt.commune IS NOT NULL
          -- Wikidata résout parfois le lieu directement au niveau du
          -- département plutôt que de la commune précise — on exclut
          -- ces valeurs du filtre "commune" pour ne pas les dupliquer
          -- avec le filtre "département".
          AND lt.commune NOT IN (
              SELECT DISTINCT departement FROM lieux_tournage WHERE departement IS NOT NULL
          )
        ORDER BY lt.commune ASC
        """,
        (region,),
    )
    return {
        "annees": [a["annee"] for a in annees],
        "departements": [d["departement"] for d in departements],
        "communes": [c["commune"] for c in communes],
    }


@app.get("/api/stats")
async def stats_globales(region: str = Query("Occitanie")):
    """
    Chiffres clés pour le panneau statistiques — c'est ce qui
    transforme l'outil de "carte sympa" en "outil d'observation" pour
    l'Agence Unique : volumétrie par département, par média, par
    décennie, complétude des données.
    """
    par_departement = await fetch_all(
        """
        SELECT lt.departement, COUNT(DISTINCT lt.film_id) AS nb_films, COUNT(*) AS nb_lieux
        FROM lieux_tournage lt
        JOIN films f ON f.id = lt.film_id
        WHERE f.region = %s AND f.statut = 'publie' AND lt.departement IS NOT NULL
        GROUP BY lt.departement ORDER BY nb_lieux DESC
        """,
        (region,),
    )
    par_media_type = await fetch_all(
        """
        SELECT media_type, COUNT(*) AS nb
        FROM films WHERE region = %s AND statut = 'publie'
        GROUP BY media_type
        """,
        (region,),
    )
    par_decennie = await fetch_all(
        """
        SELECT (annee / 10) * 10 AS decennie, COUNT(*) AS nb
        FROM films
        WHERE region = %s AND statut = 'publie' AND annee IS NOT NULL
        GROUP BY decennie ORDER BY decennie
        """,
        (region,),
    )
    totaux = await fetch_one(
        """
        SELECT
          (SELECT COUNT(*) FROM films WHERE region = %s AND statut = 'publie') AS nb_films,
          (SELECT COUNT(*) FROM lieux_tournage lt JOIN films f ON f.id = lt.film_id
             WHERE f.region = %s AND f.statut = 'publie') AS nb_lieux,
          (SELECT COUNT(*) FROM films WHERE region = %s AND statut = 'brouillon') AS nb_en_attente
        """,
        (region, region, region),
    )
    return {
        "totaux": totaux,
        "par_departement": par_departement,
        "par_media_type": par_media_type,
        "par_decennie": par_decennie,
    }


def _recommandation_departement(d: dict) -> str:
    """
    Traduction en texte des indicateurs bruts, dans l'esprit "outil
    d'aide à la décision" plutôt que "carte sympa" : quelques règles
    simples plutôt qu'un score composite arbitraire, faciles à
    expliquer et à faire évoluer avec l'Agence Unique.
    """
    nb_lieux = d["nb_lieux"] or 0
    moy_heberg = d["moy_hebergement"] or 0
    moy_resto = d["moy_restaurant"] or 0
    lieux_isoles = d["lieux_sans_hebergement_5km"] or 0

    if nb_lieux == 0:
        return "Aucune donnée suffisante pour ce département."
    if moy_heberg >= 3 and moy_resto >= 3:
        return "Bien équipé en moyenne — valorisation immédiate possible (circuit ciné-touristique)."
    if lieux_isoles > nb_lieux / 2:
        return "Plus de la moitié des lieux sont isolés (aucun hébergement à moins de 5 km) — aménagement ou signalétique à prévoir avant toute promotion."
    if moy_heberg < 1:
        return "Faible densité d'hébergement — potentiel réel mais nécessite des partenariats avec des hébergeurs avant une valorisation touristique large."
    return "Équipement intermédiaire — à évaluer au cas par cas selon les lieux les plus emblématiques."


@app.get("/api/lieux/tous-points")
async def tous_les_points(region: str = Query("Occitanie")):
    """
    Coordonnées de tous les lieux de tournage publiés, sans détail —
    juste de quoi alimenter la carte de chaleur (densité visuelle des
    zones les plus sollicitées, en complément de la choroplèthe par
    département qui raisonne au niveau administratif).
    """
    points = await fetch_all(
        """
        SELECT lt.latitude, lt.longitude
        FROM lieux_tournage lt
        JOIN films f ON f.id = lt.film_id
        WHERE f.region = %s AND f.statut = 'publie'
        """,
        (region,),
    )
    return {"points": [[float(p["latitude"]), float(p["longitude"])] for p in points]}


@app.get("/api/analyse")
async def analyse_territoriale(region: str = Query("Occitanie")):
    """
    Version approfondie de /api/stats, pensée pour la page d'analyse
    territoriale : compare les départements sur des indicateurs
    d'équipement réel (pas seulement le nombre de films), avec une
    recommandation textuelle par département.
    """
    par_departement = await fetch_all(
        """
        SELECT
            lt.departement,
            COUNT(DISTINCT lt.film_id) AS nb_films,
            COUNT(DISTINCT lt.id) AS nb_lieux,
            ROUND(AVG(hs.nombre_total) FILTER (WHERE hs.categorie = 'hebergement')::numeric, 1) AS moy_hebergement,
            ROUND(AVG(hs.nombre_total) FILTER (WHERE hs.categorie = 'restaurant')::numeric, 1) AS moy_restaurant,
            COUNT(DISTINCT hs.lieu_tournage_id) FILTER (
                WHERE hs.categorie = 'hebergement' AND hs.nombre_total = 0
            ) AS lieux_sans_hebergement_5km
        FROM lieux_tournage lt
        JOIN films f ON f.id = lt.film_id
        LEFT JOIN amenity_stats hs ON hs.lieu_tournage_id = lt.id
        WHERE f.region = %s AND f.statut = 'publie' AND lt.departement IS NOT NULL
        GROUP BY lt.departement
        ORDER BY nb_lieux DESC
        """,
        (region,),
    )

    resultat = []
    for d in par_departement:
        d = dict(d)
        d["recommandation"] = _recommandation_departement(d)
        resultat.append(d)

    # Films sans coordonnées / sans image / non validés — complétude
    # des données, utile pour prioriser le travail éditorial restant.
    completude = await fetch_one(
        """
        SELECT
            (SELECT COUNT(*) FROM films WHERE region = %s AND statut = 'brouillon') AS brouillons,
            (SELECT COUNT(*) FROM films WHERE region = %s AND statut = 'publie' AND poster_url IS NULL) AS sans_poster,
            (SELECT COUNT(*) FROM lieux_tournage lt JOIN films f ON f.id = lt.film_id
                WHERE f.region = %s AND lt.photo_url IS NULL) AS lieux_sans_photo
        """,
        (region, region, region),
    )

    return {"par_departement": resultat, "completude": completude}


# ── Détail d'un film + ses lieux de tournage ─────────────────────
TMDB_API_KEY = os.getenv("TMDB_API_KEY", "")
PLATEFORMES_CACHE_JOURS = 7


async def _plateformes_streaming(film: dict) -> list[dict]:
    """
    Où regarder ce film en France (TMDB watch/providers), mis en cache
    en base pour ne pas rappeler TMDB à chaque visite. C'est ici que
    les liens d'affiliation (Awin etc.) doivent être insérés — chaque
    entrée retournée a un champ "lien_affilie" vide à remplir avec ton
    vrai lien tracké une fois les partenariats en place.
    """
    if not film.get("tmdb_id"):
        return []

    def _parser_cache(valeur) -> list:
        """asyncpg ne décode pas automatiquement JSONB : selon le driver
        et la version, on peut recevoir soit déjà une liste, soit une
        chaîne JSON brute. On gère les deux pour ne jamais planter le
        frontend avec un .map() sur une chaîne."""
        if not valeur:
            return []
        if isinstance(valeur, str):
            try:
                return json.loads(valeur)
            except (json.JSONDecodeError, TypeError):
                return []
        return valeur

    dernier_maj = film.get("plateformes_maj")
    if film.get("plateformes_json") and dernier_maj:
        age_jours = (datetime.now(timezone.utc) - dernier_maj.replace(tzinfo=timezone.utc)).days
        if age_jours < PLATEFORMES_CACHE_JOURS:
            return _parser_cache(film["plateformes_json"])

    if not TMDB_API_KEY:
        return _parser_cache(film.get("plateformes_json"))

    endpoint = "movie" if film["media_type"] == "movie" else "tv"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"https://api.themoviedb.org/3/{endpoint}/{film['tmdb_id']}/watch/providers",
                params={"api_key": TMDB_API_KEY},
            )
            resp.raise_for_status()
            data = resp.json().get("results", {}).get("FR", {})
    except Exception:
        return _parser_cache(film.get("plateformes_json"))

    plateformes = []
    for categorie in ("flatrate", "rent", "buy"):
        for p in data.get(categorie, []):
            plateformes.append({
                "nom": p["provider_name"],
                "logo_url": f"https://image.tmdb.org/t/p/w92{p['logo_path']}",
                "type": {"flatrate": "streaming", "rent": "location", "buy": "achat"}[categorie],
                "lien_affilie": "",  # à remplir : lien Awin/partenaire pour ce provider
            })

    await _sauvegarder_plateformes_cache(film["id"], plateformes)
    return plateformes


async def _sauvegarder_plateformes_cache(film_id: int, plateformes: list[dict]) -> None:
    """
    Isolé dans sa propre fonction avec gestion d'erreur : si l'écriture
    du cache échoue (mismatch de type, base indisponible...), la page
    doit quand même s'afficher avec les plateformes fraîchement
    récupérées — juste sans les mettre en cache cette fois-ci. Ne
    JAMAIS laisser un souci de cache secondaire faire planter la fiche
    film entière (c'est ce qui causait le 500 sur tous les films).
    """
    try:
        await execute(
            "UPDATE films SET plateformes_json = %s, plateformes_maj = %s WHERE id = %s",
            (json.dumps(plateformes), datetime.now(timezone.utc), film_id),
        )
    except Exception as e:
        print(f"⚠️ Cache plateformes non sauvegardé pour film {film_id}: {e}", flush=True)


@app.get("/api/films/{film_id}")
async def detail_film(film_id: int):
    film = await fetch_one(
        "SELECT * FROM films WHERE id = %s AND statut = 'publie'", (film_id,)
    )
    if not film:
        raise HTTPException(404, "Film introuvable")

    lieux = await fetch_all(
        """
        SELECT id, nom, description, commune, departement,
               latitude, longitude, photo_url, anecdote, description_wikipedia
        FROM lieux_tournage
        WHERE film_id = %s
        """,
        (film_id,),
    )
    plateformes = await _plateformes_streaming(film)
    return {"film": film, "lieux": lieux, "plateformes": plateformes}


# ── Amenities proches d'un lieu (appelé au clic sur l'icône) ─────
@app.get("/api/lieux/{lieu_id}/amenities")
async def amenities_proches(lieu_id: int):
    lieu = await fetch_one(
        "SELECT id, nom, latitude, longitude FROM lieux_tournage WHERE id = %s",
        (lieu_id,),
    )
    if not lieu:
        raise HTTPException(404, "Lieu introuvable")

    rows = await fetch_all(
        """
        SELECT categorie, nom, latitude, longitude, distance_metres,
               adresse, telephone, site_web, horaires, photo_url, rang
        FROM amenity_cache
        WHERE lieu_tournage_id = %s
        ORDER BY categorie, rang
        """,
        (lieu_id,),
    )

    stats_rows = await fetch_all(
        """
        SELECT categorie, rayon_metres, nombre_total, nombre_500m,
               nombre_1000m, distance_min_m, distance_moy_top10_m
        FROM amenity_stats
        WHERE lieu_tournage_id = %s
        """,
        (lieu_id,),
    )
    stats_par_categorie = {r["categorie"]: r for r in stats_rows}

    par_categorie: dict[str, list[dict]] = {}
    for r in rows:
        par_categorie.setdefault(r["categorie"], []).append(r)

    # Phrase de recommandation pour le plus proche de chaque catégorie,
    # incluant le total trouvé dans le rayon (pas seulement le top 10 —
    # "3 restaurants dans le coin" et "80 restaurants dans le coin" ne
    # doivent pas avoir l'air identiques une fois coupés à 10 affichés).
    plus_proches = {}
    for categorie, items in par_categorie.items():
        if items:
            top = items[0]  # déjà trié par rang
            label = _LABELS_CATEGORIE.get(categorie, categorie)
            phrase = phrase_recommandation(label, top["nom"], top["distance_metres"])
            stat = stats_par_categorie.get(categorie)
            if stat and stat["nombre_total"] > 1:
                phrase += f" ({stat['nombre_total']} au total dans un rayon de {stat['rayon_metres'] // 1000} km.)"
            plus_proches[categorie] = phrase

    return {
        "lieu": lieu,
        "amenities": par_categorie,
        "stats": stats_par_categorie,
        "phrases_recommandation": plus_proches,
        "icones_categorie": ICONES_CATEGORIE,
    }


ORS_API_KEY = os.getenv("ORS_API_KEY", "")

# Serveur de démonstration OSRM (open source, sponsorisé par FOSSGIS) —
# aucune clé requise. Usage non-commercial raisonnable, max 1 req/s,
# aucune garantie de disponibilité : parfait pour la phase prototype,
# mais à remplacer par un vrai service (ORS avec clé, ou auto-hébergement)
# si le trafic monte vraiment en échelle.
OSRM_URL = "https://router.project-osrm.org"
_OSRM_PROFILS = {"foot-walking": "foot", "driving-car": "driving"}


_TRADUCTION_MANOEUVRES = {
    ("turn", "left"): "Tournez à gauche",
    ("turn", "right"): "Tournez à droite",
    ("turn", "slight left"): "Serrez légèrement à gauche",
    ("turn", "slight right"): "Serrez légèrement à droite",
    ("turn", "sharp left"): "Tournez fortement à gauche",
    ("turn", "sharp right"): "Tournez fortement à droite",
    ("turn", "straight"): "Continuez tout droit",
    ("turn", "uturn"): "Faites demi-tour",
    ("depart", None): "Départ",
    ("arrive", None): "Vous êtes arrivé à destination",
    ("continue", None): "Continuez tout droit",
    ("merge", None): "Rejoignez la voie",
    ("roundabout", None): "Prenez le rond-point",
    ("new name", None): "Continuez",
}


def _traduire_manoeuvre(maneuver: dict, nom_rue: str | None) -> str:
    type_ = maneuver.get("type", "")
    modifier = maneuver.get("modifier")
    phrase = (
        _TRADUCTION_MANOEUVRES.get((type_, modifier))
        or _TRADUCTION_MANOEUVRES.get((type_, None))
        or "Continuez"
    )
    if nom_rue and type_ not in ("arrive", "depart"):
        phrase += f" sur {nom_rue}"
    return phrase


async def _itineraire_osrm(coords_lonlat: list[list[float]], mode: str, avec_etapes: bool = False, tentatives: int = 2) -> dict | None:
    profil = _OSRM_PROFILS.get(mode, "driving")
    chemin_coords = ";".join(f"{lon},{lat}" for lon, lat in coords_lonlat)

    derniere_erreur = None
    for tentative in range(1, tentatives + 1):
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    f"{OSRM_URL}/route/v1/{profil}/{chemin_coords}",
                    params={
                        "overview": "full", "geometries": "geojson",
                        "steps": "true" if avec_etapes else "false",
                    },
                )
                resp.raise_for_status()
                data = resp.json()
            route = data["routes"][0]
            duree = round(route["duration"])
            if profil == "foot":
                # Le serveur de démo OSRM ne différencie pas toujours
                # correctement la vitesse piéton de la vitesse voiture sur
                # certains tronçons — on recalcule nous-mêmes avec une
                # vitesse de marche standard (5 km/h) plutôt que de faire
                # confiance à une durée parfois identique à la voiture.
                duree = round(route["distance"] / (5000 / 3600))  # 5 km/h en m/s
            resultat = {
                "type": "route_reelle",
                "geometry": route["geometry"],
                "distance_metres": round(route["distance"]),
                "duree_secondes": duree,
            }
            if avec_etapes:
                etapes = []
                for leg in route.get("legs", []):
                    for step in leg.get("steps", []):
                        loc = step["maneuver"]["location"]  # [lon, lat]
                        etapes.append({
                            "instruction": _traduire_manoeuvre(step["maneuver"], step.get("name") or None),
                            "distance_metres": round(step["distance"]),
                            "latitude": loc[1],
                            "longitude": loc[0],
                        })
                resultat["etapes_navigation"] = etapes
            return resultat
        except Exception as e:
            derniere_erreur = e
            if tentative < tentatives:
                await asyncio.sleep(1.5)  # le serveur public OSRM limite à 1 req/s

    print(f"⚠️ OSRM indisponible après {tentatives} tentative(s): {derniere_erreur}", flush=True)
    return None


def _ordre_plus_proche_voisin(lieux: list[dict]) -> list[dict]:
    """Ordonne les lieux par plus proche voisin (heuristique simple,
    pas un vrai TSP optimal — largement suffisant pour quelques lieux
    par film et beaucoup plus lisible qu'un ordre arbitraire)."""
    if len(lieux) <= 2:
        return lieux
    restants = lieux[:]
    ordre = [restants.pop(0)]
    while restants:
        dernier = ordre[-1]
        plus_proche = min(
            restants,
            key=lambda l: haversine_metres(
                float(dernier["latitude"]), float(dernier["longitude"]),
                float(l["latitude"]), float(l["longitude"]),
            ),
        )
        restants.remove(plus_proche)
        ordre.append(plus_proche)
    return ordre


@app.get("/api/itineraire")
async def itineraire_point_a_point(
    depart_lat: float = Query(...),
    depart_lon: float = Query(...),
    arrivee_lat: float = Query(...),
    arrivee_lon: float = Query(...),
    mode: str = Query("foot-walking", description="foot-walking ou driving-car"),
    etapes: bool = Query(False, description="Renvoyer aussi les instructions de navigation pas à pas"),
):
    """
    Itinéraire entre deux points (lieu de tournage ↔ commodité, ou
    position GPS réelle ↔ destination pour la navigation guidée).
    Même logique de repli que /trace : ligne droite clairement
    annoncée comme estimation si OSRM/OpenRouteService indisponibles.
    """
    if mode not in ("foot-walking", "driving-car"):
        raise HTTPException(400, "mode doit être 'foot-walking' ou 'driving-car'")

    coords = [[depart_lon, depart_lat], [arrivee_lon, arrivee_lat]]

    resultat_osrm = await _itineraire_osrm(coords, mode, avec_etapes=etapes)
    if resultat_osrm:
        return resultat_osrm

    if ORS_API_KEY:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    f"https://api.openrouteservice.org/v2/directions/{mode}/geojson",
                    headers={"Authorization": ORS_API_KEY},
                    json={"coordinates": coords},
                )
                resp.raise_for_status()
                geojson = resp.json()
            feature = geojson["features"][0]
            return {
                "type": "route_reelle",
                "geometry": feature["geometry"],
                "distance_metres": round(feature["properties"]["summary"]["distance"]),
                "duree_secondes": round(feature["properties"]["summary"]["duration"]),
            }
        except Exception as e:
            print(f"⚠️ OpenRouteService indisponible: {e} → repli ligne droite", flush=True)

    distance = haversine_metres(depart_lat, depart_lon, arrivee_lat, arrivee_lon)
    return {
        "type": "estimation_vol_oiseau",
        "geometry": {"type": "LineString", "coordinates": coords},
        "distance_metres": distance,
        "duree_secondes": None,
    }


@app.get("/api/films/{film_id}/trace")
async def trace_film(film_id: int):
    """
    "Sur les traces de {film}" — relie tous les lieux de tournage d'un
    film en Occitanie par un itinéraire réel (routes, pas à vol
    d'oiseau), via OpenRouteService. Si l'API échoue ou n'est pas
    configurée (ORS_API_KEY manquante), repli en lignes droites avec
    la distance clairement annoncée comme une estimation.
    """
    lieux = await fetch_all(
        "SELECT id, nom, commune, latitude, longitude FROM lieux_tournage WHERE film_id = %s",
        (film_id,),
    )
    if len(lieux) < 2:
        raise HTTPException(400, "Ce film n'a qu'un seul lieu recensé — pas de tracé possible.")

    lieux_ordonnes = _ordre_plus_proche_voisin(lieux)
    coords_lonlat = [[float(l["longitude"]), float(l["latitude"])] for l in lieux_ordonnes]

    resultat_osrm = await _itineraire_osrm(coords_lonlat, "driving-car")
    if resultat_osrm:
        resultat_osrm["etapes"] = lieux_ordonnes
        return resultat_osrm

    if ORS_API_KEY:
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.post(
                    "https://api.openrouteservice.org/v2/directions/driving-car/geojson",
                    headers={"Authorization": ORS_API_KEY},
                    json={"coordinates": coords_lonlat},
                )
                resp.raise_for_status()
                geojson = resp.json()
            feature = geojson["features"][0]
            return {
                "type": "route_reelle",
                "geometry": feature["geometry"],
                "distance_metres": round(feature["properties"]["summary"]["distance"]),
                "duree_secondes": round(feature["properties"]["summary"]["duration"]),
                "etapes": lieux_ordonnes,
            }
        except Exception as e:
            print(f"⚠️ OpenRouteService indisponible: {e} → repli ligne droite", flush=True)

    # Repli : lignes droites, distance clairement annoncée comme estimation
    distance_totale = sum(
        haversine_metres(
            float(lieux_ordonnes[i]["latitude"]), float(lieux_ordonnes[i]["longitude"]),
            float(lieux_ordonnes[i + 1]["latitude"]), float(lieux_ordonnes[i + 1]["longitude"]),
        )
        for i in range(len(lieux_ordonnes) - 1)
    )
    return {
        "type": "estimation_vol_oiseau",
        "geometry": {
            "type": "LineString",
            "coordinates": coords_lonlat,
        },
        "distance_metres": round(distance_totale),
        "duree_secondes": None,
        "etapes": lieux_ordonnes,
    }


# ══════════════════════════════════════════════════════════════
# PAGES RENDUES CÔTÉ SERVEUR (SEO)
# ══════════════════════════════════════════════════════════════

@app.get("/films/{slug_id}", response_class=HTMLResponse)
async def page_film(request: Request, slug_id: str):
    """
    URL du type /films/le-pacte-des-loups-42. Le slug n'est pas
    utilisé pour la recherche en base (juste l'id final) — s'il ne
    correspond pas au slug canonique du film (titre changé, faute de
    frappe dans un lien externe...), on redirige en 301 vers la bonne
    URL plutôt que d'afficher une page dupliquée sous deux adresses
    (mauvais pour le SEO).
    """
    try:
        film_id = int(slug_id.rsplit("-", 1)[-1])
    except ValueError:
        raise HTTPException(404, "Film introuvable")

    film = await fetch_one(
        "SELECT * FROM films WHERE id = %s AND statut = 'publie'", (film_id,)
    )
    if not film:
        raise HTTPException(404, "Film introuvable")

    slug_canonique = slugify(film["titre"])
    if slug_id != f"{slug_canonique}-{film_id}":
        return RedirectResponse(url_film(film), status_code=301)

    lieux = await fetch_all(
        """
        SELECT id, nom, description, commune, departement, latitude, longitude
        FROM lieux_tournage WHERE film_id = %s
        """,
        (film_id,),
    )

    return templates.TemplateResponse(
        "film_detail.html",
        {
            "request": request,
            "film": film,
            "lieux": lieux,
            "base_url": BASE_URL,
            "url_film": url_film(film),
            "meta_desc": meta_description(film, lieux),
            "json_ld": json_ld_film(film, lieux, BASE_URL),
        },
    )


@app.get("/sitemap.xml", response_class=PlainTextResponse)
async def sitemap():
    films = await fetch_all(
        "SELECT id, titre, date_maj FROM films WHERE statut = 'publie'"
    )
    urls = "\n".join(
        f"""  <url>
    <loc>{BASE_URL}{url_film(f)}</loc>
    <lastmod>{f['date_maj'].date().isoformat()}</lastmod>
    <changefreq>monthly</changefreq>
  </url>"""
        for f in films
    )
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>{BASE_URL}/</loc>
    <changefreq>weekly</changefreq>
  </url>
{urls}
</urlset>"""
    return PlainTextResponse(content=xml, media_type="application/xml")


@app.get("/robots.txt", response_class=PlainTextResponse)
async def robots():
    return f"""User-agent: *
Allow: /
Disallow: /api/
Sitemap: {BASE_URL}/sitemap.xml
"""


# Doit rester la DERNIÈRE route déclarée : sert index.html, style.css,
# app.js, manifest.json, sw.js… Si elle était déclarée plus haut, elle
# intercepterait toutes les requêtes avant que /films/{slug_id},
# /sitemap.xml etc. n'aient une chance de matcher.
app.mount("/", StaticFiles(directory="../frontend", html=True), name="static")