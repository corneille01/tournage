-- Migration v24
-- 1) Filet de sécurité idempotent : ces colonnes sont utilisées par le
--    code (import_datatourisme_jsonld.py, calculer_amenities_datatourisme.py,
--    main.py) mais n'apparaissaient dans aucune migration versionnée.
--    Si elles existent déjà en base, ces lignes ne font rien.
ALTER TABLE amenity_cache ADD COLUMN IF NOT EXISTS equipements VARCHAR(500) NULL;
ALTER TABLE amenity_cache ADD COLUMN IF NOT EXISTS note_etoiles DECIMAL(3,1) NULL;
ALTER TABLE amenity_cache ADD COLUMN IF NOT EXISTS labels_qualite VARCHAR(500) NULL;
ALTER TABLE amenity_cache ADD COLUMN IF NOT EXISTS lien_accessibilite VARCHAR(500) NULL;
ALTER TABLE amenity_cache ADD COLUMN IF NOT EXISTS langues_parlees VARCHAR(255) NULL;
ALTER TABLE amenity_cache ADD COLUMN IF NOT EXISTS description TEXT NULL;
ALTER TABLE amenity_cache ADD COLUMN IF NOT EXISTS moyens_paiement VARCHAR(255) NULL;
ALTER TABLE amenity_cache ADD COLUMN IF NOT EXISTS note_tarif VARCHAR(255) NULL;

ALTER TABLE datatourisme_objets ADD COLUMN IF NOT EXISTS photo_url VARCHAR(500) NULL;
ALTER TABLE datatourisme_objets ADD COLUMN IF NOT EXISTS equipements VARCHAR(500) NULL;
ALTER TABLE datatourisme_objets ADD COLUMN IF NOT EXISTS capacite INT NULL;
ALTER TABLE datatourisme_objets ADD COLUMN IF NOT EXISTS note_etoiles DECIMAL(3,1) NULL;
ALTER TABLE datatourisme_objets ADD COLUMN IF NOT EXISTS labels_qualite VARCHAR(500) NULL;
ALTER TABLE datatourisme_objets ADD COLUMN IF NOT EXISTS lien_accessibilite VARCHAR(500) NULL;
ALTER TABLE datatourisme_objets ADD COLUMN IF NOT EXISTS langues_parlees VARCHAR(255) NULL;
ALTER TABLE datatourisme_objets ADD COLUMN IF NOT EXISTS description TEXT NULL;
ALTER TABLE datatourisme_objets ADD COLUMN IF NOT EXISTS moyens_paiement VARCHAR(255) NULL;
ALTER TABLE datatourisme_objets ADD COLUMN IF NOT EXISTS note_tarif VARCHAR(255) NULL;

-- 2) Clé d'identité stable pour pouvoir faire un UPSERT au lieu d'un
--    DELETE+INSERT quotidien dans calculer_amenities_datatourisme.py
--    (qui détruisait sinon les distances piéton/voiture déjà calculées).
CREATE UNIQUE INDEX IF NOT EXISTS idx_amenity_cache_identite
    ON amenity_cache (lieu_tournage_id, categorie, nom, latitude, longitude);

-- 3) Nouvelle catégorie "fêtes et manifestations".
ALTER TABLE amenity_cache DROP CONSTRAINT IF EXISTS amenity_cache_categorie_check;
ALTER TABLE amenity_cache ADD CONSTRAINT amenity_cache_categorie_check
    CHECK (categorie IN ('hebergement','refuge','restaurant','office_tourisme',
                          'police','hopital','gare','aeroport','aerodrome',
                          'arret_bus','parking','distributeur','activite',
                          'fetes_manifestations'));

ALTER TABLE amenity_stats DROP CONSTRAINT IF EXISTS amenity_stats_categorie_check;
ALTER TABLE amenity_stats ADD CONSTRAINT amenity_stats_categorie_check
    CHECK (categorie IN ('hebergement','refuge','restaurant','office_tourisme',
                          'police','hopital','gare','aeroport','aerodrome',
                          'arret_bus','parking','distributeur','activite',
                          'fetes_manifestations'));

-- 4) Petite table de curseur générique, pour reprendre une synchro API
--    paginée là où elle s'est arrêtée (utilisée par import_datatourisme_api.py).
CREATE TABLE IF NOT EXISTS sync_state (
    cle     VARCHAR(100) PRIMARY KEY,
    valeur  TEXT NULL,
    maj     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);