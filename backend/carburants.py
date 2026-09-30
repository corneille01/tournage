"""backend/carburants.py - Prix des carburants autour d'un parcours.

Les prix sont importés chaque jour dans stations_carburant par
import_carburants.py (source : prix-carburants.gouv.fr via data.economie.gouv.fr,
Licence Ouverte 2.0). Ce module ne fait aucun appel réseau : il interroge la
base, choisit les stations les plus avantageuses autour du départ et de chaque
étape, et prépare des conseils lisibles.

Choix de la « meilleure » station : on minimise le coût réel d'un plein, c'est-à-dire
prix x litres + carburant consommé par le détour aller-retour. Une station un
centime moins chère mais 9 km plus loin n'est donc pas conseillée.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import statistics
from datetime import datetime, timedelta, timezone

from db import fetch_all, fetch_one
from overpass import haversine_metres

logger = logging.getLogger(__name__)

# clé -> (libellé affiché, colonne SQL, code utilisé par la source dans les ruptures)
CARBURANTS = {
    "gazole": ("Gazole", "prix_gazole", "Gazole"),
    "e10": ("SP95-E10", "prix_e10", "E10"),
    "sp95": ("SP95", "prix_sp95", "SP95"),
    "sp98": ("SP98", "prix_sp98", "SP98"),
    "e85": ("E85 (superéthanol)", "prix_e85", "E85"),
    "gplc": ("GPLc", "prix_gplc", "GPLc"),
}
_ALIAS = {"diesel": "gazole", "sp95-e10": "e10", "sp95_e10": "e10", "essence": "e10", "e85": "e85"}
CONSO_DEFAUT_L_100 = 6.5
RESERVOIR_L = 45.0
RAYON_DEPART_M = 12_000
RAYON_ETAPE_M = 6_000
FACTEUR_ROUTE = 1.3          # distance routière ~ 1,3 x distance à vol d'oiseau
PRIX_ANCIEN_JOURS = 3        # au-delà : prix signalé « à vérifier »
PRIX_PERIME_JOURS = 30       # au-delà : prix écarté des conseils
_JOURS = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"]


def normaliser_carburant(valeur) -> str | None:
    cle = str(valeur or "").strip().lower()
    cle = _ALIAS.get(cle, cle)
    return cle if cle in CARBURANTS else None


def _date_maj(station: dict, cle: str) -> datetime | None:
    """Date du prix du carburant demandé : la date propre à ce carburant si la
    source la fournit (maj_carburants), sinon la date de mise à jour de la station."""
    par_carburant = station.get("maj_carburants")
    if isinstance(par_carburant, str):
        try:
            par_carburant = json.loads(par_carburant)
        except ValueError:
            par_carburant = None
    if isinstance(par_carburant, dict) and par_carburant.get(cle):
        try:
            d = datetime.fromisoformat(str(par_carburant[cle]).replace("Z", "+00:00"))
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return station.get("maj_prix")


def _age_jours(maj: datetime | None) -> int | None:
    if not maj:
        return None
    if maj.tzinfo is None:
        maj = maj.replace(tzinfo=timezone.utc)
    return max(0, (datetime.now(timezone.utc) - maj).days)


def _libelle_maj(maj: datetime | None) -> str:
    age = _age_jours(maj)
    if age is None:
        return "Date du prix inconnue"
    if age == 0:
        return "Prix mis à jour aujourd'hui"
    if age == 1:
        return "Prix mis à jour hier"
    if age < 8:
        return f"Prix mis à jour il y a {age} jours"
    return f"Prix du {maj:%d/%m/%Y}"


def _prix_txt(p: float) -> str:
    return f"{p:.3f}".replace(".", ",")


def _eur_txt(v: float) -> str:
    return f"{v:.2f}".replace(".", ",") if abs(v) < 100 else f"{round(v)}"


def _km_txt(m: float) -> str:
    return f"{round(m)} m" if m < 1000 else f"{m / 1000:.1f}".replace(".", ",") + " km"


def _minutes(valeur) -> int | None:
    try:
        h, m = str(valeur).replace("h", ".").replace(":", ".").split(".")[:2]
        return int(h) * 60 + int(m)
    except (ValueError, TypeError):
        return None


def _horaires_dict(valeur):
    if isinstance(valeur, str):
        try:
            valeur = json.loads(valeur)
        except ValueError:
            return None
    return valeur if isinstance(valeur, dict) else None


def statut_ouverture(station: dict, quand: datetime | None) -> dict:
    """État de la station à l'heure prévue : 24h | ouverte | fermee | inconnu."""
    if station.get("automate_24_24") is True:
        return {"etat": "24h", "libelle": "Automate carte bancaire 24h/24"}
    horaires = _horaires_dict(station.get("horaires"))
    if not quand or not horaires:
        return {"etat": "inconnu", "libelle": "Horaires non précisés, à vérifier"}
    jour = horaires.get(_JOURS[quand.weekday()])
    if not isinstance(jour, dict):
        return {"etat": "inconnu", "libelle": "Horaires non précisés, à vérifier"}
    if str(jour.get("ouvert")) in ("0", "False", "false"):
        return {"etat": "fermee", "libelle": f"Fermée le {_JOURS[quand.weekday()].lower()}"}
    o, f = _minutes(jour.get("ouverture")), _minutes(jour.get("fermeture"))
    if o is None or f is None:
        return {"etat": "inconnu", "libelle": "Horaires non précisés, à vérifier"}
    t = quand.hour * 60 + quand.minute
    ouverte = (o <= t <= f) if o <= f else (t >= o or t <= f)
    fin = f"{f // 60:02d}:{f % 60:02d}"
    if o == 0 and f >= 23 * 60 + 59:
        return {"etat": "ouverte", "libelle": "Ouverte toute la journée"}
    if ouverte:
        return {"etat": "ouverte", "libelle": f"Ouverte à votre passage (jusqu'à {fin})"}
    return {"etat": "fermee", "libelle": f"Fermée à {quand:%H:%M} (horaires : {o // 60:02d}:{o % 60:02d}-{fin})"}


