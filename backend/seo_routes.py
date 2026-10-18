"""
backend/seo_routes.py — Référencement naturel (SEO) : robots.txt,
sitemap.xml et page d'index /films.

Même organisation que paysages.py, droits.py, meteo.py… : un `router`
que main.py branche avec app.include_router(seo_router).

Pourquoi ce module existe : la page d'accueil charge ses films en
JavaScript depuis /api/. Il faut donc (1) laisser Googlebot appeler les
routes de LECTURE publiques de l'API, (2) lui donner un plan du site fiable,
(3) lui offrir une page d'index avec de vrais liens vers chaque film.

Note : BASE_URL est aussi défini dans main.py (utilisé par page_film).
Si tu changes de domaine, change-le aux deux endroits (ou via la variable
d'environnement BASE_URL).
"""

import os
import time

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates

from db import fetch_all, fetch_one
from seo import url_film

router = APIRouter(tags=["SEO"])

templates = Jinja2Templates(directory="templates")
BASE_URL = os.getenv("BASE_URL", "https://tournage.pelify.app")
REGION = "Occitanie"

PAR_PAGE_FILMS = 200
_LIBELLES_TYPE = {"movie": "Film", "tv": "Série"}  # tout le reste = « Animé »

# Cache mémoire du sitemap : évite une requête SQL à chaque passage de Google
# et le protège d'une base lente au réveil (cause probable de l'« erreur de
# traitement temporaire » vue dans la Search Console).
_SITEMAP_CACHE = {"xml": None, "ts": 0.0}
_SITEMAP_TTL_S = 6 * 3600  # 6 heures


# ══════════════════════════════════════════════════════════════
# robots.txt
# ══════════════════════════════════════════════════════════════

@router.get("/robots.txt", response_class=PlainTextResponse)
async def robots():
    """
    Le contenu de la page d'accueil (liste des films, carte, filtres) est
    chargé en JavaScript depuis /api/. Si /api/ est interdit à Googlebot,
    Google voit une page vide (« Chargement… ») et ne l'indexe pas.
    On ouvre donc uniquement les routes de LECTURE publiques nécessaires
    à l'affichage ; tout le reste de /api/ reste fermé.
    (Google applique la règle la plus précise : Allow /api/films l'emporte
    sur Disallow /api/.)
    """
    regles_api = """Allow: /
Disallow: /api/
Allow: /api/films
Allow: /api/filtres
Allow: /api/stats
Allow: /api/lieux/tous-points
Allow: /api/analyse
"""
    contenu = f"""User-agent: *
Disallow: /

User-agent: Googlebot
{regles_api}
User-agent: Bingbot
{regles_api}
User-agent: OAI-SearchBot
Allow: /
Disallow: /api/

User-agent: ChatGPT-User
Allow: /
Disallow: /api/

User-agent: PerplexityBot
Allow: /
Disallow: /api/

User-agent: Perplexity-User
Allow: /
Disallow: /api/

Sitemap: {BASE_URL}/sitemap.xml
"""
    return PlainTextResponse(content=contenu, media_type="text/plain")


# ══════════════════════════════════════════════════════════════
# sitemap.xml
# ══════════════════════════════════════════════════════════════

async def _construire_sitemap_xml() -> str:
    films = await fetch_all(
        """
        SELECT id, titre, date_maj
        FROM films
        WHERE statut = 'publie'
          AND region = %s
        ORDER BY id
        """,
        (REGION,),
    )

    urls = [
        f"""  <url>
    <loc>{BASE_URL}/</loc>
    <changefreq>weekly</changefreq>
    <priority>1.0</priority>
  </url>""",
        f"""  <url>
    <loc>{BASE_URL}/films</loc>
    <changefreq>weekly</changefreq>
    <priority>0.9</priority>
  </url>""",
    ]

    # Pages éditoriales publiques à indexer
    for chemin in ("/analyse.html", "/devenir-guide.html"):
        urls.append(
            f"""  <url>
    <loc>{BASE_URL}{chemin}</loc>
  </url>"""
        )

    # Pages films (même logique canonique que /films/{slug}-{id})
    for film in films:
        lastmod = ""
        date_maj = film.get("date_maj")
        if date_maj:
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

    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "\n".join(urls)
        + "\n</urlset>\n"
    )


