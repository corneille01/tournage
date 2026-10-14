"""
backend/main.py â€” API CinÃ©Tour.

Tous les endpoints lisent des donnÃ©es dÃ©jÃ  en base (jamais d'appel
Overpass en direct sur une requÃªte visiteur) â€” voir refresh_cache.py
pour le remplissage du cache.
"""


from .geoplateforme import (
    calculer_itineraire as calculer_itineraire_geoplateforme,
    GeoplateformeError,
    RESOURCE_ITINERAIRE,
)
import logging

logger = logging.getLogger(__name__)
from .analyse_indicateurs import construire_indicateurs_cinetourisme, construire_observatoire_statistique
from .navigation_cache import navigation_cache
import os
import re
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

from .db import init_db_pool, close_db_pool, fetch_all, fetch_one, execute
from .overpass import phrase_recommandation, ICONES_CATEGORIE, haversine_metres, RAYON_RECHERCHE_M
from .seo import slugify, url_film, json_ld_film, meta_description
from .visites_cinetouristiques import creneaux_pour_date
from .distances_ign import ordre_plus_proche_voisin_ign, optimiser_etapes_ign
from .carburants import plan_carburant, stations_autour, normaliser_carburant, fiches_stations_du_lieu, CARBURANTS
from .guides import (
    guides_recommandes, exiger_admin, completer_localisation, fonction_guide,
    SPECIALITES_VALIDES, SPECIALITES_LIBELLES, MOBILITES_VALIDES, TYPES_GUIDE_VALIDES
)
from .paysages import router as paysages_router
from .droits import router as droits_router

templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))
BASE_URL = "https://tournage.pelify.app"  # Ã  remplacer par le vrai domaine en prod

_LABELS_CATEGORIE = {
    "hebergement":     "L'hÃ©bergement",
    "restaurant":      "Le restaurant",
    "office_tourisme": "L'office de tourisme",
    "police":          "Le commissariat/gendarmerie",
    "hopital":         "L'hÃ´pital",
    "gare":            "La gare",
    "aeroport":        "L'aÃ©roport",
    "aerodrome":       "L'aÃ©rodrome",
    "arret_bus":       "L'arrÃªt de bus",
    "parking":         "Le parking",
    "refuge":          "Le refuge",
    "distributeur":    "Le distributeur",
    "activite":        "L'activitÃ©",
     "fetes_manifestations": "L'Ã©vÃ©nement",
    "station_service": "La station-service",
}



PELIFY_USER_COOKIE = "pelify_user"
PELIFY_USER_SECRET = os.getenv("PELIFY_USER_SECRET", "pelify-dev-secret-change-me")


def _hash_user_token(token: str) -> str:
    return hashlib.sha256((PELIFY_USER_SECRET + ":" + token).encode("utf-8")).hexdigest()


async def _ensure_profile(request: Request, response: Response) -> dict:
    """Identifie un visiteur de faÃ§on pseudonyme pour sauvegarder ses parcours.

    Aucun nom, email ou position GPS n'est exigÃ©. Le cookie contient un jeton
    alÃ©atoire et la base ne conserve que son hash. Cela permet d'avoir un
    historique par navigateur sans transformer Pelify en systÃ¨me de compte.
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
        titre = " â†’ ".join(str(x.get("nom") or "Lieu") for x in resultat.get("etapes", [])[:3])
        if len(resultat.get("etapes", [])) > 3:
            titre += "â€¦"
        await execute(
            """INSERT INTO pelify_parcours_history
               (user_id, titre, options_json, lieux_json, resultat_json, nb_etapes, distance_metres, duree_secondes, duree_totale_estimee_secondes, budget_level)
               VALUES (%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s,%s,%s,%s,%s)""",
            (
                user["id"], titre or "Mon parcours cinÃ©ma", json.dumps(_json_safe(options)),
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


app = FastAPI(title="CinÃ©Tour API", lifespan=lifespan)

app.add_middleware(GZipMiddleware, minimum_size=500)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Ã  restreindre au domaine rÃ©el en prod
    allow_methods=["GET", "POST"],
)

app.include_router(paysages_router)
app.include_router(droits_router)

@app.get("/api/analyse/indicateurs")
async def api_analyse_indicateurs(
    region: str = Query("Occitanie")
):
    """
    Retourne les indicateurs dynamiques de l'observatoire
    du cinÃ©-tourisme pour la rÃ©gion demandÃ©e.

    Les indicateurs sont recalculÃ©s Ã  partir des donnÃ©es
    actuellement prÃ©sentes en base :
    - lieux de tournage
    - films
    - Ã©quipements touristiques
    - temps d'accÃ¨s
    - isochrones IGN prÃ©-calculÃ©s
    """
    try:
        return await construire_indicateurs_cinetourisme(region)
    except Exception as exc:
        logger.exception(
            "Erreur lors du calcul des indicateurs cinÃ©-tourisme"
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
    Dictionnaire statistique complet (offre, proximitÃ©, carence,
    diversitÃ© fonctionnelle, disparitÃ©s dÃ©partementales) par catÃ©gorie
    DATAtourisme. Endpoint sÃ©parÃ© de /api/analyse/indicateurs pour ne
    pas alourdir son cycle de rafraÃ®chissement principal : ce bloc fait
    davantage de requÃªtes SQL agrÃ©gÃ©es (percentiles par catÃ©gorie et
    par dÃ©partement) et change moins souvent que les KPI de tÃªte de page.
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
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# ISOCHRONES D'UN LIEU DE TOURNAGE
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

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



# â”€â”€ Liste des films (barre latÃ©rale) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
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
    Liste des films/sÃ©ries pour la sidebar, avec le nombre de lieux de
    tournage en Occitanie inclus directement (affichÃ© sur la carte
    avant mÃªme de cliquer) â€” Ã©vite un aller-retour supplÃ©mentaire par
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
    # Whitelist stricte : on n'insÃ¨re jamais le paramÃ¨tre "tri" tel
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
    Valeurs disponibles pour peupler les menus dÃ©roulants (annÃ©e,
    dÃ©partement, commune) â€” Ã©vite de coder ces listes en dur cÃ´tÃ©
    frontend, elles reflÃ¨tent toujours les donnÃ©es rÃ©elles en base.
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
          -- Wikidata rÃ©sout parfois le lieu directement au niveau du
          -- dÃ©partement plutÃ´t que de la commune prÃ©cise â€” on exclut
          -- ces valeurs du filtre "commune" pour ne pas les dupliquer
          -- avec le filtre "dÃ©partement".
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
    Chiffres clÃ©s pour le panneau statistiques â€” c'est ce qui
    transforme l'outil de "carte sympa" en "outil d'observation" pour
    l'Agence Unique : volumÃ©trie par dÃ©partement, par mÃ©dia, par
    dÃ©cennie, complÃ©tude des donnÃ©es.
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
    d'aide Ã  la dÃ©cision" plutÃ´t que "carte sympa" : quelques rÃ¨gles
    simples plutÃ´t qu'un score composite arbitraire, faciles Ã 
    expliquer et Ã  faire Ã©voluer avec l'Agence Unique. Les chiffres
    rÃ©els sont citÃ©s dans le texte, pas juste une Ã©tiquette qualitative.
    """
    nb_lieux = d["nb_lieux"] or 0
    moy_heberg = d["moy_hebergement"] or 0
    moy_resto = d["moy_restaurant"] or 0
    lieux_isoles = d["lieux_sans_hebergement_15km"] or 0
    part_isoles = round(100 * lieux_isoles / nb_lieux) if nb_lieux else 0

    if nb_lieux == 0:
        return "Aucune donnÃ©e suffisante pour ce dÃ©partement."
    if moy_heberg >= 3 and moy_resto >= 3:
        return (
            f"Avec {moy_heberg} hÃ©bergements et {moy_resto} restaurants en moyenne Ã  proximitÃ© des "
            f"{nb_lieux} lieux recensÃ©s, ce dÃ©partement est bien Ã©quipÃ© pour une valorisation "
            f"touristique immÃ©diate (circuit cinÃ©-touristique, signalÃ©tique) sans investissement prÃ©alable."
        )
    if part_isoles > 50:
        return (
            f"{part_isoles}% des {nb_lieux} lieux recensÃ©s ({lieux_isoles} sur {nb_lieux}) n'ont "
            f"aucun hÃ©bergement Ã  moins de 5 km â€” un amÃ©nagement (hÃ©bergement, signalÃ©tique, accÃ¨s) "
            f"est nÃ©cessaire avant toute promotion touristique de ces sites."
        )
    if moy_heberg < 1:
        return (
            f"Avec seulement {moy_heberg} hÃ©bergement en moyenne Ã  proximitÃ© des {nb_lieux} lieux, "
            f"le potentiel cinÃ©-touristique existe mais nÃ©cessite des partenariats avec des "
            f"hÃ©bergeurs locaux avant une valorisation Ã  grande Ã©chelle."
        )
    return (
        f"Ã‰quipement intermÃ©diaire ({moy_heberg} hÃ©bergements, {moy_resto} restaurants en moyenne "
        f"pour {nb_lieux} lieux) â€” Ã  Ã©valuer au cas par cas selon les lieux les plus emblÃ©matiques."
    )


@app.get("/api/lieux/tous-points")
async def tous_les_points(region: str = Query("Occitanie")):
    """
    CoordonnÃ©es de tous les lieux de tournage publiÃ©s, sans dÃ©tail â€”
    juste de quoi alimenter la carte de chaleur (densitÃ© visuelle des
    zones les plus sollicitÃ©es, en complÃ©ment de la choroplÃ¨the par
    dÃ©partement qui raisonne au niveau administratif).
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
    Seuils simples et dÃ©fendables (pas de score composite opaque) :
    moins de 20 min en voiture = bien desservi, plus de 45 min = isolÃ©.
    Retourne (Ã©tiquette, action recommandÃ©e).
    """
    if duree_secondes is None:
        return ("donnÃ©e manquante", "Lancer le prÃ©calcul des distances pour ce lieu (aucune donnÃ©e disponible actuellement).")
    minutes = duree_secondes / 60
    if minutes <= 20:
        return ("bien desservi", "Mettre ce lieu en avant dans la communication â€” l'expÃ©rience touristique y est dÃ©jÃ  fluide, sans amÃ©nagement prÃ©alable nÃ©cessaire.")
    if minutes <= 45:
        return ("accessibilitÃ© modÃ©rÃ©e", "Signaler clairement les temps de trajet rÃ©els aux visiteurs avant leur venue, pour Ã©viter une dÃ©ception sur place.")
    return ("isolÃ©", "NÃ©cessite une action avant valorisation : partenariat avec un hÃ©bergeur/restaurateur plus proche, navette dÃ©diÃ©e, ou signalÃ©tique renforcÃ©e sur les distances rÃ©elles.")


