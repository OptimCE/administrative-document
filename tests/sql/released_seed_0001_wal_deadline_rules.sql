-- ============================================================================
-- Reference data: Walloon (CWaPE) regulatory deadline rules.
--
-- These are PLATFORM DEFAULTS: id_community IS NULL. A community may override an
-- individual rule by inserting a row with its own id_community; resolution is
-- per deadline_type, most-specific-wins (see domain/deadlines.py::resolve_rules).
--
-- Coded values (shared/const.py):
--   region       1 = WAL
--   dossier_type 1 = CREATION_NOTIFICATION, 2 = MODIFICATION, 3 = ANNUAL_REPORT,
--                4 = SHARING_AUTHORIZATION, 5 = SHARING_MODIFICATION, 6 = CESSATION
--   offset_unit  1 = business_days (jours ouvres), 2 = months (calendar)
--
-- Idempotent: the unique index on
-- (id_community, region, dossier_type, trigger_event, deadline_type) is
-- NULLS NOT DISTINCT, so re-running this file changes nothing.
-- ============================================================================

BEGIN;

INSERT INTO deadline_rule (
    id_community, region, dossier_type, trigger_event, deadline_type,
    offset_value, offset_unit, recurring, recur_months, description
) VALUES
    -- The CWaPE checks a notification dossier for completeness within 10
    -- business days of its submission.
    (NULL, 1, 1, 'dossier.submitted', 'completeness_check',
     10, 1, FALSE, NULL,
     'CWaPE completeness check, 10 business days after the notification is submitted'),

    -- A dossier acknowledged as INCOMPLETE lapses if it is not completed within
    -- six months. Calendar months: a legal period in months runs on the wall clock.
    (NULL, 1, 1, 'document.acknowledged.incomplete', 'lapse',
     6, 2, FALSE, NULL,
     'Incomplete notification dossier lapses 6 months after the first acknowledgment'),

    -- Once the dossier is complete the community owes an annual report, and owes
    -- one every year thereafter (rolled on resolution, one open occurrence at a time).
    (NULL, 1, 1, 'dossier.complete', 'annual_report',
     12, 2, TRUE, 12,
     'Annual reporting to the CWaPE, recurring every 12 months from completion'),

    -- A change to the community must be notified to the CWaPE within 15
    -- business days of the modification dossier being submitted.
    (NULL, 1, 2, 'dossier.modification', 'modification_notification',
     15, 1, FALSE, NULL,
     'CWaPE modification notification, 15 business days after the change is filed')
ON CONFLICT DO NOTHING;

COMMIT;
