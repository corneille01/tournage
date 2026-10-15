"""
backend/meteo_france.py

Vigilance météorologique OFFICIELLE (Météo-France), par département.

Source : API « Vigilance » du portail https://portail-api.meteofrance.fr (gratuite, clé à créer :
application « Bulletin Vigilance », clé API de durée illimitée). Variables d'environnement :

    METEOFRANCE_API_KEY        clé API (en-tête « apikey »)
    METEOFRANCE_VIGILANCE_URL  (facultatif) adresse du produit « carte », si elle diffère de la
                               valeur par défaut ci-dessous (à vérifier dans la fiche de l'API)

Sans clé, le module renvoie None et le site affiche un lien vers vigilance.meteofrance.fr :
rien n'est inventé. Les données sont lues UNE fois par 10 minutes pour tous les départements.

Le format exact du JSON est analysé de façon tolérante (parcours récursif) ; si aucun département
n'est reconnu, les clés reçues sont écrites dans les journaux pour ajuster rapidement.
"""
from __future__ import annotations

import logging
import os
import re

import httpx

from meteo_france_cache import CacheTTL

logger = logging.getLogger(__name__)

URL_DEFAUT = "https://public-api.meteofrance.fr/public/DPVigilance/v1/cartevigilance/encours"
LIEN_PUBLIC = "https://vigilance.meteofrance.fr"

PHENOMENES = {
    "1": "Vent violent", "2": "Pluie-inondation", "3": "Orages", "4": "Crues",
    "5": "Neige-verglas", "6": "Canicule", "7": "Grand froid", "8": "Avalanches", "9": "Vagues-submersion",
}
COULEURS = {1: "vert", 2: "jaune", 3: "orange", 4: "rouge"}
CONSIGNES = {
    2: "Soyez attentif : phénomènes habituels dans la région, parfois localement dangereux.",
    3: "Soyez très vigilant : phénomènes dangereux prévus. Suivez les consignes des autorités.",
    4: "Vigilance absolue : phénomènes dangereux d'intensité exceptionnelle. Respectez les consignes.",
}

_cache = CacheTTL(ttl_s=600)
_CODE_DEPT = re.compile(r"^(\d{2}|2[AB])$")


def _entier(v, defaut=0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return defaut


def _domaines(noeud, trouves: list) -> None:
    """Collecte, où qu'ils soient, les blocs {domain_id, phenomenon_items…}."""
    if isinstance(noeud, dict):
        if "domain_id" in noeud and ("phenomenon_items" in noeud or "max_color_id" in noeud):
            trouves.append(noeud)
        for v in noeud.values():
            _domaines(v, trouves)
    elif isinstance(noeud, list):
        for v in noeud:
            _domaines(v, trouves)


def analyser_vigilance(payload: dict) -> dict:
    """→ {"J": {"31": {"max": 3, "phenomenes": [{id, libelle, niveau, couleur}]}}, "J1": {…}, "maj": "…"}"""
    produit = payload.get("product") if isinstance(payload, dict) else None
    produit = produit if isinstance(produit, dict) else (payload or {})
    sortie: dict = {"J": {}, "J1": {}, "maj": (payload or {}).get("update_time") or produit.get("update_time")}
    periodes = produit.get("periods") if isinstance(produit, dict) else None
    for p in periodes or []:
        if not isinstance(p, dict):
            continue
        ech = str(p.get("echeance") or p.get("echeance_name") or "").upper().replace("+", "").replace(" ", "")
        cle = "J1" if ech in ("J1", "J+1") else ("J" if ech == "J" else None)
        if cle is None:
            continue
        blocs: list = []
        _domaines(p, blocs)
        for b in blocs:
            code = re.sub(r"[^0-9A-Za-z]", "", str(b.get("domain_id") or "")).upper()
            if not _CODE_DEPT.match(code):
                continue   # on ignore « FRA », les zones maritimes, etc.
            phenomenes = []
            for item in b.get("phenomenon_items") or []:
                niveau = _entier(item.get("phenomenon_max_color_id"), 1)
                pid = str(item.get("phenomenon_id"))
                if niveau >= 2:
                    phenomenes.append({
                        "id": pid, "libelle": PHENOMENES.get(pid, f"Phénomène {pid}"),
                        "niveau": niveau, "couleur": COULEURS.get(niveau, "inconnue"),
                        "consigne": CONSIGNES.get(niveau, ""),
                    })
            phenomenes.sort(key=lambda x: -x["niveau"])
            sortie[cle][code] = {
                "max": max([_entier(b.get("max_color_id"), 1)] + [x["niveau"] for x in phenomenes]),
                "phenomenes": phenomenes,
            }
    return sortie


async def _telecharger() -> dict | None:
    cle = os.getenv("METEOFRANCE_API_KEY", "").strip()
    if not cle:
        return None
    url = os.getenv("METEOFRANCE_VIGILANCE_URL", "").strip() or URL_DEFAUT
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.get(url, headers={"apikey": cle, "accept": "application/json"})
        if r.status_code != 200:
            logger.warning("Vigilance Météo-France : HTTP %s", r.status_code)
            return None
        payload = r.json()
    except Exception as exc:
        logger.warning("Vigilance Météo-France indisponible : %s", exc)
        return None
    resultat = analyser_vigilance(payload)
    if not resultat["J"] and not resultat["J1"]:
        cles = list(payload.keys()) if isinstance(payload, dict) else type(payload).__name__
        logger.warning("Vigilance : aucun département reconnu. Clés reçues : %s", cles)
        return None
    return resultat


async def vigilance_departement(code: str | None) -> dict | None:
    """{"J": {...}|None, "J1": {...}|None, "maj": "..."} pour un département, ou None (non disponible)."""
    if not code:
        return None
    toutes = await _cache.obtenir("vigilance", _telecharger, ttl_echec_s=120)
    if not toutes:
        return None
    code = str(code).upper()
    return {"J": toutes["J"].get(code), "J1": toutes["J1"].get(code), "maj": toutes.get("maj")}