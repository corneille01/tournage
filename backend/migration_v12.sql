-- Migration v12 — plusieurs photos ET vidéos par lieu (au lieu d'une
-- seule photo_url). Table séparée : plus flexible, pas de limite au
-- nombre de médias, chacun avec sa propre source.
CREATE TABLE IF NOT EXISTS lieu_medias (
    id              SERIAL PRIMARY KEY,
    lieu_tournage_id INT NOT NULL REFERENCES lieux_tournage(id) ON DELETE CASCADE,
    type_media      VARCHAR(10) NOT NULL CHECK (type_media IN ('photo', 'video')),
    url             VARCHAR(500) NOT NULL,
    legende         VARCHAR(255) NULL,
    source          VARCHAR(500) NULL,
    ordre           SMALLINT NOT NULL DEFAULT 0,
    date_creation   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_lieu_medias ON lieu_medias (lieu_tournage_id, ordre);

-- La colonne photo_url de lieux_tournage n'est pas supprimée (garde la
-- compatibilité avec ce qui existe déjà) — à terme, tu peux migrer son
-- contenu vers cette nouvelle table avec :
INSERT INTO lieu_medias (lieu_tournage_id, type_media, url, ordre)
SELECT id, 'photo', photo_url, 0
FROM lieux_tournage
WHERE photo_url IS NOT NULL;
