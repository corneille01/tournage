-- Migration v30 - Stations-service comme commodité + champs complets de l'API carburants
-- Idempotente : peut être rejouée sans risque.
-- Prérequis : migration_v29.sql (table stations_carburant).

-- 1) Champs de la source qui n'étaient pas encore conservés
--    (com_arm_code, epci_code, epci_name, reg_code).
ALTER TABLE stations_carburant ADD COLUMN IF NOT EXISTS code_insee   VARCHAR(10);
ALTER TABLE stations_carburant ADD COLUMN IF NOT EXISTS epci_code    VARCHAR(20);
ALTER TABLE stations_carburant ADD COLUMN IF NOT EXISTS epci_nom     TEXT;
ALTER TABLE stations_carburant ADD COLUMN IF NOT EXISTS region_code  VARCHAR(5);

-- 2) Lien amenity_cache -> stations_carburant.
--    Le précalcul (distance, rang) vit dans amenity_cache, comme pour les
--    autres commodités ; les prix, horaires et services sont lus à la demande
--    dans stations_carburant, donc toujours à jour de l'import quotidien.
ALTER TABLE amenity_cache ADD COLUMN IF NOT EXISTS station_id BIGINT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_amenity_cache_station
    ON amenity_cache (lieu_tournage_id, station_id)
    WHERE station_id IS NOT NULL;

-- 3) Nouvelle catégorie 'station_service' (15 caractères, tient dans VARCHAR(20)).
ALTER TABLE amenity_cache DROP CONSTRAINT IF EXISTS amenity_cache_categorie_check;
ALTER TABLE amenity_cache ADD CONSTRAINT amenity_cache_categorie_check
    CHECK (categorie IN ('hebergement','refuge','restaurant','office_tourisme',
                          'police','hopital','gare','aeroport','aerodrome',
                          'arret_bus','parking','distributeur','activite',
                          'fetes_manifestations','station_service'));
