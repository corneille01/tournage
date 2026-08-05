"""
backend/import_datatourisme_jsonld.py — Importe un export JSON-LD
DATAtourisme (hébergement, restauration OU activité — même structure
pour les trois, un seul script) dans datatourisme_objets. En
streaming (ijson) : ces fichiers font plusieurs centaines de Mo,
impossible de les charger entièrement en mémoire.

Champs extraits (vérifiés sur de vrais exports avant d'écrire ce
script — voir historique du projet) :
  - Nom, Classification  : rdfs:label, @type
  - Adresse/GPS          : isLocatedAt > schema:address / schema:geo
  - Contact              : hasContact > schema:email / schema:telephone / foaf:homepage
  - Horaires             : isLocatedAt > schema:openingHoursSpecification > additionalInformation
  - Tarifs               : schema:offers > schema:priceSpecification (min/max)
  - Cuisine (restaurants): providesCuisineOfType

NB : Description, équipements ("est équipé de") et photos ne se
résolvent à aucune donnée dans les exports testés — ignorés
volontairement (vérifié sur 500+ échantillons, pas un bug de ce
script : la donnée n'est simplement pas renseignée par les offices de
tourisme sources).

Usage :
    python import_datatourisme_jsonld.py --fichier data/hebergements.jsonld --categorie hebergement
    python import_datatourisme_jsonld.py --fichier data/restaurants.jsonld.gz --categorie restaurant
    python import_datatourisme_jsonld.py --fichier data/activites.jsonld --categorie activite
"""

import argparse
import asyncio
import gzip

import ijson

from db import init_db_pool, close_db_pool, execute, executemany

CLASSIFICATIONS_CONNUES = [
    "Hotel", "Camping", "GuestHouse", "BedAndBreakfast", "HolidayVillage",
    "SelfCateringAccommodation", "RentalAccommodation", "Accommodation",
    "Restaurant", "FoodEstablishment", "PlaceOfInterest",
]


import re

# Quelques termes de thésaurus DATAtourisme fréquents, traduits — pour
# le reste, on retombe sur un découpage lisible du CamelCase (mieux que
# rien, même si moins soigné qu'une vraie traduction).
TRADUCTIONS_THESAURUS = {
    "TraditionalCuisine": "Cuisine traditionnelle",
    "VegetarianCuisine": "Cuisine végétarienne",
    "VeganCuisine": "Cuisine végane",
    "RegionalCuisine": "Cuisine régionale",
    "GastronomicCuisine": "Cuisine gastronomique",
    "WorldCuisine": "Cuisine du monde",
    "ItalianCuisine": "Cuisine italienne",
    "SeafoodCuisine": "Cuisine de la mer",
    "GrilledFoodCuisine": "Grillades",
    "FastFoodCuisine": "Restauration rapide",
    # Moyens de paiement
    "Cash": "Espèces",
    "Check": "Chèque",
    "Transfers": "Virement",
    "CreditCard": "Carte bancaire",
    "BankCard": "Carte bancaire",
    "MealVoucher": "Chèque-restaurant",
    "HolidayVoucher": "Chèque-vacances",
    "TravellersCheck": "Chèque de voyage",
    "MobilePayment": "Paiement mobile",
    "Cryptocurrency": "Cryptomonnaie",
}


def _libelle_thesaurus(id_kb: str) -> str:
    """Les références de thésaurus (kb:TraditionalCuisine) n'ont pas de
    libellé embarqué — on traduit les plus courants, sinon on découpe
    le CamelCase en mots lisibles (mieux que l'identifiant brut)."""
    terme = id_kb.split(":")[-1]
    if terme in TRADUCTIONS_THESAURUS:
        return TRADUCTIONS_THESAURUS[terme]
    return re.sub(r"(?<!^)(?=[A-Z])", " ", terme)


def _valeur_langue(champ, langue="fr"):
    """Beaucoup de champs JSON-LD sont soit une valeur directe, soit un
    objet {"@value": ..., "@language": ...}, soit une liste des deux."""
    if champ is None:
        return None
    if isinstance(champ, dict):
        return champ.get("@value")
    if isinstance(champ, list):
        for item in champ:
            if isinstance(item, dict) and item.get("@language") == langue:
                return item.get("@value")
        if champ and isinstance(champ[0], dict):
            return champ[0].get("@value")
    if isinstance(champ, str):
        return champ
    return None


