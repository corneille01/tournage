-- Migration v10 — distances/durées piéton et voiture précalculées par
-- commodité, pour ne plus jamais appeler OSRM sur une requête visiteur
-- (essentiel vu le trafic visé). Calculées par enrich_itineraires.py,
-- pas à la volée.
ALTER TABLE amenity_cache ADD COLUMN IF NOT EXISTS distance_pied_metres INT NULL;
ALTER TABLE amenity_cache ADD COLUMN IF NOT EXISTS duree_pied_secondes INT NULL;
ALTER TABLE amenity_cache ADD COLUMN IF NOT EXISTS distance_voiture_metres INT NULL;
ALTER TABLE amenity_cache ADD COLUMN IF NOT EXISTS duree_voiture_secondes INT NULL;
