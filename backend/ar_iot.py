"""
backend/ar_iot.py

Socle « pendant la visite » : réalité augmentée (AR) et objets connectés (IoT).
Aucune application AR n'existe encore : ces routes fixent le CONTRAT que les futurs clients utiliseront
(application mobile, WebXR, lunettes), pour ne rien avoir à refaire côté serveur le moment venu.

  GET  /api/ar/lieux-proches?lat=&lon=&rayon_m=      points d'intérêt géo-ancrés autour de la personne
                                                     (distance et cap calculés ; contenu à afficher)
  GET  /api/ar/lieux/{lieu_id}                       points d'intérêt d'un lieu
  POST /api/ar/points            (admin)             créer un point d'intérêt géo-ancré
  POST /api/iot/appareils        (admin)             enregistrer un appareil, renvoie sa clé (une seule fois)
  POST /api/iot/mesures          (appareil)          envoi de mesures anonymes, signées par la clé de l'appareil
  GET  /api/iot/lieux/{lieu_id}/affluence            dernière mesure d'affluence (ex. : « calme / animé »)

Vie privée : les mesures IoT sont AGRÉGÉES et ANONYMES (nombre de passages, température…). Jamais d'identifiant
d'appareil personnel, d'adresse MAC, d'image ni de position individuelle. Le serveur refuse les types inconnus.
"""
from __future__ import annotations

import hashlib
import hmac
import math
import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Query, Request

from db import execute, fetch_all, fetch_one

router = APIRouter()
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")
TYPES_MESURE = {"passages": "passages", "affluence": "pers", "temperature": "°C", "humidite": "%", "bruit": "dB", "co2": "ppm", "batterie": "%"}
RAYON_MAX_M = 2000


def _exiger_admin(request: Request) -> None:
    if not ADMIN_TOKEN:
        raise HTTPException(503, "Administration non configurée (ADMIN_TOKEN manquant)")
    if not hmac.compare_digest(request.headers.get("X-Admin-Token", "").encode(), ADMIN_TOKEN.encode()):
        raise HTTPException(401, "Jeton d'administration invalide")


def _hash_cle(cle: str) -> str:
    return hashlib.sha256(cle.encode()).hexdigest()


def distance_et_cap(lat1: float, lon1: float, lat2: float, lon2: float) -> tuple[float, float]:
    """Distance (m) et cap (° par rapport au nord, 0-360) de (lat1, lon1) vers (lat2, lon2)."""
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi, dlam = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlam / 2) ** 2
    dist = 2 * r * math.asin(math.sqrt(a))
    y = math.sin(dlam) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dlam)
    return dist, (math.degrees(math.atan2(y, x)) + 360) % 360


def _poi(r: dict, lat: float | None = None, lon: float | None = None) -> dict:
    out = {"id": r["id"], "lieu_id": r["lieu_id"], "lieu": r.get("lieu_nom"), "nom": r["nom"], "type": r["type"],
           "position": {"latitude": r["latitude"], "longitude": r["longitude"], "altitude_m": r["altitude_m"]},
           "orientation_deg": r["orientation_deg"], "rayon_declenchement_m": r["rayon_declenchement_m"],
           "contenu": {"type": r["contenu_type"], "url": r["contenu_url"], "texte": r["contenu_texte"], "licence": r["licence"]}}
    if lat is not None:
        d, cap = distance_et_cap(lat, lon, r["latitude"], r["longitude"])
        out.update({"distance_m": round(d), "cap_deg": round(cap), "declenche": d <= r["rayon_declenchement_m"]})
    return out


_COLS = ("p.id, p.lieu_id, l.nom AS lieu_nom, p.nom, p.type, p.latitude, p.longitude, p.altitude_m, p.orientation_deg, "
         "p.rayon_declenchement_m, p.contenu_type, p.contenu_url, p.contenu_texte, p.licence")


@router.get("/api/ar/lieux-proches")
async def lieux_proches(lat: float = Query(..., ge=-90, le=90), lon: float = Query(..., ge=-180, le=180),
                        rayon_m: int = Query(300, ge=10, le=RAYON_MAX_M), limite: int = Query(20, ge=1, le=50)):
    dlat = rayon_m / 111_320.0
    dlon = rayon_m / (111_320.0 * max(0.2, math.cos(math.radians(lat))))
    rows = await fetch_all(
        f"SELECT {_COLS} FROM ar_points_interet p JOIN lieux_tournage l ON l.id = p.lieu_id "
        "WHERE p.actif AND p.latitude BETWEEN %s AND %s AND p.longitude BETWEEN %s AND %s",
        (lat - dlat, lat + dlat, lon - dlon, lon + dlon))
    pois = sorted((_poi(dict(r), lat, lon) for r in rows), key=lambda x: x["distance_m"])
    pois = [p for p in pois if p["distance_m"] <= rayon_m][:limite]
    return {"position": {"latitude": lat, "longitude": lon}, "rayon_m": rayon_m, "points": pois}


@router.get("/api/ar/lieux/{lieu_id}")
async def points_du_lieu(lieu_id: int):
    rows = await fetch_all(f"SELECT {_COLS} FROM ar_points_interet p JOIN lieux_tournage l ON l.id = p.lieu_id WHERE p.actif AND p.lieu_id = %s ORDER BY p.id", (lieu_id,))
    return {"lieu_id": lieu_id, "points": [_poi(dict(r)) for r in rows]}


