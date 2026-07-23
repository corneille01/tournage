-- Migration v6 — anecdote éditoriale par lieu de tournage. Remplie à
-- la main (ou via draft récupéré par wikipedia_anecdotes.py), jamais
-- générée automatiquement sans relecture — risque de faits inventés.
ALTER TABLE lieux_tournage ADD COLUMN IF NOT EXISTS anecdote TEXT NULL;
