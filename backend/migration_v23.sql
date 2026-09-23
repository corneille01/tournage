-- Pelify Landscape V1 — demandes de droits
-- Migration V23
-- Conversation de droits entre Pelify et un photographe, sans compte obligatoire.

CREATE TABLE IF NOT EXISTS demandes_droits (
    id BIGSERIAL PRIMARY KEY,
    paysage_id INTEGER NOT NULL REFERENCES paysages(id) ON DELETE CASCADE,
    projet_id INTEGER NULL REFERENCES paysage_projets(id) ON DELETE SET NULL,
    scene_id INTEGER NULL REFERENCES paysage_scenes(id) ON DELETE SET NULL,
    demandeur_id UUID NOT NULL REFERENCES pelify_users(id) ON DELETE CASCADE,

    destinataire_nom VARCHAR(255) NULL,
    destinataire_email VARCHAR(320) NOT NULL,

    objet VARCHAR(500) NOT NULL,
    message TEXT NOT NULL,

    statut VARCHAR(30) NOT NULL DEFAULT 'en_attente'
        CHECK (statut IN (
            'en_attente',
            'reponse_recue',
            'acceptee',
            'refusee',
            'a_discuter',
            'fermee',
            'expiree'
        )),

    guest_token_hash VARCHAR(64) NOT NULL UNIQUE,
    guest_token_expires_at TIMESTAMPTZ NOT NULL,

    email_statut VARCHAR(30) NOT NULL DEFAULT 'a_envoyer'
        CHECK (email_statut IN ('a_envoyer','envoye','erreur','non_configure')),
    email_envoye_at TIMESTAMPTZ NULL,
    email_erreur TEXT NULL,

    demandeur_email VARCHAR(320) NULL,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_demandes_droits_demandeur
    ON demandes_droits(demandeur_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_demandes_droits_paysage
    ON demandes_droits(paysage_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_demandes_droits_statut
    ON demandes_droits(statut);

CREATE INDEX IF NOT EXISTS idx_demandes_droits_expiration
    ON demandes_droits(guest_token_expires_at);

CREATE TABLE IF NOT EXISTS messages_droits (
    id BIGSERIAL PRIMARY KEY,
    demande_id BIGINT NOT NULL REFERENCES demandes_droits(id) ON DELETE CASCADE,

    expediteur_type VARCHAR(20) NOT NULL
        CHECK (expediteur_type IN ('pelify','photographe')),
    expediteur_id UUID NULL REFERENCES pelify_users(id) ON DELETE SET NULL,
    expediteur_nom VARCHAR(255) NULL,
    expediteur_email VARCHAR(320) NULL,

    contenu TEXT NOT NULL,
    reponse_type VARCHAR(30) NULL
        CHECK (reponse_type IS NULL OR reponse_type IN (
            'message',
            'acceptee',
            'refusee',
            'a_discuter'
        )),

    lu BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_messages_droits_demande
    ON messages_droits(demande_id, created_at);

-- Mise à jour simple de updated_at.
CREATE OR REPLACE FUNCTION maj_demandes_droits_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_demandes_droits_updated_at ON demandes_droits;
CREATE TRIGGER trg_demandes_droits_updated_at
    BEFORE UPDATE ON demandes_droits
    FOR EACH ROW
    EXECUTE FUNCTION maj_demandes_droits_updated_at();
