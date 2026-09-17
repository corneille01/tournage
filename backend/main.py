"""
backend/main.py — API CinéTour.

Tous les endpoints lisent des données déjà en base (jamais d'appel
Overpass en direct sur une requête visiteur) — voir refresh_cache.py
pour le remplissage du cache.
"""


from geoplateforme import (
    calculer_itineraire as calculer_itineraire_geoplateforme,
    GeoplateformeError,
    RESOURCE_ITINERAIRE,
)
import logging

logger = logging.getLogger(__name__)
from analyse_indicateurs import construire_indicateurs_cinetourisme, construire_observatoire_statistique
from navigation_cache import navigation_cache
import os
import json
import asyncio
import hashlib
import secrets
import uuid
from decimal import Decimal

from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from contextlib import asynccontextmanager

from db import init_db_pool, close_db_pool, fetch_all, fetch_one, execute
from overpass import phrase_recommandation, ICONES_CATEGORIE, haversine_metres, RAYON_RECHERCHE_M
from seo import slugify, url_film, json_ld_film, meta_description
from visites_cinetouristiques import creneaux_pour_date

templates = Jinja2Templates(directory="templates")
BASE_URL = "https://tournage.pelify.app"  # à remplacer par le vrai domaine en prod

_LABELS_CATEGORIE = {
    "hebergement":     "L'hébergement",
    "restaurant":      "Le restaurant",
    "office_tourisme": "L'office de tourisme",
    "police":          "Le commissariat/gendarmerie",
    "hopital":         "L'hôpital",
    "gare":            "La gare",
    "aeroport":        "L'aéroport",
    "aerodrome":       "L'aérodrome",
    "arret_bus":       "L'arrêt de bus",
    "parking":         "Le parking",
    "refuge":          "Le refuge",
    "distributeur":    "Le distributeur",
    "activite":        "L'activité",
}



PELIFY_USER_COOKIE = "pelify_user"
PELIFY_USER_SECRET = os.getenv("PELIFY_USER_SECRET", "pelify-dev-secret-change-me")


def _hash_user_token(token: str) -> str:
    return hashlib.sha256((PELIFY_USER_SECRET + ":" + token).encode("utf-8")).hexdigest()


async def _ensure_profile(request: Request, response: Response) -> dict:
    """Identifie un visiteur de façon pseudonyme pour sauvegarder ses parcours.

    Aucun nom, email ou position GPS n'est exigé. Le cookie contient un jeton
    aléatoire et la base ne conserve que son hash. Cela permet d'avoir un
    historique par navigateur sans transformer Pelify en système de compte.
    """
    token = request.cookies.get(PELIFY_USER_COOKIE)
    created = False
    if not token or len(token) < 32:
        token = secrets.token_urlsafe(48)
        created = True
    token_hash = _hash_user_token(token)
    user = await fetch_one(
        "SELECT id, created_at, last_seen_at FROM pelify_users WHERE visitor_token_hash = %s",
        (token_hash,),
    )
    if not user:
        user_id = str(uuid.uuid4())
        await execute(
            "INSERT INTO pelify_users (id, visitor_token_hash) VALUES (%s, %s)",
            (user_id, token_hash),
        )
        user = {"id": user_id}
        created = True
    else:
        await execute("UPDATE pelify_users SET last_seen_at = NOW() WHERE id = %s", (user["id"],))
    if created:
        response.set_cookie(
            PELIFY_USER_COOKIE,
            token,
            max_age=60 * 60 * 24 * 365,
            httponly=True,
            samesite="lax",
            secure=os.getenv("COOKIE_SECURE", "1") != "0",
            path="/",
        )
    return user


def _json_safe(value):
    if isinstance(value, (datetime,)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


async def _enregistrer_parcours_historique(request: Request, response: Response, body: dict, resultat: dict):
    try:
        user = await _ensure_profile(request, response)
        options = {
            "mode": body.get("mode"),
            "temps_disponible_minutes": body.get("temps_disponible_minutes"),
            "temps_visite_minutes": body.get("temps_visite_minutes"),
            "retour_depart": body.get("retour_depart"),
            "categories_interet": body.get("categories_interet") or [],
            "budget_level": body.get("budget_level"),
            "accessibilite": body.get("accessibilite"),
            "optimiser": body.get("optimiser"),
            "inclure_visites_guidees": body.get("inclure_visites_guidees", True),
            "visites_guidees": body.get("visites_guidees") or [],
            "heure_depart": body.get("heure_depart") or "09:00",
            "date_sortie": body.get("date_sortie"),
            "budget_max_euros": body.get("budget_max_euros"),
            "depart": body.get("depart"),
        }
        resume_resultat = {
            "distance_metres": resultat.get("distance_metres"),
            "duree_secondes": resultat.get("duree_secondes"),
            "duree_totale_estimee_secondes": resultat.get("duree_totale_estimee_secondes"),
            "budget_respecte": resultat.get("budget_respecte"),
            "depart": resultat.get("depart"),
            "recommandations_par_etape": resultat.get("recommandations_par_etape", {}),
            "amenities": {k: v[:3] for k, v in (resultat.get("amenities") or {}).items()},
        }
        lieux = [{k: x.get(k) for k in ("id", "nom", "commune", "departement", "latitude", "longitude", "film_id", "film_titre")} for x in resultat.get("etapes", [])]
        titre = " → ".join(str(x.get("nom") or "Lieu") for x in resultat.get("etapes", [])[:3])
        if len(resultat.get("etapes", [])) > 3:
            titre += "…"
        await execute(
            """INSERT INTO pelify_parcours_history
               (user_id, titre, options_json, lieux_json, resultat_json, nb_etapes, distance_metres, duree_secondes, duree_totale_estimee_secondes, budget_level)
               VALUES (%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s,%s,%s,%s,%s)""",
            (
                user["id"], titre or "Mon parcours cinéma", json.dumps(_json_safe(options)),
                json.dumps(_json_safe(lieux)), json.dumps(_json_safe(resume_resultat)),
                len(lieux), resultat.get("distance_metres"), resultat.get("duree_secondes"),
                resultat.get("duree_totale_estimee_secondes"), body.get("budget_level") or "equilibre",
            ),
        )
    except Exception:
        logger.exception("Impossible d'enregistrer l'historique Pelify")

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db_pool()
    yield
    await close_db_pool()


app = FastAPI(title="CinéTour API", lifespan=lifespan)

@app.exception_handler(Exception)
async def _api_erreur_interne(request: Request, exc: Exception):
    # Évite qu'un proxy/serveur renvoie simplement « Internal Server Error »
    # et que le navigateur tente ensuite de parser cette chaîne comme du JSON.
    # Les détails techniques restent dans les logs serveur.
    import logging
    logging.getLogger(__name__).exception("Erreur interne sur %s", request.url.path, exc_info=exc)
    if request.url.path.startswith("/api/"):
        return JSONResponse(
            status_code=500,
            content={"detail": "Erreur interne du serveur pendant le traitement de cette requête."},
        )
    return JSONResponse(status_code=500, content={"detail": "Erreur interne du serveur."})

app.add_middleware(GZipMiddleware, minimum_size=500)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # à restreindre au domaine réel en prod
    allow_methods=["GET", "POST"],
)

@app.get("/api/analyse/indicateurs")
async def api_analyse_indicateurs(
    region: str = Query("Occitanie")
):
    """
    Retourne les indicateurs dynamiques de l'observatoire
    du ciné-tourisme pour la région demandée.

    Les indicateurs sont recalculés à partir des données
    actuellement présentes en base :
    - lieux de tournage
    - films
    - équipements touristiques
    - temps d'accès
    - isochrones IGN pré-calculés
    """
    try:
        return await construire_indicateurs_cinetourisme(region)
    except Exception as exc:
        logger.exception(
            "Erreur lors du calcul des indicateurs ciné-tourisme"
        )

        raise HTTPException(
            status_code=500,
            detail=f"Erreur lors du calcul des indicateurs : {exc}"
        )


@app.get("/api/analyse/observatoire")
async def api_analyse_observatoire(
    region: str = Query("Occitanie")
):
    """
    Dictionnaire statistique complet (offre, proximité, carence,
    diversité fonctionnelle, disparités départementales) par catégorie
    DATAtourisme. Endpoint séparé de /api/analyse/indicateurs pour ne
    pas alourdir son cycle de rafraîchissement principal : ce bloc fait
    davantage de requêtes SQL agrégées (percentiles par catégorie et
    par département) et change moins souvent que les KPI de tête de page.
    """
    try:
        return await construire_observatoire_statistique(region)
    except Exception as exc:
        logger.exception(
            "Erreur lors du calcul de l'observatoire statistique"
        )

        raise HTTPException(
            status_code=500,
            detail=f"Erreur lors du calcul de l'observatoire : {exc}"
        )
# ─────────────────────────────────────────────────────────────
# ISOCHRONES D'UN LIEU DE TOURNAGE
# ─────────────────────────────────────────────────────────────

@app.get("/api/lieux/{lieu_id}/isochrones")
async def isochrones_lieu(lieu_id: int):
    lieu = await fetch_one(
        """
        SELECT
            id,
            nom,
            latitude,
            longitude
        FROM lieux_tournage
        WHERE id = %s
        """,
        (lieu_id,),
    )

    if not lieu:
        raise HTTPException(
            status_code=404,
            detail="Lieu introuvable",
        )

    rows = await fetch_all(
        """
        SELECT
            mode,
            minutes,
            geometry_geojson,
            provider,
            calculated_at
        FROM isochrones
        WHERE lieu_tournage_id = %s
        ORDER BY mode, minutes
        """,
        (lieu_id,),
    )

    voiture = {}
    pied = {}

    for row in rows:
        geometry = row["geometry_geojson"]

        if isinstance(geometry, str):
            try:
                geometry = json.loads(geometry)
            except json.JSONDecodeError:
                continue

        element = {
            "minutes": row["minutes"],
            "geometry": geometry,
            "provider": row["provider"],
            "calculated_at": (
                row["calculated_at"].isoformat()
                if row["calculated_at"]
                else None
            ),
        }

        if row["mode"] == "driving-car":
            voiture[str(row["minutes"])] = element

        elif row["mode"] == "foot-walking":
            pied[str(row["minutes"])] = element

    return {
        "lieu": lieu,
        "isochrones": {
            "voiture": voiture,
            "pied": pied,
        },
    }


@app.middleware("http")
async def _gerer_cache_api(request: Request, call_next):
    response = await call_next(request)

    if not request.url.path.startswith("/api/"):
        return response

    

    ROUTES_CACHABLES_1H = (
        "/api/lieux/",
        "/api/films/",
    )

    if (
        any(
            request.url.path.startswith(p)
            for p in ROUTES_CACHABLES_1H
        )
        and "/amenities" in request.url.path
    ):
        response.headers["Cache-Control"] = (
            "public, max-age=3600, s-maxage=3600"
        )
    else:
        response.headers["Cache-Control"] = (
            "no-store, no-cache, must-revalidate"
        )
        response.headers["CDN-Cache-Control"] = "no-store"

    return response



