-- Migration v11 — lien source de l'anecdote, affiché en fin de texte
-- dans le popup. Colonne séparée (pas juste dans le texte) pour
-- pouvoir un jour lister/vérifier toutes les sources d'un coup.
ALTER TABLE lieux_tournage ADD COLUMN IF NOT EXISTS source_anecdote VARCHAR(500) NULL;
