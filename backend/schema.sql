-- ═══════════════════════════════════════════════════════════════
-- CinéTour — Schéma PostgreSQL (Render Postgres)
-- ═══════════════════════════════════════════════════════════════
--
-- ⚠️ LIS CECI AVANT D'UTILISER CE FICHIER ⚠️
--
-- Ce fichier est une RECONSTITUTION, pas un export fiable à 100 %.
-- Il part de l'ancien schema.sql du repo et y ajoute tout ce qu'on a
-- découvert dans cette conversation :
--   - colonnes lues/écrites par calculer_amenities_datatourisme.py
--     et enrich_itineraires.py mais absentes de l'ancien fichier
--   - contraintes CHECK réelles, copiées telles quelles depuis
--     pg_constraint / pg_get_constraintdef (donc CELLES-LÀ sont
--     fiables, tu les as vérifiées toi-même)
--   - la contrainte UNIQUE ajoutée sur amenity_cache dans la
--     dernière migration
--
-- Ce qui N'EST PAS fiable : les TYPES exacts (VARCHAR(n) vs TEXT,
-- INT vs DECIMAL, NULL/NOT NULL) des colonnes ajoutées à la main sur
-- la base au fil du temps sans jamais être commitées ici — je les ai
-- déduites du nom et de l'usage dans le code, pas vérifiées contre
-- la base. Marquées [DÉDUIT] ci-dessous.
--
-- Pour une vérité à 100 %, génère le vrai schéma depuis la base et
-- remplace ce fichier par sa sortie :
--
--   pg_dump "$DATABASE_URL" --schema-only --no-owner --no-privileges > schema.sql
--
-- (à lancer depuis un poste ayant pg_dump installé et l'accès réseau
-- à Neon/Render ; sinon, `\d+ nom_table` dans le Shell Render/psql
-- pour chaque table donne les types exacts un par un.)
-- ═══════════════════════════════════════════════════════════════

-- Postgres n'a pas d'ENUM inline comme MySQL — on utilise VARCHAR +
-- CHECK, plus simple à faire évoluer sans migration de type.

