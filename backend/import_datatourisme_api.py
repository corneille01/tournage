"""
backend/import_datatourisme_api.py — Synchronise la catégorie
"fêtes et manifestations" via l'API REST DATAtourisme
(https://api.datatourisme.fr/v1/docs), en remplacement du diffuseur
tant que celui-ci reste inaccessible.

Respecte les quotas documentés (20-30 requêtes concurrentes, ~10/s en
continu, 1000/h) via un simple espacement séquentiel — largement
suffisant vu le volume attendu pour l'Occitanie (pas besoin de
parallélisme ici, la pagination via meta.next est intrinsèquement
séquentielle).

Reprend automatiquement là où une exécution précédente s'est arrêtée
(URL "next" sauvegardée dans sync_state), utile si le run est
interrompu par le plafond horaire ou un timeout GitHub Actions.

⚠️ À VÉRIFIER au premier run après déploiement : la forme exacte de
takesPlaceAt (dates de l'événement) dans cette API REST. L'ontologie
DATAtourisme documente ce champ comme un LimitedPeriod (startDate,
endDate, startTime, endTime) ou un RecurrentPeriod (appliesOnDay +
dates), mais la doc REST publique ne montre pas d'exemple concret et
ce n'était pas visible dans les payloads observés jusqu'ici (tronqués
avant ce champ). _extraire_dates() essaie plusieurs formes plausibles
et logue un avertissement si rien n'est trouvé — à ajuster une fois un
vrai POI avec dates observé (voir le log "Premier objet brut reçu").

Usage :
    python import_datatourisme_api.py
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import os
import time
import urllib.parse

import httpx

from db import init_db_pool, close_db_pool, execute, fetch_one

API_BASE = os.environ.get("DATATOURISME_API_BASE", "https://api.datatourisme.fr/v1")
API_KEY = os.environ["DATATOURISME_API_KEY"]

CATEGORIE = "fetes_manifestations"

# Départements couverts par l'app (mêmes codes INSEE que dans
# enrich-itineraires.yml).
DEPARTEMENTS_INSEE = "09,11,12,30,31,32,34,46,48,65,66,81,82"

# Marge de sécurité, volontairement bien en dessous des limites
# officielles (10 req/s, 1000/h).
DELAI_ENTRE_REQUETES_S = 0.35
PLAFOND_REQUETES_PAR_RUN = 900  # sécurité : on s'arrête avant 1000/h et on reprendra au run suivant

CLE_CURSEUR = "datatourisme_api_fetes_next_url"

# On ne logue l'avertissement "dates introuvables" qu'une seule fois par
# run pour ne pas noyer les logs si le champ est structurellement absent.
_avertissement_dates_deja_logue = False

# Domaines de réseaux sociaux connus, pour NE JAMAIS les mélanger avec
# le vrai site officiel dans "site_web" (voir _extraire_site_et_reseaux).
DOMAINES_RESEAUX_SOCIAUX = {
    "facebook.com": "facebook",
    "fb.com": "facebook",
    "instagram.com": "instagram",
    "twitter.com": "twitter",
    "x.com": "twitter",
    "tiktok.com": "tiktok",
    "youtube.com": "youtube",
    "youtu.be": "youtube",
}


async def _lire_curseur() -> str | None:
    row = await fetch_one("SELECT valeur FROM sync_state WHERE cle = %s", (CLE_CURSEUR,))
    return row["valeur"] if row else None


async def _sauver_curseur(valeur: str | None) -> None:
    await execute(
        """
        INSERT INTO sync_state (cle, valeur, maj) VALUES (%s, %s, NOW())
        ON CONFLICT (cle) DO UPDATE SET valeur = EXCLUDED.valeur, maj = NOW()
        """,
        (CLE_CURSEUR, valeur),
    )


def _texte(champ, lang: str = "fr"):
    """Les champs multilingues sont de la forme {"@fr": "...", "@en": "..."}
    (confirmé sur les payloads réels observés)."""
    if champ is None:
        return None
    if isinstance(champ, dict):
        cle_lang = f"@{lang}"
        if cle_lang in champ:
            return champ[cle_lang]
        if lang in champ:
            return champ[lang]
        return next(iter(champ.values()), None)
    if isinstance(champ, list):
        return _texte(champ[0], lang) if champ else None
    return champ


def _texte_liste(champ, lang: str = "fr") -> str | None:
    """Aplatit un champ qui peut être une chaîne, un dict multilingue, ou
    une liste (potentiellement imbriquée) de chaînes/dicts multilingues,
    en une seule chaîne (les valeurs multiples sont jointes par ', ')."""
    if champ is None:
        return None
    if isinstance(champ, str):
        return champ or None
    if isinstance(champ, dict):
        return _texte(champ, lang)
    if isinstance(champ, list):
        valeurs = []
        for x in champ:
            if isinstance(x, dict):
                v = _texte(x, lang)
            elif isinstance(x, list):
                # Repli défensif : certains payloads DATAtourisme imbriquent
                # une liste dans une liste sur ce type de champ — on aplatit
                # récursivement plutôt que de planter sur le join().
                v = _texte_liste(x, lang)
            else:
                v = x
            if v:
                valeurs.append(v)
        return ", ".join(valeurs) if valeurs else None
    return None
def _reseau_social_pour_url(url: str) -> str | None:
    """Retourne le nom du réseau social ('facebook', 'instagram', ...) si
    l'URL pointe vers un domaine de réseau social connu, sinon None."""
    if not url or not isinstance(url, str):
        return None
    try:
        hote = urllib.parse.urlparse(url).netloc.lower()
    except ValueError:
        return None
    if hote.startswith("www."):
        hote = hote[4:]
    return DOMAINES_RESEAUX_SOCIAUX.get(hote)


