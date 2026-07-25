"""
backend/main.py — API CinéTour.

Tous les endpoints lisent des données déjà en base (jamais d'appel
Overpass en direct sur une requête visiteur) — voir refresh_cache.py
pour le remplissage du cache.
"""

import os
import json
import asyncio
import itertools
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
    lieux_isoles = d["lieux_sans_hebergement_5km"] or 0
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


@app.get("/api/analyse/accessibilite")
async def analyse_accessibilite(region: str = Query("Occitanie"), limite: int = Query(20)):
    """
    Pour les films/séries les plus connus (popularité TMDB), analyse
    concrète de l'accessibilité réelle en voiture aux commodités
    essentielles (hébergement, restaurant) — pas juste "il y a des
    commodités", mais "combien de temps pour s'y rendre en voiture".
    Sépare le contenu français du reste, comme demandé.
    """
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

    resultat = {"francais": [], "autres": []}

    for film in films:
        lieux = await fetch_all(
            "SELECT id, nom FROM lieux_tournage WHERE film_id = %s", (film["id"],)
        )
        if not lieux:
            continue
        lieu_ids = [l["id"] for l in lieux]

        # Le meilleur accès voiture, tous lieux du film confondus (le
        # touriste choisira naturellement le lieu le plus accessible
        # s'il y en a plusieurs).
        meilleur_heberg = await fetch_one(
            """
            SELECT nom, distance_voiture_metres, duree_voiture_secondes
            FROM amenity_cache
            WHERE lieu_tournage_id = ANY(%s) AND categorie = 'hebergement' AND rang = 1
            ORDER BY duree_voiture_secondes ASC NULLS LAST LIMIT 1
            """,
            (lieu_ids,),
        )
        meilleur_resto = await fetch_one(
            """
            SELECT nom, distance_voiture_metres, duree_voiture_secondes
            FROM amenity_cache
            WHERE lieu_tournage_id = ANY(%s) AND categorie = 'restaurant' AND rang = 1
            ORDER BY duree_voiture_secondes ASC NULLS LAST LIMIT 1
            """,
            (lieu_ids,),
        )

        # Nombre total de commodités trouvées dans le rayon (densité
        # réelle, pas seulement le top 10 affiché) — un lieu avec 2
        # hébergements dans son rayon n'a pas la même marge de
        # manœuvre qu'un lieu qui en compte 80.
        stats_globales = await fetch_all(
            """
            SELECT categorie, SUM(nombre_total) AS total
            FROM amenity_stats
            WHERE lieu_tournage_id = ANY(%s) AND categorie IN ('hebergement', 'restaurant', 'office_tourisme', 'parking')
            GROUP BY categorie
            """,
            (lieu_ids,),
        )
        nb_par_categorie = {s["categorie"]: s["total"] for s in stats_globales}

        etiquette_heberg, action_heberg = _classer_accessibilite(
            meilleur_heberg["duree_voiture_secondes"] if meilleur_heberg else None
        )
        etiquette_resto, action_resto = _classer_accessibilite(
            meilleur_resto["duree_voiture_secondes"] if meilleur_resto else None
        )

        entree = {
            "id": film["id"], "titre": film["titre"], "annee": film["annee"],
            "media_type": film["media_type"], "poster_url": film["poster_url"],
            "nombre_lieux": len(lieux),
            "hebergement": {
                "nom": meilleur_heberg["nom"] if meilleur_heberg else None,
                "duree_minutes": round(meilleur_heberg["duree_voiture_secondes"] / 60) if meilleur_heberg and meilleur_heberg["duree_voiture_secondes"] else None,
                "distance_metres": meilleur_heberg["distance_voiture_metres"] if meilleur_heberg else None,
                "nombre_total_rayon": nb_par_categorie.get("hebergement", 0),
                "etiquette": etiquette_heberg, "action": action_heberg,
            },
            "restaurant": {
                "nom": meilleur_resto["nom"] if meilleur_resto else None,
                "duree_minutes": round(meilleur_resto["duree_voiture_secondes"] / 60) if meilleur_resto and meilleur_resto["duree_voiture_secondes"] else None,
                "distance_metres": meilleur_resto["distance_voiture_metres"] if meilleur_resto else None,
                "nombre_total_rayon": nb_par_categorie.get("restaurant", 0),
                "etiquette": etiquette_resto, "action": action_resto,
            },
            "office_tourisme_total": nb_par_categorie.get("office_tourisme", 0),
            "parking_total": nb_par_categorie.get("parking", 0),
        }

        categorie_liste = "francais" if film["nationalite"] and "Français" in film["nationalite"] else "autres"
        resultat[categorie_liste].append(entree)

    return resultat


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
               adresse, telephone, site_web, horaires, photo_url, rang,
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
    chemin_coords = ";".join(f"{lon:.7f},{lat:.7f}" for lon, lat in coords_lonlat)

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
                "trajets": [
                    {"distance_metres": round(leg["distance"]), "duree_secondes": round(leg["duration"])}
                    for leg in route.get("legs", [])
                ],
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


