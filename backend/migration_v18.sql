-- Migration v18 — enrichit datatourisme_objets avec les champs
-- réellement présents dans les exports JSON-LD complets (contact,
-- tarifs, horaires, adresse détaillée, type de cuisine pour les
-- restaurants) — la Description, les équipements et les photos ne se
-- résolvent à aucune donnée dans ces exports (vérifié sur 500
-- échantillons), pas de colonne pour l'instant.
ALTER TABLE datatourisme_objets ADD COLUMN IF NOT EXISTS adresse VARCHAR(500) NULL;
ALTER TABLE datatourisme_objets ADD COLUMN IF NOT EXISTS telephone VARCHAR(50) NULL;
ALTER TABLE datatourisme_objets ADD COLUMN IF NOT EXISTS email VARCHAR(255) NULL;
ALTER TABLE datatourisme_objets ADD COLUMN IF NOT EXISTS horaires VARCHAR(500) NULL;
ALTER TABLE datatourisme_objets ADD COLUMN IF NOT EXISTS tarif_min DECIMAL(10,2) NULL;
ALTER TABLE datatourisme_objets ADD COLUMN IF NOT EXISTS tarif_max DECIMAL(10,2) NULL;
ALTER TABLE datatourisme_objets ADD COLUMN IF NOT EXISTS devise VARCHAR(10) NULL;
ALTER TABLE datatourisme_objets ADD COLUMN IF NOT EXISTS classification VARCHAR(255) NULL;
ALTER TABLE datatourisme_objets ADD COLUMN IF NOT EXISTS cuisine VARCHAR(255) NULL;

ALTER TABLE amenity_cache ADD COLUMN IF NOT EXISTS email VARCHAR(255) NULL;
ALTER TABLE amenity_cache ADD COLUMN IF NOT EXISTS tarif_min DECIMAL(10,2) NULL;
ALTER TABLE amenity_cache ADD COLUMN IF NOT EXISTS tarif_max DECIMAL(10,2) NULL;
ALTER TABLE amenity_cache ADD COLUMN IF NOT EXISTS devise VARCHAR(10) NULL;
