"""backend/guides.py - Annuaire des guides et médiateurs cinétouristiques."""
from __future__ import annotations

import logging
import os

import httpx
from fastapi import HTTPException, Request
from db import execute, fetch_all
from overpass import haversine_metres

logger = logging.getLogger(__name__)

ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")

SPECIALITES_VALIDES = {
    "cinema", "series", "histoire", "architecture", "patrimoine",
    "paysage", "culture_populaire", "production_audiovisuelle",
    "photographie", "gastronomie",
}
MOBILITES_VALIDES = {"pied", "velo", "voiture", "transport_collectif"}
TYPES_GUIDE_VALIDES = {
    "guide_conferencier", "mediateur_culturel", "accompagnateur",
    "historien", "passionne_cinema", "autre",
}

FONCTIONS_GUIDE = {
    "guide_conferencier": "Guide-conférencier",
    "mediateur_culturel": "Médiateur culturel",
    "accompagnateur": "Accompagnateur",
    "historien": "Historien",
    "passionne_cinema": "Passionné de cinéma",
    "autre": "Guide / agence de visites",
}
SPECIALITES_LIBELLES = {
    "cinema": "Cinéma", "series": "Séries", "histoire": "Histoire", "architecture": "Architecture",
    "patrimoine": "Patrimoine", "paysage": "Paysage", "culture_populaire": "Culture populaire",
    "production_audiovisuelle": "Production audiovisuelle", "photographie": "Photographie",
    "gastronomie": "Gastronomie",
}
GEOCODAGE_INVERSE_URL = "https://data.geopf.fr/geocodage/reverse"


def fonction_guide(type_guide: str | None) -> str:
    return FONCTIONS_GUIDE.get(str(type_guide or ""), "Guide / médiateur")


async def _localiser(guide: dict) -> tuple[str | None, str | None]:
    """Adresse et commune d'un guide. Lues en base si connues, sinon obtenues
    une fois par géocodage inverse IGN puis mémorisées (le point est celui
    déclaré par le guide : c'est son lieu d'activité, pas forcément son domicile)."""
    if guide.get("adresse") or guide.get("commune"):
        return guide.get("adresse"), guide.get("commune")
    try:
        async with httpx.AsyncClient(timeout=6) as client:
            r = await client.get(GEOCODAGE_INVERSE_URL, params={
                "lon": guide["longitude"], "lat": guide["latitude"], "index": "poi,address", "limit": 1})
            r.raise_for_status()
            props = ((r.json().get("features") or [{}])[0]).get("properties") or {}
    except Exception:
        logger.warning("Géocodage inverse indisponible pour le guide %s", guide.get("id"))
        return None, None
    commune = props.get("city") or props.get("municipality")
    adresse = props.get("label") or None
    if commune or adresse:
        try:
            await execute("UPDATE guides SET adresse = COALESCE(adresse, %s), commune = COALESCE(commune, %s), "
                          "code_postal = COALESCE(code_postal, %s) WHERE id = %s",
                          (adresse, commune, props.get("postcode"), guide["id"]))
        except Exception:
            logger.warning("Mémorisation de l'adresse impossible pour le guide %s", guide.get("id"))
    return adresse, commune


_MODE_VERS_MOBILITE = {
    "foot-walking": "pied",
    "driving-car": "voiture",
    "cycling-regular": "velo",
    "public-transport": "transport_collectif",
}


def exiger_admin(request: Request) -> None:
    if not ADMIN_TOKEN:
        raise HTTPException(503, "Administration des guides non configurée (variable ADMIN_TOKEN manquante)")
    if request.headers.get("X-Admin-Token") != ADMIN_TOKEN:
        raise HTTPException(401, "Jeton d'administration invalide ou absent (en-tête X-Admin-Token)")


def _score_guide(guide: dict, distance_metres: float, mode: str,
                 langue: str | None, nb_personnes: int | None) -> int:
    score = 0
    specialites = set(guide.get("specialites") or [])
    if "cinema" in specialites or "series" in specialites:
        score += 30
    if specialites & {"patrimoine", "histoire", "culture_populaire"}:
        score += 10
    rayon_m = (guide.get("rayon_intervention_km") or 0) * 1000
    if rayon_m:
        score += round(15 * max(0.0, 1 - (distance_metres / rayon_m)))
    mobilite_attendue = _MODE_VERS_MOBILITE.get(mode)
    if mobilite_attendue and mobilite_attendue in set(guide.get("mobilite") or []):
        score += 10
    if langue:
        langues_guide = {str(l).strip().lower() for l in (guide.get("langues") or [])}
        if langue.strip().lower() in langues_guide:
            score += 5
    if nb_personnes and guide.get("capacite_max") and nb_personnes <= int(guide["capacite_max"]):
        score += 5
    return score


