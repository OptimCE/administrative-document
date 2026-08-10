-- ============================================================================
-- Reference data: the Walloon (CWaPE) document catalogue.
--
-- PLATFORM DEFAULTS (id_community IS NULL). These rows register *which* forms
-- exist, in which mandated output format, and which version is currently in
-- force -- so the catalogue is queryable from day one and a document can already
-- record which template version it corresponds to.
--
-- file_ref and mapping_json are intentionally NULL: they hold the rendering
-- bundle and the field->CRM mapping, which arrive with Phase 2 (generation).
-- Registering a revised official form is then an INSERT of a new `version` row
-- plus setting the previous row's valid_to -- never a code change or a deploy.
--
-- Coded values: region 1 = WAL. Idempotent via the NULLS NOT DISTINCT unique
-- index on (id_community, region, doc_type, version).
-- ============================================================================

BEGIN;

INSERT INTO document_template (
    id_community, region, doc_type, version, valid_from, valid_to,
    file_ref, mapping_json, output_format, label
) VALUES
    -- Annexe 6 -- community notification (members/shareholders + installations).
    -- CWaPE mandated XLSX, document 5617, format of 2026-02-25.
    (NULL, 1, 'annex6_notification', 1, DATE '2026-02-25', NULL,
     NULL, NULL, 'xlsx', 'Annexe 6 - Notification de communaute (CWaPE 5617)'),

    -- Annexe 6 -- sharing form (sharing participants + installations).
    -- CWaPE mandated XLSX, document 5611.
    (NULL, 1, 'annex6_sharing_form', 1, DATE '2026-02-25', NULL,
     NULL, NULL, 'xlsx', 'Annexe 6 - Formulaire de partage (CWaPE 5611)'),

    -- Annexe 8 -- sworn declaration of the sharing participants (CWaPE 5612).
    (NULL, 1, 'annex8_sworn_declaration', 1, DATE '2026-02-25', NULL,
     NULL, NULL, 'pdf', 'Annexe 8 - Declaration sur l''honneur (CWaPE 5612)'),

    -- Standard agreement DSO <-> community representative (CWaPE 5614).
    (NULL, 1, 'dso_agreement_community', 1, DATE '2026-02-25', NULL,
     NULL, NULL, 'docx', 'Contrat type GRD - representant de communaute (CWaPE 5614)'),

    -- Standard agreement DSO <-> same-building sharing representative (CWaPE 5613).
    (NULL, 1, 'dso_agreement_building', 1, DATE '2026-02-25', NULL,
     NULL, NULL, 'docx', 'Contrat type GRD - partage au sein d''un meme batiment (CWaPE 5613)'),

    -- Up-to-date participant / installation lists for the annual reporting.
    -- Reuses the Annexe 6 layout.
    (NULL, 1, 'annual_participant_list', 1, DATE '2026-02-25', NULL,
     NULL, NULL, 'xlsx', 'Listes participants / installations (rapportage annuel)')
ON CONFLICT DO NOTHING;

COMMIT;
