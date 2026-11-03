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
--   * change: one change per (event, procedure, operation). A community event
--     (a new representative) feeds an operation procedure once per operation,
--     so UNIQUE (source_event_id, procedure_code) becomes an index on the
--     operation too. Every existing row has one change per (event, procedure),
--     so the new index holds them all.
--   * digest_setting: the summary email, per community. Off by default; an
--     admin turns it on and picks the rhythm and the recipients.
--   * piece_signature: a collected piece signed in the app through the
--     signature annexe (2026-11-03).
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

-- ---- digest_setting: the summary email, per community (phase 4) ----------
-- Off by default (no row, or `enabled` false). An admin turns it on, picks
-- the rhythm and who receives it; the daily job sends it on the scheduled
-- dates (engine/digest_schedule.py, worker/digest.py).
--   cadence        1 working days (Monday to Friday), 2 weekly on `weekday`,
--                  3 every two weeks on `weekday` from `first_on`,
--                  4 monthly, on the month's first `weekday`
--   weekday        ISO, 1 Monday … 7 Sunday; NULL for working days
--   recipient_ids  CRM user ids (another database: no FK), admins or managers
--                  of the community when saved, checked again when sent
--   enabled_at     when the rhythm took effect (turned on, or changed): no
--                  date whose run came before it is sent
--   period_from    where the next summary's period starts: when turned on,
--                  then the end of each run that handled a date
--   last_due_on    the last scheduled date handled (sent, or nothing to tell)
CREATE TABLE IF NOT EXISTS digest_setting (
    id_community   INTEGER       PRIMARY KEY,
    enabled        BOOLEAN       NOT NULL DEFAULT FALSE,
    cadence        SMALLINT      NOT NULL,
    weekday        SMALLINT,
    first_on       DATE,
    recipient_ids  INTEGER[]     NOT NULL DEFAULT '{}',
    enabled_at     TIMESTAMPTZ,
    period_from    TIMESTAMPTZ,
    last_due_on    DATE,
    updated_at     TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    updated_by     VARCHAR(255),
    CONSTRAINT ck_digest_setting_cadence CHECK (COALESCE(cadence IN (1, 2, 3, 4), FALSE)),
    CONSTRAINT ck_digest_setting_weekday CHECK (COALESCE(
        CASE WHEN cadence = 1 THEN weekday IS NULL ELSE weekday BETWEEN 1 AND 7 END,
        FALSE)),
    CONSTRAINT ck_digest_setting_first_on CHECK (COALESCE(
        CASE WHEN cadence = 3 THEN EXTRACT(ISODOW FROM first_on) = weekday
             ELSE first_on IS NULL END,
        FALSE)),
    CONSTRAINT ck_digest_setting_enabled CHECK (
        NOT enabled
        OR (cardinality(recipient_ids) > 0 AND enabled_at IS NOT NULL AND period_from IS NOT NULL))
);

-- ---- change: one per event, procedure and operation ------------------------
ALTER TABLE change DROP CONSTRAINT IF EXISTS uq_change_source;
CREATE UNIQUE INDEX IF NOT EXISTS uq_change_source_operation
    ON change (source_event_id, procedure_code, COALESCE(id_sharing_operation, 0));

-- ---- piece_signature: a collected piece signed in the app (2026-11-03) ---
-- One row per request this annexe made to the signature service for a
-- per-member collected piece (annexe 8). Its status follows the service's
-- events (signature.event.administrative_document.*); the evidence itself (the
-- event log, the sealed report) lives in the signature service. A working
-- state, not evidence: it changes. A new request for the same piece is a new
-- row (a new idempotency key); one live at a time.
--   status  1 requested (published, not yet accepted), 2 open, 3 signed,
--           4 declined, 5 expired, 6 cancelled, 7 failed
CREATE TABLE IF NOT EXISTS piece_signature (
    id                 BIGSERIAL     PRIMARY KEY,
    id_community       INTEGER       NOT NULL,
    id_document        INTEGER       NOT NULL REFERENCES document (id) ON DELETE CASCADE,
    id_version         INTEGER       NOT NULL REFERENCES document_version (id) ON DELETE CASCADE,
    idempotency_key    VARCHAR(255)  NOT NULL,
    request_id         UUID,
    status             SMALLINT      NOT NULL,
    reason             TEXT,
    -- The FINAL version the signed PDF became (none when its procedure was
    -- sent meanwhile: a sent piece takes no new version).
    id_signed_version  INTEGER       REFERENCES document_version (id),
    final_key          TEXT,
    final_sha256       CHAR(64),
    evidence_pdf_key   TEXT,
    evidence_json_key  TEXT,
    requested_by       VARCHAR(255),
    requested_at       TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    updated_at         TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_piece_signature_key UNIQUE (idempotency_key),
    CONSTRAINT ck_piece_signature_status
        CHECK (COALESCE(status IN (1, 2, 3, 4, 5, 6, 7), FALSE))
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_piece_signature_live
    ON piece_signature (id_document) WHERE status IN (1, 2);
CREATE INDEX IF NOT EXISTS ix_piece_signature_document
    ON piece_signature (id_document, id DESC);

DROP TRIGGER IF EXISTS trg_piece_signature_updated_at ON piece_signature;
CREATE TRIGGER trg_piece_signature_updated_at BEFORE UPDATE ON piece_signature
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

INSERT INTO schema_version (version, description)
VALUES (6, 'Phase 4: reminder ladder, reception address, summary setting')
ON CONFLICT DO NOTHING;

COMMIT;