# Rayon maximal du repli « guide le plus proche » quand aucun guide ne couvre le parcours.
RAYON_REPLI_M = 250_000


def _etiquette(score: int) -> str:
    if score >= 45:
        return "Très bonne correspondance avec votre parcours"
    if score >= 25:
        return "Correspond à vos critères"
    return "Correspondance partielle avec votre parcours"


async def guides_recommandes(etapes: list[dict], mode: str = "driving-car",
                             langue: str | None = None, nb_personnes: int | None = None,
                             limite: int = 3) -> list[dict]:
    if not etapes:
        return []

    requete = """
        SELECT id, nom, type_guide, bio, specialites, langues, publics,
               mobilite, latitude, longitude, rayon_intervention_km,
               capacite_max, tarif_indicatif, site_web, lien_contact,
               photo_url, source_donnee{extra}
        FROM guides
        WHERE statut = 'actif'
    """
    try:
        guides = await fetch_all(requete.format(extra=", adresse, commune"))
    except Exception:
        # Migration v29 pas encore appliquée : on continue sans adresse en base.
        logger.warning("Colonnes adresse/commune absentes de guides (migration v29 à appliquer)")
        guides = await fetch_all(requete.format(extra=""))
    if not guides:
        return []

    resultats = []
    for guide in guides:
        try:
            glat, glon = float(guide["latitude"]), float(guide["longitude"])
        except (TypeError, ValueError, KeyError):
            continue

        distances, par_etape = [], []
        for ordre, etape in enumerate(etapes, start=1):
            try:
                d = haversine_metres(glat, glon, float(etape["latitude"]), float(etape["longitude"]))
            except (TypeError, ValueError, KeyError):
                continue
            distances.append(d)
            par_etape.append({"ordre": ordre, "nom": etape.get("nom"), "distance_metres": round(d),
                              "distance_km": round(d / 1000.0, 1)})
        if not distances:
            continue

        distance_min = min(distances)
        rayon_m = (guide.get("rayon_intervention_km") or 0) * 1000
        hors_zone = bool(rayon_m and distance_min > rayon_m)
        if hors_zone and distance_min > RAYON_REPLI_M:
            continue

        score = _score_guide(guide, distance_min, mode, langue, nb_personnes)
        resultats.append({
            "id": guide["id"], "nom": guide["nom"], "type_guide": guide["type_guide"],
            "fonction": fonction_guide(guide["type_guide"]),
            "specialites_libelles": [SPECIALITES_LIBELLES.get(s, str(s).replace("_", " ").capitalize())
                                     for s in (guide.get("specialites") or [])],
            "adresse": guide.get("adresse"), "commune": guide.get("commune"),
            "latitude": glat, "longitude": glon, "distances_etapes": par_etape,
            "etape_la_plus_proche": min(par_etape, key=lambda x: x["distance_metres"]),
            "bio": guide.get("bio"), "specialites": guide.get("specialites") or [],
            "langues": guide.get("langues") or [], "publics": guide.get("publics") or [],
            "mobilite": guide.get("mobilite") or [], "tarif_indicatif": guide.get("tarif_indicatif"),
            "site_web": guide.get("site_web"), "lien_contact": guide.get("lien_contact"),
            "photo_url": guide.get("photo_url"),
            "source_donnee": guide.get("source_donnee") or "manuel",
            "distance_metres": round(distance_min), "correspondance": _etiquette(score),
            "hors_zone": hors_zone, "_score": score,
        })

    # Guides dont la zone couvre le parcours d'abord. S'il n'y en a aucun, on
    # propose les plus proches (dans RAYON_REPLI_M), clairement signalés
    # « hors zone » : mieux qu'une section vide, sans faire croire à une
    # couverture.
    dans_zone = [r for r in resultats if not r["hors_zone"]]
    if dans_zone:
        resultats = dans_zone
        resultats.sort(key=lambda x: (-x["_score"], x["distance_metres"], x["nom"].lower()))
    else:
        for r in resultats:
            r["correspondance"] = "Le plus proche de votre parcours - hors de sa zone habituelle, à confirmer avec lui"
        resultats.sort(key=lambda x: (x["distance_metres"], x["nom"].lower()))
    resultats = resultats[:limite]
    for resultat in resultats:
        resultat.pop("_score", None)
        resultat["adresse"], resultat["commune"] = await _localiser(resultat)
    return resultats