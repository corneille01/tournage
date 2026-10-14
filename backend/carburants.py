"""backend/carburants.py - Prix des carburants autour d'un parcours.

Les prix sont importÃ©s chaque jour dans stations_carburant par
import_carburants.py (source : prix-carburants.gouv.fr via data.economie.gouv.fr,
Licence Ouverte 2.0). Ce module ne fait aucun appel rÃ©seau : il interroge la
base, choisit les stations les plus avantageuses autour du dÃ©part et de chaque
Ã©tape, et prÃ©pare des conseils lisibles.

Choix de la Â« meilleure Â» station : on minimise le coÃ»t rÃ©el d'un plein, c'est-Ã -dire
prix x litres + carburant consommÃ© par le dÃ©tour aller-retour. Une station un
centime moins chÃ¨re mais 9 km plus loin n'est donc pas conseillÃ©e.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import statistics
import unicodedata
from datetime import datetime, timedelta, timezone

from .db import fetch_all, fetch_one
from .overpass import haversine_metres
from .distances_ign import distances_routieres, estimation

logger = logging.getLogger(__name__)

# clÃ© -> (libellÃ© affichÃ©, colonne SQL, code utilisÃ© par la source dans les ruptures)
CARBURANTS = {
    "gazole": ("Gazole", "prix_gazole", "Gazole"),
    "e10": ("SP95-E10", "prix_e10", "E10"),
    "sp95": ("SP95", "prix_sp95", "SP95"),
    "sp98": ("SP98", "prix_sp98", "SP98"),
    "e85": ("E85 (superÃ©thanol)", "prix_e85", "E85"),
    "gplc": ("GPLc", "prix_gplc", "GPLc"),
}
_ALIAS = {"diesel": "gazole", "sp95-e10": "e10", "sp95_e10": "e10", "essence": "e10", "e85": "e85"}
CONSO_DEFAUT_L_100 = 6.5
RESERVOIR_L = 45.0
RAYON_DEPART_M = 12_000
RAYON_ETAPE_M = 6_000
FACTEUR_ROUTE = 1.3          # distance routiÃ¨re ~ 1,3 x distance Ã  vol d'oiseau
PRIX_ANCIEN_JOURS = 3        # au-delÃ  : prix signalÃ© Â« Ã  vÃ©rifier Â»
PRIX_PERIME_JOURS = 30       # au-delÃ  : prix Ã©cartÃ© des conseils
_JOURS = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"]


def normaliser_carburant(valeur) -> str | None:
    cle = str(valeur or "").strip().lower()
    cle = _ALIAS.get(cle, cle)
    return cle if cle in CARBURANTS else None


def _date_maj(station: dict, cle: str) -> datetime | None:
    """Date du prix du carburant demandÃ© : la date propre Ã  ce carburant si la
    source la fournit (maj_carburants), sinon la date de mise Ã  jour de la station."""
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
        return "Prix mis Ã  jour aujourd'hui"
    if age == 1:
        return "Prix mis Ã  jour hier"
    if age < 8:
        return f"Prix mis Ã  jour il y a {age} jours"
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


def _plages_depuis(jour: dict) -> list[tuple[int, int]]:
    """Plages d'ouverture d'un jour, en minutes : [(ouverture, fermeture), ...]."""
    plages = []
    for texte in jour.get("plages") or []:
        try:
            o, f = str(texte).split("-")
        except ValueError:
            continue
        mo, mf = _minutes(o), _minutes(f)
        if mo is not None and mf is not None:
            plages.append((mo, mf))
    if not plages:
        mo, mf = _minutes(jour.get("ouverture")), _minutes(jour.get("fermeture"))
        if mo is not None and mf is not None:
            plages.append((mo, mf))
    return plages


def _horaires_dict(valeur):
    """Horaires sous la forme {'Lundi': {ouvert, ouverture, fermeture, plages}, ...}.

    Accepte deux formats de la source :
      - ancien : {"Lundi": {"ouvert": "1", "ouverture": "08:00", "fermeture": "19:00"}, ...}
      - v2     : {"jour": [{"nom": "Lundi", "ferme": "", "horaire": [{"ouverture": "06:00",
                 "fermeture": "12:00"}, {"ouverture": "14:00", "fermeture": "20:00"}]}, ...]}
    Plusieurs plages dans la journÃ©e (pause dÃ©jeuner) sont conservÃ©es dans Â« plages Â».
    """
    if isinstance(valeur, str):
        try:
            valeur = json.loads(valeur)
        except ValueError:
            return None
    if not isinstance(valeur, dict):
        return None
    jours = valeur.get("jour")
    if not isinstance(jours, list):
        return valeur

    def lire(d, cle):
        """La source est issue d'un XML : les attributs arrivent parfois prÃ©fixÃ©s par Â« @ Â»."""
        return d.get(cle) if d.get(cle) is not None else d.get("@" + cle)

    sortie = {}
    for j in jours:
        if not isinstance(j, dict):
            continue
        nom = str(lire(j, "nom") or "").strip().capitalize()
        if nom not in _JOURS:
            continue
        brut = j.get("horaire")
        if isinstance(brut, dict):
            brut = [brut]
        plages = []
        for h in brut or []:
            if isinstance(h, dict):
                o, f = _hhmm(lire(h, "ouverture")), _hhmm(lire(h, "fermeture"))
                if o and f:
                    plages.append(f"{o}-{f}")
        ferme = str(lire(j, "ferme") or "").strip().lower() in ("1", "true", "oui")
        # Jour fermÃ© : la source met alors une plage factice (Â« 01.00-01.00 Â»), Ã  ignorer.
        if ferme:
            plages = []
        sortie[nom] = {
            "ouvert": "0" if ferme else ("1" if plages else None),
            "ouverture": plages[0].split("-")[0] if plages else None,
            "fermeture": plages[-1].split("-")[1] if plages else None,
            "plages": plages,
        }
    return sortie or None


