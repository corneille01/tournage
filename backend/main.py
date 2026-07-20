"""
backend/main.py — API CinéTour.

Tous les endpoints lisent des données déjà en base (jamais d'appel
Overpass en direct sur une requête visiteur) — voir refresh_cache.py
pour le remplissage du cache.
"""

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from contextlib import asynccontextmanager

from db import init_db_pool, close_db_pool, fetch_all, fetch_one
from overpass import phrase_recommandation
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
        "stats": stats_par_categorie,
        "phrases_recommandation": plus_proches,
    }


@app.get("/api/health")
async def health():
    return {"status": "ok"}


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