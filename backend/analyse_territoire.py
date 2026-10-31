"""
backend/analyse_territoire.py

Analyse territoriale « du global au précis », écrite pour un élu ou un citoyen non spécialiste.

    GET /api/analyse/territoire?region=Occitanie

Neuf chapitres : l'essentiel → où → quand → quelles œuvres → comparer les grands départements →
l'offre autour des lieux → l'accessibilité → où agir en priorité → fiabilité et méthode.
Chaque chapitre contient : une question, « en bref », des chiffres clés, des graphiques, une lecture
en langage courant, des mises en garde, et (pour les curieux) les valeurs statistiques détaillées.

Principes :
- Toutes les distances et durées viennent des itinéraires IGN précalculés (amenity_cache), jamais du
  vol d'oiseau ; la différence entre les deux est mesurée au chapitre 6.
- Médiane plutôt que moyenne ; toute proportion ou médiane est accompagnée de sa fourchette plausible.
- Les écarts entre départements sont testés (Kruskal-Wallis) : « réel » ou « peut-être dû au hasard ».
- Les textes sont produits à partir des chiffres par des règles simples et publiques (pas d'IA).
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

# Besoins d'un visiteur → équipement le plus proche (durée IGN), seuil « confortable » en minutes.
BESOINS = [
    ("dormir", "Dormir", [("hebergement", "voiture", 10)]),
    ("manger", "Manger", [("restaurant", "voiture", 10)]),
    ("garer", "Se garer", [("parking", "voiture", 5)]),
    ("transport", "Prendre un transport", [("arret_bus", "pied", 10), ("gare", "voiture", 20)]),
    ("informer", "S'informer", [("office_tourisme", "voiture", 20)]),
    ("soigner", "Se soigner", [("hopital", "voiture", 30)]),
]
CATEGORIES_ANALYSEES = ["hebergement", "restaurant", "parking", "arret_bus", "gare", "aeroport",
                        "office_tourisme", "hopital", "police", "distributeur", "activite"]
LIBELLES_CATEGORIES = {
    "hebergement": "Hébergements", "restaurant": "Restaurants", "parking": "Parkings", "arret_bus": "Arrêts de bus",
    "gare": "Gares", "aeroport": "Aéroports", "office_tourisme": "Offices de tourisme", "hopital": "Hôpitaux",
    "police": "Police / gendarmerie", "distributeur": "Distributeurs / banques", "activite": "Activités",
}
SEUIL_COMPARABLE = 10      # nombre minimal de lieux pour comparer un département
SEUIL_GRAND = 20           # « grand » département du ciné-tourisme dans la base
SEUIL_BIEN_EQUIPE = 66.0   # score d'équipement (sur 100) à partir duquel un lieu est « bien équipé »


# ───────────── mise en forme ─────────────

def _norm(s) -> str:
    t = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().lower()
    return " ".join("".join(c if c.isalnum() else " " for c in t).split())


def _fr(x, d: int = 0) -> str:
    """Nombre à la française : 1 234,5."""
    if x is None:
        return "n.d."
    return f"{x:,.{d}f}".replace(",", "\u202f").replace(".", ",")


def _an(x) -> str:
    return "n.d." if x is None else str(int(round(x)))


def _pct(x, d: int = 0) -> str:
    return "n.d." if x is None else f"{_fr(100 * x, d)} %"


def _minutes(s) -> float | None:
    return None if s is None else s / 60.0


def _fourchette(ic, fmt=lambda v: _fr(v, 0)) -> str:
    return "n.d." if not ic else f"{fmt(ic[0])} à {fmt(ic[1])}"


def _fourchette_pct(ic) -> str:
    return "n.d." if not ic else f"{_fr(100 * ic[0], 0)} à {_fr(100 * ic[1], 0)} %"


def _lecture_gini(g: float) -> str:
    if g < 0.30:
        return "plutôt équilibrée"
    if g < 0.45:
        return "inégale"
    if g < 0.60:
        return "très inégale"
    return "extrêmement concentrée"


def _lecture_p(p: float | None) -> str:
    if p is None:
        return "test impossible (trop peu de données)"
    if p < 0.01:
        return "très peu probable que ce soit dû au hasard"
    if p < 0.05:
        return "probablement réel, mais à confirmer avec davantage de données"
    return "on ne peut pas exclure que ce soit dû au hasard"


def _lecture_effet(e: float | None) -> str:
    if e is None:
        return ""
    if e < 0.06:
        return "faible"
    if e < 0.14:
        return "moyen"
    return "important"


# ───────────── chargement des données ─────────────

def _aire_anneau_km2(anneau: list) -> float:
    """Aire d'un contour (lon/lat) en km², par projection locale (précision ~1 %, suffisante ici)."""
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


def _dept_du_point(lat: float, lon: float, departements: list[dict]) -> dict | None:
    from meteo import _dans_anneau
    for d in departements:
        for r in d["rings"]:
            xs = [p[0] for p in r]; ys = [p[1] for p in r]
            if min(xs) <= lon <= max(xs) and min(ys) <= lat <= max(ys) and _dans_anneau(lon, lat, r):
                return d
    return None


async def charger_donnees() -> dict:
    lieux = await fetch_all(
        """
        SELECT l.id, l.nom, l.commune, l.departement, l.latitude, l.longitude, l.photo_url,
               (l.anecdote IS NOT NULL AND l.anecdote <> '') AS a_anecdote,
               f.id AS film_id, f.titre, f.annee, f.media_type, f.popularite, f.nationalite
        FROM lieux_tournage l JOIN films f ON f.id = l.film_id AND f.statut = 'publie'
        """
    )
    mesures = await fetch_all(
        """
        SELECT lieu_tournage_id AS lieu_id, categorie,
               min(duree_voiture_secondes) AS t_voiture, min(duree_pied_secondes) AS t_pied,
               min(distance_voiture_metres) AS d_voiture
        FROM amenity_cache WHERE categorie = ANY(%s) GROUP BY 1, 2
        """,
        (CATEGORIES_ANALYSEES,),
    )
    stats = await fetch_all(
        "SELECT lieu_tournage_id AS lieu_id, categorie, nombre_1000m, rayon_metres FROM amenity_stats WHERE categorie = ANY(%s)",
        (CATEGORIES_ANALYSEES,),
    )
    ratio = await fetch_one(
        """
        SELECT count(*) AS n,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY distance_voiture_metres::numeric / distance_metres) AS mediane,
               percentile_cont(0.9) WITHIN GROUP (ORDER BY distance_voiture_metres::numeric / distance_metres) AS p90,
               avg(distance_voiture_metres::numeric / distance_metres) AS moyenne
        FROM amenity_cache WHERE distance_metres > 100 AND distance_voiture_metres IS NOT NULL
        """
    )
    try:
        guides = await fetch_one(
            """
            SELECT count(DISTINCT l.id) AS lieux_couverts, count(DISTINCT g.id) AS guides
            FROM lieux_tournage l JOIN films f ON f.id = l.film_id AND f.statut = 'publie'
            JOIN guide_films gf ON gf.film_id = l.film_id JOIN guides g ON g.id = gf.guide_id AND g.statut = 'actif'
            """
        )
    except Exception:
        guides = None
    def num(d):
        return {k: (float(v) if hasattr(v, "quantize") else v) for k, v in dict(d).items()}
    return {"lieux": [num(x) for x in lieux], "mesures": [num(x) for x in mesures], "stats": [num(x) for x in stats],
            "ratio": num(ratio) if ratio else None, "guides": dict(guides) if guides else None,
            "departements": charger_departements()}