def statut_ouverture(station: dict, quand: datetime | None) -> dict:
    """Ã‰tat de la station Ã  l'heure prÃ©vue : 24h | ouverte | fermee | inconnu."""
    if station.get("automate_24_24") is True:
        return {"etat": "24h", "libelle": "Automate carte bancaire 24h/24"}
    horaires = _horaires_dict(station.get("horaires"))
    if not quand or not horaires:
        return {"etat": "inconnu", "libelle": "Horaires non prÃ©cisÃ©s, Ã  vÃ©rifier"}
    jour = horaires.get(_JOURS[quand.weekday()])
    if not isinstance(jour, dict):
        return {"etat": "inconnu", "libelle": "Horaires non prÃ©cisÃ©s, Ã  vÃ©rifier"}
    if str(jour.get("ouvert")) in ("0", "False", "false"):
        return {"etat": "fermee", "libelle": f"FermÃ©e le {_JOURS[quand.weekday()].lower()}"}
    plages = _plages_depuis(jour)
    if not plages:
        return {"etat": "inconnu", "libelle": "Horaires non prÃ©cisÃ©s, Ã  vÃ©rifier"}
    if len(plages) == 1 and plages[0][0] == 0 and plages[0][1] >= 23 * 60 + 59:
        return {"etat": "ouverte", "libelle": "Ouverte toute la journÃ©e"}
    t = quand.hour * 60 + quand.minute
    for o, f in plages:
        if (o <= t <= f) if o <= f else (t >= o or t <= f):
            return {"etat": "ouverte", "libelle": f"Ouverte Ã  votre passage (jusqu'Ã  {f // 60:02d}:{f % 60:02d})"}
    texte = " Â· ".join(f"{o // 60:02d}:{o % 60:02d}-{f // 60:02d}:{f % 60:02d}" for o, f in plages)
    return {"etat": "fermee", "libelle": f"FermÃ©e Ã  {quand:%H:%M} (horaires : {texte})"}


async def stations_autour(lat: float, lon: float, rayon_m: float, cle: str) -> dict:
    """Stations avec un prix connu pour ce carburant, hors ruptures signalÃ©es."""
    _, col, code = CARBURANTS[cle]
    dlat = rayon_m / 111_320.0
    dlon = rayon_m / (111_320.0 * max(0.2, math.cos(math.radians(lat))))
    lignes = await fetch_all(
        f"""
        SELECT id, nom, marque, adresse, code_postal, commune, latitude, longitude,
               type_route, automate_24_24, horaires, carburants_rupture, services,
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
            perimes += 1          # prix trop ancien pour Ãªtre conseillÃ©
            continue
        item = dict(s)
        item["prix"] = float(s["prix"])
        item["distance_m"] = d            # vol d'oiseau : prÃ©sÃ©lection seulement, remplacÃ© par l'IGN dans plan_carburant
        item["distance_vol_m"] = d
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
    """Nom affichÃ©. Le jeu v2 ne fournit ni nom ni marque : Ã  dÃ©faut, Â« Station-service Â»
    suivi de la commune, l'adresse exacte Ã©tant affichÃ©e Ã  part."""
    nom = (s.get("nom") or s.get("marque") or "").strip()
    if nom:
        return nom.title() if nom.isupper() else nom
    commune = (s.get("commune") or "").strip().title()
    return f"Station-service {commune}".strip() if commune else "Station-service"


def _format_station(s: dict, achat_l: float, conso: float, prix_ref: float, quand: datetime | None) -> dict:
    # distance_m est ici une distance de ROUTE (IGN) : aller-retour = 2 Ã— distance.
    detour_km = 2 * s["distance_m"] / 1000.0
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
        "duree_secondes": s.get("duree_s"),
        "distance_source": s.get("distance_source", "vol_oiseau"),   # "ign" | "estimee" | "vol_oiseau"
        "autoroute": s.get("type_route") == "A",
        "automate_24_24": s.get("automate_24_24"),
        "ouverture": statut_ouverture(s, quand),
        # Autres carburants signalÃ©s en rupture ici (le carburant demandÃ© ne l'est jamais : filtrÃ© en amont).
        "ruptures": [lib for _, (lib, _, code) in CARBURANTS.items() if code in (s.get("carburants_rupture") or [])],
        "services_principaux": _nettoyer_services(s.get("services"))[:6],
        "maj_prix": maj.isoformat() if maj else None,
        "maj_libelle": _libelle_maj(maj),
        "age_jours": jours,
        "prix_ancien": bool(jours is None or jours >= PRIX_ANCIEN_JOURS),
        "economie_nette_eur": round(economie, 2),
        "_cout_net": s["prix"] * achat_l + cout_detour,
    }


