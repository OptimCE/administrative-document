-- ============================================================================
-- Migration 0005 — draft and final pieces (espace administratif, phase 2).
--
-- A generated piece now has working drafts (stamped with the plugin's
-- watermark) and finals (unstamped, what is sent), and a sent piece is final in
-- the database's own terms:
--
--   * document_version: stage (1 draft, 2 final, 3 to sign), the watermark
--     docgen stamped, the CRM pre-fill before the reviewer's edits, and what
--     produced the bytes (template_version, renderer_json). Every existing
--     version is FINAL: none was ever stamped, and an upload is final anyway.
--     A CHECK keeps the stage and the watermark consistent.
--   * document.id_template: the template a piece renders with, pinned. A piece
--     that already has a generated version keeps that version's template; the
--     others get the one in force today, as a piece born now would.
--   * document_render: purpose (render, or verify an existing version), and the
--     stage, watermark and pre-fill the version will carry.
--   * version_check (new, append-only): each regeneration of a version from its
--     own snapshot, and whether docgen produced the same bytes.
--   * triggers: a template a render used keeps its bundle (and is never
--     deleted); a sent procedure is never deleted, and its pieces take no new
--     version (a received piece excepted), are never deleted, and change only
--     their status.
--
-- Idempotent and forward-only. On a fresh install (schema.sql already has all
-- of this) it changes nothing.
-- ============================================================================

BEGIN;

-- ---- document_version --------------------------------------------------------
-- ADD COLUMN with a constant default is a catalog change in PostgreSQL 11+: no
-- row is rewritten and no UPDATE trigger fires, which is the only way to give
-- an append-only table a NOT NULL column. The default then goes: a writer
-- always says which stage it writes.
ALTER TABLE document_version
    ADD COLUMN IF NOT EXISTS stage SMALLINT NOT NULL DEFAULT 2,
    ADD COLUMN IF NOT EXISTS watermark VARCHAR(40),
    ADD COLUMN IF NOT EXISTS prefill_json JSONB,
    ADD COLUMN IF NOT EXISTS template_version VARCHAR(64),
    ADD COLUMN IF NOT EXISTS renderer_json JSONB;
ALTER TABLE document_version ALTER COLUMN stage DROP DEFAULT;

ALTER TABLE document_version
    DROP CONSTRAINT IF EXISTS ck_document_version_stage,
    ADD CONSTRAINT ck_document_version_stage CHECK (COALESCE(
        (stage = 1 AND watermark IS NOT NULL AND docgen_request_id IS NOT NULL)
        OR (stage = 2 AND watermark IS NULL)
        OR (stage = 3 AND watermark IS NULL AND docgen_request_id IS NOT NULL),
        FALSE));

-- ---- document.id_template ------------------------------------------------------
ALTER TABLE document
    ADD COLUMN IF NOT EXISTS id_template INTEGER REFERENCES document_template (id);

-- The guard below refuses changes to a sent procedure's pieces, so it must not
-- exist while they are pinned (a re-run finds it from the first run).
DROP TRIGGER IF EXISTS trg_document_sent_guard ON document;

UPDATE document d
   SET id_template = v.id_template
  FROM document_version v
 WHERE v.id = d.current_version_id
   AND v.id_template IS NOT NULL
   AND d.id_template IS NULL;

-- Most specific first (the community's own override, then the platform
-- default), newest version first: the order get_current_template reads in.
UPDATE document d
   SET id_template = pick.id_template
  FROM (
        SELECT DISTINCT ON (piece.id) piece.id AS id_document, t.id AS id_template
          FROM document piece
          JOIN dossier p ON p.id = piece.id_dossier
          JOIN document_template t
            ON t.region = p.region
           AND t.doc_type = piece.doc_type
           AND t.valid_to IS NULL
           AND (t.id_community IS NULL OR t.id_community = piece.id_community)
         WHERE piece.id_template IS NULL
           AND piece.kind <> 3
         ORDER BY piece.id, t.id_community NULLS LAST, t.version DESC
       ) pick
 WHERE pick.id_document = d.id;

-- ---- document_render -----------------------------------------------------------
ALTER TABLE document_render
    ADD COLUMN IF NOT EXISTS purpose SMALLINT NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS stage SMALLINT NOT NULL DEFAULT 2,
    ADD COLUMN IF NOT EXISTS watermark VARCHAR(40),
    ADD COLUMN IF NOT EXISTS prefill_json JSONB,
    ADD COLUMN IF NOT EXISTS id_document_version INTEGER REFERENCES document_version (id);
ALTER TABLE document_render ALTER COLUMN purpose DROP DEFAULT;
ALTER TABLE document_render ALTER COLUMN stage DROP DEFAULT;

ALTER TABLE document_render
    DROP CONSTRAINT IF EXISTS ck_document_render_purpose,
    ADD CONSTRAINT ck_document_render_purpose CHECK (COALESCE(
        (purpose = 1 AND id_document_version IS NULL)
        OR (purpose = 2 AND id_document_version IS NOT NULL),
        FALSE)),
    DROP CONSTRAINT IF EXISTS ck_document_render_stage,
    ADD CONSTRAINT ck_document_render_stage CHECK (COALESCE(
        (stage = 1 AND watermark IS NOT NULL) OR (stage IN (2, 3) AND watermark IS NULL),
        FALSE));