@router.get("/sitemap.xml")
async def sitemap():
    """
    Sitemap XML dynamique pour Google, avec cache mémoire de 6 h.
    Si la base est indisponible, on resert la dernière version connue
    plutôt que de renvoyer une erreur à Google.
    """
    maintenant = time.monotonic()
    cache_valide = (
        _SITEMAP_CACHE["xml"] is not None
        and (maintenant - _SITEMAP_CACHE["ts"]) < _SITEMAP_TTL_S
    )

    if not cache_valide:
        try:
            _SITEMAP_CACHE["xml"] = await _construire_sitemap_xml()
            _SITEMAP_CACHE["ts"] = maintenant
        except Exception:
            if _SITEMAP_CACHE["xml"] is None:
                raise HTTPException(503, "Sitemap temporairement indisponible")

    return Response(content=_SITEMAP_CACHE["xml"], media_type="application/xml")


# ══════════════════════════════════════════════════════════════
# /films — page d'index rendue côté serveur
# ══════════════════════════════════════════════════════════════

@router.get("/films", response_class=HTMLResponse)
async def page_films_index(request: Request, page: int = Query(1, ge=1)):
    """
    Liste de tous les films, séries et animés publiés en Occitanie, avec un
    VRAI lien <a> vers chaque page /films/{slug}-{id}. Elle donne à Google un
    chemin de découverte indépendant du JavaScript et du sitemap, et du texte
    lisible tout de suite.
    """
    ligne_total = await fetch_one(
        "SELECT COUNT(*) AS n FROM films WHERE statut = 'publie' AND region = %s",
        (REGION,),
    )
    total = int(ligne_total["n"]) if ligne_total else 0
    nb_pages = max(1, -(-total // PAR_PAGE_FILMS))  # division par excès

    if page > nb_pages:
        raise HTTPException(404, "Page introuvable")

    lignes = await fetch_all(
        """
        SELECT f.id, f.titre, f.media_type, f.annee, COUNT(lt.id) AS nb_lieux
        FROM films f
        LEFT JOIN lieux_tournage lt ON lt.film_id = f.id
        WHERE f.statut = 'publie' AND f.region = %s
        GROUP BY f.id
        ORDER BY f.titre
        LIMIT %s OFFSET %s
        """,
        (REGION, PAR_PAGE_FILMS, (page - 1) * PAR_PAGE_FILMS),
    )

    films = [
        {
            "titre": l["titre"],
            "url": url_film(l),
            "type_label": _LIBELLES_TYPE.get(l["media_type"], "Animé"),
            "annee": l.get("annee"),
            "nb_lieux": int(l["nb_lieux"] or 0),
        }
        for l in lignes
    ]

    canonical = f"{BASE_URL}/films" + (f"?page={page}" if page > 1 else "")
    titre_page = "Films, séries et animés tournés en Occitanie | Pelify"
    if page > 1:
        titre_page = f"Films, séries et animés tournés en Occitanie (page {page}) | Pelify"
    meta_desc = (
        f"Liste de {total} films, séries et animés tournés en Occitanie. "
        "Retrouvez les lieux de tournage de chaque œuvre et préparez votre visite : "
        "hébergements, restaurants, transports."
    )[:160]

    decalage = (page - 1) * PAR_PAGE_FILMS
    json_ld = {
        "@context": "https://schema.org",
        "@type": "CollectionPage",
        "name": "Films, séries et animés tournés en Occitanie",
        "url": canonical,
        "inLanguage": "fr",
        "mainEntity": {
            "@type": "ItemList",
            "numberOfItems": total,
            "itemListElement": [
                {
                    "@type": "ListItem",
                    "position": decalage + i + 1,
                    "name": f["titre"],
                    "url": BASE_URL + f["url"],
                }
                for i, f in enumerate(films)
            ],
        },
    }

    return templates.TemplateResponse(
        request,
        "films_index.html",
        {
            "films": films,
            "total": total,
            "page": page,
            "nb_pages": nb_pages,
            "base_url": BASE_URL,
            "canonical": canonical,
            "titre_page": titre_page,
            "meta_desc": meta_desc,
            "json_ld": json_ld,
        },
    )