def _choisir(stations: list, achat_l: float, conso: float, prix_ref: float, quand: datetime | None, n: int = 3) -> list:
    """Meilleures stations d'un point, triÃ©es par coÃ»t rÃ©el d'un plein."""
    hors_autoroute = [s for s in stations if s.get("type_route") != "A"]
    pool = hors_autoroute or stations
    formatees = [_format_station(s, achat_l, conso, prix_ref, quand) for s in pool]
    ouvertes = [s for s in formatees if s["ouverture"]["etat"] != "fermee"]
    formatees = ouvertes or formatees
    formatees.sort(key=lambda s: (s["_cout_net"], s["distance_m"]))
    for s in formatees:
        s.pop("_cout_net", None)
    return formatees[:n]


PRESELECTION_IGN = 5      # candidats interrogÃ©s auprÃ¨s de l'IGN, par point du parcours


async def _distances_precalculees(lieu_id) -> dict[int, dict]:
    """Distances de route IGN dÃ©jÃ  stockÃ©es (enrich_itineraires.py) entre un lieu et ses stations."""
    if lieu_id is None:
        return {}
    try:
        lignes = await fetch_all(
            """
            SELECT station_id, distance_voiture_metres, duree_voiture_secondes
            FROM amenity_cache
            WHERE lieu_tournage_id = %s AND categorie = 'station_service'
              AND station_id IS NOT NULL AND distance_voiture_metres IS NOT NULL
            """,
            (int(lieu_id),),
        )
    except Exception:
        logger.warning("Distances stations prÃ©calculÃ©es indisponibles (lieu %s)", lieu_id)
        return {}
    return {
        int(x["station_id"]): {
            "distance_metres": int(x["distance_voiture_metres"]),
            "duree_secondes": x["duree_voiture_secondes"],
            "source": "ign",
        }
        for x in lignes
    }


async def _affiner_par_route(p: dict, stations: list, achat_l: float, conso: float, restreindre: bool = True) -> list:
    """Remplace, pour les meilleurs candidats d'un point, le vol d'oiseau par la distance
    de route de la GÃ©oplateforme IGN. Le vol d'oiseau ne sert qu'Ã  prÃ©sÃ©lectionner."""
    if not stations:
        return []
    # Ã‰tape : on lit les distances IGN dÃ©jÃ  stockÃ©es dans amenity_cache (aucun appel IGN).
    # DÃ©part saisi par le visiteur : pas de prÃ©calcul possible, donc appel IGN en direct.
    deja = await _distances_precalculees(p.get("lieu_id")) if p["role"] == "etape" else {}
    if deja and restreindre:
        stations = [s for s in stations if int(s["id"]) in deja] or stations
    hors_autoroute = [s for s in stations if s.get("type_route") != "A"]
    pool = hors_autoroute or stations
    pool = sorted(
        pool,
        key=lambda s: s["prix"] * achat_l
        + 2 * FACTEUR_ROUTE * s["distance_vol_m"] / 1000.0 * conso / 100.0 * s["prix"],
    )[:PRESELECTION_IGN]

    origine = (p["lat"], p["lon"])
    a_calculer = [s for s in pool if int(s["id"]) not in deja]
    mesures = await distances_routieres(
        origine, [(float(s["latitude"]), float(s["longitude"])) for s in a_calculer],
        "driving-car", delai_max_s=20.0,
    )
    par_id = {int(s["id"]): m for s, m in zip(a_calculer, mesures)}

    affinees = []
    for s in pool:
        m = deja.get(int(s["id"])) or par_id.get(int(s["id"]))
        if not m:  # IGN indisponible : estimation, signalÃ©e Ã  l'utilisateur
            m = estimation(origine, (float(s["latitude"]), float(s["longitude"])), "driving-car")
        item = dict(s)
        item["distance_m"] = float(m["distance_metres"])
        item["duree_s"] = m.get("duree_secondes")
        item["distance_source"] = m.get("source", "ign")
        affinees.append(item)
    return affinees


