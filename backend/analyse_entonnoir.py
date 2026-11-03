"""
backend/analyse_entonnoir.py

Analyse territoriale « en entonnoir » des lieux de tournage d'Occitanie.

    GET /api/analyse/entonnoir?region=Occitanie

Question posée : « Chaque lieu de tournage peut-il accueillir des visiteurs ? »

L'analyse va du plus large au plus précis, en sept parties :
    A. Cadrer      – de combien de lieux parle-t-on, où sont-ils ?
    B. Choisir     – quels indicateurs, selon quels critères (et lesquels on écarte)
    C. Mesurer     – un indicateur par section (dormir, manger, se garer, se déplacer, s'informer,
                     se soigner, choix de l'offre, variété de l'offre)
    D. Noter       – du chiffre à la note sur 100, et solidité de cette note
    E. Comparer    – les départements entre eux (écarts réels ou dus au hasard ?)
    F. Approfondir – le département qui compte le plus de lieux de tournage
    G. Conclure    – où agir en priorité, et ce que l'analyse ne sait pas dire

Principes :
- Les durées viennent des itinéraires IGN précalculés (amenity_cache). Quand un trajet n'est pas encore
  calculé, on l'estime à partir de la ligne droite ET on le signale (part « estimée » mesurée au chapitre
  fiabilité) : on ne remplace jamais une donnée manquante par un zéro silencieux.
- Les lieux dont l'environnement n'a pas été mesuré sont EXCLUS de la note (jamais notés « 0 »).
- Toute proportion est accompagnée de sa fourchette plausible (Wilson), toute médiane aussi (bootstrap).
- Les écarts entre départements sont testés (Kruskal-Wallis, Mann-Whitney) et traduits en phrases simples.
- Les textes sont produits par des règles publiques à partir des chiffres : aucune IA, aucune saisie manuelle.
"""
from __future__ import annotations

import json
import logging
import math
import time
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query

import statistiques_simples as S
from db import fetch_all, fetch_one

logger = logging.getLogger(__name__)
router = APIRouter()

_CACHE: dict = {"t": 0.0, "region": None, "data": None}
TTL_S = 3600

# ─────────────────────────── paramètres de l'analyse ───────────────────────────

SEUIL_COMPARABLE = 10        # nombre minimal de lieux notés pour comparer un département
SEUIL_COMMUNE = 3            # nombre minimal de lieux pour comparer une commune
VITESSE_VOITURE_M_MIN = 750  # 45 km/h : moyenne réaliste en zone rurale et de montagne (estimation de repli)
VITESSE_PIED_M_MIN = 75      # 4,5 km/h
PLAFOND_ESSENTIELS = 59.0    # un lieu sans l'un des 3 essentiels ne peut pas dépasser « accueil moyen »
CATEGORIES_VARIETE = ["hebergement", "restaurant", "parking", "arret_bus", "office_tourisme", "activite"]
CATEGORIES_CHARGEES = ["hebergement", "restaurant", "parking", "arret_bus", "gare", "aeroport",
                       "office_tourisme", "hopital", "police", "distributeur", "activite"]
LIBELLE_CATEGORIE = {
    "hebergement": "hébergement", "restaurant": "restaurant", "parking": "parking", "arret_bus": "arrêt de bus",
    "gare": "gare", "aeroport": "aéroport", "office_tourisme": "office de tourisme", "hopital": "hôpital",
    "police": "police ou gendarmerie", "distributeur": "distributeur ou banque", "activite": "activité de loisir",
}

# Niveaux d'accueil (note sur 100)
CLASSES = [
    ("tres_bon", "Très bon accueil", 75.0, "#2a9d8f"),
    ("bon", "Bon accueil", 60.0, "#8ac926"),
    ("moyen", "Accueil moyen", 45.0, "#ffca3a"),
    ("limite", "Accueil limité", 30.0, "#ff924c"),
    ("tres_limite", "Accueil très limité", 0.0, "#e63946"),
]

# Les huit indicateurs retenus. « alts » : (catégorie, mode, seuil confortable en min, seuil limite en min).
# Entre les deux seuils, la note baisse linéairement de 100 à 0.
INDICATEURS = [
    {"cle": "dormir", "nom": "Dormir", "titre": "L'hébergement le plus proche", "axe": "sejour", "poids": 18, "essentiel": True,
     "type": "temps", "alts": [("hebergement", "voiture", 10, 30)],
     "besoin": "Pouvoir passer la nuit près du lieu de tournage.",
     "mesure": "Durée de trajet en voiture jusqu'à l'hébergement le plus proche (hôtel, gîte, camping…).",
     "justif": "Jusqu'à 10 minutes, le visiteur dort « sur place ». Au-delà de 30 minutes, le lieu n'est plus une étape de séjour : on y vient en excursion."},
    {"cle": "manger", "nom": "Manger", "titre": "Le restaurant le plus proche", "axe": "sejour", "poids": 18, "essentiel": True,
     "type": "temps", "alts": [("restaurant", "voiture", 10, 25)],
     "besoin": "Pouvoir déjeuner ou dîner sans détour excessif.",
     "mesure": "Durée de trajet en voiture jusqu'au restaurant le plus proche.",
     "justif": "Un repas s'insère dans une visite si l'aller-retour reste court : 10 minutes est confortable, 25 minutes devient une vraie contrainte."},
    {"cle": "garer", "nom": "Se garer", "titre": "Le parking le plus proche", "axe": "acces", "poids": 12, "essentiel": True,
     "type": "temps", "alts": [("parking", "voiture", 3, 15)],
     "besoin": "Pouvoir laisser son véhicule à proximité.",
     "mesure": "Durée de trajet en voiture jusqu'au parking le plus proche.",
     "justif": "Un parking à 3 minutes est « en vue » du lieu ; à 15 minutes il ne sert plus la visite."},
    {"cle": "transport", "nom": "Se déplacer", "titre": "Arrêt de bus ou gare à proximité", "axe": "acces", "poids": 12, "essentiel": False,
     "type": "temps", "alts": [("arret_bus", "pied", 8, 25), ("gare", "voiture", 15, 45)],
     "besoin": "Pouvoir venir sans voiture personnelle.",
     "mesure": "Meilleure des deux options : arrêt de bus à pied, ou gare en voiture.",
     "justif": "Un arrêt à 8 minutes à pied est utilisable ; une gare à 15 minutes en voiture permet une arrivée en train puis taxi ou navette."},
    {"cle": "informer", "nom": "S'informer", "titre": "L'office de tourisme le plus proche", "axe": "prise_en_charge", "poids": 10, "essentiel": False,
     "type": "temps", "alts": [("office_tourisme", "voiture", 15, 45)],
     "besoin": "Trouver cartes, conseils, réservations.",
     "mesure": "Durée de trajet en voiture jusqu'à l'office de tourisme le plus proche.",
     "justif": "L'office sert à préparer ou compléter la visite : 15 minutes reste raisonnable, 45 minutes le rend inaccessible en pratique."},
    {"cle": "soigner", "nom": "Se soigner", "titre": "L'hôpital le plus proche", "axe": "prise_en_charge", "poids": 10, "essentiel": False,
     "type": "temps", "alts": [("hopital", "voiture", 20, 60)],
     "besoin": "Être pris en charge en cas d'accident ou de malaise.",
     "mesure": "Durée de trajet en voiture jusqu'à l'hôpital le plus proche.",
     "justif": "En cas d'urgence, 20 minutes est un délai courant en France ; au-delà d'une heure, l'éloignement devient un risque pour les sites isolés ou de montagne."},
    {"cle": "choix", "nom": "Avoir le choix", "titre": "Hébergements, restaurants et activités à moins de 1 km", "axe": "sejour", "poids": 12, "essentiel": False,
     "type": "choix", "alts": [],
     "besoin": "Ne pas dépendre d'une seule adresse.",
     "mesure": "Nombre total d'hébergements, de restaurants et d'activités de loisir à moins de 1 km (à vol d'oiseau).",
     "justif": "Les premières adresses comptent beaucoup, les suivantes de moins en moins : la note progresse vite jusqu'à 3 équipements (50/100) puis plafonne à 15 équipements (100/100)."},
    {"cle": "diversite", "nom": "Variété de l'offre", "titre": "Types d'équipements présents à moins de 1 km", "axe": "sejour", "poids": 8, "essentiel": False,
     "type": "diversite", "alts": [],
     "besoin": "Trouver de quoi dormir, manger, se garer, se déplacer, s'informer et s'occuper au même endroit.",
     "mesure": "Nombre de types d'équipements présents à moins de 1 km, parmi six : hébergement, restaurant, parking, arrêt de bus, office de tourisme, activité.",
     "justif": "Une offre variée évite les déplacements multiples : 6 types sur 6 donne 100/100, 3 types donnent 50/100."},
]
AXES = {"sejour": "Séjourner sur place", "acces": "Venir et se garer", "prise_en_charge": "Être renseigné et secouru"}
IND_PAR_CLE = {i["cle"]: i for i in INDICATEURS}
ESSENTIELS = [i["cle"] for i in INDICATEURS if i["essentiel"]]
POIDS_BASE = {i["cle"]: float(i["poids"]) for i in INDICATEURS}

# Indicateurs candidats ÉCARTÉS (examinés avec les mêmes critères, pour justifier de ne pas les garder).
CANDIDATS_ECARTES = [
    {"cle": "police", "nom": "Police ou gendarmerie", "type": "temps", "alts": [("police", "voiture", 10, 30)],
     "pertinence": "faible",
     "raison": "La sécurité compte, mais elle ne décide pas d'une visite : un site sans gendarmerie proche n'est pas pour autant « inaccueillant »."},
    {"cle": "distributeur", "nom": "Distributeur ou banque", "type": "temps", "alts": [("distributeur", "voiture", 10, 30)],
     "pertinence": "faible",
     "raison": "Le paiement par carte est devenu courant : cet équipement ne change plus la capacité d'accueil d'un lieu."},
    {"cle": "aeroport", "nom": "Aéroport", "type": "temps", "alts": [("aeroport", "voiture", 60, 150)],
     "pertinence": "faible",
     "raison": "L'aéroport décrit l'accès à la région, pas l'accueil du lieu lui-même : il ne dépend que de la géographie."},
]
NOTORIETE = {"cle": "notoriete", "nom": "Notoriété de l'œuvre", "pertinence": "faible",
             "raison": "La notoriété mesure la demande potentielle, pas la capacité à accueillir : elle est utilisée plus loin pour fixer les priorités, pas pour noter."}


# ───────────────────────────── mise en forme ─────────────────────────────

def _norm(s) -> str:
    t = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().lower()
    return " ".join("".join(c if c.isalnum() else " " for c in t).split())


def _fr(x, d: int = 0) -> str:
    if x is None:
        return "n.d."
    return f"{x:,.{d}f}".replace(",", "\u202f").replace(".", ",")


def _pct(x, d: int = 0) -> str:
    return "n.d." if x is None else f"{_fr(100 * x, d)} %"


def _min(t) -> str:
    if t is None:
        return "aucun trouvé"
    t = float(t)
    if t < 60:
        return f"{_fr(t, 0)} min"
    h, m = divmod(int(round(t)), 60)
    return f"{h} h {m:02d}"


def _s(n: int, un: str = "", plusieurs: str = "s") -> str:
    return un if n < 2 else plusieurs


# Articles des départements : « le Gard », « l'Hérault », « la Lozère », « les Hautes-Pyrénées »…
_ARTICLE_DEP = {
    "Ariège": "l'", "Aude": "l'", "Aveyron": "l'", "Hérault": "l'", "Gard": "le ", "Gers": "le ", "Lot": "le ", "Tarn": "le ",
    "Tarn-et-Garonne": "le ", "Lozère": "la ", "Haute-Garonne": "la ", "Hautes-Pyrénées": "les ", "Pyrénées-Orientales": "les ",
}


def _le(nom: str, maj: bool = False) -> str:
    """« l'Hérault », « le Gard »… (avec majuscule si maj)."""
    t = _ARTICLE_DEP.get(nom, "") + nom
    return t[0].upper() + t[1:] if maj else t


def _du(nom: str) -> str:
    """« de l'Hérault », « du Gard », « de la Lozère », « des Hautes-Pyrénées »."""
    a = _ARTICLE_DEP.get(nom, "")
    return {"l'": "de l'", "le ": "du ", "la ": "de la ", "les ": "des ", "": "de "}[a] + nom if a != "" else (f"d'{nom}" if nom[:1] in "AEIOUÉÈH" else f"de {nom}")


def _dans(nom: str) -> str:
    a = _ARTICLE_DEP.get(nom, "")
    return {"l'": "dans l'", "le ": "dans le ", "la ": "dans la ", "les ": "dans les ", "": "en "}[a] + nom