def _extraire_site_et_reseaux(contact: dict) -> tuple[str | None, dict]:
    """Sépare hasContact.homepage en (site_web, reseaux_sociaux).

    C'est ICI qu'était le bug : hasContact.homepage est presque toujours
    une LISTE dans les payloads DATAtourisme (convention JSON-LD), et
    peut contenir à la fois le site officiel ET une page Facebook /
    Instagram dans la même liste. L'ancien code appelait _texte_liste()
    dessus, qui aplatit toute liste en une seule chaîne jointe par ", " —
    ce qui est très bien pour une adresse, mais fabrique un href invalide
    dès qu'il y a 2 URLs ("https://site.fr, https://facebook.com/xxx"),
    et surtout ne fait aucune différence entre "site officiel" et
    "réseau social" : le premier lien de la liste (parfois Facebook)
    finissait affiché comme "Voir le site".

    Retourne :
    - site_web : la première URL qui N'EST PAS un réseau social connu
      (jamais plusieurs URLs collées ensemble).
    - reseaux_sociaux : dict {"facebook": url, "instagram": url, ...}
      pour les liens sociaux détectés, à stocker et afficher séparément
      côté frontend (icône Facebook/Instagram dédiée, pas "Voir le
      site").
    """
    urls = contact.get("homepage")
    if not urls:
        return None, {}
    if isinstance(urls, (str, dict)):
        urls = [urls]

    site_web = None
    reseaux: dict[str, str] = {}
    for u in urls:
        valeur = _texte(u) if isinstance(u, dict) else u
        if not isinstance(valeur, str) or not valeur:
            continue
        reseau = _reseau_social_pour_url(valeur)
        if reseau:
            reseaux.setdefault(reseau, valeur)
        elif site_web is None:
            site_web = valeur
    return site_web, reseaux


def _chemin(objet: dict, *cles, defaut=None):
    """Descend dans un dict imbriqué, clé par clé, sans planter si un
    niveau est absent. Déballe automatiquement les listes à un seul
    élément (convention JSON-LD de l'API DATAtourisme : isLocatedAt,
    hasAddressCity, etc. sont presque toujours des tableaux)."""
    courant = objet
    for cle in cles:
        if isinstance(courant, list):
            courant = courant[0] if courant else None
        if not isinstance(courant, dict):
            return defaut
        courant = courant.get(cle)
    if isinstance(courant, list):
        courant = courant[0] if courant else None
    return courant if courant is not None else defaut


def _extraire_description(poi: dict, lang: str = "fr") -> str | None:
    descriptions = poi.get("hasDescription")
    if not descriptions:
        return None
    if not isinstance(descriptions, list):
        descriptions = [descriptions]
    for d in descriptions:
        for cle in ("description", "longDescription", "shortDescription"):
            texte = _texte(d.get(cle) if isinstance(d, dict) else None, lang)
            if texte:
                return texte[:1990]
    return None


def _extraire_photo(poi: dict) -> str | None:
    repr_ = poi.get("hasMainRepresentation")
    if isinstance(repr_, list):
        repr_ = repr_[0] if repr_ else None
    if not isinstance(repr_, dict):
        return None
    return repr_.get("url") or _chemin(repr_, "hasRelatedResource", "locator")


