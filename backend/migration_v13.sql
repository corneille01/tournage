-- Migration v13 — contenu multilingue. Un seul champ JSONB par table
-- plutôt qu'une colonne par langue (plus simple à faire évoluer si on
-- ajoute une langue plus tard). Structure :
--   films.i18n = {"en": {"titre": "...", "synopsis": "..."}, "es": {...}, ...}
--   lieux_tournage.i18n = {"en": {"anecdote": "...", "description_wikipedia": "..."}, ...}
-- Le français reste dans les colonnes existantes (titre, synopsis,
-- anecdote, description_wikipedia) — pas de duplication, juste un
-- complément pour les autres langues.
ALTER TABLE films ADD COLUMN IF NOT EXISTS i18n JSONB NULL;
ALTER TABLE lieux_tournage ADD COLUMN IF NOT EXISTS i18n JSONB NULL;
