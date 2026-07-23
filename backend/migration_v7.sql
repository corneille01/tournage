-- Migration v7 :
--  - nouvelle catégorie 'distributeur' (billets/banques)
--  - horaires d'ouverture (déjà dans les tags OSM, aucun appel en plus)
--  - photo par commodité (remplie séparément par enrich_photos.py,
--    PAS pendant refresh_cache.py, pour ne pas aggraver les temps
--    d'exécution déjà tendus avec Overpass)
ALTER TABLE amenity_cache DROP CONSTRAINT IF EXISTS amenity_cache_categorie_check;
ALTER TABLE amenity_cache ADD CONSTRAINT amenity_cache_categorie_check
    CHECK (categorie IN ('hebergement','refuge','restaurant','office_tourisme',
                          'police','hopital','gare','aeroport',
                          'arret_bus','parking','distributeur','activite'));

ALTER TABLE amenity_stats DROP CONSTRAINT IF EXISTS amenity_stats_categorie_check;
ALTER TABLE amenity_stats ADD CONSTRAINT amenity_stats_categorie_check
    CHECK (categorie IN ('hebergement','refuge','restaurant','office_tourisme',
                          'police','hopital','gare','aeroport',
                          'arret_bus','parking','distributeur','activite'));

ALTER TABLE amenity_cache ADD COLUMN IF NOT EXISTS horaires VARCHAR(200) NULL;
ALTER TABLE amenity_cache ADD COLUMN IF NOT EXISTS photo_url VARCHAR(500) NULL;
