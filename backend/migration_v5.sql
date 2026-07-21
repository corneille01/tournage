-- Migration v5 — score de popularité TMDB (pour le filtre "les plus
-- connus"), et aucune contrainte à ajouter cette fois.
ALTER TABLE films ADD COLUMN IF NOT EXISTS popularite REAL NULL;
CREATE INDEX IF NOT EXISTS idx_popularite ON films (popularite DESC);

-- Nettoyage : certains lieux ont leur "commune" résolue par erreur au
-- niveau du département (imprécision de la hiérarchie administrative
-- Wikidata pour certains lieux) — ça polluait le filtre "commune" avec
-- des noms de départements en double avec le filtre "département".
UPDATE lieux_tournage SET commune = NULL
WHERE commune IN (SELECT DISTINCT departement FROM lieux_tournage WHERE departement IS NOT NULL);
