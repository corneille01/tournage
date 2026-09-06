-- Migration v19
-- Isochrones Géoplateforme pour les lieux de tournage

CREATE TABLE IF NOT EXISTS isochrones (
    id BIGSERIAL PRIMARY KEY,

    lieu_tournage_id INTEGER NOT NULL
        REFERENCES lieux_tournage(id)
        ON DELETE CASCADE,

    mode VARCHAR(30) NOT NULL,

    minutes INTEGER NOT NULL,

    geometry_geojson JSONB NOT NULL,

    provider VARCHAR(50) NOT NULL DEFAULT 'geoplateforme',

    calculated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT isochrones_mode_check
        CHECK (
            mode IN (
                'driving-car',
                'foot-walking'
            )
        ),

    CONSTRAINT isochrones_minutes_check
        CHECK (minutes > 0),

    CONSTRAINT isochrones_unique
        UNIQUE (
            lieu_tournage_id,
            mode,
            minutes
        )
);

CREATE INDEX IF NOT EXISTS idx_isochrones_lieu
    ON isochrones(lieu_tournage_id);

CREATE INDEX IF NOT EXISTS idx_isochrones_mode
    ON isochrones(mode);