# ── Liste des films (barre latérale) ─────────────────────────────
@app.get("/api/films")
async def liste_films(
    region: str = Query("Occitanie"),
    media_type: str | None = Query(None, description="movie, tv ou anime"),
    annee: int | None = Query(None),
    departement: str | None = Query(None),
    commune: str | None = Query(None),
    nationalite: str | None = Query(None),
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
    if nationalite:
        conditions.append("f.nationalite = %s")
        params.append(nationalite)
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
               f.popularite, f.i18n, COUNT(lt.id) AS nb_lieux
        FROM films f
        LEFT JOIN lieux_tournage lt ON lt.film_id = f.id
        WHERE {where}
        GROUP BY f.id
        ORDER BY {ordre_sql}
        LIMIT %s OFFSET %s
        """,
        (*params, par_page, offset),
    )
    for f in films:
        f["i18n"] = _parser_json(f.get("i18n"))
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
    nationalites = await fetch_all(
        """
        SELECT DISTINCT nationalite FROM films
        WHERE region = %s AND statut = 'publie' AND nationalite IS NOT NULL
        ORDER BY nationalite ASC
        """,
        (region,),
    )
    return {
        "annees": [a["annee"] for a in annees],
        "departements": [d["departement"] for d in departements],
        "communes": [c["commune"] for c in communes],
        "nationalites": [n["nationalite"] for n in nationalites],
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
    expliquer et à faire évoluer avec l'Agence Unique. Les chiffres
    réels sont cités dans le texte, pas juste une étiquette qualitative.
    """
    nb_lieux = d["nb_lieux"] or 0
    moy_heberg = d["moy_hebergement"] or 0
    moy_resto = d["moy_restaurant"] or 0
    lieux_isoles = d["lieux_sans_hebergement_15km"] or 0
    part_isoles = round(100 * lieux_isoles / nb_lieux) if nb_lieux else 0

    if nb_lieux == 0:
        return "Aucune donnée suffisante pour ce département."
    if moy_heberg >= 3 and moy_resto >= 3:
        return (
            f"Avec {moy_heberg} hébergements et {moy_resto} restaurants en moyenne à proximité des "
            f"{nb_lieux} lieux recensés, ce département est bien équipé pour une valorisation "
            f"touristique immédiate (circuit ciné-touristique, signalétique) sans investissement préalable."
        )
    if part_isoles > 50:
        return (
            f"{part_isoles}% des {nb_lieux} lieux recensés ({lieux_isoles} sur {nb_lieux}) n'ont "
            f"aucun hébergement à moins de 5 km — un aménagement (hébergement, signalétique, accès) "
            f"est nécessaire avant toute promotion touristique de ces sites."
        )
    if moy_heberg < 1:
        return (
            f"Avec seulement {moy_heberg} hébergement en moyenne à proximité des {nb_lieux} lieux, "
            f"le potentiel ciné-touristique existe mais nécessite des partenariats avec des "
            f"hébergeurs locaux avant une valorisation à grande échelle."
        )
    return (
        f"Équipement intermédiaire ({moy_heberg} hébergements, {moy_resto} restaurants en moyenne "
        f"pour {nb_lieux} lieux) — à évaluer au cas par cas selon les lieux les plus emblématiques."
    )


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


def _classer_accessibilite(duree_secondes: int | None) -> tuple[str, str]:
    """
    Seuils simples et défendables (pas de score composite opaque) :
    moins de 20 min en voiture = bien desservi, plus de 45 min = isolé.
    Retourne (étiquette, action recommandée).
    """
    if duree_secondes is None:
        return ("donnée manquante", "Lancer le précalcul des distances pour ce lieu (aucune donnée disponible actuellement).")
    minutes = duree_secondes / 60
    if minutes <= 20:
        return ("bien desservi", "Mettre ce lieu en avant dans la communication — l'expérience touristique y est déjà fluide, sans aménagement préalable nécessaire.")
    if minutes <= 45:
        return ("accessibilité modérée", "Signaler clairement les temps de trajet réels aux visiteurs avant leur venue, pour éviter une déception sur place.")
    return ("isolé", "Nécessite une action avant valorisation : partenariat avec un hébergeur/restaurateur plus proche, navette dédiée, ou signalétique renforcée sur les distances réelles.")


CATEGORIES_ANALYSE_ACCESSIBILITE = (
    "hebergement", "restaurant", "activite", "parking",
    "office_tourisme", "gare", "aeroport", "aerodrome",
)


@app.get("/api/analyse/accessibilite")
async def analyse_accessibilite(region: str = Query("Occitanie"), limite: int = Query(20)):
    """
    Pour les films/séries les plus connus (popularité TMDB), audit
    d'équipement touristique complet — pas seulement hébergement et
    restaurant, mais aussi activités, parking, office de tourisme,
    gare et aéroport/aérodrome : une lecture pensée comme un vrai
    diagnostic d'aménagement territorial, pas juste "y a-t-il un hôtel
    à côté".

    Toutes les données sont récupérées en 4 requêtes groupées (pas une
    par film) — l'ancienne version (~140 requêtes séquentielles pour
    20 films) provoquait des lenteurs, voire des échecs.

    Méthodologie pour un film à PLUSIEURS lieux : chaque catégorie de
    commodité est cherchée en tenant compte de TOUS les lieux du film
    à la fois (un touriste peut visiter n'importe lequel des lieux,
    donc on retient la meilleure option parmi tous). Le nombre de
    lieux du film est toujours indiqué pour que cette agrégation soit
    transparente plutôt qu'implicite.
    """
    try:
        films = await fetch_all(
            """
            SELECT id, titre, annee, media_type, poster_url, popularite, nationalite
            FROM films
            WHERE region = %s AND statut = 'publie' AND popularite IS NOT NULL
            ORDER BY popularite DESC
            LIMIT %s
            """,
            (region, limite),
        )
        if not films:
            return {"francais": [], "autres": []}
        film_ids = [f["id"] for f in films]

        lieux = await fetch_all(
            "SELECT id, film_id FROM lieux_tournage WHERE film_id = ANY(%s)", (film_ids,)
        )
        lieux_par_film: dict[int, list[int]] = {}
        for l in lieux:
            lieux_par_film.setdefault(l["film_id"], []).append(l["id"])
        tous_lieu_ids = [l["id"] for l in lieux]

        if not tous_lieu_ids:
            return {"francais": [], "autres": []}

        # Une seule requête pour TOUTES les commodités, tous films et
        # catégories confondus — regroupé en Python ensuite.
        toutes_commodites = await fetch_all(
            """
            SELECT lieu_tournage_id, categorie, nom, distance_voiture_metres,
                   duree_voiture_secondes, capacite
            FROM amenity_cache
            WHERE lieu_tournage_id = ANY(%s) AND categorie = ANY(%s)
                  AND duree_voiture_secondes IS NOT NULL
            ORDER BY duree_voiture_secondes ASC
            """,
            (tous_lieu_ids, list(CATEGORIES_ANALYSE_ACCESSIBILITE)),
        )
        toutes_stats = await fetch_all(
            """
            SELECT lieu_tournage_id, categorie, nombre_total
            FROM amenity_stats
            WHERE lieu_tournage_id = ANY(%s) AND categorie = ANY(%s)
            """,
            (tous_lieu_ids, list(CATEGORIES_ANALYSE_ACCESSIBILITE)),
        )

        # Index en mémoire : (lieu_id, categorie) → [commodités triées par durée]
        commodites_par_lieu_categorie: dict[tuple[int, str], list[dict]] = {}
        for c in toutes_commodites:
            cle = (c["lieu_tournage_id"], c["categorie"])
            commodites_par_lieu_categorie.setdefault(cle, []).append(c)

        stats_par_lieu_categorie: dict[tuple[int, str], int] = {}
        for s in toutes_stats:
            cle = (s["lieu_tournage_id"], s["categorie"])
            stats_par_lieu_categorie[cle] = (stats_par_lieu_categorie.get(cle, 0) or 0) + (s["nombre_total"] or 0)

        resultat = {"francais": [], "autres": []}

        for film in films:
            lieu_ids = lieux_par_film.get(film["id"], [])
            if not lieu_ids:
                continue

            categories_analysees = {}
            score_bien_dessservi = 0
            for categorie in CATEGORIES_ANALYSE_ACCESSIBILITE:
                candidats = []
                for lid in lieu_ids:
                    candidats.extend(commodites_par_lieu_categorie.get((lid, categorie), []))
                candidats.sort(key=lambda c: c["duree_voiture_secondes"])
                top3 = candidats[:3]

                nombre_total_rayon = sum(stats_par_lieu_categorie.get((lid, categorie), 0) for lid in lieu_ids)

                meilleur = top3[0] if top3 else None
                etiquette, action = _classer_accessibilite(
                    meilleur["duree_voiture_secondes"] if meilleur else None
                )
                if etiquette == "bien desservi":
                    score_bien_dessservi += 1

                categories_analysees[categorie] = {
                    "rayon_metres": RAYON_RECHERCHE_M.get(categorie),
                    "nombre_total_rayon": nombre_total_rayon,
                    "top_plus_proches": [
                        {
                            "nom": m["nom"],
                            "duree_minutes": round(m["duree_voiture_secondes"] / 60),
                            "distance_metres": m["distance_voiture_metres"],
                            "capacite": m["capacite"],
                        }
                        for m in top3
                    ],
                    "etiquette": etiquette,
                    "action": action,
                }

            entree = {
                "id": film["id"], "titre": film["titre"], "annee": film["annee"],
                "media_type": film["media_type"], "poster_url": film["poster_url"],
                "nombre_lieux": len(lieu_ids),
                "score_equipement": f"{score_bien_dessservi}/{len(CATEGORIES_ANALYSE_ACCESSIBILITE)}",
                "categories": categories_analysees,
            }

            # Risque de sur-fréquentation : popularité forte + équipement
            # faible = afflux à anticiper plutôt qu'à subir (leçon de
            # Dubrovnik/San Juan de Gaztelugatxe après Game of Thrones).
            ratio_equipement = score_bien_dessservi / len(CATEGORIES_ANALYSE_ACCESSIBILITE)
            if film["popularite"] >= 20 and ratio_equipement < 0.375:
                entree["risque_surfrequentation"] = "élevé"
                entree["action_surfrequentation"] = "Anticiper dès maintenant : renforcer l'offre locale (hébergement, transport, accès) avant qu'un afflux imprévu ne dégrade l'expérience et le site lui-même."
            elif film["popularite"] >= 10 and ratio_equipement < 0.5:
                entree["risque_surfrequentation"] = "modéré"
                entree["action_surfrequentation"] = "À surveiller : la notoriété du titre dépasse déjà légèrement la capacité d'accueil locale actuelle."
            else:
                entree["risque_surfrequentation"] = "faible"
                entree["action_surfrequentation"] = "Équipement cohérent avec la notoriété actuelle — pas d'action urgente."

            categorie_liste = "francais" if film["nationalite"] and "Français" in film["nationalite"] else "autres"
            resultat[categorie_liste].append(entree)

        return resultat
    except Exception as e:
        # Ne JAMAIS renvoyer null silencieusement — au moins un message
        # d'erreur exploitable, visible dans l'onglet Réseau du navigateur,
        # plutôt qu'un échec invisible.
        import traceback
        print(f"❌ /api/analyse/accessibilite a échoué : {e}", flush=True)
        traceback.print_exc()
        return JSONResponse(
            status_code=500,
            content={"erreur": str(e), "francais": [], "autres": []},
        )


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
            ) AS lieux_sans_hebergement_15km
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
    total_lieux_region = sum(d["nb_lieux"] for d in par_departement) or 1
    for d in par_departement:
        d = dict(d)
        d["part_pourcentage"] = round(100 * d["nb_lieux"] / total_lieux_region, 1)
        d["recommandation"] = _recommandation_departement(d)
        resultat.append(d)

    # Films les plus reconnus (popularité TMDB) — pour la section dédiée
    # de la page d'analyse, distincte de la comparaison territoriale.
    films_notables = await fetch_all(
        """
        SELECT id, titre, annee, media_type, poster_url, popularite
        FROM films
        WHERE region = %s AND statut = 'publie' AND popularite IS NOT NULL
        ORDER BY popularite DESC
        LIMIT 10
        """,
        (region,),
    )

    # Synthèse comparative chiffrée : pourquoi le 1er département est
    # plus sollicité que le dernier, en s'appuyant uniquement sur des
    # chiffres déjà présents en base (pas d'affirmation non vérifiable).
    synthese_comparative = None
    if len(resultat) >= 2:
        premier, dernier = resultat[0], resultat[-1]
        ratio = round(premier["nb_lieux"] / dernier["nb_lieux"], 1) if dernier["nb_lieux"] else None
        synthese_comparative = (
            f"{premier['departement']} concentre {premier['nb_lieux']} lieux de tournage recensés "
            f"({premier['part_pourcentage']}% du total régional), contre seulement {dernier['nb_lieux']} "
            f"pour {dernier['departement']} ({dernier['part_pourcentage']}%)"
            + (f" — soit {ratio} fois plus de lieux." if ratio else ".")
        )

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

    return {
        "par_departement": resultat,
        "completude": completude,
        "films_notables": films_notables,
        "synthese_comparative": synthese_comparative,
    }


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
    lien_general = data.get("link")  # page TMDB "où regarder" pour ce film — repli tant qu'il n'y a pas de vrai lien d'affiliation
    for categorie in ("flatrate", "rent", "buy"):
        for p in data.get(categorie, []):
            plateformes.append({
                "nom": p["provider_name"],
                "logo_url": f"https://image.tmdb.org/t/p/w92{p['logo_path']}",
                "type": {"flatrate": "streaming", "rent": "location", "buy": "achat"}[categorie],
                "lien_affilie": "",  # à remplir : lien Awin/partenaire pour ce provider
                "lien_repli": lien_general,  # utilisé tant que lien_affilie est vide
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


def _parser_json(valeur):
    """asyncpg renvoie JSONB comme une chaîne brute par défaut (pas de
    codec enregistré) — on la parse nous-mêmes avant de la renvoyer,
    sinon le frontend recevrait une chaîne au lieu d'un objet."""
    if not valeur:
        return None
    if isinstance(valeur, str):
        try:
            return json.loads(valeur)
        except (json.JSONDecodeError, TypeError):
            return None
    return valeur


@app.get("/api/films/{film_id}")
async def detail_film(film_id: int):
    film = await fetch_one(
        "SELECT * FROM films WHERE id = %s AND statut = 'publie'", (film_id,)
    )
    if not film:
        raise HTTPException(404, "Film introuvable")
    film["i18n"] = _parser_json(film.get("i18n"))

    lieux = await fetch_all(
        """
        SELECT id, nom, description, commune, departement,
               latitude, longitude, photo_url, anecdote, source_anecdote, description_wikipedia, i18n
        FROM lieux_tournage
        WHERE film_id = %s
        """,
        (film_id,),
    )
    for l in lieux:
        l["i18n"] = _parser_json(l.get("i18n"))

    if lieux:
        medias = await fetch_all(
            """
            SELECT lieu_tournage_id, type_media, url, legende, source
            FROM lieu_medias WHERE lieu_tournage_id = ANY(%s) ORDER BY ordre
            """,
            ([l["id"] for l in lieux],),
        )
        medias_par_lieu: dict[int, list[dict]] = {}
        for m in medias:
            medias_par_lieu.setdefault(m["lieu_tournage_id"], []).append(m)
        for l in lieux:
            l["medias"] = medias_par_lieu.get(l["id"], [])

    plateformes = await _plateformes_streaming(film)
    return {"film": film, "lieux": lieux, "plateformes": plateformes}


# ── Amenities proches d'un lieu (appelé au clic sur l'icône) ─────
def _formater_distance(m: int) -> str:
    return f"{m} m" if m < 1000 else f"{m / 1000:.1f}".replace(".0", "") + " km"


def _formater_duree(secondes: int) -> str:
    minutes = round(secondes / 60)
    if minutes < 60:
        return f"{minutes} min"
    h, reste = divmod(minutes, 60)
    return f"{h}h{reste:02d}" if reste else f"{h}h"


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
               adresse, telephone, email, site_web, horaires, photo_url, rang,
               tarif_min, tarif_max, devise, equipements, capacite,
               note_etoiles, labels_qualite, lien_accessibilite, langues_parlees, description,
               moyens_paiement, note_tarif,
               distance_pied_metres, duree_pied_secondes,
               distance_voiture_metres, duree_voiture_secondes
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

    # Deux phrases par catégorie (à pied / en voiture), basées sur les
    # distances précalculées — jamais de vol d'oiseau, jamais d'appel
    # OSRM en direct ici (tout vient déjà de amenity_cache).
    phrases = {}
    for categorie, items in par_categorie.items():
        label = _LABELS_CATEGORIE.get(categorie, categorie)
        stat = stats_par_categorie.get(categorie)
        total = stat["nombre_total"] if stat else len(items)
        rayon_km = (stat["rayon_metres"] // 1000) if stat else None

        for mode, cle_distance, cle_duree, verbe in (
            ("pied", "distance_pied_metres", "duree_pied_secondes", "à pied"),
            ("voiture", "distance_voiture_metres", "duree_voiture_secondes", "en voiture"),
        ):
            candidats = [i for i in items if i.get(cle_distance) is not None]
            if not candidats:
                continue
            meilleur = min(candidats, key=lambda i: i[cle_distance])
            phrase = (
                f"{label} « {meilleur['nom']} » est situé à {_formater_distance(meilleur[cle_distance])} {verbe} "
                f"du lieu de tournage, soit environ {_formater_duree(meilleur[cle_duree])} de trajet. "
                f"C'est {label.lower()} le plus proche {verbe} parmi les {total} recensés"
                + (f" dans un rayon de {rayon_km} km." if rayon_km else ".")
            )
            phrases.setdefault(categorie, {})[mode] = {
                "texte": phrase, "nom": meilleur["nom"],
                "distance_metres": meilleur[cle_distance], "duree_secondes": meilleur[cle_duree],
            }

    return {
        "lieu": lieu,
        "amenities": par_categorie,
        "stats": stats_par_categorie,
        "phrases_pied_voiture": phrases,
        "icones_categorie": ICONES_CATEGORIE,
    }



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
async def api_itineraire(
    depart_lat: float,
    depart_lon: float,
    arrivee_lat: float,
    arrivee_lon: float,
    mode: str = "pedestrian",
    etapes: bool = True,
):
    """
    Calcule un itinéraire réel via la Géoplateforme IGN.

    Architecture :

        Client
           ↓
        FastAPI
           ↓
        Redis / Upstash
           ↓
        IGN Géoplateforme uniquement en cas de cache miss

    Le cache Redis est utilisé uniquement pour les itinéraires
    dynamiques des utilisateurs.

    Les données d'accessibilité aux équipements touristiques
    restent gérées séparément par amenity_cache/PostgreSQL.
    """

    # ─────────────────────────────────────────────────────────
    # 0. VALIDATION DES PARAMÈTRES
    # ─────────────────────────────────────────────────────────

    modes_acceptes = {
        "pedestrian",
        "car",
    }

    if mode not in modes_acceptes:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "INVALID_MODE",
                "message": (
                    "Le mode doit être 'pedestrian' "
                    "ou 'car'."
                ),
            },
        )

    # Vérification basique des coordonnées.
    if not (
        -90 <= depart_lat <= 90
        and -180 <= depart_lon <= 180
        and -90 <= arrivee_lat <= 90
        and -180 <= arrivee_lon <= 180
    ):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "INVALID_COORDINATES",
                "message": "Les coordonnées GPS sont invalides.",
            },
        )

    # Évite de demander un itinéraire vers exactement le
    # même point.
    if (
        abs(depart_lat - arrivee_lat) < 0.000001
        and abs(depart_lon - arrivee_lon) < 0.000001
    ):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "SAME_ORIGIN_DESTINATION",
                "message": (
                    "Le point de départ et le point d'arrivée "
                    "sont identiques."
                ),
            },
        )

    # ─────────────────────────────────────────────────────────
    # 1. CACHE NAVIGATION
    # ─────────────────────────────────────────────────────────

    # Si Upstash Redis est configuré, on construit une clé
    # normalisée.
    #
    # IMPORTANT :
    # navigation_cache.enabled évite de faire attendre
    # inutilement l'utilisateur lorsque Redis n'est pas
    # configuré ou temporairement indisponible.

    cache_key = navigation_cache.construire_cle(
        depart_lat=depart_lat,
        depart_lon=depart_lon,
        arrivee_lat=arrivee_lat,
        arrivee_lon=arrivee_lon,
        mode=mode,
        etapes=etapes,
    )

    lock_key = f"{cache_key}:lock"

    # ─────────────────────────────────────────────────────────
    # 2. CACHE HIT
    # ─────────────────────────────────────────────────────────

    if navigation_cache.enabled:
        resultat = await navigation_cache.get(cache_key)

        if resultat:
            resultat["cache"] = "redis"
            resultat["cache_hit"] = True

            return resultat

    # ─────────────────────────────────────────────────────────
    # 3. CACHE MISS → VERROU ANTI-RAFALE
    # ─────────────────────────────────────────────────────────

    if navigation_cache.enabled:

        verrou_obtenu = await navigation_cache.acquire_lock(
            lock_key
        )

        if not verrou_obtenu:

            # Une autre requête est déjà en train de calculer
            # exactement le même itinéraire.
            #
            # On attend son résultat plutôt que de lancer
            # plusieurs appels identiques vers IGN.

            resultat = await navigation_cache.wait_for_result(
                cache_key
            )

            if resultat:
                resultat["cache"] = "redis"
                resultat["cache_hit"] = True
                resultat["cache_deduplicated"] = True

                return resultat

            # Si aucun résultat n'est apparu après l'attente,
            # on tente de reprendre le verrou.
            #
            # Cela évite autant que possible que plusieurs
            # requêtes frappent IGN simultanément.

            verrou_obtenu = await navigation_cache.acquire_lock(
                lock_key
            )

            # Si le verrou est toujours occupé, on continue
            # exceptionnellement vers IGN.
            #
            # Le verrou possède un TTL automatique et ne peut
            # donc pas rester bloqué définitivement.

    # ─────────────────────────────────────────────────────────
    # 4. APPEL IGN
    # ─────────────────────────────────────────────────────────

    try:

        resultat = await calculer_itineraire_geoplateforme(
            depart_lat=depart_lat,
            depart_lon=depart_lon,
            arrivee_lat=arrivee_lat,
            arrivee_lon=arrivee_lon,
            mode=mode,
            avec_etapes=etapes,
        )

    except GeoplateformeError as exc:

        print(
            "❌ Géoplateforme indisponible pour "
            f"l'itinéraire : {exc}",
            flush=True,
        )

        raise HTTPException(
            status_code=503,
            detail={
                "code": "GEOPLATEFORME_UNAVAILABLE",
                "message": (
                    "Le service d'itinéraire IGN est "
                    "temporairement indisponible. Aucun "
                    "itinéraire de substitution n'a été utilisé."
                ),
                "provider": "geoplateforme",
                "resource": RESOURCE_ITINERAIRE,
            },
        ) from exc

    except Exception as exc:

        print(
            "❌ Erreur inattendue calcul itinéraire : "
            f"{exc}",
            flush=True,
        )

        raise HTTPException(
            status_code=500,
            detail={
                "code": "ITINERARY_ERROR",
                "message": (
                    "Une erreur inattendue est survenue "
                    "pendant le calcul de l'itinéraire."
                ),
                "provider": "geoplateforme",
                "resource": RESOURCE_ITINERAIRE,
            },
        ) from exc

    # ─────────────────────────────────────────────────────────
    # 5. VÉRIFICATION DE LA RÉPONSE IGN
    # ─────────────────────────────────────────────────────────

    if not resultat or not isinstance(resultat, dict):

        raise HTTPException(
            status_code=502,
            detail={
                "code": "INVALID_GEOPLATEFORME_RESPONSE",
                "message": (
                    "La Géoplateforme a répondu mais le "
                    "résultat d'itinéraire est invalide."
                ),
                "provider": "geoplateforme",
                "resource": RESOURCE_ITINERAIRE,
            },
        )

    geometry = resultat.get("geometry")

    if not geometry:

        raise HTTPException(
            status_code=502,
            detail={
                "code": "NO_ROUTE_GEOMETRY",
                "message": (
                    "La Géoplateforme n'a retourné aucune "
                    "géométrie d'itinéraire exploitable."
                ),
                "provider": "geoplateforme",
                "resource": RESOURCE_ITINERAIRE,
            },
        )

    # ─────────────────────────────────────────────────────────
    # 6. FORMAT STANDARD POUR LE FRONTEND
    # ─────────────────────────────────────────────────────────

    resultat["type"] = "route_reelle"

    resultat["provider"] = "geoplateforme"

    resultat["resource"] = RESOURCE_ITINERAIRE

    resultat["mode"] = mode

    # Le frontend de navigation utilise cette propriété.

    if etapes:

        resultat["etapes_navigation"] = (
            resultat.get("etapes_navigation")
            or resultat.get("etapes")
            or []
        )

    else:

        resultat["etapes_navigation"] = []

    # ─────────────────────────────────────────────────────────
    # 7. INFORMATIONS DE DIAGNOSTIC
    # ─────────────────────────────────────────────────────────

    resultat["cache"] = (
        "redis"
        if navigation_cache.enabled
        else "disabled"
    )

    resultat["cache_hit"] = False

    # ─────────────────────────────────────────────────────────
    # 8. ÉCRITURE DANS REDIS
    # ─────────────────────────────────────────────────────────

    if navigation_cache.enabled:

        await navigation_cache.set(
            cache_key,
            resultat,
        )

    # ─────────────────────────────────────────────────────────
    # 9. RÉPONSE
    # ─────────────────────────────────────────────────────────

    return resultat   