def _texte_simple(valeur):
    """Certains champs sont parfois une liste (variantes multilingues)
    plutôt qu'une chaîne simple — on prend la première valeur
    exploitable dans les deux cas."""
    if isinstance(valeur, list):
        valeur = valeur[0] if valeur else None
    if isinstance(valeur, dict):
        return valeur.get("@value")
    return valeur


def _classification(types: list) -> str | None:
    if not types:
        return None
    for connu in CLASSIFICATIONS_CONNUES:
        if connu in types or f"schema:{connu}" in types:
            return connu
    return types[0] if types else None


def _indexer_ids_locaux(noeud, index: dict, profondeur: int = 0) -> None:
    """
    Parcourt UN SEUL objet POI (pas tout le fichier) pour indexer tous
    les sous-nœuds ayant un contenu réel par leur @id — certains champs
    (comme l'adresse) sont parfois embarqués une fois puis seulement
    référencés ailleurs dans le même objet, pour éviter la duplication.
    """
    if profondeur > 6 or noeud is None:
        return
    if isinstance(noeud, dict):
        id_ = noeud.get("@id")
        if id_ and len(noeud) > 1:
            index.setdefault(id_, noeud)
        for v in noeud.values():
            _indexer_ids_locaux(v, index, profondeur + 1)
    elif isinstance(noeud, list):
        for item in noeud:
            _indexer_ids_locaux(item, index, profondeur + 1)


def _resoudre(noeud, index: dict):
    """Si noeud n'est qu'une référence {"@id": X} sans autre contenu,
    remplace par sa définition complète trouvée dans l'index local."""
    if isinstance(noeud, dict) and set(noeud.keys()) == {"@id"}:
        return index.get(noeud["@id"], noeud)
    return noeud


def _extraire_tarifs(offre) -> tuple:
    """Parcourt toutes les spécifications tarifaires trouvées et
    retourne le min et le max global."""
    if not offre:
        return None, None, None
    if isinstance(offre, list):
        offre = offre[0] if offre else None
    if not isinstance(offre, dict):
        return None, None, None

    specs = offre.get("schema:priceSpecification")
    if not specs:
        return None, None, None
    if isinstance(specs, dict):
        specs = [specs]

    tous_min, tous_max = [], []
    devise = None
    for spec in specs:
        mini = spec.get("schema:minPrice", {}).get("@value") if isinstance(spec.get("schema:minPrice"), dict) else None
        maxi = spec.get("schema:maxPrice", {}).get("@value") if isinstance(spec.get("schema:maxPrice"), dict) else None
        if mini:
            tous_min.append(float(mini))
        if maxi:
            tous_max.append(float(maxi))
        devise = devise or spec.get("schema:priceCurrency")

    if not tous_min and not tous_max:
        return None, None, None
    return (min(tous_min) if tous_min else None, max(tous_max) if tous_max else None, devise)


def _extraire_photo(poi: dict) -> str | None:
    """La photo (quand elle existe) est nichée derrière plusieurs
    niveaux : hasMainRepresentation > ebucore:hasRelatedResource >
    ebucore:locator — vérifié sur un vrai export avant de coder ça."""
    repr_ = poi.get("hasMainRepresentation")
    if not repr_:
        repr_secondaire = poi.get("hasRepresentation")
        if isinstance(repr_secondaire, list):
            repr_ = repr_secondaire[0] if repr_secondaire else None
        else:
            repr_ = repr_secondaire
    if not isinstance(repr_, dict):
        return None
    ressource = repr_.get("ebucore:hasRelatedResource")
    if not isinstance(ressource, dict):
        return None
    locator = ressource.get("ebucore:locator")
    return _texte_simple(locator)


