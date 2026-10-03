-- Migration v31 - Distances de route IGN précalculées pour le parcours
-- Idempotente : peut être rejouée sans risque.
--
-- Une seule table pour deux usages :
--   type_origine = 'lieu'  : trajet lieu de tournage -> lieu de tournage
--                            (ordre des étapes, sélection sous contrainte de temps)
--   type_origine = 'guide' : trajet guide -> lieu de tournage
--                            (distances affichées pour les guides recommandés)
-- Alimentée chaque jour par precalculer_distances_routieres.py (Géoplateforme IGN).
-- Les visiteurs ne déclenchent aucun appel IGN pour ces trajets : ils lisent cette table.

CREATE TABLE IF NOT EXISTS distances_routieres (
    id                    BIGSERIAL PRIMARY KEY,
    type_origine          VARCHAR(5)  NOT NULL CHECK (type_origine IN ('lieu', 'guide')),
    origine_id            INT         NOT NULL,
    lieu_destination_id   INT         NOT NULL REFERENCES lieux_tournage(id) ON DELETE CASCADE,
    mode                  VARCHAR(7)  NOT NULL CHECK (mode IN ('pied', 'voiture')),
    distance_metres       INT         NOT NULL,
    duree_secondes        INT         NULL,
    calcule_le            TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_distances_routieres
    ON distances_routieres (type_origine, origine_id, lieu_destination_id, mode);

CREATE INDEX IF NOT EXISTS idx_distances_routieres_dest
    ON distances_routieres (lieu_destination_id, mode);