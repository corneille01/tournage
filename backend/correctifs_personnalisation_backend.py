"""
correctifs_personnalisation_backend.py — Étape 1 : fiabiliser les suggestions.

À intégrer dans backend/main.py. Les fonctions [1] à [4] remplacent / complètent
_profil_score_offre et _phrase_recommandation_offre (aux alentours de la ligne 1425).
Les repères [A] à [E] (en bas) décrivent les modifications à faire dans
parcours_enrichi().

NON exécuté contre votre base : testé uniquement sur des données factices.
"""


# ══════════════════════════════════════════════════════════════
# [1] Petits utilitaires (à placer au-dessus de _profil_score_offre)
# ══════════════════════════════════════════════════════════════

def _num(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _hhmm_vers_min(hhmm):
    try:
        h, m = str(hhmm).split(":")
        return int(h) * 60 + int(m)
    except (TypeError, ValueError):
        return None


_ACCESS_NEGATIF = ("non accessible", "inaccessible", "non adapté", "non pmr", "pas accessible")
_ACCESS_POSITIF = ("pmr", "accessible", "accessibilité", "handicap", "fauteuil")


def _statut_accessibilite(item):
    """'oui' | 'non' | 'inconnu'.

    Corrige l'ancien test `"accessible" in texte`, qui validait aussi
    « non accessible ». Les indices POSITIFS ne sont cherchés que dans des champs
    structurés (équipements, labels, lien d'accessibilité) : la description libre
    contient trop de faux positifs (« accessible en voiture »…). Les indices NÉGATIFS
    sont cherchés partout.
    """
    structure = " ".join(str(item.get(k) or "") for k in ("equipements", "labels_qualite")).lower()
    tout = structure + " " + str(item.get("description") or "").lower()
    if any(x in tout for x in _ACCESS_NEGATIF):
        return "non"
    if item.get("lien_accessibilite") or any(x in structure for x in _ACCESS_POSITIF):
        return "oui"
    return "inconnu"


# ══════════════════════════════════════════════════════════════
# [2] Scoring v2 : renvoie (score, raisons) — les raisons affichées sont
#     exactement celles qui ont fait monter le score (plus de phrase générique).
# ══════════════════════════════════════════════════════════════

def _evaluer_offre(item, categorie, budget_level="equilibre", accessibilite=False,
                   mode="driving-car", heure_min=None, est_premiere=False,
                   est_derniere=False, langue=None):
    """Score déterministe + liste de raisons lisibles.

    heure_min : minute de la journée où l'on quitte l'étape (ex. 750 = 12h30).
    Les bonus de contexte (repas, hébergement, parking, gare…) ne s'appliquent
    que si l'appelant fournit heure_min / est_premiere / est_derniere.
    """
    score, raisons = 0.0, []
    a_pied = mode == "foot-walking"

    # 1. Proximité selon le mode réel (durée IGN si présente, sinon vol d'oiseau)
    duree_s = _num(item.get("duree_pied_secondes" if a_pied else "duree_voiture_secondes"))
    if duree_s is not None:
        minutes = duree_s / 60.0
    else:
        dist = _num(item.get("meilleure_distance_metres"))
        if dist is None:
            dist = _num(item.get("distance_metres"))
        if dist is None:
            dist = 999999.0
        minutes = dist / (65.0 if a_pied else 500.0)
    score += max(0.0, 38.0 - minutes * (2.0 if a_pied else 4.0))
    raisons.append(f"à {max(1, round(minutes))} min {'à pied' if a_pied else 'en voiture'}")

    # 2. Classement (étoiles) — n'est affiché que pour l'hébergement
    note = _num(item.get("note_etoiles"))
    if note is not None:
        score += min(25.0, max(0.0, note * 5.0))
        if categorie == "hebergement":
            raisons.append(f"classé {note:g} étoile(s)")

    # 3. Budget
    tarif = _num(item.get("tarif_min"))
    if tarif is not None:
        if budget_level == "economique":
            score += max(0.0, 28.0 - min(tarif, 100.0) * 0.35)
            raisons.append(f"à partir de {tarif:g} €")
        elif budget_level == "confort":
            score += min(18.0, tarif * 0.18)
        else:
            score += max(0.0, 16.0 - min(tarif, 100.0) * 0.12)

    # 4. Accessibilité : « inconnu » n'est plus traité comme « inaccessible »
    acces = _statut_accessibilite(item)
    if accessibilite:
        if acces == "oui":
            score += 28.0
            raisons.append("informations d'accessibilité disponibles")
        elif acces == "non":
            score -= 60.0
        else:
            score -= 8.0
            raisons.append("accessibilité à vérifier avant de venir")
    elif acces == "oui":
        score += 3.0

    # 5. Langue parlée sur place (champ langues_parlees, texte libre)
    if langue:
        if langue.strip().lower() in str(item.get("langues_parlees") or "").lower():
            score += 6.0
            raisons.append("langue parlée sur place")

    # 6. Contexte de la journée
    if categorie == "restaurant" and heure_min is not None:
        repas = 690 <= heure_min <= 870 or 1110 <= heure_min <= 1290  # 11h30-14h30 / 18h30-21h30
        score += 20.0 if repas else -15.0
        if repas:
            raisons.append("bien placé pour votre pause repas")
    elif categorie == "hebergement":
        score += 25.0 if est_derniere else -30.0
        if est_derniere:
            raisons.append("idéal pour finir la journée")
    elif categorie == "parking":
        score += -20.0 if a_pied else 12.0
        if not a_pied:
            raisons.append("pratique pour se garer")
    elif categorie in ("gare", "aeroport", "arret_bus"):
        score += 10.0 if (est_premiere or est_derniere) else -15.0
    elif categorie == "office_tourisme" and est_premiere:
        score += 10.0
        raisons.append("point d'information pour démarrer")

    # 7. Fiche exploitable
    if item.get("site_web"):
        score += 4.0
    if item.get("telephone"):
        score += 2.0

    return round(score, 2), raisons


def _profil_score_offre(item, categorie, budget_level="equilibre", accessibilite=False,
                        mode="driving-car"):
    """Compatibilité : gardé pour le tri global des catégories (sans contexte d'étape)."""
    return _evaluer_offre(item, categorie, budget_level, accessibilite, mode)[0]


# ══════════════════════════════════════════════════════════════
# [3] Recommandations par étape, calculées APRÈS le planning horaire
# ══════════════════════════════════════════════════════════════

def _recommandations_par_etape(etapes, par_etape, categories_retenues, planning_horaire,
                               mode, budget_level, accessibilite, langue=None,
                               max_par_etape=3, seuil=25.0):
    """Au plus `max_par_etape` propositions par étape, classées par score.

    Différences avec l'ancienne version :
      - une catégorie non pertinente n'est plus proposée par principe
        (score < seuil => rien, ex. un hôtel à la première étape) ;
      - un même établissement n'est jamais proposé sur deux étapes ;
      - l'heure de fin de visite de l'étape sert à choisir le bon moment
        pour un restaurant ;
      - la raison affichée est construite à partir des critères réellement
        appliqués.
    """
    fins = {}
    for ligne in planning_horaire:
        if ligne.get("type") == "lieu":
            fins[int(ligne["lieu_id"])] = _hhmm_vers_min(ligne.get("heure_fin_visite"))

    deja = set()
    resultat = {}
    n = len(etapes)
    for i, etape in enumerate(etapes):
        eid = int(etape["id"])
        propositions = []
        for categorie in categories_retenues:
            evalues = []
            for cand in par_etape.get(str(eid), []):
                if cand.get("categorie") != categorie:
                    continue
                try:
                    cle = (categorie, (cand.get("nom") or "").strip().lower(),
                           round(float(cand["latitude"]), 5), round(float(cand["longitude"]), 5))
                except (TypeError, ValueError, KeyError):
                    continue
                if cle in deja:
                    continue
                score, raisons = _evaluer_offre(
                    cand, categorie, budget_level, accessibilite, mode,
                    heure_min=fins.get(eid), est_premiere=(i == 0),
                    est_derniere=(i == n - 1), langue=langue,
                )
                evalues.append((score, raisons, cle, cand))
            if not evalues:
                continue
            meilleur = max(evalues, key=lambda t: t[0])
            if meilleur[0] >= seuil:
                propositions.append(meilleur)

        propositions.sort(key=lambda t: t[0], reverse=True)
        recs = []
        for score, raisons, cle, cand in propositions[:max_par_etape]:
            deja.add(cle)
            rec = dict(cand)
            rec["score_personnalise"] = score
            rec["raisons"] = raisons
            texte = ", ".join(raisons)
            rec["raison"] = (texte[:1].upper() + texte[1:] + ".") if texte else "Correspond à vos critères."
            rec["action_url"] = rec.get("site_web") or None
            rec["action_label"] = ("Voir / réserver" if rec.get("site_web")
                                   else ("Appeler" if rec.get("telephone") else None))
            recs.append(rec)
        resultat[str(eid)] = recs
    return resultat


# ══════════════════════════════════════════════════════════════
# [4] Budget basé sur les suggestions RETENUES (et non sur le moins cher
#     de chaque catégorie, qui contredisait le niveau « confort »)
# ══════════════════════════════════════════════════════════════

def _budget_depuis_recommandations(recos_par_etape, nb_personnes=None):
    """Somme des tarifs minimum renseignés des suggestions restaurant / activité /
    hébergement. Hypothèse : restaurant et activité = prix par personne,
    hébergement = prix par chambre. Aucun tarif absent n'est inventé."""
    personnes = max(1, int(nb_personnes or 1))
    items, total, vus = [], 0.0, set()
    for recs in recos_par_etape.values():
        for r in recs:
            cat = r.get("categorie")
            if cat not in ("restaurant", "activite", "hebergement"):
                continue
            tarif = _num(r.get("tarif_min"))
            if tarif is None:
                continue
            cle = (cat, (r.get("nom") or "").strip().lower())
            if cle in vus:
                continue
            vus.add(cle)
            quantite = 1 if cat == "hebergement" else personnes
            items.append({"categorie": cat, "nom": r.get("nom"), "tarif_min": tarif,
                          "quantite": quantite, "devise": r.get("devise") or "EUR"})
            total += tarif * quantite
    return items, (round(total, 2) if items else None)


# ══════════════════════════════════════════════════════════════
# MODIFICATIONS À FAIRE DANS parcours_enrichi()
# ══════════════════════════════════════════════════════════════
#
# [A] Requête amenity_cache : ajouter les colonnes qui manquaient. Sans elles,
#     note_etoiles / equipements / labels_qualite / lien_accessibilite /
#     description valaient toujours None : les notes et l'accessibilité
#     n'influençaient AUCUN score, et x.description n'était jamais affichée
#     côté front.
#
#         SELECT lieu_tournage_id, categorie, nom, latitude, longitude,
#                distance_metres, adresse, telephone, site_web, email,
#                horaires, photo_url, tarif_min, tarif_max, devise,
#                capacite, description, equipements, note_etoiles,
#                labels_qualite, lien_accessibilite, langues_parlees,
#                distance_pied_metres, duree_pied_secondes,
#                distance_voiture_metres, duree_voiture_secondes
#         FROM amenity_cache
#         WHERE lieu_tournage_id IN ({placeholders})
#         ORDER BY lieu_tournage_id, categorie, distance_metres ASC
#
#     (Tronquer `description` à ~240 caractères avant de l'envoyer au navigateur.)
#
# [B] Tri global des catégories : passer le mode.
#         item["score_personnalise"] = _profil_score_offre(
#             item, categorie, budget_level, accessibilite, mode)
#
# [C] SUPPRIMER l'ancien bloc « Recommandations individualisées »
#     (de `recommandations_par_etape = {}` jusqu'à la fin de la boucle
#     `for etape in etapes`) et garder une copie complète avant la troncature :
#
#         par_etape_complet = {k: list(v) for k, v in par_etape.items()}
#         for cle, items in par_etape.items():      # troncature existante
#             par_etape[cle] = items[:limite]
#
# [D] Planning horaire : décalage d'un tronçon quand il n'y a PAS de point de
#     départ. Sans départ, `trajets` a n-1 tronçons (étape i -> i+1), alors que
#     le code lisait troncons[i] pour l'étape i : l'heure d'arrivée à l'étape 1
#     incluait le trajet 1->2 et la dernière étape n'avait aucun trajet.
#
#         minute_courante = _minutes_hhmm(heure_depart)
#         minute_depart = minute_courante                       # <- AJOUT
#         decalage = 0 if depart_effectif else 1                # <- AJOUT
#         ...
#         for i, etape in enumerate(etapes):
#             idx = i - decalage                                # <- AJOUT
#             trajet = troncons[idx] if 0 <= idx < len(troncons) else None
#
# [E] Après la boucle du planning (remplace le calcul de duree_totale_estimee,
#     l'ancien budget_items / budget_estime_euros / budget_max_respecte) :
#
#         retour_s = 0
#         if retour_depart and depart_effectif and troncons:
#             retour_s = int(troncons[-1].get("duree_secondes") or 0)
#         duree_totale_estimee = (minute_courante - minute_depart) * 60 + retour_s
#         # (le planning contient déjà visites, guides et attentes : plus de
#         #  double comptage, ni de guide compté sans étape correspondante)
#
#         recommandations_par_etape = _recommandations_par_etape(
#             etapes, par_etape_complet, list(categories.keys()), planning_horaire,
#             mode, budget_level, accessibilite, langue_guide)
#         budget_items, budget_estime_euros = _budget_depuis_recommandations(
#             recommandations_par_etape, nb_personnes)
#         budget_max_respecte = (None if budget_max_euros is None or budget_estime_euros is None
#                                else budget_estime_euros <= budget_max_euros)
#
#     et ajouter dans le dict `reponse` la clé que le front lit mais que le
#     backend n'envoyait jamais :
#
#         "attente_visites_guidees_minutes": attente_visites_guidees_minutes,