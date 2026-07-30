-- Migration v17 — table pour les données DATAtourisme (officielles,
-- bien plus complètes qu'OpenStreetMap pour hébergement/restauration/
-- activités en France). Remplace Overpass pour ces catégories, sans
-- rien changer au reste de l'app : les résultats sont recopiés dans
-- amenity_cache (même structure que ce qui existait déjà).
CREATE TABLE IF NOT EXISTS datatourisme_objets (
    id              SERIAL PRIMARY KEY,
    identifiant_dt  VARCHAR(100) UNIQUE NOT NULL,
    nom             VARCHAR(255) NOT NULL,
    categorie       VARCHAR(50) NOT NULL,
    commune         VARCHAR(255) NULL,
    departement     VARCHAR(100) NULL,
    latitude        DECIMAL(10,7) NOT NULL,
    longitude       DECIMAL(10,7) NOT NULL,
    site_web        VARCHAR(500) NULL,
    date_maj_dt     TIMESTAMPTZ NULL
);
CREATE INDEX IF NOT EXISTS idx_datatourisme_categorie ON datatourisme_objets (categorie);
CREATE INDEX IF NOT EXISTS idx_datatourisme_coords ON datatourisme_objets (latitude, longitude);
