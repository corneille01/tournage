"""
backend/meteo.py

Météo et risques pour un point (lieu de tournage, étape de parcours).

  GET /api/lieux/{lieu_id}/meteo?date=AAAA-MM-JJ     météo + risques d'un lieu
  GET /api/meteo?lat=…&lon=…&date=…                  idem pour un point libre (France métropolitaine)
  meteo_pour_parcours()                              météo de chaque étape, à l'heure de passage prévue

Deux sources, jamais mélangées à l'affichage :
  - VIGILANCE OFFICIELLE Météo-France (par département, aujourd'hui et demain) → alertes « officielles »
  - PRÉVISIONS Open-Meteo (modèles Météo-France AROME/ARPEGE et autres, 1 à 11 km) → météo détaillée et
    avertissements « indicatifs » calculés par Pelify à partir de seuils simples.

Open-Meteo : gratuit pour un usage NON commercial (attribution obligatoire, CC BY 4.0, ~10 000 appels/jour).
Si Pelify devient commercial, souscrire à l'offre « Commercial » (OPEN_METEO_API_KEY + OPEN_METEO_URL, voir
ci-dessous) ou remplacer `_telecharger_previsions` : le reste du module ne change pas.
Résultats mis en cache (30 min par maille de ~1 km) : un lieu populaire = un seul appel externe.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import unicodedata
from datetime import date as Date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter, HTTPException, Query

from db import fetch_one
from meteo_france import vigilance_departement, LIEN_PUBLIC as LIEN_VIGILANCE
from meteo_france_cache import CacheTTL

logger = logging.getLogger(__name__)
router = APIRouter()
PARIS = ZoneInfo("Europe/Paris")

OPEN_METEO_URL = os.getenv("OPEN_METEO_URL", "https://api.open-meteo.com/v1/forecast")
OPEN_METEO_CLE = os.getenv("OPEN_METEO_API_KEY", "").strip()      # offre commerciale : « customer-api.open-meteo.com »
_cache_previsions = CacheTTL(ttl_s=1800, taille_max=3000)

DEPARTEMENTS_OCCITANIE = {
    "ariege": "09", "aude": "11", "aveyron": "12", "gard": "30", "haute garonne": "31", "gers": "32",
    "herault": "34", "lot": "46", "lozere": "48", "hautes pyrenees": "65", "pyrenees orientales": "66",
    "tarn": "81", "tarn et garonne": "82",
}
# Départements méditerranéens les plus exposés aux feux de forêt (arrêtés préfectoraux sur l'accès aux massifs)
DEPARTEMENTS_FEUX = {"11", "30", "34", "48", "66"}

WMO = {
    0: ("Ciel dégagé", "fa-sun"), 1: ("Plutôt dégagé", "fa-sun"), 2: ("Partiellement nuageux", "fa-cloud-sun"),
    3: ("Couvert", "fa-cloud"), 45: ("Brouillard", "fa-smog"), 48: ("Brouillard givrant", "fa-smog"),
    51: ("Bruine légère", "fa-cloud-rain"), 53: ("Bruine", "fa-cloud-rain"), 55: ("Bruine dense", "fa-cloud-rain"),
    56: ("Bruine verglaçante", "fa-snowflake"), 57: ("Bruine verglaçante dense", "fa-snowflake"),
    61: ("Pluie faible", "fa-cloud-rain"), 63: ("Pluie modérée", "fa-cloud-rain"), 65: ("Pluie forte", "fa-cloud-showers-heavy"),
    66: ("Pluie verglaçante", "fa-snowflake"), 67: ("Pluie verglaçante forte", "fa-snowflake"),
    71: ("Neige faible", "fa-snowflake"), 73: ("Neige modérée", "fa-snowflake"), 75: ("Neige forte", "fa-snowflake"),
    77: ("Grains de neige", "fa-snowflake"), 80: ("Averses faibles", "fa-cloud-rain"),
    81: ("Averses", "fa-cloud-showers-heavy"), 82: ("Averses violentes", "fa-cloud-showers-heavy"),
    85: ("Averses de neige", "fa-snowflake"), 86: ("Fortes averses de neige", "fa-snowflake"),
    95: ("Orage", "fa-cloud-bolt"), 96: ("Orage avec grêle", "fa-cloud-bolt"), 99: ("Orage violent avec grêle", "fa-cloud-bolt"),
}
CODES_ORAGE = {95, 96, 99}
CODES_GRELE = {96, 99}
CODES_NEIGE_VERGLAS = {56, 57, 66, 67, 71, 73, 75, 77, 85, 86}


def _norm(s) -> str:
    t = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().lower()
    return " ".join("".join(c if c.isalnum() else " " for c in t).split())


def libelle_meteo(code) -> tuple[str, str]:
    try:
        return WMO.get(int(code), ("Conditions variables", "fa-cloud-sun"))
    except (TypeError, ValueError):
        return ("Conditions variables", "fa-cloud-sun")


# ─────────────────────────────────────────────────────────────
# Département d'un point (contour officiel déjà présent dans frontend/)
# ─────────────────────────────────────────────────────────────

_CONTOURS: list | None = None


def _charger_contours() -> list:
    global _CONTOURS
    if _CONTOURS is not None:
        return _CONTOURS
    _CONTOURS = []
    chemin = Path(__file__).resolve().parent.parent / "frontend" / "departements-occitanie.geojson"
    try:
        data = json.loads(chemin.read_text(encoding="utf-8"))
        for f in data.get("features", []):
            g = f.get("geometry") or {}
            polys = g.get("coordinates") if g.get("type") == "MultiPolygon" else [g.get("coordinates")] if g.get("type") == "Polygon" else []
            pr = f.get("properties") or {}
            brut = pr.get("dep_code") or pr.get("dep_current_code") or pr.get("code") or ""
            code = str(brut[0] if isinstance(brut, list) and brut else brut)
            for poly in polys:
                if not poly:
                    continue
                anneau = poly[0]
                xs = [p[0] for p in anneau]; ys = [p[1] for p in anneau]
                _CONTOURS.append((code, min(xs), max(xs), min(ys), max(ys), anneau))
    except Exception:
        logger.warning("Contours des départements illisibles : %s", chemin)
    return _CONTOURS


def _dans_anneau(lon: float, lat: float, anneau: list) -> bool:
    dedans, j = False, len(anneau) - 1
    for i in range(len(anneau)):
        xi, yi = anneau[i][0], anneau[i][1]
        xj, yj = anneau[j][0], anneau[j][1]
        if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / ((yj - yi) or 1e-12) + xi:
            dedans = not dedans
        j = i
    return dedans


def code_departement(lat: float, lon: float, nom: str | None = None) -> str | None:
    for code, x0, x1, y0, y1, anneau in _charger_contours():
        if x0 <= lon <= x1 and y0 <= lat <= y1 and _dans_anneau(lon, lat, anneau):
            return code
    return DEPARTEMENTS_OCCITANIE.get(_norm(nom)) if nom else None


# ─────────────────────────────────────────────────────────────
# Prévisions (Open-Meteo)
# ─────────────────────────────────────────────────────────────

async def _telecharger_previsions(lat: float, lon: float) -> dict | None:
    params = {
        "latitude": lat, "longitude": lon, "timezone": "Europe/Paris", "forecast_days": 8,
        "current": "temperature_2m,apparent_temperature,precipitation,weather_code,wind_speed_10m,wind_gusts_10m",
        "daily": ("weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,precipitation_probability_max,"
                  "wind_gusts_10m_max,uv_index_max,sunrise,sunset"),
        "hourly": "temperature_2m,precipitation,precipitation_probability,weather_code,wind_gusts_10m",
    }
    if OPEN_METEO_CLE:
        params["apikey"] = OPEN_METEO_CLE
    try:
        async with httpx.AsyncClient(timeout=7.0, headers={"User-Agent": "Pelify/1.0 (reservations@pelify.app)"}) as client:
            r = await client.get(OPEN_METEO_URL, params=params)
        if r.status_code != 200:
            logger.warning("Open-Meteo : HTTP %s", r.status_code)
            return None
        return _normaliser_previsions(r.json())
    except Exception as exc:
        logger.warning("Open-Meteo indisponible : %s", exc)
        return None


def _normaliser_previsions(brut: dict) -> dict:
    cur, dj, dh = brut.get("current") or {}, brut.get("daily") or {}, brut.get("hourly") or {}
    jours = []
    for i, jour in enumerate(dj.get("time") or []):
        code = (dj.get("weather_code") or [None])[i]
        lib, icone = libelle_meteo(code)
        jours.append({
            "date": jour, "code": code, "libelle": lib, "icone": icone,
            "tmin": (dj.get("temperature_2m_min") or [None])[i], "tmax": (dj.get("temperature_2m_max") or [None])[i],
            "pluie_mm": (dj.get("precipitation_sum") or [None])[i],
            "proba_pluie": (dj.get("precipitation_probability_max") or [None])[i],
            "rafales_kmh": (dj.get("wind_gusts_10m_max") or [None])[i],
            "uv": (dj.get("uv_index_max") or [None])[i],
            "lever": (dj.get("sunrise") or [None])[i], "coucher": (dj.get("sunset") or [None])[i],
        })
    heures: dict[str, list] = {}
    for i, t in enumerate(dh.get("time") or []):
        jour, heure = t.split("T")
        code = (dh.get("weather_code") or [None])[i]
        lib, icone = libelle_meteo(code)
        heures.setdefault(jour, []).append({
            "heure": heure[:5], "temperature": (dh.get("temperature_2m") or [None])[i],
            "pluie_mm": (dh.get("precipitation") or [None])[i],
            "proba_pluie": (dh.get("precipitation_probability") or [None])[i],
            "code": code, "libelle": lib, "icone": icone,
            "rafales_kmh": (dh.get("wind_gusts_10m") or [None])[i],
        })
    lib, icone = libelle_meteo(cur.get("weather_code"))
    actuel = {
        "temperature": cur.get("temperature_2m"), "ressenti": cur.get("apparent_temperature"),
        "pluie_mm": cur.get("precipitation"), "vent_kmh": cur.get("wind_speed_10m"),
        "rafales_kmh": cur.get("wind_gusts_10m"), "code": cur.get("weather_code"), "libelle": lib, "icone": icone,
        "heure": cur.get("time"),
    } if cur else None
    return {"altitude_m": brut.get("elevation"), "actuel": actuel, "jours": jours, "heures": heures}


async def previsions(lat: float, lon: float) -> dict | None:
    cle = (round(lat, 2), round(lon, 2))      # maille d'environ 1 km
    return await _cache_previsions.obtenir(cle, lambda: _telecharger_previsions(round(lat, 2), round(lon, 2)), ttl_echec_s=90)


# ─────────────────────────────────────────────────────────────
# Risques : vigilance officielle + avertissements indicatifs
# ─────────────────────────────────────────────────────────────

ORDRE = {"ok": 0, "info": 1, "attention": 2, "danger": 3}


def _num(v, defaut=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return defaut


def evaluer_risques(jour: dict | None, heure: dict | None, altitude: float | None, dept: str | None,
                    date: Date, vigilance: dict | None, aujourdhui: Date) -> list[dict]:
    risques: list[dict] = []

    # 1) Vigilance OFFICIELLE (aujourd'hui et demain seulement)
    ecart = (date - aujourdhui).days
    echeance = "J" if ecart == 0 else "J1" if ecart == 1 else None
    if vigilance and echeance and vigilance.get(echeance):
        for p in vigilance[echeance]["phenomenes"]:
            risques.append({
                "niveau": "danger" if p["niveau"] >= 3 else "attention", "source": "vigilance",
                "titre": f"Vigilance {p['couleur']} : {p['libelle']}", "texte": p["consigne"],
                "couleur": p["couleur"],
            })

    # 2) Avertissements INDICATIFS (seuils simples sur la prévision)
    if jour:
        rafales = _num(jour.get("rafales_kmh"), 0)
        pluie = _num(jour.get("pluie_mm"), 0)
        tmax, tmin = _num(jour.get("tmax")), _num(jour.get("tmin"))
        code = jour.get("code")
        code = int(code) if code is not None else None
        if heure and (_num(heure.get("proba_pluie"), 0) >= 60 or _num(heure.get("pluie_mm"), 0) >= 1.5) \
                and not (code in CODES_ORAGE):
            risques.append({"niveau": "info", "source": "prevision", "titre": "Pluie probable à l'heure de passage prévue",
                            "texte": "Prévoyez un vêtement imperméable pour cette étape."})
        if rafales >= 90:
            risques.append({"niveau": "danger", "source": "prevision", "titre": "Rafales très violentes prévues",
                            "texte": f"Rafales jusqu'à {rafales:.0f} km/h : évitez crêtes, forêts et falaises."})
        elif rafales >= 60:
            risques.append({"niveau": "attention", "source": "prevision", "titre": "Vent fort",
                            "texte": f"Rafales jusqu'à {rafales:.0f} km/h : prudence en hauteur et près des arbres."})
        if pluie >= 50:
            risques.append({"niveau": "danger", "source": "prevision", "titre": "Pluies très abondantes",
                            "texte": f"{pluie:.0f} mm attendus : risque de crues soudaines, évitez cours d'eau et gués."})
        elif pluie >= 20:
            risques.append({"niveau": "attention", "source": "prevision", "titre": "Fortes pluies",
                            "texte": f"{pluie:.0f} mm attendus : routes et sentiers glissants, prudence près des cours d'eau."})
        if code in CODES_ORAGE:
            risques.append({"niveau": "danger" if code in CODES_GRELE else "attention", "source": "prevision",
                            "titre": "Orages prévus" + (" avec grêle" if code in CODES_GRELE else ""),
                            "texte": "Évitez les sorties en hauteur et les abords des arbres isolés ; abritez-vous dans un bâtiment ou un véhicule."})
        if tmax is not None and tmax >= 40:
            risques.append({"niveau": "danger", "source": "prevision", "titre": "Chaleur extrême",
                            "texte": f"Jusqu'à {tmax:.0f} °C : évitez les efforts en journée, buvez beaucoup, protégez-vous du soleil."})
        elif tmax is not None and tmax >= 35:
            risques.append({"niveau": "attention", "source": "prevision", "titre": "Forte chaleur",
                            "texte": f"Jusqu'à {tmax:.0f} °C : eau, chapeau, pauses à l'ombre ; privilégiez le matin."})
        if tmin is not None and tmin <= -5:
            risques.append({"niveau": "attention", "source": "prevision", "titre": "Grand froid",
                            "texte": f"Jusqu'à {tmin:.0f} °C la nuit : vêtements chauds, vérifiez votre véhicule."})
        if code in CODES_NEIGE_VERGLAS or (tmin is not None and tmin <= 0 and pluie >= 1):
            risques.append({"niveau": "attention", "source": "prevision", "titre": "Neige ou verglas possibles",
                            "texte": "Routes et sentiers potentiellement glissants, surtout en altitude et à l'aube."})
        if code in (45, 48):
            risques.append({"niveau": "info", "source": "prevision", "titre": "Brouillard",
                            "texte": "Visibilité réduite sur les routes de montagne."})
        uv = _num(jour.get("uv"), 0)
        if uv >= 8:
            risques.append({"niveau": "info", "source": "prevision", "titre": f"Indice UV élevé ({uv:.0f})",
                            "texte": "Protection solaire indispensable entre 12 h et 16 h."})
        if altitude and altitude >= 1500 and (tmin is not None and tmin <= 3):
            risques.append({"niveau": "info", "source": "prevision", "titre": "Zone de montagne",
                            "texte": f"Altitude {altitude:.0f} m : températures basses et météo changeante, prévoyez des couches chaudes."})

    # 3) Saison des feux de forêt (information permanente, pas une alerte en temps réel)
    if dept in DEPARTEMENTS_FEUX and 6 <= date.month <= 9:
        chaud_venteux = bool(jour) and (_num(jour.get("tmax"), 0) >= 32 and _num(jour.get("rafales_kmh"), 0) >= 50)
        risques.append({
            "niveau": "attention" if chaud_venteux else "info", "source": "saison",
            "titre": "Feux de forêt : conditions favorables (indicatif)" if chaud_venteux else "Saison des feux de forêt",
            "texte": ("Chaleur et vent : danger de feux accru. " if chaud_venteux else "") +
                     "Du 1er juin au 30 septembre, l'accès aux massifs peut être déconseillé ou interdit par arrêté préfectoral. "
                     "Consultez la « Météo des forêts » de Météo-France et le site de la préfecture avant de partir.",
        })
    risques.sort(key=lambda r: (-ORDRE[r["niveau"]], r["source"] != "vigilance"))
    return risques


# ─────────────────────────────────────────────────────────────
# Assemblage
# ─────────────────────────────────────────────────────────────

def _aujourdhui() -> Date:
    return datetime.now(PARIS).date()


def _date(valeur: str | None) -> Date:
    if not valeur:
        return _aujourdhui()
    try:
        return datetime.strptime(str(valeur)[:10], "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(400, "date invalide (format AAAA-MM-JJ)")


async def meteo_point(lat: float, lon: float, date: Date | None = None, heure: str | None = None,
                      nom_departement: str | None = None, compact: bool = False) -> dict:
    date = date or _aujourdhui()
    aujourdhui = _aujourdhui()
    dept = code_departement(lat, lon, nom_departement)
    prev, vig = await asyncio.gather(previsions(lat, lon), vigilance_departement(dept))

    iso = date.isoformat()
    jour = next((j for j in (prev or {}).get("jours", []) if j["date"] == iso), None)
    a_heure = None
    if prev and heure and iso in prev.get("heures", {}):
        h0 = str(heure)[:2]
        a_heure = next((h for h in prev["heures"][iso] if h["heure"][:2] == h0), None)

    risques = evaluer_risques(jour, a_heure, (prev or {}).get("altitude_m"), dept, date, vig, aujourdhui)
    niveau = max((r["niveau"] for r in risques), key=lambda n: ORDRE[n], default="ok")

    ecart = (date - aujourdhui).days
    echeance = "J" if ecart == 0 else "J1" if ecart == 1 else None
    vigilance = {
        "disponible": vig is not None, "echeance": echeance, "maj": (vig or {}).get("maj"), "lien": LIEN_VIGILANCE,
        "phenomenes": ((vig or {}).get(echeance) or {}).get("phenomenes", []) if echeance else [],
        "niveau_max": (((vig or {}).get(echeance) or {}).get("max", 1)) if echeance else None,
    }
    sortie = {
        "date": iso, "departement": dept, "altitude_m": (prev or {}).get("altitude_m"),
        "previsions_disponibles": prev is not None,
        "jour": jour, "a_l_heure": a_heure, "vigilance": vigilance, "risques": risques, "niveau": niveau,
        "hors_horizon": prev is not None and jour is None,
    }
    if not compact:
        sortie["actuel"] = (prev or {}).get("actuel") if date == aujourdhui else None
        sortie["prochains_jours"] = [j for j in (prev or {}).get("jours", []) if j["date"] >= aujourdhui.isoformat()][:5]
        sortie["sources"] = [
            {"nom": "Prévisions : Open-Meteo.com (modèles Météo-France AROME/ARPEGE et autres)", "lien": "https://open-meteo.com"},
            {"nom": "Vigilance : Météo-France", "lien": LIEN_VIGILANCE},
        ]
    return sortie


@router.get("/api/lieux/{lieu_id}/meteo")
async def meteo_lieu(lieu_id: int, date: str | None = Query(None), heure: str | None = Query(None)):
    lieu = await fetch_one("SELECT id, nom, latitude, longitude, departement FROM lieux_tournage WHERE id = %s", (lieu_id,))
    if not lieu:
        raise HTTPException(404, "Lieu introuvable")
    resultat = await meteo_point(float(lieu["latitude"]), float(lieu["longitude"]), _date(date), heure, lieu.get("departement"))
    resultat["lieu"] = {"id": int(lieu["id"]), "nom": lieu["nom"]}
    return resultat


@router.get("/api/meteo")
async def meteo_libre(lat: float = Query(..., ge=41.0, le=51.5), lon: float = Query(..., ge=-5.5, le=9.8),
                      date: str | None = Query(None), heure: str | None = Query(None)):
    return await meteo_point(lat, lon, _date(date), heure)


async def meteo_pour_parcours(etapes: list[dict], depart: dict | None, date_sortie: str | None,
                              heure_depart: str | None, planning_horaire: list) -> dict | None:
    """Météo de chaque étape, à l'heure de passage prévue. Jamais bloquant pour le parcours."""
    if not etapes:
        return None
    try:
        from carburants import _dates_planning
        _, arrivees = _dates_planning(date_sortie, heure_depart, planning_horaire)
    except Exception:
        arrivees = {}
    try:
        date = datetime.strptime(str(date_sortie)[:10], "%Y-%m-%d").date() if date_sortie else _aujourdhui()
    except ValueError:
        date = _aujourdhui()

    async def un(i: int, e: dict):
        quand = arrivees.get(i)
        # Une étape atteinte après minuit est météorologiquement le lendemain.
        m = await meteo_point(float(e["latitude"]), float(e["longitude"]), quand.date() if quand else date,
                              quand.strftime("%H:%M") if quand else None, e.get("departement"), compact=True)
        return {"ordre": i, "nom": e.get("nom"), "lieu_id": e.get("id"),
                "passage": quand.strftime("%H:%M") if quand else None, "meteo": m}

    try:
        points = await asyncio.wait_for(asyncio.gather(*(un(i, e) for i, e in enumerate(etapes[:12], start=1))), timeout=12)
    except Exception:
        logger.exception("Météo du parcours indisponible")
        return None

    niveau = max((p["meteo"]["niveau"] for p in points), key=lambda n: ORDRE[n], default="ok")
    alertes, vus = [], set()
    for p in points:
        for r in p["meteo"]["risques"]:
            if ORDRE[r["niveau"]] >= 2:
                cle = (r["titre"], r["source"])
                if cle not in vus:
                    vus.add(cle)
                    alertes.append({**r, "etapes": [q["ordre"] for q in points if any(
                        x["titre"] == r["titre"] for x in q["meteo"]["risques"])]})
    return {
        "date": date.isoformat(), "niveau": niveau, "alertes": alertes, "points": points,
        "vigilance_disponible": any(p["meteo"]["vigilance"]["disponible"] for p in points),
        "sources": [{"nom": "Prévisions : Open-Meteo.com", "lien": "https://open-meteo.com"},
                    {"nom": "Vigilance : Météo-France", "lien": LIEN_VIGILANCE}],
    }