async def _ordre_optimise(lieux: list[dict]) -> list[dict]:
    """
    Optimise l'ordre d'un petit circuit sans utiliser OSRM.

    Pour rester léger côté API, on utilise une heuristique de plus
    proche voisin basée sur la distance géographique.

    Les trajets réels entre les lieux sont ensuite calculés par
    Géoplateforme IGN dans _itineraire_multi_etapes().
    """

    return _ordre_plus_proche_voisin(lieux)





def _adresse_complete(lieu: dict) -> str:
    return ", ".join(p for p in (lieu["nom"], lieu.get("commune"), lieu.get("departement")) if p)



def _profil_score_offre(item, categorie, budget_level="equilibre", accessibilite=False):
    """Score déterministe d'une commodité pour une personnalisation légère.

    Le score ne prétend pas être une note de qualité universelle : il sert à
    ordonner les offres en fonction des préférences choisies par l'utilisateur.
    """
    score = 0.0
    distance = float(item.get("meilleure_distance_metres") or item.get("distance_metres") or 999999)
    # Proximité : elle pèse davantage pour un parcours à pied.
    score += max(0.0, 38.0 - distance / 180.0)

    note = item.get("note_etoiles")
    try:
        note = float(note) if note is not None else None
    except (TypeError, ValueError):
        note = None
    if note is not None:
        score += min(25.0, max(0.0, note * 5.0))

    tarif = item.get("tarif_min")
    try:
        tarif = float(tarif) if tarif is not None else None
    except (TypeError, ValueError):
        tarif = None
    if tarif is not None:
        if budget_level == "economique":
            score += max(0.0, 28.0 - min(tarif, 100.0) * 0.35)
        elif budget_level == "confort":
            score += min(18.0, tarif * 0.18)
        else:
            score += max(0.0, 16.0 - min(tarif, 100.0) * 0.12)

    texte_access = " ".join(str(item.get(k) or "") for k in ("equipements", "labels_qualite", "description", "lien_accessibilite")).lower()
    accessible = bool(item.get("lien_accessibilite")) or any(x in texte_access for x in ("pmr", "accessible", "accessibilité", "handicap"))
    if accessibilite:
        score += 28.0 if accessible else -28.0
    elif accessible:
        score += 3.0

    # Petit bonus si une fiche web exploitable est disponible.
    if item.get("site_web"):
        score += 4.0
    if item.get("telephone"):
        score += 2.0
    return round(score, 2)


