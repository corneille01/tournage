-- Migration v26
--
-- Ajoute l'annuaire des guides et médiateurs cinétouristiques
-- (voir backend/guides.py pour le moteur de matching).
--
-- MVP : pas de compte utilisateur guide. Chaque fiche est saisie à la
-- main (POST /api/guides, protégé par ADMIN_TOKEN) ou directement en
-- SQL, pour un lancement limité à quelques dizaines de profils
-- vérifiés en Occitanie avant d'envisager un annuaire plus large.
--
-- La "zone d'intervention" est un simple point + un rayon en km,
-- volontairement plus simple qu'une liste de communes couvertes :
-- suffisant pour filtrer par distance à une étape de parcours.

CREATE TABLE IF NOT EXISTS guides (
    id                      SERIAL PRIMARY KEY,
    nom                     VARCHAR(255) NOT NULL,
    type_guide              VARCHAR(30) NOT NULL DEFAULT 'autre'
                                CHECK (type_guide IN (
                                    'guide_conferencier', 'mediateur_culturel', 'accompagnateur',
                                    'historien', 'passionne_cinema', 'autre'
                                )),
    bio                     TEXT NULL,

    -- Tableaux Postgres natifs plutôt que des tables de jonction :
    -- cohérent avec le reste du schéma (voir reseaux_sociaux JSONB sur
    -- datatourisme_objets) et largement suffisant tant que le nombre
    -- de guides reste de l'ordre de quelques centaines.
    specialites             TEXT[] NOT NULL DEFAULT '{}',
    langues                 TEXT[] NOT NULL DEFAULT '{}',
    publics                 TEXT[] NOT NULL DEFAULT '{}',
    mobilite                TEXT[] NOT NULL DEFAULT '{}',

    latitude                DECIMAL(10, 7) NOT NULL,
    longitude               DECIMAL(10, 7) NOT NULL,
    rayon_intervention_km   INT NOT NULL DEFAULT 30,

    capacite_max            INT NULL,
    tarif_indicatif         VARCHAR(255) NULL,
    site_web                VARCHAR(500) NULL,
    lien_contact            VARCHAR(500) NULL,
    photo_url               VARCHAR(500) NULL,

    -- 'en_attente' par défaut au niveau applicatif si le statut n'est
    -- pas précisé à la création (voir main.py::creer_guide) : une
    -- fiche non vérifiée n'apparaît jamais dans le matching, qui ne
    -- lit que statut = 'actif'.
    statut                  VARCHAR(15) NOT NULL DEFAULT 'actif'
                                CHECK (statut IN ('actif', 'inactif', 'en_attente')),

    date_creation           TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    date_maj                TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_guides_statut ON guides (statut);
CREATE INDEX IF NOT EXISTS idx_guides_coords ON guides (latitude, longitude);

-- Réutilise le trigger générique déjà défini dans schema.sql
-- (maj_date_modification) pour date_maj, comme pour films/amenity_cache.
DROP TRIGGER IF EXISTS trg_guides_date_maj ON guides;
CREATE TRIGGER trg_guides_date_maj
    BEFORE UPDATE ON guides
    FOR EACH ROW EXECUTE FUNCTION maj_date_modification();