# ───────────── préparation ─────────────

def _score_besoin(alternatives, mes_lieu: dict, seuil_facteur: float = 1.0) -> tuple[float, float | None]:
    """Score 0-1 d'un besoin : 1 si l'équipement est à moins du seuil, 0 au-delà de 2 × le seuil."""
    meilleur, minutes = 0.0, None
    for cat, mode, seuil in alternatives:
        m = mes_lieu.get(cat)
        t = _minutes(m.get("t_voiture" if mode == "voiture" else "t_pied")) if m else None
        if t is None:
            continue
        s = seuil * seuil_facteur
        score = 1.0 if t <= s else 0.0 if t >= 2 * s else 1.0 - (t - s) / s
        if minutes is None or score > meilleur:
            meilleur, minutes = score, t
    return meilleur, minutes


def preparer(donnees: dict) -> dict:
    deps = donnees["departements"]
    par_norm = {_norm(d["nom"]): d for d in deps}
    mes, st = defaultdict(dict), defaultdict(dict)
    for m in donnees["mesures"]:
        mes[m["lieu_id"]][m["categorie"]] = m
    for s in donnees["stats"]:
        st[s["lieu_id"]][s["categorie"]] = s
    lieux = []
    for l in donnees["lieux"]:
        d = par_norm.get(_norm(l.get("departement"))) if l.get("departement") else None
        deduit = False
        if d is None and l.get("latitude") is not None:
            d = _dept_du_point(float(l["latitude"]), float(l["longitude"]), deps)
            deduit = d is not None
        x = dict(l)
        x["dept_code"], x["dept_nom"] = (d["code"], d["nom"]) if d else (None, "Non localisé")
        x["dept_deduit"] = deduit
        x["mes"], x["st"] = mes.get(l["id"], {}), st.get(l["id"], {})
        x["besoins"] = {cle: dict(zip(("score", "minutes"), _score_besoin(alts, x["mes"]))) for cle, _, alts in BESOINS}
        x["equipement"] = 100.0 * sum(b["score"] for b in x["besoins"].values()) / len(BESOINS)
        x["besoins_couverts"] = sum(1 for b in x["besoins"].values() if b["score"] >= 1.0)
        lieux.append(x)
    return {"lieux": lieux, "departements": deps, "ratio": donnees.get("ratio"), "guides": donnees.get("guides")}


# ───────────── chapitres ─────────────

def chapitre_apercu(P: dict) -> dict:
    L = P["lieux"]
    films = {l["film_id"]: l for l in L}
    communes = {l["commune"] for l in L if l.get("commune")}
    annees = [f["annee"] for f in films.values() if f.get("annee")]
    series = sum(1 for f in films.values() if f["media_type"] == "tv")
    depts_ok = {l["dept_code"] for l in L if l["dept_code"]}
    ic_series = S.intervalle_wilson(series, len(films))
    par_oeuvre = list(Counter(l["film_id"] for l in L).values())
    return {
        "id": "apercu", "numero": 1, "titre": "L'essentiel en un coup d'œil",
        "question": "De quoi parle-t-on exactement ?",
        "en_bref": (f"La base recense {len(films)} œuvres (films et séries) tournées en Occitanie, pour "
                    f"{len(L)} lieux de tournage dans {len(communes)} communes et {len(depts_ok)} départements, de {_an(min(annees))} à {_an(max(annees))}."),
        "chiffres": [
            {"valeur": len(films), "label": "œuvres", "aide": "Films et séries dont au moins une scène est tournée dans la région."},
            {"valeur": len(L), "label": "lieux de tournage", "aide": "Un lieu = un endroit précis (un château, un village, une plage…)."},
            {"valeur": len(communes), "label": "communes concernées", "aide": None},
            {"valeur": len(depts_ok), "label": "départements représentés", "aide": "Sur 13 départements en Occitanie."},
            {"valeur": _pct(series / len(films)), "label": "de séries", "aide": f"Fourchette plausible : {_fourchette_pct(ic_series)}."},
            {"valeur": _fr(S.mediane(par_oeuvre), 0), "label": "lieu(x) par œuvre (médiane)", "aide": f"Moyenne {_fr(S.moyenne(par_oeuvre), 1)} : quelques œuvres ont beaucoup de lieux."},
        ],
        "graphiques": [],
        "lecture": ["Ce chapitre pose le décor. Les suivants vont du plus large (où tourne-t-on ?) au plus précis (quels lieux faut-il équiper en priorité ?)."],
        "attention": ["Ces chiffres décrivent ce qui est **référencé** dans la base, pas l'ensemble des tournages réalisés en Occitanie."],
        "pour_les_curieux": [],
    }


