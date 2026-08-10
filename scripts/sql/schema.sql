-- ============================================================================
-- Local database schema (LOCAL_DATABASE_URL) — administrative-document service.
--
-- Single source of truth for the LOCAL (owned) database. The CRM core tables
-- (community, address, member, sharing_operation, …) live in a separate
-- database and are NOT declared here; this service only reads them via
-- CrmCoreReadPort. Cross-DB references (id_sharing_operation) are plain
-- columns, never foreign keys.
--
-- Mirrors shared/models/local_models.py. When changing models, update this file
-- and add a migration under scripts/sql/migrations/.
--
-- Two invariants are enforced in the database rather than only in the service,
-- because the product requires them to be unforgeable (spec R1/R2/R4):
--   1. status_event and document_version are APPEND-ONLY (UPDATE/DELETE raise).
--   2. dossier.status / document.status are a *cache* of the status_event
--      journal head; a deferred constraint trigger rejects, at COMMIT, any
--      status that was not accompanied by a journal row. A rollback is
--      therefore necessarily a new traced event, never an in-place mutation.
-- ============================================================================

-- ---- Shared utilities ------------------------------------------------------
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS trigger AS $$
BEGIN
  NEW.updated_at := CURRENT_TIMESTAMP;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Append-only guard. Attached BEFORE UPDATE OR DELETE to the journal and the
-- version table so history cannot be rewritten, even by a direct SQL client.
CREATE OR REPLACE FUNCTION forbid_mutation()
RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION '% is append-only: % is not permitted on row %',
        TG_TABLE_NAME, TG_OP, COALESCE(OLD.id::text, '?')
        USING ERRCODE = 'integrity_constraint_violation';
END;
$$ LANGUAGE plpgsql;

CREATE TABLE IF NOT EXISTS schema_version (
    version      INTEGER     PRIMARY KEY,
    description  TEXT        NOT NULL,
    applied_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO schema_version (version, description) VALUES
    (1, 'Administrative-document initial schema')
ON CONFLICT DO NOTHING;

-- ============================================================================
-- REFERENCE DATA (region-keyed, not tenant-owned)
--
-- document_template and deadline_rule are platform reference data, so
-- id_community is NULLABLE: NULL = platform default for the region, a non-null
-- value = a community-specific override. Resolution is most-specific-wins
-- (… WHERE (id_community = :cid OR id_community IS NULL)
--    ORDER BY id_community NULLS LAST LIMIT 1). v1 ships only NULL rows.
-- This is the one deliberate exception to "id_community NOT NULL on every owned
-- table"; it is what makes a regulatory form revision a data change (a new
-- version row) rather than a deployment.
-- ============================================================================