def _extraire_equipements(poi: dict) -> str | None:
    """Deux sources possibles, combinées : isEquippedWith (références
    de thésaurus, ex: kb:Wifi) et hasFeature (booléens explicites, ex:
    internetAccess=true) — vérifiées sur un vrai export."""
    noms = []

    equip_brut = poi.get("isEquippedWith")
    if equip_brut:
        if not isinstance(equip_brut, list):
            equip_brut = [equip_brut]
        for e in equip_brut:
            if isinstance(e, dict) and e.get("@id"):
                noms.append(_libelle_thesaurus(e["@id"]))

    feature = poi.get("hasFeature")
    if isinstance(feature, dict):
        traductions_feature = {
            "internetAccess": "Wifi", "petsAllowed": "Animaux acceptés",
            "airConditioning": "Climatisation", "smokeFree": "Non fumeur",
        }
        for cle, libelle in traductions_feature.items():
            valeur = feature.get(cle)
            if isinstance(valeur, dict) and valeur.get("@value") == "true":
                noms.append(libelle)
        # "charged" est un cas particulier : true ET false sont tous les
        # deux des informations utiles (payant vs gratuit), contrairement
        # aux autres qui ne valent la peine d'être affichés que si vrais.
        charge = feature.get("charged")
        if isinstance(charge, dict):
            noms.append("Service payant" if charge.get("@value") == "true" else "Service gratuit")

    return ", ".join(dict.fromkeys(noms)) or None  # dict.fromkeys = dédoublonne en gardant l'ordre


TRADUCTIONS_LANGUES = {
    "fr": "Français", "en": "Anglais", "es": "Espagnol", "de": "Allemand",
    "it": "Italien", "nl": "Néerlandais", "pt": "Portugais", "zh": "Chinois",
}


def _extraire_notation(poi: dict) -> tuple:
    """hasReview mélange deux choses différentes : un classement en
    étoiles (ScaleRating, ex: '3 étoiles') et des labels qualité
    (LabelRating, ex: 'Accueil Vélo') — on sépare les deux plutôt que
    de les mélanger dans un seul champ confus."""
    reviews = poi.get("hasReview")
    if not reviews:
        return None, None
    if not isinstance(reviews, list):
        reviews = [reviews]

    etoiles = None
    labels = []
    for r in reviews:
        if not isinstance(r, dict):
            continue
        rv = r.get("hasReviewValue")
        if not isinstance(rv, dict):
            continue
        type_ = rv.get("@type", [])
        if isinstance(type_, str):
            type_ = [type_]
        if "ScaleRating" in type_:
            valeur = rv.get("schema:ratingValue")
            if isinstance(valeur, dict):
                etoiles = valeur.get("@value")
        elif "LabelRating" in type_:
            libelle = _valeur_langue(rv.get("rdfs:label"))
            if libelle:
                labels.append(libelle)

    return (float(etoiles) if etoiles else None), (", ".join(labels) or None)


def _extraire_accessibilite(poi: dict) -> str | None:
    """
    Pas de lien direct fiable possible : Acceslibre n'expose aucune
    URL construite à partir du seul identifiant qu'on récupère
    (hasExternalIdentifier) — vérifié, y compris un ticket resté
    ouvert sur leur propre dépôt GitHub demandant cette fonctionnalité,
    qui n'existe donc pas. L'URL réelle d'une fiche suit un format
    /app/{departement-ville}/a/{categorie}/erp/{nom-slug}/ qu'on ne
    peut pas reconstituer sans interroger leur API (nécessite une clé,
    demande manuelle).

    On renvoie donc un lien vers leur page de RECHERCHE, avec le nom
    de l'établissement pré-rempli — un vrai lien qui fonctionne,
    quitte à ce que l'utilisateur doive cliquer une fois de plus pour
    confirmer le bon résultat, plutôt qu'un lien direct qui tombe
    systématiquement en erreur 404.
    """
    refs = poi.get("hasExternalReference")
    if not refs:
        return None
    if not isinstance(refs, list):
        refs = [refs]

    a_une_reference_acceslibre = False
    for r in refs:
        if not isinstance(r, dict):
            continue
        plateforme = r.get("hasExternalPlatform")
        if isinstance(plateforme, dict) and plateforme.get("@id") == "kb:AcceslibrePlatform":
            a_une_reference_acceslibre = True
            break

    if not a_une_reference_acceslibre:
        return None

    nom = _valeur_langue(poi.get("rdfs:label"))
    if not nom:
        return None
    from urllib.parse import quote_plus
    return f"https://acceslibre.beta.gouv.fr/recherche/?q={quote_plus(nom)}"


def _extraire_langues(poi: dict) -> str | None:
    langues = poi.get("availableLanguage")
    if not langues:
        return None
    if not isinstance(langues, list):
        langues = [langues]
    noms = [TRADUCTIONS_LANGUES.get(l, l) for l in langues if isinstance(l, str)]
    return ", ".join(dict.fromkeys(noms)) or None


