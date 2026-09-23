"""
backend/paysages.py — Module "Landscape Studio" : repérage et gestion
de paysages pour projets audiovisuels, greffé sur Pelify.

V1 volontairement sans IA (cf. cahier des charges) : base de paysages
+ métadonnées + recherche multicritère + carte + import Openverse.
Extension dôme 3D : chaque paysage porte un `media_type` (photo /
panorama / 360 / video_360 / a_capturer) et un `type_reference`
(personnelle / artiste / open_source / panorama_360), et chaque scène
porte une `intention_artistique` + un `format_souhaite`. Pas de score
de compatibilité automatique pour l'instant (volontaire — cf. réponse
sur le dispositif de projection non encore spécifié) : on affiche
juste les formats disponibles, le front s'occupe des pictos.

Rattaché à l'identité pseudonyme existante de main.py (cookie
`pelify_user` → table pelify_users) plutôt qu'un second système de
comptes. `_ensure_profile` est dupliquée ici à l'identique (et non
importée depuis main.py) pour éviter un import circulaire, puisque
main.py importe ce module — si tu fais évoluer la logique de profil
dans main.py, reporte le changement ici aussi.
"""

import hashlib
import os
import secrets
import uuid

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from db import execute, fetch_all, fetch_one
from openverse import rechercher_openverse
from overpass import haversine_metres

router = APIRouter(prefix="/api/paysages", tags=["Landscape Studio"])

PELIFY_USER_COOKIE = "pelify_user"
PELIFY_USER_SECRET = os.getenv("PELIFY_USER_SECRET", "pelify-dev-secret-change-me")

STATUTS_LIEN_VALIDES = {"candidat", "selectionne", "alternative", "rejete"}
MEDIA_TYPES_VALIDES = {"photo", "panorama", "360", "video_360", "a_capturer"}
TYPES_REFERENCE_VALIDES = {"personnelle", "artiste", "open_source", "panorama_360"}
STATUTS_DROITS_VALIDES = {"utilisable", "a_negocier", "libre_licence", "a_capturer", "a_verifier"}
FORMATS_SOUHAITES_VALIDES = {"photo", "panorama", "360", "video_360", "peu_importe"}


def _hash_user_token(token: str) -> str:
    return hashlib.sha256((PELIFY_USER_SECRET + ":" + token).encode("utf-8")).hexdigest()


async def _ensure_profile(request: Request, response: Response) -> dict:
    token = request.cookies.get(PELIFY_USER_COOKIE)
    created = False
    if not token or len(token) < 32:
        token = secrets.token_urlsafe(48)
        created = True
    token_hash = _hash_user_token(token)
    user = await fetch_one(
        "SELECT id FROM pelify_users WHERE visitor_token_hash = %s", (token_hash,),
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
            PELIFY_USER_COOKIE, token, max_age=60 * 60 * 24 * 365,
            httponly=True, samesite="lax",
            secure=os.getenv("COOKIE_SECURE", "1") != "0", path="/",
        )
    return user


def _defaut_statut_droits(type_reference: str, source_type: str) -> str:
    """Une image d'artiste n'est jamais présumée utilisable par défaut."""
    if type_reference == "artiste":
        return "a_negocier"
    if source_type == "user" and type_reference == "personnelle":
        return "utilisable"
    return "a_verifier"


# ---------------------------------------------------------------------------
# Schémas
# ---------------------------------------------------------------------------

class ProjetCreation(BaseModel):
    nom: str = Field(..., min_length=1, max_length=255)
    description: str | None = None


class SceneCreation(BaseModel):
    numero: int | None = None
    titre: str | None = None
    description: str | None = None
    besoin_type: str | None = None
    besoin_elements: list[str] = Field(default_factory=list)
    ambiance: str | None = None
    environnement: str | None = None
    epoque: str | None = None
    region: str | None = None
    ville_reference: str | None = None
    distance_max_km: int | None = None
    intention_artistique: str | None = None
    format_souhaite: str = "peu_importe"


class PaysageCreation(BaseModel):
    nom: str = Field(..., min_length=1, max_length=255)
    description: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    type: str | None = None
    environnement: str | None = None
    ambiance: str | None = None
    elements: list[str] = Field(default_factory=list)
    image_url: str | None = None
    thumbnail_url: str | None = None
    source_type: str = "user"
    source_nom: str | None = None
    source_url: str | None = None
    auteur: str | None = None
    auteur_url: str | None = None
    licence: str | None = None
    licence_url: str | None = None
    media_type: str = "photo"
    type_reference: str = "open_source"
    statut_droits: str | None = None  # calculé automatiquement si non fourni
    artiste_nom: str | None = None
    artiste_contact: str | None = None