def _txt_distance(st: dict, cible: str) -> str:
    if st.get("distance_source") == "ign":
        return f"Ã  {_km_txt(st['distance_m'])} de {cible} par la route"
    return f"Ã  environ {_km_txt(st['distance_m'])} de {cible} (estimation, itinÃ©raire IGN indisponible)"


# â”€â”€ VÃ©hicules Ã©lectriques â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Source : stations-service qui dÃ©clarent une borne de recharge dans prix-carburants.gouv.fr.
# Ce n'est PAS un recensement complet des bornes (IRVE) : l'utilisateur en est averti.
ELECTRIQUE_CLES = {"electrique", "Ã©lectrique", "electric", "ev", "vehicule_electrique", "bev"}
CONSO_DEFAUT_KWH_100 = 17.0
RAYON_RECHARGE_DEPART_M = 12_000
RAYON_RECHARGE_ETAPE_M = 8_000


def est_electrique(valeur) -> bool:
    return str(valeur or "").strip().lower() in ELECTRIQUE_CLES


def _a_borne(services) -> bool:
    for srv in (services or []):
        t = unicodedata.normalize("NFKD", str(srv)).encode("ascii", "ignore").decode().lower()
        if "borne" in t or "recharge" in t or "irve" in t or "vehicule electrique" in t:
            return True
    return False


async def _stations_avec_borne_autour(lat: float, lon: float, rayon_m: float) -> list:
    """Stations avec borne autour d'un point. Rayon progressif (rayon, puis 20 km, puis 40 km)
    jusqu'Ã  trouver au moins 3 stations : un parcours en zone rurale ne reste pas sans rÃ©sultat."""
    sortie: list = []
    for rayon in sorted({float(rayon_m), 20_000.0, 40_000.0}):
        if rayon < rayon_m:
            continue
        dlat = rayon / 111_320.0
        dlon = rayon / (111_320.0 * max(0.2, math.cos(math.radians(lat))))
        lignes = await fetch_all(
            """
            SELECT id, nom, marque, adresse, code_postal, commune, latitude, longitude,
                   type_route, automate_24_24, horaires, carburants_rupture, services
            FROM stations_carburant
            WHERE latitude BETWEEN %s AND %s AND longitude BETWEEN %s AND %s
            """,
            (lat - dlat, lat + dlat, lon - dlon, lon + dlon),
        )
        sortie = []
        for s in lignes:
            if not _a_borne(s.get("services")):
                continue
            d = haversine_metres(lat, lon, float(s["latitude"]), float(s["longitude"]))
            if d > rayon:
                continue
            item = dict(s)
            item["prix"] = 1.0                 # valeur neutre : seule la distance classe les bornes
            item["distance_m"] = d
            item["distance_vol_m"] = d         # prÃ©sÃ©lection seulement ; remplacÃ© par l'IGN ensuite
            sortie.append(item)
        if len(sortie) >= 3:
            break
    return sortie


def _format_recharge(s: dict, quand: datetime | None) -> dict:
    return {
        "id": s["id"], "nom": _nom_station(s), "marque": s.get("marque"), "adresse": _adresse(s),
        "latitude": float(s["latitude"]), "longitude": float(s["longitude"]),
        "prix": None, "distance_m": round(s["distance_m"]),
        "duree_secondes": s.get("duree_s"),
        "distance_source": s.get("distance_source", "vol_oiseau"),
        "autoroute": s.get("type_route") == "A",
        "automate_24_24": s.get("automate_24_24"),
        "ouverture": statut_ouverture(s, quand),
        "ruptures": [], "borne_electrique": True,
        "services_principaux": _nettoyer_services(s.get("services"))[:6],
        "maj_prix": None, "maj_libelle": None, "age_jours": None, "prix_ancien": False,
        "economie_nette_eur": 0,
    }


async def _plan_recharge(**kw) -> dict:
    """Enveloppe : une erreur imprÃ©vue donne un message clair au lieu de faire disparaÃ®tre le volet."""
    try:
        return await _plan_recharge_interne(**kw)
    except Exception:
        logger.exception("Volet recharge Ã©lectrique indisponible")
        return {"actif": False, "raison": "Les bornes de recharge sont momentanÃ©ment indisponibles pour ce parcours."}


