-- ============================================================================
-- Phase 2 of the procedures engine: register the key description.
--
-- For a key outside the CWaPE's standard list, a sharing procedure needs "la
-- description et les formules de la clé". The CWaPE publishes no form for it,
-- so this bundle is OptimCE's own layout (jinja-html, rendered to PDF), unlike
-- every other Walloon template, which wraps a regulator file.
--
-- Reference data for the dev stack and the tests, which re-apply the seeds on
-- every provisioning. An existing database gets the same row from migration
-- 0005 (optimce-migrator 005); this file writes no schema_version. Coded
-- values: region 1 = WAL. Idempotent.
-- ============================================================================

BEGIN;

INSERT INTO document_template (
    id_community, region, doc_type, version, valid_from, valid_to,
    file_ref, mapping_json, output_format, label
) VALUES
    (NULL, 1, 'key_description', 1, DATE '2026-10-30', NULL,
     's3://optimce-templates/administrative-document/key_description/v1/',
     '{"engine": "jinja-html"}'::jsonb, 'pdf',
     'Description et formules de la cle de repartition (OptimCE)')
ON CONFLICT DO NOTHING;

COMMIT;
