-- Migration v15 — capacité d'accueil (tag OSM "capacity", ex: nombre
-- de chambres/lits) quand elle est renseignée. Souvent absente sur
-- OSM pour les petits établissements — normal de voir beaucoup de
-- NULL, ce n'est pas un manque de notre côté.
ALTER TABLE amenity_cache ADD COLUMN IF NOT EXISTS capacite INT NULL;
