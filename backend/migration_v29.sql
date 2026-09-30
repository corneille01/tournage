-- Migration v29 - prix des carburants + adresse des guides
-- Idempotente : peut être rejouée sans risque.

CREATE TABLE IF NOT EXISTS stations_carburant (
    id                 BIGINT PRIMARY KEY,           -- identifiant officiel du point de vente
    latitude           DOUBLE PRECISION NOT NULL,
    longitude          DOUBLE PRECISION NOT NULL,
    nom                TEXT,
    marque             TEXT,
    adresse            TEXT,
    code_postal        VARCHAR(10),
    commune            TEXT,
    dep_code           VARCHAR(5),
    dep_nom            TEXT,
    region             TEXT,
    type_route         VARCHAR(1),                   -- 'R' route, 'A' autoroute
    automate_24_24     BOOLEAN,
    horaires           JSONB,
    carburants_dispo   TEXT[] NOT NULL DEFAULT '{}',
    carburants_rupture TEXT[] NOT NULL DEFAULT '{}',
    prix_gazole        NUMERIC(6,3),
    prix_sp95          NUMERIC(6,3),
    prix_sp98          NUMERIC(6,3),
    prix_e10           NUMERIC(6,3),
    prix_e85           NUMERIC(6,3),
    prix_gplc          NUMERIC(6,3),
    maj_prix           TIMESTAMPTZ,                  -- dernière mise à jour des prix côté source (champ « update »)
    maj_carburants     JSONB,                        -- dates par carburant, si l'API les fournit
    services           TEXT[] NOT NULL DEFAULT '{}',
    synchro_at         TIMESTAMPTZ NOT NULL DEFAULT now()  -- dernière synchro Pelify
);

ALTER TABLE stations_carburant ADD COLUMN IF NOT EXISTS maj_carburants JSONB;

CREATE INDEX IF NOT EXISTS idx_stations_carburant_geo ON stations_carburant (latitude, longitude);
CREATE INDEX IF NOT EXISTS idx_stations_carburant_dep ON stations_carburant (dep_code);
CREATE INDEX IF NOT EXISTS idx_stations_carburant_synchro ON stations_carburant (synchro_at);

-- Localisation lisible des guides (renseignée à l'import ou, à défaut,
-- par géocodage inverse IGN la première fois qu'un guide est recommandé).
ALTER TABLE guides ADD COLUMN IF NOT EXISTS adresse TEXT;
ALTER TABLE guides ADD COLUMN IF NOT EXISTS commune TEXT;
ALTER TABLE guides ADD COLUMN IF NOT EXISTS code_postal VARCHAR(10);