def _tronquer(valeur, longueur_max: int = 495):
    """Sécurité : certains champs texte (équipements, horaires...)
    peuvent dépasser la limite des colonnes VARCHAR(500) sur des cas
    rares (établissement avec énormément d'équipements listés, texte
    d'horaires très détaillé...). On tronque plutôt que de planter
    tout l'import pour une seule ligne récalcitrante."""
    if valeur is None:
        return None
    valeur = str(valeur)
    return valeur[:longueur_max] if len(valeur) > longueur_max else valeur


def _extraire_description(poi: dict) -> str | None:
    """rdfs:comment est le chemin le plus direct et fiable (vérifié sur
    plusieurs exports réels) — on ne creuse dans owl:topObjectProperty
    que si rdfs:comment est absent."""
    direct = _valeur_langue(poi.get("rdfs:comment"))
    if direct:
        return direct

    conteneur = poi.get("owl:topObjectProperty")
    if isinstance(conteneur, list):
        conteneur = conteneur[0] if conteneur else None
    if not isinstance(conteneur, dict):
        return None
    return _valeur_langue(conteneur.get("owl:topDataProperty"))


def _extraire_moyens_paiement(poi: dict) -> str | None:
    """schema:acceptedPaymentMethod — références de thésaurus (comme
    la cuisine/les équipements), à décoder avec le même helper."""
    offre = poi.get("schema:offers")
    if isinstance(offre, list):
        offre = offre[0] if offre else None
    if not isinstance(offre, dict):
        return None

    moyens = offre.get("schema:acceptedPaymentMethod")
    if not moyens:
        return None
    if not isinstance(moyens, list):
        moyens = [moyens]

    noms = []
    for m in moyens:
        if isinstance(m, dict) and m.get("@id"):
            noms.append(_libelle_thesaurus(m["@id"]))
    return ", ".join(dict.fromkeys(noms)) or None


def _extraire_note_tarif(poi: dict) -> str | None:
    """additionalInformation sur priceSpecification — une précision
    utile (ex: supplément petit-déjeuner), en plus du prix min/max déjà
    récupéré ailleurs."""
    offre = poi.get("schema:offers")
    if isinstance(offre, list):
        offre = offre[0] if offre else None
    if not isinstance(offre, dict):
        return None

    specs = offre.get("schema:priceSpecification")
    if isinstance(specs, list):
        specs = specs[0] if specs else None
    if not isinstance(specs, dict):
        return None

    return _valeur_langue(specs.get("additionalInformation"))


