-- Test-only DDL for the CRM tables this service reads, plus the two it appends
-- to (audit_log and notification).
--
-- The real CRM schema is owned by crm-backend. This service SELECTs from it
-- (see ports/crm_core_sqlalchemy.py) and INSERTs audit and notification rows;
-- scripts/sql/schema.sql declares the LOCAL (owned) tables only. Tests run
-- against a single Postgres instance, so we mirror the minimum CRM DDL the
-- suite needs here.
--
-- Keep column types identical to production. In particular `address.number` is
-- an INTEGER in the real CRM — mirroring it as VARCHAR here would hide the
-- str/int bug class at the port boundary.

-- ---- community -------------------------------------------------------------
-- Mirrors core/database/models.py::Community plus the legal/regulatory columns
-- ports/crm_core_sqlalchemy.py reads. `regulator` is the coded field that
-- resolves to a Region (BE-WAL-CWAPE -> WAL); see reference/regulators.json.
CREATE TABLE IF NOT EXISTS community (
    id                       INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name                     VARCHAR(255) NOT NULL UNIQUE,
    auth_community_id        VARCHAR(255) NOT NULL UNIQUE,
    regulator                VARCHAR(32)  NOT NULL DEFAULT 'BE-WAL-CWAPE',
    vat_number               VARCHAR(32),
    legal_name               VARCHAR(255),
    iban                     VARCHAR(34),
    account_holder_name      VARCHAR(255),
    headquarters_address_id  INTEGER,
    created_at               TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at               TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- ---- community_subscription -------------------------------------------------
-- Mirrors core/database/models.py::CommunitySubscription — the per-annexe
-- feature gate checked by require_feature().
CREATE TABLE IF NOT EXISTS community_subscription (
    id           INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    id_community INTEGER     NOT NULL,
    feature      VARCHAR(64) NOT NULL,
    is_active    BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_community_subscription_community_feature
        UNIQUE (id_community, feature)
);

CREATE INDEX IF NOT EXISTS idx_community_subscription_id_community
    ON community_subscription (id_community);

-- ---- address ----------------------------------------------------------------
-- Joined from community.headquarters_address_id for the dossier's legal header.
CREATE TABLE IF NOT EXISTS address (
    id           INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    street       VARCHAR(255),
    number       INTEGER,  -- matches the real CRM: house number is an integer column
    postcode     VARCHAR(16),
    supplement   VARCHAR(255),
    city         VARCHAR(255),
    id_community INTEGER,
    created_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- ---- sharing_operation ------------------------------------------------------
-- Every dossier is filed for one of these. The service reads it (scoped by
-- id_community) to validate what a dossier may be attached to.
CREATE TABLE IF NOT EXISTS sharing_operation (
    id           INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name         VARCHAR(255) NOT NULL,
    type         INTEGER,
    is_public    BOOLEAN NOT NULL DEFAULT FALSE,
    id_community INTEGER NOT NULL,
    created_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- ---- manager / member / individual / company --------------------------------
-- The participant identity the generated annexes are filled from (Phase 2).
-- Mirrors crm-backend/src/modules/members/domain/member.models.ts. A member is
-- exactly one of individual|company, keyed on the same id (joined-table
-- inheritance), so both are LEFT JOINed and one side is always NULL.
CREATE TABLE IF NOT EXISTS manager (
    id           INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name         VARCHAR(255),
    email        VARCHAR(255),
    id_community INTEGER,
    created_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS member (
    id                  INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name                VARCHAR(255) NOT NULL,
    member_type         INTEGER NOT NULL,   -- 1=INDIVIDUAL, 2=COMPANY
    status              INTEGER,            -- 1=ACTIVE, 2=INACTIVE, 3=PENDING
    iban                VARCHAR(255),
    id_home_address     INTEGER,
    id_billing_address  INTEGER,
    id_community        INTEGER NOT NULL,
    created_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS individual (
    id            INTEGER PRIMARY KEY REFERENCES member (id),
    first_name    VARCHAR(255),
    nrn           VARCHAR(255),
    email         VARCHAR(255),
    phone_number  VARCHAR(255),
    social_rate   BOOLEAN NOT NULL DEFAULT FALSE,
    id_manager    INTEGER
);

CREATE TABLE IF NOT EXISTS company (
    id          INTEGER PRIMARY KEY REFERENCES member (id),
    vat_number  VARCHAR(255),
    id_manager  INTEGER
);

-- ---- meter / meter_data -----------------------------------------------------
-- Mirrors crm-backend/src/modules/meters/domain/meter.models.ts. `grd` is the
-- distribution-system operator (ELIA/AIEG/AIESH/ORES/RESA/REW) the CWaPE annexes
-- require per delivery point; `injection_status` / `production_chain` /
-- `total_generating_capacity` fill the production-installation sheets.
CREATE TABLE IF NOT EXISTS meter (
    ean                VARCHAR(64) PRIMARY KEY,
    meter_number       VARCHAR(255),
    id_address         INTEGER,
    tarif_group        INTEGER,
    phases_number      INTEGER,
    reading_frequency  INTEGER,
    id_community       INTEGER NOT NULL,
    created_at         TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at         TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS meter_data (
    id                        INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ean                       VARCHAR(64) NOT NULL REFERENCES meter (ean),
    id_member                 INTEGER,
    id_sharing_operation      INTEGER,
    status                    INTEGER,   -- 1=ACTIVE
    client_type               INTEGER,   -- 1=Résidentiel, 2=Professionnel, 3=Industriel
    injection_status          INTEGER,   -- 1..4, NULL for a pure consumer
    production_chain          INTEGER,   -- 1..7
    total_generating_capacity DOUBLE PRECISION,
    grd                       VARCHAR(255),
    start_date                DATE,
    end_date                  DATE,
    id_community              INTEGER NOT NULL,
    created_at                TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at                TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_meter_data_op ON meter_data (id_sharing_operation);

-- ---- app_user ---------------------------------------------------------------
-- Mirrors shared/models/crm_models.py::AppUserModel. Only the columns the
-- audit log service reads — auth_user_id -> (id, email) — are present.
CREATE TABLE IF NOT EXISTS app_user (
    id            INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    auth_user_id  VARCHAR(255) NOT NULL UNIQUE,
    email         VARCHAR(256) NOT NULL,
    -- Preferred language, resolved onto every queued email at enqueue time.
    locale        VARCHAR(8)   NULL,
    -- Denormalised onto the queued row as the recipient display name.
    first_name    TEXT         NULL,
    last_name     TEXT         NULL
);

-- ---- user_member_link -------------------------------------------------------
-- Which portal identity represents which member. Mirrors
-- crm-backend/database_script/init.sql. Carries NO id_community: community
-- scope comes from member.id_community, which is why every query joining this
-- table must also filter the member.
--
-- Needed here because `GET /filings/mine` resolves the caller to their member
-- id(s) through it — the row filter that makes decision B4 enforceable.
CREATE TABLE IF NOT EXISTS user_member_link (
    id        INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    id_user   INTEGER NOT NULL REFERENCES app_user (id) ON DELETE CASCADE,
    id_member INTEGER NOT NULL REFERENCES member (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_user_member_link_user ON user_member_link (id_user);
CREATE INDEX IF NOT EXISTS idx_user_member_link_member ON user_member_link (id_member);

-- ---- audit_log --------------------------------------------------------------
-- Mirrors core/database/models.py::AuditLogModel and the production DDL in
-- crm-backend/database_script/2026-05-27_audit_log.sql. Append-only by
-- convention. Indexes from the production migration are omitted here — they
-- exist only to keep production reads fast and don't affect test correctness.
CREATE TABLE IF NOT EXISTS audit_log (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    id_community INTEGER REFERENCES community(id) ON DELETE CASCADE,
    timestamp    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    action       VARCHAR(128) NOT NULL,
    source       VARCHAR(32)  NOT NULL,
    entity_type  VARCHAR(64)  NOT NULL,
    entity_id    VARCHAR(64),
    user_id      INTEGER,
    user_email   VARCHAR(256),
    payload      JSONB        NOT NULL DEFAULT '{}'::jsonb
);

-- ---- community_user ---------------------------------------------------------
-- Mirrors crm-backend's community_user join table. Read to narrow a
-- notification fan-out to a community's managers.
CREATE TABLE IF NOT EXISTS community_user (
    id_community INTEGER REFERENCES community(id) ON DELETE CASCADE,
    id_user      INTEGER REFERENCES app_user(id) ON DELETE CASCADE,
    role         VARCHAR(50) NOT NULL,
    PRIMARY KEY (id_community, id_user)
);

-- ---- notification -----------------------------------------------------------
-- Mirrors crm-backend's production DDL. This service only INSERTs one row per
-- recipient through core/notifications; reads are served by crm-backend.
CREATE TABLE IF NOT EXISTS notification (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    id_community INTEGER REFERENCES community(id) ON DELETE CASCADE,
    id_user      INTEGER NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    type         VARCHAR(128) NOT NULL,
    data         JSONB        NOT NULL DEFAULT '{}'::jsonb,
    read_at      TIMESTAMPTZ,
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- ---- notification delivery layer ---------------------------------------------
-- Mirrors crm-backend/database_script/2026-08-03_notification_delivery.sql.
-- `core/notifications` writes one outbound_message per emailable recipient and
-- reads notification_preference to decide what is deliverable. Sending and the
-- suppression check belong to the notification-dispatch worker; email_suppression
-- is mirrored here only so the schema stays a faithful copy.
CREATE TABLE IF NOT EXISTS outbound_message (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    id_notification BIGINT NULL REFERENCES notification(id) ON DELETE SET NULL,
    id_community    INTEGER NULL REFERENCES community(id) ON DELETE CASCADE,
    channel         SMALLINT     NOT NULL CHECK (channel IN (1, 2)),
    recipient       VARCHAR(320) NOT NULL,
    recipient_name  VARCHAR(255) NULL,
    locale          VARCHAR(8)   NOT NULL DEFAULT '',
    type            VARCHAR(128) NOT NULL,
    category        SMALLINT     NOT NULL CHECK (category IN (1, 2)),
    data            JSONB        NOT NULL DEFAULT '{}'::jsonb,
    dedupe_key      VARCHAR(200) NOT NULL,
    status          SMALLINT     NOT NULL DEFAULT 1 CHECK (status IN (1, 2, 3, 4, 5)),
    attempts        SMALLINT     NOT NULL DEFAULT 0,
    last_error      TEXT         NULL,
    scheduled_for   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    claimed_at      TIMESTAMPTZ  NULL,
    sent_at         TIMESTAMPTZ  NULL,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_outbound_message_dedupe
    ON outbound_message (dedupe_key);
CREATE INDEX IF NOT EXISTS ix_outbound_message_due
    ON outbound_message (scheduled_for) WHERE status = 1;
CREATE INDEX IF NOT EXISTS ix_outbound_message_stale
    ON outbound_message (claimed_at) WHERE status = 5;

CREATE TABLE IF NOT EXISTS email_suppression (
    email      VARCHAR(320) PRIMARY KEY,
    reason     SMALLINT     NOT NULL CHECK (reason IN (1, 2, 3, 4)),
    detail     TEXT         NULL,
    created_at TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS notification_preference (
    id_user     INTEGER      NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    type_prefix VARCHAR(128) NOT NULL,
    channel     SMALLINT     NOT NULL CHECK (channel IN (1, 2)),
    mode        SMALLINT     NOT NULL CHECK (mode IN (1, 3)),

    PRIMARY KEY (id_user, type_prefix, channel)
);
