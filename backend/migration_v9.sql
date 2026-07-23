-- Migration v9 — nationalité du film (ex: "Français"), pour l'en-tête
-- du popup ("Film Français · 1970").
ALTER TABLE films ADD COLUMN IF NOT EXISTS nationalite VARCHAR(100) NULL;