async def stations_autour(lat: float, lon: float, rayon_m: float, cle: str) -> dict:
    """Stations avec un prix connu pour ce carburant, hors ruptures signalées."""
    _, col, code = CARBURANTS[cle]
    dlat = rayon_m / 111_320.0
    dlon = rayon_m / (111_320.0 * max(0.2, math.cos(math.radians(lat))))
    lignes = await fetch_all(
        f"""
        SELECT id, nom, marque, adresse, code_postal, commune, latitude, longitude,
               type_route, automate_24_24, horaires, carburants_rupture,
               {col} AS prix, maj_prix, maj_carburants
        FROM stations_carburant
        WHERE latitude BETWEEN %s AND %s AND longitude BETWEEN %s AND %s
        """,
        (lat - dlat, lat + dlat, lon - dlon, lon + dlon),
    )
    stations, ruptures, total, perimes = [], 0, 0, 0
    for s in lignes:
        d = haversine_metres(lat, lon, float(s["latitude"]), float(s["longitude"]))
        if d > rayon_m:
            continue
        if s["prix"] is None and code not in (s["carburants_rupture"] or []):
            continue          # ce carburant n'est pas vendu ici
        total += 1
        if code in (s["carburants_rupture"] or []):
            ruptures += 1
            continue
        maj = _date_maj(s, cle)
        age = _age_jours(maj)
        if age is not None and age > PRIX_PERIME_JOURS:
            perimes += 1          # prix trop ancien pour être conseillé
            continue
        item = dict(s)
        item["prix"] = float(s["prix"])
        item["distance_m"] = d
        item["maj_effective"] = maj
        stations.append(item)
    return {"stations": stations, "nb_rupture": ruptures, "nb_total": total, "nb_perimes": perimes}