class PaysageDepuisOpenverse(BaseModel):
    id_openverse: str
    titre: str | None = None
    image_url: str
    thumbnail_url: str | None = None
    auteur: str | None = None
    auteur_url: str | None = None
    licence: str | None = None
    licence_url: str | None = None
    source_nom: str | None = None
    source_url: str | None = None
    # Renseignés par l'utilisateur après import (Openverse ne géolocalise pas)
    nom: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    type: str | None = None
    environnement: str | None = None
    ambiance: str | None = None
    elements: list[str] = Field(default_factory=list)
    media_type: str = "photo"


class PaysageMiseAJour(BaseModel):
    """Édition partielle (PATCH) — tous les champs sont optionnels, seuls
    ceux effectivement fournis par le client sont mis à jour (cf.
    `exclude_unset` dans la route). C'est ce qui manquait pour compléter
    un paysage après import Openverse (géolocalisation, statut de droits)
    ou après ajout manuel rapide."""
    nom: str | None = Field(None, min_length=1, max_length=255)
    description: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    type: str | None = None
    environnement: str | None = None
    ambiance: str | None = None
    elements: list[str] | None = None
    image_url: str | None = None
    thumbnail_url: str | None = None
    auteur: str | None = None
    auteur_url: str | None = None
    licence: str | None = None
    licence_url: str | None = None
    media_type: str | None = None
    type_reference: str | None = None
    statut_droits: str | None = None
    artiste_nom: str | None = None
    artiste_contact: str | None = None


class LienScenePaysage(BaseModel):
    paysage_id: int
    statut: str = "candidat"
    notes: str | None = None


# ---------------------------------------------------------------------------
# Projets
# ---------------------------------------------------------------------------

@router.get("/projets")
async def lister_projets(request: Request, response: Response):
    user = await _ensure_profile(request, response)
    return await fetch_all(
        "SELECT id, nom, description, created_at FROM paysage_projets "
        "WHERE user_id = %s ORDER BY created_at DESC",
        (user["id"],),
    )


@router.post("/projets", status_code=201)
async def creer_projet(payload: ProjetCreation, request: Request, response: Response):
    user = await _ensure_profile(request, response)
    projet_id = await execute(
        "INSERT INTO paysage_projets (user_id, nom, description) "
        "VALUES (%s, %s, %s) RETURNING id",
        (user["id"], payload.nom, payload.description),
    )
    return {"id": projet_id, "nom": payload.nom, "description": payload.description}


@router.get("/projets/{projet_id}")
async def obtenir_projet(projet_id: int, request: Request, response: Response):
    user = await _ensure_profile(request, response)
    projet = await fetch_one(
        "SELECT id, nom, description, created_at FROM paysage_projets "
        "WHERE id = %s AND user_id = %s",
        (projet_id, user["id"]),
    )
    if not projet:
        raise HTTPException(404, "Projet introuvable")
    scenes = await fetch_all(
        "SELECT id, numero, titre, besoin_type, ambiance, format_souhaite, created_at "
        "FROM paysage_scenes WHERE projet_id = %s ORDER BY numero NULLS LAST, created_at",
        (projet_id,),
    )
    projet["scenes"] = scenes
    return projet


@router.delete("/projets/{projet_id}", status_code=204)
async def supprimer_projet(projet_id: int, request: Request, response: Response):
    """Supprime un projet et tout ce qui lui appartient (scènes, liens
    vers des paysages) — mais jamais les paysages eux-mêmes, qui sont
    des fiches de bibliothèque partagées indépendantes du projet.

    Route manquante comme l'était PATCH /paysages/{id} : rien ne
    permettait de supprimer un dossier de projet, seulement d'en créer
    et d'en lister. Suppression explicite des lignes filles plutôt que
    de compter uniquement sur un éventuel ON DELETE CASCADE en base,
    au cas où les contraintes de clé étrangère ne l'auraient pas prévu.
    """
    user = await _ensure_profile(request, response)
    projet = await fetch_one(
        "SELECT id FROM paysage_projets WHERE id = %s AND user_id = %s",
        (projet_id, user["id"]),
    )
    if not projet:
        raise HTTPException(404, "Projet introuvable")

    await execute(
        "DELETE FROM paysage_scene_liens WHERE scene_id IN "
        "(SELECT id FROM paysage_scenes WHERE projet_id = %s)",
        (projet_id,),
    )
    await execute("DELETE FROM paysage_scenes WHERE projet_id = %s", (projet_id,))
    await execute("DELETE FROM paysage_projets WHERE id = %s", (projet_id,))
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Scènes
# ---------------------------------------------------------------------------

