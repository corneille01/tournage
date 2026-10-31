-- Migration v33 - Communauté « après la visite » + socle AR / IoT (« pendant la visite »)
-- Idempotente. Principes : pseudonymat par défaut, minimisation des données, suppression en cascade
-- (droit à l'effacement), traçabilité des décisions de modération (DSA), aucune donnée personnelle
-- dans les mesures IoT.

-- ═══════════════ COMMUNAUTÉ ═══════════════
CREATE TABLE IF NOT EXISTS communaute_profils (
    user_id          UUID PRIMARY KEY REFERENCES pelify_users(id) ON DELETE CASCADE,
    pseudo           VARCHAR(24)  NOT NULL,
    pseudo_norm      VARCHAR(24)  NOT NULL UNIQUE,
    statut           VARCHAR(10)  NOT NULL DEFAULT 'actif' CHECK (statut IN ('actif', 'suspendu', 'banni')),
    posts_valides    INT          NOT NULL DEFAULT 0,
    regles_version   VARCHAR(10)  NOT NULL,
    consentement_le  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    cree_le          TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS communaute_posts (
    id               BIGSERIAL PRIMARY KEY,
    auteur_id        UUID NOT NULL REFERENCES communaute_profils(user_id) ON DELETE CASCADE,
    lieu_id          INT  NULL REFERENCES lieux_tournage(id) ON DELETE SET NULL,
    film_id          INT  NULL REFERENCES films(id) ON DELETE SET NULL,
    titre            VARCHAR(120) NULL,
    texte            TEXT NOT NULL CHECK (char_length(texte) <= 4000),
    note             SMALLINT NULL CHECK (note BETWEEN 1 AND 5),
    date_visite      DATE NULL,
    statut           VARCHAR(12) NOT NULL DEFAULT 'publie' CHECK (statut IN ('publie', 'en_attente', 'masque', 'supprime')),
    motif_moderation TEXT NULL,
    nb_commentaires  INT NOT NULL DEFAULT 0,
    nb_likes         INT NOT NULL DEFAULT 0,
    cree_le          TIMESTAMPTZ NOT NULL DEFAULT now(),
    modifie_le       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_cposts_lieu  ON communaute_posts (lieu_id, cree_le DESC) WHERE statut = 'publie';
CREATE INDEX IF NOT EXISTS idx_cposts_film  ON communaute_posts (film_id, cree_le DESC) WHERE statut = 'publie';
CREATE INDEX IF NOT EXISTS idx_cposts_recent ON communaute_posts (cree_le DESC) WHERE statut = 'publie';
CREATE INDEX IF NOT EXISTS idx_cposts_auteur ON communaute_posts (auteur_id);
CREATE INDEX IF NOT EXISTS idx_cposts_file  ON communaute_posts (cree_le) WHERE statut = 'en_attente';

CREATE TABLE IF NOT EXISTS communaute_medias (
    id            BIGSERIAL PRIMARY KEY,
    post_id       BIGINT NULL REFERENCES communaute_posts(id) ON DELETE CASCADE,
    auteur_id     UUID   NOT NULL REFERENCES communaute_profils(user_id) ON DELETE CASCADE,
    cle_stockage  TEXT   NOT NULL UNIQUE,
    type          VARCHAR(8)  NOT NULL DEFAULT 'photo' CHECK (type IN ('photo', 'video')),
    mime          VARCHAR(40) NOT NULL,
    taille_octets INT NULL,
    largeur       INT NULL,
    hauteur       INT NULL,
    alt_texte     VARCHAR(200) NULL,
    statut        VARCHAR(12) NOT NULL DEFAULT 'televerse' CHECK (statut IN ('televerse', 'valide', 'refuse', 'supprime')),
    cree_le       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_cmedias_post ON communaute_medias (post_id);

CREATE TABLE IF NOT EXISTS communaute_commentaires (
    id         BIGSERIAL PRIMARY KEY,
    post_id    BIGINT NOT NULL REFERENCES communaute_posts(id) ON DELETE CASCADE,
    parent_id  BIGINT NULL REFERENCES communaute_commentaires(id) ON DELETE CASCADE,
    auteur_id  UUID NOT NULL REFERENCES communaute_profils(user_id) ON DELETE CASCADE,
    texte      TEXT NOT NULL CHECK (char_length(texte) <= 1500),
    statut     VARCHAR(12) NOT NULL DEFAULT 'publie' CHECK (statut IN ('publie', 'masque', 'supprime')),
    cree_le    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_ccom_post ON communaute_commentaires (post_id, id);

CREATE TABLE IF NOT EXISTS communaute_reactions (
    post_id  BIGINT NOT NULL REFERENCES communaute_posts(id) ON DELETE CASCADE,
    user_id  UUID   NOT NULL REFERENCES communaute_profils(user_id) ON DELETE CASCADE,
    cree_le  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (post_id, user_id)
);

-- Signalements (notice-and-action du DSA) : un utilisateur ne signale qu'une fois la même cible.
CREATE TABLE IF NOT EXISTS communaute_signalements (
    id          BIGSERIAL PRIMARY KEY,
    cible_type  VARCHAR(12) NOT NULL CHECK (cible_type IN ('post', 'commentaire', 'media')),
    cible_id    BIGINT NOT NULL,
    auteur_id   UUID NULL REFERENCES pelify_users(id) ON DELETE SET NULL,   -- signaler ne demande pas de profil
    motif       VARCHAR(16) NOT NULL CHECK (motif IN ('illegal', 'haine', 'harcelement', 'vie_privee', 'spam', 'mineur', 'droit_auteur', 'autre')),
    detail      VARCHAR(1000) NULL,
    statut      VARCHAR(8) NOT NULL DEFAULT 'ouvert' CHECK (statut IN ('ouvert', 'traite', 'rejete')),
    cree_le     TIMESTAMPTZ NOT NULL DEFAULT now(),
    traite_le   TIMESTAMPTZ NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_csignal ON communaute_signalements (cible_type, cible_id, auteur_id);
CREATE INDEX IF NOT EXISTS idx_csignal_ouverts ON communaute_signalements (cree_le) WHERE statut = 'ouvert';

-- Exposé des motifs et traçabilité de chaque décision de modération (DSA art. 17).
CREATE TABLE IF NOT EXISTS communaute_journal_moderation (
    id          BIGSERIAL PRIMARY KEY,
    moderateur  VARCHAR(60) NOT NULL DEFAULT 'equipe',
    action      VARCHAR(20) NOT NULL,
    cible_type  VARCHAR(12) NOT NULL,
    cible_id    BIGINT NOT NULL,
    motif       TEXT NULL,
    cree_le     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ═══════════════ AR / IoT (socle pour la visite sur place) ═══════════════
-- Points d'intérêt géo-ancrés : ce qu'un client de réalité augmentée affichera à l'endroit exact
-- d'une scène (image du plan d'origine, texte, modèle 3D…).
CREATE TABLE IF NOT EXISTS ar_points_interet (
    id                    BIGSERIAL PRIMARY KEY,
    lieu_id               INT NOT NULL REFERENCES lieux_tournage(id) ON DELETE CASCADE,
    nom                   VARCHAR(160) NOT NULL,
    type                  VARCHAR(16) NOT NULL DEFAULT 'scene' CHECK (type IN ('scene', 'panneau', 'historique', 'point_de_vue')),
    latitude              DOUBLE PRECISION NOT NULL,
    longitude             DOUBLE PRECISION NOT NULL,
    altitude_m            REAL NULL,
    orientation_deg       REAL NULL CHECK (orientation_deg IS NULL OR (orientation_deg >= 0 AND orientation_deg < 360)),
    rayon_declenchement_m INT NOT NULL DEFAULT 30,
    contenu_type          VARCHAR(10) NOT NULL DEFAULT 'texte' CHECK (contenu_type IN ('texte', 'image', 'video', 'modele3d', 'audio')),
    contenu_url           TEXT NULL,
    contenu_texte         TEXT NULL,
    licence               VARCHAR(60) NULL,
    actif                 BOOLEAN NOT NULL DEFAULT TRUE,
    cree_le               TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_ar_poi_lieu ON ar_points_interet (lieu_id) WHERE actif;
CREATE INDEX IF NOT EXISTS idx_ar_poi_geo ON ar_points_interet (latitude, longitude) WHERE actif;

-- Appareils connectés posés sur site (bornes d'info, compteurs d'affluence, balises, capteurs météo).
CREATE TABLE IF NOT EXISTS iot_appareils (
    id                UUID PRIMARY KEY,
    nom               VARCHAR(120) NOT NULL,
    type              VARCHAR(20) NOT NULL CHECK (type IN ('borne_info', 'compteur_affluence', 'balise_ble', 'capteur_meteo', 'autre')),
    lieu_id           INT NULL REFERENCES lieux_tournage(id) ON DELETE SET NULL,
    cle_hash          TEXT NOT NULL,
    actif             BOOLEAN NOT NULL DEFAULT TRUE,
    derniere_activite TIMESTAMPTZ NULL,
    cree_le           TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Mesures agrégées et ANONYMES uniquement (jamais d'identifiant d'appareil personnel, d'adresse MAC ou d'image).
CREATE TABLE IF NOT EXISTS iot_mesures (
    id          BIGSERIAL PRIMARY KEY,
    appareil_id UUID NOT NULL REFERENCES iot_appareils(id) ON DELETE CASCADE,
    type        VARCHAR(30) NOT NULL,
    valeur      DOUBLE PRECISION NOT NULL,
    unite       VARCHAR(12) NULL,
    mesure_le   TIMESTAMPTZ NOT NULL,
    recu_le     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_iot_mesures ON iot_mesures (appareil_id, mesure_le DESC);