def chapitre_repartition(P: dict) -> dict:
    L, deps = P["lieux"], P["departements"]
    cpt = Counter(l["dept_code"] for l in L if l["dept_code"])
    n_loc, non_loc = sum(cpt.values()), sum(1 for l in L if not l["dept_code"])
    oeuvres, communes = defaultdict(set), defaultdict(set)
    for l in L:
        if l["dept_code"]:
            oeuvres[l["dept_code"]].add(l["film_id"])
            communes[l["dept_code"]].add(l["commune"])
    lignes = []
    for d in deps:
        n = cpt.get(d["code"], 0)
        lignes.append({"code": d["code"], "departement": d["nom"], "lieux": n, "part": n / n_loc if n_loc else 0,
                       "oeuvres": len(oeuvres[d["code"]]), "communes": len(communes[d["code"]]),
                       "densite": 1000 * n / d["aire_km2"] if d["aire_km2"] else None})
    lignes.sort(key=lambda r: -r["lieux"])
    valeurs = [r["lieux"] for r in lignes]
    g, h, top3 = S.gini(valeurs), S.hhi(valeurs), S.part_des_premiers(valeurs, 3)
    pts = S.courbe_lorenz(valeurs)
    sans = [r["departement"] for r in lignes if r["lieux"] == 0]
    cpt_com = Counter(l["commune"] for l in L if l.get("commune"))
    part_com_unique = sum(1 for c in cpt_com.values() if c == 1) / len(cpt_com)
    premier = lignes[0]
    dens_med = S.mediane([r["densite"] for r in lignes if r["densite"] is not None])
    moitie = math.ceil(len(valeurs) / 2)
    return {
        "id": "repartition", "numero": 2, "titre": "Où tourne-t-on ?",
        "question": "L'activité de tournage est-elle répartie sur tout le territoire ou concentrée ?",
        "en_bref": (f"La répartition est {_lecture_gini(g)} : {premier['departement']} concentre à lui seul {_pct(premier['part'])} des lieux, "
                    f"et les 3 premiers départements en regroupent {_pct(top3)}."),
        "chiffres": [
            {"valeur": _pct(premier["part"]), "label": f"des lieux en {premier['departement']}", "aide": f"{premier['lieux']} lieux sur {n_loc} localisés."},
            {"valeur": _pct(top3), "label": "dans les 3 premiers départements", "aide": "Si tout était réparti équitablement entre 13 départements, ce serait 23 %."},
            {"valeur": _fr(g, 2), "label": "indice de Gini", "aide": "0 = répartition parfaitement égale ; 1 = tout au même endroit."},
            {"valeur": len(sans), "label": "département(s) sans aucun lieu", "aide": ", ".join(sans) if sans else None},
        ],
        "graphiques": [
            {"id": "graphe-dept", "type": "barres_h", "titre": "Nombre de lieux de tournage par département",
             "etiquettes": [r["departement"] for r in lignes], "valeurs": [r["lieux"] for r in lignes]},
            {"id": "graphe-lorenz", "type": "lorenz", "titre": "Courbe de concentration (Lorenz)",
             "points": [[round(x, 4), round(y, 4)] for x, y in pts]},
        ],
        "tableaux": [
            {"titre": "Détail par département", "colonnes": ["Département", "Lieux", "Part", "Œuvres", "Communes", "Lieux / 1 000 km²"],
             "lignes": [[r["departement"], r["lieux"], _pct(r["part"]), r["oeuvres"], r["communes"], _fr(r["densite"], 1)] for r in lignes]},
            {"titre": "Communes les plus citées", "colonnes": ["Commune", "Lieux de tournage"], "lignes": [[c, n] for c, n in cpt_com.most_common(8)]}],
        "lecture": [
            "**Comment lire la courbe de concentration :** si les lieux étaient répartis équitablement, la courbe suivrait la diagonale. "
            f"Plus elle se creuse, plus quelques départements captent l'essentiel de l'activité. Ici, les {moitie} départements les moins dotés (sur {len(valeurs)}) ne portent que "
            f"{_pct(pts[moitie][1])} des lieux.",
            f"**À l'échelle des communes :** {_pct(part_com_unique)} des communes ne comptent qu'un seul lieu : le tournage est un phénomène de « points » plus que de grandes zones.",
        ],
        "attention": [
            f"{non_loc} lieu(x) n'ont pas pu être rattaché(s) à un département." if non_loc else "Tous les lieux sont rattachés à un département.",
            "Cette concentration reflète ce que la base **recense** : un département peu représenté est peut-être simplement moins documenté, pas moins photogénique.",
            "Les densités par 1 000 km² sont calculées à partir des contours officiels des départements ; elles ne tiennent pas compte de la population.",
        ],
        "pour_les_curieux": [
            {"terme": "Indice de Gini", "valeur": _fr(g, 3), "explication": "Mesure d'inégalité (0 à 1), utilisée aussi pour les revenus. Ici appliquée aux nombres de lieux par département (13 départements, y compris ceux à zéro)."},
            {"terme": "Indice de Herfindahl-Hirschman (HHI)", "valeur": _fr(h, 0), "explication": "De 0 à 10 000. Mesure la dépendance à quelques acteurs ; au-delà de 2 500, on parle de marché très concentré. 13 départements parfaitement égaux donneraient 769."},
            {"terme": "Densité médiane", "valeur": f"{_fr(dens_med, 1)} lieux / 1 000 km²", "explication": "Rapporte le nombre de lieux à la surface du département (contours officiels)."},
        ],
        "_synthese": {"gini": g, "top3": top3, "premier": premier["departement"], "part_premier": premier["part"]},
    }


