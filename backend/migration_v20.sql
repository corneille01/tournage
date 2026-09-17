-- Pelify V4.7 — profil visiteur pseudonyme + historique des parcours
-- Le profil est identifié par un cookie aléatoire dont seul le hash est stocké.
-- Aucun email, nom ou mot de passe n'est requis.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS pelify_users (
    id UUID PRIMARY KEY,
    visitor_token_hash VARCHAR(64) NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS pelify_parcours_history (
    id BIGSERIAL PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES pelify_users(id) ON DELETE CASCADE,
    titre VARCHAR(500) NOT NULL,
    options_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    lieux_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    resultat_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    nb_etapes INTEGER NOT NULL DEFAULT 0,
    distance_metres INTEGER,
    duree_secondes INTEGER,
    duree_totale_estimee_secondes INTEGER,
    budget_level VARCHAR(20) NOT NULL DEFAULT 'equilibre',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_pelify_history_user_date
    ON pelify_parcours_history(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_pelify_history_budget
    ON pelify_parcours_history(user_id, budget_level);

-- Préserve une date de dernière activité facilement exploitable.
CREATE OR REPLACE FUNCTION pelify_touch_user()
RETURNS TRIGGER AS $$
BEGIN
    UPDATE pelify_users SET last_seen_at = NOW() WHERE id = NEW.user_id;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_pelify_touch_user ON pelify_parcours_history;
CREATE TRIGGER trg_pelify_touch_user
    AFTER INSERT ON pelify_parcours_history
    FOR EACH ROW EXECUTE FUNCTION pelify_touch_user();