def _phrase_recommandation_offre(item, categorie, profil, etape_nom):
    nom = item.get("nom") or "cet établissement"
    distance = item.get("meilleure_distance_metres")
    distance_txt = f"à {round(float(distance))} m" if distance is not None and float(distance) < 1000 else (f"à {float(distance)/1000:.1f} km" if distance is not None else "à proximité")
    raisons = [distance_txt]
    if profil == "economique" and item.get("tarif_min") is not None:
        raisons.append("un positionnement tarifaire intéressant")
    if profil == "confort" and item.get("note_etoiles") is not None:
        raisons.append("un niveau de confort renseigné")
    if item.get("note_etoiles") is not None:
        raisons.append(f"{item['note_etoiles']} étoile(s) renseignée(s)")
    return f"Pour l’étape « {etape_nom} », {nom} est une suggestion pertinente pour votre parcours : {', '.join(raisons)}."


def _optimiser_etapes_approx(etapes, depart, mode, temps_disponible_minutes, temps_visite_minutes, retour_depart):
    """Pré-sélection rapide avant les appels IGN.

    On évite une explosion du nombre d'appels réseau : l'heuristique utilise
    les distances géographiques puis l'itinéraire IGN est recalculé ensuite.
    """
    if not temps_disponible_minutes or len(etapes) <= 1:
        return list(etapes), []
    vitesse_kmh = 45.0 if mode == "driving-car" else 4.5
    facteur_route = 1.30 if mode == "driving-car" else 1.15
    budget_h = max(0.25, float(temps_disponible_minutes) / 60.0)
    temps_visites_h = len(etapes) * float(temps_visite_minutes) / 60.0
    temps_deplacement_h = max(0.0, budget_h - temps_visites_h)
    if temps_deplacement_h <= 0:
        return ([etapes[0]] if etapes else []), [x for x in etapes[1:]]

    points = []
    if depart:
        points.append(depart)
    else:
        points.append(etapes[0])
    restants = list(etapes)
    # Si le départ est déjà une étape, elle doit rester la première.
    if not depart and restants:
        choisi = [restants.pop(0)]
    else:
        choisi = []

    distance_estimee = 0.0
    courant = points[0]
    while restants:
        candidat = min(restants, key=lambda x: haversine_metres(float(courant["latitude"]), float(courant["longitude"]), float(x["latitude"]), float(x["longitude"])))
        d = haversine_metres(float(courant["latitude"]), float(courant["longitude"]), float(candidat["latitude"]), float(candidat["longitude"]))
        prochaine_distance = distance_estimee + d * facteur_route
        retour = 0.0
        if retour_depart:
            base = depart if depart else (choisi[0] if choisi else etapes[0])
            retour = haversine_metres(float(candidat["latitude"]), float(candidat["longitude"]), float(base["latitude"]), float(base["longitude"])) * facteur_route
        heures = (prochaine_distance + retour) / 1000.0 / vitesse_kmh
        visites = (len(choisi) + 1) * float(temps_visite_minutes) / 60.0
        if heures + visites > budget_h and choisi:
            break
        choisi.append(candidat)
        restants.remove(candidat)
        distance_estimee = prochaine_distance
        courant = candidat

    ids_choisis = {int(x["id"]) for x in choisi if x.get("id") is not None}
    exclus = [x for x in etapes if x.get("id") is not None and int(x["id"]) not in ids_choisis]
    return choisi, exclus


