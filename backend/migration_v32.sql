-- Migration v32 - Guides rattachés à des œuvres (films / séries) + œuvres proposées par les guides
-- Idempotente : peut être rejouée sans risque.
--
-- guide_films : les œuvres pour lesquelles un guide accepte d'accompagner les visiteurs.
--   Alimenté par le formulaire « devenir guide » (liste de toutes les œuvres de la base).
--   Le bouton « Guides » du grand popup d'un lieu ne montre que les guides liés à l'œuvre
--   (un guide peut être plus proche d'un lieu sans pouvoir accompagner pour cette œuvre).
-- films.proposee_par_guide : œuvre saisie librement dans le formulaire, absente de la base.
--   Elle est enregistrée avec statut = 'brouillon' : invisible du public tant qu'elle n'est pas
--   vérifiée et complétée (lieux de tournage) puis passée en 'publie'.

CREATE TABLE IF NOT EXISTS guide_films (
    guide_id       INT         NOT NULL REFERENCES guides(id) ON DELETE CASCADE,
    film_id        INT         NOT NULL REFERENCES films(id)  ON DELETE CASCADE,
    date_creation  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (guide_id, film_id)
);

CREATE INDEX IF NOT EXISTS idx_guide_films_film ON guide_films (film_id);

ALTER TABLE films ADD COLUMN IF NOT EXISTS proposee_par_guide BOOLEAN NOT NULL DEFAULT FALSE;