def _extraire_objet(poi: dict, categorie: str) -> dict | None:
    identifiant = poi.get("dc:identifier")
    nom = _valeur_langue(poi.get("rdfs:label"))
    if not identifiant or not nom:
        return None

    index_local = {}
    _indexer_ids_locaux(poi, index_local)

    localisation = poi.get("isLocatedAt") or {}
    if isinstance(localisation, list):
        localisation = localisation[0] if localisation else {}
    adresse_obj = _resoudre(localisation.get("schema:address") or {}, index_local)
    if isinstance(adresse_obj, list):
        adresse_obj = adresse_obj[0] if adresse_obj else {}
    geo = localisation.get("schema:geo") or {}

    lat_brut = geo.get("schema:latitude", {}).get("@value") if isinstance(geo.get("schema:latitude"), dict) else None
    lon_brut = geo.get("schema:longitude", {}).get("@value") if isinstance(geo.get("schema:longitude"), dict) else None
    if not lat_brut or not lon_brut:
        return None

    commune = _texte_simple(adresse_obj.get("schema:addressLocality"))
    ville_obj = adresse_obj.get("hasAddressCity") or {}
    departement = None
    dept_obj = ville_obj.get("isPartOfDepartment")
    if isinstance(dept_obj, dict):
        departement = _valeur_langue(dept_obj.get("rdfs:label"))

    contact_brut = poi.get("hasContact") or {}
    if isinstance(contact_brut, list):
        contact = max(contact_brut, key=lambda c: len(c) if isinstance(c, dict) else 0) if contact_brut else {}
    else:
        contact = contact_brut
    contact = _resoudre(contact, index_local)

    horaires = None
    ouverture = localisation.get("schema:openingHoursSpecification")
    if isinstance(ouverture, dict):
        horaires = _valeur_langue(ouverture.get("additionalInformation"))
    elif isinstance(ouverture, list) and ouverture:
        horaires = _valeur_langue(ouverture[0].get("additionalInformation"))

    tarif_min, tarif_max, devise = _extraire_tarifs(poi.get("schema:offers"))

    rue = _texte_simple(adresse_obj.get("schema:streetAddress"))
    cp = _texte_simple(adresse_obj.get("schema:postalCode"))
    adresse_complete = ", ".join(p for p in (rue, cp, commune) if p) or None

    cuisine = None
    cuisine_brut = poi.get("providesCuisineOfType")
    if cuisine_brut:
        if not isinstance(cuisine_brut, list):
            cuisine_brut = [cuisine_brut]
        noms_cuisine = []
        for c in cuisine_brut:
            if not isinstance(c, dict):
                continue
            # Soit un vrai objet avec rdfs:label, soit juste une
            # référence de thésaurus {"@id": "kb:TraditionalCuisine"}.
            label = _valeur_langue(c.get("rdfs:label"))
            if not label and c.get("@id"):
                label = _libelle_thesaurus(c["@id"])
            if label:
                noms_cuisine.append(label)
        cuisine = ", ".join(noms_cuisine) or None

    # hasTheme a des libellés plus riches quand il contient des
    # catégories de cuisine (ex: "Cuisine traditionnelle française" au
    # lieu de notre "Cuisine traditionnelle" deviné) — on préfère cette
    # version si elle existe, sans écraser le résultat s'il n'y en a pas.
    themes = poi.get("hasTheme")
    if themes:
        if not isinstance(themes, list):
            themes = [themes]
        noms_cuisine_theme = []
        for t in themes:
            if not isinstance(t, dict):
                continue
            type_ = t.get("@type", [])
            if isinstance(type_, str):
                type_ = [type_]
            if "CuisineCategory" in type_:
                libelle = _valeur_langue(t.get("rdfs:label"))
                if libelle:
                    noms_cuisine_theme.append(libelle)
        if noms_cuisine_theme:
            cuisine = ", ".join(dict.fromkeys(noms_cuisine_theme))

    photo_url = _extraire_photo(poi)
    equipements = _extraire_equipements(poi)
    description = _extraire_description(poi)
    moyens_paiement = _extraire_moyens_paiement(poi)
    note_tarif = _extraire_note_tarif(poi)

    capacite = None
    allowed = poi.get("allowedPersons")
    if isinstance(allowed, dict):
        capacite = allowed.get("@value")

    note_etoiles, labels_qualite = _extraire_notation(poi)
    lien_accessibilite = _extraire_accessibilite(poi)
    langues_parlees = _extraire_langues(poi)

    contact_reservation = _resoudre(poi.get("hasBookingContact") or {}, index_local)
    telephone = _texte_simple(contact.get("schema:telephone")) or _texte_simple(contact_reservation.get("schema:telephone"))
    email = _texte_simple(contact.get("schema:email")) or _texte_simple(contact_reservation.get("schema:email"))

    return {
        "identifiant_dt": _tronquer(identifiant, 95),
        "nom": _tronquer(nom, 250),
        "categorie": categorie,
        "classification": _tronquer(_classification(poi.get("@type", [])), 250),
        "commune": _tronquer(commune, 250),
        "departement": _tronquer(departement, 95),
        "latitude": float(lat_brut),
        "longitude": float(lon_brut),
        "adresse": _tronquer(adresse_complete, 495),
        "telephone": _tronquer(telephone, 45),
        "email": _tronquer(email, 250),
        "site_web": _tronquer(_texte_simple(contact.get("foaf:homepage")), 495),
        "horaires": _tronquer(horaires, 495),
        "tarif_min": tarif_min,
        "tarif_max": tarif_max,
        "devise": _tronquer(devise, 10),
        "cuisine": _tronquer(cuisine, 250),
        "photo_url": _tronquer(photo_url, 495),
        "equipements": _tronquer(equipements, 495),
        "description": _tronquer(description, 1990),
        "moyens_paiement": _tronquer(moyens_paiement, 250),
        "note_tarif": _tronquer(note_tarif, 250),
        "capacite": int(capacite) if capacite else None,
        "note_etoiles": note_etoiles,
        "labels_qualite": _tronquer(labels_qualite, 495),
        "lien_accessibilite": _tronquer(lien_accessibilite, 495),
        "langues_parlees": _tronquer(langues_parlees, 250),
    }