async def _itineraire_multi_etapes(lieux_ordonnes, mode):
    """
    Calcule un itinéraire réel entre plusieurs lieux avec
    la Géoplateforme IGN.

    La Géoplateforme est l'unique fournisseur d'itinéraire.

    Aucun fallback :
    - pas d'OSRM public
    - pas d'OpenRouteService
    - pas de ligne droite

    Si un seul tronçon échoue, l'itinéraire complet est considéré
    comme indisponible.
    """

    if not lieux_ordonnes or len(lieux_ordonnes) < 2:
        raise GeoplateformeError(
            "Au moins deux lieux sont nécessaires pour calculer "
            "un itinéraire."
        )

    geometries = []
    trajets = []

    distance_totale = 0.0
    duree_totale = 0.0
    duree_disponible = True

    nb_troncons = len(lieux_ordonnes) - 1

    for i in range(nb_troncons):

        depart = lieux_ordonnes[i]
        arrivee = lieux_ordonnes[i + 1]

        depart_lat = float(depart["latitude"])
        depart_lon = float(depart["longitude"])

        arrivee_lat = float(arrivee["latitude"])
        arrivee_lon = float(arrivee["longitude"])

        try:
            resultat = await calculer_itineraire_geoplateforme(
                depart_lat=depart_lat,
                depart_lon=depart_lon,
                arrivee_lat=arrivee_lat,
                arrivee_lon=arrivee_lon,
                mode=mode,
                avec_etapes=False,
            )

        except GeoplateformeError as exc:
            raise GeoplateformeError(
                f"Échec du tronçon {i + 1}/{nb_troncons} "
                f"avec la Géoplateforme IGN : {exc}"
            ) from exc

        except Exception as exc:
            raise GeoplateformeError(
                f"Erreur inattendue sur le tronçon "
                f"{i + 1}/{nb_troncons} : {exc}"
            ) from exc

        # IMPORTANT :
        # Aucun fallback géométrique.
        if not resultat:
            raise GeoplateformeError(
                f"La Géoplateforme IGN n'a retourné aucun itinéraire "
                f"pour le tronçon {i + 1}/{nb_troncons}."
            )

        geometry = resultat.get("geometry")

        if not geometry:
            raise GeoplateformeError(
                f"La Géoplateforme IGN n'a retourné aucune géométrie "
                f"pour le tronçon {i + 1}/{nb_troncons}."
            )

        distance = resultat.get("distance_metres", 0)
        duree = resultat.get("duree_secondes")

        try:
            distance = float(distance or 0)
        except (TypeError, ValueError):
            distance = 0.0

        if duree is not None:
            try:
                duree = float(duree)
            except (TypeError, ValueError):
                duree = None

        if duree is None:
            duree_disponible = False
        else:
            duree_totale += duree

        distance_totale += distance

        geometries.append(geometry)

        trajets.append({
            "depart": {
                "id": depart.get("id"),
                "nom": depart.get("nom"),
                "latitude": depart_lat,
                "longitude": depart_lon,
            },
            "arrivee": {
                "id": arrivee.get("id"),
                "nom": arrivee.get("nom"),
                "latitude": arrivee_lat,
                "longitude": arrivee_lon,
            },
            "distance_metres": round(distance),
            "duree_secondes": (
                round(duree)
                if duree is not None
                else None
            ),
            "geometry": geometry,
            "provider": "geoplateforme",
            "resource": RESOURCE_ITINERAIRE
                if "RESOURCE_ITINERAIRE" in globals()
                else "bdtopo-osrm",
        })

    # ---------------------------------------------------------
    # Fusion des géométries
    # ---------------------------------------------------------

    coordinates = []

    for geometry in geometries:

        geometry_type = geometry.get("type")
        geometry_coordinates = geometry.get("coordinates", [])

        if geometry_type == "LineString":

            if not geometry_coordinates:
                continue

            if not coordinates:
                coordinates.extend(geometry_coordinates)

            elif coordinates[-1] == geometry_coordinates[0]:
                coordinates.extend(geometry_coordinates[1:])

            else:
                coordinates.extend(geometry_coordinates)

        elif geometry_type == "MultiLineString":

            for line in geometry_coordinates:

                if not line:
                    continue

                if not coordinates:
                    coordinates.extend(line)

                elif coordinates[-1] == line[0]:
                    coordinates.extend(line[1:])

                else:
                    coordinates.extend(line)

        else:
            raise GeoplateformeError(
                f"Type de géométrie IGN non supporté : "
                f"{geometry_type}"
            )

    if len(coordinates) < 2:
        raise GeoplateformeError(
            "La Géoplateforme IGN a retourné une géométrie "
            "insuffisante pour construire l'itinéraire."
        )

    return {
        "type": "route_reelle",
        "provider": "geoplateforme",
        "resource": "bdtopo-osrm",
        "mode": mode,
        "distance_metres": round(distance_totale),
        "duree_secondes": (
            round(duree_totale)
            if duree_disponible
            else None
        ),
        "geometry": {
            "type": "LineString",
            "coordinates": coordinates,
        },
        "trajets": trajets,
        "nb_troncons": nb_troncons,
    }