async def _plan_recharge_interne(*, depart, etapes, planning_horaire, distance_m, consommation,
                                 date_sortie, heure_depart) -> dict:
    try:
        conso = min(40.0, max(8.0, float(consommation)))
    except (TypeError, ValueError):
        conso = CONSO_DEFAUT_KWH_100
    distance_km = float(distance_m) / 1000.0 if distance_m else None
    kwh = distance_km * conso / 100.0 if distance_km else None

    depart_dt, arrivees = _dates_planning(date_sortie, heure_depart, planning_horaire)
    points = []
    if depart and depart.get("latitude") is not None:
        points.append({"role": "depart", "ordre": 0, "nom": depart.get("nom") or "Votre dÃ©part",
                       "lat": float(depart["latitude"]), "lon": float(depart["longitude"]),
                       "quand": depart_dt, "rayon": RAYON_RECHARGE_DEPART_M})
    for i, e in enumerate(etapes, start=1):
        points.append({"role": "etape", "ordre": i, "nom": e.get("nom") or f"Ã‰tape {i}",
                       "lat": float(e["latitude"]), "lon": float(e["longitude"]), "lieu_id": e.get("id"),
                       "quand": arrivees.get(i), "rayon": RAYON_RECHARGE_ETAPE_M})

    candidates = await asyncio.gather(*(_stations_avec_borne_autour(p["lat"], p["lon"], p["rayon"]) for p in points))
    if not any(candidates):
        rempli = await fetch_one("SELECT count(*) AS n FROM stations_carburant")
        if not rempli or not rempli.get("n"):
            return {"actif": False, "raison": "Les donnÃ©es de stations ne sont pas encore chargÃ©es."}
        return {"actif": False, "raison": (
            "Aucune borne de recharge n'est rÃ©fÃ©rencÃ©e autour de ce parcours dans nos donnÃ©es "
            "(stations-service avec borne). Consultez l'application de votre opÃ©rateur de recharge.")}

    affinees = await asyncio.gather(*(
        _affiner_par_route(p, c, 30.0, conso, restreindre=False) for p, c in zip(points, candidates)))

    sorties, conseils, avertissements = [], [], []
    for p, brutes, stations in zip(points, candidates, affinees):
        formatees = [_format_recharge(s, p["quand"]) for s in stations]
        ouvertes = [x for x in formatees if x["ouverture"]["etat"] != "fermee"]
        formatees = ouvertes or formatees
        formatees.sort(key=lambda x: x["distance_m"])
        sorties.append({
            "role": p["role"], "ordre": p["ordre"], "nom": p["nom"],
            "latitude": p["lat"], "longitude": p["lon"],
            "passage": p["quand"].isoformat() if p["quand"] else None,
            "nb_stations": len(brutes), "nb_rupture": 0, "stations": formatees[:3],
        })
        if not formatees:
            lieu = "votre dÃ©part" if p["role"] == "depart" else "l'Ã©tape %s - %s" % (p["ordre"], p["nom"])
            avertissements.append(
                "Aucune borne de recharge rÃ©fÃ©rencÃ©e prÃ¨s de %s : prÃ©voyez une recharge avant ou aprÃ¨s." % lieu)

    if kwh:
        conseils.append({"icone": "fa-charging-station", "texte": (
            f"Pour {distance_km:.0f} km avec {str(round(conso, 1)).replace('.', ',')} kWh/100 km, comptez environ "
            f"{kwh:.0f} kWh. Les tarifs de recharge dÃ©pendent de l'opÃ©rateur : Pelify ne les affiche pas.")})
    depart_sortie = next((x for x in sorties if x["role"] == "depart"), None)
    if depart_sortie and depart_sortie["stations"]:
        st = depart_sortie["stations"][0]
        conseils.append({"icone": "fa-flag-checkered", "texte": (
            f"Avant de partir : {st['nom']}, {_txt_distance(st, 'votre dÃ©part')}, borne de recharge indiquÃ©e. "
            f"{st['ouverture']['libelle']}.")})
    avertissements.append(
        "Cette liste vient des stations-service qui dÃ©clarent une borne de recharge : elle n'est pas "
        "exhaustive. VÃ©rifiez la disponibilitÃ© et la compatibilitÃ© de votre prise sur l'application de votre opÃ©rateur.")

    return {
        "actif": True, "type": "electrique", "carburant": "electrique", "carburant_libelle": "Ã‰lectrique",
        "consommation_kwh_100km": conso, "distance_km": round(distance_km, 1) if distance_km else None,
        "energie_estimee_kwh": round(kwh, 1) if kwh else None,
        "cout_estime_eur": None, "prix_moyen_zone": None,
        "points": sorties, "conseils": conseils, "avertissements": avertissements,
        "maj_donnees": None,
        "source": "prix-carburants.gouv.fr (stations dÃ©clarant une borne de recharge)",
    }


