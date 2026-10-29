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
--   1. status_event, document_version and correspondence are APPEND-ONLY
--      (UPDATE/DELETE raise).
--   2. dossier.status/step and document.status are a *copy* of the
--      status_event journal head. Appending a journal row writes them (trigger
--      propagate_journal_head); writing them directly is refused; and a
--      deferred constraint trigger rejects, at COMMIT, a subject born with a
--      status nobody journaled. A rollback is therefore necessarily a new
--      traced event, never an in-place mutation.
--
-- Status codes are two-level since the procedures engine (migration 0003):
-- dossier.status holds only the engine's common statuses (shared/const.py
-- ProcedureStatus) and dossier.step the regulator plugin's own step, a string
-- the plugin validates. No regulator-specific code is ever a database enum.
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

-- Only version 1 is self-inserted, although this file also embodies version 3
-- (migrations/0003_procedures_engine.sql). Version 2 is reference DATA (the
-- seeds), not schema, so a database created from this file has not received
-- it yet: self-inserting 3 would make optimce-migrator see MAX(version) = 3 and
-- skip 2, leaving a fresh install with no template catalogue. 0003 is
-- idempotent against this file, so applying it on top of a fresh install is a
-- no-op for the schema.
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

-- ---- procedure_definition --------------------------------------------------
-- One versioned entry of a regulator's catalogue (a "type de démarche"). The
-- rows are a PROJECTION of the plugin's YAML files (regulators/<code>/catalogue),
-- written by the catalogue sync at deploy; nobody edits them by hand. A dossier
-- keeps the version it was opened under, so a catalogue revision is a new row
-- and never rewrites a running procedure.
--
-- Generic enums are coded integers (shared/const.py); everything regulator-
-- specific (codes, steps, response types, the transitions graph) is text or
-- JSON the plugin validates — adding a regulator needs no migration.
--
-- content_sha256 NULL marks a row RESERVED by a data migration (0003 converts
-- the July dossiers before any sync has run, and they need a definition to
-- point at). The sync fills a reserved row, after checking its identity
-- columns agree with the YAML; it refuses a filled row whose content changed
-- without a version bump.
CREATE TABLE IF NOT EXISTS procedure_definition (
    id                    SERIAL       PRIMARY KEY,
    regulator             VARCHAR(32)  NOT NULL,   -- plugin code = community.regulator
    code                  VARCHAR(64)  NOT NULL,   -- e.g. 'PARTAGE_MODIF'
    version               INTEGER      NOT NULL,
    valid_from            DATE         NOT NULL,
    valid_to              DATE,                    -- NULL = currently in force
    scope                 SMALLINT     NOT NULL,   -- ProcedureScope: 1=community, 2=operation
    recipient             SMALLINT     NOT NULL,   -- Recipient: 1=grd_ref, 2=regulator
    channel               SMALLINT     NOT NULL,   -- Channel: 1=email, 2=online_form, 3=post
    family                VARCHAR(64)  NOT NULL,   -- one open draft per family; v1: = code
    handles_change_kinds  TEXT[]       NOT NULL DEFAULT '{}',
    escalation_rules_json JSONB        NOT NULL DEFAULT '{}'::jsonb,  -- variant switching
    required_documents_json JSONB      NOT NULL DEFAULT '[]'::jsonb,
    transitions_json      JSONB        NOT NULL DEFAULT '{}'::jsonb,  -- steps + edges
    response_types_json   JSONB        NOT NULL DEFAULT '[]'::jsonb,
    labels_json           JSONB        NOT NULL DEFAULT '{}'::jsonb,  -- fr/en/nl/de
    content_sha256        VARCHAR(64),             -- NULL = reserved, not yet synced
    synced_at             TIMESTAMPTZ,
    created_at            TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at            TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_procedure_definition_version UNIQUE (regulator, code, version),
    CONSTRAINT ck_procedure_definition_validity
        CHECK (valid_to IS NULL OR valid_from <= valid_to),
    CONSTRAINT ck_procedure_definition_scope CHECK (scope IN (1, 2)),
    CONSTRAINT ck_procedure_definition_recipient CHECK (recipient IN (1, 2)),
    CONSTRAINT ck_procedure_definition_channel CHECK (channel IN (1, 2, 3))
);