async def _verifier_proprietaire_projet(projet_id: int, user_id: str) -> None:
    projet = await fetch_one(
        "SELECT id FROM paysage_projets WHERE id = %s AND user_id = %s",
        (projet_id, user_id),
    )
    if not projet:
        raise HTTPException(404, "Projet introuvable")


@router.post("/projets/{projet_id}/scenes", status_code=201)
async def creer_scene(projet_id: int, payload: SceneCreation, request: Request, response: Response):
    user = await _ensure_profile(request, response)
    await _verifier_proprietaire_projet(projet_id, user["id"])
    if payload.format_souhaite not in FORMATS_SOUHAITES_VALIDES:
        raise HTTPException(400, f"format_souhaite invalide, attendu parmi {sorted(FORMATS_SOUHAITES_VALIDES)}")
    scene_id = await execute(
        """INSERT INTO paysage_scenes
               (projet_id, numero, titre, description, besoin_type, besoin_elements,
                ambiance, environnement, epoque, region, ville_reference, distance_max_km,
                intention_artistique, format_souhaite)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
           RETURNING id""",
        (
            projet_id, payload.numero, payload.titre, payload.description,
            payload.besoin_type, payload.besoin_elements, payload.ambiance,
            payload.environnement, payload.epoque, payload.region,
            payload.ville_reference, payload.distance_max_km,
            payload.intention_artistique, payload.format_souhaite,
        ),
    )
    return {"id": scene_id, **payload.model_dump()}


@router.get("/scenes/{scene_id}")
async def obtenir_scene(scene_id: int, request: Request, response: Response):
    user = await _ensure_profile(request, response)
    scene = await fetch_one(
        """SELECT s.* FROM paysage_scenes s
           JOIN paysage_projets p ON p.id = s.projet_id
           WHERE s.id = %s AND p.user_id = %s""",
        (scene_id, user["id"]),
    )
    if not scene:
        raise HTTPException(404, "Scène introuvable")
    liens = await fetch_all(
        """SELECT l.id AS lien_id, l.statut, l.notes, pa.*
           FROM paysage_scene_liens l
           JOIN paysages pa ON pa.id = l.paysage_id
           WHERE l.scene_id = %s
           ORDER BY l.statut, l.created_at""",
        (scene_id,),
    )
    scene["paysages"] = liens
    return scene


# ---------------------------------------------------------------------------
# Recherche multicritère de paysages déjà enregistrés
# ---------------------------------------------------------------------------

@router.get("")
async def rechercher_paysages(
    type: str | None = None,
    environnement: str | None = None,
    ambiance: str | None = None,
    elements: list[str] = Query(default=[]),
    media_type: str | None = None,
    statut_droits: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
    distance_max_km: float | None = None,
):
    """Recherche multicritère (V1, sans IA) dans les paysages déjà enregistrés."""
    conditions = []
    params: list = []

    if type:
        conditions.append("type = %s")
        params.append(type)
    if environnement:
        conditions.append("environnement = %s")
        params.append(environnement)
    if ambiance:
        conditions.append("ambiance ILIKE %s")
        params.append(f"%{ambiance}%")
    if elements:
        conditions.append("elements && %s")
        params.append(elements)
    if media_type:
        conditions.append("media_type = %s")
        params.append(media_type)
    if statut_droits:
        conditions.append("statut_droits = %s")
        params.append(statut_droits)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    lignes = await fetch_all(
        f"SELECT * FROM paysages {where} ORDER BY created_at DESC LIMIT 100", tuple(params),
    )

    if lat is not None and lon is not None:
        for l in lignes:
            if l["latitude"] is not None and l["longitude"] is not None:
                l["distance_km"] = round(
                    haversine_metres(lat, lon, float(l["latitude"]), float(l["longitude"])) / 1000, 1,
                )
            else:
                l["distance_km"] = None
        if distance_max_km is not None:
            lignes = [l for l in lignes if l["distance_km"] is not None and l["distance_km"] <= distance_max_km]
        lignes.sort(key=lambda l: (l["distance_km"] is None, l["distance_km"]))

    return lignes