async def plan_carburant(*, depart, etapes, planning_horaire, distance_m, carburant, consommation,
                         date_sortie, heure_depart, mode) -> dict | None:
    """PrÃ©pare les conseils carburant. Renvoie None si le sujet ne s'applique pas."""
    if mode == "driving-car" and est_electrique(carburant):
        return await _plan_recharge(
            depart=depart, etapes=etapes, planning_horaire=planning_horaire, distance_m=distance_m,
            consommation=consommation, date_sortie=date_sortie, heure_depart=heure_depart)
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
        points.append({"role": "depart", "ordre": 0, "nom": depart.get("nom") or "Votre dÃ©part",
                       "lat": float(depart["latitude"]), "lon": float(depart["longitude"]),
                       "quand": depart_dt, "rayon": RAYON_DEPART_M})
    for i, e in enumerate(etapes, start=1):
        points.append({"role": "etape", "ordre": i, "nom": e.get("nom") or f"Ã‰tape {i}",
                       "lat": float(e["latitude"]), "lon": float(e["longitude"]), "lieu_id": e.get("id"),
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
            return {"actif": False, "raison": "Les prix des carburants ne sont pas encore chargÃ©s."}
        return {"actif": False, "raison": f"Aucune station avec un prix de {libelle} connu autour de ce parcours."}

    prix_ref = statistics.median(prix_vus.values())
    plus_recent = None
    for r in resultats:
        for s in r["stations"]:
            m = s.get("maj_effective")
            if m and (plus_recent is None or m > plus_recent):
                plus_recent = m

    sorties, conseils, avertissements = [], [], []
    affinees = await asyncio.gather(*(_affiner_par_route(p, r["stations"], achat_l, conso) for p, r in zip(points, resultats)))
    for p, r, stations_ign in zip(points, resultats, affinees):
        top = _choisir(stations_ign, achat_l, conso, prix_ref, p["quand"])
        sorties.append({
            "role": p["role"], "ordre": p["ordre"], "nom": p["nom"],
            "latitude": p["lat"], "longitude": p["lon"],
            "passage": p["quand"].isoformat() if p["quand"] else None,
            "nb_stations": r["nb_total"], "nb_rupture": r["nb_rupture"], "stations": top,
        })
        if r["nb_total"] >= 3 and r["nb_rupture"] >= 2 and r["nb_rupture"] / r["nb_total"] >= 0.3:
            avertissements.append(
                f"Rupture de {libelle} signalÃ©e dans {r['nb_rupture']} station(s) sur {r['nb_total']} "
                f"autour de {p['nom']} : prÃ©voyez une solution de repli."
            )

    def nom_point(x):
        return "votre dÃ©part" if x["role"] == "depart" else f"l'Ã©tape {x['ordre']} - {x['nom']}"

    if litres:
        cout = litres * prix_ref
        conseils.append({"icone": "fa-gas-pump", "texte": (
            f"Pour {distance_km:.0f} km avec {str(round(conso, 1)).replace('.', ',')} L/100 km, comptez environ {litres:.1f} L de {libelle}, "
            f"soit environ {_eur_txt(cout)} â‚¬ au prix moyen constatÃ© autour de votre parcours "
            f"({_prix_txt(prix_ref)} â‚¬/L). Modifiez la consommation dans vos critÃ¨res pour affiner.")})
        if litres > RESERVOIR_L * 0.9:
            avertissements.append(
                f"Ce parcours consomme environ {litres:.0f} L : prÃ©voyez au moins un plein en cours de route.")

    meilleurs = [(s, x) for s in sorties for x in s["stations"][:1]]
    depart_sortie = next((s for s in sorties if s["role"] == "depart"), None)
    if depart_sortie and depart_sortie["stations"]:
        st = depart_sortie["stations"][0]
        eco = f", soit environ {_eur_txt(st['economie_nette_eur'])} â‚¬ d'Ã©conomie sur un plein" if st["economie_nette_eur"] >= 1 else ""
        conseils.append({"icone": "fa-flag-checkered", "texte": (
            f"Avant de partir : {st['nom']}, {_txt_distance(st, 'votre dÃ©part')}, "
            f"{_prix_txt(st['prix'])} â‚¬/L{eco}. {st['ouverture']['libelle']}.")})
    if meilleurs:
        s, st = min(meilleurs, key=lambda t: t[1]["prix"])
        if not (depart_sortie and s is depart_sortie):
            eco = f" (environ {_eur_txt(st['economie_nette_eur'])} â‚¬ d'Ã©conomie sur un plein)" if st["economie_nette_eur"] >= 1 else ""
            conseils.append({"icone": "fa-piggy-bank", "texte": (
                f"Le carburant le moins cher de votre parcours se trouve prÃ¨s de {nom_point(s)} : {st['nom']}, "
                f"{_prix_txt(st['prix'])} â‚¬/L{eco}. {st['ouverture']['libelle']}.")})
    for s in sorties:
        if s["stations"] and s["stations"][0]["ouverture"]["etat"] == "fermee":
            avertissements.append(
                f"PrÃ¨s de {nom_point(s)}, les stations trouvÃ©es seront fermÃ©es Ã  votre passage "
                f"({s['stations'][0]['ouverture']['libelle']}). Anticipez votre plein.")
    anciens = [x for _, x in meilleurs if x["prix_ancien"]]
    if anciens:
        pire = max((x["age_jours"] for x in anciens if x["age_jours"] is not None), default=None)
        avertissements.append(
            "Certains prix conseillÃ©s ne sont pas rÃ©cents"
            + (f" (jusqu'Ã  {pire} jours)" if pire is not None else "")
            + " : vÃ©rifiez-les sur place avant de vous arrÃªter.")

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


# â”€â”€ Fiche complÃ¨te d'une station (commoditÃ© Â« station_service Â») â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Sert /api/lieux/{id}/amenities. Ne contient aucune notion d'heure courante :
# cette route est mise en cache 1 h par le CDN, l'Ã©tat Â« ouvert maintenant Â» et
# l'Ã¢ge des prix sont donc calculÃ©s cÃ´tÃ© navigateur Ã  partir des donnÃ©es brutes
# ci-dessous (horaires de la semaine, dates de mise Ã  jour).

_ORDRE_CARBURANTS = ("gazole", "e10", "sp95", "sp98", "e85", "gplc")

# (mot-clÃ© sans accent, icÃ´ne FontAwesome 6, groupe). Le premier mot-clÃ© trouvÃ© gagne ;
# l'ordre des groupes dÃ©finit l'ordre d'affichage.
_GROUPES_SERVICES = ("confort", "boutique", "auto", "energie", "poids_lourds", "paiement", "autre")
_SERVICES_ICONES = (
    ("toilette", "fa-restroom", "confort"),
    ("douche", "fa-shower", "confort"),
    ("espace bebe", "fa-baby", "confort"),
    ("wifi", "fa-wifi", "confort"),
    ("boutique alimentaire", "fa-basket-shopping", "boutique"),
    ("boutique non alimentaire", "fa-store", "boutique"),
    ("restauration sur place", "fa-utensils", "boutique"),
    ("restauration a emporter", "fa-burger", "boutique"),
    ("relais colis", "fa-box", "boutique"),
    ("laverie", "fa-jug-detergent", "boutique"),
    ("gonflage", "fa-wind", "auto"),
    ("lavage", "fa-soap", "auto"),
    ("reparation", "fa-wrench", "auto"),
    ("location de vehicule", "fa-car", "auto"),
    ("additif", "fa-droplet", "auto"),
    ("carburant additive", "fa-droplet", "auto"),
    ("borne", "fa-charging-station", "energie"),
    ("gaz domestique", "fa-fire-flame-simple", "energie"),
    ("petrole lampant", "fa-oil-can", "energie"),
    ("fioul", "fa-oil-can", "energie"),
    ("piste poids lourds", "fa-truck", "poids_lourds"),
    ("dab", "fa-money-bill-1", "paiement"),
    ("automate cb", "fa-credit-card", "paiement"),
)


def _sans_accents(texte) -> str:
    t = unicodedata.normalize("NFD", str(texte or "").lower())
    return "".join(c for c in t if unicodedata.category(c) != "Mn")


def _nettoyer_services(services) -> list[str]:
    """La source dÃ©coupe certains libellÃ©s sur Â« / Â» et Â« , Â» :
    Â« Automate CB 24/24 Â» devient Â« Automate CB 24 Â» + Â« 24 Â», et
    Â« Services rÃ©paration, entretien Â» devient deux entrÃ©es. On les recolle."""
    brut = [str(x).strip() for x in (services or []) if str(x).strip()]
    bas = [_sans_accents(x) for x in brut]
    resultat: list[str] = []
    for i, (txt, b) in enumerate(zip(brut, bas)):
        if b == "24" and any(y.startswith("automate cb") for y in bas):
            continue
        if b == "entretien" and any(y == "services reparation" for y in bas):
            continue
        if b.startswith("automate cb") and b.endswith("24") and "24" in bas[i + 1:i + 2]:
            txt = "Automate CB 24/24"
        elif b == "services reparation" and "entretien" in bas[i + 1:i + 2]:
            txt = "Services rÃ©paration, entretien"
        if txt not in resultat:
            resultat.append(txt)
    return resultat


def _services_detailles(services) -> list[dict]:
    """[{libelle, icone, groupe}] triÃ©s par groupe puis par ordre d'origine."""
    sortie = []
    for txt in _nettoyer_services(services):
        b = _sans_accents(txt)
        icone, groupe = "fa-circle-check", "autre"
        for mot, ic, gr in _SERVICES_ICONES:
            if mot in b:
                icone, groupe = ic, gr
                break
        sortie.append({"libelle": txt, "icone": icone, "groupe": groupe})
    sortie.sort(key=lambda x: _GROUPES_SERVICES.index(x["groupe"]))
    return sortie


def _hhmm(valeur) -> str | None:
    m = _minutes(valeur)
    return None if m is None else f"{m // 60:02d}:{m % 60:02d}"


def horaires_semaine(valeur, automate=None) -> list[dict] | None:
    """Horaires structurÃ©s du lundi au dimanche :
    [{jour, ouvert(True/False/None), ouverture('HH:MM'), fermeture('HH:MM'), plages(['HH:MM-HH:MM']),
      tout_le_jour}]. Renvoie None si la source ne donne aucun horaire exploitable."""
    h = _horaires_dict(valeur)
    if not h:
        return None
    jours = []
    for nom in _JOURS:
        j = h.get(nom)
        if not isinstance(j, dict):
            jours.append({"jour": nom, "ouvert": None, "ouverture": None, "fermeture": None,
                          "plages": [], "tout_le_jour": False})
            continue
        ouvert_brut = str(j.get("ouvert"))
        ouvert = False if ouvert_brut in ("0", "False", "false") else (True if ouvert_brut in ("1", "True", "true") else None)
        plages = [f"{o // 60:02d}:{o % 60:02d}-{f // 60:02d}:{f % 60:02d}" for o, f in _plages_depuis(j)]
        o, f = _hhmm(j.get("ouverture")), _hhmm(j.get("fermeture"))
        tout = bool(ouvert and len(plages) <= 1 and o == "00:00" and f in ("23:59", "00:00"))
        jours.append({"jour": nom, "ouvert": ouvert, "ouverture": o, "fermeture": f,
                      "plages": plages, "tout_le_jour": tout})
    return jours


def fiche_station(s: dict) -> dict:
    """Toutes les informations utiles Ã  un conducteur, Ã  partir d'une ligne de
    stations_carburant : identitÃ©, territoire, type de voie, automate 24/24,
    horaires, carburants (prix, rupture, date), services."""
    dispo = set(s.get("carburants_dispo") or [])
    rupture = set(s.get("carburants_rupture") or [])
    carburants = []
    for cle in _ORDRE_CARBURANTS:
        libelle, col, code = CARBURANTS[cle]
        prix = s.get(col)
        prix = float(prix) if prix is not None else None
        en_rupture = code in rupture
        # Le champ Â« fuel Â» de la source est parfois incomplet alors que le prix existe :
        # un prix connu ou une rupture signalÃ©e suffit Ã  dire que le carburant est proposÃ©.
        if prix is None and code not in dispo and not en_rupture:
            continue
        maj = _date_maj(s, cle)
        carburants.append({
            "cle": cle, "libelle": libelle, "prix": prix, "rupture": en_rupture,
            "maj": maj.isoformat() if maj else None,
        })
    maj_station = s.get("maj_prix")
    return {
        "id": s.get("id"),
        "marque": s.get("marque"),
        "nom": _nom_station(s),
        "adresse_complete": _adresse(s),
        "commune": (s.get("commune") or "").title() or None,
        "code_postal": s.get("code_postal"),
        "code_insee": s.get("code_insee"),
        "epci": s.get("epci_nom"),
        "departement": s.get("dep_nom"),
        "dep_code": s.get("dep_code"),
        "region": s.get("region"),
        "autoroute": s.get("type_route") == "A",
        "automate_24_24": s.get("automate_24_24"),
        "horaires": horaires_semaine(s.get("horaires"), s.get("automate_24_24")),
        "carburants": carburants,
        "services": _services_detailles(s.get("services")),
        "maj_prix": maj_station.isoformat() if maj_station else None,
    }


async def fiches_stations_du_lieu(lieu_id: int) -> dict[int, dict]:
    """{rang: fiche} pour les stations prÃ©calculÃ©es d'un lieu (categorie 'station_service').
    Le rang sert de clÃ© de rapprochement avec les lignes d'amenity_cache."""
    lignes = await fetch_all(
        """
        SELECT ac.rang,
               s.id, s.nom, s.marque, s.adresse, s.code_postal, s.commune, s.code_insee,
               s.epci_nom, s.dep_code, s.dep_nom, s.region, s.type_route, s.automate_24_24,
               s.horaires, s.carburants_dispo, s.carburants_rupture,
               s.prix_gazole, s.prix_sp95, s.prix_sp98, s.prix_e10, s.prix_e85, s.prix_gplc,
               s.maj_prix, s.maj_carburants, s.services
        FROM amenity_cache ac
        JOIN stations_carburant s ON s.id = ac.station_id
        WHERE ac.lieu_tournage_id = %s AND ac.categorie = 'station_service'
        """,
        (lieu_id,),
    )
    return {int(l["rang"]): fiche_station(l) for l in lignes}
