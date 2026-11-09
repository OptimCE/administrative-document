-- ============================================================================
-- Migration 0003 — the procedures engine (espace administratif, phases 0+1).
--
-- Two parts, in one transaction:
--
--   1. SCHEMA. The engine's tables (procedure_definition, change,
--      correspondence, processed_event, module_activation, working_calendar),
--      the new columns of dossier / document / status_event / deadline /
--      deadline_rule, and the journal triggers: appending a status_event now
--      WRITES the subject's status (and a dossier's step), and a direct write
--      of either is refused.
--
--   2. DATA. Every July dossier becomes a procedure of the CWaPE catalogue —
--      one lifecycle, no parallel July one. The mapping was agreed before this
--      file was written (docs/specs/espace-administratif.md, data model):
--
--        July DossierType          procedure              scope       operation
--        1 CREATION_NOTIFICATION   CE_NOTIFICATION        community   -> NULL
--        2 MODIFICATION            CE_ACTUALISATION       community   -> NULL
--        3 ANNUAL_REPORT           CE_RAPPORT_ANNUEL      community   -> NULL, period_key = year
--        4 SHARING_AUTHORIZATION   PARTAGE_AUTORISATION   operation   kept
--        5 SHARING_MODIFICATION    PARTAGE_MODIF          operation   kept, variant authorization
--        6 CESSATION               PARTAGE_FIN            operation   kept
--
--        July DossierStatus   sharing types          CE types
--        1 IN_PREPARATION     DRAFT / draft          DRAFT / draft
--        2 SUBMITTED          SENT / sent            SENT / sent
--        3 COMPLETE           SENT / in_review       IN_FORCE / in_force
--        4 CLOSED             IN_FORCE / in_force    IN_FORCE / in_force
--        5 LAPSED             LAPSED / lapsed        LAPSED / lapsed
--
--      The operation a CE dossier loses is kept in metadata_json.legacy, with
--      its July type and status. The journal is append-only, so nothing in it
--      is rewritten: each converted dossier and document gets ONE new event,
--      actor 'system:migration', context {"migration": "0003"}, and the
--      propagation trigger moves its status. The July sendings and
--      acknowledgements are replayed as correspondence; open deadlines keep
--      their dates and gain (party, kind); the July deadline_rule rows,
--      community overrides included, are deleted — rules now come only from
--      the catalogue, whose sync relinks the converted deadlines by code.
--
-- The migration refuses to guess. It aborts, listing the rows, when a July
-- dossier is outside Wallonia, has a type, status or deadline it has no mapping
-- for, or when two July drafts would become two open drafts of one family.
--
-- Procedure definitions: the catalogue is YAML synced at deploy, AFTER this
-- runs, yet a converted dossier needs a definition to point at. So this file
-- RESERVES the BE-WAL-CWAPE v1 rows it uses (content_sha256 NULL); the sync
-- fills them once it has checked their scope, recipient, channel and family
-- against the YAML.
--
-- Idempotent and forward-only: every DDL statement is IF [NOT] EXISTS or
-- DROP-then-ADD, and the data part only touches July rows not yet converted
-- (dossier.id_procedure_definition IS NULL). Applied on top of a fresh
-- schema.sql install it changes nothing but the July deadline rules.
--
-- Uses ON COMMIT DROP temp tables, so it must run inside one transaction:
-- here BEGIN/COMMIT, in optimce-migrator the transaction the migrator opens.
-- ============================================================================

BEGIN;

-- ############################################################################
-- 1. SCHEMA
-- ############################################################################