def _dates_planning(date_sortie: str | None, heure_depart: str | None, planning_horaire: list) -> tuple[datetime, dict]:
    try:
        base = datetime.strptime(str(date_sortie), "%Y-%m-%d")
    except ValueError:
        base = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    hm = _minutes(heure_depart or "09:00") or 9 * 60
    courant = base + timedelta(minutes=hm)
    depart_dt, arrivees = courant, {}
    for x in planning_horaire or []:
        if x.get("type") != "lieu":
            continue
        m = _minutes(x.get("heure_arrivee"))
        if m is None:
            continue
        dt = courant.replace(hour=m // 60, minute=m % 60)
        if dt < courant:
            dt += timedelta(days=1)
        arrivees[int(x.get("ordre") or 0)] = dt
        courant = dt
    return depart_dt, arrivees


def _adresse(s: dict) -> str:
    fin = " ".join(x for x in (s.get("code_postal"), (s.get("commune") or "").title()) if x)
    return ", ".join(x for x in (s.get("adresse"), fin) if x)


def _nom_station(s: dict) -> str:
    nom = s.get("nom") or s.get("marque") or "Station-service"
    return nom.title() if nom.isupper() else nom


def _format_station(s: dict, achat_l: float, conso: float, prix_ref: float, quand: datetime | None) -> dict:
    detour_km = 2 * FACTEUR_ROUTE * s["distance_m"] / 1000.0
    cout_detour = detour_km * conso / 100.0 * s["prix"]
    economie = (prix_ref - s["prix"]) * achat_l - cout_detour
    maj = s.get("maj_effective") or s.get("maj_prix")
    jours = _age_jours(maj)
    return {
        "id": s["id"],
        "nom": _nom_station(s),
        "marque": s.get("marque"),
        "adresse": _adresse(s),
        "latitude": float(s["latitude"]), "longitude": float(s["longitude"]),
        "prix": s["prix"], "distance_m": round(s["distance_m"]),
        "autoroute": s.get("type_route") == "A",
        "ouverture": statut_ouverture(s, quand),
        "maj_prix": maj.isoformat() if maj else None,
        "maj_libelle": _libelle_maj(maj),
        "age_jours": jours,
        "prix_ancien": bool(jours is None or jours >= PRIX_ANCIEN_JOURS),
        "economie_nette_eur": round(economie, 2),
        "_cout_net": s["prix"] * achat_l + cout_detour,
    }


def _choisir(stations: list, achat_l: float, conso: float, prix_ref: float, quand: datetime | None, n: int = 3) -> list:
    """Meilleures stations d'un point, triées par coût réel d'un plein."""
    hors_autoroute = [s for s in stations if s.get("type_route") != "A"]
    pool = hors_autoroute or stations
    formatees = [_format_station(s, achat_l, conso, prix_ref, quand) for s in pool]
    ouvertes = [s for s in formatees if s["ouverture"]["etat"] != "fermee"]
    formatees = ouvertes or formatees
    formatees.sort(key=lambda s: (s["_cout_net"], s["distance_m"]))
    for s in formatees:
        s.pop("_cout_net", None)
    return formatees[:n]


async def plan_carburant(*, depart, etapes, planning_horaire, distance_m, carburant, consommation,
                         date_sortie, heure_depart, mode) -> dict | None:
    """Prépare les conseils carburant. Renvoie None si le sujet ne s'applique pas."""
    cle = normaliser_carburant(carburant)
    if mode != "driving-car" or not cle:
        return None
    libelle = CARBURANTS[cle][0]
    try:
        conso = min(25.0, max(2.0, float(consommation)))
    except (TypeError, ValueError):
        conso = CONSO_DEFAUT_L_100
    distance_km = float(distance_m) / 1000.0 if distance_m else None
    litres = distance_km * conso / 100.0 if distance_km else None
    achat_l = min(max(litres or 30.0, 15.0), RESERVOIR_L)

    depart_dt, arrivees = _dates_planning(date_sortie, heure_depart, planning_horaire)
    points = []
    if depart and depart.get("latitude") is not None:
        points.append({"role": "depart", "ordre": 0, "nom": depart.get("nom") or "Votre départ",
                       "lat": float(depart["latitude"]), "lon": float(depart["longitude"]),
                       "quand": depart_dt, "rayon": RAYON_DEPART_M})
    for i, e in enumerate(etapes, start=1):
        points.append({"role": "etape", "ordre": i, "nom": e.get("nom") or f"Étape {i}",
                       "lat": float(e["latitude"]), "lon": float(e["longitude"]),
                       "quand": arrivees.get(i), "rayon": RAYON_ETAPE_M})

    async def chercher(p):
        r = await stations_autour(p["lat"], p["lon"], p["rayon"], cle)
        if not r["stations"]:
            r = await stations_autour(p["lat"], p["lon"], p["rayon"] * 2, cle)
        return r

    resultats = await asyncio.gather(*(chercher(p) for p in points))
    prix_vus = {}
    for r in resultats:
        for s in r["stations"]:
            prix_vus[s["id"]] = s["prix"]
    if not prix_vus:
        nb_perimes = sum(r.get("nb_perimes", 0) for r in resultats)
        if nb_perimes:
            return {"actif": False, "raison": (
                f"Les {nb_perimes} station(s) proches ont des prix de {libelle} datant de plus de "
                f"{PRIX_PERIME_JOURS} jours : Pelify ne les conseille pas.")}
        rempli = await fetch_one("SELECT count(*) AS n FROM stations_carburant")
        if not rempli or not rempli.get("n"):
            return {"actif": False, "raison": "Les prix des carburants ne sont pas encore chargés."}
        return {"actif": False, "raison": f"Aucune station avec un prix de {libelle} connu autour de ce parcours."}

    prix_ref = statistics.median(prix_vus.values())
    plus_recent = None
    for r in resultats:
        for s in r["stations"]:
            m = s.get("maj_effective")
            if m and (plus_recent is None or m > plus_recent):
                plus_recent = m

    sorties, conseils, avertissements = [], [], []
    for p, r in zip(points, resultats):
        top = _choisir(r["stations"], achat_l, conso, prix_ref, p["quand"])
        sorties.append({
            "role": p["role"], "ordre": p["ordre"], "nom": p["nom"],
            "latitude": p["lat"], "longitude": p["lon"],
            "passage": p["quand"].isoformat() if p["quand"] else None,
            "nb_stations": r["nb_total"], "nb_rupture": r["nb_rupture"], "stations": top,
        })
        if r["nb_total"] >= 3 and r["nb_rupture"] >= 2 and r["nb_rupture"] / r["nb_total"] >= 0.3:
            avertissements.append(
                f"Rupture de {libelle} signalée dans {r['nb_rupture']} station(s) sur {r['nb_total']} "
                f"autour de {p['nom']} : prévoyez une solution de repli."
            )

    def nom_point(x):
        return "votre départ" if x["role"] == "depart" else f"l'étape {x['ordre']} - {x['nom']}"

    if litres:
        cout = litres * prix_ref
        conseils.append({"icone": "fa-gas-pump", "texte": (
            f"Pour {distance_km:.0f} km avec {str(round(conso, 1)).replace('.', ',')} L/100 km, comptez environ {litres:.1f} L de {libelle}, "
            f"soit environ {_eur_txt(cout)} € au prix moyen constaté autour de votre parcours "
            f"({_prix_txt(prix_ref)} €/L). Modifiez la consommation dans vos critères pour affiner.")})
        if litres > RESERVOIR_L * 0.9:
            avertissements.append(
                f"Ce parcours consomme environ {litres:.0f} L : prévoyez au moins un plein en cours de route.")

    meilleurs = [(s, x) for s in sorties for x in s["stations"][:1]]
    depart_sortie = next((s for s in sorties if s["role"] == "depart"), None)
    if depart_sortie and depart_sortie["stations"]:
        st = depart_sortie["stations"][0]
        eco = f", soit environ {_eur_txt(st['economie_nette_eur'])} € d'économie sur un plein" if st["economie_nette_eur"] >= 1 else ""
        conseils.append({"icone": "fa-flag-checkered", "texte": (
            f"Avant de partir : {st['nom']}, à {_km_txt(st['distance_m'])} de votre départ, "
            f"{_prix_txt(st['prix'])} €/L{eco}. {st['ouverture']['libelle']}.")})
    if meilleurs:
        s, st = min(meilleurs, key=lambda t: t[1]["prix"])
        if not (depart_sortie and s is depart_sortie):
            eco = f" (environ {_eur_txt(st['economie_nette_eur'])} € d'économie sur un plein)" if st["economie_nette_eur"] >= 1 else ""
            conseils.append({"icone": "fa-piggy-bank", "texte": (
                f"Le carburant le moins cher de votre parcours se trouve près de {nom_point(s)} : {st['nom']}, "
                f"{_prix_txt(st['prix'])} €/L{eco}. {st['ouverture']['libelle']}.")})
    for s in sorties:
        if s["stations"] and s["stations"][0]["ouverture"]["etat"] == "fermee":
            avertissements.append(
                f"Près de {nom_point(s)}, les stations trouvées seront fermées à votre passage "
                f"({s['stations'][0]['ouverture']['libelle']}). Anticipez votre plein.")
    anciens = [x for _, x in meilleurs if x["prix_ancien"]]
    if anciens:
        pire = max((x["age_jours"] for x in anciens if x["age_jours"] is not None), default=None)
        avertissements.append(
            "Certains prix conseillés ne sont pas récents"
            + (f" (jusqu'à {pire} jours)" if pire is not None else "")
            + " : vérifiez-les sur place avant de vous arrêter.")

    return {
        "actif": True, "carburant": cle, "carburant_libelle": libelle,
        "consommation_l_100km": conso, "distance_km": round(distance_km, 1) if distance_km else None,
        "litres_estimes": round(litres, 1) if litres else None,
        "prix_moyen_zone": round(prix_ref, 3),
        "cout_estime_eur": round(litres * prix_ref, 2) if litres else None,
        "points": sorties, "conseils": conseils, "avertissements": avertissements,
        "maj_donnees": plus_recent.isoformat() if plus_recent else None,
        "source": "prix-carburants.gouv.fr via data.economie.gouv.fr (Licence Ouverte 2.0)",
    }