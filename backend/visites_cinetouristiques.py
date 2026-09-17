"""Référentiel léger des visites cinétouristiques documentées.

Les créneaux sont volontairement conservés comme données éditoriales datées :
Pelify ne doit jamais transformer un horaire ancien en disponibilité actuelle.
Quand aucun créneau daté n'est connu pour la date demandée, l'interface demande
une vérification sur la page de réservation.
"""
from datetime import date

VISITES = [
    {
        "id": "collioure-cine-balade",
        "film_ids": [15],
        "nom": "Office de tourisme de Collioure — Ciné-balade",
        "description": "Ciné-balade sur les traces des films tournés à Collioure.",
        "duree_minutes": 120,
        "lien": "https://boutique.tourisme-collioure.com/cine-balades/cine-balades",
        "creneaux": [
            ("2026-07-09", "10:15", "12:15"),
            ("2026-07-23", "10:15", "12:15"),
            ("2026-08-13", "10:15", "12:15"),
            ("2026-08-27", "10:15", "12:15"),
        ],
    },
    {
        "id": "sete-dna-pied-mai",
        "film_ids": [128],
        "nom": "Office de tourisme Archipel de Thau — Cinétour DNA à pied",
        "description": "Cinétour pédestre sur les lieux de tournage de Demain nous appartient.",
        "duree_minutes": 120,
        "lien": "https://billetterie.archipel-thau.com/loisirs/visites-guidees-a-pied/cinetour-pedestre-dna-aujourdhui-vous-appartient",
        "creneaux_regles": [{"debut": "2026-05-18", "fin": "2026-05-30", "jours": [0, 2, 5], "start": "16:00", "end": "18:00"}],
    },
    {
        "id": "sete-dna-pied-juin",
        "film_ids": [128],
        "nom": "Office de tourisme Archipel de Thau — Cinétour DNA à pied",
        "description": "Cinétour pédestre sur les lieux de tournage de Demain nous appartient.",
        "duree_minutes": 120,
        "lien": "https://billetterie.archipel-thau.com/loisirs/visites-guidees-a-pied/cinetour-pedestre-dna-aujourdhui-vous-appartient",
        "creneaux_regles": [{"debut": "2026-06-01", "fin": "2026-06-29", "jours": [0, 2, 5], "start": "16:30", "end": "18:30"}],
    },
    {
        "id": "sete-dna-bateau",
        "film_ids": [128],
        "nom": "Office de tourisme Archipel de Thau — Cinétour DNA en bateau",
        "description": "Balade en bateau sur les lieux de tournage de Demain nous appartient.",
        "duree_minutes": 60,
        "lien": "https://billetterie.archipel-thau.com/loisirs/excursions-et-promenades-en-bateau/cinetour-bateau-dna-lequipage-vous-appartient",
        "creneaux_regles": [{"debut": "2026-05-24", "fin": "2026-10-25", "jours": [6], "start": "09:45", "end": "10:45"}],
    },
    {
        "id": "montpellier-usgs-aout",
        "film_ids": [131],
        "nom": "Office de tourisme Montpellier — Au cœur de la série",
        "description": "Visite guidée dans le centre historique autour de Un si grand soleil.",
        "duree_minutes": 120,
        "lien": "https://book.montpellier-tourisme.fr/fr/voir-faire/1999897/au-c%C5%93ur-de-la-s%C3%A9rie-un-si-grand-soleil-centre-historique/afficher-les-details",
        "creneaux": [(f"2026-08-{d:02d}", "09:30", "11:30") for d in (7, 14, 21, 28)],
    },
    {
        "id": "palavas-cinema-2026-09-29",
        "film_ids": [131, 136],
        "nom": "Office de tourisme de Palavas-les-Flots — Le cinéma à Palavas",
        "description": "Visite thématique autour des films et séries tournés à Palavas-les-Flots.",
        "duree_minutes": 120,
        "lien": "https://billetterie.palavas-tourisme.com/fr/produit/le-cinema-a-palavas",
        "creneaux": [("2026-09-29", "10:00", "12:00")],
    },
]


def _jour(d: date) -> int:
    return d.weekday()  # lundi=0


def creneaux_pour_date(date_sortie: str | None, film_ids: list[int] | None = None) -> list[dict]:
    if not date_sortie:
        return []
    try:
        d = date.fromisoformat(str(date_sortie))
    except ValueError:
        return []
    wanted = {int(x) for x in (film_ids or [])}
    result = []
    for visite in VISITES:
        if wanted and not wanted.intersection(visite["film_ids"]):
            continue
        for item in visite.get("creneaux", []):
            if item[0] == date_sortie:
                result.append({**{k: v for k, v in visite.items() if k not in ("creneaux", "creneaux_regles")}, "date": date_sortie, "heure_debut": item[1], "heure_fin": item[2]})
        for regle in visite.get("creneaux_regles", []):
            debut = date.fromisoformat(regle["debut"])
            fin = date.fromisoformat(regle["fin"])
            if debut <= d <= fin and _jour(d) in regle["jours"]:
                result.append({**{k: v for k, v in visite.items() if k not in ("creneaux", "creneaux_regles")}, "date": date_sortie, "heure_debut": regle["start"], "heure_fin": regle["end"]})
    return result
