-- ═══════════════════════════════════════════════════════════════
-- CinéTour — Schéma PostgreSQL (Render Postgres)
-- ═══════════════════════════════════════════════════════════════
-- Remplace l'ancien schema.sql (MySQL/université) pour la base LIVE
-- du site. La base universitaire reste utilisable comme espace de
-- travail personnel si tu veux, mais n'est plus dans le chemin du
-- site en production (accès distant impossible depuis phpMyAdmin
-- seul — voir historique de la conversation).
--
-- À exécuter une fois, via l'onglet "Shell" ou "Connect" du dashboard
-- Render Postgres, ou avec psql / un client Postgres depuis ton PC.

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

CREATE TABLE amenity_cache (
    id              SERIAL PRIMARY KEY,
    lieu_tournage_id INT NOT NULL REFERENCES lieux_tournage(id) ON DELETE CASCADE,
    categorie       VARCHAR(20) NOT NULL
                        CHECK (categorie IN ('hebergement','restaurant','office_tourisme',
                                              'police','hopital','gare','aeroport',
                                              'arret_bus','parking')),
    nom             VARCHAR(255) NOT NULL,
    latitude        DECIMAL(10, 7) NOT NULL,
    longitude       DECIMAL(10, 7) NOT NULL,
    distance_metres INT NOT NULL,
    osm_id          BIGINT NULL,
    adresse         VARCHAR(500) NULL,
    telephone       VARCHAR(50) NULL,
    site_web        VARCHAR(500) NULL,
    rang            SMALLINT NOT NULL,
    date_maj        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_lieu_categorie ON amenity_cache (lieu_tournage_id, categorie, rang);

CREATE TABLE amenity_stats (
    id              SERIAL PRIMARY KEY,
    lieu_tournage_id INT NOT NULL REFERENCES lieux_tournage(id) ON DELETE CASCADE,
    categorie       VARCHAR(20) NOT NULL
                        CHECK (categorie IN ('hebergement','restaurant','office_tourisme',
                                              'police','hopital','gare','aeroport',
                                              'arret_bus','parking')),
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

CREATE TABLE datatourisme_objets (
    id              SERIAL PRIMARY KEY,
    identifiant_dt  VARCHAR(95) NOT NULL UNIQUE,
    nom             VARCHAR(255) NOT NULL,
    categorie       VARCHAR(30) NOT NULL DEFAULT 'fetes_manifestations',
    commune         VARCHAR(255) NULL,
    departement     VARCHAR(100) NULL,
    latitude        DECIMAL(10, 7) NOT NULL,
    longitude       DECIMAL(10, 7) NOT NULL,
    adresse         VARCHAR(500) NULL,
    telephone       VARCHAR(50) NULL,
    site_web        VARCHAR(500) NULL,
    description     TEXT NULL,
    photo_url       VARCHAR(500) NULL,
    date_creation   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    date_maj        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_datatourisme_departement ON datatourisme_objets (departement);
CREATE INDEX idx_datatourisme_coords ON datatourisme_objets (latitude, longitude);

-- réutilise le trigger déjà défini dans schema.sql (maj_date_modification)
CREATE TRIGGER trg_datatourisme_objets_date_maj
    BEFORE UPDATE ON datatourisme_objets
    FOR EACH ROW EXECUTE FUNCTION maj_date_modification();




-- Migration : ajoute les champs DATAtourisme jusqu'ici non exploités
-- à la table datatourisme_objets.
-- À exécuter une fois avant de déployer la nouvelle version du script.

ALTER TABLE datatourisme_objets
    ADD COLUMN IF NOT EXISTS uri TEXT,
    ADD COLUMN IF NOT EXISTS types TEXT,                    -- ex: "MusicEvent, Concert, EntertainmentAndEvent"
    ADD COLUMN IF NOT EXISTS insee VARCHAR(10),
    ADD COLUMN IF NOT EXISTS adresse_localite VARCHAR(250),  -- addressLocality (peut différer de "commune")
    ADD COLUMN IF NOT EXISTS photo_credits VARCHAR(495),
    ADD COLUMN IF NOT EXISTS photo_licence VARCHAR(95),      -- ex: "By-NC-ND 4.0"
    ADD COLUMN IF NOT EXISTS organisme_diffuseur VARCHAR(250), -- hasBeenCreatedBy.legalName
    ADD COLUMN IF NOT EXISTS date_maj_source DATE,           -- lastUpdate
    ADD COLUMN IF NOT EXISTS date_maj_datatourisme TIMESTAMPTZ, -- lastUpdateDatatourisme
    ADD COLUMN IF NOT EXISTS description_en TEXT,
    ADD COLUMN IF NOT EXISTS date_debut DATE,                -- première date trouvée dans takesPlaceAt
    ADD COLUMN IF NOT EXISTS date_fin DATE,                  -- dernière date trouvée dans takesPlaceAt
    ADD COLUMN IF NOT EXISTS dates_evenement JSONB;           -- structure brute complète (plusieurs représentations possibles)

-- Index utiles pour les cas d'usage évidents (filtrage par type et par date)
CREATE INDEX IF NOT EXISTS idx_datatourisme_objets_date_debut
    ON datatourisme_objets (date_debut);
CREATE INDEX IF NOT EXISTS idx_datatourisme_objets_insee
    ON datatourisme_objets (insee);