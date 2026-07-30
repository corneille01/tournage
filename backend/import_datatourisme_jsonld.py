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

from db import init_db_pool, close_db_pool, execute

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

    return {
        "identifiant_dt": identifiant,
        "nom": nom,
        "categorie": categorie,
        "classification": _classification(poi.get("@type", [])),
        "commune": commune,
        "departement": departement,
        "latitude": float(lat_brut),
        "longitude": float(lon_brut),
        "adresse": adresse_complete,
        "telephone": _texte_simple(contact.get("schema:telephone")),
        "email": _texte_simple(contact.get("schema:email")),
        "site_web": _texte_simple(contact.get("foaf:homepage")),
        "horaires": horaires,
        "tarif_min": tarif_min,
        "tarif_max": tarif_max,
        "devise": devise,
        "cuisine": cuisine,
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

                await execute(
                    """
                    INSERT INTO datatourisme_objets
                        (identifiant_dt, nom, categorie, classification, commune, departement,
                         latitude, longitude, adresse, telephone, email, site_web,
                         horaires, tarif_min, tarif_max, devise, cuisine)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (identifiant_dt) DO UPDATE SET
                        nom = EXCLUDED.nom, classification = EXCLUDED.classification,
                        commune = EXCLUDED.commune, departement = EXCLUDED.departement,
                        latitude = EXCLUDED.latitude, longitude = EXCLUDED.longitude,
                        adresse = EXCLUDED.adresse, telephone = EXCLUDED.telephone,
                        email = EXCLUDED.email, site_web = EXCLUDED.site_web,
                        horaires = EXCLUDED.horaires, tarif_min = EXCLUDED.tarif_min,
                        tarif_max = EXCLUDED.tarif_max, devise = EXCLUDED.devise,
                        cuisine = EXCLUDED.cuisine
                    """,
                    (
                        objet["identifiant_dt"], objet["nom"], objet["categorie"], objet["classification"],
                        objet["commune"], objet["departement"], objet["latitude"], objet["longitude"],
                        objet["adresse"], objet["telephone"], objet["email"], objet["site_web"],
                        objet["horaires"], objet["tarif_min"], objet["tarif_max"], objet["devise"],
                        objet["cuisine"],
                    ),
                )
                importes += 1

                if importes % 2000 == 0:
                    print(f"  … {importes} importés (+ {ignores} ignorés)", flush=True)

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
