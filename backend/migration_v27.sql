-- Migration v27
--
-- Fait passer les visites cinétouristiques de "codées en dur dans
-- backend/visites_cinetouristiques.py" à "100% base de données".
--
-- ⚠️ Le fichier .py qui contenait la liste VISITES=[...] (6 entrées :
-- Collioure, 3 visites Sète/DNA, Montpellier USGS, Palavas) a été
-- réécrit pour ne plus lire QUE cette table. Cette migration reprend
-- ces 6 entrées telles quelles dans les INSERT ci-dessous, pour ne
-- rien perdre au passage.
--
-- creneaux       : JSONB, liste de [date_iso, heure_debut, heure_fin]
-- creneaux_regles: JSONB, liste de {debut, fin, jours:[0=lundi..6=dimanche], start, end}

CREATE TABLE IF NOT EXISTS visites_cinetouristiques (
    id                SERIAL PRIMARY KEY,
    slug              VARCHAR(150) NOT NULL UNIQUE,
    film_ids          INT[] NOT NULL DEFAULT '{}',
    nom               VARCHAR(255) NOT NULL,
    description       TEXT NULL,
    duree_minutes     INT NOT NULL,
    lien              VARCHAR(500) NULL,
    creneaux          JSONB NOT NULL DEFAULT '[]',
    creneaux_regles   JSONB NOT NULL DEFAULT '[]',
    statut            VARCHAR(15) NOT NULL DEFAULT 'actif'
                          CHECK (statut IN ('actif', 'inactif', 'en_attente')),
    date_creation     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    date_maj          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_visites_statut ON visites_cinetouristiques (statut);
CREATE INDEX IF NOT EXISTS idx_visites_film_ids ON visites_cinetouristiques USING GIN (film_ids);

DROP TRIGGER IF EXISTS trg_visites_date_maj ON visites_cinetouristiques;
CREATE TRIGGER trg_visites_date_maj
    BEFORE UPDATE ON visites_cinetouristiques
    FOR EACH ROW EXECUTE FUNCTION maj_date_modification();

-- ── Reprise des 6 visites précédemment codées en dur ──────────────
-- ON CONFLICT (slug) DO NOTHING : ce bloc est rejouable sans dupliquer
-- si la migration est relancée par erreur.

INSERT INTO visites_cinetouristiques (slug, film_ids, nom, description, duree_minutes, lien, creneaux, creneaux_regles)
VALUES (
    'collioure-cine-balade', ARRAY[15],
    'Office de tourisme de Collioure — Ciné-balade',
    'Ciné-balade sur les traces des films tournés à Collioure.',
    120, 'https://boutique.tourisme-collioure.com/cine-balades/cine-balades',
    '[["2026-07-09","10:15","12:15"],["2026-07-23","10:15","12:15"],["2026-08-13","10:15","12:15"],["2026-08-27","10:15","12:15"]]'::jsonb,
    '[]'::jsonb
) ON CONFLICT (slug) DO NOTHING;

INSERT INTO visites_cinetouristiques (slug, film_ids, nom, description, duree_minutes, lien, creneaux, creneaux_regles)
VALUES (
    'sete-dna-pied-mai', ARRAY[128],
    'Office de tourisme Archipel de Thau — Cinétour DNA à pied',
    'Cinétour pédestre sur les lieux de tournage de Demain nous appartient.',
    120, 'https://billetterie.archipel-thau.com/loisirs/visites-guidees-a-pied/cinetour-pedestre-dna-aujourdhui-vous-appartient',
    '[]'::jsonb,
    '[{"debut":"2026-05-18","fin":"2026-05-30","jours":[0,2,5],"start":"16:00","end":"18:00"}]'::jsonb
) ON CONFLICT (slug) DO NOTHING;

INSERT INTO visites_cinetouristiques (slug, film_ids, nom, description, duree_minutes, lien, creneaux, creneaux_regles)
VALUES (
    'sete-dna-pied-juin', ARRAY[128],
    'Office de tourisme Archipel de Thau — Cinétour DNA à pied',
    'Cinétour pédestre sur les lieux de tournage de Demain nous appartient.',
    120, 'https://billetterie.archipel-thau.com/loisirs/visites-guidees-a-pied/cinetour-pedestre-dna-aujourdhui-vous-appartient',
    '[]'::jsonb,
    '[{"debut":"2026-06-01","fin":"2026-06-29","jours":[0,2,5],"start":"16:30","end":"18:30"}]'::jsonb
) ON CONFLICT (slug) DO NOTHING;

INSERT INTO visites_cinetouristiques (slug, film_ids, nom, description, duree_minutes, lien, creneaux, creneaux_regles)
VALUES (
    'sete-dna-bateau', ARRAY[128],
    'Office de tourisme Archipel de Thau — Cinétour DNA en bateau',
    'Balade en bateau sur les lieux de tournage de Demain nous appartient.',
    60, 'https://billetterie.archipel-thau.com/loisirs/excursions-et-promenades-en-bateau/cinetour-bateau-dna-lequipage-vous-appartient',
    '[]'::jsonb,
    '[{"debut":"2026-05-24","fin":"2026-10-25","jours":[6],"start":"09:45","end":"10:45"}]'::jsonb
) ON CONFLICT (slug) DO NOTHING;

INSERT INTO visites_cinetouristiques (slug, film_ids, nom, description, duree_minutes, lien, creneaux, creneaux_regles)
VALUES (
    'montpellier-usgs-aout', ARRAY[131],
    'Office de tourisme Montpellier — Au cœur de la série',
    'Visite guidée dans le centre historique autour de Un si grand soleil.',
    120, 'https://book.montpellier-tourisme.fr/fr/voir-faire/1999897/au-c%C5%93ur-de-la-s%C3%A9rie-un-si-grand-soleil-centre-historique/afficher-les-details',
    '[["2026-08-07","09:30","11:30"],["2026-08-14","09:30","11:30"],["2026-08-21","09:30","11:30"],["2026-08-28","09:30","11:30"]]'::jsonb,
    '[]'::jsonb
) ON CONFLICT (slug) DO NOTHING;

INSERT INTO visites_cinetouristiques (slug, film_ids, nom, description, duree_minutes, lien, creneaux, creneaux_regles)
VALUES (
    'palavas-cinema-2026-09-29', ARRAY[131,136],
    'Office de tourisme de Palavas-les-Flots — Le cinéma à Palavas',
    'Visite thématique autour des films et séries tournés à Palavas-les-Flots.',
    120, 'https://billetterie.palavas-tourisme.com/fr/produit/le-cinema-a-palavas',
    '[["2026-09-29","10:00","12:00"]]'::jsonb,
    '[]'::jsonb
) ON CONFLICT (slug) DO NOTHING;