def _liste(items) -> str:
    items = [str(i) for i in items]
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " et " + items[-1] if items else ""


def _fp(p) -> str:
    """p-value lisible."""
    if p is None:
        return "n.d."
    return "< 0,001" if p < 0.001 else _fr(p, 3)


def _fourchette_pct(ic) -> str:
    return "n.d." if not ic else f"{_fr(100 * ic[0])} à {_fr(100 * ic[1])} %"


def _fourchette(ic, d: int = 0) -> str:
    return "n.d." if not ic else f"{_fr(ic[0], d)} à {_fr(ic[1], d)}"


def _lecture_p(p) -> str:
    if p is None:
        return "test impossible (trop peu de données)"
    if p < 0.01:
        return "écart réel : très peu probable que ce soit dû au hasard"
    if p < 0.05:
        return "écart probablement réel, à confirmer avec davantage de données"
    return "écart non démontré : il peut s'agir d'un simple effet du hasard"


def _lecture_effet(e) -> str:
    if e is None:
        return ""
    return "faible" if e < 0.06 else "moyen" if e < 0.14 else "important"


def _classe(score: float) -> tuple:
    for c in CLASSES:
        if score >= c[2]:
            return c
    return CLASSES[-1]


def _med(v):
    return S.mediane(v) if v else None


# ───────────────────────────── données ─────────────────────────────

def _aire_anneau_km2(anneau: list) -> float:
    if len(anneau) < 4:
        return 0.0
    lat0 = sum(p[1] for p in anneau) / len(anneau)
    kx, ky = 111.320 * math.cos(math.radians(lat0)), 110.574
    pts = [(p[0] * kx, p[1] * ky) for p in anneau]
    return abs(sum(pts[i][0] * pts[(i + 1) % len(pts)][1] - pts[(i + 1) % len(pts)][0] * pts[i][1] for i in range(len(pts)))) / 2


def charger_departements() -> list[dict]:
    chemin = Path(__file__).resolve().parent.parent / "frontend" / "departements-occitanie.geojson"
    out = []
    try:
        data = json.loads(chemin.read_text(encoding="utf-8"))
        for f in data.get("features", []):
            pr, g = f.get("properties") or {}, f.get("geometry") or {}
            polys = g.get("coordinates") if g.get("type") == "MultiPolygon" else [g.get("coordinates")] if g.get("type") == "Polygon" else []
            code = pr.get("dep_code") or pr.get("dep_current_code") or pr.get("code") or ""
            code = str(code[0] if isinstance(code, list) and code else code)
            nom = pr.get("dep_name") or pr.get("nom") or code
            nom = str(nom[0] if isinstance(nom, list) and nom else nom)
            rings = [p[0] for p in polys if p]
            out.append({"code": code, "nom": nom, "rings": rings, "aire_km2": sum(_aire_anneau_km2(r) for r in rings)})
    except Exception:
        logger.warning("Contours des départements illisibles")
    return out


def _dept_du_point(lat: float, lon: float, departements: list[dict]):
    from meteo import _dans_anneau
    for d in departements:
        for r in d["rings"]:
            xs = [p[0] for p in r]
            ys = [p[1] for p in r]
            if min(xs) <= lon <= max(xs) and min(ys) <= lat <= max(ys) and _dans_anneau(lon, lat, r):
                return d
    return None


async def charger_donnees() -> dict:
    lieux = await fetch_all(
        """
        SELECT l.id, l.nom, l.commune, l.departement, l.latitude, l.longitude,
               f.id AS film_id, f.titre, f.annee, f.media_type, f.popularite
        FROM lieux_tournage l JOIN films f ON f.id = l.film_id AND f.statut = 'publie'
        """
    )
    mesures = await fetch_all(
        """
        SELECT lieu_tournage_id AS lieu_id, categorie,
               min(duree_voiture_secondes) AS t_voiture, min(duree_pied_secondes) AS t_pied,
               min(distance_metres) AS d_air,
               count(*) FILTER (WHERE distance_metres <= 1000) AS n1000
        FROM amenity_cache WHERE categorie = ANY(%s) GROUP BY 1, 2
        """,
        (CATEGORIES_CHARGEES,),
    )
    stats = await fetch_all(
        "SELECT lieu_tournage_id AS lieu_id, categorie, nombre_1000m, rayon_metres FROM amenity_stats WHERE categorie = ANY(%s)",
        (CATEGORIES_CHARGEES,),
    )
    ratio = await fetch_one(
        """
        SELECT count(*) AS n,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY distance_voiture_metres::numeric / distance_metres) AS mediane
        FROM amenity_cache WHERE distance_metres > 100 AND distance_voiture_metres IS NOT NULL
        """
    )

    def num(d):
        return {k: (float(v) if hasattr(v, "quantize") else v) for k, v in dict(d).items()}

    return {"lieux": [num(x) for x in lieux], "mesures": [num(x) for x in mesures], "stats": [num(x) for x in stats],
            "ratio": num(ratio) if ratio else None, "departements": charger_departements()}


# ───────────────────────────── notes ─────────────────────────────

def _note_temps(t, confort, limite, f: float = 1.0) -> float:
    """100 si le trajet est sous le seuil confortable, 0 au-delà du seuil limite, dégressif entre les deux."""
    if t is None:
        return 0.0
    c, l = confort * f, limite * f
    return 100.0 if t <= c else 0.0 if t >= l else 100.0 * (l - t) / (l - c)


def sous_note(ind: dict, brut, f: float = 1.0) -> float:
    if ind["type"] == "temps":
        return max(_note_temps(a["t"], c, l, f) for a, (_, _, c, l) in zip(brut, ind["alts"]))
    if ind["type"] == "choix":
        return 100.0 * min(1.0, math.log1p(brut["n"]) / math.log1p(15.0 * f))
    return 100.0 * brut["k"] / len(CATEGORIES_VARIETE)


def note_globale(subs: dict, poids: dict, plafond: bool = True) -> tuple[float, bool]:
    total = sum(poids[k] * subs[k] for k in poids) / sum(poids.values())
    if plafond and any(subs.get(k, 0) <= 0 for k in ESSENTIELS) and total > PLAFOND_ESSENTIELS:
        return PLAFOND_ESSENTIELS, True
    return total, False


def _brut_temps(alts, mes_l: dict, ratio: float) -> list[dict]:
    out = []
    for cat, mode, _, _ in alts:
        m = mes_l.get(cat)
        t, est = None, False
        if m:
            sec = m.get("t_voiture" if mode == "voiture" else "t_pied")
            if sec is not None:
                t = sec / 60.0
            elif m.get("d_air") is not None:
                vitesse = VITESSE_VOITURE_M_MIN if mode == "voiture" else VITESSE_PIED_M_MIN
                t, est = m["d_air"] * ratio / vitesse, True
        out.append({"t": t, "est": est, "mode": mode, "cat": cat})
    return out


def _compte_1km(cat: str, mes_l: dict, st_l: dict) -> int:
    if cat in st_l and st_l[cat].get("nombre_1000m") is not None:
        return int(st_l[cat]["nombre_1000m"])
    if cat in mes_l:
        return int(mes_l[cat].get("n1000") or 0)
    return 0


def _brut(ind: dict, mes_l: dict, st_l: dict, ratio: float):
    if ind["type"] == "temps":
        return _brut_temps(ind["alts"], mes_l, ratio)
    if ind["type"] == "choix":
        return {"n": sum(_compte_1km(c, mes_l, st_l) for c in ("hebergement", "restaurant", "activite"))}
    return {"k": sum(1 for c in CATEGORIES_VARIETE if _compte_1km(c, mes_l, st_l) > 0)}


def _valeur_affichee(ind: dict, brut, f: float = 1.0):
    """(valeur, estimée ?) : minutes de la meilleure option, nombre d'équipements ou de types."""
    if ind["type"] == "temps":
        meilleur = max(zip(brut, ind["alts"]), key=lambda z: (_note_temps(z[0]["t"], z[1][2], z[1][3], f), -(z[0]["t"] if z[0]["t"] is not None else 1e9)))
        return meilleur[0]["t"], meilleur[0]["est"]
    if ind["type"] == "choix":
        return brut["n"], False
    return brut["k"], False


def _classe_indicateur(ind: dict, valeur, note: float) -> int:
    """0 = très bien servi … 3 = hors de portée / insuffisant."""
    if ind["type"] == "temps":
        return 0 if note >= 100 else 1 if note >= 50 else 2 if note > 0 else 3
    if ind["type"] == "choix":
        return 3 if valeur == 0 else 2 if valeur < 5 else 1 if valeur < 15 else 0
    return 3 if valeur <= 1 else 2 if valeur <= 3 else 1 if valeur <= 5 else 0


LIBELLES_CLASSE_TEMPS = ["Confortable", "Acceptable", "Difficile", "Hors de portée"]
LIBELLES_CLASSE_CHOIX = ["15 équipements ou plus", "5 à 14 équipements", "1 à 4 équipements", "Aucun équipement"]
LIBELLES_CLASSE_VARIETE = ["6 types sur 6", "4 à 5 types", "2 à 3 types", "0 à 1 type"]
COULEURS_CLASSE_IND = ["#2a9d8f", "#8ac926", "#ff924c", "#e63946"]


def preparer(donnees: dict) -> dict:
    deps = donnees["departements"]
    par_norm = {_norm(d["nom"]): d for d in deps}
    mes, st = defaultdict(dict), defaultdict(dict)
    for m in donnees["mesures"]:
        mes[m["lieu_id"]][m["categorie"]] = m
    for s in donnees["stats"]:
        st[s["lieu_id"]][s["categorie"]] = s
    ratio = ((donnees.get("ratio") or {}).get("mediane")) or 1.3
    lieux = []
    for l in donnees["lieux"]:
        d = par_norm.get(_norm(l.get("departement"))) if l.get("departement") else None
        deduit = False
        if d is None and l.get("latitude") is not None:
            d = _dept_du_point(float(l["latitude"]), float(l["longitude"]), deps)
            deduit = d is not None
        x = dict(l)
        x["dept_code"], x["dept_nom"] = (d["code"], d["nom"]) if d else (None, None)
        x["dept_deduit"] = deduit
        x["mes"], x["st"] = mes.get(l["id"], {}), st.get(l["id"], {})
        x["mesure"] = bool(x["mes"] or x["st"])
        x["trajet_reel"] = any(m.get("t_voiture") is not None for m in x["mes"].values())
        x["commune_cle"] = _norm(l.get("commune"))
        if x["mesure"]:
            x["brut"] = {i["cle"]: _brut(i, x["mes"], x["st"], ratio) for i in INDICATEURS}
            x["val"], x["est"], x["sub"], x["cl"] = {}, {}, {}, {}
            for i in INDICATEURS:
                v, e = _valeur_affichee(i, x["brut"][i["cle"]])
                x["val"][i["cle"]], x["est"][i["cle"]] = v, e
                x["sub"][i["cle"]] = sous_note(i, x["brut"][i["cle"]])
                x["cl"][i["cle"]] = _classe_indicateur(i, v if v is not None else 0, x["sub"][i["cle"]])
            x["note"], x["plafonne"] = note_globale(x["sub"], POIDS_BASE)
            x["classe"] = _classe(x["note"])[0]
        lieux.append(x)
    return {"lieux": lieux, "departements": deps, "ratio": ratio, "ratio_mesure": bool((donnees.get("ratio") or {}).get("mediane")),
            "mes_brutes": mes, "st_brutes": st}


# ───────────────────────────── outils de construction ─────────────────────────────

def _chiffre(valeur, label, aide=""):
    return {"valeur": valeur, "label": label, "aide": aide}


def _sec(id_, titre, question, bref, chiffres=None, graphiques=None, tableaux=None, lecture=None, attention=None, detail=None):
    return {"id": id_, "titre": titre, "question": question, "en_bref": bref, "chiffres": chiffres or [],
            "graphiques": graphiques or [], "tableaux": tableaux or [], "lecture": [x for x in (lecture or []) if x],
            "attention": [a for a in (attention or []) if a], "detail": detail or []}


def _terme(terme, valeur, explication):
    return {"terme": terme, "valeur": valeur, "explication": explication}


def _mann_whitney(a: list[float], b: list[float]) -> dict | None:
    """Compare deux groupes. « prob_sup » = chance qu'un élément tiré au hasard dans a soit mieux noté qu'un élément de b."""
    n1, n2 = len(a), len(b)
    if n1 < 5 or n2 < 5:
        return None
    r = S.rangs(a + b)
    u1 = sum(r[:n1]) - n1 * (n1 + 1) / 2
    total = n1 + n2
    egalites = sum(t ** 3 - t for t in Counter(a + b).values())
    var = n1 * n2 / 12.0 * ((total + 1) - egalites / (total * (total - 1)))
    if var <= 0:
        return None
    z = (u1 - n1 * n2 / 2) / math.sqrt(var)
    return {"p": math.erfc(abs(z) / math.sqrt(2)), "prob_sup": u1 / (n1 * n2), "n1": n1, "n2": n2}