-- At most one version in force per (regulator, code).
CREATE UNIQUE INDEX IF NOT EXISTS uq_procedure_definition_current
    ON procedure_definition (regulator, code)
    WHERE valid_to IS NULL;

DROP TRIGGER IF EXISTS trg_procedure_definition_updated_at ON procedure_definition;
CREATE TRIGGER trg_procedure_definition_updated_at BEFORE UPDATE ON procedure_definition
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---- deadline_rule ---------------------------------------------------------
-- The regulatory clocks, as data.
--
-- Two generations share the table until the July lifecycle is removed:
--   * LEGACY rows (id_procedure_definition IS NULL): "when <trigger_event>
--     happens on a dossier of <dossier_type> in <region>, a <deadline_type>
--     deadline falls due <offset_value> <offset_unit> later". Seeded by
--     seeds/0001, community-overridable, read only by the July code.
--   * CATALOGUE rows: belong to one procedure_definition, written by the
--     catalogue sync, never overridden per community. `code` names the rule
--     inside its procedure and is what deadline.deadline_type carries.
--     kind 1 (relative) = <offset_value> <offset_unit> after <anchor_event>;
--     kind 2 (annual) = on <annual_month>/<annual_day> of the period.
CREATE TABLE IF NOT EXISTS deadline_rule (
    id            SERIAL       PRIMARY KEY,
    id_community  INTEGER,                    -- NULL = platform default
    region        INTEGER,                    -- Region (legacy rows)
    dossier_type  INTEGER,                    -- DossierType (legacy rows)
    trigger_event VARCHAR(64),                -- TriggerEvent (legacy rows)
    deadline_type VARCHAR(64),                -- DeadlineType (legacy rows)
    offset_value  INTEGER,
    offset_unit   INTEGER,                    -- OffsetUnit: 1=business_days, 2=months, 3=calendar_days
    recurring     BOOLEAN      NOT NULL DEFAULT FALSE,
    recur_months  INTEGER,                    -- roll interval when recurring
    description   VARCHAR(255),
    id_procedure_definition INTEGER REFERENCES procedure_definition (id) ON DELETE CASCADE,
    code          VARCHAR(64),                -- rule code inside its procedure
    kind          SMALLINT,                   -- RuleKind: 1=relative, 2=annual
    anchor_event  VARCHAR(64),                -- plugin code of what starts the clock
    party         SMALLINT,                   -- DeadlineParty the deadline binds
    deadline_kind SMALLINT,                   -- DeadlineKind of the deadline produced
    annual_month  SMALLINT,
    annual_day    SMALLINT,
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_deadline_rule_recur
        CHECK (NOT recurring OR recur_months IS NOT NULL),
    CONSTRAINT ck_deadline_rule_offset_unit
        CHECK (offset_unit IS NULL OR offset_unit IN (1, 2, 3)),
    -- A row is wholly legacy or wholly catalogue, never a mix. COALESCE(…, FALSE)
    -- because a CHECK that evaluates to NULL PASSES: `kind IN (1, 2)` on a NULL
    -- kind would otherwise let an incomplete catalogue row through. Same below.
    CONSTRAINT ck_deadline_rule_generation CHECK (COALESCE(
        (id_procedure_definition IS NULL
            AND region IS NOT NULL AND dossier_type IS NOT NULL
            AND trigger_event IS NOT NULL AND deadline_type IS NOT NULL
            AND offset_value IS NOT NULL AND offset_unit IS NOT NULL)
        OR
        (id_procedure_definition IS NOT NULL AND id_community IS NULL
            AND code IS NOT NULL AND kind IN (1, 2)
            AND party IN (1, 2, 3) AND deadline_kind IN (1, 2, 3, 4)
            AND (kind <> 1 OR (anchor_event IS NOT NULL
                               AND offset_value IS NOT NULL AND offset_unit IS NOT NULL))
            AND (kind <> 2 OR (annual_month BETWEEN 1 AND 12
                               AND annual_day BETWEEN 1 AND 31))),
        FALSE)
    )
);

-- Legacy uniqueness only: catalogue rows leave every column of this index NULL
-- but deadline_type, so NULLS NOT DISTINCT would make two procedures' rules
-- collide. Catalogue rows are unique by (procedure, code) instead.
DROP INDEX IF EXISTS uq_deadline_rule;
CREATE UNIQUE INDEX IF NOT EXISTS uq_deadline_rule
    ON deadline_rule (id_community, region, dossier_type, trigger_event, deadline_type)
    NULLS NOT DISTINCT
    WHERE id_procedure_definition IS NULL;
