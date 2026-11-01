-- ============================================================================
-- Migration 0006 — notifications and complements (espace administratif, phase 4).
--
--   * deadline.reminders_sent: the rungs of the reminder ladder (J-14, J-7, J-2
--     before the due date) already told for a deadline of the community. The
--     July marker reminded_at held one window only; it stays for a third
--     party's late answer, told once, and is still stamped at the first rung.
--     A deadline reminded before the ladder was told every rung its reminder
--     day had reached.
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

INSERT INTO schema_version (version, description)
VALUES (6, 'Phase 4: reminder ladder')
ON CONFLICT DO NOTHING;

COMMIT;
