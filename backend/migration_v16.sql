-- Migration v16 — tracking des clics vers les partenaires (préalable
-- indispensable avant de négocier une vraie commission : il faut
-- pouvoir prouver le trafic généré). Table légère, pas de données
-- personnelles collectées (juste le type de lien cliqué, quand, et
-- vers quoi) — conforme RGPD "minimisation des données".
CREATE TABLE IF NOT EXISTS clics_partenaires (
    id              SERIAL PRIMARY KEY,
    type_lien       VARCHAR(50) NOT NULL,
    nom_partenaire  VARCHAR(255) NULL,
    lieu_tournage_id INT NULL REFERENCES lieux_tournage(id) ON DELETE SET NULL,
    film_id         INT NULL REFERENCES films(id) ON DELETE SET NULL,
    categorie       VARCHAR(50) NULL,
    date_clic       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_clics_partenaires_date ON clics_partenaires (date_clic);
CREATE INDEX IF NOT EXISTS idx_clics_partenaires_type ON clics_partenaires (type_lien, nom_partenaire);

CREATE TABLE IF NOT EXISTS partenaires_sponsorises (
    id              SERIAL PRIMARY KEY,
    nom             VARCHAR(255) NOT NULL,
    categorie       VARCHAR(50) NOT NULL,
    lieu_tournage_id INT NOT NULL REFERENCES lieux_tournage(id) ON DELETE CASCADE,
    lien            VARCHAR(500) NOT NULL,
    actif           BOOLEAN DEFAULT TRUE,
    date_debut      DATE DEFAULT CURRENT_DATE,
    date_fin        DATE NULL
);
