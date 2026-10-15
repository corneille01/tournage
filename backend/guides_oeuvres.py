"""
backend/guides_oeuvres.py

Guides rattachés à des œuvres (films / séries / animés).

  GET  /api/oeuvres                      liste légère de toutes les œuvres publiées (formulaire guide)
  GET  /api/films/{film_id}/guides       guides qui accompagnent cette œuvre, avec leur distance
                                         de ROUTE jusqu'au lieu de tournage (?lieu_id=…)
  enregistrer_oeuvres_guide()            appelée par la candidature : liens guide ↔ œuvres et
                                         création des œuvres proposées (statut 'brouillon')

Distances : table `distances_routieres` (précalcul IGN quotidien, voir
precalculer_distances_routieres.py) ; à défaut, un appel IGN en direct mis en cache ; à défaut,
une estimation clairement signalée. Le vol d'oiseau n'est jamais présenté comme une distance
de route.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
import unicodedata

from fastapi import APIRouter, HTTPException, Query

from db import fetch_all, fetch_one, execute, executemany
from distances_ign import charger_distances_guides, mesure_ou_estimation, estimation
from guides import fonction_guide, SPECIALITES_LIBELLES

logger = logging.getLogger(__name__)
router = APIRouter()

MAX_OEUVRES_PAR_GUIDE = 60
MAX_PROPOSITIONS = 5
TYPES_MEDIA = {"movie", "tv", "anime"}
_CACHE_OEUVRES: dict = {"t": 0.0, "data": None}
TTL_OEUVRES_S = 600


def normaliser(texte) -> str:
    """minuscules, sans accents ni ponctuation : « L'Île aux pachydermes » → « l ile aux pachydermes »."""
    t = unicodedata.normalize("NFKD", str(texte or "")).encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


# ─────────────────────────────────────────────────────────────
# Liste des œuvres (pour la recherche rapide du formulaire)
# ─────────────────────────────────────────────────────────────

@router.get("/api/oeuvres")
async def liste_oeuvres():
    maintenant = time.monotonic()
    if _CACHE_OEUVRES["data"] is not None and maintenant - _CACHE_OEUVRES["t"] < TTL_OEUVRES_S:
        return _CACHE_OEUVRES["data"]
    lignes = await fetch_all(
        "SELECT id, titre, titre_original, annee, media_type FROM films "
        "WHERE statut = 'publie' ORDER BY titre"
    )
    oeuvres = [{
        "id": int(r["id"]), "titre": r["titre"], "annee": r["annee"], "type": r["media_type"],
        # texte de recherche pré-normalisé : le navigateur filtre instantanément, sans accents
        "recherche": normaliser(f"{r['titre']} {r.get('titre_original') or ''} {r['annee'] or ''}"),
    } for r in lignes]
    data = {"oeuvres": oeuvres, "total": len(oeuvres)}
    _CACHE_OEUVRES.update(t=maintenant, data=data)
    return data


# ─────────────────────────────────────────────────────────────
# Enregistrement des choix d'un guide (appelée par la candidature)
# ─────────────────────────────────────────────────────────────

def _liste_ids(valeur) -> list[int]:
    if not isinstance(valeur, list):
        return []
    ids: list[int] = []
    for x in valeur:
        try:
            n = int(x)
        except (TypeError, ValueError):
            continue
        if n > 0 and n not in ids:
            ids.append(n)
    return ids[:MAX_OEUVRES_PAR_GUIDE]


def _nettoyer_propositions(valeur) -> list[dict]:
    if not isinstance(valeur, list):
        return []
    propositions, vus = [], set()
    for x in valeur:
        if not isinstance(x, dict):
            continue
        titre = re.sub(r"\s+", " ", str(x.get("titre") or "")).strip()[:255]
        if len(titre) < 2 or normaliser(titre) in vus:
            continue
        vus.add(normaliser(titre))
        try:
            annee = int(x.get("annee"))
            annee = annee if 1888 <= annee <= 2100 else None
        except (TypeError, ValueError):
            annee = None
        media = str(x.get("type") or "movie")
        propositions.append({"titre": titre, "annee": annee, "type": media if media in TYPES_MEDIA else "movie"})
    return propositions[:MAX_PROPOSITIONS]


async def enregistrer_oeuvres_guide(guide_id: int, film_ids, oeuvres_proposees) -> dict:
    """Relie le guide aux œuvres choisies et crée (en brouillon) celles qu'il propose et que la
    base ne connaît pas encore. Une œuvre déjà présente (même titre, même année si connue) est
    réutilisée : aucun doublon."""
    ids = _liste_ids(film_ids)
    if ids:
        existants = {int(r["id"]) for r in await fetch_all("SELECT id FROM films WHERE id = ANY(%s)", (ids,))}
        ids = [i for i in ids if i in existants]

    crees, reutilises = [], []
    propositions = _nettoyer_propositions(oeuvres_proposees)
    if propositions:
        connus = await fetch_all("SELECT id, titre, titre_original, annee FROM films")
        for p in propositions:
            cle = normaliser(p["titre"])
            trouve = None
            for f in connus:
                memes_titres = cle in (normaliser(f["titre"]), normaliser(f.get("titre_original")))
                if memes_titres and (p["annee"] is None or f["annee"] is None or int(f["annee"]) == p["annee"]):
                    trouve = int(f["id"])
                    break
            if trouve is not None:
                reutilises.append(p["titre"])
                if trouve not in ids:
                    ids.append(trouve)
                continue
            nouvel_id = await execute(
                """
                INSERT INTO films (titre, media_type, annee, source_donnee, statut, proposee_par_guide)
                VALUES (%s, %s, %s, 'manuel', 'brouillon', TRUE)
                RETURNING id
                """,
                (p["titre"], p["type"], p["annee"]),
            )
            connus.append({"id": nouvel_id, "titre": p["titre"], "titre_original": None, "annee": p["annee"]})
            crees.append(p["titre"])
            ids.append(int(nouvel_id))

    if ids:
        await executemany(
            "INSERT INTO guide_films (guide_id, film_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            [(int(guide_id), int(i)) for i in ids],
        )
        _CACHE_OEUVRES["data"] = None   # la liste publique ne change pas (brouillons exclus), mais restons prudents
    return {"oeuvres_liees": len(ids), "oeuvres_creees": crees, "oeuvres_existantes_reutilisees": reutilises}


