"""
backend/seo.py — Utilitaires pour les URLs propres et les données
structurées (Schema.org) des pages film.
"""

import re
import unicodedata


def slugify(texte: str) -> str:
    """
    'Le Pacte des Loups' → 'le-pacte-des-loups'
    Utilisé pour construire /films/{slug}-{id}.
    """
    texte = unicodedata.normalize("NFKD", texte).encode("ascii", "ignore").decode()
    texte = texte.lower()
    texte = re.sub(r"[^a-z0-9]+", "-", texte)
    return texte.strip("-")


def url_film(film: dict) -> str:
    return f"/films/{slugify(film['titre'])}-{film['id']}"


def json_ld_film(film: dict, lieux: list[dict], base_url: str) -> dict:
    """
    Schema.org combiné Movie + liste de TouristAttraction (les lieux).
    Aide Google à comprendre qu'il s'agit d'une œuvre ET d'un contenu
    touristique géolocalisé — les deux angles de recherche possibles
    ("lieux de tournage de X" et "que visiter à X").
    """
    type_schema = "Movie" if film["media_type"] == "movie" else "TVSeries"

    data = {
        "@context": "https://schema.org",
        "@type": type_schema,
        "name": film["titre"],
        "description": film.get("synopsis") or "",
        "url": base_url + url_film(film),
    }
    if film.get("poster_url"):
        data["image"] = film["poster_url"]
    if film.get("annee"):
        data["datePublished"] = str(film["annee"])

    if lieux:
        data["locationCreated"] = [
            {
                "@type": "TouristAttraction",
                "name": lieu["nom"],
                "address": lieu.get("commune"),
                "geo": {
                    "@type": "GeoCoordinates",
                    "latitude": float(lieu["latitude"]),
                    "longitude": float(lieu["longitude"]),
                },
            }
            for lieu in lieux
        ]
    return data


def meta_description(film: dict, lieux: list[dict]) -> str:
    """Meta description unique par film, avec les communes citées (bon signal SEO local)."""
    communes = sorted({l["commune"] for l in lieux if l.get("commune")})
    lieu_txt = ", ".join(communes[:3]) if communes else "Occitanie"
    base = f"Où a été tourné {film['titre']} ? Découvrez les lieux de tournage à {lieu_txt}"
    base += " et tout ce qu'il faut savoir pour les visiter : hébergements, restaurants, transports."
    return base[:160]