CATEGORIES_ANALYSE_ACCESSIBILITE = (
    "hebergement", "restaurant", "activite", "parking",
    "office_tourisme", "gare", "aeroport", "aerodrome",
)


@app.get("/api/analyse/accessibilite")
async def analyse_accessibilite(region: str = Query("Occitanie"), limite: int = Query(20)):
    """
    Pour les films/sÃ©ries les plus connus (popularitÃ© TMDB), audit
    d'Ã©quipement touristique complet â€” pas seulement hÃ©bergement et
    restaurant, mais aussi activitÃ©s, parking, office de tourisme,
    gare et aÃ©roport/aÃ©rodrome : une lecture pensÃ©e comme un vrai
    diagnostic d'amÃ©nagement territorial, pas juste "y a-t-il un hÃ´tel
    Ã  cÃ´tÃ©".

    Toutes les donnÃ©es sont rÃ©cupÃ©rÃ©es en 4 requÃªtes groupÃ©es (pas une
    par film) â€” l'ancienne version (~140 requÃªtes sÃ©quentielles pour
    20 films) provoquait des lenteurs, voire des Ã©checs.

    MÃ©thodologie pour un film Ã  PLUSIEURS lieux : chaque catÃ©gorie de
    commoditÃ© est cherchÃ©e en tenant compte de TOUS les lieux du film
    Ã  la fois (un touriste peut visiter n'importe lequel des lieux,
    donc on retient la meilleure option parmi tous). Le nombre de
    lieux du film est toujours indiquÃ© pour que cette agrÃ©gation soit
    transparente plutÃ´t qu'implicite.
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

        # Une seule requÃªte pour TOUTES les commoditÃ©s, tous films et
        # catÃ©gories confondus â€” regroupÃ© en Python ensuite.
        toutes_commodites = await fetch_all(
            """
                SELECT lieu_tournage_id, categorie, nom, latitude, longitude,
               distance_metres, adresse, telephone, site_web,
               horaires, photo_url, tarif_min, tarif_max, devise,
               capacite,
+              types, organisme_diffuseur, reseaux_sociaux, photo_credits, photo_licence,
               distance_pied_metres, duree_pied_secondes,
               distance_voiture_metres, duree_voiture_secondes
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

        # Index en mÃ©moire : (lieu_id, categorie) â†’ [commoditÃ©s triÃ©es par durÃ©e]
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

            # Risque de sur-frÃ©quentation : popularitÃ© forte + Ã©quipement
            # faible = afflux Ã  anticiper plutÃ´t qu'Ã  subir (leÃ§on de
            # Dubrovnik/San Juan de Gaztelugatxe aprÃ¨s Game of Thrones).
            ratio_equipement = score_bien_dessservi / len(CATEGORIES_ANALYSE_ACCESSIBILITE)
            if film["popularite"] >= 20 and ratio_equipement < 0.375:
                entree["risque_surfrequentation"] = "Ã©levÃ©"
                entree["action_surfrequentation"] = "Anticiper dÃ¨s maintenant : renforcer l'offre locale (hÃ©bergement, transport, accÃ¨s) avant qu'un afflux imprÃ©vu ne dÃ©grade l'expÃ©rience et le site lui-mÃªme."
            elif film["popularite"] >= 10 and ratio_equipement < 0.5:
                entree["risque_surfrequentation"] = "modÃ©rÃ©"
                entree["action_surfrequentation"] = "Ã€ surveiller : la notoriÃ©tÃ© du titre dÃ©passe dÃ©jÃ  lÃ©gÃ¨rement la capacitÃ© d'accueil locale actuelle."
            else:
                entree["risque_surfrequentation"] = "faible"
                entree["action_surfrequentation"] = "Ã‰quipement cohÃ©rent avec la notoriÃ©tÃ© actuelle â€” pas d'action urgente."

            categorie_liste = "francais" if film["nationalite"] and "FranÃ§ais" in film["nationalite"] else "autres"
            resultat[categorie_liste].append(entree)

        return resultat
    except Exception as e:
        # Ne JAMAIS renvoyer null silencieusement â€” au moins un message
        # d'erreur exploitable, visible dans l'onglet RÃ©seau du navigateur,
        # plutÃ´t qu'un Ã©chec invisible.
        import traceback
        print(f"âŒ /api/analyse/accessibilite a Ã©chouÃ© : {e}", flush=True)
        traceback.print_exc()
        return JSONResponse(
            status_code=500,
            content={"erreur": str(e), "francais": [], "autres": []},
        )


