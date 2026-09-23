-- ============================================================
-- Pelify Landscape V1 — Gestion structurée des licences
-- Migration V22
-- ============================================================

ALTER TABLE paysages
    ADD COLUMN IF NOT EXISTS source_identifier VARCHAR(255),
    ADD COLUMN IF NOT EXISTS licence_version VARCHAR(50),
    ADD COLUMN IF NOT EXISTS usage_commercial BOOLEAN,
    ADD COLUMN IF NOT EXISTS modification_autorisee BOOLEAN,
    ADD COLUMN IF NOT EXISTS attribution_requise BOOLEAN,
    ADD COLUMN IF NOT EXISTS licence_code VARCHAR(50),
    ADD COLUMN IF NOT EXISTS verification_statut VARCHAR(30)
        NOT NULL DEFAULT 'a_verifier',
    ADD COLUMN IF NOT EXISTS verification_date TIMESTAMP NULL,
    ADD COLUMN IF NOT EXISTS dimensions_largeur INTEGER NULL,
    ADD COLUMN IF NOT EXISTS dimensions_hauteur INTEGER NULL;

-- Les valeurs de vérification correspondent à l'état des
-- informations disponibles, pas à une garantie juridique.
ALTER TABLE paysages
    DROP CONSTRAINT IF EXISTS paysages_verification_statut_check;

ALTER TABLE paysages
    ADD CONSTRAINT paysages_verification_statut_check
    CHECK (
        verification_statut IN (
            'a_verifier',
            'metadata_openverse',
            'verifie_manuellement',
            'droits_confirmes',
            'droits_refuses'
        )
    );

CREATE INDEX IF NOT EXISTS idx_paysages_source_identifier
    ON paysages(source_type, source_identifier);

CREATE INDEX IF NOT EXISTS idx_paysages_licence_code
    ON paysages(licence_code);

CREATE INDEX IF NOT EXISTS idx_paysages_usage_commercial
    ON paysages(usage_commercial);

CREATE INDEX IF NOT EXISTS idx_paysages_verification_statut
    ON paysages(verification_statut);

-- Les anciennes fiches restent prudentes.
UPDATE paysages
SET verification_statut = 'a_verifier'
WHERE verification_statut IS NULL;

-- Les anciennes fiches Openverse doivent être considérées
-- comme provenant de métadonnées externes.
UPDATE paysages
SET verification_statut = 'metadata_openverse'
WHERE source_type = 'openverse'
  AND licence IS NOT NULL
  AND verification_statut = 'a_verifier';