def _groupes_departements(L: list[dict]) -> dict[str, list[dict]]:
    g = defaultdict(list)
    for x in L:
        if x["dept_nom"]:
            g[x["dept_nom"]].append(x)
    return g


def _notes(L):
    return [x["note"] for x in L]


def _tableau_lieux(titre, lignes_lieux, extra_col=None, extra_fn=None):
    cols = ["Lieu (œuvre)", "Commune", "Département", "Note /100"] + ([extra_col] if extra_col else [])
    lignes = []
    for x in lignes_lieux:
        base = [f"{x['nom']} ({x['titre']})", x.get("commune") or "n.d.", x["dept_nom"] or "n.d.", _fr(x["note"], 0)]
        lignes.append(base + ([extra_fn(x)] if extra_fn else []))
    return {"titre": titre, "colonnes": cols, "lignes": lignes}


# ───────────────────────────── PARTIE A : cadrer ─────────────────────────────

def sec_perimetre(P: dict) -> dict:
    L = P["lieux"]
    n = len(L)
    localises = [x for x in L if x["dept_nom"]]
    mesures = [x for x in L if x["mesure"]]
    reels = [x for x in mesures if x["trajet_reel"]]
    oeuvres = len({x["film_id"] for x in L})
    communes = len({x["commune_cle"] for x in L if x["commune_cle"]})
    endroits = len({(round(float(x["latitude"]), 4), round(float(x["longitude"]), 4)) for x in L if x.get("latitude") is not None})
    nb_deps = len({x["dept_nom"] for x in localises})
    if n == 0:
        return _sec("perimetre", "Le périmètre : de combien de lieux parle-t-on ?", "", "Aucun lieu de tournage publié n'a été trouvé en base.")
    part_m = len(mesures) / n
    return _sec(
        "perimetre", "Le périmètre : de combien de lieux parle-t-on ?",
        "Avant de mesurer quoi que ce soit, il faut savoir combien de lieux sont réellement analysables.",
        f"La base compte **{_fr(n)} lieux de tournage** pour **{_fr(oeuvres)} œuvres**, répartis sur **{nb_deps} départements**. "
        f"L'environnement de **{_fr(len(mesures))} d'entre eux ({_pct(part_m)})** a été mesuré : ce sont eux qui sont notés.",
        chiffres=[
            _chiffre(_fr(n), "lieux de tournage", f"soit {_fr(endroits)} endroits distincts"),
            _chiffre(_fr(oeuvres), "œuvres (films, séries)"),
            _chiffre(_fr(communes), "communes concernées"),
            _chiffre(_pct(part_m), "lieux notés", "ceux dont l'environnement est mesuré"),
        ],
        graphiques=[{"id": "g-entonnoir", "type": "entonnoir", "titre": "De la base brute aux lieux réellement notés",
                     "etiquettes": ["Lieux publiés", "Département identifié", "Environnement mesuré", "Avec trajets routiers réels"],
                     "valeurs": [n, len(localises), len(mesures), len(reels)]}],
        lecture=[
            "Ici, un « lieu » est un lieu de tournage rattaché à une œuvre. Un même endroit filmé pour deux œuvres compte donc deux fois"
            + (f" : cela explique l'écart entre {_fr(n)} lieux et {_fr(endroits)} endroits distincts." if endroits < n else
               " (ici, chaque lieu correspond à un endroit distinct)."),
            "Chaque marche de l'entonnoir retire les lieux pour lesquels on manquerait d'information. On préfère ne pas noter un lieu "
            "plutôt que lui attribuer un zéro qui ne reflèterait qu'une absence de mesure.",
        ],
        attention=[
            f"Les {_fr(n - len(mesures))} lieux non mesurés ne sont pas notés : si leur profil diffère des autres, les résultats ne les décrivent pas." if n > len(mesures) else
            "Tous les lieux publiés ont été mesurés.",
            f"{_fr(len(localises) - len([x for x in localises if not x['dept_deduit']]))} lieux n'avaient pas de département renseigné : il a été déduit de leurs coordonnées."
            if any(x["dept_deduit"] for x in localises) else "",
        ],
    )


def stats_departements(P: dict) -> list[dict]:
    """Une ligne par département de la région (y compris ceux sans lieu)."""
    L = P["lieux"]
    g = _groupes_departements(L)
    total = sum(len(v) for v in g.values()) or 1
    out = []
    for d in P["departements"]:
        v = g.get(d["nom"], [])
        notes = _notes([x for x in v if x["mesure"]])
        out.append({
            "nom": d["nom"], "code": d["code"], "aire": d["aire_km2"], "n": len(v), "part": len(v) / total,
            "oeuvres": len({x["film_id"] for x in v}), "communes": len({x["commune_cle"] for x in v if x["commune_cle"]}),
            "densite": 1000.0 * len(v) / d["aire_km2"] if d["aire_km2"] else None,
            "n_notes": len(notes),
        })
    return out


def sec_repartition(P: dict, SD: list[dict]) -> dict:
    avec = [d for d in SD if d["n"] > 0]
    if not avec:
        return _sec("repartition", "Où se concentrent les lieux ?", "", "Aucun lieu n'a pu être rattaché à un département.")
    tri = sorted(SD, key=lambda d: -d["n"])
    chef = tri[0]
    g = S.gini([d["n"] for d in SD])
    top3 = S.part_des_premiers([d["n"] for d in SD], 3)
    h = S.hhi([d["n"] for d in SD])
    par_dens = sorted([d for d in SD if d["densite"] is not None], key=lambda d: -d["densite"])
    chef_dens = par_dens[0] if par_dens else None
    rang_dens_chef = next((i for i, d in enumerate(par_dens, 1) if d["nom"] == chef["nom"]), None)
    lecture = [
        f"Les trois premiers départements ({', '.join(d['nom'] for d in tri[:3])}) regroupent **{_pct(top3)}** des lieux. "
        f"Le coefficient de Gini ({_fr(g, 2)}) place cette répartition comme « {'plutôt équilibrée' if g < 0.30 else 'inégale' if g < 0.45 else 'très inégale' if g < 0.60 else 'extrêmement concentrée'} » : "
        "0 signifie que chaque département en compte autant, 1 que tout se trouve dans un seul.",
        f"En nombre brut, **{_le(chef['nom'], True)}** arrive en tête ({_fr(chef['n'])} lieux, {_pct(chef['part'])} du total). "
        + (f"Rapporté à la superficie, il se classe {rang_dens_chef}{'er' if rang_dens_chef == 1 else 'e'} sur {len(par_dens)} "
           f"({_fr(chef['densite'], 1)} lieux pour 1 000 km²)" + (f" ; la plus forte densité est {_dans(chef_dens['nom'])} ({_fr(chef_dens['densite'], 1)})." if chef_dens and chef_dens['nom'] != chef['nom'] else ".")
           if rang_dens_chef else ""),
        f"**{_le(chef['nom'], True)} fera l'objet d'une étude approfondie** (partie F), parce qu'il concentre le plus grand nombre de lieux à analyser.",
    ]
    vides = [d["nom"] for d in SD if d["n"] == 0]
    return _sec(
        "repartition", "Où se concentrent les lieux ?",
        "Les lieux de tournage sont-ils répartis équitablement entre les départements ?",
        f"**{_le(chef['nom'], True)}** compte le plus de lieux de tournage (**{_fr(chef['n'])}**, soit {_pct(chef['part'])}). "
        f"Les trois premiers départements en rassemblent {_pct(top3)}.",
        chiffres=[
            _chiffre(chef["nom"], "département n° 1", f"{_fr(chef['n'])} lieux"),
            _chiffre(_pct(top3), "part des 3 premiers", "sur l'ensemble des lieux"),
            _chiffre(_fr(g, 2), "indice de Gini", "0 = équitable, 1 = tout au même endroit"),
            _chiffre(_fr(h, 0) if h is not None else "n.d.", "indice de Herfindahl", "au-delà de 2 500 : très concentré"),
        ],
        graphiques=[
            {"id": "g-nb", "type": "barres_h", "titre": "Nombre de lieux de tournage par département",
             "etiquettes": [d["nom"] for d in tri], "valeurs": [d["n"] for d in tri], "unite": "lieux"},
            {"id": "g-dens", "type": "barres_h", "titre": "Lieux pour 1 000 km² (densité)",
             "etiquettes": [d["nom"] for d in par_dens], "valeurs": [round(d["densite"], 2) for d in par_dens], "unite": "lieux / 1 000 km²"},
        ],
        tableaux=[{"titre": "Les départements en chiffres", "colonnes": ["Département", "Lieux", "Part", "Œuvres", "Communes", "Densité (/1 000 km²)"],
                   "lignes": [[d["nom"], _fr(d["n"]), _pct(d["part"], 1), _fr(d["oeuvres"]), _fr(d["communes"]), _fr(d["densite"], 1)] for d in tri]}],
        lecture=lecture,
        attention=[f"Aucun lieu dans : {', '.join(vides)}." if vides else "",
                   "Le nombre de lieux dépend des œuvres recensées dans la base, pas seulement de l'attractivité réelle des territoires."],
        detail=[_terme("Coefficient de Gini", _fr(g, 2), "Mesure l'inégalité d'une répartition entre 0 (parfaitement égale) et 1 (tout concentré)."),
                _terme("Indice de Herfindahl-Hirschman", _fr(h, 0) if h is not None else "n.d.",
                       "Somme des carrés des parts de chaque département (en %). Sur 13 départements, une répartition parfaitement égale donne environ 770.")],
    )


# ───────────────────────────── PARTIE B : choisir ─────────────────────────────

def _diagnostic(spec: dict, P: dict, subs_ref: dict) -> dict:
    """Passe un indicateur (retenu ou candidat) au crible des cinq critères."""
    M = [x for x in P["lieux"] if x["mesure"]]
    n = len(M)
    if n == 0:
        return {"couverture": None, "fiabilite": None, "distinction": None, "redondance": None}
    ratio = P["ratio"]
    if spec["type"] == "temps":
        vals, est, notes = [], 0, []
        for x in M:
            b = _brut_temps(spec["alts"], x["mes"], ratio)
            ok = [a for a in b if a["t"] is not None]
            notes.append(max(_note_temps(a["t"], c, l) for a, (_, _, c, l) in zip(b, spec["alts"])))
            if ok:
                vals.append(ok)
                est += 1 if all(a["est"] for a in ok) else 0
        couverture = len(vals) / n
        fiabilite = 1 - est / len(vals) if vals else None
        classes = Counter(0 if s >= 100 else 1 if s >= 50 else 2 if s > 0 else 3 for s in notes)
    else:
        notes = [x["sub"][spec["cle"]] for x in M]
        couverture, fiabilite = 1.0, None
        classes = Counter(x["cl"][spec["cle"]] for x in M)
    distinction = max(classes.values()) / n if classes else None
    rho_max, avec = 0.0, None
    for cle, ref in subs_ref.items():
        if cle == spec["cle"] or len(ref) != len(notes):
            continue
        r = S.spearman(notes, ref)
        if r and abs(r["rho"]) > abs(rho_max):
            rho_max, avec = r["rho"], cle
    return {"couverture": couverture, "fiabilite": fiabilite, "distinction": distinction, "redondance": abs(rho_max), "rho": rho_max, "avec": avec}


def _feu(valeur, bon, moyen, sens: str = "haut"):
    if valeur is None:
        return "na"
    if sens == "haut":
        return "ok" if valeur >= bon else "reserve" if valeur >= moyen else "ko"
    return "ok" if valeur <= bon else "reserve" if valeur <= moyen else "ko"


