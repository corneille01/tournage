-- Migration v8 — description Wikipédia du LIEU lui-même (son histoire,
-- pas ce qui s'y passe dans le film — vient compléter lieu.description
-- qui elle reste la note narrative liée au tournage).
ALTER TABLE lieux_tournage ADD COLUMN IF NOT EXISTS description_wikipedia TEXT NULL;
