-- ═══════════════════════════════════════════════════════════════
-- Migration v2 — à exécuter une fois dans le SQL Editor de Neon,
-- APRÈS schema.sql (ne remplace rien, ajoute des colonnes/index).
-- ═══════════════════════════════════════════════════════════════

-- QID Wikidata du LIEU lui-même (différent de celui du film) —
-- nécessaire pour aller chercher sa photo (P18) spécifiquement.
ALTER TABLE lieux_tournage ADD COLUMN IF NOT EXISTS wikidata_qid VARCHAR(20) NULL;

-- Cache des plateformes de streaming (TMDB watch/providers), en JSON
-- brut — évite de rappeler l'API TMDB à chaque visite.
ALTER TABLE films ADD COLUMN IF NOT EXISTS plateformes_json JSONB NULL;
ALTER TABLE films ADD COLUMN IF NOT EXISTS plateformes_maj TIMESTAMP NULL;

-- Index utiles pour les nouveaux filtres (année, département, recherche)
CREATE INDEX IF NOT EXISTS idx_annee ON films (annee);
CREATE INDEX IF NOT EXISTS idx_departement ON lieux_tournage (departement);
CREATE INDEX IF NOT EXISTS idx_titre_recherche ON films USING gin (to_tsvector('french', titre));

-- Nouvelle catégorie 'activite' (attractions touristiques, pour le
-- bouton "que faire aux alentours") — il faut élargir les contraintes
-- CHECK existantes qui n'autorisaient pas encore cette valeur.
ALTER TABLE amenity_cache DROP CONSTRAINT IF EXISTS amenity_cache_categorie_check;
ALTER TABLE amenity_cache ADD CONSTRAINT amenity_cache_categorie_check
    CHECK (categorie IN ('hebergement','restaurant','office_tourisme',
                          'police','hopital','gare','aeroport',
                          'arret_bus','parking','activite'));

ALTER TABLE amenity_stats DROP CONSTRAINT IF EXISTS amenity_stats_categorie_check;
ALTER TABLE amenity_stats ADD CONSTRAINT amenity_stats_categorie_check
    CHECK (categorie IN ('hebergement','restaurant','office_tourisme',
                          'police','hopital','gare','aeroport',
                          'arret_bus','parking','activite'));
