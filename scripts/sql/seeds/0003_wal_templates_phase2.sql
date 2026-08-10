-- ============================================================================
-- Phase 2: point the Walloon catalogue at its rendering bundles, and register
-- the four main CWaPE forms the Phase-1 catalogue did not cover.
--
-- file_ref is the versioned S3 prefix of a bundle under
-- administrative-document/document-templates/. mapping_json records what the
-- bundle can fill, for operators reading the registry -- the authoritative map
-- lives in the bundle's manifest.json, next to the document it describes.
--
-- Registering a revised official form stays a data change: add a row with the
-- next `version`, set the previous row's valid_to, and publish a new versioned
-- bundle directory. Never mutate a published prefix -- the template cache in
-- document-generation is keyed by URI and has no invalidation.
--
-- Coded values: region 1 = WAL. Idempotent.
-- ============================================================================

BEGIN;

-- ---- bundles for the forms already registered in 0002 ----------------------

UPDATE document_template
   SET file_ref = 's3://optimce-templates/administrative-document/'
                  || doc_type || '/v1/',
       mapping_json = '{"engine": "xlsx", "sources": ["members", "installations"]}'::jsonb
 WHERE id_community IS NULL
   AND region = 1
   AND valid_to IS NULL
   AND doc_type IN ('annex6_notification', 'annual_participant_list');

UPDATE document_template
   SET file_ref = 's3://optimce-templates/administrative-document/'
                  || doc_type || '/v1/',
       mapping_json = '{"engine": "xlsx",
                        "sources": ["participants", "installations", "storage"]}'::jsonb
 WHERE id_community IS NULL
   AND region = 1
   AND valid_to IS NULL
   AND doc_type = 'annex6_sharing_form';

UPDATE document_template
   SET file_ref = 's3://optimce-templates/administrative-document/'
                  || doc_type || '/v1/',
       mapping_json = '{"engine": "pdf-form"}'::jsonb
 WHERE id_community IS NULL
   AND region = 1
   AND valid_to IS NULL
   AND doc_type = 'annex8_sworn_declaration';

-- The two DSO standard agreements (CWaPE 5614 / 5613): Word contracts rendered
-- by the docx engine from the regulator's own convention, with its
-- "[a completer]" markers turned into template expressions and nothing else
-- touched.
UPDATE document_template
   SET file_ref = 's3://optimce-templates/administrative-document/'
                  || doc_type || '/v1/',
       mapping_json = '{"engine": "docx"}'::jsonb
 WHERE id_community IS NULL
   AND region = 1
   AND valid_to IS NULL
   AND doc_type IN ('dso_agreement_community', 'dso_agreement_building');

-- ---- the main forms, not covered by the Phase-1 catalogue -------------------
-- Annexes 6/7/8 are attachments TO these; a dossier needs the covering form too.

INSERT INTO document_template (
    id_community, region, doc_type, version, valid_from, valid_to,
    file_ref, mapping_json, output_format, label
) VALUES
    (NULL, 1, 'community_notification', 1, DATE '2024-11-04', NULL,
     's3://optimce-templates/administrative-document/community_notification/v1/',
     '{"engine": "pdf-form"}'::jsonb, 'pdf',
     'Formulaire de notification d''une communaute d''energie (CWaPE)'),

    (NULL, 1, 'community_modification', 1, DATE '2024-06-27', NULL,
     's3://optimce-templates/administrative-document/community_modification/v1/',
     '{"engine": "pdf-form"}'::jsonb, 'pdf',
     'Formulaire de modification d''une communaute d''energie (CWaPE)'),

    (NULL, 1, 'sharing_notification', 1, DATE '2025-07-01', NULL,
     's3://optimce-templates/administrative-document/sharing_notification/v1/',
     '{"engine": "pdf-form"}'::jsonb, 'pdf',
     'Formulaire de notification d''une activite de partage (CWaPE)'),

    (NULL, 1, 'sharing_modification', 1, DATE '2025-07-01', NULL,
     's3://optimce-templates/administrative-document/sharing_modification/v1/',
     '{"engine": "pdf-form"}'::jsonb, 'pdf',
     'Formulaire de modification d''une activite de partage (CWaPE)'),

    -- Signed per legal-entity member, so the reviewer picks which one.
    (NULL, 1, 'annex7_legal_entity', 1, DATE '2026-02-06', NULL,
     's3://optimce-templates/administrative-document/annex7_legal_entity/v1/',
     '{"engine": "pdf-form"}'::jsonb, 'pdf',
     'Annexe 7 - Societes et associations (CWaPE)')
ON CONFLICT DO NOTHING;

INSERT INTO schema_version (version, description)
VALUES (2, 'Phase 2: generation bundles + the main CWaPE forms')
ON CONFLICT DO NOTHING;

COMMIT;