def chapitre_temps(P: dict) -> dict:
    films = {l["film_id"]: l for l in P["lieux"]}
    par_dec: dict[int, dict] = defaultdict(lambda: {"oeuvres": 0, "series": 0, "lieux": 0})
    for l in P["lieux"]:
        if l.get("annee"):
            par_dec[(l["annee"] // 10) * 10]["lieux"] += 1
    for f in films.values():
        if f.get("annee"):
            d = par_dec[(f["annee"] // 10) * 10]
            d["oeuvres"] += 1
            d["series"] += 1 if f["media_type"] == "tv" else 0
    decs = sorted(par_dec)
    annees = [f["annee"] for f in films.values() if f.get("annee")]
    depuis_2010 = sum(1 for a in annees if a >= 2010)
    ic = S.intervalle_wilson(depuis_2010, len(annees))
    med = S.mediane(annees)
    meilleure = max(decs, key=lambda d: par_dec[d]["oeuvres"])
    return {
        "id": "temps", "numero": 3, "titre": "Quand a-t-on tourné ?",
        "question": "Le tournage en Occitanie est-il un phénomène ancien ou récent ?",
        "en_bref": (f"La moitié des œuvres date d'après {_an(med)}. Les années {meilleure} sont les plus riches ({par_dec[meilleure]['oeuvres']} œuvres) ; "
                    f"{_pct(depuis_2010 / len(annees))} des œuvres référencées sont postérieures à 2010."),
        "chiffres": [
            {"valeur": _an(med), "label": "année médiane des œuvres", "aide": "La moitié des œuvres est plus ancienne, l'autre moitié plus récente."},
            {"valeur": _pct(depuis_2010 / len(annees)), "label": "depuis 2010", "aide": f"Fourchette plausible : {_fourchette_pct(ic)}."},
            {"valeur": f"{meilleure}s", "label": "décennie la plus riche", "aide": f"{par_dec[meilleure]['oeuvres']} œuvres."},
        ],
        "graphiques": [{"id": "graphe-decennies", "type": "barres", "titre": "Œuvres par décennie de sortie",
                        "etiquettes": [f"{d}s" for d in decs], "valeurs": [par_dec[d]["oeuvres"] for d in decs],
                        "valeurs2": [par_dec[d]["series"] for d in decs], "legende2": "dont séries"}],
        "tableaux": [{"titre": "Détail par décennie", "colonnes": ["Décennie", "Œuvres", "dont séries", "Lieux de tournage"],
                      "lignes": [[f"{d}s", par_dec[d]["oeuvres"], par_dec[d]["series"], par_dec[d]["lieux"]] for d in decs]}],
        "lecture": ["Les séries restent minoritaires mais apparaissent surtout dans les décennies récentes : elles tournent plus longtemps et reviennent plus souvent sur les mêmes lieux."],
        "attention": [
            "**Biais de documentation :** les œuvres récentes sont mieux recensées (bases ouvertes, tournages annoncés) que les anciennes. Une hausse apparente ne prouve pas à elle seule une hausse réelle des tournages.",
            "Peu d'œuvres par décennie (souvent moins de 20) : les écarts d'une décennie à l'autre sont à interpréter avec prudence.",
        ],
        "pour_les_curieux": [{"terme": "Médiane", "valeur": _an(med), "explication": "Valeur centrale : autant d'œuvres avant qu'après. Elle n'est pas faussée par quelques films très anciens ou très récents, contrairement à la moyenne."}],
        "_synthese": {},
    }


def chapitre_oeuvres(P: dict) -> dict:
    films = {l["film_id"]: l for l in P["lieux"]}
    nat = Counter((f.get("nationalite") or "Non renseignée") for f in films.values())
    pops = [float(f["popularite"]) for f in films.values() if f.get("popularite") is not None]
    moy, med = S.moyenne(pops), S.mediane(pops)
    top10 = S.part_des_premiers(pops, max(1, round(len(pops) * 0.1)))
    nb_lieux = Counter(l["film_id"] for l in P["lieux"])
    un_seul = sum(1 for n in nb_lieux.values() if n == 1) / len(nb_lieux)
    plus = sorted(films.values(), key=lambda f: -(f.get("popularite") or 0))[:8]
    return {
        "id": "oeuvres", "numero": 4, "titre": "Quelles œuvres ?",
        "question": "Quelles productions font vivre le territoire à l'écran ?",
        "en_bref": (f"Une minorité d'œuvres très populaires pèse très lourd : les 10 % les plus populaires cumulent {_pct(top10)} de la popularité totale. "
                    f"La popularité moyenne ({_fr(moy, 1)}) est supérieure à la médiane ({_fr(med, 1)}) : signe d'une série « tirée vers le haut » par quelques succès."),
        "chiffres": [
            {"valeur": _fr(med, 1), "label": "popularité médiane", "aide": "Indice de popularité TMDB : plus il est haut, plus l'œuvre est consultée."},
            {"valeur": _fr(moy, 1), "label": "popularité moyenne", "aide": "Écart avec la médiane = présence de quelques très gros succès."},
            {"valeur": _pct(top10), "label": "de la popularité dans le top 10 %", "aide": None},
            {"valeur": _pct(un_seul), "label": "des œuvres n'ont qu'un seul lieu", "aide": "Les autres ont plusieurs lieux : plus propices à un parcours."},
        ],
        "graphiques": [
            {"id": "graphe-nationalites", "type": "barres_h", "titre": "Nationalité des œuvres (principales)",
             "etiquettes": [k for k, _ in nat.most_common(8)], "valeurs": [v for _, v in nat.most_common(8)]},
            {"id": "graphe-populaires", "type": "barres_h", "titre": "Les 8 œuvres les plus populaires",
             "etiquettes": [f["titre"] for f in plus], "valeurs": [round(float(f.get("popularite") or 0), 1) for f in plus]},
        ],
        "tableaux": [],
        "lecture": [
            "**Pourquoi la médiane ?** Imaginons 9 foyers qui gagnent 2 000 € et un qui gagne 20 000 € : la moyenne (3 800 €) ne représente personne. "
            "La médiane (2 000 €) décrit le foyer « typique ». Même logique ici pour la popularité des œuvres.",
            "Pour un élu : les quelques grands titres sont des **leviers de notoriété** ; l'ensemble des autres œuvres constitue la **diversité** de l'offre.",
        ],
        "attention": ["La popularité est un indicateur d'audience mondiale à un instant donné, pas une mesure de l'attractivité touristique d'un lieu."],
        "pour_les_curieux": [{"terme": "Concentration du top 10 %", "valeur": _pct(top10), "explication": "Part du total de popularité détenue par les 10 % d'œuvres les plus populaires. À égalité parfaite, ce serait 10 %."}],
        "_synthese": {},
    }


def chapitre_departements(P: dict) -> dict:
    par = defaultdict(list)
    for l in P["lieux"]:
        par[l["dept_nom"]].append(l)
    comparables = {k: v for k, v in par.items() if len(v) >= SEUIL_COMPARABLE and k != "Non localisé"}
    petits = {k: len(v) for k, v in par.items() if len(v) < SEUIL_COMPARABLE and k != "Non localisé"}
    lignes = []
    for nom, ls in comparables.items():
        t_dormir = [b for b in (x["besoins"]["dormir"]["minutes"] for x in ls) if b is not None]
        equip = [x["equipement"] for x in ls]
        bien = sum(1 for e in equip if e >= SEUIL_BIEN_EQUIPE)
        lignes.append({"departement": nom, "n": len(ls), "grand": len(ls) >= SEUIL_GRAND,
                       "t_dormir_med": S.mediane(t_dormir), "equip_med": S.mediane(equip), "equip_ic": S.intervalle_bootstrap(equip),
                       "bien": bien / len(ls), "bien_ic": S.intervalle_wilson(bien, len(ls))})
    lignes.sort(key=lambda r: -r["equip_med"])
    if not lignes:
        return {"id": "departements", "numero": 5, "titre": "Comparer les grands départements", "question": "", "en_bref": "Pas assez de données pour comparer les départements.",
                "chiffres": [], "graphiques": [], "tableaux": [], "lecture": [], "attention": [], "pour_les_curieux": [], "_synthese": {"kw": None}}
    kw_e = S.kruskal_wallis([[x["equipement"] for x in ls] for ls in comparables.values()])
    kw_t = S.kruskal_wallis([[b for b in (x["besoins"]["dormir"]["minutes"] for x in ls) if b is not None] for ls in comparables.values()])
    meilleur, pire = lignes[0], lignes[-1]
    return {
        "id": "departements", "numero": 5, "titre": "Comparer les grands départements",
        "question": "Les lieux de tournage sont-ils aussi bien équipés d'un département à l'autre ?",
        "en_bref": (f"Sur {len(comparables)} départements comparables (au moins {SEUIL_COMPARABLE} lieux), le score d'équipement médian va de "
                    f"{_fr(pire['equip_med'], 0)}/100 ({pire['departement']}) à {_fr(meilleur['equip_med'], 0)}/100 ({meilleur['departement']}). "
                    + (f"Cet écart est {_lecture_effet(kw_e['epsilon2'])} ; {_lecture_p(kw_e['p'])}." if kw_e else "")),
        "chiffres": [
            {"valeur": f"{_fr(meilleur['equip_med'], 0)} / 100", "label": f"équipement médian · {meilleur['departement']}", "aide": f"Fourchette plausible : {_fourchette(meilleur['equip_ic'])}."},
            {"valeur": f"{_fr(pire['equip_med'], 0)} / 100", "label": f"équipement médian · {pire['departement']}", "aide": f"Fourchette plausible : {_fourchette(pire['equip_ic'])}."},
            {"valeur": _fr(kw_e["p"], 3) if kw_e else "n.d.", "label": "p-value (test de Kruskal-Wallis)", "aide": "Plus elle est petite, moins l'écart observé est attribuable au hasard (< 0,05 : écart probablement réel)."},
        ],
        "graphiques": [{"id": "graphe-dept-equip", "type": "barres_h", "titre": "Score d'équipement médian par département (sur 100)",
                        "etiquettes": [r["departement"] for r in lignes], "valeurs": [round(r["equip_med"], 1) for r in lignes],
                        "bas": [round(r["equip_ic"][0], 1) if r["equip_ic"] else None for r in lignes],
                        "haut": [round(r["equip_ic"][1], 1) if r["equip_ic"] else None for r in lignes]}],
        "tableaux": [{"titre": "Comparaison détaillée", "colonnes": ["Département", "Lieux", "Équipement /100 (médiane)", "Fourchette plausible", "Lieux « bien équipés »", "Temps médian pour dormir (min)"],
                      "lignes": [[("★ " if r["grand"] else "") + r["departement"], r["n"], _fr(r["equip_med"], 0), _fourchette(r["equip_ic"]),
                                  f"{_pct(r['bien'])} ({_fourchette_pct(r['bien_ic'])})", _fr(r["t_dormir_med"], 0)] for r in lignes]}],
        "lecture": [
            "**★ = grand département du ciné-tourisme** (au moins 20 lieux dans la base). Les départements avec moins de 10 lieux ne sont pas comparés : un département à 2 lieux ne permet aucune conclusion"
            + (f" ({', '.join(f'{k} : {v}' for k, v in sorted(petits.items(), key=lambda kv: -kv[1]))})." if petits else "."),
            "**Comment lire la « fourchette plausible » :** c'est l'intervalle dans lequel se situe très probablement la vraie valeur du département. Si les fourchettes de deux départements se chevauchent largement, leur différence n'est pas démontrée.",
            (f"**Écart réel ou hasard ?** Le test de Kruskal-Wallis compare tous les départements d'un coup : p = {_fr(kw_e['p'], 3)} → {_lecture_p(kw_e['p'])}. "
             f"La taille de l'effet ({_fr(kw_e['epsilon2'], 2)}) indique un écart {_lecture_effet(kw_e['epsilon2'])}.") if kw_e else "",
        ],
        "attention": [
            "**Des lieux qui se ressemblent :** plusieurs lieux d'une même commune partagent les mêmes équipements. La vraie incertitude est donc plus grande que celle affichée : prenez ces comparaisons comme des **tendances**, pas comme des classements définitifs.",
            "Le score d'équipement résume 6 besoins du visiteur (dormir, manger, se garer, se déplacer, s'informer, se soigner) à partir des **temps de trajet IGN**.",
        ],
        "pour_les_curieux": [
            {"terme": "Test de Kruskal-Wallis", "valeur": f"H = {_fr(kw_e['H'], 1)} · p = {_fr(kw_e['p'], 3)}" if kw_e else "n.d.", "explication": "Compare plusieurs groupes sans supposer que les données suivent une courbe en cloche (adapté aux petits échantillons et aux valeurs extrêmes)."},
            {"terme": "Taille de l'effet (epsilon²)", "valeur": _fr(kw_e["epsilon2"], 3) if kw_e else "n.d.", "explication": "Mesure l'ampleur de l'écart, indépendamment de la taille de l'échantillon : < 0,06 faible, 0,06-0,14 moyen, > 0,14 important."},
            {"terme": "Temps pour dormir : test", "valeur": f"p = {_fr(kw_t['p'], 3)}" if kw_t else "n.d.", "explication": "Même test appliqué au temps de trajet jusqu'à l'hébergement le plus proche."},
            {"terme": "Fourchette (bootstrap)", "valeur": "95 %", "explication": "Obtenue en « rejouant » 1 000 fois l'échantillon par tirage avec remise ; on garde la zone où tombent 95 % des résultats."},
        ],
        "_synthese": {"kw": kw_e},
    }


def chapitre_equipements(P: dict) -> dict:
    L = P["lieux"]
    lignes = []
    for cat in ["hebergement", "restaurant", "parking", "arret_bus", "distributeur", "activite", "office_tourisme", "police", "gare", "hopital", "aeroport"]:
        comptes = [x["st"][cat]["nombre_1000m"] for x in L if cat in x["st"] and x["st"][cat].get("nombre_1000m") is not None]
        t = [m for m in (_minutes(x["mes"][cat]["t_voiture"]) for x in L if cat in x["mes"]) if m is not None]
        if not comptes:
            continue
        k = sum(1 for c in comptes if c > 0)
        lignes.append({"libelle": LIBELLES_CATEGORIES[cat], "cat": cat, "pct": k / len(comptes), "ic": S.intervalle_wilson(k, len(comptes)),
                       "med_nb": S.mediane(comptes), "moy_nb": S.moyenne(comptes), "t_med": S.mediane(t), "t_p90": S.quantile(t, 0.9)})
    if not lignes:
        return {"id": "equipements", "numero": 6, "titre": "L'offre autour des lieux", "question": "", "en_bref": "Données d'équipement indisponibles.", "chiffres": [], "graphiques": [], "tableaux": [], "lecture": [], "attention": [], "pour_les_curieux": [], "_synthese": {"manques": []}}
    heb = next((r for r in lignes if r["cat"] == "hebergement"), None)
    ratio, g = P.get("ratio") or {}, P.get("guides") or {}
    manques = sorted(lignes, key=lambda r: r["pct"])[:3]
    frequents = sorted(lignes, key=lambda r: -r["pct"])[:3]
    return {
        "id": "equipements", "numero": 6, "titre": "L'offre autour des lieux",
        "question": "Un visiteur trouve-t-il de quoi dormir, manger, se garer… près des lieux de tournage ?",
        "en_bref": (f"À moins de 1 km d'un lieu de tournage, on trouve le plus souvent : {', '.join(r['libelle'].lower() + ' (' + _pct(r['pct']) + ' des lieux)' for r in frequents)}. "
                    f"Les équipements les plus rares sont : {', '.join(r['libelle'].lower() + ' (' + _pct(r['pct']) + ')' for r in manques)}."),
        "chiffres": ([{"valeur": _pct(heb["pct"]), "label": "des lieux ont un hébergement à moins de 1 km", "aide": f"Fourchette plausible : {_fourchette_pct(heb['ic'])}."},
                      {"valeur": f"{_fr(heb['med_nb'], 0)} vs {_fr(heb['moy_nb'], 1)}", "label": "hébergements à 1 km : médiane vs moyenne", "aide": "La moyenne est tirée par quelques lieux de centre-ville très équipés."}] if heb else [])
                   + ([{"valeur": f"×{_fr(ratio['mediane'], 2)}", "label": "distance réelle ÷ vol d'oiseau (médiane)", "aide": f"Calculé sur {_fr(ratio['n'], 0)} trajets IGN : la route est en général {_fr(100 * (ratio['mediane'] - 1), 0)} % plus longue que la ligne droite."}] if ratio.get("mediane") else [])
                   + ([{"valeur": f"{g['lieux_couverts']} / {len(L)}", "label": "lieux avec au moins un guide", "aide": f"{g['guides']} guide(s) actif(s) rattaché(s) à des œuvres."}] if g and g.get("guides") else []),
        "graphiques": [{"id": "graphe-couverture", "type": "barres_h", "titre": "Part des lieux ayant au moins un équipement à moins de 1 km (%)",
                        "etiquettes": [r["libelle"] for r in lignes], "valeurs": [round(100 * r["pct"], 1) for r in lignes],
                        "bas": [round(100 * r["ic"][0], 1) for r in lignes], "haut": [round(100 * r["ic"][1], 1) for r in lignes]}],
        "tableaux": [{"titre": "Équipements autour des lieux", "colonnes": ["Équipement", "Lieux avec ≥ 1 à 1 km", "Médiane à 1 km", "Moyenne à 1 km", "Temps médian en voiture (min)", "9 lieux sur 10 en moins de (min)"],
                      "lignes": [[r["libelle"], f"{_pct(r['pct'])} ({_fourchette_pct(r['ic'])})", _fr(r["med_nb"], 0), _fr(r["moy_nb"], 1), _fr(r["t_med"], 0), _fr(r["t_p90"], 0)] for r in lignes]}],
        "lecture": [
            "**Moyenne ou médiane ?** Quand la moyenne est très supérieure à la médiane, quelques lieux très équipés (centres-villes) « gonflent » le chiffre : le lieu typique est moins bien doté que la moyenne ne le laisse croire.",
            (f"**Pourquoi mesurer par la route ?** À vol d'oiseau, un équipement paraît toujours plus proche qu'il ne l'est vraiment : montagnes, rivières et routes sinueuses allongent le trajet. "
             f"Ici, la distance routière médiane vaut {_fr(ratio['mediane'], 2)} fois la ligne droite, et dans 1 cas sur 10 elle dépasse {_fr(ratio['p90'], 1)} fois. Toutes les analyses de cette page utilisent les trajets IGN.") if ratio.get("mediane") else "",
        ],
        "attention": ["Les équipements viennent de sources ouvertes (OpenStreetMap, DATAtourisme) : un équipement absent de ces bases n'apparaît pas ici. Le nombre à 1 km est un comptage à vol d'oiseau (zone circulaire) ; les temps de trajet, eux, sont routiers."],
        "pour_les_curieux": [{"terme": "Intervalle de Wilson", "valeur": "95 %", "explication": "Fourchette plausible d'une proportion observée sur un échantillon. Contrairement à la formule classique, elle reste fiable quand l'échantillon est petit ou que la proportion est proche de 0 % ou 100 %."}],
        "_synthese": {"manques": [r["libelle"] for r in manques]},
    }


def chapitre_accessibilite(P: dict) -> dict:
    L = P["lieux"]
    def serie(cat):
        return [m for m in (_minutes(x["mes"][cat]["t_voiture"]) for x in L if cat in x["mes"]) if m is not None]
    blocs = []
    for cat, nom in (("hebergement", "un hébergement"), ("gare", "une gare"), ("hopital", "un hôpital"), ("aeroport", "un aéroport")):
        t = serie(cat)
        if t:
            blocs.append({"cat": cat, "nom": nom, "med": S.mediane(t), "p90": S.quantile(t, 0.9),
                          "moins15": sum(1 for v in t if v <= 15) / len(t), "moins30": sum(1 for v in t if v <= 30) / len(t)})
    tranches = [(0, 5, "≤ 5 min"), (5, 10, "5-10"), (10, 20, "10-20"), (20, 30, "20-30"), (30, 10 ** 9, "> 30")]
    hist = {b["cat"]: [sum(1 for v in serie(b["cat"]) if lo < v <= hi or (lo == 0 and v == 0)) for lo, hi, _ in tranches] for b in blocs}
    isoles = [x for x in L if (x["besoins"]["dormir"]["minutes"] or 99) > 20]
    par = {b["cat"]: b for b in blocs}
    resume = ("Données d'accessibilité insuffisantes." if not blocs else
              "Depuis un lieu de tournage typique, il faut " + ", ".join(
                  f"{_fr(par[c]['med'], 0)} min en voiture jusqu'à {par[c]['nom']}" for c in ("hebergement", "gare", "hopital") if c in par)
              + f". {len(isoles)} lieux ({_pct(len(isoles) / len(L))}) sont à plus de 20 minutes de tout hébergement.")
    return {
        "id": "accessibilite", "numero": 7, "titre": "Peut-on y accéder facilement ?",
        "question": "En combien de temps rejoint-on les services essentiels depuis un lieu de tournage ?",
        "en_bref": resume,
        "chiffres": [{"valeur": f"{_fr(b['med'], 0)} min", "label": f"jusqu'à {b['nom']} (médiane)", "aide": f"9 lieux sur 10 sont à moins de {_fr(b['p90'], 0)} min."} for b in blocs],
        "graphiques": [{"id": "graphe-acces", "type": "histogramme_groupe", "titre": "Combien de lieux, selon le temps de trajet en voiture ?",
                        "etiquettes": [t for _, _, t in tranches], "series": [{"nom": b["nom"], "valeurs": hist[b["cat"]]} for b in blocs]}] if blocs else [],
        "tableaux": [{"titre": "Temps de trajet en voiture (itinéraires IGN)", "colonnes": ["Vers…", "Médiane (min)", "9 lieux sur 10 en moins de (min)", "Lieux à ≤ 15 min", "Lieux à ≤ 30 min"],
                      "lignes": [[b["nom"], _fr(b["med"], 0), _fr(b["p90"], 0), _pct(b["moins15"]), _pct(b["moins30"])] for b in blocs]}] if blocs else [],
        "lecture": ["**Le « 9 lieux sur 10 » :** c'est le 90ᵉ percentile. Il décrit les situations les plus défavorables sans se laisser fausser par les 1 ou 2 lieux les plus isolés.",
                    "Un hôpital ou une gare éloignés ne rendent pas un lieu « inaccessible » : mais pour l'accueil de publics fragiles (seniors, familles, personnes à mobilité réduite), ces temps comptent."],
        "attention": ["Temps de trajet **en voiture, hors embouteillages**. Les lieux dont aucun équipement n'a été trouvé dans le rayon de recherche sont exclus du calcul du temps médian : ils sont donc probablement encore plus isolés que ce que la médiane indique."],
        "pour_les_curieux": [{"terme": "Percentile (90ᵉ)", "valeur": "P90", "explication": "Valeur sous laquelle se trouvent 90 % des observations. Le 50ᵉ percentile est la médiane."}],
        "_synthese": {"isoles": len(isoles), "part_isoles": len(isoles) / len(L)},
    }


def _classer(L: list, seuil_facteur: float = 1.0, poids_notoriete: float = 1.0) -> list[dict]:
    films = {l["film_id"]: float(l["popularite"]) for l in L if l.get("popularite") is not None}
    ids = list(films)
    rangs_pct = dict(zip(ids, S.rang_percentile([films[i] for i in ids])))
    out = []
    for x in L:
        if x["film_id"] not in rangs_pct:
            continue
        equip = x["equipement"] if seuil_facteur == 1.0 else 100.0 * sum(_score_besoin(alts, x["mes"], seuil_facteur)[0] for _, _, alts in BESOINS) / len(BESOINS)
        notor = rangs_pct[x["film_id"]]
        out.append({"lieu": x, "notoriete": notor, "equipement": equip, "priorite": (notor / 100.0) ** poids_notoriete * (1 - equip / 100.0)})
    return out


def chapitre_priorites(P: dict) -> dict:
    L = P["lieux"]
    base = _classer(L)
    vide = {"id": "priorites", "numero": 8, "titre": "Où agir en priorité ?", "question": "", "en_bref": "Pas assez de données pour établir des priorités.",
            "chiffres": [], "graphiques": [], "tableaux": [], "lecture": [], "attention": [], "pour_les_curieux": [], "_synthese": {"total": 0}}
    if len(base) < 5:
        return vide
    for c in base:
        haut_n, bien_eq = c["notoriete"] >= 50.0, c["equipement"] >= SEUIL_BIEN_EQUIPE
        c["quadrant"] = "etoiles" if haut_n and bien_eq else "a_renforcer" if haut_n else "atouts_discrets" if bien_eq else "a_surveiller"
    top = sorted(base, key=lambda c: -c["priorite"])
    top10 = [c["lieu"]["id"] for c in top[:10]]
    variantes = {"seuils plus stricts (-25 %)": _classer(L, 0.75), "seuils plus souples (+50 %)": _classer(L, 1.5), "notoriété moins pesante": _classer(L, 1.0, 0.5)}
    jacc = {k: S.jaccard(top10, [c["lieu"]["id"] for c in sorted(v, key=lambda c: -c["priorite"])[:10]]) for k, v in variantes.items()}
    stabilite = S.moyenne(list(jacc.values()))
    sp = S.spearman([c["notoriete"] for c in base], [c["equipement"] for c in base])
    cptq = Counter(c["quadrant"] for c in base)
    par_dept = Counter(c["lieu"]["dept_nom"] for c in base if c["quadrant"] == "a_renforcer")
    manquants = Counter()
    for c in top[:10]:
        for cle, nom, _ in BESOINS:
            if c["lieu"]["besoins"][cle]["score"] < 1.0:
                manquants[nom] += 1
    return {
        "id": "priorites", "numero": 8, "titre": "Où agir en priorité ?",
        "question": "Quels lieux, déjà célèbres, méritent d'être mieux équipés pour accueillir les visiteurs ?",
        "en_bref": (f"{cptq['a_renforcer']} lieux sur {len(base)} cumulent une forte notoriété (film populaire) et un équipement insuffisant : ce sont les priorités d'investissement. "
                    f"La liste de tête est {'très stable' if stabilite >= 0.7 else 'assez stable' if stabilite >= 0.5 else 'sensible aux hypothèses'} "
                    f"(en moyenne {_fr(10 * stabilite, 0)} lieux sur 10 restent dans le top 10 quand on change la méthode)."),
        "chiffres": [
            {"valeur": cptq["a_renforcer"], "label": "lieux « à renforcer »", "aide": "Œuvre populaire + équipement < 66/100."},
            {"valeur": cptq["etoiles"], "label": "lieux « vitrines »", "aide": "Œuvre populaire + bien équipé : à valoriser tels quels."},
            {"valeur": cptq["atouts_discrets"], "label": "« atouts discrets »", "aide": "Bien équipé mais œuvre peu connue : potentiel de communication."},
            {"valeur": _fr(sp["rho"], 2) if sp else "n.d.", "label": "lien notoriété ↔ équipement",
             "aide": (f"Corrélation de Spearman, p = {_fr(sp['p'], 3)} : " + ("les lieux célèbres ne sont pas mieux équipés que les autres." if abs(sp['rho']) < 0.2 else "il existe un lien entre notoriété et équipement.")) if sp else ""},
        ],
        "graphiques": [{"id": "graphe-quadrant", "type": "nuage", "titre": "Notoriété de l'œuvre × équipement du lieu",
                        "points": [{"x": round(c["equipement"], 1), "y": round(c["notoriete"], 1), "nom": c["lieu"]["nom"], "q": c["quadrant"]} for c in base]}],
        "tableaux": [{"titre": "Les 10 lieux à équiper en priorité", "colonnes": ["#", "Lieu", "Commune", "Département", "Œuvre", "Notoriété (0-100)", "Équipement (/100)", "Besoins non couverts"],
                      "lignes": [[i + 1, c["lieu"]["nom"], c["lieu"]["commune"], c["lieu"]["dept_nom"], c["lieu"]["titre"], _fr(c["notoriete"], 0), _fr(c["equipement"], 0),
                                  ", ".join(nom for cle, nom, _ in BESOINS if c["lieu"]["besoins"][cle]["score"] < 1.0) or "aucun"] for i, c in enumerate(top[:10])]},
                     {"titre": "Lieux « à renforcer » par département", "colonnes": ["Département", "Lieux à renforcer"], "lignes": [[d, n] for d, n in par_dept.most_common()]}],
        "lecture": [
            "**Comment est calculé le score d'équipement ?** Pour chacun des 6 besoins d'un visiteur (dormir, manger, se garer, se déplacer, s'informer, se soigner), on regarde le temps de trajet IGN jusqu'à l'équipement le plus proche : "
            "plein score si c'est « confortable » (ex. : 10 min en voiture pour dormir), zéro si c'est deux fois trop long. Le score est la moyenne des 6 besoins.",
            f"**Besoins les plus souvent manquants parmi les 10 lieux prioritaires :** {', '.join(f'{k} ({v})' for k, v in manquants.most_common(3)) or 'aucun'}.",
            f"**Vérification de solidité :** nous avons refait le classement avec des seuils plus stricts, plus souples, et en pesant moins la notoriété. Résultat : {', '.join(f'{k} → {int(round(10 * v))} lieux communs sur 10' for k, v in jacc.items())}.",
        ],
        "attention": ["Ce classement **aide à décider**, il ne décide pas : il ne mesure ni la fréquentation réelle, ni la capacité d'accueil, ni les contraintes de protection des sites. La notoriété est celle de l'œuvre (TMDB), pas celle du lieu."],
        "pour_les_curieux": [
            {"terme": "Corrélation de Spearman", "valeur": f"ρ = {_fr(sp['rho'], 2)} · p = {_fr(sp['p'], 3)}" if sp else "n.d.", "explication": "Mesure si deux grandeurs évoluent ensemble (de -1 à +1), sans supposer de lien en ligne droite. 0 = aucun lien."},
            {"terme": "Indice de Jaccard", "valeur": _fr(stabilite, 2), "explication": "Recouvrement entre deux listes (0 = rien en commun, 1 = identiques). Sert ici à vérifier que le top 10 ne dépend pas d'un choix arbitraire."},
            {"terme": "Rang percentile", "valeur": "0-100", "explication": "Position d'une œuvre parmi toutes les autres : 90 = plus populaire que 90 % d'entre elles. Évite que quelques méga-succès écrasent l'échelle."},
        ],
        "_synthese": {"a_renforcer": cptq["a_renforcer"], "total": len(base), "top3_dept": par_dept.most_common(3)},
    }


def chapitre_fiabilite(P: dict) -> dict:
    L = P["lieux"]
    n = len(L)
    def pct(f): return sum(1 for x in L if f(x)) / n
    avec_mes = pct(lambda x: bool(x["mes"]))
    dedu = sum(1 for x in L if x["dept_deduit"])
    sans_dep = sum(1 for x in L if not x["dept_code"])
    n_eff = len({x["commune"] for x in L})
    glossaire = [
        ("Médiane", "La valeur du milieu : la moitié des lieux est en dessous, l'autre moitié au-dessus. Plus représentative du cas « typique » que la moyenne quand quelques cas sont extrêmes."),
        ("Moyenne", "Somme divisée par le nombre de cas. Sensible aux valeurs extrêmes."),
        ("Percentile (P90)", "Valeur sous laquelle se trouvent 90 % des cas. Décrit les situations défavorables sans être faussé par les 1 ou 2 pires."),
        ("Fourchette plausible (intervalle de confiance à 95 %)", "Zone dans laquelle se situe très probablement la vraie valeur, compte tenu du nombre de cas observés. Plus l'échantillon est petit, plus elle est large."),
        ("Indice de Gini", "Mesure d'inégalité de répartition, de 0 (égalité) à 1 (tout au même endroit)."),
        ("p-value", "Probabilité d'observer un écart au moins aussi grand par pur hasard. En dessous de 0,05, on considère l'écart probablement réel. Ce n'est pas la probabilité que l'hypothèse soit vraie."),
        ("Taille d'effet", "Ampleur d'un écart, indépendamment du nombre de cas. Un écart peut être « significatif » mais minuscule."),
        ("Corrélation", "Deux grandeurs qui varient ensemble. Corrélation n'est pas causalité."),
        ("Isochrone", "Zone atteignable en un temps donné (ex. 15 minutes en voiture) depuis un point, calculée par l'IGN."),
    ]
    return {
        "id": "fiabilite", "numero": 9, "titre": "Fiabilité des données et méthode",
        "question": "Peut-on se fier à ces chiffres ? Quelles sont leurs limites ?",
        "en_bref": (f"L'analyse s'appuie sur {n} lieux dont {_pct(avec_mes)} disposent de trajets IGN précalculés. "
                    f"Les comparaisons fines entre départements restent fragiles : les lieux se concentrent dans {n_eff} communes seulement."),
        "chiffres": [
            {"valeur": n, "label": "lieux analysés", "aide": None},
            {"valeur": _pct(avec_mes), "label": "avec trajets IGN précalculés", "aide": "Toutes les distances de cette page sont routières."},
            {"valeur": _pct(pct(lambda x: bool(x.get("photo_url")))), "label": "avec photo", "aide": None},
            {"valeur": _pct(pct(lambda x: bool(x.get("a_anecdote")))), "label": "avec anecdote", "aide": "Contenu éditorial encore à enrichir."},
            {"valeur": sans_dep + dedu, "label": "lieux sans département renseigné", "aide": f"{dedu} rattaché(s) automatiquement par leur position, {sans_dep} non localisé(s)."},
        ],
        "graphiques": [], "tableaux": [],
        "lecture": [
            "**Ce qui rend l'analyse solide :** distances routières officielles (IGN) plutôt qu'à vol d'oiseau ; médianes plutôt que moyennes ; fourchettes plausibles systématiques ; tests statistiques adaptés aux petits échantillons ; classement de priorité vérifié sous plusieurs hypothèses.",
            "**Ce qui reste à améliorer :** rapporter les équipements à la population et à la fréquentation (données INSEE et offices de tourisme), intégrer la saisonnalité, et élargir la base d'œuvres pour des comparaisons départementales plus robustes.",
        ],
        "attention": [
            f"**Petits nombres :** {n} lieux répartis sur 13 départements, dont plusieurs à moins de 10 lieux. Un seul lieu en plus ou en moins peut changer un pourcentage.",
            "**Biais de recensement :** la base reflète ce que les sources ouvertes (Wikidata, TMDB, DATAtourisme, OpenStreetMap) savent. Un territoire sous-documenté paraît moins riche qu'il ne l'est.",
            "**Corrélation n'est pas causalité** : un lien statistique ne prouve pas qu'améliorer un équipement augmentera la fréquentation.",
        ],
        "pour_les_curieux": [{"terme": t, "valeur": "", "explication": d} for t, d in glossaire],
        "_synthese": {},
    }


def _messages_cles(ch: dict) -> list[dict]:
    rep, eq, pri, acc, dep = (ch[k]["_synthese"] for k in ("repartition", "equipements", "priorites", "accessibilite", "departements"))
    msgs = [{"chapitre": 2, "titre": "Une activité très concentrée",
             "texte": f"{rep['premier']} concentre {_pct(rep['part_premier'])} des lieux référencés ; les 3 premiers départements en regroupent {_pct(rep['top3'])}."}]
    if dep.get("kw"):
        msgs.append({"chapitre": 5, "titre": "Des écarts d'équipement entre départements",
                     "texte": f"Écart {_lecture_effet(dep['kw']['epsilon2'])} entre départements : {_lecture_p(dep['kw']['p'])}."})
    if eq.get("manques"):
        msgs.append({"chapitre": 6, "titre": "Des services rares autour des lieux", "texte": f"Les équipements les plus rares à moins de 1 km : {', '.join(m.lower() for m in eq['manques'])}."})
    msgs.append({"chapitre": 7, "titre": "Des lieux isolés", "texte": f"{acc['isoles']} lieux ({_pct(acc['part_isoles'])}) sont à plus de 20 minutes de tout hébergement."})
    if pri.get("total"):
        msgs.append({"chapitre": 8, "titre": "Où investir en premier",
                     "texte": f"{pri['a_renforcer']} lieux associent une œuvre populaire à un équipement insuffisant"
                              + (f", surtout en {', '.join(d for d, _ in pri['top3_dept'])}." if pri.get("top3_dept") else ".")})
    return msgs


def construire(donnees: dict, region: str = "Occitanie") -> dict:
    P = preparer(donnees)
    if not P["lieux"]:
        raise ValueError("Aucun lieu de tournage publié.")
    ch = {"apercu": chapitre_apercu(P), "repartition": chapitre_repartition(P), "temps": chapitre_temps(P),
          "oeuvres": chapitre_oeuvres(P), "departements": chapitre_departements(P), "equipements": chapitre_equipements(P),
          "accessibilite": chapitre_accessibilite(P), "priorites": chapitre_priorites(P), "fiabilite": chapitre_fiabilite(P)}
    messages = _messages_cles(ch)
    chapitres = [{k: v for k, v in c.items() if k != "_synthese"} for c in ch.values()]
    return {"region": region, "genere_le": datetime.now().isoformat(timespec="seconds"),
            "perimetre": {"lieux": len(P["lieux"]), "oeuvres": len({l["film_id"] for l in P["lieux"]})},
            "messages_cles": messages, "chapitres": chapitres}


@router.get("/api/analyse/territoire")
async def api_analyse_territoire(region: str = Query("Occitanie")):
    maintenant = time.monotonic()
    if _CACHE["data"] is not None and _CACHE["region"] == region and maintenant - _CACHE["t"] < TTL_S:
        return _CACHE["data"]
    try:
        resultat = construire(await charger_donnees(), region)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    except Exception as exc:
        logger.exception("Analyse territoriale indisponible")
        raise HTTPException(500, f"Analyse indisponible : {exc}")
    _CACHE.update(t=maintenant, region=region, data=resultat)
    return resultat