@app.get("/api/geocodage")
async def api_geocodage(q: str = Query(..., min_length=3, max_length=200)):
    """Recherche d'adresse via le service de géocodage IGN.

    Le navigateur ne contacte pas directement le service IGN : le backend
    joue le rôle de proxy afin de garder une intégration homogène avec Pelify.
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                "https://data.geopf.fr/geocodage/search",
                params={"q": q, "limit": 5, "autocomplete": "true"},
                headers={"Accept": "application/json"},
            )
        response.raise_for_status()
        data = response.json()
    except Exception as exc:
        logger.warning("Géocodage IGN indisponible: %s", exc)
        raise HTTPException(502, "Le service de recherche d'adresse IGN est momentanément indisponible.") from exc

    features = data.get("features", []) if isinstance(data, dict) else []
    resultats = []
    for feature in features[:5]:
        props = feature.get("properties") or {}
        geometry = feature.get("geometry") or {}
        coords = geometry.get("coordinates") or []
        if len(coords) < 2:
            continue
        resultats.append({
            "label": props.get("label") or props.get("name") or "Adresse",
            "latitude": float(coords[1]),
            "longitude": float(coords[0]),
            "type": props.get("type"),
            "idban": props.get("idban") or props.get("id"),
            "source": "BAN via Géoplateforme",
        })
    return {"resultats": resultats}


async def _itineraire_depuis_point(lieu_depart, etapes, mode, retour_depart=False):
    """Construit un parcours IGN depuis un point arbitraire puis les étapes.

    lieu_depart doit contenir latitude/longitude et peut avoir id=None.
    """
    if not etapes:
        raise GeoplateformeError("Aucune étape sélectionnée.")

    segments = []
    distance_totale = 0
    duree_totale = 0
    duree_disponible = True

    points = [lieu_depart] + list(etapes)
    if retour_depart and len(etapes) >= 1:
        points.append(lieu_depart)

    for i in range(len(points) - 1):
        depart = points[i]
        arrivee = points[i + 1]
        resultat = await calculer_itineraire_geoplateforme(
            depart_lat=float(depart["latitude"]),
            depart_lon=float(depart["longitude"]),
            arrivee_lat=float(arrivee["latitude"]),
            arrivee_lon=float(arrivee["longitude"]),
            mode=mode,
            avec_etapes=False,
        )
        if not resultat or not resultat.get("geometry"):
            raise GeoplateformeError(f"Aucune géométrie IGN pour le tronçon {i + 1}.")
        distance = float(resultat.get("distance_metres") or 0)
        duree = resultat.get("duree_secondes")
        if duree is None:
            duree_disponible = False
        else:
            duree_totale += float(duree)
        distance_totale += distance
        segments.append({
            "depart": {"id": depart.get("id"), "nom": depart.get("nom") or "Point de départ", "latitude": float(depart["latitude"]), "longitude": float(depart["longitude"])},
            "arrivee": {"id": arrivee.get("id"), "nom": arrivee.get("nom") or "Étape", "latitude": float(arrivee["latitude"]), "longitude": float(arrivee["longitude"])},
            "distance_metres": round(distance),
            "duree_secondes": round(float(duree)) if duree is not None else None,
            "geometry": resultat["geometry"],
        })

    coords=[]
    for segment in segments:
        g=segment["geometry"]
        lines = g.get("coordinates", []) if g.get("type") == "LineString" else [x for x in g.get("coordinates", [])]
        if g.get("type") == "LineString":
            lines=[lines]
        for line in lines:
            if not line: continue
            if not coords: coords.extend(line)
            elif coords[-1] == line[0]: coords.extend(line[1:])
            else: coords.extend(line)

    return {
        "type":"route_reelle",
        "provider":"geoplateforme",
        "resource":RESOURCE_ITINERAIRE,
        "mode":mode,
        "distance_metres":round(distance_totale),
        "duree_secondes":round(duree_totale) if duree_disponible else None,
        "geometry":{"type":"LineString","coordinates":coords},
        "trajets":segments,
        "retour_depart":bool(retour_depart),
    }


@app.get("/api/films/{film_id}/trace")
async def trace_film(film_id: int):
    """
    "Sur les traces de {film}" — relie tous les lieux de tournage d'un
    film en Occitanie par le trajet EN VOITURE le plus rapide (pas
    juste le plus proche voisin), calculé tronçon par tronçon pour
    rester fiable même avec beaucoup de lieux (voir
    _itineraire_multi_etapes pour le pourquoi).
    """
    lieux = await fetch_all(
        """SELECT l.id, l.nom, l.commune, l.departement, l.latitude, l.longitude,
                  l.film_id, f.titre AS film_titre, f.media_type, f.annee, f.poster_url
           FROM lieux_tournage l LEFT JOIN films f ON f.id = l.film_id
           WHERE l.film_id = %s""",
        (film_id,),
    )
    if len(lieux) < 2:
        raise HTTPException(400, "Ce film n'a qu'un seul lieu recensé — pas de tracé possible.")

    lieux_ordonnes = await _ordre_optimise(lieux)
    resultat = await _itineraire_multi_etapes(lieux_ordonnes, "driving-car")
    resultat["etapes"] = lieux_ordonnes
    resultat["adresses"] = [_adresse_complete(l) for l in lieux_ordonnes]
    return resultat


@app.post("/api/parcours/enrichi")
async def parcours_enrichi(request: Request, response: Response):
    """
    V4 — construit un parcours cinétouristique personnalisé à partir
    d'une sélection de lieux de tournage et renvoie les offres
    touristiques déjà présentes dans amenity_cache autour de chaque
    étape.

    Le calcul est volontairement basé sur les données déjà en cache :
    aucune requête Overpass/DATAtourisme en direct depuis une requête
    visiteur.

    Body JSON :
      {
        "lieu_ids": [1, 2, 3],
        "mode": "driving-car",
        "limite_par_categorie": 4
      }

    L'ordre reçu est conservé : c'est le choix de l'utilisateur.
    L'endpoint recalcule ensuite les tronçons routiers avec la
    Géoplateforme IGN et agrège hébergements, restaurants, activités,
    offices de tourisme et autres catégories disponibles dans le cache.
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "JSON invalide")

    lieu_ids = body.get("lieu_ids")
    mode = body.get("mode", "driving-car")
    limite = body.get("limite_par_categorie", 4)
    depart = body.get("depart") or None
    retour_depart = bool(body.get("retour_depart", False))
    temps_disponible_minutes = body.get("temps_disponible_minutes")
    temps_visite_minutes = body.get("temps_visite_minutes", 45)
    categories_interet = body.get("categories_interet") or []
    budget_level = str(body.get("budget_level") or "equilibre")
    accessibilite = bool(body.get("accessibilite", False))
    optimiser = bool(body.get("optimiser", False))
    inclure_visites_guidees = bool(body.get("inclure_visites_guidees", True))
    visites_guidees = body.get("visites_guidees") or []
    heure_depart = str(body.get("heure_depart") or "09:00")
    date_sortie = str(body.get("date_sortie") or "")
    budget_max_euros = body.get("budget_max_euros")

    if not isinstance(lieu_ids, list):
        raise HTTPException(400, "lieu_ids doit être une liste")

    try:
        lieu_ids = list(dict.fromkeys(int(x) for x in lieu_ids))
    except (TypeError, ValueError):
        raise HTTPException(400, "Les identifiants de lieux doivent être numériques")

    if not lieu_ids:
        raise HTTPException(400, "Sélectionnez au moins un lieu")
    if len(lieu_ids) > 30:
        raise HTTPException(400, "Un parcours ne peut pas contenir plus de 30 étapes")
    if mode not in {"driving-car", "foot-walking"}:
        raise HTTPException(400, "Mode invalide")

    try:
        temps_visite_minutes = max(0, min(int(temps_visite_minutes), 240))
    except (TypeError, ValueError):
        temps_visite_minutes = 45
    if temps_disponible_minutes not in (None, ""):
        try:
            temps_disponible_minutes = max(15, min(int(temps_disponible_minutes), 1440))
        except (TypeError, ValueError):
            raise HTTPException(400, "temps_disponible_minutes invalide")
    else:
        temps_disponible_minutes = None
    if not isinstance(categories_interet, list):
        categories_interet = []
    categories_interet = [str(x) for x in categories_interet]
    if budget_level not in {"economique", "equilibre", "confort"}:
        budget_level = "equilibre"
    if not isinstance(visites_guidees, list):
        visites_guidees = []
    visites_guidees_nettoyees = []
    if inclure_visites_guidees:
        for v in visites_guidees[:20]:
            try:
                film_id = int(v.get("film_id"))
                duree = max(1, min(int(v.get("duree_minutes")), 600))
            except (TypeError, ValueError, AttributeError):
                continue
            visites_guidees_nettoyees.append({"film_id": film_id, "nom": str(v.get("nom") or "Visite guidée"), "duree_minutes": duree, "lien": v.get("lien"), "heure_debut": v.get("heure_debut"), "heure_fin": v.get("heure_fin")})
    visites_guidees = visites_guidees_nettoyees

    try:
        limite = max(1, min(int(limite), 10))
    except (TypeError, ValueError):
        limite = 4

    import re as _re
    if not _re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", heure_depart):
        heure_depart = "09:00"
    if budget_max_euros not in (None, ""):
        try: budget_max_euros = max(0, float(budget_max_euros))
        except (TypeError, ValueError): budget_max_euros = None
    else: budget_max_euros = None

    placeholders = ",".join(["%s"] * len(lieu_ids))
    lieux = await fetch_all(
        f"""
        SELECT l.id, l.nom, l.commune, l.departement, l.latitude, l.longitude,
               l.film_id, f.titre AS film_titre, f.media_type, f.annee, f.poster_url
        FROM lieux_tournage l
        LEFT JOIN films f ON f.id = l.film_id
        WHERE l.id IN ({placeholders})
        """,
        tuple(lieu_ids),
    )
    par_id = {int(l["id"]): l for l in lieux}
    if len(par_id) != len(lieu_ids):
        manquants = [x for x in lieu_ids if x not in par_id]
        raise HTTPException(404, f"Lieu(x) introuvable(s) : {manquants}")

    film_ids_parcours = list({int(x["film_id"]) for x in lieux if x.get("film_id") is not None})
    visites_disponibles = creneaux_pour_date(date_sortie, film_ids_parcours) if date_sortie else []

    # On conserve exactement l'ordre choisi dans l'interface, sauf si
    # l'utilisateur demande explicitement une optimisation sous contrainte de temps.
    etapes = [par_id[x] for x in lieu_ids]
    etapes_originales = list(etapes)
    etapes_exclues_optimisation = []
    if optimiser:
        etapes, etapes_exclues_optimisation = _optimiser_etapes_approx(
            etapes, depart, mode, temps_disponible_minutes, temps_visite_minutes, retour_depart
        )
        if not etapes:
            raise HTTPException(400, "Aucune étape ne peut tenir dans les critères choisis.")

    # Les visites datées sont traitées comme de vraies contraintes horaires.
    # Si une visite est disponible à une heure précise et que l'optimisation est
    # demandée, on peut faire passer l'étape concernée en priorité.
    guide_planifie = None
    if inclure_visites_guidees and visites_disponibles and optimiser:
        for guide in visites_disponibles:
            for etape in etapes:
                if int(etape.get("film_id") or -1) in {int(x) for x in guide.get("film_ids", [])}:
                    guide_planifie = {**guide, "film_id": int(etape.get("film_id")), "lieu_id": int(etape["id"]), "etape_nom": etape.get("nom")}
                    break
            if guide_planifie:
                break
        if guide_planifie:
            cible = next((x for x in etapes if int(x["id"]) == guide_planifie["lieu_id"]), None)
            if cible and etapes and int(etapes[0]["id"]) != guide_planifie["lieu_id"]:
                autres = [x for x in etapes if int(x["id"]) != guide_planifie["lieu_id"]]
                ordonnes = [cible]
                courant = cible
                while autres:
                    suivant = min(autres, key=lambda x: haversine_metres(float(courant["latitude"]), float(courant["longitude"]), float(x["latitude"]), float(x["longitude"])))
                    ordonnes.append(suivant); autres.remove(suivant); courant = suivant
                etapes = ordonnes

    resultat_route = None
    depart_effectif = None
    if depart:
        try:
            depart_lat = float(depart.get("latitude"))
            depart_lon = float(depart.get("longitude"))
            if not (-90 <= depart_lat <= 90 and -180 <= depart_lon <= 180):
                raise ValueError
            depart_effectif = {"id": None, "nom": depart.get("nom") or "Point de départ", "latitude": depart_lat, "longitude": depart_lon}
        except (TypeError, ValueError, AttributeError):
            raise HTTPException(400, "Point de départ invalide")

    if depart_effectif:
        try:
            resultat_route = await _itineraire_depuis_point(depart_effectif, etapes, mode, retour_depart)
        except GeoplateformeError as exc:
            raise HTTPException(502, f"Itinéraire IGN indisponible : {exc}") from exc
    elif len(etapes) >= 2:
        try:
            resultat_route = await _itineraire_multi_etapes(etapes, mode)
        except GeoplateformeError as exc:
            raise HTTPException(502, f"Itinéraire IGN indisponible : {exc}") from exc

    # Les catégories sont celles réellement présentes dans amenity_cache.
    # Cela permet à V4 d'évoluer sans changer cet endpoint lorsque de
    # nouvelles catégories sont importées.
    rows = await fetch_all(
        f"""
        SELECT lieu_tournage_id, categorie, nom, latitude, longitude,
               distance_metres, adresse, telephone, email, site_web,
               horaires, photo_url, tarif_min, tarif_max, devise,
               equipements, capacite, note_etoiles, labels_qualite,
               lien_accessibilite, langues_parlees, description,
               moyens_paiement, note_tarif,
               distance_pied_metres, duree_pied_secondes,
               distance_voiture_metres, duree_voiture_secondes
        FROM amenity_cache
        WHERE lieu_tournage_id IN ({placeholders})
        ORDER BY lieu_tournage_id, categorie, distance_metres ASC
        """,
        tuple(lieu_ids),
    )

    # Agrégation globale sans doublons. Un même établissement peut être
    # proche de plusieurs étapes ; on le garde une seule fois dans la
    # synthèse globale et on conserve ses étapes de proximité.
    categories = {}
    vus = {}
    par_etape = {str(x): [] for x in lieu_ids}

    for row in rows:
        categorie = row["categorie"]
        item = dict(row)
        item["lieu_tournage_id"] = int(row["lieu_tournage_id"])
        par_etape[str(item["lieu_tournage_id"])].append(item)

        # Clé stable : osm_id n'est pas toujours disponible, donc on
        # utilise catégorie + coordonnées + nom normalisé.
        cle = (
            categorie,
            round(float(row["latitude"]), 5),
            round(float(row["longitude"]), 5),
            (row["nom"] or "").strip().lower(),
        )
        if cle not in vus:
            vus[cle] = {
                "item": item,
                "proche_de": [item["lieu_tournage_id"]],
                "meilleure_distance_metres": row["distance_metres"],
            }
        else:
            vus[cle]["proche_de"].append(item["lieu_tournage_id"])
            d = row["distance_metres"]
            if d is not None and (
                vus[cle]["meilleure_distance_metres"] is None
                or d < vus[cle]["meilleure_distance_metres"]
            ):
                vus[cle]["meilleure_distance_metres"] = d

    for valeur in vus.values():
        item = dict(valeur["item"])
        item["proche_de"] = valeur["proche_de"]
        item["meilleure_distance_metres"] = valeur["meilleure_distance_metres"]
        categories.setdefault(item["categorie"], []).append(item)

    if categories_interet:
        categories = {k: v for k, v in categories.items() if k in categories_interet}

    recommandations_par_etape = {}
    for categorie, items in categories.items():
        for item in items:
            item["score_personnalise"] = _profil_score_offre(item, categorie, budget_level, accessibilite)
        items.sort(key=lambda x: (x.get("score_personnalise", 0), -(x.get("meilleure_distance_metres") or 10**9)), reverse=True)
        categories[categorie] = items[:limite]

    # Recommandations individualisées : une proposition par étape et catégorie
    # utile, sans inventer de lien de réservation.
    for etape in etapes:
        eid = int(etape["id"])
        recommandations_par_etape[str(eid)] = []
        for categorie, items in categories.items():
            candidats = [x for x in par_etape.get(str(eid), []) if x.get("categorie") == categorie]
            if not candidats:
                continue
            for item in candidats:
                item["score_personnalise"] = _profil_score_offre(item, categorie, budget_level, accessibilite)
            meilleur = max(candidats, key=lambda x: x.get("score_personnalise", 0))
            rec = dict(meilleur)
            rec["raison"] = _phrase_recommandation_offre(rec, categorie, budget_level, etape.get("nom") or "cette étape")
            rec["action_url"] = rec.get("site_web") or None
            rec["action_label"] = "Voir / réserver" if rec.get("site_web") else ("Appeler" if rec.get("telephone") else None)
            recommandations_par_etape[str(eid)].append(rec)

    # Réduire également le détail par étape afin de ne pas envoyer une
    # réponse inutilement volumineuse au navigateur.
    for cle, items in par_etape.items():
        par_etape[cle] = items[:limite]

    duree_trajet = resultat_route.get("duree_secondes") if resultat_route else 0
    duree_visite = len(etapes) * temps_visite_minutes * 60
    film_guides = {int(v["film_id"]): v for v in visites_guidees}
    if guide_planifie:
        film_guides[int(guide_planifie["film_id"])] = {
            "film_id": int(guide_planifie["film_id"]), "nom": guide_planifie["nom"],
            "duree_minutes": int(guide_planifie["duree_minutes"]), "lien": guide_planifie.get("lien"),
            "heure_debut": guide_planifie.get("heure_debut"), "heure_fin": guide_planifie.get("heure_fin")
        }
    duree_visites_guidees = sum(int(v["duree_minutes"]) for v in film_guides.values())
    # Temps d'attente éventuellement nécessaire pour rejoindre un créneau publié.
    attente_visites_guidees_minutes = sum(int(x.get("attente_minutes") or 0) for x in planning_horaire if x.get("type") == "visite_guidee")
    duree_totale_estimee = (duree_trajet or 0) + duree_visite + duree_visites_guidees * 60 + attente_visites_guidees_minutes * 60
    budget_respecte = None if temps_disponible_minutes is None else duree_totale_estimee <= temps_disponible_minutes * 60

    # Planning horaire indicatif : il utilise les durées IGN des tronçons et
    # le temps de visite choisi. Il s'agit d'un planning estimatif, pas d'une
    # promesse d'horaires d'ouverture.
    def _minutes_hhmm(hhmm):
        h, m = [int(x) for x in hhmm.split(":")]
        return h * 60 + m
    def _hhmm(minutes):
        minutes = int(minutes) % (24 * 60)
        return f"{minutes // 60:02d}:{minutes % 60:02d}"
    planning_horaire = []
    minute_courante = _minutes_hhmm(heure_depart)
    troncons = (resultat_route or {}).get("trajets") or []
    for i, etape in enumerate(etapes):
        trajet = troncons[i] if i < len(troncons) else None
        if trajet and trajet.get("duree_secondes") is not None:
            minute_courante += round(float(trajet["duree_secondes"]) / 60)
        arrivee = _hhmm(minute_courante)
        depart_visite = minute_courante
        minute_courante += int(temps_visite_minutes)
        planning_horaire.append({
            "ordre": i + 1, "type": "lieu", "lieu_id": int(etape["id"]), "nom": etape.get("nom"),
            "film_id": etape.get("film_id"), "film_titre": etape.get("film_titre"),
            "media_type": etape.get("media_type"), "annee": etape.get("annee"),
            "heure_arrivee": arrivee, "heure_fin_visite": _hhmm(minute_courante),
            "temps_visite_minutes": int(temps_visite_minutes),
        })
        guide = film_guides.get(int(etape.get("film_id") or 0))
        # Une visite guidée liée à une œuvre n'est ajoutée qu'une seule fois,
        # sur la première étape de cette œuvre présente dans le parcours.
        if guide and not any(x.get("type") == "visite_guidee" and x.get("film_id") == int(etape.get("film_id") or 0) for x in planning_horaire):
            debut_guide = minute_courante
            attente = 0
            if guide.get("heure_debut"):
                cible = _minutes_hhmm(guide["heure_debut"])
                if minute_courante <= cible:
                    attente = cible - minute_courante
                    minute_courante = cible
            fin_guide = minute_courante + int(guide["duree_minutes"])
            guide_feasible = not guide.get("heure_fin") or fin_guide <= _minutes_hhmm(guide["heure_fin"])
            minute_courante = fin_guide
            planning_horaire.append({
                "ordre": i + 1, "type": "visite_guidee", "lieu_id": int(etape["id"]),
                "film_id": int(etape.get("film_id") or 0), "film_titre": etape.get("film_titre"),
                "nom": guide["nom"], "heure_arrivee": _hhmm(debut_guide),
                "heure_debut_guide": _hhmm(_minutes_hhmm(guide["heure_debut"])) if guide.get("heure_debut") else _hhmm(debut_guide),
                "heure_fin_visite": _hhmm(fin_guide), "temps_visite_minutes": int(guide["duree_minutes"]),
                "attente_minutes": attente, "creneau_respecte": guide_feasible,
                "lien": guide.get("lien"),
            })

    # Budget indicatif : uniquement les tarifs minimum renseignés par les
    # sources. Pelify ne transforme jamais une absence de tarif en prix inventé.
    budget_items = []
    for categorie in ("restaurant", "activite", "hebergement"):
        items = categories.get(categorie) or []
        if items:
            item = min(items, key=lambda x: float(x.get("tarif_min")) if x.get("tarif_min") is not None else 10**9)
            if item.get("tarif_min") is not None:
                try:
                    budget_items.append({"categorie": categorie, "nom": item.get("nom"), "tarif_min": float(item.get("tarif_min")), "devise": item.get("devise") or "EUR"})
                except (TypeError, ValueError): pass
    budget_estime_euros = round(sum(x["tarif_min"] for x in budget_items), 2) if budget_items else None
    budget_max_respecte = None if budget_max_euros is None or budget_estime_euros is None else budget_estime_euros <= budget_max_euros

    reponse = {
        "etapes": etapes,
        "nb_etapes": len(etapes),
        "mode": mode,
        "depart": depart_effectif,
        "retour_depart": retour_depart,
        "temps_disponible_minutes": temps_disponible_minutes,
        "temps_visite_minutes_par_etape": temps_visite_minutes,
        "duree_visite_estimee_secondes": duree_visite,
        "duree_visites_guidees_secondes": duree_visites_guidees * 60,
        "duree_totale_estimee_secondes": duree_totale_estimee,
        "visites_guidees": visites_guidees,
        "visites_guidees_disponibles": visites_disponibles,
        "visites_guidees_planifiees": [guide_planifie] if guide_planifie else [],
        "date_sortie": date_sortie or None,
        "scenario_recommande": {
            "type": "visite_guidee" if guide_planifie else "parcours_personnalise",
            "message": (f"Commencez par {guide_planifie['nom']} à {guide_planifie.get('heure_debut')} puis poursuivez avec les étapes optimisées." if guide_planifie and guide_planifie.get("heure_debut") else "Parcours calculé selon vos critères."),
        },
        "budget_respecte": budget_respecte,
        "categories_interet": categories_interet,
        "budget_level": budget_level,
        "accessibilite": accessibilite,
        "optimiser": optimiser,
        "inclure_visites_guidees": inclure_visites_guidees,
        "heure_depart": heure_depart,
        "planning_horaire": planning_horaire,
        "budget_max_euros": budget_max_euros,
        "budget_estime_euros": budget_estime_euros,
        "budget_items": budget_items,
        "budget_max_respecte": budget_max_respecte,
        "etapes_originales": etapes_originales,
        "etapes_exclues_optimisation": etapes_exclues_optimisation,
        "amenities": categories,
        "amenities_par_etape": par_etape,
        "recommandations_par_etape": recommandations_par_etape,
        "labels_categories": _LABELS_CATEGORIE,
        "icones_categories": ICONES_CATEGORIE,
    }

    if resultat_route:
        reponse.update(resultat_route)

    # Chaque calcul devient un parcours de l'historique du visiteur.
    await _enregistrer_parcours_historique(request, response, body, reponse)
    return reponse