@app.get("/api/analyse")
async def analyse_territoriale(region: str = Query("Occitanie")):
    """
    Version approfondie de /api/stats, pensÃ©e pour la page d'analyse
    territoriale : compare les dÃ©partements sur des indicateurs
    d'Ã©quipement rÃ©el (pas seulement le nombre de films), avec une
    recommandation textuelle par dÃ©partement.
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

    # Films les plus reconnus (popularitÃ© TMDB) â€” pour la section dÃ©diÃ©e
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

    # SynthÃ¨se comparative chiffrÃ©e : pourquoi le 1er dÃ©partement est
    # plus sollicitÃ© que le dernier, en s'appuyant uniquement sur des
    # chiffres dÃ©jÃ  prÃ©sents en base (pas d'affirmation non vÃ©rifiable).
    synthese_comparative = None
    if len(resultat) >= 2:
        premier, dernier = resultat[0], resultat[-1]
        ratio = round(premier["nb_lieux"] / dernier["nb_lieux"], 1) if dernier["nb_lieux"] else None
        synthese_comparative = (
            f"{premier['departement']} concentre {premier['nb_lieux']} lieux de tournage recensÃ©s "
            f"({premier['part_pourcentage']}% du total rÃ©gional), contre seulement {dernier['nb_lieux']} "
            f"pour {dernier['departement']} ({dernier['part_pourcentage']}%)"
            + (f" â€” soit {ratio} fois plus de lieux." if ratio else ".")
        )

    # Films sans coordonnÃ©es / sans image / non validÃ©s â€” complÃ©tude
    # des donnÃ©es, utile pour prioriser le travail Ã©ditorial restant.
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


# â”€â”€ DÃ©tail d'un film + ses lieux de tournage â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
TMDB_API_KEY = os.getenv("TMDB_API_KEY", "")
PLATEFORMES_CACHE_JOURS = 7


async def _plateformes_streaming(film: dict) -> list[dict]:
    """
    OÃ¹ regarder ce film en France (TMDB watch/providers), mis en cache
    en base pour ne pas rappeler TMDB Ã  chaque visite. C'est ici que
    les liens d'affiliation (Awin etc.) doivent Ãªtre insÃ©rÃ©s â€” chaque
    entrÃ©e retournÃ©e a un champ "lien_affilie" vide Ã  remplir avec ton
    vrai lien trackÃ© une fois les partenariats en place.
    """
    if not film.get("tmdb_id"):
        return []

    def _parser_cache(valeur) -> list:
        """asyncpg ne dÃ©code pas automatiquement JSONB : selon le driver
        et la version, on peut recevoir soit dÃ©jÃ  une liste, soit une
        chaÃ®ne JSON brute. On gÃ¨re les deux pour ne jamais planter le
        frontend avec un .map() sur une chaÃ®ne."""
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
    lien_general = data.get("link")  # page TMDB "oÃ¹ regarder" pour ce film â€” repli tant qu'il n'y a pas de vrai lien d'affiliation
    for categorie in ("flatrate", "rent", "buy"):
        for p in data.get(categorie, []):
            plateformes.append({
                "nom": p["provider_name"],
                "logo_url": f"https://image.tmdb.org/t/p/w92{p['logo_path']}",
                "type": {"flatrate": "streaming", "rent": "location", "buy": "achat"}[categorie],
                "lien_affilie": "",  # Ã  remplir : lien Awin/partenaire pour ce provider
                "lien_repli": lien_general,  # utilisÃ© tant que lien_affilie est vide
            })

    await _sauvegarder_plateformes_cache(film["id"], plateformes)
    return plateformes


async def _sauvegarder_plateformes_cache(film_id: int, plateformes: list[dict]) -> None:
    """
    IsolÃ© dans sa propre fonction avec gestion d'erreur : si l'Ã©criture
    du cache Ã©choue (mismatch de type, base indisponible...), la page
    doit quand mÃªme s'afficher avec les plateformes fraÃ®chement
    rÃ©cupÃ©rÃ©es â€” juste sans les mettre en cache cette fois-ci. Ne
    JAMAIS laisser un souci de cache secondaire faire planter la fiche
    film entiÃ¨re (c'est ce qui causait le 500 sur tous les films).
    """
    try:
        await execute(
            "UPDATE films SET plateformes_json = %s, plateformes_maj = %s WHERE id = %s",
            (json.dumps(plateformes), datetime.now(timezone.utc), film_id),
        )
    except Exception as e:
        print(f"âš ï¸ Cache plateformes non sauvegardÃ© pour film {film_id}: {e}", flush=True)


def _parser_json(valeur):
    """asyncpg renvoie JSONB comme une chaÃ®ne brute par dÃ©faut (pas de
    codec enregistrÃ©) â€” on la parse nous-mÃªmes avant de la renvoyer,
    sinon le frontend recevrait une chaÃ®ne au lieu d'un objet."""
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


# â”€â”€ Amenities proches d'un lieu (appelÃ© au clic sur l'icÃ´ne) â”€â”€â”€â”€â”€
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
               types, organisme_diffuseur, reseaux_sociaux, photo_credits, photo_licence,
               distance_pied_metres, duree_pied_secondes,
               distance_voiture_metres, duree_voiture_secondes
        FROM amenity_cache
        WHERE lieu_tournage_id = %s
        ORDER BY categorie, rang
        """,
        (lieu_id,),
    )

    amenities_par_categorie: dict[str, list[dict]] = {}
    for row in rows:
        item = dict(row)
        item["distance_texte"] = (
            _formater_distance(item["distance_metres"])
            if item["distance_metres"] is not None else None
        )
        item["duree_pied_texte"] = (
            _formater_duree(item["duree_pied_secondes"])
            if item["duree_pied_secondes"] is not None else None
        )
        item["duree_voiture_texte"] = (
            _formater_duree(item["duree_voiture_secondes"])
            if item["duree_voiture_secondes"] is not None else None
        )
        amenities_par_categorie.setdefault(item["categorie"], []).append(item)

    # Stations-service : on joint la fiche complÃ¨te (prix, ruptures, horaires, services...)
    # lue dans stations_carburant. En cas de souci (migration pas encore jouÃ©e...), le reste
    # des commoditÃ©s du lieu reste servi normalement.
    if amenities_par_categorie.get("station_service"):
        try:
            fiches = await fiches_stations_du_lieu(lieu_id)
            for item in amenities_par_categorie["station_service"]:
                item["station"] = fiches.get(int(item["rang"]))
        except Exception:
            logging.getLogger(__name__).exception("Fiches stations-service indisponibles")

    return {"lieu": lieu, "amenities": amenities_par_categorie}


def _ordre_plus_proche_voisin(lieux: list[dict]) -> list[dict]:
    """Ordonne les lieux par plus proche voisin (heuristique simple,
    pas un vrai TSP optimal â€” largement suffisant pour quelques lieux
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
    Calcule un itinÃ©raire rÃ©el via la GÃ©oplateforme IGN.

    Architecture :

        Client
           â†“
        FastAPI
           â†“
        Redis / Upstash
           â†“
        IGN GÃ©oplateforme uniquement en cas de cache miss

    Le cache Redis est utilisÃ© uniquement pour les itinÃ©raires
    dynamiques des utilisateurs.

    Les donnÃ©es d'accessibilitÃ© aux Ã©quipements touristiques
    restent gÃ©rÃ©es sÃ©parÃ©ment par amenity_cache/PostgreSQL.
    """

    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # 0. VALIDATION DES PARAMÃˆTRES
    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

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
                    "Le mode doit Ãªtre 'pedestrian' "
                    "ou 'car'."
                ),
            },
        )

    # VÃ©rification basique des coordonnÃ©es.
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
                "message": "Les coordonnÃ©es GPS sont invalides.",
            },
        )

    # Ã‰vite de demander un itinÃ©raire vers exactement le
    # mÃªme point.
    if (
        abs(depart_lat - arrivee_lat) < 0.000001
        and abs(depart_lon - arrivee_lon) < 0.000001
    ):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "SAME_ORIGIN_DESTINATION",
                "message": (
                    "Le point de dÃ©part et le point d'arrivÃ©e "
                    "sont identiques."
                ),
            },
        )

    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # 1. CACHE NAVIGATION
    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    # Si Upstash Redis est configurÃ©, on construit une clÃ©
    # normalisÃ©e.
    #
    # IMPORTANT :
    # navigation_cache.enabled Ã©vite de faire attendre
    # inutilement l'utilisateur lorsque Redis n'est pas
    # configurÃ© ou temporairement indisponible.

    cache_key = navigation_cache.construire_cle(
        depart_lat=depart_lat,
        depart_lon=depart_lon,
        arrivee_lat=arrivee_lat,
        arrivee_lon=arrivee_lon,
        mode=mode,
        etapes=etapes,
    )

    lock_key = f"{cache_key}:lock"

    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # 2. CACHE HIT
    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    if navigation_cache.enabled:
        resultat = await navigation_cache.get(cache_key)

        if resultat:
            resultat["cache"] = "redis"
            resultat["cache_hit"] = True

            return resultat

    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # 3. CACHE MISS â†’ VERROU ANTI-RAFALE
    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    if navigation_cache.enabled:

        verrou_obtenu = await navigation_cache.acquire_lock(
            lock_key
        )

        if not verrou_obtenu:

            # Une autre requÃªte est dÃ©jÃ  en train de calculer
            # exactement le mÃªme itinÃ©raire.
            #
            # On attend son rÃ©sultat plutÃ´t que de lancer
            # plusieurs appels identiques vers IGN.

            resultat = await navigation_cache.wait_for_result(
                cache_key
            )

            if resultat:
                resultat["cache"] = "redis"
                resultat["cache_hit"] = True
                resultat["cache_deduplicated"] = True

                return resultat

            # Si aucun rÃ©sultat n'est apparu aprÃ¨s l'attente,
            # on tente de reprendre le verrou.
            #
            # Cela Ã©vite autant que possible que plusieurs
            # requÃªtes frappent IGN simultanÃ©ment.

            verrou_obtenu = await navigation_cache.acquire_lock(
                lock_key
            )

            # Si le verrou est toujours occupÃ©, on continue
            # exceptionnellement vers IGN.
            #
            # Le verrou possÃ¨de un TTL automatique et ne peut
            # donc pas rester bloquÃ© dÃ©finitivement.

    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # 4. APPEL IGN
    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

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
            "âŒ GÃ©oplateforme indisponible pour "
            f"l'itinÃ©raire : {exc}",
            flush=True,
        )

        raise HTTPException(
            status_code=503,
            detail={
                "code": "GEOPLATEFORME_UNAVAILABLE",
                "message": (
                    "Le service d'itinÃ©raire IGN est "
                    "temporairement indisponible. Aucun "
                    "itinÃ©raire de substitution n'a Ã©tÃ© utilisÃ©."
                ),
                "provider": "geoplateforme",
                "resource": RESOURCE_ITINERAIRE,
            },
        ) from exc

    except Exception as exc:

        print(
            "âŒ Erreur inattendue calcul itinÃ©raire : "
            f"{exc}",
            flush=True,
        )

        raise HTTPException(
            status_code=500,
            detail={
                "code": "ITINERARY_ERROR",
                "message": (
                    "Une erreur inattendue est survenue "
                    "pendant le calcul de l'itinÃ©raire."
                ),
                "provider": "geoplateforme",
                "resource": RESOURCE_ITINERAIRE,
            },
        ) from exc

    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # 5. VÃ‰RIFICATION DE LA RÃ‰PONSE IGN
    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    if not resultat or not isinstance(resultat, dict):

        raise HTTPException(
            status_code=502,
            detail={
                "code": "INVALID_GEOPLATEFORME_RESPONSE",
                "message": (
                    "La GÃ©oplateforme a rÃ©pondu mais le "
                    "rÃ©sultat d'itinÃ©raire est invalide."
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
                    "La GÃ©oplateforme n'a retournÃ© aucune "
                    "gÃ©omÃ©trie d'itinÃ©raire exploitable."
                ),
                "provider": "geoplateforme",
                "resource": RESOURCE_ITINERAIRE,
            },
        )

    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # 6. FORMAT STANDARD POUR LE FRONTEND
    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    resultat["type"] = "route_reelle"

    resultat["provider"] = "geoplateforme"

    resultat["resource"] = RESOURCE_ITINERAIRE

    resultat["mode"] = mode

    # Le frontend de navigation utilise cette propriÃ©tÃ©.

    if etapes:

        resultat["etapes_navigation"] = (
            resultat.get("etapes_navigation")
            or resultat.get("etapes")
            or []
        )

    else:

        resultat["etapes_navigation"] = []

    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # 7. INFORMATIONS DE DIAGNOSTIC
    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    resultat["cache"] = (
        "redis"
        if navigation_cache.enabled
        else "disabled"
    )

    resultat["cache_hit"] = False

    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # 8. Ã‰CRITURE DANS REDIS
    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    if navigation_cache.enabled:

        await navigation_cache.set(
            cache_key,
            resultat,
        )

    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # 9. RÃ‰PONSE
    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    return resultat   


async def _ordre_optimise(lieux: list[dict]) -> list[dict]:
    """
    Ordonne un petit circuit : Ã  chaque pas, on va vers le lieu le plus proche
    PAR LA ROUTE (GÃ©oplateforme IGN, rÃ©sultats mis en cache). Le vol d'oiseau ne
    sert qu'Ã  prÃ©sÃ©lectionner quelques candidats avant d'interroger l'IGN.
    """

    return await ordre_plus_proche_voisin_ign(lieux, "driving-car")





def _adresse_complete(lieu: dict) -> str:
    return ", ".join(p for p in (lieu["nom"], lieu.get("commune"), lieu.get("departement")) if p)



# tarif_min (DATAtourisme) est le prix le plus BAS de toute la fiche : il peut
# correspondre Ã  une taxe de sÃ©jour, un menu enfant ou une option. En dessous
# de ces seuils on le juge peu fiable et on ne l'utilise ni pour le score, ni
# pour le budget, ni pour l'affichage.
_SEUIL_TARIF_MIN_FIABLE = {"hebergement": 15.0, "restaurant": 5.0}
_LABELS_CATEGORIE_SINGULIER = {
    "restaurant": "restaurant", "hebergement": "hÃ©bergement", "activite": "activitÃ©",
    "office_tourisme": "office de tourisme", "parking": "parking", "gare": "gare",
    "aeroport": "aÃ©roport", "refuge": "refuge", "arret_bus": "arrÃªt de bus",
    "fetes_manifestations": "Ã©vÃ©nement",
}


def _nombre(valeur):
    try:
        return float(valeur) if valeur is not None else None
    except (TypeError, ValueError):
        return None


def _tarif_fiable(item, categorie):
    mn = _nombre(item.get("tarif_min"))
    if mn is None or mn <= 0:
        return None
    return mn if mn >= _SEUIL_TARIF_MIN_FIABLE.get(categorie, 0.0) else None


def _fmt_eur(valeur):
    txt = f"{valeur:.2f}".rstrip("0").rstrip(".")
    return txt.replace(".", ",") + " â‚¬"


def _libelle_tarif(item, categorie):
    """Texte de prix honnÃªte : dit ce que reprÃ©sente le chiffre affichÃ©."""
    mn, mx = _tarif_fiable(item, categorie), _nombre(item.get("tarif_max"))
    if mn is not None:
        if mx and mx > mn * 1.05:
            return f"Tarifs relevÃ©s : de {_fmt_eur(mn)} Ã  {_fmt_eur(mx)}"
        return f"Ã€ partir de {_fmt_eur(mn)}"
    if categorie == "restaurant" and mx:
        return f"Tarifs relevÃ©s jusqu'Ã  {_fmt_eur(mx)}"
    return None


_CATEGORIES_TRANSPORT = {"gare", "aeroport", "aerodrome", "arret_bus", "refuge"}
_MOTS_VIDES = {"de", "du", "des", "la", "le", "les", "et", "en", "au", "aux", "sur", "sous", "dans", "pour", "par", "the", "of"}


def _tokens_nom(texte):
    import unicodedata
    t = unicodedata.normalize("NFD", str(texte or "").lower())
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    return {m for m in re.findall(r"[a-z0-9]+", t) if len(m) > 3 and m not in _MOTS_VIDES}


def _est_l_etape_elle_meme(item, etape_nom):
    """Ã‰vite de suggÃ©rer, pour Â« ChÃ¢teau X Â», l'offre Â« ChÃ¢teau et remparts de X Â»."""
    commun = _tokens_nom(item.get("nom")) & _tokens_nom(etape_nom)
    return len(commun) >= 3 and float(item.get("distance_metres") or 0) < 300


def _profil_score_offre(item, categorie, budget_level="equilibre", accessibilite=False,
                        mode="driving-car", extremite=False):
    """Score dÃ©terministe d'une commoditÃ© pour une personnalisation lÃ©gÃ¨re.

    Le score sert Ã  ordonner les offres selon les prÃ©fÃ©rences choisies ; ce
    n'est pas une note de qualitÃ© universelle. Les points de transport ne
    sont pertinents qu'au dÃ©but ou Ã  la fin du parcours.
    """
    score = 0.0
    distance = float(item.get("meilleure_distance_metres") or item.get("distance_metres") or 999999)
    echelle = 180.0 if mode != "driving-car" else 400.0
    score += max(0.0, 38.0 - distance / echelle)

    if categorie in _CATEGORIES_TRANSPORT:
        score += 10.0 if (extremite and categorie in ("gare", "aeroport")) else -15.0

    note = item.get("note_etoiles")
    try:
        note = float(note) if note is not None else None
    except (TypeError, ValueError):
        note = None
    if note is not None:
        score += min(25.0, max(0.0, note * 5.0))

    tarif = _tarif_fiable(item, categorie)
    if tarif is not None:
        if budget_level == "economique":
            score += max(0.0, 28.0 - min(tarif, 100.0) * 0.35)
        elif budget_level == "confort":
            score += min(18.0, tarif * 0.18)
        else:
            score += max(0.0, 16.0 - min(tarif, 100.0) * 0.12)

    texte_access = " ".join(str(item.get(k) or "") for k in ("equipements", "labels_qualite", "description", "lien_accessibilite")).lower()
    accessible = bool(item.get("lien_accessibilite")) or any(x in texte_access for x in ("pmr", "accessible", "accessibilitÃ©", "handicap"))
    if accessibilite:
        score += 28.0 if accessible else -28.0
    elif accessible:
        score += 3.0

    if item.get("site_web"):
        score += 4.0
    if item.get("telephone"):
        score += 2.0
    return round(score, 2)


def _phrase_recommandation_offre(item, categorie, profil, etape_nom, mode="driving-car",
                                 accessibilite=False, etape_ordre=None):
    """Raison affichÃ©e sous la suggestion. Elle dit toujours de QUELLE Ã©tape
    l'offre est proche (numÃ©ro et nom) et ce que reprÃ©sente chaque chiffre.
    Ex. : Â« Ã€ 1 min en voiture de l'Ã©tape 3 - SommiÃ¨res Â· Tarifs relevÃ©s jusqu'Ã  25,5 â‚¬ Â»."""
    a_pied = mode != "driving-car"
    duree = item.get("duree_pied_secondes") if a_pied else item.get("duree_voiture_secondes")
    distance = item.get("meilleure_distance_metres") or item.get("distance_metres")
    de_etape = ""
    if etape_ordre:
        de_etape = f" de l'Ã©tape {etape_ordre}" + (f" - {etape_nom}" if etape_nom else "")
    elif etape_nom:
        de_etape = f" de Â« {etape_nom} Â»"
    raisons = []
    if duree is not None:
        mn = max(1, round(float(duree) / 60))
        raisons.append(f"Ã€ {mn} min {'Ã  pied' if a_pied else 'en voiture'}{de_etape}")
    elif distance is not None:
        d = float(distance)
        raisons.append((f"Ã€ {round(d)} m" if d < 1000 else f"Ã€ {d / 1000:.1f} km".replace(".", ",")) + de_etape)
    else:
        raisons.append(f"Proche{de_etape}" if de_etape else "Proche de l'Ã©tape")
    libelle = _libelle_tarif(item, categorie)
    if libelle:
        raisons.append(libelle + (" (budget serrÃ©)" if profil == "economique" and _tarif_fiable(item, categorie) else ""))
    note = _nombre(item.get("note_etoiles"))
    if note is not None:
        raisons.append(f"{note:g} Ã©toile(s)")
    texte_access = " ".join(str(item.get(k) or "") for k in ("equipements", "labels_qualite", "lien_accessibilite")).lower()
    if accessibilite and (item.get("lien_accessibilite") or any(x in texte_access for x in ("pmr", "accessible", "handicap"))):
        raisons.append("accessibilitÃ© renseignÃ©e")
    return " Â· ".join(raisons)


def _recommandations_par_etape(etapes, categories, par_etape, budget_level, accessibilite, mode, seuil=15.0, max_par_etape=3):
    """Jusqu'Ã  `max_par_etape` suggestions par Ã©tape, une par catÃ©gorie, triÃ©es
    par score. Les catÃ©gories retenues sont celles de `categories` (donc
    dÃ©jÃ  filtrÃ©es par les centres d'intÃ©rÃªt de l'utilisateur)."""
    resultat = {}
    n = len(etapes)
    for idx, etape in enumerate(etapes):
        eid = str(int(etape["id"]))
        extremite = idx == 0 or idx == n - 1
        propositions = []
        for categorie in categories.keys():
            if categorie in ("arret_bus", "refuge", "aerodrome"):
                continue  # visibles dans Â« Toutes les offres Â», pas des suggestions d'Ã©tape
            if categorie in ("gare", "aeroport") and not extremite:
                continue
            candidats = []
            for x in par_etape.get(eid, []):
                if x.get("categorie") != categorie or _est_l_etape_elle_meme(x, etape.get("nom")):
                    continue
                if categorie in ("gare", "aeroport"):
                    d_s = x.get("duree_pied_secondes") if mode != "driving-car" else x.get("duree_voiture_secondes")
                    if (d_s is not None and float(d_s) > 1200) or (d_s is None and float(x.get("distance_metres") or 0) > 3000):
                        continue
                x["score_personnalise"] = _profil_score_offre(x, categorie, budget_level, accessibilite, mode, extremite)
                candidats.append(x)
            if not candidats:
                continue
            meilleur = max(candidats, key=lambda x: x["score_personnalise"])
            if meilleur["score_personnalise"] < seuil:
                continue
            rec = dict(meilleur)
            rec["etape_id"] = int(etape["id"])
            rec["etape_ordre"] = idx + 1
            rec["etape_nom"] = etape.get("nom")
            rec["categorie_label"] = _LABELS_CATEGORIE_SINGULIER.get(categorie, categorie)
            rec["tarif_libelle"] = _libelle_tarif(rec, categorie)
            rec["raison"] = _phrase_recommandation_offre(rec, categorie, budget_level, etape.get("nom"), mode, accessibilite, idx + 1)
            rec["action_url"] = rec.get("site_web") or None
            rec["action_label"] = "Voir / rÃ©server" if rec.get("site_web") else ("Appeler" if rec.get("telephone") else None)
            propositions.append(rec)
        propositions.sort(key=lambda x: x["score_personnalise"], reverse=True)
        resultat[eid] = propositions[:max_par_etape]
    return resultat


def _optimiser_etapes_approx(etapes, depart, mode, temps_disponible_minutes, temps_visite_minutes, retour_depart):
    """PrÃ©-sÃ©lection rapide avant les appels IGN.

    On Ã©vite une explosion du nombre d'appels rÃ©seau : l'heuristique utilise
    les distances gÃ©ographiques puis l'itinÃ©raire IGN est recalculÃ© ensuite.
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
    # Si le dÃ©part est dÃ©jÃ  une Ã©tape, elle doit rester la premiÃ¨re.
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
    Calcule un itinÃ©raire rÃ©el entre plusieurs lieux avec
    la GÃ©oplateforme IGN.

    La GÃ©oplateforme est l'unique fournisseur d'itinÃ©raire.

    Aucun fallback :
    - pas d'OSRM public
    - pas d'OpenRouteService
    - pas de ligne droite

    Si un seul tronÃ§on Ã©choue, l'itinÃ©raire complet est considÃ©rÃ©
    comme indisponible.
    """

    if not lieux_ordonnes or len(lieux_ordonnes) < 2:
        raise GeoplateformeError(
            "Au moins deux lieux sont nÃ©cessaires pour calculer "
            "un itinÃ©raire."
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
                f"Ã‰chec du tronÃ§on {i + 1}/{nb_troncons} "
                f"avec la GÃ©oplateforme IGN : {exc}"
            ) from exc

        except Exception as exc:
            raise GeoplateformeError(
                f"Erreur inattendue sur le tronÃ§on "
                f"{i + 1}/{nb_troncons} : {exc}"
            ) from exc

        # IMPORTANT :
        # Aucun fallback gÃ©omÃ©trique.
        if not resultat:
            raise GeoplateformeError(
                f"La GÃ©oplateforme IGN n'a retournÃ© aucun itinÃ©raire "
                f"pour le tronÃ§on {i + 1}/{nb_troncons}."
            )

        geometry = resultat.get("geometry")

        if not geometry:
            raise GeoplateformeError(
                f"La GÃ©oplateforme IGN n'a retournÃ© aucune gÃ©omÃ©trie "
                f"pour le tronÃ§on {i + 1}/{nb_troncons}."
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
    # Fusion des gÃ©omÃ©tries
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
                f"Type de gÃ©omÃ©trie IGN non supportÃ© : "
                f"{geometry_type}"
            )

    if len(coordinates) < 2:
        raise GeoplateformeError(
            "La GÃ©oplateforme IGN a retournÃ© une gÃ©omÃ©trie "
            "insuffisante pour construire l'itinÃ©raire."
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



@app.get("/api/carburants/autour")
async def api_carburants_autour(
    lat: float = Query(..., ge=-90, le=90), lon: float = Query(..., ge=-180, le=180),
    rayon_km: float = Query(10, gt=0, le=30), carburant: str = Query("e10"),
    limite: int = Query(20, ge=1, le=50),
):
    """Stations les moins chÃ¨res autour d'un point (donnÃ©es importÃ©es chaque jour)."""
    cle = normaliser_carburant(carburant)
    if not cle:
        raise HTTPException(400, f"Carburant inconnu. Valeurs : {', '.join(CARBURANTS)}")
    r = await stations_autour(lat, lon, rayon_km * 1000, cle)
    stations = sorted(r["stations"], key=lambda x: (x["prix"], x["distance_m"]))[:limite]
    return {
        "carburant": cle, "nb_stations": r["nb_total"], "nb_rupture": r["nb_rupture"],
        "stations": [{
            "id": x["id"], "nom": x.get("nom") or x.get("marque"), "marque": x.get("marque"),
            "adresse": x.get("adresse"), "commune": x.get("commune"),
            "latitude": float(x["latitude"]), "longitude": float(x["longitude"]),
            "prix": x["prix"], "distance_m": round(x["distance_m"]),
            "maj_prix": x["maj_prix"].isoformat() if x.get("maj_prix") else None,
        } for x in stations],
    }


@app.get("/api/geocodage")
async def api_geocodage(q: str = Query(..., min_length=3, max_length=200)):
    """Recherche d'adresse via le service de gÃ©ocodage IGN.

    Le navigateur ne contacte pas directement le service IGN : le backend
    joue le rÃ´le de proxy afin de garder une intÃ©gration homogÃ¨ne avec Pelify.
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
        logger.warning("GÃ©ocodage IGN indisponible: %s", exc)
        raise HTTPException(502, "Le service de recherche d'adresse IGN est momentanÃ©ment indisponible.") from exc

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
            "source": "BAN via GÃ©oplateforme",
        })
    return {"resultats": resultats}


async def _itineraire_depuis_point(lieu_depart, etapes, mode, retour_depart=False):
    """Construit un parcours IGN depuis un point arbitraire puis les Ã©tapes.

    lieu_depart doit contenir latitude/longitude et peut avoir id=None.
    """
    if not etapes:
        raise GeoplateformeError("Aucune Ã©tape sÃ©lectionnÃ©e.")

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
            raise GeoplateformeError(f"Aucune gÃ©omÃ©trie IGN pour le tronÃ§on {i + 1}.")
        distance = float(resultat.get("distance_metres") or 0)
        duree = resultat.get("duree_secondes")
        if duree is None:
            duree_disponible = False
        else:
            duree_totale += float(duree)
        distance_totale += distance
        segments.append({
            "depart": {"id": depart.get("id"), "nom": depart.get("nom") or "Point de dÃ©part", "latitude": float(depart["latitude"]), "longitude": float(depart["longitude"])},
            "arrivee": {"id": arrivee.get("id"), "nom": arrivee.get("nom") or "Ã‰tape", "latitude": float(arrivee["latitude"]), "longitude": float(arrivee["longitude"])},
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
    "Sur les traces de {film}" â€” relie tous les lieux de tournage d'un
    film en Occitanie par le trajet EN VOITURE le plus rapide (pas
    juste le plus proche voisin), calculÃ© tronÃ§on par tronÃ§on pour
    rester fiable mÃªme avec beaucoup de lieux (voir
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
        raise HTTPException(400, "Ce film n'a qu'un seul lieu recensÃ© â€” pas de tracÃ© possible.")

    lieux_ordonnes = await _ordre_optimise(lieux)
    resultat = await _itineraire_multi_etapes(lieux_ordonnes, "driving-car")
    resultat["etapes"] = lieux_ordonnes
    resultat["adresses"] = [_adresse_complete(l) for l in lieux_ordonnes]
    return resultat


@app.post("/api/parcours/enrichi")
async def parcours_enrichi(request: Request, response: Response):
    """
    V4 â€” construit un parcours cinÃ©touristique personnalisÃ© Ã  partir
    d'une sÃ©lection de lieux de tournage et renvoie les offres
    touristiques dÃ©jÃ  prÃ©sentes dans amenity_cache autour de chaque
    Ã©tape.

    Le calcul est volontairement basÃ© sur les donnÃ©es dÃ©jÃ  en cache :
    aucune requÃªte Overpass/DATAtourisme en direct depuis une requÃªte
    visiteur.

    Body JSON :
      {
        "lieu_ids": [1, 2, 3],
        "mode": "driving-car",
        "limite_par_categorie": 4
      }

    L'ordre reÃ§u est conservÃ© : c'est le choix de l'utilisateur.
    L'endpoint recalcule ensuite les tronÃ§ons routiers avec la
    GÃ©oplateforme IGN et agrÃ¨ge hÃ©bergements, restaurants, activitÃ©s,
    offices de tourisme et autres catÃ©gories disponibles dans le cache.
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
    inclure_guides = bool(body.get("inclure_guides", True))
    carburant = body.get("carburant") or "e10"          # "aucun" dÃ©sactive le volet carburant
    consommation_l_100km = body.get("consommation_l_100km")
    inclure_carburant = bool(body.get("inclure_carburant", True))
    langue_guide = body.get("langue") or None
    if langue_guide is not None:
        langue_guide = str(langue_guide).strip()[:20] or None
    nb_personnes = body.get("nb_personnes")
    try:
        nb_personnes = int(nb_personnes) if nb_personnes not in (None, "") else None
    except (TypeError, ValueError):
        nb_personnes = None

    if not isinstance(lieu_ids, list):
        raise HTTPException(400, "lieu_ids doit Ãªtre une liste")

    try:
        lieu_ids = list(dict.fromkeys(int(x) for x in lieu_ids))
    except (TypeError, ValueError):
        raise HTTPException(400, "Les identifiants de lieux doivent Ãªtre numÃ©riques")

    if not lieu_ids:
        raise HTTPException(400, "SÃ©lectionnez au moins un lieu")
    if len(lieu_ids) > 30:
        raise HTTPException(400, "Un parcours ne peut pas contenir plus de 30 Ã©tapes")
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
            visites_guidees_nettoyees.append({"film_id": film_id, "nom": str(v.get("nom") or "Visite guidÃ©e"), "duree_minutes": duree, "lien": v.get("lien"), "heure_debut": v.get("heure_debut"), "heure_fin": v.get("heure_fin")})
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
               l.description, l.anecdote,
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
    visites_disponibles = (await creneaux_pour_date(date_sortie, film_ids_parcours)) if date_sortie else []

    # On conserve exactement l'ordre choisi dans l'interface, sauf si
    # l'utilisateur demande explicitement une optimisation sous contrainte de temps.
    etapes = [par_id[x] for x in lieu_ids]
    etapes_originales = list(etapes)
    etapes_exclues_optimisation = []
    if optimiser:
        etapes, etapes_exclues_optimisation = await optimiser_etapes_ign(
            etapes, depart, mode, temps_disponible_minutes, temps_visite_minutes, retour_depart
        )
        if not etapes:
            raise HTTPException(400, "Aucune Ã©tape ne peut tenir dans les critÃ¨res choisis.")

    # Les visites datÃ©es sont traitÃ©es comme de vraies contraintes horaires.
    # Si une visite est disponible Ã  une heure prÃ©cise et que l'optimisation est
    # demandÃ©e, on peut faire passer l'Ã©tape concernÃ©e en prioritÃ©.
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
                # Ordre des Ã©tapes suivantes : plus proche voisin PAR LA ROUTE (IGN).
                etapes = await ordre_plus_proche_voisin_ign([cible] + autres, mode)

    # Guides et mÃ©diateurs dont la zone d'intervention couvre ce parcours.
    # Ne remplace jamais une visite guidÃ©e existante (visites_disponibles) :
    # c'est une piste complÃ©mentaire quand aucune offre packagÃ©e n'existe,
    # ou simplement une option supplÃ©mentaire Ã  cÃ´tÃ©.
    guides_pertinents = (
        await guides_recommandes(etapes, mode=mode, langue=langue_guide, nb_personnes=nb_personnes)
        if inclure_guides else []
    )

    resultat_route = None
    depart_effectif = None
    if depart:
        try:
            depart_lat = float(depart.get("latitude"))
            depart_lon = float(depart.get("longitude"))
            if not (-90 <= depart_lat <= 90 and -180 <= depart_lon <= 180):
                raise ValueError
            depart_effectif = {"id": None, "nom": depart.get("nom") or "Point de dÃ©part", "latitude": depart_lat, "longitude": depart_lon}
        except (TypeError, ValueError, AttributeError):
            raise HTTPException(400, "Point de dÃ©part invalide")

    if depart_effectif:
        try:
            resultat_route = await _itineraire_depuis_point(depart_effectif, etapes, mode, retour_depart)
        except GeoplateformeError as exc:
            raise HTTPException(502, f"ItinÃ©raire IGN indisponible : {exc}") from exc
    elif len(etapes) >= 2:
        try:
            resultat_route = await _itineraire_multi_etapes(etapes, mode)
        except GeoplateformeError as exc:
            raise HTTPException(502, f"ItinÃ©raire IGN indisponible : {exc}") from exc

    # Les catÃ©gories sont celles rÃ©ellement prÃ©sentes dans amenity_cache.
    # Cela permet Ã  V4 d'Ã©voluer sans changer cet endpoint lorsque de
    # nouvelles catÃ©gories sont importÃ©es.
    rows = await fetch_all(
        f"""
        SELECT lieu_tournage_id, categorie, nom, latitude, longitude,
               distance_metres, adresse, telephone, site_web,
               horaires, photo_url, tarif_min, tarif_max, devise,
               capacite, description, note_etoiles, equipements,
               labels_qualite, lien_accessibilite, langues_parlees,
               distance_pied_metres, duree_pied_secondes,
               distance_voiture_metres, duree_voiture_secondes
        FROM amenity_cache
        WHERE lieu_tournage_id IN ({placeholders})
          AND categorie <> 'station_service'   -- les stations ont leur propre volet Â« carburants Â»
        ORDER BY lieu_tournage_id, categorie
        """,
        tuple(lieu_ids),
    )

    # AgrÃ©gation globale sans doublons. Un mÃªme Ã©tablissement peut Ãªtre
    # proche de plusieurs Ã©tapes ; on le garde une seule fois dans la
    # synthÃ¨se globale et on conserve ses Ã©tapes de proximitÃ©.
    categories = {}
    vus = {}
    par_etape = {str(x): [] for x in lieu_ids}

    # Classement des offres : distance de ROUTE IGN (prÃ©calculÃ©e) selon le mode de
    # dÃ©placement ; le vol d'oiseau (distance_metres) ne sert que si elle manque.
    cle_route = "distance_voiture_metres" if mode == "driving-car" else "distance_pied_metres"

    def _distance_classement(r):
        d = r.get(cle_route)
        return d if d is not None else r.get("distance_metres")

    for row in rows:
        categorie = row["categorie"]
        item = dict(row)
        item["lieu_tournage_id"] = int(row["lieu_tournage_id"])
        item["distance_classement_metres"] = _distance_classement(row)
        par_etape[str(item["lieu_tournage_id"])].append(item)

        # ClÃ© stable : osm_id n'est pas toujours disponible, donc on
        # utilise catÃ©gorie + coordonnÃ©es + nom normalisÃ©.
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
                "meilleure_distance_metres": _distance_classement(row),
            }
        else:
            vus[cle]["proche_de"].append(item["lieu_tournage_id"])
            d = _distance_classement(row)
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

    for categorie, items in categories.items():
        for item in items:
            item["score_personnalise"] = _profil_score_offre(item, categorie, budget_level, accessibilite, mode)
            item["tarif_libelle"] = _libelle_tarif(item, categorie)
        items.sort(key=lambda x: (x.get("score_personnalise", 0), -(x.get("meilleure_distance_metres") or 10**9)), reverse=True)
        categories[categorie] = items[:limite]

    # Recommandations individualisÃ©es : score par Ã©tape, sans inventer de
    # lien de rÃ©servation.
    recommandations_par_etape = _recommandations_par_etape(
        etapes, categories, par_etape, budget_level, accessibilite, mode
    )

    # RÃ©duire Ã©galement le dÃ©tail par Ã©tape afin de ne pas envoyer une
    # rÃ©ponse inutilement volumineuse au navigateur.
    # `limite_par_categorie` s'applique PAR catÃ©gorie : avant, les `limite` offres les plus
    # proches toutes catÃ©gories confondues Ã©taient gardÃ©es, et des restaurants proches
    # pouvaient Ã©vincer tous les hÃ©bergements d'une Ã©tape. Les offres dont la distance de
    # route IGN est connue passent avant celles qui n'ont que le vol d'oiseau.
    for cle, items in par_etape.items():
        if categories_interet:
            items = [x for x in items if x.get("categorie") in categories_interet]
        items.sort(key=lambda x: (
            x.get(cle_route) is None,
            x.get("distance_classement_metres") is None,
            x.get("distance_classement_metres") or 0,
        ))
        gardes, compte = [], {}
        for it in items:
            c = it.get("categorie")
            if compte.get(c, 0) >= limite:
                continue
            compte[c] = compte.get(c, 0) + 1
            gardes.append(it)
        par_etape[cle] = gardes

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

    # Planning horaire indicatif : il utilise les durÃ©es IGN des tronÃ§ons et
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
        # Une visite guidÃ©e liÃ©e Ã  une Å“uvre n'est ajoutÃ©e qu'une seule fois,
        # sur la premiÃ¨re Ã©tape de cette Å“uvre prÃ©sente dans le parcours.
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

    # Seules les visites guidÃ©es rÃ©ellement placÃ©es dans le planning comptent.
    duree_visites_guidees = sum(
        int(x.get("temps_visite_minutes") or 0)
        for x in planning_horaire if x.get("type") == "visite_guidee"
    )

    # Calcul final aprÃ¨s construction du planning.
    attente_visites_guidees_minutes = sum(
        int(x.get("attente_minutes") or 0)
        for x in planning_horaire
        if x.get("type") == "visite_guidee"
    )
    duree_totale_estimee = (
        (duree_trajet or 0)
        + duree_visite
        + duree_visites_guidees * 60
        + attente_visites_guidees_minutes * 60
    )
    budget_respecte = (
        None
        if temps_disponible_minutes is None
        else duree_totale_estimee <= temps_disponible_minutes * 60
    )

    # Volet carburant : prix des stations autour du dÃ©part et de chaque Ã©tape.
    carburants = None
    if inclure_carburant and mode == "driving-car":
        try:
            carburants = await plan_carburant(
                depart=depart_effectif, etapes=etapes, planning_horaire=planning_horaire,
                distance_m=(resultat_route or {}).get("distance_metres"),
                carburant=carburant, consommation=consommation_l_100km,
                date_sortie=date_sortie, heure_depart=heure_depart, mode=mode,
            )
        except Exception:
            logging.getLogger(__name__).exception("Volet carburant indisponible")
            carburants = None

    # Budget indicatif : offre la moins chÃ¨re parmi celles dont le tarif est
    # jugÃ© fiable (voir _tarif_fiable), plus le carburant estimÃ©. Pelify ne
    # transforme jamais une absence de tarif en prix inventÃ©.
    budget_items = []
    for categorie in ("restaurant", "activite", "hebergement"):
        candidats = [(_tarif_fiable(x, categorie), x) for x in (categories.get(categorie) or [])]
        candidats = [(p, x) for p, x in candidats if p is not None]
        if candidats:
            prix, item = min(candidats, key=lambda t: t[0])
            budget_items.append({"categorie": categorie, "nom": item.get("nom"), "tarif_min": prix,
                                 "devise": item.get("devise") or "EUR"})
    if carburants and carburants.get("actif") and carburants.get("cout_estime_eur") is not None:
        budget_items.append({
            "categorie": "carburant", "estimation": True, "devise": "EUR",
            "nom": f"{carburants['carburant_libelle']}, {carburants['litres_estimes']} L estimÃ©s",
            "tarif_min": float(carburants["cout_estime_eur"]),
        })
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
        "guides_recommandes": guides_pertinents,
        "inclure_guides": inclure_guides,
        "date_sortie": date_sortie or None,
        "scenario_recommande": {
            "type": "visite_guidee" if guide_planifie else "parcours_personnalise",
            "message": (f"Commencez par {guide_planifie['nom']} Ã  {guide_planifie.get('heure_debut')} puis poursuivez avec les Ã©tapes optimisÃ©es." if guide_planifie and guide_planifie.get("heure_debut") else "Parcours calculÃ© selon vos critÃ¨res."),
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
        "carburants": carburants,
        "labels_categories": _LABELS_CATEGORIE,
        "icones_categories": ICONES_CATEGORIE,
    }

    if resultat_route:
        reponse.update(resultat_route)

    # Chaque calcul devient un parcours de l'historique du visiteur.
    await _enregistrer_parcours_historique(request, response, body, reponse)
    return reponse


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# V5 â€” ANNUAIRE DES GUIDES ET MÃ‰DIATEURS CINÃ‰TOURISTIQUES
#
# MVP volontairement simple : pas de compte guide, pas de rÃ©servation
# ni de messagerie in-app. Un guide est une fiche saisie manuellement
# (via POST, protÃ©gÃ© par ADMIN_TOKEN, ou directement en base), avec
# une zone d'intervention = un point + un rayon en kilomÃ¨tres. Le
# matching (backend/guides.py) filtre par zone puis trie par
# pertinence ; il ne renvoie jamais de score chiffrÃ© au visiteur.
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

@app.get("/api/guides")
async def liste_guides(statut: str = Query("actif")):
    """
    Annuaire public. `statut=actif` (par dÃ©faut) ne montre que les
    fiches publiÃ©es ; `statut=tous` permet Ã  l'admin de voir aussi les
    fiches en_attente/inactives (pas de donnÃ©es sensibles dans cette
    table, donc pas besoin de protÃ©ger la lecture elle-mÃªme).

    Chaque fiche porte en plus : `fonction` et `specialites_libelles` (libellÃ©s prÃªts Ã 
    afficher), `commune`, `code_postal` et `departement` (dÃ©duit du code postal). L'adresse
    postale n'est pas exposÃ©e. Commune et code postal manquants sont complÃ©tÃ©s une fois par
    gÃ©ocodage inverse puis mÃ©morisÃ©s.
    """
    if statut == "tous":
        where, params = "", ()
    else:
        if statut not in {"actif", "inactif", "en_attente"}:
            raise HTTPException(400, "statut invalide")
        where, params = "WHERE statut = %s", (statut,)
    requete = f"""
        SELECT id, nom, type_guide, bio, specialites, langues, publics,
               mobilite, latitude, longitude, rayon_intervention_km,
               capacite_max, tarif_indicatif, site_web, lien_contact,
               photo_url, statut, date_creation{{extra}}
        FROM guides
        {where}
        ORDER BY date_creation DESC
        """
    try:
        lignes = await fetch_all(requete.format(extra=", commune, code_postal"), params)
    except Exception:
        # Colonnes commune/code_postal absentes (migration v29 pas encore appliquÃ©e).
        logging.getLogger(__name__).warning("Colonnes commune/code_postal absentes de guides")
        lignes = await fetch_all(requete.format(extra=""), params)
    lignes = [dict(g) for g in lignes]
    if lignes and "commune" in lignes[0]:
        await completer_localisation(lignes)
    for g in lignes:
        g["fonction"] = fonction_guide(g.get("type_guide"))
        g["specialites_libelles"] = [SPECIALITES_LIBELLES.get(x, x) for x in (g.get("specialites") or [])]
        cp = str(g.get("code_postal") or "").strip()
        g["departement"] = cp[:2] if len(cp) == 5 and cp.isdigit() else None
    return {"guides": lignes, "total": len(lignes)}


@app.post("/api/guides")
async def creer_guide(request: Request):
    """
    CrÃ©ation d'une fiche guide. ProtÃ©gÃ© par un jeton d'administration
    simple (en-tÃªte X-Admin-Token = variable d'env ADMIN_TOKEN) : il
    n'y a pour l'instant aucun compte utilisateur guide, donc pas de
    self-service â€” c'est Pelify (ou l'office de tourisme partenaire)
    qui saisit les fiches vÃ©rifiÃ©es, comme convenu pour le lancement
    limitÃ© Ã  l'Occitanie.
    """
    exiger_admin(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "JSON invalide")

    nom = str(body.get("nom") or "").strip()
    if not nom:
        raise HTTPException(400, "Le nom du guide est obligatoire")

    type_guide = str(body.get("type_guide") or "autre")
    if type_guide not in TYPES_GUIDE_VALIDES:
        raise HTTPException(400, f"type_guide invalide (valeurs possibles : {sorted(TYPES_GUIDE_VALIDES)})")

    try:
        latitude = float(body.get("latitude"))
        longitude = float(body.get("longitude"))
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            raise ValueError
    except (TypeError, ValueError):
        raise HTTPException(400, "latitude/longitude invalides")

    def _liste_valeurs(cle: str, valides: set[str]) -> list[str]:
        valeurs = body.get(cle) or []
        if not isinstance(valeurs, list):
            raise HTTPException(400, f"{cle} doit Ãªtre une liste")
        valeurs = [str(x) for x in valeurs]
        invalides = [x for x in valeurs if x not in valides]
        if invalides:
            raise HTTPException(400, f"{cle} contient des valeurs invalides : {invalides} (valeurs possibles : {sorted(valides)})")
        return valeurs

    specialites = _liste_valeurs("specialites", SPECIALITES_VALIDES)
    mobilite = _liste_valeurs("mobilite", MOBILITES_VALIDES)
    langues = [str(x).strip().lower() for x in (body.get("langues") or []) if str(x).strip()][:10]
    publics = [str(x).strip() for x in (body.get("publics") or []) if str(x).strip()][:10]

    rayon_intervention_km = body.get("rayon_intervention_km")
    try:
        rayon_intervention_km = max(1, min(int(rayon_intervention_km), 200)) if rayon_intervention_km not in (None, "") else 30
    except (TypeError, ValueError):
        rayon_intervention_km = 30

    capacite_max = body.get("capacite_max")
    try:
        capacite_max = max(1, int(capacite_max)) if capacite_max not in (None, "") else None
    except (TypeError, ValueError):
        capacite_max = None

    statut = str(body.get("statut") or "actif")
    if statut not in {"actif", "inactif", "en_attente"}:
        statut = "en_attente"

    guide_id = await execute(
        """
        INSERT INTO guides (
            nom, type_guide, bio, specialites, langues, publics, mobilite,
            latitude, longitude, rayon_intervention_km, capacite_max,
            tarif_indicatif, site_web, lien_contact, photo_url, statut
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        RETURNING id
        """,
        (
            nom, type_guide, body.get("bio"), specialites, langues, publics, mobilite,
            latitude, longitude, rayon_intervention_km, capacite_max,
            body.get("tarif_indicatif"), body.get("site_web"), body.get("lien_contact"),
            body.get("photo_url"), statut,
        ),
    )
    return {"id": guide_id, "message": "Guide crÃ©Ã©"}


@app.post("/api/guides/candidature")
async def candidature_guide(request: Request):
    """
    Formulaire public d'inscription Ã  l'annuaire â€” voir
    frontend/devenir-guide.html. Contrairement Ã  POST /api/guides,
    aucun jeton n'est requis : n'importe qui peut candidater, mais la
    fiche est TOUJOURS crÃ©Ã©e avec statut='en_attente' et n'apparaÃ®t
    donc jamais dans le matching (guides_recommandes ne lit que les
    fiches statut='actif') tant qu'elle n'a pas Ã©tÃ© validÃ©e Ã  la main
    via PATCH /api/guides/{id}/statut.
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "JSON invalide")

    # PiÃ¨ge Ã  robots : un champ cachÃ© cÃ´tÃ© formulaire, que seul un bot
    # remplit habituellement. On ne le dit pas dans le message d'erreur
    # pour ne pas aider Ã  le contourner.
    if str(body.get("site_web_confirmation") or "").strip():
        raise HTTPException(400, "Candidature invalide")

    nom = str(body.get("nom") or "").strip()
    if not nom:
        raise HTTPException(400, "Le nom est obligatoire")
    contact = str(body.get("lien_contact") or "").strip()
    if not contact:
        raise HTTPException(400, "Indiquez un email ou un moyen de contact")

    type_guide = str(body.get("type_guide") or "autre")
    if type_guide not in TYPES_GUIDE_VALIDES:
        type_guide = "autre"

    def _liste_valeurs_publique(cle: str, valides: set[str]) -> list[str]:
        valeurs = body.get(cle) or []
        if not isinstance(valeurs, list):
            return []
        return [str(x) for x in valeurs if str(x) in valides][:10]

    specialites = _liste_valeurs_publique("specialites", SPECIALITES_VALIDES)
    mobilite = _liste_valeurs_publique("mobilite", MOBILITES_VALIDES)
    langues = [str(x).strip().lower() for x in (body.get("langues") or []) if str(x).strip()][:10]
    publics = [str(x).strip() for x in (body.get("publics") or []) if str(x).strip()][:10]

    try:
        latitude = float(body.get("latitude"))
        longitude = float(body.get("longitude"))
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            raise ValueError
    except (TypeError, ValueError):
        raise HTTPException(400, "Indiquez la zone oÃ¹ vous intervenez (latitude/longitude)")

    rayon_intervention_km = body.get("rayon_intervention_km")
    try:
        rayon_intervention_km = max(1, min(int(rayon_intervention_km), 200)) if rayon_intervention_km not in (None, "") else 30
    except (TypeError, ValueError):
        rayon_intervention_km = 30

    capacite_max = body.get("capacite_max")
    try:
        capacite_max = max(1, int(capacite_max)) if capacite_max not in (None, "") else None
    except (TypeError, ValueError):
        capacite_max = None

    guide_id = await execute(
        """
        INSERT INTO guides (
            nom, type_guide, bio, specialites, langues, publics, mobilite,
            latitude, longitude, rayon_intervention_km, capacite_max,
            tarif_indicatif, site_web, lien_contact, photo_url, statut
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        RETURNING id
        """,
        (
            nom, type_guide, body.get("bio"), specialites, langues, publics, mobilite,
            latitude, longitude, rayon_intervention_km, capacite_max,
            body.get("tarif_indicatif"), body.get("site_web"), contact,
            None, "en_attente",
        ),
    )
    return {"id": guide_id, "message": "Merci ! Votre candidature a Ã©tÃ© enregistrÃ©e et sera vÃ©rifiÃ©e avant publication."}


@app.patch("/api/guides/{guide_id}/statut")
async def changer_statut_guide(guide_id: int, request: Request):
    """Publier/dÃ©publier une fiche (ex: passer 'en_attente' Ã  'actif' aprÃ¨s vÃ©rification)."""
    exiger_admin(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "JSON invalide")
    statut = str(body.get("statut") or "")
    if statut not in {"actif", "inactif", "en_attente"}:
        raise HTTPException(400, "statut invalide")
    resultat = await execute(
        "UPDATE guides SET statut = %s WHERE id = %s RETURNING id",
        (statut, guide_id),
    )
    if not resultat:
        raise HTTPException(404, "Guide introuvable")
    return {"id": guide_id, "statut": statut}


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# V5.1 â€” VISITES CINÃ‰TOURISTIQUES (100% BASE DE DONNÃ‰ES)
#
# Depuis migration_v27, plus aucune visite n'est codÃ©e en dur dans
# visites_cinetouristiques.py : tout vient de la table du mÃªme nom.
# MÃªmes principes que pour les guides : lecture publique, Ã©criture
# protÃ©gÃ©e par ADMIN_TOKEN (pas de self-service ici, les crÃ©neaux
# datÃ©s engagent la crÃ©dibilitÃ© de Pelify).
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

def _slugifier_visite(nom: str) -> str:
    import re as _re
    import unicodedata as _ud
    base = _ud.normalize("NFKD", nom).encode("ascii", "ignore").decode("ascii")
    base = _re.sub(r"[^a-zA-Z0-9]+", "-", base).strip("-").lower()
    return base[:140] or "visite"


@app.get("/api/visites")
async def liste_visites(statut: str = Query("actif"), film_id: int | None = Query(None)):
    conditions, params = [], []
    if statut != "tous":
        if statut not in {"actif", "inactif", "en_attente"}:
            raise HTTPException(400, "statut invalide")
        conditions.append("statut = %s"); params.append(statut)
    if film_id is not None:
        conditions.append("film_ids @> %s"); params.append([int(film_id)])
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    lignes = await fetch_all(
        f"""
        SELECT id, slug, film_ids, nom, description, duree_minutes, lien,
               creneaux, creneaux_regles, statut, date_creation
        FROM visites_cinetouristiques
        {where}
        ORDER BY date_creation DESC
        """,
        tuple(params),
    )
    for l in lignes:
        l["creneaux"] = _parser_json(l.get("creneaux")) or []
        l["creneaux_regles"] = _parser_json(l.get("creneaux_regles")) or []
    return {"visites": lignes, "total": len(lignes)}


@app.post("/api/visites")
async def creer_visite(request: Request):
    exiger_admin(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "JSON invalide")

    nom = str(body.get("nom") or "").strip()
    if not nom:
        raise HTTPException(400, "Le nom de la visite est obligatoire")
    try:
        film_ids = list(dict.fromkeys(int(x) for x in (body.get("film_ids") or [])))
    except (TypeError, ValueError):
        raise HTTPException(400, "film_ids doit Ãªtre une liste d'identifiants numÃ©riques")
    if not film_ids:
        raise HTTPException(400, "SÃ©lectionnez au moins un film")
    try:
        duree_minutes = max(1, min(int(body.get("duree_minutes")), 600))
    except (TypeError, ValueError):
        raise HTTPException(400, "duree_minutes invalide")

    creneaux = body.get("creneaux") or []
    creneaux_regles = body.get("creneaux_regles") or []
    if not isinstance(creneaux, list) or not isinstance(creneaux_regles, list):
        raise HTTPException(400, "creneaux et creneaux_regles doivent Ãªtre des listes")

    slug = str(body.get("slug") or _slugifier_visite(nom))
    statut = str(body.get("statut") or "actif")
    if statut not in {"actif", "inactif", "en_attente"}:
        statut = "en_attente"

    try:
        visite_id = await execute(
            """
            INSERT INTO visites_cinetouristiques
                (slug, film_ids, nom, description, duree_minutes, lien,
                 creneaux, creneaux_regles, statut)
            VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s)
            RETURNING id
            """,
            (
                slug, film_ids, nom, body.get("description"), duree_minutes,
                body.get("lien"), json.dumps(creneaux), json.dumps(creneaux_regles),
                statut,
            ),
        )
    except Exception as exc:
        if "unique" in str(exc).lower() or "duplicate" in str(exc).lower():
            raise HTTPException(409, f"Le slug Â« {slug} Â» existe dÃ©jÃ , prÃ©cisez-en un autre.")
        raise
    return {"id": visite_id, "slug": slug, "message": "Visite crÃ©Ã©e"}


@app.patch("/api/visites/{visite_id}")
async def modifier_visite(visite_id: int, request: Request):
    """
    Mise Ã  jour partielle : n'envoyer que les champs Ã  changer. Utile
    pour corriger un crÃ©neau (`creneaux`) sans retaper toute la fiche.
    """
    exiger_admin(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "JSON invalide")

    champs, valeurs = [], []
    if "nom" in body:
        nom = str(body["nom"] or "").strip()
        if not nom:
            raise HTTPException(400, "nom ne peut pas Ãªtre vide")
        champs.append("nom = %s"); valeurs.append(nom)
    if "description" in body:
        champs.append("description = %s"); valeurs.append(body["description"])
    if "duree_minutes" in body:
        try:
            champs.append("duree_minutes = %s"); valeurs.append(max(1, min(int(body["duree_minutes"]), 600)))
        except (TypeError, ValueError):
            raise HTTPException(400, "duree_minutes invalide")
    if "lien" in body:
        champs.append("lien = %s"); valeurs.append(body["lien"])
    if "film_ids" in body:
        try:
            film_ids = list(dict.fromkeys(int(x) for x in (body["film_ids"] or [])))
        except (TypeError, ValueError):
            raise HTTPException(400, "film_ids invalide")
        if not film_ids:
            raise HTTPException(400, "film_ids ne peut pas Ãªtre vide")
        champs.append("film_ids = %s"); valeurs.append(film_ids)
    if "creneaux" in body:
        if not isinstance(body["creneaux"], list):
            raise HTTPException(400, "creneaux doit Ãªtre une liste")
        champs.append("creneaux = %s::jsonb"); valeurs.append(json.dumps(body["creneaux"]))
    if "creneaux_regles" in body:
        if not isinstance(body["creneaux_regles"], list):
            raise HTTPException(400, "creneaux_regles doit Ãªtre une liste")
        champs.append("creneaux_regles = %s::jsonb"); valeurs.append(json.dumps(body["creneaux_regles"]))
    if "statut" in body:
        if body["statut"] not in {"actif", "inactif", "en_attente"}:
            raise HTTPException(400, "statut invalide")
        champs.append("statut = %s"); valeurs.append(body["statut"])

    if not champs:
        raise HTTPException(400, "Aucun champ Ã  mettre Ã  jour")

    valeurs.append(visite_id)
    resultat = await execute(
        f"UPDATE visites_cinetouristiques SET {', '.join(champs)} WHERE id = %s RETURNING id",
        tuple(valeurs),
    )
    if not resultat:
        raise HTTPException(404, "Visite introuvable")
    return {"id": visite_id, "message": "Visite mise Ã  jour"}


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# V4.7 â€” HISTORIQUE, TABLEAU DE BORD ET GÃ‰NÃ‰RATEUR DE PARCOURS
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

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
    """Construit des scÃ©narios gÃ©ographiques lisibles, sans prÃ©tendre optimiser l'itinÃ©raire IGN.

    Le but est de proposer des familles de lieux cohÃ©rentes avant le calcul IGN final.
    On regroupe les candidats autour de plusieurs noyaux gÃ©ographiques et on dÃ©doublonne
    les coordonnÃ©es quasi identiques (un mÃªme lieu peut Ãªtre associÃ© Ã  plusieurs Å“uvres).
    """
    if not candidats:
        return []
    dlat = float(depart.get("latitude")); dlon = float(depart.get("longitude"))
    mode = mode or "driving-car"
    # Rayon de cohÃ©rence du scÃ©nario. Plus serrÃ© Ã  pied.
    rayon = 18000 if mode == "foot-walking" else 55000
    temps_h = max(0.5, float(temps_minutes or 240) / 60.0)
    # Le nombre de lieux qu'un scÃ©nario peut raisonnablement contenir avant IGN.
    max_places_temps = max(2, min(max_etapes, int((temps_minutes or 240) / max(15, int(temps_visite_minutes or 45)))))
    max_places_temps = min(max_places_temps, 6)

    # DÃ©doublonnage gÃ©ographique : un mÃªme point de tournage liÃ© Ã  plusieurs Å“uvres
    # ne doit pas consommer deux Ã©tapes dans un scÃ©nario.
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
    # On garde un vivier raisonnable : assez large pour proposer plusieurs scÃ©narios.
    vivier = uniques[:40]
    if not vivier:
        return []

    # Noyaux : le dÃ©part + les candidats espacÃ©s. Cela produit des scÃ©narios
    # gÃ©ographiques diffÃ©rents plutÃ´t qu'une simple liste triÃ©e par distance.
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
        # Estimation prudente et explicite : visites + une approximation gÃ©ographique.
        distance_approx = 0.0
        prev = (dlat, dlon)
        for x in lieux:
            distance_approx += haversine_metres(prev[0], prev[1], float(x["latitude"]), float(x["longitude"]))
            prev = (float(x["latitude"]), float(x["longitude"]))
        if retour := False:
            distance_approx += haversine_metres(prev[0], prev[1], dlat, dlon)
        # Coefficient de prudence : on ne prÃ©sente jamais ceci comme le temps IGN rÃ©el.
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
            "note": "Estimation indicative avant calcul IGN ; le trajet rÃ©el sera recalculÃ© aprÃ¨s votre sÃ©lection.",
        })
    scenarios.sort(key=lambda s: (not s["compatible_temps"], s["temps_approx_minutes"], s["distance_depuis_depart_metres"]))
    return scenarios[:max_scenarios]


@app.post("/api/parcours/generer")
async def api_generer_parcours(request: Request, response: Response):
    """GÃ©nÃ¨re des possibilitÃ©s/scÃ©narios Ã  partir des critÃ¨res utilisateur.

    Cette Ã©tape ne remplace pas le calcul IGN : elle construit un vivier et des
    scÃ©narios gÃ©ographiquement cohÃ©rents. Le calcul IGN final intervient aprÃ¨s
    sÃ©lection des lieux.
    """
    try:
        body = await request.json()
        lieu_ids = body.get("lieu_ids") or []
        depart = body.get("depart")
        mode = body.get("mode") or "driving-car"
        temps = int(body.get("temps_disponible_minutes") or 240)
        visite = int(body.get("temps_visite_minutes") or 45)
        retour = bool(body.get("retour_depart", False))
        max_etapes = max(2, min(int(body.get("max_etapes") or 8), 15))
        if not depart or depart.get("latitude") is None or depart.get("longitude") is None:
            raise HTTPException(400, "Un point de dÃ©part est nÃ©cessaire pour gÃ©nÃ©rer automatiquement des possibilitÃ©s.")

        # Avec des lieux dÃ©jÃ  choisis, on construit les scÃ©narios sur ce vivier.
        # Sans lieux, on cherche autour du point de dÃ©part.
        if lieu_ids:
            lieu_ids = list(dict.fromkeys(int(x) for x in lieu_ids))[:40]
        else:
            lat, lon = float(depart["latitude"]), float(depart["longitude"])
            delta = 0.20 if mode == "foot-walking" else 0.65
            candidates = await fetch_all(
                """SELECT l.id, l.nom, l.commune, l.departement, l.latitude, l.longitude, l.film_id, f.titre AS film_titre,
                          f.media_type, f.annee, f.poster_url
                   FROM lieux_tournage l LEFT JOIN films f ON f.id = l.film_id
                   WHERE l.latitude BETWEEN %s AND %s AND l.longitude BETWEEN %s AND %s
                     AND l.latitude IS NOT NULL AND l.longitude IS NOT NULL""",
                (lat-delta, lat+delta, lon-delta, lon+delta),
            )
            candidates.sort(key=lambda x: haversine_metres(lat, lon, float(x["latitude"]), float(x["longitude"])))
            lieu_ids = [int(x["id"]) for x in candidates[:60]]

        if not lieu_ids:
            raise HTTPException(404, "Aucun lieu de tournage trouvÃ© autour du dÃ©part.")

        # RequÃªte IN paramÃ©trÃ©e, compatible asyncpg/psycopg selon le driver utilisÃ©.
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

    except HTTPException:
        # DÃ©jÃ  une erreur volontaire (400/404 ci-dessus) : FastAPI la
        # sÃ©rialise dÃ©jÃ  correctement en JSON, on la laisse remonter.
        raise

    except Exception as exc:
        # Toute autre exception non prÃ©vue plantait silencieusement et
        # Starlette renvoyait "Internal Server Error" en texte brut â€”
        # ce que le frontend ne peut pas parser en JSON (d'oÃ¹ l'erreur
        # "Unexpected token 'I', 'Internal S'... is not valid JSON").
        # On journalise la trace complÃ¨te cÃ´tÃ© serveur (visible dans
        # les logs de dÃ©ploiement) et on renvoie une VRAIE erreur JSON,
        # avec le message affichÃ© tel quel cÃ´tÃ© app pour diagnostiquer
        # prÃ©cisÃ©ment la cause au prochain essai.
        logger.exception(
            "Erreur inattendue dans /api/parcours/generer"
        )
        raise HTTPException(
            500,
            f"Erreur lors de la gÃ©nÃ©ration des possibilitÃ©s : {exc}",
        )

# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# PAGES RENDUES CÃ”TÃ‰ SERVEUR (SEO)
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

@app.get("/films/{slug_id}", response_class=HTMLResponse)
async def page_film(request: Request, slug_id: str):
    """
    URL du type /films/le-pacte-des-loups-42. Le slug n'est pas
    utilisÃ© pour la recherche en base (juste l'id final) â€” s'il ne
    correspond pas au slug canonique du film (titre changÃ©, faute de
    frappe dans un lien externe...), on redirige en 301 vers la bonne
    URL plutÃ´t que d'afficher une page dupliquÃ©e sous deux adresses
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
    Inclut uniquement les contenus publiÃ©s en Occitanie.
    Les URLs des films sont gÃ©nÃ©rÃ©es avec la mÃªme logique canonique
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


# Doit rester la DERNIÃˆRE route dÃ©clarÃ©e : sert index.html, style.css,
# app.js, manifest.json, sw.jsâ€¦ Si elle Ã©tait dÃ©clarÃ©e plus haut, elle
# intercepterait toutes les requÃªtes avant que /films/{slug_id},
# /sitemap.xml etc. n'aient une chance de matcher.
app.mount("/", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "..", "frontend"), html=True), name="static")

