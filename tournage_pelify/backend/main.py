"""
backend/main.py — API CinéTour.

Tous les endpoints lisent des données déjà en base (jamais d'appel
Overpass en direct sur une requête visiteur) — voir refresh_cache.py
pour le remplissage du cache.
"""

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from contextlib import asynccontextmanager

from db import init_db_pool, close_db_pool, fetch_all, fetch_one
from backend.overpass import phrase_recommandation

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
    page: int = Query(1, ge=1),
    par_page: int = Query(30, le=100),
):
    offset = (page - 1) * par_page
    conditions = ["region = %s", "statut = 'publie'"]
    params: list = [region]

    if media_type:
        conditions.append("media_type = %s")
        params.append(media_type)

    where = " AND ".join(conditions)
    films = await fetch_all(
        f"""
        SELECT id, titre, titre_original, media_type, annee, poster_url
        FROM films
        WHERE {where}
        ORDER BY titre ASC
        LIMIT %s OFFSET %s
        """,
        (*params, par_page, offset),
    )
    return {"films": films, "page": page}


# ── Détail d'un film + ses lieux de tournage ─────────────────────
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
               latitude, longitude, photo_url
        FROM lieux_tournage
        WHERE film_id = %s
        """,
        (film_id,),
    )
    return {"film": film, "lieux": lieux}


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
               adresse, telephone, site_web, rang
        FROM amenity_cache
        WHERE lieu_tournage_id = %s
        ORDER BY categorie, rang
        """,
        (lieu_id,),
    )

    par_categorie: dict[str, list[dict]] = {}
    for r in rows:
        par_categorie.setdefault(r["categorie"], []).append(r)

    # Phrase de recommandation pour le plus proche de chaque catégorie
    plus_proches = {}
    for categorie, items in par_categorie.items():
        if items:
            top = items[0]  # déjà trié par rang
            label = _LABELS_CATEGORIE.get(categorie, categorie)
            plus_proches[categorie] = phrase_recommandation(
                label, top["nom"], top["distance_metres"]
            )

    return {
        "lieu": lieu,
        "amenities": par_categorie,
        "phrases_recommandation": plus_proches,
    }


@app.get("/api/health")
async def health():
    return {"status": "ok"}