def _ouvrir_fichier(chemin: str):
    """Détecte un fichier gzip par ses octets magiques (1f 8b), peu
    importe l'extension — le serveur DATAtourisme renvoie parfois le
    flux compressé même quand on ne s'y attend pas."""
    with open(chemin, "rb") as f:
        entete = f.read(2)
    if entete == b"\x1f\x8b":
        return gzip.open(chemin, "rb")
    return open(chemin, "rb")


async def main(fichier: str, categorie: str):
    await init_db_pool()
    try:
        importes = 0
        ignores = 0
        lot: list[tuple] = []
        TAILLE_LOT = 500

        requete = """
            INSERT INTO datatourisme_objets
                (identifiant_dt, nom, categorie, classification, commune, departement,
                 latitude, longitude, adresse, telephone, email, site_web,
                 horaires, tarif_min, tarif_max, devise, cuisine, photo_url, equipements, capacite,
                 note_etoiles, labels_qualite, lien_accessibilite, langues_parlees, description,
                 moyens_paiement, note_tarif)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (identifiant_dt) DO UPDATE SET
                nom = EXCLUDED.nom, classification = EXCLUDED.classification,
                commune = EXCLUDED.commune, departement = EXCLUDED.departement,
                latitude = EXCLUDED.latitude, longitude = EXCLUDED.longitude,
                adresse = EXCLUDED.adresse, telephone = EXCLUDED.telephone,
                email = EXCLUDED.email, site_web = EXCLUDED.site_web,
                horaires = EXCLUDED.horaires, tarif_min = EXCLUDED.tarif_min,
                tarif_max = EXCLUDED.tarif_max, devise = EXCLUDED.devise,
                cuisine = EXCLUDED.cuisine, photo_url = EXCLUDED.photo_url,
                equipements = EXCLUDED.equipements, capacite = EXCLUDED.capacite,
                note_etoiles = EXCLUDED.note_etoiles, labels_qualite = EXCLUDED.labels_qualite,
                lien_accessibilite = EXCLUDED.lien_accessibilite, langues_parlees = EXCLUDED.langues_parlees,
                description = EXCLUDED.description, moyens_paiement = EXCLUDED.moyens_paiement,
                note_tarif = EXCLUDED.note_tarif
        """

        async def _vider_le_lot():
            nonlocal lot
            if lot:
                await executemany(requete, lot)
                lot = []

        with _ouvrir_fichier(fichier) as f:
            for poi in ijson.items(f, "@graph.item"):
                try:
                    objet = _extraire_objet(poi, categorie)
                except Exception as e:
                    print(f"  ⚠️ Entrée ignorée (erreur d'extraction): {e}", flush=True)
                    ignores += 1
                    continue
                if not objet:
                    ignores += 1
                    continue

                lot.append((
                    objet["identifiant_dt"], objet["nom"], objet["categorie"], objet["classification"],
                    objet["commune"], objet["departement"], objet["latitude"], objet["longitude"],
                    objet["adresse"], objet["telephone"], objet["email"], objet["site_web"],
                    objet["horaires"], objet["tarif_min"], objet["tarif_max"], objet["devise"],
                    objet["cuisine"], objet["photo_url"], objet["equipements"], objet["capacite"],
                    objet["note_etoiles"], objet["labels_qualite"], objet["lien_accessibilite"],
                    objet["langues_parlees"], objet["description"],
                    objet["moyens_paiement"], objet["note_tarif"],
                ))
                importes += 1

                if len(lot) >= TAILLE_LOT:
                    await _vider_le_lot()

                if importes % 2000 == 0:
                    print(f"  … {importes} importés (+ {ignores} ignorés)", flush=True)

        await _vider_le_lot()  # dernier lot, potentiellement incomplet

        print(f"\nTerminé : {importes} importé(s), {ignores} ignoré(s).", flush=True)
    finally:
        await close_db_pool()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--fichier", required=True)
    parser.add_argument("--categorie", required=True, choices=[
        "hebergement", "restaurant", "activite", "office_tourisme",
    ])
    args = parser.parse_args()
    asyncio.run(main(args.fichier, args.categorie))