-- ---- document_template -----------------------------------------------------
CREATE TABLE IF NOT EXISTS document_template (
    id            SERIAL       PRIMARY KEY,
    id_community  INTEGER,                    -- NULL = platform default
    region        INTEGER      NOT NULL,      -- Region: 1=WAL, 2=BRU, 3=VLA
    doc_type      VARCHAR(64)  NOT NULL,      -- e.g. 'annex6_notification'
    version       INTEGER      NOT NULL,      -- a form revision = a new row
    valid_from    DATE         NOT NULL,
    valid_to      DATE,                       -- NULL = currently in force
    file_ref      VARCHAR(512),               -- s3:// template bundle (Phase 2)
    mapping_json  JSONB,                      -- field -> CRM data mapping (Phase 2)
    output_format VARCHAR(16)  NOT NULL DEFAULT 'pdf',  -- pdf | xlsx | docx
    label         VARCHAR(255),
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- One row per (scope, region, doc_type, version). NULLS NOT DISTINCT so the
-- platform-default rows (id_community IS NULL) are still covered.
CREATE UNIQUE INDEX IF NOT EXISTS uq_document_template_version
    ON document_template (id_community, region, doc_type, version) NULLS NOT DISTINCT;
-- At most one *current* (open-ended) template per (scope, region, doc_type), so
-- "the template in force today" is never ambiguous.
CREATE UNIQUE INDEX IF NOT EXISTS uq_document_template_current
    ON document_template (id_community, region, doc_type) NULLS NOT DISTINCT
    WHERE valid_to IS NULL;
CREATE INDEX IF NOT EXISTS ix_document_template_lookup
    ON document_template (region, doc_type);

DROP TRIGGER IF EXISTS trg_document_template_updated_at ON document_template;
CREATE TRIGGER trg_document_template_updated_at BEFORE UPDATE ON document_template
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---- deadline_rule ---------------------------------------------------------
-- The regulatory clocks, as data. "When <trigger_event> happens on a dossier of
-- <dossier_type> in <region>, a <deadline_type> deadline falls due
-- <offset_value> <offset_unit> later."
CREATE TABLE IF NOT EXISTS deadline_rule (
    id            SERIAL       PRIMARY KEY,
    id_community  INTEGER,                    -- NULL = platform default
    region        INTEGER      NOT NULL,      -- Region
    dossier_type  INTEGER      NOT NULL,      -- DossierType
    trigger_event VARCHAR(64)  NOT NULL,      -- TriggerEvent, e.g. 'dossier.submitted'
    deadline_type VARCHAR(64)  NOT NULL,      -- DeadlineType, e.g. 'completeness_check'
    offset_value  INTEGER      NOT NULL,
    offset_unit   INTEGER      NOT NULL,      -- OffsetUnit: 1=business_days, 2=months
    recurring     BOOLEAN      NOT NULL DEFAULT FALSE,
    recur_months  INTEGER,                    -- roll interval when recurring
    description   VARCHAR(255),
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_deadline_rule_recur
        CHECK (NOT recurring OR recur_months IS NOT NULL)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_deadline_rule
    ON deadline_rule (id_community, region, dossier_type, trigger_event, deadline_type)
    NULLS NOT DISTINCT;
CREATE INDEX IF NOT EXISTS ix_deadline_rule_lookup
    ON deadline_rule (region, dossier_type, trigger_event);

DROP TRIGGER IF EXISTS trg_deadline_rule_updated_at ON deadline_rule;
CREATE TRIGGER trg_deadline_rule_updated_at BEFORE UPDATE ON deadline_rule
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ============================================================================
-- TENANT DATA (id_community NOT NULL — stamped from current_internal_community_id)
-- ============================================================================

-- ---- dossier ---------------------------------------------------------------
-- Every dossier concerns exactly ONE sharing operation: a community running
-- several operations files a separate dossier per operation, so
-- id_sharing_operation is NOT NULL. It is a cross-DB reference (the operation
-- lives in the CRM), hence a plain column and never a foreign key — the service
-- validates it against the CRM, scoped to the caller's community, on create.
CREATE TABLE IF NOT EXISTS dossier (
    id                   SERIAL       PRIMARY KEY,
    id_community         INTEGER      NOT NULL,
    id_sharing_operation INTEGER      NOT NULL,    -- cross-DB ref, plain column
    dossier_type         INTEGER      NOT NULL,    -- DossierType
    status               INTEGER      NOT NULL,    -- DossierStatus (journal-head cache)
    region               INTEGER      NOT NULL,    -- resolved from community.regulator
    external_ref         VARCHAR(128),             -- CWaPE / DSO file reference
    title                VARCHAR(255),
    submitted_at         TIMESTAMPTZ,
    metadata_json        JSONB        NOT NULL DEFAULT '{}'::jsonb,
    created_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_dossier_status ON dossier (id_community, status);
CREATE INDEX IF NOT EXISTS ix_dossier_type   ON dossier (id_community, dossier_type);
-- The per-operation view: "everything filed for this sharing operation".
CREATE INDEX IF NOT EXISTS ix_dossier_operation
    ON dossier (id_community, id_sharing_operation);
-- An authority reference identifies exactly one dossier of a given type.
CREATE UNIQUE INDEX IF NOT EXISTS uq_dossier_external_ref
    ON dossier (id_community, dossier_type, external_ref)
    WHERE external_ref IS NOT NULL;

DROP TRIGGER IF EXISTS trg_dossier_updated_at ON dossier;
CREATE TRIGGER trg_dossier_updated_at BEFORE UPDATE ON dossier
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---- document --------------------------------------------------------------
CREATE TABLE IF NOT EXISTS document (
    id                 SERIAL       PRIMARY KEY,
    id_community       INTEGER      NOT NULL,
    id_dossier         INTEGER      NOT NULL REFERENCES dossier (id) ON DELETE CASCADE,
    doc_type           VARCHAR(64)  NOT NULL,
    origin             INTEGER      NOT NULL,   -- DocOrigin: 1=generated, 2=uploaded
    status             INTEGER      NOT NULL,   -- DocumentStatus (journal-head cache)
    current_version_id INTEGER,                 -- FK added after document_version exists
    title              VARCHAR(255),
    created_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_document_dossier ON document (id_community, id_dossier);
CREATE INDEX IF NOT EXISTS ix_document_status  ON document (id_community, status);

DROP TRIGGER IF EXISTS trg_document_updated_at ON document;
CREATE TRIGGER trg_document_updated_at BEFORE UPDATE ON document
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---- document_version ------------------------------------------------------
-- APPEND-ONLY. A version is the evidentiary record of what was produced or
-- transmitted, so it is never updated: re-uploading allocates the next
-- version_no and a new object. file_ref points at a content-addressed key
-- (…/{sha256}) so the bytes behind a version can never be swapped either.
CREATE TABLE IF NOT EXISTS document_version (
    id                 SERIAL       PRIMARY KEY,
    id_community       INTEGER      NOT NULL,
    id_document        INTEGER      NOT NULL REFERENCES document (id) ON DELETE CASCADE,
    version_no         INTEGER      NOT NULL,
    file_ref           VARCHAR(512) NOT NULL,   -- s3://OUTPUT_BUCKET/administrative-document/...
    content_sha256     VARCHAR(64),
    content_type       VARCHAR(128),
    byte_size          BIGINT,
    original_filename  VARCHAR(255),
    id_template        INTEGER      REFERENCES document_template (id),  -- NULL when uploaded
    data_snapshot_json JSONB,                   -- frozen render input (Phase 2)
    generated_by       VARCHAR(255),            -- x-user-id, or 'system'
    -- Which render produced this version. Append-only, so this survives the next
    -- regeneration reusing document_render.docgen_request_id -- which is what
    -- lets the result handler tell "already handled" from "stale/superseded"
    -- instead of guessing.
    docgen_request_id  VARCHAR(64),             -- NULL when uploaded
    created_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    immutable_from     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_document_version_no UNIQUE (id_document, version_no)
);

-- CREATE TABLE IF NOT EXISTS is a no-op on an existing table, so a column added
-- to the block above would never reach a database created before it. While the
-- service is unreleased and this file IS the migration, every added column needs
-- its idempotent ALTER here too, or re-applying schema.sql silently does nothing
-- and the failure surfaces much later as an UndefinedColumnError at runtime.
ALTER TABLE document_version ADD COLUMN IF NOT EXISTS docgen_request_id VARCHAR(64);

-- Answers "have I already attached this render?" from the result handler, which
-- knows only a request_id.
CREATE INDEX IF NOT EXISTS ix_document_version_docgen
    ON document_version (docgen_request_id)
    WHERE docgen_request_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_document_version_doc
    ON document_version (id_community, id_document);

DROP TRIGGER IF EXISTS trg_document_version_immutable ON document_version;
CREATE TRIGGER trg_document_version_immutable
    BEFORE UPDATE OR DELETE ON document_version
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

-- Circular reference: a document points at its current version, which points
-- back at the document. Added here, once both tables exist.
ALTER TABLE document DROP CONSTRAINT IF EXISTS fk_document_current_version;
ALTER TABLE document ADD CONSTRAINT fk_document_current_version
    FOREIGN KEY (current_version_id) REFERENCES document_version (id);

-- ---- document_render -------------------------------------------------------
-- The IN-FLIGHT state of a generation (Phase 2). A row exists only while a
-- render is pending or has failed; success deletes it and the resulting
-- document_version becomes the record.
--
-- Deliberately NOT columns on `document`:
--   * document_version.file_ref is NOT NULL and the table is append-only, so a
--     version row cannot be written until the artifact exists -- the snapshot
--     has to live somewhere in between, and it must be captured at REQUEST time
--     (re-reading the CRM later would defeat the whole point of a snapshot).
--   * UNIQUE (id_document) makes "one render in flight per document" an INSERT
--     ... ON CONFLICT DO NOTHING, with no SELECT-then-UPDATE race.
--   * clearing the state is a DELETE, not six NULL assignments.
--
-- Render state is NEVER journaled in status_event: that journal is the
-- regulatory record and its head is what assert_status_matches_journal()
-- compares document.status against, so a technical retry written there would
-- both pollute the evidence and break every later status change.
CREATE TABLE IF NOT EXISTS document_render (
    id                 SERIAL       PRIMARY KEY,
    id_community       INTEGER      NOT NULL,
    id_document        INTEGER      NOT NULL UNIQUE
                                    REFERENCES document (id) ON DELETE CASCADE,
    docgen_request_id  VARCHAR(64)  NOT NULL,
    render_state       INTEGER      NOT NULL,   -- RenderState: 1=pending, 2=failed
    render_error_json  JSONB,                   -- {code, message, permanent}
    id_template        INTEGER      NOT NULL REFERENCES document_template (id),
    data_snapshot_json JSONB        NOT NULL,   -- exactly what was sent to render
    requested_by       VARCHAR(255),            -- x-user-id
    requested_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    created_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- The ONLY index here not led by id_community, and deliberately so: the docgen
-- result handler arrives holding a request_id and does not yet know the tenant.
-- Tenancy is then read FROM this row -- never from the message, which is data
-- that round-tripped through another service.
CREATE UNIQUE INDEX IF NOT EXISTS uq_document_render_request
    ON document_render (docgen_request_id);

DROP TRIGGER IF EXISTS trg_document_render_updated_at ON document_render;
CREATE TRIGGER trg_document_render_updated_at
    BEFORE UPDATE ON document_render
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---- status_event ----------------------------------------------------------
-- THE JOURNAL (spec R1). Append-only and the source of truth for status.
-- subject_id is polymorphic (document or dossier) and deliberately carries no
-- foreign key: the audit trail must outlive the row it describes.
CREATE TABLE IF NOT EXISTS status_event (
    id            BIGSERIAL    PRIMARY KEY,
    id_community  INTEGER      NOT NULL,
    subject_type  INTEGER      NOT NULL,   -- SubjectType: 1=document, 2=dossier
    subject_id    INTEGER      NOT NULL,
    from_status   INTEGER,                 -- NULL on the birth event
    to_status     INTEGER      NOT NULL,
    is_corrective BOOLEAN      NOT NULL DEFAULT FALSE,
    actor_id      VARCHAR(255),            -- x-user-id (Keycloak sub)
    occurred_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    context_json  JSONB        NOT NULL DEFAULT '{}'::jsonb  -- submission_date, refs, reason
);

-- Latest-head lookup, and the timeline read.
CREATE INDEX IF NOT EXISTS ix_status_event_subject
    ON status_event (id_community, subject_type, subject_id, id DESC);

DROP TRIGGER IF EXISTS trg_status_event_immutable ON status_event;
CREATE TRIGGER trg_status_event_immutable
    BEFORE UPDATE OR DELETE ON status_event
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

-- ---- status cache <-> journal consistency ----------------------------------
-- Deferred to COMMIT so a subject and its birth event can be written in either
-- order inside one transaction (the subject's SERIAL id is only known after the
-- insert). Any UPDATE of a status column that is not accompanied by a matching
-- journal row aborts the transaction.
CREATE OR REPLACE FUNCTION assert_status_matches_journal()
RETURNS trigger AS $$
DECLARE
    v_subject_type INTEGER := TG_ARGV[0]::INTEGER;
    v_head         INTEGER;
BEGIN
    -- Deferred to COMMIT, so the status column and its journal row (written in
    -- either order within the transaction) are both visible. The service makes
    -- exactly one status change per subject per transaction, so the change's NEW
    -- value is the one that must equal the journal head.
    SELECT to_status INTO v_head
      FROM status_event
     WHERE subject_type = v_subject_type
       AND subject_id   = NEW.id
     ORDER BY id DESC
     LIMIT 1;

    IF v_head IS NULL THEN
        RAISE EXCEPTION
            '% % has status % but no status_event: every status change must be journaled',
            TG_TABLE_NAME, NEW.id, NEW.status
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    IF v_head IS DISTINCT FROM NEW.status THEN
        RAISE EXCEPTION
            '% % status % disagrees with journal head %: status is a cache, append a status_event instead',
            TG_TABLE_NAME, NEW.id, NEW.status, v_head
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_dossier_status_journaled ON dossier;
CREATE CONSTRAINT TRIGGER trg_dossier_status_journaled
    AFTER INSERT OR UPDATE OF status ON dossier
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION assert_status_matches_journal('2');

DROP TRIGGER IF EXISTS trg_document_status_journaled ON document;
CREATE CONSTRAINT TRIGGER trg_document_status_journaled
    AFTER INSERT OR UPDATE OF status ON document
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION assert_status_matches_journal('1');

-- ---- deadline --------------------------------------------------------------
CREATE TABLE IF NOT EXISTS deadline (
    id                    SERIAL       PRIMARY KEY,
    id_community          INTEGER      NOT NULL,
    id_dossier            INTEGER      NOT NULL REFERENCES dossier (id) ON DELETE CASCADE,
    deadline_type         VARCHAR(64)  NOT NULL,   -- DeadlineType
    due_date              DATE         NOT NULL,
    status                INTEGER      NOT NULL DEFAULT 1,  -- DeadlineStatus: 1=open
    recurring             BOOLEAN      NOT NULL DEFAULT FALSE,
    derived_from_event_id BIGINT       REFERENCES status_event (id),
    id_deadline_rule      INTEGER      REFERENCES deadline_rule (id),
    resolved_at           TIMESTAMPTZ,
    -- When the "due soon" reminder was emitted for THIS occurrence. The local
    -- idempotency marker for a sweep that does not mutate the row it reads:
    -- `outbound_message.dedupe_key` protects the email, but nothing protects the
    -- in-app notification, so a daily sweep would otherwise create a duplicate
    -- bell entry every day until the due date. A rolled recurring occurrence is
    -- a fresh INSERT, so it starts NULL and gets its own reminder.
    reminded_at           TIMESTAMPTZ,
    created_at            TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at            TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- CREATE TABLE IF NOT EXISTS never adds a column, so an added column needs an
-- idempotent ALTER beside it or re-applying this file silently does nothing.
ALTER TABLE deadline ADD COLUMN IF NOT EXISTS reminded_at TIMESTAMPTZ;

-- The dashboard read: everything open, soonest first.
CREATE INDEX IF NOT EXISTS ix_deadline_dashboard
    ON deadline (id_community, status, due_date);
CREATE INDEX IF NOT EXISTS ix_deadline_dossier ON deadline (id_dossier);
-- The reminder sweep: open, un-reminded, due inside the window.
CREATE INDEX IF NOT EXISTS ix_deadline_reminder
    ON deadline (id_community, due_date) WHERE status = 1 AND reminded_at IS NULL;

-- Idempotency: re-processing the same status_event cannot create a second
-- deadline of the same type. Partial, because a rolled recurring occurrence
-- carries no originating event (see below).
CREATE UNIQUE INDEX IF NOT EXISTS uq_deadline_event
    ON deadline (id_dossier, deadline_type, derived_from_event_id)
    WHERE derived_from_event_id IS NOT NULL;

-- A recurring obligation (annual reporting) has exactly ONE open occurrence at
-- a time; the next is only created when the current one is resolved. This index
-- is what makes the roll safe under concurrency and retries.
CREATE UNIQUE INDEX IF NOT EXISTS uq_deadline_open_recurring
    ON deadline (id_dossier, deadline_type)
    WHERE status = 1 AND recurring = TRUE;

DROP TRIGGER IF EXISTS trg_deadline_updated_at ON deadline;
CREATE TRIGGER trg_deadline_updated_at BEFORE UPDATE ON deadline
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