# ══════════════════════════════════════════════════════════════
# V4.7 — HISTORIQUE, TABLEAU DE BORD ET GÉNÉRATEUR DE PARCOURS
# ══════════════════════════════════════════════════════════════

@app.get("/api/me")
async def api_me(request: Request, response: Response):
    user = await _ensure_profile(request, response)
    count = await fetch_one("SELECT COUNT(*) AS n FROM pelify_parcours_history WHERE user_id = %s", (user["id"],))
    return {"profil_id": str(user["id"]), "historique_parcours": int(count["n"] or 0), "type": "profil_visiteur"}


@app.get("/api/parcours/historique")
async def api_historique(request: Request, response: Response, limite: int = Query(20, ge=1, le=100)):
    user = await _ensure_profile(request, response)
    rows = await fetch_all(
        """SELECT id, titre, created_at, options_json, lieux_json, resultat_json,
                  nb_etapes, distance_metres, duree_secondes, duree_totale_estimee_secondes, budget_level
           FROM pelify_parcours_history WHERE user_id = %s ORDER BY created_at DESC LIMIT %s""",
        (user["id"], limite),
    )
    return {"parcours": _json_safe(rows)}


@app.get("/api/parcours/dashboard")
async def api_dashboard(request: Request, response: Response):
    user = await _ensure_profile(request, response)
    resume = await fetch_one(
        """SELECT COUNT(*) AS parcours,
                  COALESCE(SUM(nb_etapes),0) AS etapes,
                  COALESCE(SUM(distance_metres),0) AS distance_metres,
                  COALESCE(SUM(duree_secondes),0) AS duree_secondes
           FROM pelify_parcours_history WHERE user_id = %s""",
        (user["id"],),
    )
    films = await fetch_all(
        """SELECT x->>'film_titre' AS film, COUNT(*) AS occurrences
           FROM pelify_parcours_history h, jsonb_array_elements(h.lieux_json) x
           WHERE h.user_id = %s AND COALESCE(x->>'film_titre','') <> ''
           GROUP BY x->>'film_titre' ORDER BY occurrences DESC, film LIMIT 10""",
        (user["id"],),
    )
    communes = await fetch_all(
        """SELECT x->>'commune' AS commune, COUNT(*) AS occurrences
           FROM pelify_parcours_history h, jsonb_array_elements(h.lieux_json) x
           WHERE h.user_id = %s AND COALESCE(x->>'commune','') <> ''
           GROUP BY x->>'commune' ORDER BY occurrences DESC, commune LIMIT 10""",
        (user["id"],),
    )
    modes = await fetch_all(
        """SELECT options_json->>'mode' AS mode, COUNT(*) AS occurrences
           FROM pelify_parcours_history WHERE user_id = %s GROUP BY options_json->>'mode'""",
        (user["id"],),
    )
    budgets = await fetch_all(
        """SELECT budget_level, COUNT(*) AS occurrences
           FROM pelify_parcours_history WHERE user_id = %s GROUP BY budget_level""",
        (user["id"],),
    )
    categories = await fetch_all(
        """SELECT cat, COUNT(*) AS occurrences
           FROM pelify_parcours_history h
           CROSS JOIN LATERAL jsonb_array_elements_text(COALESCE(h.options_json->'categories_interet','[]'::jsonb)) cat
           WHERE h.user_id = %s GROUP BY cat ORDER BY occurrences DESC LIMIT 10""",
        (user["id"],),
    )
    recent = await fetch_all(
        """SELECT id, titre, created_at, nb_etapes, distance_metres, duree_totale_estimee_secondes, budget_level
           FROM pelify_parcours_history WHERE user_id = %s ORDER BY created_at DESC LIMIT 8""",
        (user["id"],),
    )
    return {"resume": _json_safe(resume or {}), "films": _json_safe(films), "communes": _json_safe(communes), "modes": _json_safe(modes), "budgets": _json_safe(budgets), "categories": _json_safe(categories), "recent": _json_safe(recent)}


@app.get("/api/parcours/historique/{parcours_id}")
async def api_historique_detail(parcours_id: int, request: Request, response: Response):
    user = await _ensure_profile(request, response)
    row = await fetch_one("SELECT * FROM pelify_parcours_history WHERE id = %s AND user_id = %s", (parcours_id, user["id"]))
    if not row:
        raise HTTPException(404, "Parcours introuvable")
    return _json_safe(row)


@app.delete("/api/parcours/historique")
async def api_historique_effacer(request: Request, response: Response):
    user = await _ensure_profile(request, response)
    await execute("DELETE FROM pelify_parcours_history WHERE user_id = %s", (user["id"],))
    return {"ok": True}