@router.post("/api/ar/points")
async def creer_point(request: Request):
    _exiger_admin(request)
    b = await request.json()
    try:
        lieu_id, lat, lon = int(b["lieu_id"]), float(b["latitude"]), float(b["longitude"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(400, "lieu_id, latitude et longitude sont obligatoires")
    if not await fetch_one("SELECT 1 AS x FROM lieux_tournage WHERE id = %s", (lieu_id,)):
        raise HTTPException(404, "Lieu introuvable")
    pid = await execute(
        "INSERT INTO ar_points_interet (lieu_id, nom, type, latitude, longitude, altitude_m, orientation_deg, rayon_declenchement_m, "
        "contenu_type, contenu_url, contenu_texte, licence) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
        (lieu_id, str(b.get("nom") or "Point d'intérêt")[:160], b.get("type") or "scene", lat, lon, b.get("altitude_m"), b.get("orientation_deg"),
         int(b.get("rayon_declenchement_m") or 30), b.get("contenu_type") or "texte", b.get("contenu_url"), b.get("contenu_texte"), b.get("licence")))
    return {"id": pid}


@router.post("/api/iot/appareils")
async def creer_appareil(request: Request):
    _exiger_admin(request)
    b = await request.json()
    type_ = str(b.get("type") or "autre")
    if type_ not in ("borne_info", "compteur_affluence", "balise_ble", "capteur_meteo", "autre"):
        raise HTTPException(400, "type d'appareil invalide")
    cle = secrets.token_urlsafe(32)
    aid = str(uuid.uuid4())
    await execute("INSERT INTO iot_appareils (id, nom, type, lieu_id, cle_hash) VALUES (%s, %s, %s, %s, %s)",
                  (aid, str(b.get("nom") or "Appareil")[:120], type_, b.get("lieu_id"), _hash_cle(cle)))
    return {"appareil_id": aid, "cle_secrete": cle, "avertissement": "Conservez cette clé : elle ne sera plus jamais affichée."}


@router.post("/api/iot/mesures")
async def recevoir_mesures(request: Request):
    """Corps : {"appareil_id": "...", "mesures": [{"type": "passages", "valeur": 12, "mesure_le": "2026-10-17T12:00:00Z"}]}
    En-tête : X-Device-Key = clé secrète de l'appareil. Au plus 100 mesures par envoi."""
    b = await request.json()
    try:
        aid = str(uuid.UUID(str(b.get("appareil_id"))))
    except ValueError:
        raise HTTPException(400, "appareil_id invalide")
    appareil = await fetch_one("SELECT id, cle_hash, actif FROM iot_appareils WHERE id = %s", (aid,))
    cle = request.headers.get("X-Device-Key", "")
    if not appareil or not appareil["actif"] or not hmac.compare_digest(_hash_cle(cle), appareil["cle_hash"]):
        raise HTTPException(401, "Appareil inconnu ou clé invalide")
    lignes = []
    for m in (b.get("mesures") or [])[:100]:
        type_ = str(m.get("type"))
        if type_ not in TYPES_MESURE:
            raise HTTPException(400, f"Type de mesure non autorisé : {type_} (autorisés : {sorted(TYPES_MESURE)})")
        try:
            valeur = float(m.get("valeur"))
            quand = datetime.fromisoformat(str(m.get("mesure_le")).replace("Z", "+00:00")) if m.get("mesure_le") else datetime.now(timezone.utc)
        except (TypeError, ValueError):
            raise HTTPException(400, "Mesure invalide")
        if quand.tzinfo is None:
            quand = quand.replace(tzinfo=timezone.utc)
        if not math.isfinite(valeur) or abs((quand - datetime.now(timezone.utc)).total_seconds()) > 86400 * 2:
            raise HTTPException(400, "Mesure hors plage")
        lignes.append((aid, type_, valeur, TYPES_MESURE[type_], quand))
    for l in lignes:
        await execute("INSERT INTO iot_mesures (appareil_id, type, valeur, unite, mesure_le) VALUES (%s,%s,%s,%s,%s)", l)
    await execute("UPDATE iot_appareils SET derniere_activite = now() WHERE id = %s", (aid,))
    return {"enregistrees": len(lignes)}


@router.get("/api/iot/lieux/{lieu_id}/affluence")
async def affluence(lieu_id: int):
    """Dernière mesure d'affluence des 30 dernières minutes (aucune donnée individuelle)."""
    r = await fetch_one(
        "SELECT m.valeur, m.mesure_le FROM iot_mesures m JOIN iot_appareils a ON a.id = m.appareil_id "
        "WHERE a.lieu_id = %s AND a.actif AND m.type IN ('affluence', 'passages') AND m.mesure_le > now() - interval '30 minutes' "
        "ORDER BY m.mesure_le DESC LIMIT 1", (lieu_id,))
    if not r:
        return {"lieu_id": lieu_id, "disponible": False}
    v = float(r["valeur"])
    return {"lieu_id": lieu_id, "disponible": True, "valeur": v, "niveau": "calme" if v < 10 else "animé" if v < 40 else "très fréquenté",
            "mesure_le": r["mesure_le"].isoformat()}