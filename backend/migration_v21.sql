-- Pelify V4.9 — extension "Landscape Studio" pour la projection sur
-- dôme 3D : format visuel disponible par paysage (photo / panorama /
-- 360° / vidéo 360° / à capturer), distinction référence artistique
-- vs image réutilisable, et intention artistique par scène.
-- Additive uniquement : ne touche à aucune colonne existante de la
-- migration_v21 (droits_a_verifier reste tel quel, en complément de
-- statut_droits qui est plus précis).
ALTER TABLE paysages
ADD COLUMN IF NOT EXISTS media_type VARCHAR(20) NOT NULL DEFAULT 'photo'
CHECK (media_type IN ('photo', 'panorama', '360', 'video_360', 'a_capturer')),
ADD COLUMN IF NOT EXISTS type_reference VARCHAR(20) NOT NULL DEFAULT 'open_source'
CHECK (type_reference IN ('personnelle', 'artiste', 'open_source', 'panorama_360')),
ADD COLUMN IF NOT EXISTS statut_droits VARCHAR(20) NOT NULL DEFAULT 'a_verifier'
CHECK (statut_droits IN ('utilisable', 'a_negocier', 'libre_licence', 'a_capturer', 'a_verifier')),
ADD COLUMN IF NOT EXISTS artiste_nom VARCHAR(255),
ADD COLUMN IF NOT EXISTS artiste_contact VARCHAR(255);
CREATE INDEX IF NOT EXISTS idx_paysages_media_type ON paysages(media_type);
CREATE INDEX IF NOT EXISTS idx_paysages_statut_droits ON paysages(statut_droits);
ALTER TABLE paysage_scenes
ADD COLUMN IF NOT EXISTS intention_artistique TEXT,
ADD COLUMN IF NOT EXISTS format_souhaite VARCHAR(20) NOT NULL DEFAULT 'peu_importe'
CHECK (format_souhaite IN ('photo', 'panorama', '360', 'video_360', 'peu_importe'));
-- Un paysage "artiste" (Benoit Colomb et consorts) ne doit jamais être
-- considéré comme utilisable par défaut : les droits sont toujours à
-- négocier tant que personne ne l'a explicitement validé.
UPDATE paysages
SET statut_droits = 'a_negocier'
WHERE type_reference = 'artiste' AND statut_droits = 'a_verifier';