@app.delete("/api/parcours/historique/{parcours_id}")
async def api_historique_supprimer(parcours_id: int, request: Request, response: Response):
    user = await _ensure_profile(request, response)
    result = await execute("DELETE FROM pelify_parcours_history WHERE id = %s AND user_id = %s", (parcours_id, user["id"]))
    return {"ok": True}


def _construire_scenarios(candidats, depart, mode, temps_minutes, temps_visite_minutes, max_scenarios=4, max_etapes=5):
    """Construit des scénarios géographiques lisibles, sans prétendre optimiser l'itinéraire IGN.

    Le but est de proposer des familles de lieux cohérentes avant le calcul IGN final.
    On regroupe les candidats autour de plusieurs noyaux géographiques et on dédoublonne
    les coordonnées quasi identiques (un même lieu peut être associé à plusieurs œuvres).
    """
    if not candidats:
        return []
    dlat = float(depart.get("latitude")); dlon = float(depart.get("longitude"))
    mode = mode or "driving-car"
    # Rayon de cohérence du scénario. Plus serré à pied.
    rayon = 18000 if mode == "foot-walking" else 55000
    temps_h = max(0.5, float(temps_minutes or 240) / 60.0)
    # Le nombre de lieux qu'un scénario peut raisonnablement contenir avant IGN.
    max_places_temps = max(2, min(max_etapes, int((temps_minutes or 240) / max(15, int(temps_visite_minutes or 45)))))
    max_places_temps = min(max_places_temps, 6)

    # Dédoublonnage géographique : un même point de tournage lié à plusieurs œuvres
    # ne doit pas consommer deux étapes dans un scénario.
    uniques = []
    vus = set()
    for x in candidats:
        try:
            lat, lon = float(x["latitude"]), float(x["longitude"])
        except (TypeError, ValueError, KeyError):
            continue
        cle = (round(lat, 4), round(lon, 4), (str(x.get("nom") or "").strip().lower()))
        if cle in vus:
            continue
        vus.add(cle)
        x = dict(x)
        x["distance_depart_metres"] = int(haversine_metres(dlat, dlon, lat, lon))
        uniques.append(x)

    uniques.sort(key=lambda x: x["distance_depart_metres"])
    # On garde un vivier raisonnable : assez large pour proposer plusieurs scénarios.
    vivier = uniques[:40]
    if not vivier:
        return []

    # Noyaux : le départ + les candidats espacés. Cela produit des scénarios
    # géographiques différents plutôt qu'une simple liste triée par distance.
    noyaux = []
    for x in vivier:
        if all(haversine_metres(float(x["latitude"]), float(x["longitude"]), float(n["latitude"]), float(n["longitude"])) > rayon * 0.55 for n in noyaux):
            noyaux.append(x)
            if len(noyaux) >= max_scenarios:
                break
    if not noyaux:
        noyaux = [vivier[0]]

    scenarios = []
    for idx, noyau in enumerate(noyaux, 1):
        proches = []
        for x in vivier:
            dist_noyau = haversine_metres(float(noyau["latitude"]), float(noyau["longitude"]), float(x["latitude"]), float(x["longitude"]))
            if dist_noyau <= rayon:
                proches.append((dist_noyau, x))
        proches.sort(key=lambda t: (t[0], t[1].get("distance_depart_metres", 0)))
        lieux = []
        for _, x in proches:
            if any(haversine_metres(float(x["latitude"]), float(x["longitude"]), float(y["latitude"]), float(y["longitude"])) < 80 for y in lieux):
                continue
            lieux.append(x)
            if len(lieux) >= max_places_temps:
                break
        if not lieux:
            continue
        # Estimation prudente et explicite : visites + une approximation géographique.
        distance_approx = 0.0
        prev = (dlat, dlon)
        for x in lieux:
            distance_approx += haversine_metres(prev[0], prev[1], float(x["latitude"]), float(x["longitude"]))
            prev = (float(x["latitude"]), float(x["longitude"]))
        if retour := False:
            distance_approx += haversine_metres(prev[0], prev[1], dlat, dlon)
        # Coefficient de prudence : on ne présente jamais ceci comme le temps IGN réel.
        vitesse_kmh = 35 if mode == "driving-car" else 4.2
        deplacement_min = int((distance_approx / 1000) / vitesse_kmh * 60 * 1.25)
        visite_min = len(lieux) * int(temps_visite_minutes or 45)
        total_min = deplacement_min + visite_min
        films = []
        for x in lieux:
            ft = x.get("film_titre")
            if ft and ft not in films:
                films.append(ft)
        depart_km = round((haversine_metres(dlat, dlon, float(noyau["latitude"]), float(noyau["longitude"])) / 1000), 1)
        scenarios.append({
            "id": f"scenario-{idx}",
            "titre": f"{noyau.get('commune') or noyau.get('nom') or 'Secteur'} & environs",
            "lieux": _json_safe(lieux),
            "lieu_ids": [int(x["id"]) for x in lieux],
            "nb_etapes": len(lieux),
            "films": films,
            "distance_approx_metres": int(distance_approx),
            "temps_approx_minutes": total_min,
            "temps_deplacement_approx_minutes": deplacement_min,
            "temps_visite_minutes": visite_min,
            "distance_depuis_depart_metres": int(haversine_metres(dlat, dlon, float(noyau["latitude"]), float(noyau["longitude"]))),
            "distance_depuis_depart_km": depart_km,
            "compatible_temps": total_min <= int(temps_minutes or 240),
            "note": "Estimation indicative avant calcul IGN ; le trajet réel sera recalculé après votre sélection.",
        })
    scenarios.sort(key=lambda s: (not s["compatible_temps"], s["temps_approx_minutes"], s["distance_depuis_depart_metres"]))
    return scenarios[:max_scenarios]


@app.post("/api/parcours/generer")
async def api_generer_parcours(request: Request, response: Response):
    """Génère des possibilités/scénarios à partir des critères utilisateur.

    Cette étape ne remplace pas le calcul IGN : elle construit un vivier et des
    scénarios géographiquement cohérents. Le calcul IGN final intervient après
    sélection des lieux.
    """
    body = await request.json()
    lieu_ids = body.get("lieu_ids") or []
    depart = body.get("depart")
    mode = body.get("mode") or "driving-car"
    temps = int(body.get("temps_disponible_minutes") or 240)
    visite = int(body.get("temps_visite_minutes") or 45)
    retour = bool(body.get("retour_depart", False))
    max_etapes = max(2, min(int(body.get("max_etapes") or 8), 15))
    if not depart or depart.get("latitude") is None or depart.get("longitude") is None:
        raise HTTPException(400, "Un point de départ est nécessaire pour générer automatiquement des possibilités.")

    # Avec des lieux déjà choisis, on construit les scénarios sur ce vivier.
    # Sans lieux, on cherche autour du point de départ.
    if lieu_ids:
        lieu_ids = list(dict.fromkeys(int(x) for x in lieu_ids))[:40]
    else:
        lat, lon = float(depart["latitude"]), float(depart["longitude"])
        delta = 0.20 if mode == "foot-walking" else 0.65
        candidates = await fetch_all(
            """SELECT id, nom, commune, departement, latitude, longitude, film_id, f.titre AS film_titre,
                      f.media_type, f.annee, f.poster_url
               FROM lieux_tournage l LEFT JOIN films f ON f.id = l.film_id
               WHERE l.latitude BETWEEN %s AND %s AND l.longitude BETWEEN %s AND %s
                 AND l.latitude IS NOT NULL AND l.longitude IS NOT NULL""",
            (lat-delta, lat+delta, lon-delta, lon+delta),
        )
        candidates.sort(key=lambda x: haversine_metres(lat, lon, float(x["latitude"]), float(x["longitude"])))
        lieu_ids = [int(x["id"]) for x in candidates[:60]]

    if not lieu_ids:
        raise HTTPException(404, "Aucun lieu de tournage trouvé autour du départ.")

    # Requête IN paramétrée, compatible asyncpg/psycopg selon le driver utilisé.
    placeholders = ",".join([f"%s"] * len(lieu_ids))
    candidats_lieux = await fetch_all(
        f"""SELECT l.id, l.nom, l.commune, l.departement, l.latitude, l.longitude,
                   l.film_id, f.titre AS film_titre, f.media_type, f.annee, f.poster_url
            FROM lieux_tournage l LEFT JOIN films f ON f.id = l.film_id
            WHERE l.id IN ({placeholders})""",
        tuple(lieu_ids),
    )
    ordre = {int(v): i for i, v in enumerate(lieu_ids)}
    candidats_lieux.sort(key=lambda x: ordre.get(int(x["id"]), 999999))
    scenarios = _construire_scenarios(candidats_lieux, depart, mode, temps, visite, max_scenarios=4, max_etapes=min(6, max_etapes))
    selection_recommandee = scenarios[0]["lieu_ids"] if scenarios else []
    candidats_lieux_safe = _json_safe(candidats_lieux[:40])
    return {
        "candidats": lieu_ids,
        "candidats_lieux": candidats_lieux_safe,
        "scenarios": scenarios,
        "selection_recommandee": selection_recommandee,
        "selection_recommandee_lieux": _json_safe([x for x in candidats_lieux if int(x["id"]) in set(selection_recommandee)]),
        "exclus_selection": [],
        "payload": {
            "mode": mode,
            "temps_disponible_minutes": temps,
            "temps_visite_minutes": visite,
            "retour_depart": retour,
            "categories_interet": body.get("categories_interet") or [],
            "budget_level": body.get("budget_level") or "equilibre",
            "budget_max_euros": body.get("budget_max_euros"),
            "accessibilite": bool(body.get("accessibilite", False)),
            "optimiser": bool(body.get("optimiser", True)),
            "date_sortie": body.get("date_sortie"),
            "heure_depart": body.get("heure_depart") or "09:00",
        },
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
    """
    Sitemap XML dynamique pour Google.
    Inclut uniquement les contenus publiés en Occitanie.
    Les URLs des films sont générées avec la même logique canonique
    que les pages /films/{slug}-{id}.
    """
    films = await fetch_all(
        """
        SELECT id, titre, date_maj
        FROM films
        WHERE statut = 'publie'
          AND region = 'Occitanie'
        ORDER BY id
        """
    )

    urls = []

    # Page d'accueil
    urls.append(
        f"""  <url>
    <loc>{BASE_URL}/</loc>
    <changefreq>weekly</changefreq>
    <priority>1.0</priority>
  </url>"""
    )

    # Pages films
    for film in films:
        lastmod = ""
        if film.get("date_maj"):
            date_maj = film["date_maj"]
            if hasattr(date_maj, "date"):
                date_maj = date_maj.date()
            lastmod = f"\n    <lastmod>{date_maj.isoformat()}</lastmod>"

        urls.append(
            f"""  <url>
    <loc>{BASE_URL}{url_film(film)}</loc>{lastmod}
    <changefreq>monthly</changefreq>
    <priority>0.8</priority>
  </url>"""
        )

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
{chr(10).join(urls)}
</urlset>
"""

    return PlainTextResponse(
        content=xml,
        media_type="application/xml",
    )


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