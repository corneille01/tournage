-- Migration v14 — sépare "aérodrome" (terrain d'aviation légère, sans
-- code IATA) de "aéroport" (aéroport commercial, avec code IATA).
ALTER TABLE amenity_cache DROP CONSTRAINT IF EXISTS amenity_cache_categorie_check;
ALTER TABLE amenity_cache ADD CONSTRAINT amenity_cache_categorie_check
    CHECK (categorie IN ('hebergement','refuge','restaurant','office_tourisme',
                          'police','hopital','gare','aeroport','aerodrome',
                          'arret_bus','parking','distributeur','activite'));

ALTER TABLE amenity_stats DROP CONSTRAINT IF EXISTS amenity_stats_categorie_check;
ALTER TABLE amenity_stats ADD CONSTRAINT amenity_stats_categorie_check
    CHECK (categorie IN ('hebergement','refuge','restaurant','office_tourisme',
                          'police','hopital','gare','aeroport','aerodrome',
                          'arret_bus','parking','distributeur','activite'));