def sec_demarche(P: dict) -> dict:
    M = [x for x in P["lieux"] if x["mesure"]]
    if not M:
        return _sec("demarche", "Comment choisir les indicateurs ?", "", "Aucune mesure disponible.")
    subs_ref = {i["cle"]: [x["sub"][i["cle"]] for x in M] for i in INDICATEURS}
    lignes, couleurs, retenus_diag = [], [], {}
    LIB = {"ok": "✔", "reserve": "≈", "ko": "✘", "na": "—"}

    def ligne(spec, retenu: bool, pert: str, raison: str = ""):
        d = _diagnostic(spec, P, subs_ref)
        f = [_feu(d["couverture"], 0.70, 0.40), _feu(d["fiabilite"], 0.60, 0.30), _feu(d["distinction"], 0.75, 0.90, "bas"),
             _feu(d["redondance"], 0.70, 0.85, "bas")]
        pire = ("couverture", "fiabilite", "distinction", "redondance")[max(range(4), key=lambda i: {"na": -1, "ok": 0, "reserve": 1, "ko": 2}[f[i]])]
        mauvais = [k for k, v in zip(("couverture", "fiabilite", "distinction", "redondance"), f) if v in ("reserve", "ko")]
        if not retenu:
            verdict, commentaire = "Écarté", raison
        elif mauvais:
            noms = {"couverture": "données manquantes pour une part des lieux", "fiabilite": "beaucoup de durées estimées",
                    "distinction": "distingue peu les lieux entre eux", "redondance": "proche d'un autre indicateur"}
            verdict, commentaire = "Retenu avec réserve", " ; ".join(noms[k] for k in mauvais).capitalize() + "."
        else:
            verdict, commentaire = "Retenu", "Les quatre contrôles chiffrés sont satisfaisants."
        retenus_diag[spec["cle"]] = d
        lignes.append([spec["nom"], pert,
                       f"{LIB[f[0]]} {_pct(d['couverture'])}", f"{LIB[f[1]]} {_pct(d['fiabilite'])}",
                       f"{LIB[f[2]]} {_pct(d['distinction'])}",
                       f"{LIB[f[3]]} {_fr(d['redondance'], 2)}" + (f" (avec {IND_PAR_CLE[d['avec']]['nom'].lower()})" if d.get("avec") and d['avec'] in IND_PAR_CLE else ""),
                       verdict + (" : " + commentaire if commentaire else "")])
        couleurs.append({"Retenu": "ok", "Retenu avec réserve": "reserve", "Écarté": "ko"}[verdict])

    for i in INDICATEURS:
        ligne(i, True, "Forte")
    for c in CANDIDATS_ECARTES:
        ligne(c, False, "Faible", c["raison"])
    lignes.append([NOTORIETE["nom"], "Faible (pour la capacité)", "—", "—", "—", "—", "Écarté : " + NOTORIETE["raison"]])
    couleurs.append("ko")

    reserves = [l[0] for l, c in zip(lignes, couleurs) if c == "reserve"]
    rho_tab = _tableau_correlation(M)
    return _sec(
        "demarche", "Comment choisir les indicateurs ?",
        "Que doit pouvoir faire un visiteur autour d'un lieu de tournage, et comment le vérifier avec des chiffres ?",
        "La capacité d'accueil d'un lieu se juge sur **trois questions simples** : peut-on y venir et s'y garer, peut-on y séjourner, "
        "et sera-t-on renseigné et secouru en cas de besoin ? **Huit indicateurs** répondent à ces questions ; trois autres candidats ont été examinés puis écartés.",
        chiffres=[_chiffre("3", "questions du visiteur", "venir · séjourner · être pris en charge"),
                  _chiffre("8", "indicateurs retenus"), _chiffre(str(len(CANDIDATS_ECARTES) + 1), "candidats écartés"),
                  _chiffre("4", "contrôles chiffrés", "données, fiabilité, distinction, non-redondance")],
        tableaux=[
            {"titre": "Les cinq critères appliqués à chaque indicateur candidat (le premier est un jugement, les quatre autres sont mesurés)",
             "colonnes": ["Critère", "Question posée", "Seuil « satisfaisant »"],
             "lignes": [
                 ["1. Pertinence", "L'indicateur répond-il à un besoin réel d'un visiteur ?", "Jugement argumenté (voir la colonne « Pertinence » ci-dessous)"],
                 ["2. Données exploitables", "Pour quelle part des lieux peut-on calculer l'indicateur ?", "70 % ou plus"],
                 ["3. Fiabilité", "Quelle part des durées vient d'un vrai itinéraire routier (et non d'une estimation en ligne droite) ?", "60 % ou plus"],
                 ["4. Pouvoir de distinction", "Les lieux se répartissent-ils entre plusieurs niveaux ? Un indicateur où 90 % des lieux sont au même niveau n'apprend rien.", "Le niveau le plus fréquent regroupe moins de 75 % des lieux"],
                 ["5. Non-redondance", "L'indicateur apporte-t-il une information différente des autres ? (corrélation de rang de Spearman)", "Corrélation inférieure à 0,70 avec chacun des autres"],
             ]},
            {"titre": "Résultat du passage en revue (✔ satisfaisant · ≈ limite · ✘ insuffisant)",
             "colonnes": ["Indicateur candidat", "Pertinence", "Données exploitables", "Fiabilité", "Distinction (niveau le plus fréquent)", "Redondance maximale", "Décision"],
             "lignes": lignes, "feux": couleurs},
            rho_tab,
        ],
        lecture=[
            "Un bon indicateur est **utile** (il répond à un besoin), **mesurable** (on dispose de la donnée), **fiable** (la donnée est exacte), "
            "**discriminant** (il fait apparaître des différences) et **indépendant** (il n'en recopie pas un autre). Le tableau applique ces contrôles de la même façon à tous les candidats.",
            "Les trois indicateurs écartés (police, distributeurs, aéroport) ainsi que la notoriété des œuvres ne sont pas mauvais en soi : "
            "ils décrivent autre chose que l'accueil du lieu (sécurité, paiement, accès à la région, demande potentielle).",
            (f"Pour {_liste(reserves)}, un contrôle est à la limite : l'indicateur est conservé parce qu'il répond à un besoin essentiel, mais ses résultats sont à lire avec prudence (voir la section dédiée)."
             if reserves else "Aucun indicateur retenu ne présente de faiblesse chiffrée sur les quatre contrôles."),
        ],
        attention=["Les seuils (70 %, 60 %, 75 %, 0,70) sont des conventions de bon sens, pas des lois : ils servent à rendre la sélection transparente et contestable."],
    )


def _tableau_correlation(M: list[dict]) -> dict:
    cles = [i["cle"] for i in INDICATEURS]
    mat, txt = [], []
    for a in cles:
        lig, ltxt = [], []
        for b in cles:
            if a == b:
                lig.append(None)
                ltxt.append("—")
                continue
            r = S.spearman([x["sub"][a] for x in M], [x["sub"][b] for x in M])
            lig.append(None if not r else round(100 * (1 - abs(r["rho"])), 1))   # 100 = totalement indépendants (vert)
            ltxt.append("n.d." if not r else _fr(r["rho"], 2))
        mat.append(lig)
        txt.append(ltxt)
    return {"titre": "Les indicateurs se recoupent-ils ? (corrélation de rang : 0 = indépendants, ±1 = identiques)",
            "colonnes": [""] + [IND_PAR_CLE[c]["nom"] for c in cles],
            "lignes": [[IND_PAR_CLE[c]["nom"]] + t for c, t in zip(cles, txt)], "chaleur": mat, "chaleur_colonnes_depuis": 1}


# ───────────────────────────── PARTIE C : un indicateur par section ─────────────────────────────

def sec_indicateur(P: dict, ind: dict, numero: int) -> dict:
    M = [x for x in P["lieux"] if x["mesure"]]
    cle = ind["cle"]
    n = len(M)
    titre = f"Indicateur {numero} sur 8 : {ind['nom']} — {ind['titre'][0].lower() + ind['titre'][1:]}"
    if not n:
        return _sec(f"ind-{cle}", titre, ind["besoin"], "Pas de mesure disponible.")
    sub = [x["sub"][cle] for x in M]
    ok = sum(1 for s in sub if s >= 50)
    p_ok = ok / n
    ic = S.intervalle_wilson(ok, n)
    classes = Counter(x["cl"][cle] for x in M)
    libs = LIBELLES_CLASSE_TEMPS if ind["type"] == "temps" else LIBELLES_CLASSE_CHOIX if ind["type"] == "choix" else LIBELLES_CLASSE_VARIETE
    vals = [x["val"][cle] for x in M if x["val"][cle] is not None]
    mediane = S.mediane(vals) if vals else None
    est = sum(1 for x in M if x["est"][cle])
    sans = sum(1 for x in M if (x["val"][cle] is None) or (ind["type"] != "temps" and x["val"][cle] == 0))
    g = _groupes_departements(M)
    comp = {d: v for d, v in g.items() if len(v) >= SEUIL_COMPARABLE}
    lignes_d = []
    for d, v in comp.items():
        k = sum(1 for x in v if x["sub"][cle] >= 50)
        w = S.intervalle_wilson(k, len(v))
        vv = [x["val"][cle] for x in v if x["val"][cle] is not None]
        lignes_d.append({"nom": d, "n": len(v), "p": k / len(v), "ic": w, "med": S.mediane(vv) if vv else None,
                         "sub": [x["sub"][cle] for x in v]})
    lignes_d.sort(key=lambda r: -r["p"])
    kw = S.kruskal_wallis([r["sub"] for r in lignes_d]) if len(lignes_d) >= 2 else None

    def fmt(v):
        return _min(v) if ind["type"] == "temps" else ("n.d." if v is None else _fr(v))

    unite_med = "" if ind["type"] == "temps" else (" équipements" if ind["type"] == "choix" else " types")
    seuils = ""
    if ind["type"] == "temps":
        c0, l0 = ind["alts"][0][2], ind["alts"][0][3]
        seuils = " ; ".join(f"{LIBELLE_CATEGORIE[a[0]]} ({'à pied' if a[1] == 'pied' else 'en voiture'}) : confortable ≤ {a[2]} min, hors de portée ≥ {a[3]} min" for a in ind["alts"])
    # lecture
    lec = []
    if ind["type"] == "temps":
        hors = classes.get(3, 0) / n
        conf = classes.get(0, 0) / n
        lec.append(f"Dans **{_pct(conf)}** des cas le visiteur trouve ce qu'il cherche dans la zone « confortable » ; dans **{_pct(hors)}** des cas il est « hors de portée » (trop loin, ou rien dans la zone de recherche).")
        if cle != "transport" and mediane is not None:
            lec.append(f"Le temps médian jusqu'à l'équipement le plus proche est de **{_min(mediane)}** : la moitié des lieux sont plus près, la moitié plus loin.")
    elif ind["type"] == "choix":
        lec.append(f"La moitié des lieux comptent au plus **{_fr(mediane)} équipements** à moins de 1 km ; **{_pct(classes.get(3, 0) / n)}** n'en ont aucun.")
    else:
        lec.append(f"La moitié des lieux disposent d'au plus **{_fr(mediane)} types d'équipements sur 6** à moins de 1 km ; **{_pct(classes.get(3, 0) / n)}** n'en ont qu'un ou aucun.")
    if len(lignes_d) >= 2:
        hi, lo = lignes_d[0], lignes_d[-1]
        lec.append(f"Entre départements, l'écart va de **{_pct(lo['p'])}** de lieux correctement servis {_dans(lo['nom'])} à **{_pct(hi['p'])}** {_dans(hi['nom'])}. "
                   f"Verdict du test : {_lecture_p(kw['p']) if kw else 'comparaison impossible'}"
                   + (f" (importance de l'écart : {_lecture_effet(kw['epsilon2'])})." if kw else "."))
    else:
        lec.append("Aucun département ne compte assez de lieux mesurés pour être comparé de façon fiable.")
    attention = []
    if est and ind["type"] == "temps":
        attention.append(f"Pour {_fr(est)} lieux ({_pct(est / n)}), le trajet routier n'est pas encore calculé : la durée est estimée depuis la ligne droite.")
    if cle == "transport":
        attention.append("Cet indicateur combine deux options (bus à pied, gare en voiture) : on ne peut pas en tirer une durée unique, d'où l'absence de médiane.")
    if cle == "soigner":
        attention.append("Il s'agit de la distance à l'hôpital, pas de la qualité des soins ni des services d'urgence disponibles.")
    if cle in ("choix", "diversite"):
        attention.append("Les équipements sont comptés à vol d'oiseau dans un rayon de 1 km ; ce n'est pas un temps de trajet.")
    # pires lieux
    tri_pire = sorted(M, key=lambda x: (x["sub"][cle], -(x["val"][cle] if (ind["type"] == "temps" and x["val"][cle] is not None) else 1e9 if ind["type"] == "temps" else 0)))[:6]
    tabs = []
    if lignes_d:
        tabs.append({"titre": "Part des lieux correctement servis, par département",
                     "colonnes": ["Département", "Lieux mesurés", "Correctement servis", "Fourchette plausible (95 %)", "Valeur médiane"],
                     "lignes": [[r["nom"], _fr(r["n"]), _pct(r["p"]), _fourchette_pct(r["ic"]), fmt(r["med"]) + (unite_med if r["med"] is not None and ind["type"] != "temps" else "") if cle != "transport" else "n.d."] for r in lignes_d]})
    tabs.append(_tableau_lieux(f"Les lieux les moins bien servis pour « {ind['nom'].lower()} »", tri_pire, "Valeur",
                               lambda x: (fmt(x["val"][cle]) + (unite_med if ind["type"] != "temps" else "")) if x["val"][cle] is not None else "aucun trouvé"))
    graphs = [{"id": f"g-{cle}-cl", "type": "classes", "titre": "Combien de lieux dans chaque niveau ?", "etiquettes": libs,
               "valeurs": [classes.get(i, 0) for i in range(4)], "couleurs": COULEURS_CLASSE_IND}]
    if lignes_d:
        graphs.append({"id": f"g-{cle}-dep", "type": "barres_h", "titre": "Part des lieux correctement servis (avec fourchette plausible)",
                       "etiquettes": [r["nom"] for r in lignes_d], "valeurs": [round(100 * r["p"], 1) for r in lignes_d],
                       "bas": [round(100 * r["ic"][0], 1) if r["ic"] else None for r in lignes_d],
                       "haut": [round(100 * r["ic"][1], 1) if r["ic"] else None for r in lignes_d], "unite": "%", "max": 100})
    return _sec(
        f"ind-{cle}", titre, ind["besoin"],
        f"**{_pct(p_ok)}** des lieux sont correctement servis pour « {ind['nom'].lower()} » (fourchette plausible : {_fourchette_pct(ic)}). "
        + (f"Le plus proche est à {_min(mediane)} en médiane." if ind["type"] == "temps" and cle != "transport" and mediane is not None else "").strip(),
        chiffres=[_chiffre(_pct(p_ok), "correctement servis", "niveau confortable ou acceptable"),
                  _chiffre(fmt(mediane) + (unite_med if ind["type"] != "temps" and mediane is not None else "") if cle != "transport" else "—", "valeur médiane"),
                  _chiffre(_pct(classes.get(3, 0) / n), "hors de portée" if ind["type"] == "temps" else "niveau insuffisant"),
                  _chiffre(f"{ind['poids']} %", "poids dans la note", "voir la partie D")],
        graphiques=graphs, tableaux=tabs, lecture=lec, attention=attention,
        detail=[_terme("Ce qui est mesuré", "", ind["mesure"]),
                _terme("Seuils retenus", "", (seuils + ". " if seuils else "") + ind["justif"]),
                _terme("Correctement servi", "", "Note de l'indicateur d'au moins 50 sur 100, c'est-à-dire niveau « confortable » ou « acceptable »."),
                _terme("Test d'écart entre départements (Kruskal-Wallis)", (f"p {_fp(kw['p'])}" if kw else "n.d."),
                       "Compare la répartition des notes entre départements d'au moins "
                       f"{SEUIL_COMPARABLE} lieux. Plus p est petit, moins l'écart observé est attribuable au hasard."),
                _terme("Durées estimées", _pct(est / n) if ind["type"] == "temps" else "sans objet", "Part des lieux dont le trajet routier n'est pas calculé.")],
    )


