"""backend/guides.py — Annuaire des guides et médiateurs cinétouristiques.

MVP volontairement simple, pensé pour un lancement limité à l'Occitanie
avec quelques dizaines de fiches vérifiées à la main (pas d'auto-inscription,
pas de réservation, pas de messagerie in-app) :

  - une fiche guide = un point (zone d'intervention) + un rayon en km ;
  - le matching filtre par zone puis trie par pertinence ;
  - Pelify n'affiche jamais de score chiffré au visiteur, seulement un
    classement déjà trié et une étiquette qualitative — un guide ne
    devient pas "une note sur 100".

Ce module ne fait aucune requête réseau : il lit uniquement la table
`guides` (voir migration_v26.sql / schema.sql).
"""
from __future__ import annotations

import os

from fastapi import HTTPException, Request

from db import fetch_all
from overpass import haversine_metres

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

# Correspondance entre le mode de déplacement du parcours (comme utilisé
# ailleurs dans main.py/api_itineraire) et les valeurs de mobilite du guide.
_MODE_VERS_MOBILITE = {"foot-walking": "pied", "driving-car": "voiture"}


def exiger_admin(request: Request) -> None:
    """Protège les endpoints d'écriture de l'annuaire (pas de compte guide en MVP)."""
    if not ADMIN_TOKEN:
        raise HTTPException(503, "Administration des guides non configurée (variable ADMIN_TOKEN manquante)")
    if request.headers.get("X-Admin-Token") != ADMIN_TOKEN:
        raise HTTPException(401, "Jeton d'administration invalide ou absent (en-tête X-Admin-Token)")


def _score_guide(guide: dict, distance_metres: float, mode: str, langue: str | None, nb_personnes: int | None) -> int:
    """
    Score interne uniquement (jamais exposé). Grille volontairement simple :
    pas besoin d'IA pour démarrer, un score technique additif suffit pour
    trier — voir le barème détaillé dans /areas/tournage.md.
    """
    score = 0
    specialites = set(guide.get("specialites") or [])
    if "cinema" in specialites or "series" in specialites:
        score += 30
    if specialites & {"patrimoine", "histoire", "culture_populaire"}:
        score += 10

    rayon_m = (guide.get("rayon_intervention_km") or 0) * 1000
    if rayon_m:
        proximite = max(0.0, 1 - (distance_metres / rayon_m))
        score += round(15 * proximite)

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


def _etiquette(score: int) -> str:
    """Traduit le score en phrase, jamais en pourcentage affiché."""
    if score >= 45:
        return "Très bonne correspondance avec votre parcours"
    if score >= 25:
        return "Correspond à vos critères"
    return "Spécialisé cinéma et lieux de tournage"


async def guides_recommandes(
    etapes: list[dict],
    mode: str = "driving-car",
    langue: str | None = None,
    nb_personnes: int | None = None,
    limite: int = 3,
) -> list[dict]:
    """
    Retourne jusqu'à `limite` guides dont la zone d'intervention couvre
    au moins une étape du parcours, triés par pertinence décroissante.

    `etapes` : liste de dicts avec au minimum `latitude`/`longitude`
    (le même format que les lignes de `lieux_tournage` utilisées dans
    main.py::parcours_enrichi).
    """
    if not etapes:
        return []

    guides = await fetch_all(
        """
        SELECT id, nom, type_guide, bio, specialites, langues, publics,
               mobilite, latitude, longitude, rayon_intervention_km,
               capacite_max, tarif_indicatif, site_web, lien_contact,
               photo_url
        FROM guides
        WHERE statut = 'actif'
        """
    )
    if not guides:
        return []

    resultats = []
    for guide in guides:
        try:
            glat, glon = float(guide["latitude"]), float(guide["longitude"])
        except (TypeError, ValueError):
            continue

        distance_min = min(
            haversine_metres(glat, glon, float(e["latitude"]), float(e["longitude"]))
            for e in etapes
        )

        rayon_m = (guide.get("rayon_intervention_km") or 0) * 1000
        if rayon_m and distance_min > rayon_m:
            continue

        score = _score_guide(guide, distance_min, mode, langue, nb_personnes)
        resultats.append({
            "id": guide["id"],
            "nom": guide["nom"],
            "type_guide": guide["type_guide"],
            "bio": guide.get("bio"),
            "specialites": guide.get("specialites") or [],
            "langues": guide.get("langues") or [],
            "mobilite": guide.get("mobilite") or [],
            "tarif_indicatif": guide.get("tarif_indicatif"),
            "site_web": guide.get("site_web"),
            "lien_contact": guide.get("lien_contact"),
            "photo_url": guide.get("photo_url"),
            "distance_metres": round(distance_min),
            "correspondance": _etiquette(score),
            "_score": score,
        })

    resultats.sort(key=lambda x: x["_score"], reverse=True)
    for r in resultats:
        del r["_score"]
    return resultats[:limite]