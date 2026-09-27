-- Migration v25
--
-- 1) FIX BLOQUANT : reseaux_sociaux est écrit par import_datatourisme_api.py
--    (INSERT INTO datatourisme_objets ... reseaux_sociaux ...) mais cette
--    colonne n'a jamais été créée dans aucune migration versionnée. Sans
--    elle, l'import plante dès le premier POI de chaque page et interrompt
--    toute la synchro en cours (voir conversation de debug).
ALTER TABLE datatourisme_objets ADD COLUMN IF NOT EXISTS reseaux_sociaux JSONB NULL;

-- 2) Champs déjà extraits et stockés dans datatourisme_objets mais jamais
--    recopiés dans amenity_cache par calculer_amenities_datatourisme.py —
--    donc jamais visibles côté frontend, faute de colonnes en face.
ALTER TABLE amenity_cache ADD COLUMN IF NOT EXISTS types VARCHAR(500) NULL;
ALTER TABLE amenity_cache ADD COLUMN IF NOT EXISTS organisme_diffuseur VARCHAR(250) NULL;
ALTER TABLE amenity_cache ADD COLUMN IF NOT EXISTS reseaux_sociaux JSONB NULL;
ALTER TABLE amenity_cache ADD COLUMN IF NOT EXISTS photo_credits VARCHAR(495) NULL;
ALTER TABLE amenity_cache ADD COLUMN IF NOT EXISTS photo_licence VARCHAR(95) NULL;