# ───────────────────────────── PARTIE D : noter ─────────────────────────────

def _scenarios(M: list[dict]) -> list[dict]:
    """Recalcule la note avec d'autres choix raisonnables et mesure ce qui change."""
    base = {x["id"]: x["note"] for x in M}
    cl_base = {x["id"]: x["classe"] for x in M}
    egal = {k: 1.0 for k in POIDS_BASE}
    sejour = dict(POIDS_BASE, **{k: POIDS_BASE[k] * 1.5 for k in ("dormir", "manger", "garer")})
    journee = dict(POIDS_BASE, dormir=POIDS_BASE["dormir"] * 0.5, garer=POIDS_BASE["garer"] * 1.5, informer=POIDS_BASE["informer"] * 1.5)
    defs = [("Poids égaux pour les huit indicateurs", egal, True, 1.0),
            ("Priorité au séjour (dormir, manger, se garer pèsent 50 % de plus)", sejour, True, 1.0),
            ("Priorité à la visite d'une journée (dormir pèse moitié moins)", journee, True, 1.0),
            ("Sans la règle du plafond (les essentiels peuvent être compensés)", POIDS_BASE, False, 1.0),
            ("Seuils de temps 25 % plus stricts", POIDS_BASE, True, 0.75),
            ("Seuils de temps 25 % plus souples", POIDS_BASE, True, 1.25)]
    # f : facteur sur les seuils (0.75 = plus strict, car les seuils sont plus courts)
    ref_dept = _medianes_departements(M, base)
    out = []
    for nom, poids, plaf, f in defs:
        notes = {}
        for x in M:
            subs = {i["cle"]: (sous_note(i, x["brut"][i["cle"]], f) if f != 1.0 else x["sub"][i["cle"]]) for i in INDICATEURS}
            notes[x["id"]] = note_globale(subs, poids, plaf)[0]
        ids = [x["id"] for x in M]
        r = S.spearman([base[i] for i in ids], [notes[i] for i in ids])
        meme_classe = sum(1 for i in ids if _classe(notes[i])[0] == cl_base[i]) / len(ids)
        alt_dept = _medianes_departements(M, notes)
        communs = [d for d in ref_dept if d in alt_dept]
        rd = S.spearman([ref_dept[d] for d in communs], [alt_dept[d] for d in communs]) if len(communs) >= 4 else None
        dernier_base = min(ref_dept, key=ref_dept.get) if ref_dept else None
        dernier_alt = min(alt_dept, key=alt_dept.get) if alt_dept else None
        premier_base = max(ref_dept, key=ref_dept.get) if ref_dept else None
        premier_alt = max(alt_dept, key=alt_dept.get) if alt_dept else None
        out.append({"nom": nom, "rho": r["rho"] if r else None, "meme_classe": meme_classe,
                    "rho_dept": rd["rho"] if rd else None, "meme_dernier": dernier_base == dernier_alt, "meme_premier": premier_base == premier_alt})
    return out


def _medianes_departements(M: list[dict], notes: dict) -> dict:
    g = defaultdict(list)
    for x in M:
        if x["dept_nom"]:
            g[x["dept_nom"]].append(notes[x["id"]])
    return {d: S.mediane(v) for d, v in g.items() if len(v) >= SEUIL_COMPARABLE}


def sec_scoring(P: dict) -> dict:
    M = [x for x in P["lieux"] if x["mesure"]]
    if not M:
        return _sec("scoring", "Du chiffre à la note", "", "Pas de mesure disponible.")
    sc = _scenarios(M)
    stable_notes = all((s["rho"] or 0) >= 0.9 for s in sc)
    stable_dept = all((s["rho_dept"] is None or s["rho_dept"] >= 0.9) and s["meme_premier"] and s["meme_dernier"] for s in sc)
    poids_axes = {a: sum(i["poids"] for i in INDICATEURS if i["axe"] == a) for a in AXES}
    rhos = [s["rho"] for s in sc if s["rho"] is not None]
    classes_stables = [s["meme_classe"] for s in sc]
    lignes_ind = [[i["nom"], AXES[i["axe"]], f"{i['poids']} %",
                   (" ou ".join(f"{LIBELLE_CATEGORIE[a[0]]} : ≤ {a[2]} min → 100, ≥ {a[3]} min → 0" for a in i["alts"]) if i["type"] == "temps"
                    else "0 équipement → 0 ; 3 → 50 ; 15 et plus → 100" if i["type"] == "choix" else "chaque type présent apporte 1/6 de la note"),
                   "Oui" if i["essentiel"] else "Non"] for i in INDICATEURS]
    return _sec(
        "scoring", "Du chiffre à la note : comment est calculée la capacité d'accueil ?",
        "Comment transformer huit mesures hétérogènes (minutes, nombres d'équipements) en une note unique et comparable ?",
        "Chaque indicateur est converti en une **note de 0 à 100**, puis les huit notes sont combinées selon des **poids** qui reflètent l'importance de chaque besoin. "
        "Une règle de **plafond** empêche un lieu sans hébergement, sans restaurant ou sans parking proche d'être jugé « bon ».",
        chiffres=[_chiffre("8", "indicateurs notés sur 100"), _chiffre(" / ".join(str(poids_axes[a]) for a in ("sejour", "acces", "prise_en_charge")), "poids des trois axes (%)", "séjourner / venir / être pris en charge"),
                  _chiffre(f"{_fr(min(rhos), 2)}" if rhos else "n.d.", "corrélation minimale entre variantes", "1 = classement inchangé"),
                  _chiffre(_pct(min(classes_stables)) if classes_stables else "n.d.", "lieux qui gardent leur niveau", "dans la variante la plus perturbatrice")],
        graphiques=[{"id": "g-poids", "type": "barres_h", "titre": "Poids de chaque indicateur dans la note finale (%)",
                     "etiquettes": [i["nom"] for i in sorted(INDICATEURS, key=lambda z: -z["poids"])],
                     "valeurs": [i["poids"] for i in sorted(INDICATEURS, key=lambda z: -z["poids"])], "unite": "%"}],
        tableaux=[
            {"titre": "Règles de conversion et de pondération", "colonnes": ["Indicateur", "Axe", "Poids", "Conversion en note", "Essentiel ?"], "lignes": lignes_ind},
            {"titre": "Niveaux d'accueil", "colonnes": ["Note", "Niveau"],
             "lignes": [[f"{_fr(c[2])} à {_fr(CLASSES[i - 1][2] - 0.1, 1) if i else '100'}" if i else "75 à 100", c[1]] for i, c in enumerate(CLASSES)]},
            {"titre": "La note résiste-t-elle à d'autres choix raisonnables ?",
             "colonnes": ["Variante testée", "Notes comparables aux nôtres (corrélation)", "Lieux restant au même niveau", "Classement des départements (corrélation)", "Même département en tête ?", "Même département en queue ?"],
             "lignes": [[s["nom"], _fr(s["rho"], 2), _pct(s["meme_classe"]), _fr(s["rho_dept"], 2), "oui" if s["meme_premier"] else "non", "oui" if s["meme_dernier"] else "non"] for s in sc]},
        ],
        lecture=[
            "**Pourquoi des poids ?** Les besoins n'ont pas la même importance : on peut renoncer à un office de tourisme, pas à un repas ni à un endroit où dormir. "
            f"Le séjour pèse {poids_axes['sejour']} %, parce que la capacité d'accueil se juge d'abord à la possibilité de rester sur place ; l'accès {poids_axes['acces']} % ; l'information et les secours {poids_axes['prise_en_charge']} %.",
            "**Pourquoi un plafond ?** Une moyenne pondérée permet à de bons résultats de masquer un manque essentiel. Un lieu sans hébergement, sans restaurant ou sans parking à distance raisonnable "
            f"est donc limité à {_fr(PLAFOND_ESSENTIELS)} / 100 (« accueil moyen » au mieux), même s'il excelle sur le reste.",
            ("**Solidité :** quelle que soit la variante testée, les notes restent très proches des nôtres, le classement des départements est stable et les mêmes départements arrivent en tête et en queue : les conclusions ne dépendent pas d'un choix de poids ou de seuil particulier."
             if stable_notes and stable_dept else
             "**Solidité :** les notes individuelles restent proches des nôtres, mais le classement des départements bouge selon les choix de poids ou de seuil. Retenez les écarts importants entre départements, pas les petits écarts de rang."
             if stable_notes else
             "**Solidité :** certaines variantes modifient sensiblement les notes. Les résultats sont des ordres de grandeur : les écarts fins, entre lieux comme entre départements, ne sont pas fiables."),
        ],
        attention=["Les poids et les seuils sont des choix d'experts, argumentés mais discutables : c'est pourquoi leur influence est testée ci-dessus.",
                   "La note mesure la proximité des équipements, pas leur qualité, leur capacité ni leur ouverture réelle."],
        detail=[_terme("Corrélation de rang (Spearman)", "", "Mesure si deux classements vont dans le même sens : 1 = identiques, 0 = sans lien."),
                _terme("Plafond des essentiels", _fr(PLAFOND_ESSENTIELS), "Note maximale d'un lieu pour lequel l'un des trois essentiels (dormir, manger, se garer) obtient 0.")],
    )


