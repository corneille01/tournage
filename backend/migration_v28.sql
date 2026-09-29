-- Migration v28 — provenance et synchronisation DATAtourisme
ALTER TABLE guides ADD COLUMN IF NOT EXISTS source_donnee VARCHAR(30) NOT NULL DEFAULT 'manuel';
ALTER TABLE guides ADD COLUMN IF NOT EXISTS datatourisme_uuid VARCHAR(120);
ALTER TABLE guides ADD COLUMN IF NOT EXISTS datatourisme_type VARCHAR(120);
ALTER TABLE guides ADD COLUMN IF NOT EXISTS datatourisme_last_update TIMESTAMPTZ;
ALTER TABLE guides ADD COLUMN IF NOT EXISTS datatourisme_creator TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS idx_guides_datatourisme_uuid
    ON guides (datatourisme_uuid) WHERE datatourisme_uuid IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_guides_source_donnee ON guides (source_donnee);
CREATE INDEX IF NOT EXISTS idx_guides_datatourisme_type ON guides (datatourisme_type);