-- ---- version_check -------------------------------------------------------------
CREATE TABLE IF NOT EXISTS version_check (
    id                  SERIAL       PRIMARY KEY,
    id_community        INTEGER      NOT NULL,
    id_document_version INTEGER      NOT NULL REFERENCES document_version (id),
    docgen_request_id   VARCHAR(64)  NOT NULL,
    outcome             SMALLINT     NOT NULL,
    content_sha256      VARCHAR(64),
    renderer_json       JSONB,
    error_json          JSONB,
    requested_by        VARCHAR(255),
    requested_at        TIMESTAMPTZ  NOT NULL,
    checked_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_version_check_request UNIQUE (docgen_request_id),
    CONSTRAINT ck_version_check_outcome CHECK (COALESCE(
        (outcome IN (1, 2) AND content_sha256 IS NOT NULL)
        OR (outcome = 3 AND error_json IS NOT NULL),
        FALSE))
);

CREATE INDEX IF NOT EXISTS ix_version_check_version
    ON version_check (id_community, id_document_version, id DESC);

DROP TRIGGER IF EXISTS trg_version_check_immutable ON version_check;
CREATE TRIGGER trg_version_check_immutable
    BEFORE UPDATE OR DELETE ON version_check
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

-- ---- a template a render used keeps its bundle ---------------------------------
CREATE OR REPLACE FUNCTION forbid_used_template_rewrite()
RETURNS trigger AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM document_version WHERE id_template = OLD.id)
       AND NOT EXISTS (SELECT 1 FROM document_render WHERE id_template = OLD.id) THEN
        RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
    END IF;
    IF TG_OP = 'DELETE'
       OR NEW.file_ref IS DISTINCT FROM OLD.file_ref
       OR NEW.version <> OLD.version
       OR NEW.region <> OLD.region
       OR NEW.doc_type <> OLD.doc_type
       OR NEW.output_format <> OLD.output_format
       OR NEW.id_community IS DISTINCT FROM OLD.id_community THEN
        RAISE EXCEPTION
            'document_template %: a render used it, so its bundle cannot change (% refused); register the next version instead',
            OLD.id, TG_OP
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_document_template_in_use ON document_template;
CREATE TRIGGER trg_document_template_in_use
    BEFORE UPDATE OR DELETE ON document_template
    FOR EACH ROW EXECUTE FUNCTION forbid_used_template_rewrite();

-- ---- a sent piece is final -----------------------------------------------------
CREATE OR REPLACE FUNCTION forbid_version_after_freeze()
RETURNS trigger AS $$
DECLARE
    v_kind   SMALLINT;
    v_frozen TIMESTAMPTZ;
BEGIN
    SELECT d.kind, p.frozen_at INTO v_kind, v_frozen
      FROM document d JOIN dossier p ON p.id = d.id_dossier
     WHERE d.id = NEW.id_document;
    IF v_frozen IS NOT NULL AND v_kind IS DISTINCT FROM 3 THEN
        RAISE EXCEPTION
            'document %: its procedure was sent at %, so it takes no new version',
            NEW.id_document, v_frozen
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_document_version_after_freeze ON document_version;
CREATE TRIGGER trg_document_version_after_freeze
    BEFORE INSERT ON document_version
    FOR EACH ROW EXECUTE FUNCTION forbid_version_after_freeze();

CREATE OR REPLACE FUNCTION forbid_sent_piece_rewrite()
RETURNS trigger AS $$
DECLARE
    v_frozen TIMESTAMPTZ;
BEGIN
    SELECT frozen_at INTO v_frozen FROM dossier WHERE id = OLD.id_dossier;
    IF v_frozen IS NOT NULL THEN
        IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'document %: its procedure was sent at %, so it cannot be deleted',
                OLD.id, v_frozen
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF OLD.kind <> 3
           AND (to_jsonb(NEW) - 'status' - 'updated_at')
               IS DISTINCT FROM (to_jsonb(OLD) - 'status' - 'updated_at') THEN
            RAISE EXCEPTION
                'document %: its procedure was sent at %, so only its status can change',
                OLD.id, v_frozen
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
    END IF;
    RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_document_sent_guard
    BEFORE UPDATE OR DELETE ON document
    FOR EACH ROW EXECUTE FUNCTION forbid_sent_piece_rewrite();

-- The procedure itself: a cascaded delete reaches its pieces after the
-- procedure row (and frozen_at with it) is gone.
CREATE OR REPLACE FUNCTION forbid_sent_procedure_delete()
RETURNS trigger AS $$
BEGIN
    IF OLD.frozen_at IS NOT NULL THEN
        RAISE EXCEPTION 'dossier %: it was sent at %, so it cannot be deleted',
            OLD.id, OLD.frozen_at
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN OLD;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_dossier_sent_guard ON dossier;
CREATE TRIGGER trg_dossier_sent_guard
    BEFORE DELETE ON dossier
    FOR EACH ROW EXECUTE FUNCTION forbid_sent_procedure_delete();

-- ---- the key description is registered ----------------------------------------
-- Seed 0004 writes the same row on the dev stack; ON CONFLICT keeps either
-- order harmless.
INSERT INTO document_template (
    id_community, region, doc_type, version, valid_from, valid_to,
    file_ref, mapping_json, output_format, label
) VALUES
    (NULL, 1, 'key_description', 1, DATE '2026-10-30', NULL,
     's3://optimce-templates/administrative-document/key_description/v1/',
     '{"engine": "jinja-html"}'::jsonb, 'pdf',
     'Description et formules de la cle de repartition (OptimCE)')
ON CONFLICT DO NOTHING;

INSERT INTO schema_version (version, description)
VALUES (5, 'Draft and final pieces: stages, pinned templates, sent pieces final')
ON CONFLICT DO NOTHING;

COMMIT;
