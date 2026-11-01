-- ============================================================================
-- Migration 0006 — notifications and complements (espace administratif, phase 4).
--
--   * deadline.reminders_sent: the rungs of the reminder ladder (J-14, J-7, J-2
--     before the due date) already told for a deadline of the community. The
--     July marker reminded_at held one window only; it stays for a third
--     party's late answer, told once, and is still stamped at the first rung.
--     A deadline reminded before the ladder was told every rung its reminder
--     day had reached.
--   * inbound_address, inbound_mail, inbound_mail_attachment: the reception
--     address. Each community's address carries a random code; the worker
--     polls one mailbox by IMAP, stores each mail and its attachments, and
--     attaches it to the procedure its [OPT-<id>] reference names.
--
-- Idempotent and forward-only. On a fresh install (schema.sql already has all
-- of this) it changes nothing.
-- ============================================================================

BEGIN;

-- ---- deadline: the reminder ladder ------------------------------------------
-- A constant default is a catalog change: no row is rewritten.
ALTER TABLE deadline
    ADD COLUMN IF NOT EXISTS reminders_sent SMALLINT[] NOT NULL DEFAULT '{}';

UPDATE deadline
   SET reminders_sent = ARRAY(
           SELECT rung::SMALLINT
             FROM unnest(ARRAY[14, 7, 2]) AS rung
            WHERE rung >= due_date - (reminded_at AT TIME ZONE 'Europe/Brussels')::date
            ORDER BY rung DESC
       )
 WHERE reminded_at IS NOT NULL
   AND party = 1
   AND reminders_sent = '{}';
-- Reminded more than 14 days ahead cannot happen (the window was 14 days),
-- but a row reminded then stays told: at least the first rung.
UPDATE deadline
   SET reminders_sent = ARRAY[14]::SMALLINT[]
 WHERE reminded_at IS NOT NULL
   AND party = 1
   AND reminders_sent = '{}';

-- ---- inbound_address / inbound_mail (phase 4, the reception address) -----
-- One mailbox serves every community; each community's address carries a
-- random code (`token`, INBOUND_ADDRESS_TEMPLATE). One live code per
-- community: rotating it revokes the old one, which then reaches nobody.
CREATE TABLE IF NOT EXISTS inbound_address (
    id            SERIAL       PRIMARY KEY,
    id_community  INTEGER      NOT NULL,
    token         VARCHAR(32)  NOT NULL,
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    created_by    VARCHAR(255),
    revoked_at    TIMESTAMPTZ,
    CONSTRAINT uq_inbound_address_token UNIQUE (token)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_inbound_address_live
    ON inbound_address (id_community) WHERE revoked_at IS NULL;

-- A mail the reception address received: the raw .eml (content-addressed in
-- storage), what the worker read in it, and what became of it. Its status is
-- a working state, not evidence: the evidence is the file, and the
-- correspondence a manager records from it (append-only).
--   status  1 unmatched (no procedure found), 2 attached (to qualify),
--           3 recorded (became a correspondence), 4 discarded
CREATE TABLE IF NOT EXISTS inbound_mail (
    id                 BIGSERIAL     PRIMARY KEY,
    id_community       INTEGER       NOT NULL,
    id_inbound_address INTEGER       NOT NULL REFERENCES inbound_address (id),
    message_id         VARCHAR(998),
    content_sha256     CHAR(64)      NOT NULL,
    file_ref           TEXT          NOT NULL,
    byte_size          INTEGER       NOT NULL,
    from_address       VARCHAR(320),
    subject            VARCHAR(998),
    sent_at            TIMESTAMPTZ,
    body_text          TEXT,
    received_at        TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    status             SMALLINT      NOT NULL DEFAULT 1,
    id_dossier         INTEGER       REFERENCES dossier (id) ON DELETE SET NULL,
    -- How it found its procedure: 'reference' ([OPT-<id>]), 'external_ref'
    -- (the CWaPE's or the GRD's), 'manual'.
    match_reason       VARCHAR(32),
    -- The response type the plugin suggests; a manager confirms or changes it.
    suggested_response VARCHAR(64),
    id_correspondence  BIGINT        REFERENCES correspondence (id),
    handled_by         VARCHAR(255),
    handled_at         TIMESTAMPTZ,
    updated_at         TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_inbound_mail_status CHECK (COALESCE(status IN (1, 2, 3, 4), FALSE)),
    -- The worker's idempotency: a mail read twice (a crash before it was
    -- marked seen) is stored once.
    CONSTRAINT uq_inbound_mail_content UNIQUE (id_community, content_sha256)
);
CREATE INDEX IF NOT EXISTS ix_inbound_mail_status
    ON inbound_mail (id_community, status, received_at DESC);
CREATE INDEX IF NOT EXISTS ix_inbound_mail_dossier ON inbound_mail (id_dossier);

-- Its attachments, each stored on its own (a forwarded message as its .eml).
-- Evidence: append-only, like correspondence.
CREATE TABLE IF NOT EXISTS inbound_mail_attachment (
    id               BIGSERIAL     PRIMARY KEY,
    id_inbound_mail  BIGINT        NOT NULL REFERENCES inbound_mail (id),
    id_community     INTEGER       NOT NULL,
    position         SMALLINT      NOT NULL,
    filename         VARCHAR(255)  NOT NULL,
    content_type     VARCHAR(255)  NOT NULL,
    byte_size        INTEGER       NOT NULL,
    content_sha256   CHAR(64)      NOT NULL,
    file_ref         TEXT          NOT NULL,
    created_at       TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_inbound_mail_attachment UNIQUE (id_inbound_mail, position)
);
DROP TRIGGER IF EXISTS trg_inbound_mail_attachment_immutable ON inbound_mail_attachment;
CREATE TRIGGER trg_inbound_mail_attachment_immutable
    BEFORE UPDATE OR DELETE ON inbound_mail_attachment
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

INSERT INTO schema_version (version, description)
VALUES (6, 'Phase 4: reminder ladder, reception address')
ON CONFLICT DO NOTHING;

COMMIT;
