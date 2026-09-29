"""backend/visites_cinetouristiques.py — Visites cinétouristiques documentées.

Depuis la migration_v27, TOUTES ces données viennent de la table
`visites_cinetouristiques` — il n'y a plus une seule valeur en dur dans
ce fichier. Une visite qui change (nouveau créneau, fermeture, tarif)
se corrige en base, sans redéploiement.

Les créneaux restent des données éditoriales datées, saisies par un
humain (via POST/PATCH /api/visites) : Pelify ne doit jamais
transformer un horaire ancien en disponibilité actuelle. Quand aucun
créneau daté n'est connu pour la date demandée, l'interface renvoie
une liste vide et demande une vérification sur la page de réservation.
"""
from __future__ import annotations

from datetime import date

from db import fetch_all


def _jour(d: date) -> int:
    return d.weekday()  # lundi=0


def _parser_json_liste(valeur) -> list:
    """asyncpg renvoie JSONB comme une chaîne brute (pas de codec
    enregistré) — voir main.py::_parser_json pour la même logique."""
    import json
    if not valeur:
        return []
    if isinstance(valeur, str):
        try:
            valeur = json.loads(valeur)
        except (json.JSONDecodeError, TypeError):
            return []
    return valeur if isinstance(valeur, list) else []


async def creneaux_pour_date(date_sortie: str | None, film_ids: list[int] | None = None) -> list[dict]:
    """
    Retourne les créneaux de visites cinétouristiques disponibles pour
    `date_sortie` (ISO "AAAA-MM-JJ"), limités aux visites concernant au
    moins un des `film_ids` si cette liste est fournie et non vide.
    """
    if not date_sortie:
        return []
    try:
        d = date.fromisoformat(str(date_sortie))
    except ValueError:
        return []

    wanted = {int(x) for x in (film_ids or [])}
    if wanted:
        lignes = await fetch_all(
            """
            SELECT slug, film_ids, nom, description, duree_minutes, lien,
                   creneaux, creneaux_regles
            FROM visites_cinetouristiques
            WHERE statut = 'actif' AND film_ids && %s
            """,
            (list(wanted),),
        )
    else:
        lignes = await fetch_all(
            """
            SELECT slug, film_ids, nom, description, duree_minutes, lien,
                   creneaux, creneaux_regles
            FROM visites_cinetouristiques
            WHERE statut = 'actif'
            """
        )

    result = []
    for visite in lignes:
        base = {
            "id": visite["slug"],
            "film_ids": [int(x) for x in (visite.get("film_ids") or [])],
            "nom": visite["nom"],
            "description": visite.get("description"),
            "duree_minutes": visite["duree_minutes"],
            "lien": visite.get("lien"),
        }
        for item in _parser_json_liste(visite.get("creneaux")):
            # item attendu : [date_iso, heure_debut, heure_fin]
            if len(item) == 3 and item[0] == date_sortie:
                result.append({**base, "date": date_sortie, "heure_debut": item[1], "heure_fin": item[2]})
        for regle in _parser_json_liste(visite.get("creneaux_regles")):
            try:
                debut = date.fromisoformat(regle["debut"])
                fin = date.fromisoformat(regle["fin"])
            except (KeyError, ValueError, TypeError):
                continue
            if debut <= d <= fin and _jour(d) in (regle.get("jours") or []):
                result.append({**base, "date": date_sortie, "heure_debut": regle["start"], "heure_fin": regle["end"]})
    return result