async def _ordre_optimise(lieux: list[dict]) -> list[dict]:
    """
    Pour un petit nombre de lieux (≤ 8), teste TOUTES les combinaisons
    d'ordre possibles et garde celle qui minimise le temps de trajet
    total en voiture (via la matrice OSRM, un seul appel réseau quel
    que soit le nombre de permutations testées ensuite). Au-delà de 8
    lieux, le nombre de permutations explose (9! = 362880) — on
    retombe sur l'heuristique du plus proche voisin, plus rapide bien
    que légèrement moins optimale.
    """
    if len(lieux) > 8:
        return _ordre_plus_proche_voisin(lieux)

    points = [(float(l["latitude"]), float(l["longitude"])) for l in lieux]
    matrice = await _table_complete_osrm(points)
    if matrice is None:
        return _ordre_plus_proche_voisin(lieux)

    meilleur_ordre = None
    meilleure_duree = float("inf")
    for permutation in itertools.permutations(range(len(lieux))):
        duree = sum(matrice[permutation[i]][permutation[i + 1]] for i in range(len(permutation) - 1))
        if duree < meilleure_duree:
            meilleure_duree = duree
            meilleur_ordre = permutation

    return [lieux[i] for i in meilleur_ordre]


async def _table_complete_osrm(points: list[tuple[float, float]]) -> list[list[float]] | None:
    """Matrice complète des durées (secondes) entre chaque paire de points, en voiture."""
    coords = ";".join(f"{lon:.7f},{lat:.7f}" for lat, lon in points)
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(f"{OSRM_URL}/table/v1/driving/{coords}", params={"annotations": "duration"})
            resp.raise_for_status()
            data = resp.json()
        return data["durations"]
    except Exception as e:
        print(f"⚠️ Table OSRM indisponible (trace complète): {e}", flush=True)
        return None


def _adresse_complete(lieu: dict) -> str:
    return ", ".join(p for p in (lieu["nom"], lieu.get("commune"), lieu.get("departement")) if p)


@app.get("/api/films/{film_id}/trace")
async def trace_film(film_id: int):
    """
    "Sur les traces de {film}" — relie tous les lieux de tournage d'un
    film en Occitanie par le trajet EN VOITURE le plus rapide (pas
    juste le plus proche voisin), via OSRM. Repli en lignes droites,
    clairement annoncé comme estimation, si OSRM est indisponible.
    """
    lieux = await fetch_all(
        "SELECT id, nom, commune, departement, latitude, longitude FROM lieux_tournage WHERE film_id = %s",
        (film_id,),
    )
    if len(lieux) < 2:
        raise HTTPException(400, "Ce film n'a qu'un seul lieu recensé — pas de tracé possible.")

    lieux_ordonnes = await _ordre_optimise(lieux)
    coords_lonlat = [[float(l["longitude"]), float(l["latitude"])] for l in lieux_ordonnes]

    resultat_osrm = await _itineraire_osrm(coords_lonlat, "driving-car")
    if resultat_osrm:
        resultat_osrm["etapes"] = lieux_ordonnes
        resultat_osrm["adresses"] = [_adresse_complete(l) for l in lieux_ordonnes]
        return resultat_osrm

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
        "adresses": [_adresse_complete(l) for l in lieux_ordonnes],
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