@router.get("/carte")
async def paysages_pour_carte(
    type: str | None = None,
    ambiance: str | None = None,
    media_type: str | None = None,
):
    """Liste allégée (id, nom, coords, type, format) pour affichage Leaflet."""
    conditions = ["latitude IS NOT NULL", "longitude IS NOT NULL"]
    params: list = []
    if type:
        conditions.append("type = %s")
        params.append(type)
    if ambiance:
        conditions.append("ambiance ILIKE %s")
        params.append(f"%{ambiance}%")
    if media_type:
        conditions.append("media_type = %s")
        params.append(media_type)
    return await fetch_all(
        f"SELECT id, nom, latitude, longitude, type, ambiance, thumbnail_url, media_type "
        f"FROM paysages WHERE {' AND '.join(conditions)}",
        tuple(params),
    )


@router.get("/{paysage_id}")
async def obtenir_paysage(paysage_id: int):
    paysage = await fetch_one("SELECT * FROM paysages WHERE id = %s", (paysage_id,))
    if not paysage:
        raise HTTPException(404, "Paysage introuvable")
    return paysage


# ---------------------------------------------------------------------------
# Ajout de paysages (import perso ou depuis Openverse)
# ---------------------------------------------------------------------------

def _valider_enums_paysage(media_type: str, type_reference: str, statut_droits: str | None) -> None:
    if media_type not in MEDIA_TYPES_VALIDES:
        raise HTTPException(400, f"media_type invalide, attendu parmi {sorted(MEDIA_TYPES_VALIDES)}")
    if type_reference not in TYPES_REFERENCE_VALIDES:
        raise HTTPException(400, f"type_reference invalide, attendu parmi {sorted(TYPES_REFERENCE_VALIDES)}")
    if statut_droits is not None and statut_droits not in STATUTS_DROITS_VALIDES:
        raise HTTPException(400, f"statut_droits invalide, attendu parmi {sorted(STATUTS_DROITS_VALIDES)}")


@router.post("", status_code=201)
async def creer_paysage(payload: PaysageCreation, request: Request, response: Response):
    user = await _ensure_profile(request, response)
    _valider_enums_paysage(payload.media_type, payload.type_reference, payload.statut_droits)
    statut_droits = payload.statut_droits or _defaut_statut_droits(payload.type_reference, payload.source_type)
    droits_a_verifier = statut_droits in ("a_verifier", "a_negocier")

    paysage_id = await execute(
        """INSERT INTO paysages
               (user_id, nom, description, latitude, longitude, type, environnement,
                ambiance, elements, image_url, thumbnail_url, source_type, source_nom,
                source_url, auteur, auteur_url, licence, licence_url, droits_a_verifier,
                media_type, type_reference, statut_droits, artiste_nom, artiste_contact)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
           RETURNING id""",
        (
            user["id"], payload.nom, payload.description, payload.latitude, payload.longitude,
            payload.type, payload.environnement, payload.ambiance, payload.elements,
            payload.image_url, payload.thumbnail_url, payload.source_type, payload.source_nom,
            payload.source_url, payload.auteur, payload.auteur_url, payload.licence,
            payload.licence_url, droits_a_verifier,
            payload.media_type, payload.type_reference, statut_droits,
            payload.artiste_nom, payload.artiste_contact,
        ),
    )
    return {"id": paysage_id, "statut_droits": statut_droits}


@router.patch("/{paysage_id}")
async def modifier_paysage(
    paysage_id: int, payload: PaysageMiseAJour, request: Request, response: Response,
):
    """Édition partielle d'un paysage existant.

    Route qui manquait purement et simplement : rien côté API ne
    permettait de compléter un paysage après sa création. Résultat
    concret — un import Openverse arrive sans lat/lon (Openverse ne
    géolocalise pas) et sans critères de recherche (type/environnement/
    ambiance), et restait donc invisible sur /carte (qui exige lat/lon
    non nulles) et introuvable via /api/paysages (dont les filtres
    comparent à des colonnes NULL, qui ne matchent jamais).

    Bibliothèque partagée entre tous les visiteurs identifiés (comme la
    recherche GET "" plus haut, qui ne filtre pas non plus par
    utilisateur) : `_ensure_profile` sert seulement à garder une session
    active, pas à restreindre l'édition au créateur de la fiche.
    """
    user = await _ensure_profile(request, response)
    existant = await fetch_one("SELECT * FROM paysages WHERE id = %s", (paysage_id,))
    if not existant:
        raise HTTPException(404, "Paysage introuvable")

    champs = payload.model_dump(exclude_unset=True)
    if not champs:
        raise HTTPException(400, "Aucun champ à mettre à jour")

    media_type = champs.get("media_type", existant["media_type"])
    type_reference = champs.get("type_reference", existant["type_reference"])
    statut_droits = champs.get("statut_droits", existant["statut_droits"])
    _valider_enums_paysage(media_type, type_reference, statut_droits)

    # Si le statut de droits (ou le type de référence, qui pilote son
    # défaut — cf. _defaut_statut_droits) change, on resynchronise le
    # drapeau droits_a_verifier hérité de migration_v21 en plus de la
    # colonne statut_droits plus précise, comme le fait creer_paysage.
    if "statut_droits" in champs or "type_reference" in champs:
        champs["statut_droits"] = statut_droits
        champs["droits_a_verifier"] = statut_droits in ("a_verifier", "a_negocier")

    colonnes = list(champs.keys())
    valeurs = list(champs.values())
    set_clause = ", ".join(f"{c} = %s" for c in colonnes)
    valeurs.append(paysage_id)

    await execute(f"UPDATE paysages SET {set_clause} WHERE id = %s", tuple(valeurs))
    return await fetch_one("SELECT * FROM paysages WHERE id = %s", (paysage_id,))