CREATE TABLE films (
    id              SERIAL PRIMARY KEY,
    tmdb_id         INT NULL,
    wikidata_qid    VARCHAR(20) NULL UNIQUE,
    titre           VARCHAR(255) NOT NULL,
    titre_original  VARCHAR(255) NULL,
    media_type      VARCHAR(10) NOT NULL DEFAULT 'movie'
                        CHECK (media_type IN ('movie','tv','anime')),
    annee           SMALLINT NULL,
    synopsis        TEXT NULL,
    poster_url      VARCHAR(500) NULL,
    region          VARCHAR(100) NOT NULL DEFAULT 'Occitanie',
    source_donnee   VARCHAR(20) NOT NULL DEFAULT 'manuel'
                        CHECK (source_donnee IN ('wikidata','manuel','lieuxtournage','autre')),
    statut          VARCHAR(10) NOT NULL DEFAULT 'brouillon'
                        CHECK (statut IN ('brouillon','publie')),
    date_creation   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    date_maj        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_region_statut ON films (region, statut);
CREATE INDEX idx_media_type ON films (media_type);
CREATE INDEX idx_tmdb ON films (tmdb_id);

CREATE TABLE lieux_tournage (
    id              SERIAL PRIMARY KEY,
    film_id         INT NOT NULL REFERENCES films(id) ON DELETE CASCADE,
    nom             VARCHAR(255) NOT NULL,
    description     TEXT NULL,
    commune         VARCHAR(150) NULL,
    departement     VARCHAR(100) NULL,
    latitude        DECIMAL(10, 7) NOT NULL,
    longitude       DECIMAL(10, 7) NOT NULL,
    photo_url       VARCHAR(500) NULL,
    date_creation   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_film ON lieux_tournage (film_id);
CREATE INDEX idx_coords ON lieux_tournage (latitude, longitude);

-- ─────────────────────────────────────────────────────────────
-- amenity_cache
-- Colonnes d'origine confirmées par l'ancien schema.sql + celles
-- lues/écrites par calculer_amenities_datatourisme.py (INSERT) et
-- enrich_itineraires.py (UPDATE distance_{mode}/duree_{mode}).
-- La contrainte CHECK ci-dessous est copiée telle quelle depuis ta
-- vérification pg_constraint — fiable. La contrainte UNIQUE est
-- celle ajoutée par la migration de cette conversation.
-- ─────────────────────────────────────────────────────────────
CREATE TABLE amenity_cache (
    id                      SERIAL PRIMARY KEY,
    lieu_tournage_id        INT NOT NULL REFERENCES lieux_tournage(id) ON DELETE CASCADE,
    categorie               VARCHAR(20) NOT NULL
                                CHECK (categorie IN (
                                    'hebergement', 'restaurant', 'office_tourisme', 'police',
                                    'hopital', 'gare', 'aeroport', 'aerodrome', 'arret_bus',
                                    'parking', 'distributeur', 'activite', 'refuge',
                                    'fetes_manifestations'
                                )),  -- confirmé via pg_constraint (amenity_cache_categorie_check)
    nom                     VARCHAR(255) NOT NULL,
    latitude                DECIMAL(10, 7) NOT NULL,
    longitude               DECIMAL(10, 7) NOT NULL,
    distance_metres         INT NOT NULL,
    osm_id                  BIGINT NULL,
    adresse                 VARCHAR(500) NULL,
    telephone               VARCHAR(50) NULL,
    site_web                VARCHAR(500) NULL,
    rang                    SMALLINT NOT NULL,

    -- [DÉDUIT] alimentées par calculer_amenities_datatourisme.py,
    -- types/longueurs non vérifiés contre la base réelle :
    email                   VARCHAR(255) NULL,
    horaires                TEXT NULL,
    tarif_min               DECIMAL(10, 2) NULL,
    tarif_max               DECIMAL(10, 2) NULL,
    devise                  VARCHAR(10) NULL,
    photo_url               VARCHAR(500) NULL,
    equipements             TEXT NULL,
    capacite                INT NULL,
    note_etoiles            DECIMAL(2, 1) NULL,
    labels_qualite          TEXT NULL,
    lien_accessibilite      VARCHAR(500) NULL,
    langues_parlees         VARCHAR(255) NULL,
    description             TEXT NULL,
    moyens_paiement         VARCHAR(255) NULL,
    note_tarif              VARCHAR(50) NULL,

    -- [DÉDUIT] alimentées par enrich_itineraires.py (Géoplateforme IGN),
    -- NULL tant que le précalcul n'est pas passé sur cette ligne :
    distance_pied_metres    INT NULL,
    duree_pied_secondes     INT NULL,
    distance_voiture_metres INT NULL,
    duree_voiture_secondes  INT NULL,

    date_maj                TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    -- Ajoutée par la migration "migration_amenity_cache.sql" de
    -- cette conversation — requise par l'ON CONFLICT de
    -- calculer_amenities_datatourisme.py, absente jusqu'ici.
    CONSTRAINT amenity_cache_lieu_categorie_nom_latlon_key
        UNIQUE (lieu_tournage_id, categorie, nom, latitude, longitude)
);
CREATE INDEX idx_lieu_categorie ON amenity_cache (lieu_tournage_id, categorie, rang);

-- ─────────────────────────────────────────────────────────────
-- amenity_stats
-- Contrainte CHECK copiée telle quelle depuis ta vérification
-- pg_constraint (amenity_stats_categorie_check) — noter qu'elle
-- diffère légèrement de celle d'amenity_cache (les deux listes ont
-- dérivé indépendamment ; aucune n'a été harmonisée ici, on reflète
-- juste l'état réel constaté).
-- ─────────────────────────────────────────────────────────────
CREATE TABLE amenity_stats (
    id              SERIAL PRIMARY KEY,
    lieu_tournage_id INT NOT NULL REFERENCES lieux_tournage(id) ON DELETE CASCADE,
    categorie       VARCHAR(20) NOT NULL
                        CHECK (categorie IN (
                            'hebergement', 'refuge', 'restaurant', 'office_tourisme',
                            'police', 'hopital', 'gare', 'aeroport', 'aerodrome',
                            'arret_bus', 'parking', 'distributeur', 'activite',
                            'fetes_manifestations'
                        )),  -- confirmé via pg_constraint + 'fetes_manifestations'
                            -- ajouté par la migration de cette conversation
    rayon_metres    INT NOT NULL,
    nombre_total    INT NOT NULL,
    nombre_500m     INT NOT NULL,
    nombre_1000m    INT NOT NULL,
    distance_min_m  INT NULL,
    distance_moy_top10_m INT NULL,
    date_maj        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (lieu_tournage_id, categorie)
);

CREATE VIEW v_plus_proche AS
SELECT lieu_tournage_id, categorie, nom, distance_metres
FROM amenity_cache
WHERE rang = 1;

-- ── Mise à jour automatique de date_maj (Postgres n'a pas d'équivalent
-- direct à "ON UPDATE CURRENT_TIMESTAMP" de MySQL, on le fait via trigger) ──
CREATE OR REPLACE FUNCTION maj_date_modification()
RETURNS TRIGGER AS $$
BEGIN
    NEW.date_maj = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_films_date_maj
    BEFORE UPDATE ON films
    FOR EACH ROW EXECUTE FUNCTION maj_date_modification();

CREATE TRIGGER trg_amenity_cache_date_maj
    BEFORE UPDATE ON amenity_cache
    FOR EACH ROW EXECUTE FUNCTION maj_date_modification();

CREATE TRIGGER trg_amenity_stats_date_maj
    BEFORE UPDATE ON amenity_stats
    FOR EACH ROW EXECUTE FUNCTION maj_date_modification();

CREATE TABLE sync_state (
    cle     VARCHAR(100) PRIMARY KEY,
    valeur  TEXT NULL,
    maj     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ─────────────────────────────────────────────────────────────
-- datatourisme_objets
-- Table d'origine + toutes les colonnes ajoutées par la migration
-- "migration_champs_manquants.sql" de cette conversation, déjà
-- fusionnées ici (plus besoin de les lancer séparément si tu repars
-- de ce fichier pour une base neuve).
-- ─────────────────────────────────────────────────────────────
CREATE TABLE datatourisme_objets (
    id                      SERIAL PRIMARY KEY,
    identifiant_dt          VARCHAR(95) NOT NULL UNIQUE,
    nom                     VARCHAR(255) NOT NULL,
    categorie               VARCHAR(30) NOT NULL DEFAULT 'fetes_manifestations',
    commune                 VARCHAR(255) NULL,
    departement             VARCHAR(100) NULL,
    latitude                DECIMAL(10, 7) NOT NULL,
    longitude               DECIMAL(10, 7) NOT NULL,
    adresse                 VARCHAR(500) NULL,
    telephone               VARCHAR(50) NULL,
    site_web                VARCHAR(500) NULL,
    description             TEXT NULL,
    photo_url               VARCHAR(500) NULL,
    date_creation           TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    date_maj                TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    -- Ajoutées par migration_champs_manquants.sql :
    uri                     TEXT NULL,
    types                   TEXT NULL,       -- ex: "MusicEvent, Concert, EntertainmentAndEvent"
    insee                   VARCHAR(10) NULL,
    adresse_localite        VARCHAR(250) NULL, -- addressLocality (peut différer de "commune")
    photo_credits           VARCHAR(495) NULL,
    photo_licence           VARCHAR(95) NULL,  -- ex: "By-NC-ND 4.0"
    organisme_diffuseur     VARCHAR(250) NULL, -- hasBeenCreatedBy.legalName
    date_maj_source         DATE NULL,         -- lastUpdate
    date_maj_datatourisme   TIMESTAMPTZ NULL,  -- lastUpdateDatatourisme
    description_en          TEXT NULL,
    date_debut              DATE NULL,         -- reste NULL : takesPlaceAt absent de cette API REST
    date_fin                DATE NULL,         -- idem
    dates_evenement         JSONB NULL         -- idem
);
CREATE INDEX idx_datatourisme_departement ON datatourisme_objets (departement);
CREATE INDEX idx_datatourisme_coords ON datatourisme_objets (latitude, longitude);
CREATE INDEX idx_datatourisme_objets_date_debut ON datatourisme_objets (date_debut);
CREATE INDEX idx_datatourisme_objets_insee ON datatourisme_objets (insee);

CREATE TRIGGER trg_datatourisme_objets_date_maj
    BEFORE UPDATE ON datatourisme_objets
    FOR EACH ROW EXECUTE FUNCTION maj_date_modification();