CREATE INDEX IF NOT EXISTS ix_deadline_rule_lookup
    ON deadline_rule (region, dossier_type, trigger_event);
CREATE UNIQUE INDEX IF NOT EXISTS uq_deadline_rule_code
    ON deadline_rule (id_procedure_definition, code)
    WHERE id_procedure_definition IS NOT NULL;

DROP TRIGGER IF EXISTS trg_deadline_rule_updated_at ON deadline_rule;
CREATE TRIGGER trg_deadline_rule_updated_at BEFORE UPDATE ON deadline_rule
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ============================================================================
-- TENANT DATA (id_community NOT NULL — stamped from current_internal_community_id)
-- ============================================================================

-- ---- dossier ---------------------------------------------------------------
-- A procedure ("démarche"): one sending of one catalogue type to one recipient.
--
-- Scope decides the subject. An operation-scope procedure concerns exactly ONE
-- sharing operation (a community running several operations files one per
-- operation); a community-scope one (CE_NOTIFICATION, CE_RAPPORT_ANNUEL, …)
-- concerns none, so id_sharing_operation is NULL there. It is a cross-DB
-- reference (the operation lives in the CRM), hence a plain column and never a
-- foreign key — the service validates it against the CRM on create.
--
-- status is the engine's common status (ProcedureStatus) and step the plugin's
-- own; both are a copy of the journal head, written only by appending a
-- status_event (see propagate_journal_head below).
--
-- dossier_type is the July classification, kept until that lifecycle is removed:
-- a row with dossier_type and no procedure definition is a July dossier not yet
-- converted (migration 0003 converts them all).
CREATE TABLE IF NOT EXISTS dossier (
    id                   SERIAL       PRIMARY KEY,
    id_community         INTEGER      NOT NULL,
    id_sharing_operation INTEGER,                  -- cross-DB ref; NULL for community scope
    dossier_type         INTEGER,                  -- DossierType (July rows only)
    status               INTEGER      NOT NULL,    -- ProcedureStatus (journal-head copy)
    step                 VARCHAR(64),              -- plugin step (journal-head copy)
    region               INTEGER      NOT NULL,    -- resolved from community.regulator
    external_ref         VARCHAR(128),             -- CWaPE / DSO file reference
    title                VARCHAR(255),
    submitted_at         TIMESTAMPTZ,
    metadata_json        JSONB        NOT NULL DEFAULT '{}'::jsonb,
    id_procedure_definition INTEGER   REFERENCES procedure_definition (id),
    regulator            VARCHAR(32),              -- plugin code, kept from creation
    family               VARCHAR(64),
    scope                SMALLINT,                 -- ProcedureScope, copied from the definition
    grd_ref              VARCHAR(64),              -- reference DSO, when the recipient is one
    variant              VARCHAR(32),              -- plugin route; CWaPE: simple | authorization
    requested_effective_date DATE,
    effective_date       DATE,
    opened_by            SMALLINT,                 -- OpenedBy: 1=system, 2=user, 3=migration
    frozen_at            TIMESTAMPTZ,              -- sent: no change is attached after this
    period_key           VARCHAR(16),              -- calendar procedures: one per period
    next_action_code     VARCHAR(64),
    next_action_due      DATE,
    effect_error_json    JSONB,                    -- last effect write the core DB refused
    created_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    -- A row is a July dossier awaiting conversion, or a complete procedure.
    CONSTRAINT ck_dossier_procedure CHECK (COALESCE(
        (id_procedure_definition IS NULL AND dossier_type IS NOT NULL
            AND id_sharing_operation IS NOT NULL)
        OR
        (id_procedure_definition IS NOT NULL AND regulator IS NOT NULL
            AND family IS NOT NULL AND scope IN (1, 2) AND opened_by IN (1, 2, 3)),
        FALSE)
    ),
    CONSTRAINT ck_dossier_scope_subject CHECK (
        scope IS NULL
        OR (scope = 1 AND id_sharing_operation IS NULL)
        OR (scope = 2 AND id_sharing_operation IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS ix_dossier_status ON dossier (id_community, status);
CREATE INDEX IF NOT EXISTS ix_dossier_type   ON dossier (id_community, dossier_type);
-- The per-operation view: "everything filed for this sharing operation".
CREATE INDEX IF NOT EXISTS ix_dossier_operation
    ON dossier (id_community, id_sharing_operation);
-- An authority reference identifies exactly one dossier of a given type (July
-- rows) and one procedure of a given family (every converted or engine row).
CREATE UNIQUE INDEX IF NOT EXISTS uq_dossier_external_ref
    ON dossier (id_community, dossier_type, external_ref)
    WHERE external_ref IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_dossier_external_ref_family
    ON dossier (id_community, family, external_ref)
    WHERE external_ref IS NOT NULL AND family IS NOT NULL;
-- ONE open draft per operation (or community) and family — the drawer every
-- incoming change is filed into. Draft and ready are the open statuses
-- (ProcedureStatus 1 and 6). period_key splits calendar procedures: the 2026
-- annual report still open does not block opening 2027's.
CREATE UNIQUE INDEX IF NOT EXISTS uq_dossier_open_draft
    ON dossier (id_community, id_sharing_operation, family, period_key) NULLS NOT DISTINCT
    WHERE status IN (1, 6) AND family IS NOT NULL;

DROP TRIGGER IF EXISTS trg_dossier_updated_at ON dossier;
CREATE TRIGGER trg_dossier_updated_at BEFORE UPDATE ON dossier
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---- document --------------------------------------------------------------
-- A piece of a procedure: required by the catalogue (requirement_code, the
-- plugin's code; for CWaPE it is the July doc_type) and generated, collected
-- from a participant (id_member) or received from the recipient. "Generating"
-- is never a status: it is read from document_render.
CREATE TABLE IF NOT EXISTS document (
    id                 SERIAL       PRIMARY KEY,
    id_community       INTEGER      NOT NULL,
    id_dossier         INTEGER      NOT NULL REFERENCES dossier (id) ON DELETE CASCADE,
    doc_type           VARCHAR(64)  NOT NULL,   -- template key the generation resolves
    origin             INTEGER      NOT NULL,   -- DocOrigin: 1=generated, 2=uploaded
    status             INTEGER      NOT NULL,   -- ProcedureDocumentStatus (journal-head copy)
    current_version_id INTEGER,                 -- FK added after document_version exists
    title              VARCHAR(255),
    kind               SMALLINT,                -- DocumentKind: 1=generated, 2=collected, 3=received
    required           BOOLEAN      NOT NULL DEFAULT TRUE,
    requirement_code   VARCHAR(64),             -- plugin piece code
    condition_json     JSONB,                   -- why a conditional piece is required
    id_member          INTEGER,                 -- cross-DB ref: who supplies or signs it
    created_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_document_kind CHECK (kind IS NULL OR kind IN (1, 2, 3)),
    -- A piece is a July document awaiting conversion, or fully described.
    CONSTRAINT ck_document_requirement
        CHECK ((kind IS NULL) = (requirement_code IS NULL))
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
    -- The plugin's step, journaled in the same row as the common status. Only a
    -- dossier has steps.
    from_step     VARCHAR(64),
    to_step       VARCHAR(64),
    is_corrective BOOLEAN      NOT NULL DEFAULT FALSE,
    actor_id      VARCHAR(255),            -- x-user-id (Keycloak sub)
    occurred_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    context_json  JSONB        NOT NULL DEFAULT '{}'::jsonb,  -- submission_date, refs, reason
    CONSTRAINT ck_status_event_step_subject
        CHECK (subject_type = 2 OR (from_step IS NULL AND to_step IS NULL))
);

-- Latest-head lookup, and the timeline read.
CREATE INDEX IF NOT EXISTS ix_status_event_subject
    ON status_event (id_community, subject_type, subject_id, id DESC);

DROP TRIGGER IF EXISTS trg_status_event_immutable ON status_event;
CREATE TRIGGER trg_status_event_immutable
    BEFORE UPDATE OR DELETE ON status_event
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

-- ---- the journal writes the status -----------------------------------------
-- Appending a status_event copies its (to_status, to_step) onto the subject.
-- That makes the journal the ONLY writer of the status columns: the guard below
-- refuses a direct write, and this function's UPDATE is the one write it lets
-- through (it runs one trigger level down). Scoped by id_community, so a
-- journal row can never move another tenant's subject.
--
-- A subject journaled before it exists (its id taken from the sequence first)
-- is simply not found here; its INSERT then carries the status, which the
-- deferred check below verifies against this same journal row.
CREATE OR REPLACE FUNCTION propagate_journal_head()
RETURNS trigger AS $$
BEGIN
    IF NEW.subject_type = 2 THEN
        UPDATE dossier
           SET status = NEW.to_status, step = NEW.to_step
         WHERE id = NEW.subject_id
           AND id_community = NEW.id_community
           AND (status IS DISTINCT FROM NEW.to_status OR step IS DISTINCT FROM NEW.to_step);
    ELSIF NEW.subject_type = 1 THEN
        UPDATE document
           SET status = NEW.to_status
         WHERE id = NEW.subject_id
           AND id_community = NEW.id_community
           AND status IS DISTINCT FROM NEW.to_status;
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_status_event_propagate ON status_event;
CREATE TRIGGER trg_status_event_propagate
    AFTER INSERT ON status_event
    FOR EACH ROW EXECUTE FUNCTION propagate_journal_head();

-- Refuse any statement that moves a status (or step) by hand. A statement
-- issued by a client runs this at trigger depth 1; propagate_journal_head's
-- UPDATE runs it at depth 2 and is let through. Rewriting the same value is not
-- a move, so an ORM flush that re-sends an unchanged status is harmless. The
-- step is compared through to_jsonb because document has no step column.
CREATE OR REPLACE FUNCTION forbid_direct_status_write()
RETURNS trigger AS $$
BEGIN
    IF pg_trigger_depth() = 1
       AND (NEW.status IS DISTINCT FROM OLD.status
            OR to_jsonb(NEW) -> 'step' IS DISTINCT FROM to_jsonb(OLD) -> 'step') THEN
        RAISE EXCEPTION
            '% %: status is written by appending a status_event, never directly',
            TG_TABLE_NAME, OLD.id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_dossier_status_guard ON dossier;
CREATE TRIGGER trg_dossier_status_guard
    BEFORE UPDATE OF status, step ON dossier
    FOR EACH ROW EXECUTE FUNCTION forbid_direct_status_write();

DROP TRIGGER IF EXISTS trg_document_status_guard ON document;
CREATE TRIGGER trg_document_status_guard
    BEFORE UPDATE OF status ON document
    FOR EACH ROW EXECUTE FUNCTION forbid_direct_status_write();

-- ---- status copy <-> journal consistency -----------------------------------
-- Deferred to COMMIT so a subject and its birth event can be written in either
-- order inside one transaction (the subject's SERIAL id is only known after the
-- insert). A subject whose status is not the head of its journal at COMMIT —
-- typically one born with a status nobody journaled — aborts the transaction.
CREATE OR REPLACE FUNCTION assert_status_matches_journal()
RETURNS trigger AS $$
DECLARE
    v_subject_type INTEGER := TG_ARGV[0]::INTEGER;
    v_community    INTEGER;
    v_status       INTEGER;
    v_step         TEXT;
    v_head_status  INTEGER;
    v_head_step    TEXT;
BEGIN
    -- The row as it stands NOW, not NEW: NEW is the row as of the event that
    -- queued this check, and since the journal writes the status itself a
    -- subject may change several times in one transaction. Every queued check
    -- therefore compares the final row with the final head.
    IF v_subject_type = 2 THEN
        SELECT id_community, status, step INTO v_community, v_status, v_step
          FROM dossier WHERE id = NEW.id;
    ELSE
        SELECT id_community, status, NULL INTO v_community, v_status, v_step
          FROM document WHERE id = NEW.id;
    END IF;
    IF NOT FOUND THEN
        RETURN NULL;  -- deleted later in the same transaction
    END IF;

    SELECT to_status, to_step INTO v_head_status, v_head_step
      FROM status_event
     WHERE id_community = v_community
       AND subject_type = v_subject_type
       AND subject_id   = NEW.id
     ORDER BY id DESC
     LIMIT 1;

    IF NOT FOUND THEN
        RAISE EXCEPTION
            '% % has status % but no status_event: every status change must be journaled',
            TG_TABLE_NAME, NEW.id, v_status
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    IF v_head_status IS DISTINCT FROM v_status OR v_head_step IS DISTINCT FROM v_step THEN
        RAISE EXCEPTION
            '% % status %/% disagrees with journal head %/%: append a status_event instead',
            TG_TABLE_NAME, NEW.id, v_status, v_step, v_head_status, v_head_step
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_dossier_status_journaled ON dossier;
CREATE CONSTRAINT TRIGGER trg_dossier_status_journaled
    AFTER INSERT OR UPDATE OF status, step ON dossier
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION assert_status_matches_journal('2');

DROP TRIGGER IF EXISTS trg_document_status_journaled ON document;
CREATE CONSTRAINT TRIGGER trg_document_status_journaled
    AFTER INSERT OR UPDATE OF status ON document
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION assert_status_matches_journal('1');

-- ---- correspondence --------------------------------------------------------
-- APPEND-ONLY. Every message sent or received for a procedure — the sending
-- itself, an acknowledgement, a request for complements, a decision, an
-- amendment — with its archived file. `type` is the plugin's code
-- (CWaPE: sent | receipt | completeness_ack | incomplete_notice | …); a
-- correction is a new row, never an edit. file_ref is content-addressed like
-- document_version's.
CREATE TABLE IF NOT EXISTS correspondence (
    id                BIGSERIAL    PRIMARY KEY,
    id_community      INTEGER      NOT NULL,
    id_dossier        INTEGER      NOT NULL REFERENCES dossier (id),
    direction         SMALLINT     NOT NULL,   -- CorrespondenceDirection: 1=outbound, 2=inbound
    counterparty      SMALLINT     NOT NULL,   -- Counterparty: 1=grd, 2=regulator, 3=participant, 4=other
    channel           SMALLINT     NOT NULL,   -- Channel: 1=email, 2=online_form, 3=post, 4=upload
    type              VARCHAR(64)  NOT NULL,   -- plugin code
    occurred_at       TIMESTAMPTZ  NOT NULL,   -- when it was sent or received, as declared
    subject           VARCHAR(255),
    body_text         TEXT,
    external_ref      VARCHAR(128),            -- the reference the message carries
    file_ref          VARCHAR(512),
    content_sha256    VARCHAR(64),
    content_type      VARCHAR(128),
    byte_size         BIGINT,
    original_filename VARCHAR(255),
    context_json      JSONB        NOT NULL DEFAULT '{}'::jsonb,
    recorded_by       VARCHAR(255),            -- x-user-id, or 'system:…'
    created_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_correspondence_direction CHECK (direction IN (1, 2)),
    CONSTRAINT ck_correspondence_counterparty CHECK (counterparty IN (1, 2, 3, 4)),
    CONSTRAINT ck_correspondence_channel CHECK (channel IN (1, 2, 3, 4))
);

-- The procedure's thread, oldest first.
CREATE INDEX IF NOT EXISTS ix_correspondence_dossier
    ON correspondence (id_community, id_dossier, occurred_at);

DROP TRIGGER IF EXISTS trg_correspondence_immutable ON correspondence;
CREATE TRIGGER trg_correspondence_immutable
    BEFORE UPDATE OR DELETE ON correspondence
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

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
    -- Who must act and what kind of clock this is. NULL on a July deadline not
    -- yet converted (migration 0003 converts them all).
    party                 SMALLINT,    -- DeadlineParty: 1=community, 2=grd, 3=regulator
    kind                  SMALLINT,    -- DeadlineKind: 1=submission, 2=response, 3=lapse, 4=effect
    -- The message that started this clock (an incomplete notice, a decision…).
    derived_from_correspondence_id BIGINT REFERENCES correspondence (id),
    created_at            TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at            TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_deadline_party_kind CHECK (COALESCE(
        (party IS NULL AND kind IS NULL)
        OR (party IN (1, 2, 3) AND kind IN (1, 2, 3, 4)),
        FALSE)
    )
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

-- ============================================================================
-- PROCEDURES ENGINE (espace administratif)
--
-- The engine turns core events (core.evt.* on the CORE_EVENTS stream) into
-- changes, files them into procedures, and tracks activation per community.
-- ============================================================================

-- ---- change ----------------------------------------------------------------
-- A gap between the CRM and what the authorities know, born from one core event
-- (or from the baseline, or by hand). `kind` is the engine's vocabulary
-- ('meter_added', 'key_changed', …), identical for every regulator; which
-- procedure it feeds is the regulator's catalogue decision, so one event can
-- produce one change per target procedure — hence the unique pair below.
CREATE TABLE IF NOT EXISTS change (
    id                   BIGSERIAL    PRIMARY KEY,
    id_community         INTEGER      NOT NULL,
    id_sharing_operation INTEGER,                 -- cross-DB ref; NULL for community scope
    kind                 VARCHAR(64)  NOT NULL,
    subject_type         SMALLINT     NOT NULL,   -- ChangeSubjectType: 1=meter, 2=key, 3=member, 4=operation, 5=community
    subject_ref          VARCHAR(64)  NOT NULL,   -- EAN, key id, member id, …
    payload_json         JSONB        NOT NULL DEFAULT '{}'::jsonb,  -- snapshot: EAN, member, GRD, direction
    source_event_id      UUID,                    -- NULL for a baseline or manual change
    procedure_code       VARCHAR(64)  NOT NULL,   -- the catalogue type it targets
    status               SMALLINT     NOT NULL,   -- ChangeStatus
    id_dossier           INTEGER      REFERENCES dossier (id),
    detected_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    created_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_change_source UNIQUE (source_event_id, procedure_code),
    CONSTRAINT ck_change_subject_type CHECK (subject_type IN (1, 2, 3, 4, 5)),
    CONSTRAINT ck_change_status CHECK (status IN (1, 2, 3, 4, 5, 6, 7)),
    -- attached, applied and refused changes belong to a procedure.
    CONSTRAINT ck_change_filed CHECK (status NOT IN (2, 6, 7) OR id_dossier IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS ix_change_dossier ON change (id_community, id_dossier);
-- Compensation: "is the inverse of this change still sitting in a draft?"
CREATE INDEX IF NOT EXISTS ix_change_subject
    ON change (id_community, id_sharing_operation, subject_type, subject_ref);

DROP TRIGGER IF EXISTS trg_change_updated_at ON change;
CREATE TRIGGER trg_change_updated_at BEFORE UPDATE ON change
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---- processed_event -------------------------------------------------------
-- Every core event the consumer has handled, INCLUDING the ones acknowledged
-- without creating anything (module disabled, regulator without a plugin, …).
-- It is the consumer's idempotency key: a redelivered event finds its row and
-- is acknowledged again, and nothing else happens.
CREATE TABLE IF NOT EXISTS processed_event (
    event_id     UUID         PRIMARY KEY,
    id_community INTEGER      NOT NULL,
    event_type   VARCHAR(64)  NOT NULL,
    outcome      SMALLINT     NOT NULL,   -- EventOutcome
    processed_at TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_processed_event_community
    ON processed_event (id_community, processed_at);

-- ---- module_activation -----------------------------------------------------
-- One row per period during which the module was active for a community. The
-- activation itself is the CRM's community_subscription (shared by every
-- annexe, with no history); this table dates the start of the tracking and
-- holds the baseline (état de départ) photographed at that start.
CREATE TABLE IF NOT EXISTS module_activation (
    id                    SERIAL       PRIMARY KEY,
    id_community          INTEGER      NOT NULL,
    enabled_at            TIMESTAMPTZ  NOT NULL,
    disabled_at           TIMESTAMPTZ,
    baseline_json         JSONB,
    baseline_confirmed_at TIMESTAMPTZ,
    confirmed_by          VARCHAR(255),          -- x-user-id
    created_at            TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at            TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_module_activation_period
        CHECK (disabled_at IS NULL OR enabled_at <= disabled_at)
);

-- At most one open period per community.
CREATE UNIQUE INDEX IF NOT EXISTS uq_module_activation_open
    ON module_activation (id_community)
    WHERE disabled_at IS NULL;

DROP TRIGGER IF EXISTS trg_module_activation_updated_at ON module_activation;
CREATE TRIGGER trg_module_activation_updated_at BEFORE UPDATE ON module_activation
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---- working_calendar ------------------------------------------------------
-- The public holidays of a region, as data: one row per non-working weekday,
-- loaded from the plugin's calendar file (regulators/<code>/calendar/<region>.yaml).
-- `region` is the plugin's region string ('WAL'), not the Region int code.
CREATE TABLE IF NOT EXISTS working_calendar (
    region VARCHAR(8)   NOT NULL,
    day    DATE         NOT NULL,
    label  VARCHAR(128) NOT NULL,
    PRIMARY KEY (region, day)
);