def _extraire_photo_credits(poi: dict) -> tuple[str | None, str | None]:
    """Retourne (credits, licence) depuis hasMainRepresentation.hasAnnotation.
    Certaines licences (ex. By-NC-ND) imposent d'afficher le crédit à côté
    de l'image — jusqu'ici on ne le stockait pas du tout."""
    repr_ = poi.get("hasMainRepresentation")
    if isinstance(repr_, list):
        repr_ = repr_[0] if repr_ else None
    if not isinstance(repr_, dict):
        return None, None
    annotation = _chemin(repr_, "hasAnnotation", defaut={})
    credits_ = _texte_liste(annotation.get("credits"))
    licence = annotation.get("isCoveredBy")
    if isinstance(licence, list):
        licence = licence[0] if licence else None
    return credits_, licence


def _extraire_types(poi: dict) -> str | None:
    """Le champ "type" liste les catégories DATAtourisme de l'objet
    (ex. MusicEvent, Concert, EntertainmentAndEvent). Utile pour filtrer
    par type d'événement côté appli — jusqu'ici entièrement ignoré."""
    types_ = poi.get("type")
    if not types_:
        return None
    if isinstance(types_, str):
        return types_
    if isinstance(types_, list):
        return ", ".join(str(t) for t in types_ if t) or None
    return None


def _parse_date(valeur) -> datetime.date | None:
    """Convertit une chaîne 'YYYY-MM-DD...' en véritable datetime.date.

    asyncpg exige un objet date pour les colonnes DATE — lui passer une
    chaîne plante l'insertion avec 'str' object has no attribute
    'toordinal', même si la chaîne est au bon format."""
    if not valeur or not isinstance(valeur, str):
        return None
    try:
        return datetime.date.fromisoformat(valeur[:10])
    except ValueError:
        return None


def _parse_datetime(valeur) -> datetime.datetime | None:
    """Convertit une chaîne ISO 8601 (ex. '2026-09-09T13:01:06.218Z') en
    véritable datetime.datetime, pour les mêmes raisons que _parse_date
    — nécessaire pour une colonne TIMESTAMPTZ."""
    if not valeur or not isinstance(valeur, str):
        return None
    try:
        return datetime.datetime.fromisoformat(valeur.replace("Z", "+00:00"))
    except ValueError:
        return None


def _extraire_dates(poi: dict) -> tuple[datetime.date | None, datetime.date | None, str | None]:
    """Extrait les dates de l'événement depuis takesPlaceAt.

    Retourne (date_debut, date_fin, json_brut_str) :
    - date_debut / date_fin : bornes simplifiées (la plus proche / la plus
      lointaine trouvée parmi toutes les périodes), pratiques pour trier
      ou filtrer "événements à venir".
    - json_brut_str : la structure takesPlaceAt telle quelle (sérialisée),
      pour ne perdre aucune information si un événement a plusieurs
      représentations / une récurrence complexe.

    ⚠️ Forme non confirmée sur un vrai payload REST à ce jour — voir la
    note en tête de fichier. Si ça ne matche rien, un avertissement est
    logué une fois par run plutôt que de faire planter l'extraction.
    """
    global _avertissement_dates_deja_logue

    periodes = poi.get("takesPlaceAt")
    if not periodes:
        # Repli sur schema:startDate / schema:endDate, mentionnés dans la
        # doc de l'ontologie comme parfois présents en parallèle.
        sd = poi.get("startDate") or _chemin(poi, "schema:startDate")
        ed = poi.get("endDate") or _chemin(poi, "schema:endDate")
        sd = _parse_date(_texte_liste(sd)) if sd else None
        ed = _parse_date(_texte_liste(ed)) if ed else None
        if sd or ed:
            return sd, ed, json.dumps({"startDate": sd, "endDate": ed}, ensure_ascii=False, default=str)
        if not _avertissement_dates_deja_logue:
            print(
                "  ⚠️ Aucun champ de date trouvé (takesPlaceAt/startDate/endDate absents) "
                "— à vérifier sur le payload brut loggué plus haut.",
                flush=True,
            )
            _avertissement_dates_deja_logue = True
        return None, None, None

    if not isinstance(periodes, list):
        periodes = [periodes]

    dates_debut: list[str] = []
    dates_fin: list[str] = []
    for p in periodes:
        if not isinstance(p, dict):
            continue
        sd = _parse_date(_texte_liste(p.get("startDate")))
        ed = _parse_date(_texte_liste(p.get("endDate")))
        if sd:
            dates_debut.append(sd)
        if ed:
            dates_fin.append(ed)

    date_debut = min(dates_debut) if dates_debut else None
    date_fin = max(dates_fin) if dates_fin else (max(dates_debut) if dates_debut else None)

    try:
        json_brut = json.dumps(periodes, ensure_ascii=False, default=str)
    except TypeError:
        json_brut = None

    if not date_debut and not date_fin and not _avertissement_dates_deja_logue:
        print(
            "  ⚠️ takesPlaceAt présent mais aucune startDate/endDate exploitable "
            "— structure probablement différente de ce qui est attendu, à vérifier.",
            flush=True,
        )
        _avertissement_dates_deja_logue = True

    return date_debut, date_fin, json_brut