def sec_resultats(P: dict) -> dict:
    M = [x for x in P["lieux"] if x["mesure"]]
    if not M:
        return _sec("resultats", "La capacité d'accueil de chaque lieu", "", "Aucun lieu noté.")
    notes = _notes(M)
    med = S.mediane(notes)
    ic = S.intervalle_bootstrap(notes)
    comptes = Counter(x["classe"] for x in M)
    n = len(M)
    plaf = sum(1 for x in M if x["plafonne"])
    bons = comptes.get("tres_bon", 0) + comptes.get("bon", 0)
    faibles = comptes.get("limite", 0) + comptes.get("tres_limite", 0)
    med_ind = {i["cle"]: S.mediane([x["sub"][i["cle"]] for x in M]) for i in INDICATEURS}
    faible = min(med_ind, key=med_ind.get)
    fort = max(med_ind, key=med_ind.get)
    bins = [0] * 10
    for s in notes:
        bins[min(9, int(s // 10))] += 1
    top = sorted(M, key=lambda x: -x["note"])[:10]
    bas = sorted(M, key=lambda x: x["note"])[:10]
    export = {"titre": "Tous les lieux notés", "masque": True,
              "colonnes": ["Lieu", "Œuvre", "Commune", "Département", "Note", "Niveau"] + [i["nom"] for i in INDICATEURS],
              "lignes": [[x["nom"], x["titre"], x.get("commune") or "", x["dept_nom"] or "", _fr(x["note"], 1), _classe(x["note"])[1]] + [_fr(x["sub"][i["cle"]], 0) for i in INDICATEURS]
                         for x in sorted(M, key=lambda z: -z["note"])]}
    return _sec(
        "resultats", "La capacité d'accueil de chaque lieu",
        "Au final, quels lieux de tournage peuvent bien accueillir des visiteurs, et lesquels non ?",
        f"La note médiane est de **{_fr(med, 0)} sur 100** (fourchette plausible : {_fourchette(ic)}). "
        f"**{_pct(bons / n)}** des lieux offrent un accueil bon ou très bon ; **{_pct(faibles / n)}** un accueil limité ou très limité. "
        f"Le point faible général est « {IND_PAR_CLE[faible]['nom'].lower()} », le point fort « {IND_PAR_CLE[fort]['nom'].lower()} ».",
        chiffres=[_chiffre(_fr(med, 0), "note médiane /100"), _chiffre(_pct(bons / n), "accueil bon ou très bon"),
                  _chiffre(_pct(faibles / n), "accueil limité ou très limité"), _chiffre(_fr(plaf), "lieux plafonnés", "manque d'un essentiel")],
        graphiques=[
            {"id": "g-distrib", "type": "barres", "titre": "Nombre de lieux par tranche de note", "etiquettes": [f"{10 * i}–{10 * i + 10}" for i in range(10)], "valeurs": bins,
             "couleurs": [_classe(10 * i + 5)[3] for i in range(10)]},
            {"id": "g-niveaux", "type": "classes", "titre": "Répartition par niveau d'accueil", "etiquettes": [c[1] for c in CLASSES],
             "valeurs": [comptes.get(c[0], 0) for c in CLASSES], "couleurs": [c[3] for c in CLASSES]},
            {"id": "g-points", "type": "barres_h", "titre": "Note médiane de chaque indicateur (sur 100)",
             "etiquettes": [IND_PAR_CLE[k]["nom"] for k in sorted(med_ind, key=med_ind.get)], "valeurs": [round(med_ind[k], 1) for k in sorted(med_ind, key=med_ind.get)], "unite": "/100", "max": 100},
        ],
        tableaux=[_tableau_lieux("Les dix lieux les mieux équipés", top), _tableau_lieux("Les dix lieux les moins bien équipés", bas), export],
        lecture=[
            f"Un lieu sur deux obtient moins de {_fr(med, 0)}/100. La note n'est pas un jugement de valeur : elle dit si un visiteur peut, sans difficulté, **y venir, y rester et être pris en charge**.",
            f"**{_fr(plaf)} lieux** ont vu leur note plafonnée à {_fr(PLAFOND_ESSENTIELS)} : ils manquent d'un hébergement, d'un restaurant ou d'un parking à distance raisonnable. "
            "Ce sont les lieux où le déficit est le plus concret pour un visiteur.",
            f"Le point le plus fragile de l'ensemble est « {IND_PAR_CLE[faible]['nom'].lower()} » (note médiane {_fr(med_ind[faible], 0)}/100), "
            f"le plus solide « {IND_PAR_CLE[fort]['nom'].lower()} » ({_fr(med_ind[fort], 0)}/100).",
        ],
        attention=["La liste complète des lieux avec leurs notes détaillées est téléchargeable (bouton CSV)."],
    )


# ───────────────────────────── PARTIE E : comparer ─────────────────────────────

def stats_notes_departements(P: dict) -> list[dict]:
    M = [x for x in P["lieux"] if x["mesure"]]
    g = _groupes_departements(M)
    out = []
    for d, v in g.items():
        notes = _notes(v)
        n = len(v)
        bons = sum(1 for x in v if x["note"] >= 60)
        faibles = sum(1 for x in v if x["note"] < 45)
        med_ind = {i["cle"]: S.mediane([x["sub"][i["cle"]] for x in v]) for i in INDICATEURS}
        out.append({"nom": d, "n": n, "notes": notes, "med": S.mediane(notes), "ic": S.intervalle_bootstrap(notes) if n >= SEUIL_COMPARABLE else None,
                    "bons": bons / n, "ic_bons": S.intervalle_wilson(bons, n), "faibles": faibles / n,
                    "ind": med_ind, "fort": max(med_ind, key=med_ind.get), "faible": min(med_ind, key=med_ind.get),
                    "classes": Counter(x["classe"] for x in v), "comparable": n >= SEUIL_COMPARABLE, "lieux": v})
    out.sort(key=lambda r: -r["med"])
    return out


def sec_comparaison(P: dict, SN: list[dict], SD: list[dict]) -> dict:
    comp = [r for r in SN if r["comparable"]]
    if len(comp) < 2:
        return _sec("comparaison", "Comparer les départements", "", f"Moins de deux départements comptent au moins {SEUIL_COMPARABLE} lieux notés : la comparaison ne serait pas fiable.")
    kw = S.kruskal_wallis([r["notes"] for r in comp])
    meilleur, pire = comp[0], comp[-1]
    nb_lieux = {d["nom"]: d["n"] for d in SD}
    sp = S.spearman([nb_lieux.get(r["nom"], 0) for r in comp], [r["med"] for r in comp]) if len(comp) >= 4 else None
    chef = max(SD, key=lambda d: d["n"])
    chef_r = next((r for r in comp if r["nom"] == chef["nom"]), None)
    # leader contre chaque autre département
    lignes_mw, lecture_mw = [], []
    if chef_r:
        for r in comp:
            if r["nom"] == chef_r["nom"]:
                continue
            mw = _mann_whitney(chef_r["notes"], r["notes"])
            if mw:
                lignes_mw.append([r["nom"], _fr(r["med"], 0), f"{_fr(100 * mw['prob_sup'])} %", _lecture_p(mw["p"])])
    heat = [[round(r["ind"][i["cle"]], 1) for i in INDICATEURS] for r in comp]
    cl_series = [{"nom": c[1], "couleur": c[3], "valeurs": [round(100 * r["classes"].get(c[0], 0) / r["n"], 1) for r in comp]} for c in CLASSES]
    lec = [
        f"**{_le(meilleur['nom'], True)}** a la meilleure note médiane ({_fr(meilleur['med'], 0)}/100) et **{_le(pire['nom'])}** la plus faible ({_fr(pire['med'], 0)}/100). "
        f"Test d'ensemble : {_lecture_p(kw['p']) if kw else 'impossible'}"
        + (f" ; l'importance de l'écart est « {_lecture_effet(kw['epsilon2'])} »." if kw else "."),
        ("Les départements qui comptent beaucoup de lieux ne sont pas forcément mieux équipés : " +
         (f"la corrélation entre nombre de lieux et note médiane est de {_fr(sp['rho'], 2)} ({'lien net' if abs(sp['rho']) >= 0.5 else 'lien faible' if abs(sp['rho']) >= 0.3 else 'pas de lien'})." if sp else "trop peu de départements pour le mesurer.")),
        "Dans le tableau de chaleur, chaque case est la note médiane (sur 100) d'un indicateur dans un département : vert = bien servi, rouge = mal servi. "
        "Lisez-le en colonne pour repérer le point faible commun, en ligne pour le profil de chaque département.",
    ]
    if chef_r and lignes_mw:
        lec.append(f"Comparaison {_du(chef_r['nom'])} (département n° 1) avec les autres : la colonne « chance de mieux faire » donne la probabilité qu'un lieu tiré au hasard {_dans(chef_r['nom'])} soit mieux noté qu'un lieu tiré au hasard dans l'autre département (50 % = aucune différence).")
    tab_dept = {"titre": "Les départements comparés (au moins " + str(SEUIL_COMPARABLE) + " lieux notés)",
                "colonnes": ["Département", "Lieux notés", "Note médiane", "Fourchette plausible", "Accueil bon ou très bon", "Accueil limité ou très limité", "Point fort", "Point faible"],
                "lignes": [[r["nom"], _fr(r["n"]), _fr(r["med"], 0), _fourchette(r["ic"]), _pct(r["bons"]), _pct(r["faibles"]),
                            IND_PAR_CLE[r["fort"]]["nom"], IND_PAR_CLE[r["faible"]]["nom"]] for r in comp]}
    tabs = [tab_dept,
            {"titre": "Tableau de chaleur : note médiane de chaque indicateur par département (0 = très mal servi, 100 = très bien servi)",
             "colonnes": ["Département"] + [i["nom"] for i in INDICATEURS],
             "lignes": [[r["nom"]] + [_fr(r["ind"][i["cle"]], 0) for i in INDICATEURS] for r in comp], "chaleur": heat, "chaleur_colonnes_depuis": 1}]
    if lignes_mw:
        tabs.append({"titre": f"{chef_r['nom']} face aux autres départements", "colonnes": ["Autre département", "Sa note médiane", "Chance que " + _le(chef_r["nom"]) + " fasse mieux", "Écart réel ou hasard ?"], "lignes": lignes_mw})
    exclus = [r["nom"] for r in SN if not r["comparable"]]
    return _sec(
        "comparaison", "Comparer les départements",
        "Les départements offrent-ils la même capacité d'accueil, et les écarts observés sont-ils significatifs ?",
        f"La note médiane va de **{_fr(pire['med'], 0)}** ({pire['nom']}) à **{_fr(meilleur['med'], 0)}** ({meilleur['nom']}). "
        f"{'Cet écart est statistiquement établi : il est très peu probable qu’il soit dû au hasard.' if kw and kw['p'] < 0.05 else 'Cet écart pourrait n’être dû qu’au hasard.'}",
        chiffres=[_chiffre(meilleur["nom"], "meilleure note médiane", _fr(meilleur["med"], 0) + "/100"), _chiffre(pire["nom"], "plus faible note médiane", _fr(pire["med"], 0) + "/100"),
                  _chiffre(_fp(kw["p"]) if kw else "n.d.", "probabilité que l'écart soit dû au hasard", "p-value du test d'ensemble"),
                  _chiffre(str(len(comp)), "départements comparés")],
        graphiques=[
            {"id": "g-med", "type": "barres_h", "titre": "Note médiane par département (avec fourchette plausible)", "etiquettes": [r["nom"] for r in comp],
             "valeurs": [round(r["med"], 1) for r in comp], "bas": [round(r["ic"][0], 1) if r["ic"] else None for r in comp],
             "haut": [round(r["ic"][1], 1) if r["ic"] else None for r in comp], "unite": "/100", "max": 100},
            {"id": "g-classes", "type": "empile_h", "titre": "Répartition des niveaux d'accueil dans chaque département (%)", "etiquettes": [r["nom"] for r in comp], "series": cl_series},
        ],
        tableaux=tabs, lecture=lec,
        attention=[f"Écartés de la comparaison faute d'assez de lieux notés (moins de {SEUIL_COMPARABLE}) : {_liste(exclus)}." if exclus else "",
                   "Un département peut avoir une note médiane élevée tout en comptant des lieux très mal équipés : voir la part de lieux à accueil limité."],
        detail=[_terme("Test de Kruskal-Wallis", f"H = {_fr(kw['H'], 1)} ; p {_fp(kw['p'])}" if kw else "n.d.",
                       "Compare toutes les distributions de notes d'un seul coup, sans supposer qu'elles suivent une loi particulière."),
                _terme("Taille d'effet (epsilon²)", _fr(kw["epsilon2"], 3) if kw else "n.d.", "Part de la variabilité des notes expliquée par le département : < 0,06 faible ; 0,06–0,14 moyen ; > 0,14 important."),
                _terme("Chance de mieux faire", "", "Probabilité qu'un élément tiré au hasard dans un groupe ait une meilleure note qu'un élément de l'autre groupe (statistique U de Mann-Whitney divisée par n₁×n₂).")],
    )


# ───────────────────────────── PARTIE F : approfondir le département n° 1 ─────────────────────────────

def _chef(SD: list[dict]):
    avec = [d for d in SD if d["n"] > 0]
    return max(avec, key=lambda d: d["n"]) if avec else None


def sec_zoom_portrait(P: dict, SD: list[dict]) -> dict:
    chef = _chef(SD)
    if not chef:
        return _sec("zoom-portrait", "Le département n° 1 : portrait", "", "Aucun département n'a pu être identifié.")
    L = [x for x in P["lieux"] if x["dept_nom"] == chef["nom"]]
    communes = Counter()
    noms = {}
    for x in L:
        if x["commune_cle"]:
            communes[x["commune_cle"]] += 1
            noms.setdefault(x["commune_cle"], x.get("commune"))
    films = Counter(x["titre"] for x in L)
    top_c = communes.most_common(10)
    g = S.gini(list(communes.values())) if communes else None
    top3 = S.part_des_premiers(list(communes.values()), 3) if communes else None
    mesures = [x for x in L if x["mesure"]]
    annees = [x["annee"] for x in L if x.get("annee")]
    return _sec(
        "zoom-portrait", f"{chef['nom']} : portrait du premier département de tournage",
        f"Pourquoi {_le(chef['nom'])} concentre-t-il autant de lieux, et comment sont-ils répartis sur son territoire ?",
        f"**{_le(chef['nom'], True)}** rassemble **{_fr(chef['n'])} lieux** ({_pct(chef['part'])} de la région) répartis sur **{_fr(chef['communes'])} communes** et liés à **{_fr(chef['oeuvres'])} œuvres**. "
        f"{_fr(len(mesures))} lieux sont mesurés et notés. "
        + (f"Les trois premières communes en regroupent {_pct(top3)}." if top3 is not None else ""),
        chiffres=[_chiffre(_fr(chef["n"]), "lieux de tournage"), _chiffre(_fr(chef["communes"]), "communes"), _chiffre(_fr(chef["oeuvres"]), "œuvres"),
                  _chiffre(_fr(chef["densite"], 1), "lieux / 1 000 km²", f"superficie : {_fr(chef['aire'])} km²")],
        graphiques=[{"id": "g-communes", "type": "barres_h", "titre": "Les dix communes qui accueillent le plus de lieux", "etiquettes": [noms[c] or c for c, _ in top_c], "valeurs": [v for _, v in top_c], "unite": "lieux"},
                    {"id": "g-films", "type": "barres_h", "titre": "Les œuvres qui comptent le plus de lieux dans le département",
                     "etiquettes": [t for t, _ in films.most_common(8)], "valeurs": [v for _, v in films.most_common(8)], "unite": "lieux"}],
        lecture=[
            (f"La concentration entre communes est {'forte' if g is not None and g >= 0.45 else 'modérée' if g is not None and g >= 0.30 else 'faible'} (Gini = {_fr(g, 2)}) : "
             + ("quelques communes concentrent l'essentiel de l'activité de tournage ; les équiper en priorité a donc un effet démultiplié."
                if g is not None and g >= 0.45 else "les lieux sont dispersés sur le territoire, ce qui rend l'effort d'équipement plus diffus.")) if g is not None else "",
            (f"Les œuvres tournées dans le département s'étalent de {min(annees)} à {max(annees)}." if annees else ""),
            f"Rapporté à sa superficie, {_le(chef['nom'])} compte {_fr(chef['densite'], 1)} lieux pour 1 000 km² : "
            f"{'au-dessus' if chef['densite'] is not None and chef['densite'] > (sum(d['n'] for d in SD) * 1000.0 / (sum(d['aire'] for d in SD) or 1)) else 'en dessous'} de la moyenne régionale "
            f"({_fr(sum(d['n'] for d in SD) * 1000.0 / (sum(d['aire'] for d in SD) or 1), 1)}).",
        ],
        attention=["Plusieurs lieux peuvent correspondre à un même endroit filmé pour plusieurs œuvres."],
    )


def sec_zoom_equipement(P: dict, SD: list[dict], SN: list[dict]) -> dict:
    chef = _chef(SD)
    M = [x for x in P["lieux"] if x["mesure"]]
    if not chef or not M:
        return _sec("zoom-equipement", "Capacité d'accueil du département n° 1", "", "Pas assez de données.")
    V = [x for x in M if x["dept_nom"] == chef["nom"]]
    R = [x for x in M if x["dept_nom"] != chef["nom"] and x["dept_nom"]]
    if len(V) < 3:
        return _sec("zoom-equipement", f"{chef['nom']} : capacité d'accueil", "", f"Seulement {len(V)} lieu(x) noté(s) dans le département : analyse approfondie impossible.")
    med_v, med_r = S.mediane(_notes(V)), S.mediane(_notes(R)) if R else None
    ic = S.intervalle_bootstrap(_notes(V))
    mw = _mann_whitney(_notes(V), _notes(R)) if R else None
    comptes = Counter(x["classe"] for x in V)
    n = len(V)
    ecarts = []
    for i in INDICATEURS:
        mv = S.mediane([x["sub"][i["cle"]] for x in V])
        mr = S.mediane([x["sub"][i["cle"]] for x in R]) if R else None
        ecarts.append((i, mv, mr, (mv - mr) if mr is not None else None))
    ecarts_tri = sorted([e for e in ecarts if e[3] is not None], key=lambda e: e[3])
    point_faible = min(ecarts, key=lambda e: e[1])
    isoles = sorted([x for x in V if x["plafonne"]], key=lambda x: x["note"])[:10]
    plaf = sum(1 for x in V if x["plafonne"])
    zones_blanches = {i["cle"]: sum(1 for x in V if x["cl"][i["cle"]] == 3) for i in INDICATEURS}
    zb_tri = sorted(zones_blanches.items(), key=lambda kv: -kv[1])
    lec = [
        f"La note médiane {_du(chef['nom'])} est de **{_fr(med_v, 0)}/100** (fourchette plausible : {_fourchette(ic)}), contre {_fr(med_r, 0)}/100 pour le reste de la région."
        if med_r is not None else f"La note médiane est de {_fr(med_v, 0)}/100.",
        (f"Si l'on prend au hasard un lieu {_du(chef['nom'])} et un lieu d'un autre département, le premier est mieux équipé dans **{_fr(100 * mw['prob_sup'])} %** des cas ({_lecture_p(mw['p'])})." if mw else ""),
        f"Le point le plus fragile du département est « **{point_faible[0]['nom'].lower()}** » (note médiane {_fr(point_faible[1], 0)}/100)."
        + (f" Par rapport au reste de la région, il se distingue surtout par un écart sur « {ecarts_tri[0][0]['nom'].lower()} » ({_fr(ecarts_tri[0][3], 0)} points)." if ecarts_tri and ecarts_tri[0][3] < -5 else ""),
        (f"**{_fr(plaf)} lieux** ({_pct(plaf / n)}) sont plafonnés faute d'un essentiel à distance raisonnable : c'est là que l'effort de terrain serait le plus visible." if plaf else
         "Aucun lieu n'est plafonné : les essentiels (dormir, manger, se garer) sont présents à distance raisonnable partout."),
    ]
    tabs = [{"titre": f"Profil de {chef['nom']} indicateur par indicateur", "colonnes": ["Indicateur", f"{chef['nom']} (note médiane)", "Reste de la région", "Écart", "Lieux hors de portée"],
             "lignes": [[i["nom"], _fr(mv, 0), _fr(mr, 0), ("+" if d and d > 0 else "") + _fr(d, 0), _fr(zones_blanches[i["cle"]])] for i, mv, mr, d in ecarts]}]
    if isoles:
        tabs.append(_tableau_lieux(f"Lieux plafonnés de {chef['nom']} (les plus isolés d'abord)", isoles, "Essentiel manquant",
                                   lambda x: ", ".join(IND_PAR_CLE[k]["nom"].lower() for k in ESSENTIELS if x["sub"][k] <= 0)))
    tabs.append(_tableau_lieux(f"Les cinq lieux les mieux équipés de {chef['nom']}", sorted(V, key=lambda x: -x["note"])[:5]))
    return _sec(
        "zoom-equipement", f"{chef['nom']} : capacité d'accueil lieu par lieu",
        f"Les lieux de tournage {_du(chef['nom'])} sont-ils mieux ou moins bien équipés que ceux du reste de la région, et sur quels points précis ?",
        f"{_dans(chef['nom']).capitalize()}, la note médiane est de **{_fr(med_v, 0)}/100**"
        + (f" ({'au-dessus' if med_v > med_r + 2 else 'en dessous' if med_v < med_r - 2 else 'proche'} du reste de la région : {_fr(med_r, 0)}/100)" if med_r is not None else "")
        + f". **{_pct((comptes.get('tres_bon', 0) + comptes.get('bon', 0)) / n)}** des lieux offrent un bon accueil, **{_pct((comptes.get('limite', 0) + comptes.get('tres_limite', 0)) / n)}** un accueil limité.",
        chiffres=[_chiffre(_fr(med_v, 0), f"note médiane — {chef['nom']}"), _chiffre(_fr(med_r, 0) if med_r is not None else "n.d.", "note médiane — reste de la région"),
                  _chiffre(_fr(n), "lieux notés"), _chiffre(_fr(plaf), "lieux plafonnés")],
        graphiques=[
            {"id": "g-ecarts", "type": "ecarts", "titre": f"Écart de {chef['nom']} avec le reste de la région, par indicateur (points sur 100)",
             "etiquettes": [e[0]["nom"] for e in ecarts_tri], "valeurs": [round(e[3], 1) for e in ecarts_tri]},
            {"id": "g-zb", "type": "barres_h", "titre": f"Nombre de lieux « hors de portée » pour chaque besoin — {chef['nom']}",
             "etiquettes": [IND_PAR_CLE[k]["nom"] for k, _ in zb_tri], "valeurs": [v for _, v in zb_tri], "unite": "lieux"},
        ],
        tableaux=tabs, lecture=lec,
        attention=[f"{_fr(n)} lieux notés : échantillon suffisant pour une analyse fiable." if n >= SEUIL_COMPARABLE else
                   f"Seulement {n} lieux notés : prudence dans l'interprétation."],
    )


def sec_zoom_communes(P: dict, SD: list[dict]) -> dict:
    chef = _chef(SD)
    M = [x for x in P["lieux"] if x["mesure"] and chef and x["dept_nom"] == chef["nom"]]
    if not chef:
        return _sec("zoom-communes", "Disparités entre communes", "", "Pas de département de référence.")
    g = defaultdict(list)
    noms = {}
    for x in M:
        if x["commune_cle"]:
            g[x["commune_cle"]].append(x)
            noms.setdefault(x["commune_cle"], x.get("commune"))
    gros = {c: v for c, v in g.items() if len(v) >= SEUIL_COMMUNE}
    if len(gros) < 2:
        return _sec("zoom-communes", f"{chef['nom']} : disparités entre communes", "", f"Moins de deux communes {_du(chef['nom'])} comptent au moins {SEUIL_COMMUNE} lieux notés : la comparaison ne serait pas fiable.")
    lignes = sorted(({"nom": noms[c], "n": len(v), "med": S.mediane(_notes(v)), "min": min(_notes(v)), "max": max(_notes(v)),
                      "faible": min(INDICATEURS, key=lambda i: S.mediane([x["sub"][i["cle"]] for x in v]))["nom"], "notes": _notes(v)} for c, v in gros.items()), key=lambda r: -r["med"])
    kw = S.kruskal_wallis([r["notes"] for r in lignes])
    ecart_interne = lignes[0]["med"] - lignes[-1]["med"]
    couvert = sum(r["n"] for r in lignes) / len(M) if M else 0
    return _sec(
        "zoom-communes", f"{chef['nom']} : disparités entre communes",
        f"À l'intérieur {_du(chef['nom'])}, l'accueil dépend-il de la commune où se trouve le lieu ?",
        f"Entre communes {_du(chef['nom'])} comptant au moins {SEUIL_COMMUNE} lieux notés, la note médiane varie de **{_fr(lignes[-1]['med'], 0)}** ({lignes[-1]['nom']}) à **{_fr(lignes[0]['med'], 0)}** ({lignes[0]['nom']}), "
        f"soit **{_fr(ecart_interne, 0)} points** d'écart. Test : {_lecture_p(kw['p']) if kw else 'impossible'}.",
        chiffres=[_chiffre(_fr(len(lignes)), "communes comparées"), _chiffre(_pct(couvert), "des lieux notés couverts"), _chiffre(_fr(ecart_interne, 0), "points d'écart interne", "meilleure − plus faible commune")],
        graphiques=[{"id": "g-comm", "type": "barres_h", "titre": "Note médiane par commune", "etiquettes": [r["nom"] for r in lignes], "valeurs": [round(r["med"], 1) for r in lignes], "unite": "/100", "max": 100}],
        tableaux=[{"titre": "Les communes comparées", "colonnes": ["Commune", "Lieux notés", "Note médiane", "Note la plus basse", "Note la plus haute", "Point le plus faible"],
                   "lignes": [[r["nom"], _fr(r["n"]), _fr(r["med"], 0), _fr(r["min"], 0), _fr(r["max"], 0), r["faible"]] for r in lignes]}],
        lecture=[f"Une commune bien équipée peut « porter » les lieux voisins : c'est l'enjeu d'une stratégie de pôles, où les communes les mieux dotées servent de base de séjour pour des sites plus isolés." if ecart_interne >= 15 else
                 "Les communes se ressemblent : l'accueil dépend davantage du lieu précis que de la commune.",
                 "Les petites communes (moins de trois lieux notés) ne sont pas comparées : une seule valeur ne permet pas de conclure."],
        attention=["Une commune compte souvent peu de lieux : lire les écarts comme des tendances, à confirmer sur le terrain."],
    )


# ───────────────────────────── PARTIE G : conclure ─────────────────────────────

def sec_priorites(P: dict, SN: list[dict]) -> dict:
    M = [x for x in P["lieux"] if x["mesure"]]
    pop = [(x, float(x["popularite"])) for x in M if x.get("popularite") is not None]
    if len(pop) < 10:
        return _sec("priorites", "Où agir en priorité ?", "", "La popularité des œuvres n'est pas renseignée pour assez de lieux : le croisement avec la notoriété n'est pas possible.")
    rang = S.rang_percentile([p for _, p in pop])
    items = []
    for (x, p), r in zip(pop, rang):
        items.append({"x": x, "pop": r})
    seuil_pop, seuil_note = 60.0, 45.0
    cible = [i for i in items if i["pop"] >= seuil_pop and i["x"]["note"] < seuil_note]
    vitrines = [i for i in items if i["pop"] >= seuil_pop and i["x"]["note"] >= 60]
    discrets = [i for i in items if i["pop"] < seuil_pop and i["x"]["note"] >= 60]
    cible.sort(key=lambda i: (-i["pop"], i["x"]["note"]))
    sp = S.spearman([i["pop"] for i in items], [i["x"]["note"] for i in items])
    comp = [r for r in SN if r["comparable"]]
    actions = []
    for r in sorted(comp, key=lambda z: z["med"]):
        actions.append([r["nom"], _fr(r["med"], 0), IND_PAR_CLE[r["faible"]]["nom"], _fr(r["ind"][r["faible"]], 0) + "/100",
                        f"{_fr(sum(1 for x in r['lieux'] if x['cl'][r['faible']] == 3))} lieu(x) hors de portée"])
    id_c, id_v, id_d = {id(i) for i in cible}, {id(i) for i in vitrines}, {id(i) for i in discrets}
    pts = [{"x": round(i["pop"], 1), "y": round(i["x"]["note"], 1), "nom": f"{i['x']['nom']} ({i['x']['titre']})",
            "q": "cible" if id(i) in id_c else "vitrine" if id(i) in id_v else "discret" if id(i) in id_d else "autre"} for i in items]
    return _sec(
        "priorites", "Où agir en priorité ?",
        "Quels lieux combinent forte notoriété et faible capacité d'accueil, c'est-à-dire les plus gros manques à combler ?",
        f"**{_fr(len(cible))} lieux** liés à des œuvres très populaires offrent un accueil limité ou très limité : ce sont les **priorités d'aménagement**. "
        f"À l'inverse, **{_fr(len(vitrines))} lieux** sont à la fois célèbres et bien équipés : de véritables vitrines.",
        chiffres=[_chiffre(_fr(len(cible)), "priorités", "populaires et peu équipés"), _chiffre(_fr(len(vitrines)), "vitrines", "populaires et bien équipés"),
                  _chiffre(_fr(len(discrets)), "atouts discrets", "bien équipés, peu connus"), _chiffre(_fr(sp["rho"], 2) if sp else "n.d.", "lien notoriété ↔ équipement", "corrélation de rang")],
        graphiques=[{"id": "g-quadrant", "type": "nuage", "titre": "Notoriété de l'œuvre × capacité d'accueil du lieu", "points": pts,
                     "axe_x": "Notoriété de l'œuvre (rang, 0 à 100)", "axe_y": "Capacité d'accueil (note /100)", "seuil_x": seuil_pop, "seuil_y": seuil_note}],
        tableaux=[_tableau_lieux("Les dix premières priorités (les plus célèbres, accueil le plus faible)", [i["x"] for i in cible[:10]], "Notoriété (rang /100)",
                                 lambda x: _fr(next(i["pop"] for i in items if i["x"] is x), 0)),
                  {"titre": "Levier prioritaire par département", "colonnes": ["Département", "Note médiane", "Point le plus faible", "Note de ce point", "Ampleur"], "lignes": actions}] if actions else
                 [_tableau_lieux("Les dix premières priorités", [i["x"] for i in cible[:10]])],
        lecture=[
            "Le croisement est volontairement simple : **notoriété** de l'œuvre (rang parmi toutes les œuvres, de 0 à 100) et **capacité d'accueil** du lieu. "
            "Les lieux en haut à gauche du graphique sont connus mais mal équipés ; les lieux en haut à droite sont des réussites.",
            (f"Lien entre notoriété et équipement : {_fr(sp['rho'], 2)} — " + ("les lieux célèbres ne sont pas mieux équipés que les autres : l'équipement ne suit pas la demande." if abs(sp['rho']) < 0.2 else
                                                                                "les lieux célèbres tendent à être mieux équipés." if sp['rho'] > 0 else "les lieux célèbres tendent à être moins bien équipés.")) if sp else "",
            "Pour chaque département, le levier prioritaire est le besoin pour lequel la note médiane est la plus basse : c'est là que chaque euro investi change le plus l'accueil.",
        ],
        attention=["La popularité provient d'une base de films internationale : elle reflète la notoriété globale de l'œuvre, pas son attrait touristique local."],
    )


def sec_fiabilite(P: dict) -> dict:
    L = P["lieux"]
    M = [x for x in L if x["mesure"]]
    n = len(L)
    if not M:
        return _sec("fiabilite", "Fiabilité et limites", "", "Pas de mesure.")
    est = {i["cle"]: sum(1 for x in M if x["est"][i["cle"]]) / len(M) for i in INDICATEURS if i["type"] == "temps"}
    deduits = sum(1 for x in L if x["dept_deduit"])
    non_loc = sum(1 for x in L if not x["dept_nom"])
    comp = len({x["dept_nom"] for x in M if x["dept_nom"]})
    petits = [d for d, v in _groupes_departements(M).items() if len(v) < SEUIL_COMPARABLE]
    return _sec(
        "fiabilite", "Fiabilité des données et limites de l'analyse",
        "Que peut-on affirmer avec confiance, et que ne sait-on pas dire ?",
        f"L'analyse repose sur **{_fr(len(M))} lieux mesurés** sur {_fr(n)}. Les durées proviennent d'itinéraires IGN précalculés, complétés par une estimation signalée quand le trajet manque. "
        "Elle décrit la **proximité** des équipements, non leur qualité ni leur disponibilité.",
        chiffres=[_chiffre(_pct(len(M) / n), "lieux notés"), _chiffre(_pct(sum(est.values()) / len(est)) if est else "n.d.", "durées estimées en moyenne", "le reste vient d'itinéraires IGN"),
                  _chiffre(_fr(deduits), "lieux au département déduit", "à partir de leurs coordonnées"), _chiffre(_fr(non_loc), "lieux non localisés")],
        tableaux=[{"titre": "Part des durées estimées (et non calculées par itinéraire), par indicateur", "colonnes": ["Indicateur", "Durées estimées"],
                   "lignes": [[IND_PAR_CLE[k]["nom"], _pct(v)] for k, v in sorted(est.items(), key=lambda kv: -kv[1])]}],
        lecture=[
            "**Ce que l'analyse permet :** repérer où l'environnement d'un lieu de tournage rend la visite difficile, comparer les départements sur des bases identiques, hiérarchiser les besoins d'aménagement.",
            "**Ce qu'elle ne permet pas :** juger la qualité des hébergements ou des restaurants, savoir s'ils sont ouverts ou complets, mesurer la fréquentation réelle des lieux, tenir compte de la saison.",
        ],
        attention=[
            "Les seuils de temps et les poids sont des choix argumentés mais non uniques ; leur influence est testée dans la partie D.",
            ("Les départements comptant moins de " + str(SEUIL_COMPARABLE) + " lieux notés (" + _liste(petits) + ") ne sont pas comparés.") if petits else "",
            "Les équipements ne sont connus que dans le rayon de recherche utilisé lors de la collecte : « aucun équipement » signifie « aucun dans ce rayon ».",
            "Les lieux sont comptés une fois par œuvre : un endroit très filmé pèse davantage dans les moyennes.",
        ],
        detail=[_terme("Source des durées", "", "Itinéraires de la Géoplateforme IGN, précalculés depuis chaque lieu vers les équipements les plus proches."),
                _terme("Estimation de repli", "", f"Quand un trajet n'est pas calculé : distance à vol d'oiseau × {_fr(P['ratio'], 2)} ({'rapport médian observé' if P['ratio_mesure'] else 'valeur par défaut'}) ÷ 45 km/h en voiture, ÷ 4,5 km/h à pied."),
                _terme("Fourchettes plausibles", "", "Proportions : méthode de Wilson. Médianes : 1 000 rééchantillonnages (bootstrap). Niveau de confiance : 95 %.")],
    )


# ───────────────────────────── assemblage ─────────────────────────────

def construire(donnees: dict, region: str = "Occitanie") -> dict:
    P = preparer(donnees)
    SD = stats_departements(P)
    SN = stats_notes_departements(P)
    chef = _chef(SD)
    nom_chef = chef["nom"] if chef else "département n° 1"
    parties = [
        {"id": "A", "titre": "Cadrer le sujet", "intro": "Combien de lieux, et où ? On commence par délimiter ce que l'on étudie.",
         "sections": [sec_perimetre(P), sec_repartition(P, SD)]},
        {"id": "B", "titre": "Choisir les indicateurs", "intro": "Que faut-il mesurer pour juger si un lieu peut accueillir des visiteurs ? On choisit, on teste, on écarte.",
         "sections": [sec_demarche(P)]},
        {"id": "C", "titre": "Mesurer, indicateur par indicateur", "intro": "Huit besoins du visiteur, étudiés un par un : définition, seuils, résultats, comparaison entre départements.",
         "sections": [sec_indicateur(P, i, k) for k, i in enumerate(INDICATEURS, 1)]},
        {"id": "D", "titre": "Noter chaque lieu", "intro": "Les huit mesures deviennent une note sur 100 ; on vérifie que cette note résiste à d'autres choix raisonnables.",
         "sections": [sec_scoring(P), sec_resultats(P)]},
        {"id": "E", "titre": "Comparer les départements", "intro": "Les différences observées sont-elles réelles, ou dues au hasard ?",
         "sections": [sec_comparaison(P, SN, SD)]},
        {"id": "F", "titre": f"Approfondir : {nom_chef}", "intro": f"{nom_chef} compte le plus de lieux de tournage : on l'étudie à la loupe.",
         "sections": [sec_zoom_portrait(P, SD), sec_zoom_equipement(P, SD, SN), sec_zoom_communes(P, SD)]},
        {"id": "G", "titre": "Conclure", "intro": "Où agir en priorité, et jusqu'où l'analyse permet de conclure.",
         "sections": [sec_priorites(P, SN), sec_fiabilite(P)]},
    ]
    n = 0
    for p in parties:
        for s in p["sections"]:
            n += 1
            s["numero"] = n
    L = P["lieux"]
    M = [x for x in L if x["mesure"]]
    return {
        "region": region, "genere_le": datetime.now().isoformat(timespec="seconds"),
        "nb_lieux": len(L), "nb_notes": len(M), "departement_phare": nom_chef,
        "note_mediane": S.mediane(_notes(M)) if M else None,
        "classes": [{"cle": c[0], "libelle": c[1], "min": c[2], "couleur": c[3]} for c in CLASSES],
        "parties": parties,
    }


@router.get("/api/analyse/entonnoir")
async def api_analyse_entonnoir(region: str = Query("Occitanie"), rafraichir: bool = Query(False)):
    if not rafraichir and _CACHE["data"] and _CACHE["region"] == region and time.time() - _CACHE["t"] < TTL_S:
        return _CACHE["data"]
    try:
        donnees = await charger_donnees()
        resultat = construire(donnees, region)
    except Exception as exc:
        logger.exception("Analyse en entonnoir : échec")
        raise HTTPException(status_code=500, detail=f"Calcul de l'analyse impossible : {exc}")
    _CACHE.update({"t": time.time(), "region": region, "data": resultat})
    return resultat 