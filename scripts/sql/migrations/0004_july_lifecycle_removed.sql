-- ============================================================================
-- Migration 0004 — the July lifecycle is gone (espace administratif, phase 1).
--
-- 0003 converted every July dossier into a procedure but had to keep the July
-- shape admissible while the July code still ran: nullable procedure columns,
-- CHECKs with a July branch. The July routes, state machine and sweep are now
-- removed, so a dossier, a piece and a deadline are always the engine's:
--
--   * dossier:  id_procedure_definition, regulator, family, scope, opened_by
--               and step NOT NULL; no July branch in its CHECKs; the July
--               indexes on dossier_type dropped;
--   * document: kind and requirement_code NOT NULL;
--   * deadline: party and kind NOT NULL; the July recurring roll's index
--     dropped (each year is its own procedure now);
--   * deadline_rule: the July rules still present are deleted (the dev seeds
--     re-inserted them on every provisioning after 0003). Its July columns and
--     its CHECK's July branch stay: on a fresh install the released reference
--     data (optimce-migrator 002) inserts the July rules into them before 0003
--     deletes them.
--
-- Columns are not dropped either (dossier.dossier_type, deadline.recurring…):
-- 0003 reads them on a fresh install, where it runs on top of schema.sql.
--
-- Refuses rather than guesses: anything still July-shaped (a row 0003 did not
-- convert, a July rule a deadline still points at) aborts the migration and
-- lists it.
--
-- Idempotent and forward-only. On top of 0003 on a fresh install it puts back
-- the CHECKs schema.sql had and 0003 loosened; otherwise it changes nothing.
-- ============================================================================

BEGIN;

-- ---- refuse anything July-shaped --------------------------------------------
DO $$
DECLARE
    v_found TEXT;
BEGIN
    SELECT string_agg(item, '; ') INTO v_found FROM (
        SELECT 'dossier ' || string_agg(id::text, ', ' ORDER BY id) AS item
          FROM dossier
         WHERE id_procedure_definition IS NULL OR regulator IS NULL OR family IS NULL
            OR scope IS NULL OR opened_by IS NULL OR step IS NULL
        HAVING count(*) > 0
        UNION ALL
        SELECT 'document ' || string_agg(id::text, ', ' ORDER BY id)
          FROM document
         WHERE kind IS NULL OR requirement_code IS NULL
        HAVING count(*) > 0
        UNION ALL
        SELECT 'deadline ' || string_agg(id::text, ', ' ORDER BY id)
          FROM deadline
         WHERE party IS NULL OR kind IS NULL
        HAVING count(*) > 0
        UNION ALL
        SELECT 'July rule still referenced ' || string_agg(DISTINCT r.id::text, ', ')
          FROM deadline_rule r
          JOIN deadline d ON d.id_deadline_rule = r.id
         WHERE r.id_procedure_definition IS NULL
        HAVING count(*) > 0
    ) found;
    IF v_found IS NOT NULL THEN
        RAISE EXCEPTION 'migration 0004: July-shaped rows remain, convert them first: %', v_found;
    END IF;
END $$;

-- ---- the July rules ----------------------------------------------------------
DELETE FROM deadline_rule WHERE id_procedure_definition IS NULL;

-- ---- dossier -----------------------------------------------------------------
ALTER TABLE dossier
    ALTER COLUMN id_procedure_definition SET NOT NULL,
    ALTER COLUMN regulator               SET NOT NULL,
    ALTER COLUMN family                  SET NOT NULL,
    ALTER COLUMN scope                   SET NOT NULL,
    ALTER COLUMN opened_by               SET NOT NULL,
    ALTER COLUMN step                    SET NOT NULL,
    DROP CONSTRAINT IF EXISTS ck_dossier_procedure,
    ADD CONSTRAINT ck_dossier_procedure CHECK (scope IN (1, 2) AND opened_by IN (1, 2, 3)),
    DROP CONSTRAINT IF EXISTS ck_dossier_scope_subject,
    ADD CONSTRAINT ck_dossier_scope_subject CHECK (
        (scope = 1 AND id_sharing_operation IS NULL)
        OR (scope = 2 AND id_sharing_operation IS NOT NULL)
    );

DROP INDEX IF EXISTS ix_dossier_type;
DROP INDEX IF EXISTS uq_dossier_external_ref;

-- ---- document ----------------------------------------------------------------
ALTER TABLE document
    ALTER COLUMN kind             SET NOT NULL,
    ALTER COLUMN requirement_code SET NOT NULL,
    DROP CONSTRAINT IF EXISTS ck_document_requirement,
    DROP CONSTRAINT IF EXISTS ck_document_kind,
    ADD CONSTRAINT ck_document_kind CHECK (kind IN (1, 2, 3));

-- ---- deadline ----------------------------------------------------------------
ALTER TABLE deadline
    ALTER COLUMN party SET NOT NULL,
    ALTER COLUMN kind  SET NOT NULL,
    DROP CONSTRAINT IF EXISTS ck_deadline_party_kind,
    ADD CONSTRAINT ck_deadline_party_kind CHECK (party IN (1, 2, 3) AND kind IN (1, 2, 3, 4));

DROP INDEX IF EXISTS uq_deadline_open_recurring;

INSERT INTO schema_version (version, description)
VALUES (4, 'July lifecycle removed: every dossier is a procedure')
ON CONFLICT DO NOTHING;

COMMIT;