def _extraire_objet(poi: dict) -> dict | None:
    uuid = poi.get("uuid")
    nom = _texte(poi.get("label"))
    geo = _chemin(poi, "isLocatedAt", "geo", defaut={})
    lat, lon = geo.get("latitude"), geo.get("longitude")

    if not (uuid and nom and lat and lon):
        return None

    adresse_obj = _chemin(poi, "isLocatedAt", "address", defaut={})
    commune = _texte_liste(_chemin(adresse_obj, "hasAddressCity", "label"))
    departement = _texte_liste(_chemin(adresse_obj, "hasAddressCity", "isPartOfDepartment", "label"))
    rue = _texte_liste(adresse_obj.get("streetAddress"))
    # postalCode est généralement une chaîne, mais certains POI le renvoient
    # en liste (parfois plusieurs codes postaux pour une même adresse) —
    # d'où le même traitement défensif que pour les autres champs.
    cp = _texte_liste(adresse_obj.get("postalCode"))
    adresse_complete = ", ".join(p for p in (rue, cp, commune) if p) or None

    insee = _chemin(adresse_obj, "hasAddressCity", "insee")
    if isinstance(insee, list):
        insee = insee[0] if insee else None
    adresse_localite = _texte_liste(adresse_obj.get("addressLocality"))

    contact = poi.get("hasContact") or {}
    if isinstance(contact, list):
        contact = contact[0] if contact else {}

    photo_credits, photo_licence = _extraire_photo_credits(poi)
    date_debut, date_fin, dates_json = _extraire_dates(poi)
    # hasContact.homepage peut contenir le site officiel ET une page
    # Facebook/Instagram dans la même liste : on les sépare pour ne
    # jamais coller plusieurs URLs dans "site_web" (voir docstring de
    # _extraire_site_et_reseaux pour le détail du bug corrigé ici).
    site_web, reseaux_sociaux = _extraire_site_et_reseaux(contact)

    organisme = _texte_liste(_chemin(poi, "hasBeenCreatedBy", "legalName"))

    return {
        "identifiant_dt": str(uuid)[:95],
        "nom": nom[:250],
        "commune": (commune or "")[:250] or None,
        "departement": (departement or "")[:95] or None,
        "latitude": float(lat),
        "longitude": float(lon),
        "adresse": (adresse_complete or "")[:495] or None,
        "telephone": (_texte_liste(contact.get("telephone")) or "")[:45] or None,
        "site_web": (site_web or "")[:495] or None,
        "reseaux_sociaux": json.dumps(reseaux_sociaux, ensure_ascii=False) if reseaux_sociaux else None,
        "description": _extraire_description(poi, "fr"),
        "photo_url": (_extraire_photo(poi) or "")[:495] or None,
        # Champs précédemment inexploités :
        "uri": poi.get("uri"),
        "types": _extraire_types(poi),
        "insee": (str(insee)[:10] if insee else None),
        "adresse_localite": (adresse_localite or "")[:250] or None,
        "photo_credits": (photo_credits or "")[:495] or None,
        "photo_licence": (photo_licence or "")[:95] or None,
        "organisme_diffuseur": (organisme or "")[:250] or None,
        "date_maj_source": _parse_date(poi.get("lastUpdate")),
        "date_maj_datatourisme": _parse_datetime(poi.get("lastUpdateDatatourisme")),
        "description_en": _extraire_description(poi, "en"),
        "date_debut": date_debut,
        "date_fin": date_fin,
        "dates_evenement": dates_json,
    }