# ─────────────────────────────────────────────────────────────
# Guides d'une œuvre, avec la distance de route jusqu'au lieu
# ─────────────────────────────────────────────────────────────

@router.get("/api/films/{film_id}/guides")
async def guides_du_film(film_id: int, lieu_id: int | None = Query(None)):
    film = await fetch_one("SELECT id, titre FROM films WHERE id = %s AND statut = 'publie'", (film_id,))
    if not film:
        raise HTTPException(404, "Œuvre introuvable")

    lieux = await fetch_all(
        "SELECT id, nom, latitude, longitude FROM lieux_tournage WHERE film_id = %s", (film_id,))
    if lieu_id is not None:
        lieux = [l for l in lieux if int(l["id"]) == int(lieu_id)]
        if not lieux:
            raise HTTPException(404, "Ce lieu n'appartient pas à cette œuvre")
    if not lieux:
        return {"film_id": film_id, "lieu_id": lieu_id, "guides": [], "total": 0}

    colonnes = ("g.id, g.nom, g.type_guide, g.bio, g.specialites, g.langues, g.publics, g.mobilite, "
                "g.latitude, g.longitude, g.rayon_intervention_km, g.capacite_max, g.tarif_indicatif, "
                "g.site_web, g.lien_contact, g.photo_url")
    requete = (f"SELECT {colonnes}{{extra}} FROM guides g JOIN guide_films gf ON gf.guide_id = g.id "
               "WHERE gf.film_id = %s AND g.statut = 'actif'")
    try:
        guides = await fetch_all(requete.format(extra=", g.commune"), (film_id,))
    except Exception:  # colonne commune absente (migration v29 non appliquée)
        guides = await fetch_all(requete.format(extra=""), (film_id,))
    guides = [dict(g) for g in guides]
    if not guides:
        return {"film_id": film_id, "lieu_id": lieu_id, "guides": [], "total": 0}

    # 1) trajets précalculés  2) appel IGN en direct (si un seul lieu)  3) estimation signalée
    base = await charger_distances_guides([g["id"] for g in guides], [l["id"] for l in lieux])

    async def meilleure_mesure(g: dict) -> tuple[dict, int]:
        candidats = []
        for l in lieux:
            m = base.get((int(g["id"]), int(l["id"])))
            if m is None and len(lieux) == 1:
                m = await mesure_ou_estimation(
                    (float(g["latitude"]), float(g["longitude"])),
                    (float(l["latitude"]), float(l["longitude"])), "driving-car")
            elif m is None:
                m = estimation((float(g["latitude"]), float(g["longitude"])),
                               (float(l["latitude"]), float(l["longitude"])), "driving-car")
            candidats.append((m, int(l["id"])))
        return min(candidats, key=lambda c: c[0]["distance_metres"])

    mesures = await asyncio.gather(*(meilleure_mesure(g) for g in guides[:40]))
    sortie = []
    for g, (m, lieu_proche) in zip(guides, mesures):
        rayon_m = (g.get("rayon_intervention_km") or 0) * 1000
        sortie.append({
            "id": g["id"], "nom": g["nom"], "fonction": fonction_guide(g.get("type_guide")),
            "bio": g.get("bio"), "langues": g.get("langues") or [],
            "specialites_libelles": [SPECIALITES_LIBELLES.get(x, x) for x in (g.get("specialites") or [])],
            "publics": g.get("publics") or [], "mobilite": g.get("mobilite") or [],
            "commune": g.get("commune"), "photo_url": g.get("photo_url"),
            "capacite_max": g.get("capacite_max"), "tarif_indicatif": g.get("tarif_indicatif"),
            "site_web": g.get("site_web"), "lien_contact": g.get("lien_contact"),
            "rayon_intervention_km": g.get("rayon_intervention_km"),
            "distance_metres": m["distance_metres"], "duree_secondes": m.get("duree_secondes"),
            "distance_source": m.get("source", "ign"),          # "ign" | "estimee"
            "lieu_le_plus_proche_id": lieu_proche,
            "hors_zone": bool(rayon_m and m["distance_metres"] > rayon_m),
        })
    sortie.sort(key=lambda x: (x["distance_metres"], x["nom"].lower()))
    if sortie:
        sortie[0]["plus_proche"] = True
    return {"film_id": film_id, "film_titre": film["titre"], "lieu_id": lieu_id, "guides": sortie, "total": len(sortie)}