-- ---- procedure_definition --------------------------------------------------
CREATE TABLE IF NOT EXISTS procedure_definition (
    id                    SERIAL       PRIMARY KEY,
    regulator             VARCHAR(32)  NOT NULL,
    code                  VARCHAR(64)  NOT NULL,
    version               INTEGER      NOT NULL,
    valid_from            DATE         NOT NULL,
    valid_to              DATE,
    scope                 SMALLINT     NOT NULL,
    recipient             SMALLINT     NOT NULL,
    channel               SMALLINT     NOT NULL,
    family                VARCHAR(64)  NOT NULL,
    handles_change_kinds  TEXT[]       NOT NULL DEFAULT '{}',
    escalation_rules_json JSONB        NOT NULL DEFAULT '{}'::jsonb,
    required_documents_json JSONB      NOT NULL DEFAULT '[]'::jsonb,
    transitions_json      JSONB        NOT NULL DEFAULT '{}'::jsonb,
    response_types_json   JSONB        NOT NULL DEFAULT '[]'::jsonb,
    labels_json           JSONB        NOT NULL DEFAULT '{}'::jsonb,
    content_sha256        VARCHAR(64),
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

CREATE UNIQUE INDEX IF NOT EXISTS uq_procedure_definition_current
    ON procedure_definition (regulator, code)
    WHERE valid_to IS NULL;

DROP TRIGGER IF EXISTS trg_procedure_definition_updated_at ON procedure_definition;
CREATE TRIGGER trg_procedure_definition_updated_at BEFORE UPDATE ON procedure_definition
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---- deadline_rule ---------------------------------------------------------
ALTER TABLE deadline_rule
    ALTER COLUMN region        DROP NOT NULL,
    ALTER COLUMN dossier_type  DROP NOT NULL,
    ALTER COLUMN trigger_event DROP NOT NULL,
    ALTER COLUMN deadline_type DROP NOT NULL,
    ALTER COLUMN offset_value  DROP NOT NULL,
    ALTER COLUMN offset_unit   DROP NOT NULL,
    ADD COLUMN IF NOT EXISTS id_procedure_definition INTEGER
        REFERENCES procedure_definition (id) ON DELETE CASCADE,
    ADD COLUMN IF NOT EXISTS code          VARCHAR(64),
    ADD COLUMN IF NOT EXISTS kind          SMALLINT,
    ADD COLUMN IF NOT EXISTS anchor_event  VARCHAR(64),
    ADD COLUMN IF NOT EXISTS party         SMALLINT,
    ADD COLUMN IF NOT EXISTS deadline_kind SMALLINT,
    ADD COLUMN IF NOT EXISTS annual_month  SMALLINT,
    ADD COLUMN IF NOT EXISTS annual_day    SMALLINT;

ALTER TABLE deadline_rule
    DROP CONSTRAINT IF EXISTS ck_deadline_rule_offset_unit,
    ADD CONSTRAINT ck_deadline_rule_offset_unit
        CHECK (offset_unit IS NULL OR offset_unit IN (1, 2, 3)),
    DROP CONSTRAINT IF EXISTS ck_deadline_rule_generation,
    ADD CONSTRAINT ck_deadline_rule_generation CHECK (COALESCE(
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
    );

DROP INDEX IF EXISTS uq_deadline_rule;
CREATE UNIQUE INDEX IF NOT EXISTS uq_deadline_rule
    ON deadline_rule (id_community, region, dossier_type, trigger_event, deadline_type)
    NULLS NOT DISTINCT
    WHERE id_procedure_definition IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_deadline_rule_code
    ON deadline_rule (id_procedure_definition, code)
    WHERE id_procedure_definition IS NOT NULL;

-- ---- dossier ---------------------------------------------------------------
ALTER TABLE dossier
    ALTER COLUMN id_sharing_operation DROP NOT NULL,
    ALTER COLUMN dossier_type         DROP NOT NULL,
    ADD COLUMN IF NOT EXISTS step                     VARCHAR(64),
    ADD COLUMN IF NOT EXISTS id_procedure_definition  INTEGER
        REFERENCES procedure_definition (id),
    ADD COLUMN IF NOT EXISTS regulator                VARCHAR(32),
    ADD COLUMN IF NOT EXISTS family                   VARCHAR(64),
    ADD COLUMN IF NOT EXISTS scope                    SMALLINT,
    ADD COLUMN IF NOT EXISTS grd_ref                  VARCHAR(64),
    ADD COLUMN IF NOT EXISTS variant                  VARCHAR(32),
    ADD COLUMN IF NOT EXISTS requested_effective_date DATE,
    ADD COLUMN IF NOT EXISTS effective_date           DATE,
    ADD COLUMN IF NOT EXISTS opened_by                SMALLINT,
    ADD COLUMN IF NOT EXISTS frozen_at                TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS period_key               VARCHAR(16),
    ADD COLUMN IF NOT EXISTS next_action_code         VARCHAR(64),
    ADD COLUMN IF NOT EXISTS next_action_due          DATE,
    ADD COLUMN IF NOT EXISTS effect_error_json        JSONB;

ALTER TABLE dossier
    DROP CONSTRAINT IF EXISTS ck_dossier_procedure,
    ADD CONSTRAINT ck_dossier_procedure CHECK (COALESCE(
        (id_procedure_definition IS NULL AND dossier_type IS NOT NULL
            AND id_sharing_operation IS NOT NULL)
        OR
        (id_procedure_definition IS NOT NULL AND regulator IS NOT NULL
            AND family IS NOT NULL AND scope IN (1, 2) AND opened_by IN (1, 2, 3)),
        FALSE)
    ),
    DROP CONSTRAINT IF EXISTS ck_dossier_scope_subject,
    ADD CONSTRAINT ck_dossier_scope_subject CHECK (
        scope IS NULL
        OR (scope = 1 AND id_sharing_operation IS NULL)
        OR (scope = 2 AND id_sharing_operation IS NOT NULL)
    );

CREATE UNIQUE INDEX IF NOT EXISTS uq_dossier_external_ref_family
    ON dossier (id_community, family, external_ref)
    WHERE external_ref IS NOT NULL AND family IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_dossier_open_draft
    ON dossier (id_community, id_sharing_operation, family, period_key) NULLS NOT DISTINCT
    WHERE status IN (1, 6) AND family IS NOT NULL;

-- ---- document --------------------------------------------------------------
ALTER TABLE document
    ADD COLUMN IF NOT EXISTS kind             SMALLINT,
    ADD COLUMN IF NOT EXISTS required         BOOLEAN NOT NULL DEFAULT TRUE,
    ADD COLUMN IF NOT EXISTS requirement_code VARCHAR(64),
    ADD COLUMN IF NOT EXISTS condition_json   JSONB,
    ADD COLUMN IF NOT EXISTS id_member        INTEGER;

ALTER TABLE document
    DROP CONSTRAINT IF EXISTS ck_document_kind,
    ADD CONSTRAINT ck_document_kind CHECK (kind IS NULL OR kind IN (1, 2, 3)),
    DROP CONSTRAINT IF EXISTS ck_document_requirement,
    ADD CONSTRAINT ck_document_requirement
        CHECK ((kind IS NULL) = (requirement_code IS NULL));

-- ---- status_event ----------------------------------------------------------
ALTER TABLE status_event
    ADD COLUMN IF NOT EXISTS from_step VARCHAR(64),
    ADD COLUMN IF NOT EXISTS to_step   VARCHAR(64);

ALTER TABLE status_event
    DROP CONSTRAINT IF EXISTS ck_status_event_step_subject,
    ADD CONSTRAINT ck_status_event_step_subject
        CHECK (subject_type = 2 OR (from_step IS NULL AND to_step IS NULL));

-- ---- the journal writes the status -----------------------------------------
-- Function bodies identical to schema.sql, where they are explained.
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
    IF v_subject_type = 2 THEN
        SELECT id_community, status, step INTO v_community, v_status, v_step
          FROM dossier WHERE id = NEW.id;
    ELSE
        SELECT id_community, status, NULL INTO v_community, v_status, v_step
          FROM document WHERE id = NEW.id;
    END IF;
    IF NOT FOUND THEN
        RETURN NULL;
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
CREATE TABLE IF NOT EXISTS correspondence (
    id                BIGSERIAL    PRIMARY KEY,
    id_community      INTEGER      NOT NULL,
    id_dossier        INTEGER      NOT NULL REFERENCES dossier (id),
    direction         SMALLINT     NOT NULL,
    counterparty      SMALLINT     NOT NULL,
    channel           SMALLINT     NOT NULL,
    type              VARCHAR(64)  NOT NULL,
    occurred_at       TIMESTAMPTZ  NOT NULL,
    subject           VARCHAR(255),
    body_text         TEXT,
    external_ref      VARCHAR(128),
    file_ref          VARCHAR(512),
    content_sha256    VARCHAR(64),
    content_type      VARCHAR(128),
    byte_size         BIGINT,
    original_filename VARCHAR(255),
    context_json      JSONB        NOT NULL DEFAULT '{}'::jsonb,
    recorded_by       VARCHAR(255),
    created_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_correspondence_direction CHECK (direction IN (1, 2)),
    CONSTRAINT ck_correspondence_counterparty CHECK (counterparty IN (1, 2, 3, 4)),
    CONSTRAINT ck_correspondence_channel CHECK (channel IN (1, 2, 3, 4))
);

CREATE INDEX IF NOT EXISTS ix_correspondence_dossier
    ON correspondence (id_community, id_dossier, occurred_at);

DROP TRIGGER IF EXISTS trg_correspondence_immutable ON correspondence;
CREATE TRIGGER trg_correspondence_immutable
    BEFORE UPDATE OR DELETE ON correspondence
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

-- ---- deadline --------------------------------------------------------------
ALTER TABLE deadline
    ADD COLUMN IF NOT EXISTS party SMALLINT,
    ADD COLUMN IF NOT EXISTS kind  SMALLINT,
    ADD COLUMN IF NOT EXISTS derived_from_correspondence_id BIGINT
        REFERENCES correspondence (id);

ALTER TABLE deadline
    DROP CONSTRAINT IF EXISTS ck_deadline_party_kind,
    ADD CONSTRAINT ck_deadline_party_kind CHECK (COALESCE(
        (party IS NULL AND kind IS NULL)
        OR (party IN (1, 2, 3) AND kind IN (1, 2, 3, 4)),
        FALSE)
    );

-- ---- change ----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS change (
    id                   BIGSERIAL    PRIMARY KEY,
    id_community         INTEGER      NOT NULL,
    id_sharing_operation INTEGER,
    kind                 VARCHAR(64)  NOT NULL,
    subject_type         SMALLINT     NOT NULL,
    subject_ref          VARCHAR(64)  NOT NULL,
    payload_json         JSONB        NOT NULL DEFAULT '{}'::jsonb,
    source_event_id      UUID,
    procedure_code       VARCHAR(64)  NOT NULL,
    status               SMALLINT     NOT NULL,
    id_dossier           INTEGER      REFERENCES dossier (id),
    detected_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    created_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_change_source UNIQUE (source_event_id, procedure_code),
    CONSTRAINT ck_change_subject_type CHECK (subject_type IN (1, 2, 3, 4, 5)),
    CONSTRAINT ck_change_status CHECK (status IN (1, 2, 3, 4, 5, 6, 7)),
    CONSTRAINT ck_change_filed CHECK (status NOT IN (2, 6, 7) OR id_dossier IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS ix_change_dossier ON change (id_community, id_dossier);
CREATE INDEX IF NOT EXISTS ix_change_subject
    ON change (id_community, id_sharing_operation, subject_type, subject_ref);

DROP TRIGGER IF EXISTS trg_change_updated_at ON change;
CREATE TRIGGER trg_change_updated_at BEFORE UPDATE ON change
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---- processed_event -------------------------------------------------------
CREATE TABLE IF NOT EXISTS processed_event (
    event_id     UUID         PRIMARY KEY,
    id_community INTEGER      NOT NULL,
    event_type   VARCHAR(64)  NOT NULL,
    outcome      SMALLINT     NOT NULL,
    processed_at TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_processed_event_community
    ON processed_event (id_community, processed_at);

-- ---- module_activation -----------------------------------------------------
CREATE TABLE IF NOT EXISTS module_activation (
    id                    SERIAL       PRIMARY KEY,
    id_community          INTEGER      NOT NULL,
    enabled_at            TIMESTAMPTZ  NOT NULL,
    disabled_at           TIMESTAMPTZ,
    baseline_json         JSONB,
    baseline_confirmed_at TIMESTAMPTZ,
    confirmed_by          VARCHAR(255),
    created_at            TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at            TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_module_activation_period
        CHECK (disabled_at IS NULL OR enabled_at <= disabled_at)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_module_activation_open
    ON module_activation (id_community)
    WHERE disabled_at IS NULL;

DROP TRIGGER IF EXISTS trg_module_activation_updated_at ON module_activation;
CREATE TRIGGER trg_module_activation_updated_at BEFORE UPDATE ON module_activation
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---- working_calendar ------------------------------------------------------
CREATE TABLE IF NOT EXISTS working_calendar (
    region VARCHAR(8)   NOT NULL,
    day    DATE         NOT NULL,
    label  VARCHAR(128) NOT NULL,
    PRIMARY KEY (region, day)
);

-- ############################################################################
-- 2. DATA — July dossiers become CWaPE procedures
-- ############################################################################

-- ---- the agreed mapping, as tables -----------------------------------------
CREATE TEMP TABLE m0003_type (
    dossier_type INTEGER  PRIMARY KEY,   -- July DossierType
    code         TEXT     NOT NULL,      -- CWaPE procedure = family
    scope        SMALLINT NOT NULL,      -- ProcedureScope
    recipient    SMALLINT NOT NULL,      -- Recipient
    channel      SMALLINT NOT NULL,      -- Channel
    variant      TEXT
) ON COMMIT DROP;

INSERT INTO m0003_type VALUES
    (1, 'CE_NOTIFICATION',      1, 2, 2, NULL),
    (2, 'CE_ACTUALISATION',     1, 2, 2, NULL),
    (3, 'CE_RAPPORT_ANNUEL',    1, 2, 2, NULL),
    (4, 'PARTAGE_AUTORISATION', 2, 1, 1, NULL),
    -- The July row does not say what changed: the authorization route is the
    -- one that cannot under-notify.
    (5, 'PARTAGE_MODIF',        2, 1, 1, 'authorization'),
    (6, 'PARTAGE_FIN',          2, 1, 1, NULL);

CREATE TEMP TABLE m0003_status (
    scope         SMALLINT NOT NULL,
    legacy_status INTEGER  NOT NULL,     -- July DossierStatus
    status        INTEGER  NOT NULL,     -- ProcedureStatus
    step          TEXT     NOT NULL,     -- CWaPE step
    PRIMARY KEY (scope, legacy_status)
) ON COMMIT DROP;

INSERT INTO m0003_status VALUES
    -- operation scope (sharing): COMPLETE only meant "declared complete"; the
    -- GRD/CWaPE review was still to come.
    (2, 1, 1, 'draft'), (2, 2, 2, 'sent'), (2, 3, 2, 'in_review'),
    -- CLOSED records no outcome; a refusal is corrected by a later transition.
    (2, 4, 7, 'in_force'), (2, 5, 5, 'lapsed'),
    -- community scope (CE): completeness is the whole procedure.
    (1, 1, 1, 'draft'), (1, 2, 2, 'sent'), (1, 3, 7, 'in_force'),
    (1, 4, 7, 'in_force'), (1, 5, 5, 'lapsed');

CREATE TEMP TABLE m0003_deadline (
    legacy_type TEXT     PRIMARY KEY,    -- July DeadlineType
    code        TEXT     NOT NULL,       -- CWaPE catalogue rule code
    party       SMALLINT,                -- DeadlineParty; NULL = the recipient's
    kind        SMALLINT NOT NULL        -- DeadlineKind
) ON COMMIT DROP;

INSERT INTO m0003_deadline VALUES
    ('completeness_check',        'completeness', NULL, 2),
    ('lapse',                     'lapse',        1,    3),
    ('modification_notification', 'submission',   1,    1),
    ('annual_report',             'submission',   1,    1);

-- A submission_date / acknowledged_date that is not a date reads as NULL here
-- (and the event's own timestamp is used) instead of aborting the migration.
-- OR REPLACE: unlike the temp tables, a temp function outlives the COMMIT, so a
-- second run in the same session would otherwise fail on it.
CREATE OR REPLACE FUNCTION pg_temp.m0003_date(v TEXT) RETURNS DATE AS $$
BEGIN
    RETURN v::date;
EXCEPTION WHEN others THEN
    RETURN NULL;
END;
$$ LANGUAGE plpgsql IMMUTABLE;

-- ---- refuse to guess -------------------------------------------------------
DO $$
DECLARE
    v_rows TEXT;
BEGIN
    SELECT string_agg(format('dossier %s (community %s, region %s)', id, id_community, region), '; ')
      INTO v_rows
      FROM dossier
     WHERE id_procedure_definition IS NULL AND region <> 1;
    IF v_rows IS NOT NULL THEN
        RAISE EXCEPTION '0003: July dossiers outside Wallonia have no catalogue to join: %', v_rows;
    END IF;

    SELECT string_agg(format('dossier %s (type %s, status %s)', d.id, d.dossier_type, d.status), '; ')
      INTO v_rows
      FROM dossier d
      LEFT JOIN m0003_type t ON t.dossier_type = d.dossier_type
      LEFT JOIN m0003_status s ON s.scope = t.scope AND s.legacy_status = d.status
     WHERE d.id_procedure_definition IS NULL AND s.status IS NULL;
    IF v_rows IS NOT NULL THEN
        RAISE EXCEPTION '0003: July dossiers with an unmapped type or status: %', v_rows;
    END IF;

    SELECT string_agg(format('document %s (status %s)', doc.id, doc.status), '; ')
      INTO v_rows
      FROM document doc
     WHERE doc.requirement_code IS NULL AND doc.status NOT IN (1, 2, 3, 4, 5);
    IF v_rows IS NOT NULL THEN
        RAISE EXCEPTION '0003: July documents with an unmapped status: %', v_rows;
    END IF;

    SELECT string_agg(format('deadline %s (%s)', dl.id, dl.deadline_type), '; ')
      INTO v_rows
      FROM deadline dl
      LEFT JOIN m0003_deadline map ON map.legacy_type = dl.deadline_type
     WHERE dl.party IS NULL AND map.code IS NULL;
    IF v_rows IS NOT NULL THEN
        RAISE EXCEPTION '0003: July deadlines of an unmapped type: %', v_rows;
    END IF;

    -- Two July drafts that would become two open drafts of one family: which
    -- one the changes belong to is a human decision.
    SELECT string_agg(ids, '; ')
      INTO v_rows
      FROM (
        SELECT format('community %s, %s: dossiers %s', d.id_community, t.code,
                      string_agg(d.id::text, ', ' ORDER BY d.id)) AS ids
          FROM dossier d
          JOIN m0003_type t ON t.dossier_type = d.dossier_type
         WHERE d.id_procedure_definition IS NULL AND d.status = 1
         GROUP BY d.id_community,
                  CASE WHEN t.scope = 2 THEN d.id_sharing_operation END,
                  t.code,
                  CASE WHEN t.code = 'CE_RAPPORT_ANNUEL'
                       THEN to_char(d.created_at AT TIME ZONE 'Europe/Brussels', 'YYYY') END
        HAVING count(*) > 1
      ) duplicates;
    IF v_rows IS NOT NULL THEN
        RAISE EXCEPTION '0003: July drafts that would be two open drafts of one family '
                        '(cancel or merge them first): %', v_rows;
    END IF;
END $$;

-- ---- reserve the catalogue rows the conversion points at -------------------
-- valid_from is a placeholder the sync overwrites from the YAML.
INSERT INTO procedure_definition
    (regulator, code, version, valid_from, scope, recipient, channel, family)
SELECT DISTINCT 'BE-WAL-CWAPE', t.code, 1, DATE '2026-01-01',
       t.scope, t.recipient, t.channel, t.code
  FROM dossier d
  JOIN m0003_type t ON t.dossier_type = d.dossier_type
 WHERE d.id_procedure_definition IS NULL
ON CONFLICT (regulator, code, version) DO NOTHING;

-- A definition the sync already wrote must agree with the mapping, or the
-- converted dossiers would contradict their own definition.
DO $$
DECLARE
    v_rows TEXT;
BEGIN
    SELECT string_agg(pd.code, ', ')
      INTO v_rows
      FROM procedure_definition pd
      JOIN m0003_type t ON t.code = pd.code
     WHERE pd.regulator = 'BE-WAL-CWAPE' AND pd.version = 1
       AND (pd.scope, pd.recipient, pd.channel, pd.family)
           IS DISTINCT FROM (t.scope, t.recipient, t.channel, t.code);
    IF v_rows IS NOT NULL THEN
        RAISE EXCEPTION '0003: catalogue definitions disagree with the July mapping: %', v_rows;
    END IF;
END $$;

-- ---- dossiers --------------------------------------------------------------
CREATE TEMP TABLE m0003_dossier ON COMMIT DROP AS
SELECT d.id,
       d.id_community,
       d.dossier_type,
       d.status                 AS legacy_status,
       d.id_sharing_operation   AS legacy_operation,
       t.code,
       t.scope,
       t.recipient,
       t.channel,
       t.variant,
       s.status                 AS new_status,
       s.step                   AS new_step,
       pd.id                    AS id_procedure_definition,
       CASE WHEN t.code = 'CE_RAPPORT_ANNUEL'
            THEN to_char(d.created_at AT TIME ZONE 'Europe/Brussels', 'YYYY') END
                                AS period_key,
       -- Sent, so frozen: no change is attached to it any more.
       CASE WHEN d.status <> 1 THEN
            COALESCE(d.submitted_at,
                     (SELECT min(e.occurred_at) FROM status_event e
                       WHERE e.subject_type = 2 AND e.subject_id = d.id AND e.to_status = 2),
                     d.updated_at)
       END                      AS frozen_at
  FROM dossier d
  JOIN m0003_type t ON t.dossier_type = d.dossier_type
  JOIN m0003_status s ON s.scope = t.scope AND s.legacy_status = d.status
  JOIN procedure_definition pd
    ON pd.regulator = 'BE-WAL-CWAPE' AND pd.code = t.code AND pd.version = 1
 WHERE d.id_procedure_definition IS NULL;

-- Every column but status and step, which only the journal writes (below).
UPDATE dossier d
   SET id_procedure_definition = m.id_procedure_definition,
       regulator               = 'BE-WAL-CWAPE',
       family                  = m.code,
       scope                   = m.scope,
       id_sharing_operation    = CASE WHEN m.scope = 1 THEN NULL ELSE d.id_sharing_operation END,
       variant                 = m.variant,
       opened_by               = 3,
       frozen_at               = m.frozen_at,
       period_key              = m.period_key,
       metadata_json           = d.metadata_json || jsonb_build_object(
           'legacy', jsonb_build_object(
               'migration', '0003',
               'dossier_type', m.dossier_type,
               'status', m.legacy_status,
               'id_sharing_operation', m.legacy_operation))
  FROM m0003_dossier m
 WHERE d.id = m.id;

INSERT INTO status_event
    (id_community, subject_type, subject_id, from_status, to_status,
     from_step, to_step, is_corrective, actor_id, context_json)
SELECT m.id_community, 2, m.id, m.legacy_status, m.new_status,
       NULL, m.new_step, FALSE, 'system:migration',
       jsonb_build_object('migration', '0003', 'legacy_status', m.legacy_status)
  FROM m0003_dossier m
 ORDER BY m.id;

-- ---- documents -------------------------------------------------------------
--   DRAFT        -> MISSING without a version, READY with one
--   READY / SENT / ACKNOWLEDGED -> READY (what was sent is frozen in its versions)
--   OBSOLETE     -> OBSOLETE, no longer required
CREATE TEMP TABLE m0003_document ON COMMIT DROP AS
SELECT doc.id,
       doc.id_community,
       doc.status AS legacy_status,
       CASE doc.status
            WHEN 1 THEN CASE WHEN doc.current_version_id IS NULL THEN 6 ELSE 2 END
            WHEN 5 THEN 5
            ELSE 2
       END        AS new_status,
       CASE WHEN doc.origin = 1 THEN 1 ELSE 2 END AS kind   -- generated : collected
  FROM document doc
  JOIN m0003_dossier m ON m.id = doc.id_dossier
 WHERE doc.requirement_code IS NULL;

UPDATE document doc
   SET kind             = m.kind,
       requirement_code = doc.doc_type,
       required         = (m.legacy_status <> 5)
  FROM m0003_document m
 WHERE doc.id = m.id;

INSERT INTO status_event
    (id_community, subject_type, subject_id, from_status, to_status,
     is_corrective, actor_id, context_json)
SELECT m.id_community, 1, m.id, m.legacy_status, m.new_status,
       FALSE, 'system:migration',
       jsonb_build_object('migration', '0003', 'legacy_status', m.legacy_status)
  FROM m0003_document m
 ORDER BY m.id;

-- ---- correspondence replayed from the July journal -------------------------
-- Every submission is an outbound 'sent', at its declared submission date.
INSERT INTO correspondence
    (id_community, id_dossier, direction, counterparty, channel, type,
     occurred_at, context_json, recorded_by)
SELECT e.id_community, m.id, 1,
       CASE WHEN m.recipient = 1 THEN 1 ELSE 2 END,     -- grd : regulator
       m.channel,
       'sent',
       COALESCE(pg_temp.m0003_date(e.context_json ->> 'submission_date')::timestamp
                    AT TIME ZONE 'Europe/Brussels',
                e.occurred_at),
       jsonb_build_object('migration', '0003', 'status_event_id', e.id),
       e.actor_id
  FROM status_event e
  JOIN m0003_dossier m ON m.id = e.subject_id
 WHERE e.subject_type = 2
   AND e.to_status = 2
   AND e.context_json ->> 'migration' IS NULL
 ORDER BY e.id;

-- Every document acknowledgement is an inbound completeness answer.
INSERT INTO correspondence
    (id_community, id_dossier, direction, counterparty, channel, type,
     occurred_at, external_ref, context_json, recorded_by)
SELECT e.id_community, m.id, 2,
       CASE WHEN m.recipient = 1 THEN 1 ELSE 2 END,
       1,                                               -- email
       CASE WHEN e.context_json ->> 'result' = 'incomplete'
            THEN 'incomplete_notice' ELSE 'completeness_ack' END,
       COALESCE(pg_temp.m0003_date(e.context_json ->> 'acknowledged_date')::timestamp
                    AT TIME ZONE 'Europe/Brussels',
                e.occurred_at),
       NULLIF(e.context_json ->> 'authority_file_ref', ''),
       jsonb_build_object('migration', '0003', 'status_event_id', e.id, 'id_document', doc.id),
       e.actor_id
  FROM status_event e
  JOIN document doc ON doc.id = e.subject_id
  JOIN m0003_dossier m ON m.id = doc.id_dossier
 WHERE e.subject_type = 1
   AND e.to_status = 4
   AND e.context_json ->> 'migration' IS NULL
 ORDER BY e.id;

-- The authority's file reference becomes the procedure's, where it has none
-- and the reference is unambiguous (one candidate, not used by another
-- procedure of the family).
UPDATE dossier d
   SET external_ref = ref.external_ref
  FROM (
        SELECT DISTINCT ON (c.id_dossier) c.id_dossier, c.external_ref
          FROM correspondence c
          JOIN m0003_dossier m ON m.id = c.id_dossier
         WHERE c.external_ref IS NOT NULL
         ORDER BY c.id_dossier, c.occurred_at, c.id
       ) ref
 WHERE d.id = ref.id_dossier
   AND d.external_ref IS NULL
   AND NOT EXISTS (
        SELECT 1 FROM dossier o
         WHERE o.id_community = d.id_community
           AND o.id <> d.id
           AND (o.family = d.family OR o.dossier_type = d.dossier_type)
           AND o.external_ref = ref.external_ref)
   AND NOT EXISTS (
        SELECT 1 FROM correspondence c2
          JOIN dossier o2 ON o2.id = c2.id_dossier
         WHERE o2.id <> d.id
           AND o2.id_community = d.id_community
           AND o2.family = d.family
           AND c2.external_ref = ref.external_ref);

-- ---- deadlines -------------------------------------------------------------
-- Dates kept; (party, kind) added; the type renamed to the catalogue rule code
-- the sync relinks by. An annual report no longer rolls: each year is its own
-- procedure (period_key).
UPDATE deadline dl
   SET party            = COALESCE(map.party, CASE WHEN m.recipient = 1 THEN 2 ELSE 3 END),
       kind             = map.kind,
       deadline_type    = map.code,
       recurring        = FALSE,
       id_deadline_rule = NULL
  FROM m0003_dossier m, m0003_deadline map
 WHERE dl.id_dossier = m.id
   AND dl.party IS NULL
   AND map.legacy_type = dl.deadline_type;

-- ---- the July rules ----------------------------------------------------------
-- Defaults and community overrides alike: rules come from the catalogue now.
UPDATE deadline
   SET id_deadline_rule = NULL
 WHERE id_deadline_rule IN (SELECT id FROM deadline_rule WHERE id_procedure_definition IS NULL);
DELETE FROM deadline_rule WHERE id_procedure_definition IS NULL;

INSERT INTO schema_version (version, description)
VALUES (3, 'Procedures engine: catalogue, changes, correspondence; July dossiers converted')
ON CONFLICT DO NOTHING;

COMMIT;