async def main():
    await init_db_pool()
    try:
        url = await _lire_curseur()
        if url:
            print(f"Reprise à partir du curseur sauvegardé : {url[:80]}…", flush=True)
        else:
            url = (
                f"{API_BASE}/entertainmentAndEvent"
                f"?page_size=100&lang=fr"
                f"&filters=isLocatedAt.address.hasAddressCity.isPartOfDepartment.insee[in]={DEPARTEMENTS_INSEE}"
            )

        importes = 0
        ignores = 0
        requetes = 0
        premier_log_fait = False

        async with httpx.AsyncClient(timeout=30) as client:
            while url and requetes < PLAFOND_REQUETES_PAR_RUN:
                debut = time.monotonic()

                resp = await client.get(url, headers={"X-API-Key": API_KEY})
                requetes += 1

                if resp.status_code == 429:
                    attente = float(resp.headers.get("Retry-After", "5"))
                    print(f"  ⚠️ 429 reçu, pause {attente}s", flush=True)
                    await asyncio.sleep(attente)
                    continue

                resp.raise_for_status()
                data = resp.json()

                if not premier_log_fait and data.get("objects"):
                    print("── Premier objet brut reçu (à comparer aux extractions) ──", flush=True)
                    print(data["objects"][0], flush=True)
                    premier_log_fait = True

                for poi in data.get("objects", []):
                    try:
                        objet = _extraire_objet(poi)
                    except Exception as exc:
                        # Un POI malformé ne doit jamais interrompre toute
                        # la synchro : on le logue et on continue.
                        ignores += 1
                        print(
                            f"  ⚠️ POI ignoré (erreur d'extraction: {exc}) — uuid={poi.get('uuid')!r}",
                            flush=True,
                        )
                        continue

                    if not objet:
                        ignores += 1
                        continue

                    await execute(
                        """
                        INSERT INTO datatourisme_objets
                            (identifiant_dt, nom, categorie, commune, departement,
                             latitude, longitude, adresse, telephone, site_web,
                             reseaux_sociaux,
                             description, photo_url,
                             uri, types, insee, adresse_localite,
                             photo_credits, photo_licence, organisme_diffuseur,
                             date_maj_source, date_maj_datatourisme, description_en,
                             date_debut, date_fin, dates_evenement)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (identifiant_dt) DO UPDATE SET
                            nom = EXCLUDED.nom, commune = EXCLUDED.commune,
                            departement = EXCLUDED.departement,
                            latitude = EXCLUDED.latitude, longitude = EXCLUDED.longitude,
                            adresse = EXCLUDED.adresse, telephone = EXCLUDED.telephone,
                            site_web = EXCLUDED.site_web,
                            reseaux_sociaux = EXCLUDED.reseaux_sociaux,
                            description = EXCLUDED.description,
                            photo_url = EXCLUDED.photo_url,
                            uri = EXCLUDED.uri, types = EXCLUDED.types,
                            insee = EXCLUDED.insee, adresse_localite = EXCLUDED.adresse_localite,
                            photo_credits = EXCLUDED.photo_credits,
                            photo_licence = EXCLUDED.photo_licence,
                            organisme_diffuseur = EXCLUDED.organisme_diffuseur,
                            date_maj_source = EXCLUDED.date_maj_source,
                            date_maj_datatourisme = EXCLUDED.date_maj_datatourisme,
                            description_en = EXCLUDED.description_en,
                            date_debut = EXCLUDED.date_debut, date_fin = EXCLUDED.date_fin,
                            dates_evenement = EXCLUDED.dates_evenement
                        """,
                        (
                            objet["identifiant_dt"], objet["nom"], CATEGORIE,
                            objet["commune"], objet["departement"],
                            objet["latitude"], objet["longitude"], objet["adresse"],
                            objet["telephone"], objet["site_web"], objet["reseaux_sociaux"],
                            objet["description"], objet["photo_url"],
                            objet["uri"], objet["types"], objet["insee"], objet["adresse_localite"],
                            objet["photo_credits"], objet["photo_licence"], objet["organisme_diffuseur"],
                            objet["date_maj_source"], objet["date_maj_datatourisme"], objet["description_en"],
                            objet["date_debut"], objet["date_fin"], objet["dates_evenement"],
                        ),
                    )
                    importes += 1

                url = _chemin(data, "meta", "next")

                # Sauvegarde le curseur à CHAQUE page, pas seulement à la
                # fin — si le run est interrompu, on ne repart pas de zéro.
                await _sauver_curseur(url)

                if importes % 200 == 0 and importes:
                    print(f"  … {importes} importés (+ {ignores} ignorés), {requetes} requêtes", flush=True)

                ecoule = time.monotonic() - debut
                await asyncio.sleep(max(0.0, DELAI_ENTRE_REQUETES_S - ecoule))

        if url:
            print(
                f"\nPlafond de {PLAFOND_REQUETES_PAR_RUN} requêtes atteint pour ce run — "
                f"reprise automatique au prochain run. {importes} importé(s) jusqu'ici.",
                flush=True,
            )
        else:
            await _sauver_curseur(None)  # synchro complète, on repart de la page 1 au prochain cron
            print(f"\nSynchro complète : {importes} importé(s), {ignores} ignoré(s).", flush=True)

    finally:
        await close_db_pool()


if __name__ == "__main__":
    asyncio.run(main())