@router.get("/images/recherche")
async def rechercher_images(
    q: str = Query(..., min_length=2, description="Recherche de paysage (Openverse)"),
    page: int = Query(1, ge=1),
):
    """Proxy vers Openverse (mis en cache Redis) — ne modifie rien en base."""
    try:
        return await rechercher_openverse(q, page=page)
    except Exception as exc:
        raise HTTPException(502, f"Openverse indisponible : {exc}") from exc


@router.post("/depuis-openverse", status_code=201)
async def enregistrer_depuis_openverse(
    payload: PaysageDepuisOpenverse, request: Request, response: Response,
):
    """Enregistre un résultat Openverse comme paysage, avec attribution conservée.

    type_reference='open_source' toujours (c'est la définition même
    d'une image Openverse) — droits jamais présumés "utilisable" sans
    vérification.
    """
    user = await _ensure_profile(request, response)
    if payload.media_type not in MEDIA_TYPES_VALIDES:
        raise HTTPException(400, f"media_type invalide, attendu parmi {sorted(MEDIA_TYPES_VALIDES)}")

    paysage_id = await execute(
        """INSERT INTO paysages
               (user_id, nom, latitude, longitude, type, environnement, ambiance, elements,
                image_url, thumbnail_url, source_type, source_nom, source_url,
                auteur, auteur_url, licence, licence_url, droits_a_verifier,
                media_type, type_reference, statut_droits)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'openverse',%s,%s,%s,%s,%s,%s,TRUE,%s,'open_source','a_verifier')
           RETURNING id""",
        (
            user["id"], payload.nom or payload.titre or "Paysage sans titre",
            payload.latitude, payload.longitude, payload.type, payload.environnement,
            payload.ambiance, payload.elements, payload.image_url, payload.thumbnail_url,
            payload.source_nom, payload.source_url, payload.auteur, payload.auteur_url,
            payload.licence, payload.licence_url, payload.media_type,
        ),
    )
    return {"id": paysage_id}


# ---------------------------------------------------------------------------
# Association scène ↔ paysage
# ---------------------------------------------------------------------------

@router.post("/scenes/{scene_id}/lier", status_code=201)
async def lier_paysage_a_scene(scene_id: int, payload: LienScenePaysage, request: Request, response: Response):
    user = await _ensure_profile(request, response)
    scene = await fetch_one(
        """SELECT s.id FROM paysage_scenes s
           JOIN paysage_projets p ON p.id = s.projet_id
           WHERE s.id = %s AND p.user_id = %s""",
        (scene_id, user["id"]),
    )
    if not scene:
        raise HTTPException(404, "Scène introuvable")
    if payload.statut not in STATUTS_LIEN_VALIDES:
        raise HTTPException(400, f"Statut invalide, attendu parmi {sorted(STATUTS_LIEN_VALIDES)}")

    lien_id = await execute(
        """INSERT INTO paysage_scene_liens (scene_id, paysage_id, statut, notes)
           VALUES (%s, %s, %s, %s)
           ON CONFLICT (scene_id, paysage_id)
           DO UPDATE SET statut = EXCLUDED.statut, notes = EXCLUDED.notes
           RETURNING id""",
        (scene_id, payload.paysage_id, payload.statut, payload.notes),
    )
    return {"id": lien_id, "scene_id": scene_id, "paysage_id": payload.paysage_id, "statut": payload.statut}