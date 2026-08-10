# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Initial release. Tracks the regulatory dossiers an energy community must
  produce, submit and follow up — creation notification, annual reporting,
  energy-sharing authorisations, modifications — and generates the mandated
  forms.
- **A journaled state machine.** Documents move `draft → ready → sent →
  acknowledged` (plus `obsolete`); dossiers move `in_preparation → submitted →
  complete → closed` (plus `lapsed`). A status is never edited in place: every
  change appends an immutable `status_event`, and a DEFERRED constraint trigger
  rejects any `status` not backed by a journal row. A correction is a new traced
  transition; history is never rewritten.
- **Immutable versions.** `document_version` is append-only with a
  content-addressed `file_ref` and no update path, so what was transmitted can
  always be retrieved exactly as it was sent.
- **Derived deadlines.** `deadline_rule` rows make regulatory clocks
  configuration rather than code: a transition evaluates the matching rules and
  materialises the deadlines. Business-day arithmetic respects Belgian public
  holidays, including the movable feasts. A daily sweep at 06:00 Europe/Brussels
  raises upcoming and missed deadlines.
- **Generation of the mandated CWaPE forms.** `GET /documents/{id}/prefill`
  builds the payload from the CRM and persists nothing; `POST
  /documents/{id}/generate` freezes the snapshot, claims the render slot, commits
  and publishes; `GET /documents/{id}/render-status` polls. Rendering happens in
  the platform's `document-generation` service over NATS. `document_render` is
  the in-flight slot and its `UNIQUE (id_document)` makes "one render at a time"
  an `INSERT … ON CONFLICT DO NOTHING` — a second concurrent request gets a 409.
  Results correlate by `docgen_request_id`, never by round-tripped metadata.
- **Eleven registered template bundles** wrapping the regulator's own forms — six
  fillable PDFs (AcroForm), three XLSX workbooks and two DOCX conventions — each
  with a manifest declaring the field bindings, the repeating-block anchors and
  the form's real row capacity.
- **Two-tier template and deadline-rule registries.** `id_community IS NULL` is a
  platform default and a community row is an override; reads resolve to either,
  writes only ever touch the community's own row.
- Region resolution from the shared `reference/regulators.json`, so an unknown or
  inactive regulator fails loudly instead of silently falling back to Wallonia.
- A member-facing `GET /filings/mine`, a cross-dossier `GET /deadlines`
  dashboard, and a per-dossier timeline over the status journal.
- Notifications for `admin_deadline.due_soon` (informational),
  `admin_deadline.missed` and `admin_dossier.acknowledged` (transactional).
- Two deployables from one codebase: the FastAPI API (`main:app`) and a NATS
  worker (`worker.main`) that runs the generation and result consumers plus the
  deadline scheduler.
- API error messages in English, French, Dutch and German, selected from the
  request's `Accept-Language`.

### Notes

- Two PostgreSQL databases are used: the CRM database, read-only, as the source
  for communities, members, meters and subscriptions; and a local database owning
  the dossiers, documents, versions, status journal and deadlines. Cross-database
  references are plain columns, never foreign keys.
- `scripts/sql/schema.sql` is both the schema and the migration — there is no
  Alembic. While the project is unreleased, adding a column also requires an
  idempotent `ALTER TABLE … ADD COLUMN IF NOT EXISTS` beside the `CREATE TABLE`,
  because `CREATE TABLE IF NOT EXISTS` never adds one.
- The CWaPE form files under `document-templates/` are the regulator's own
  documents, redistributed unmodified. They are not covered by this repository's
  Apache-2.0 licence — see [NOTICE](NOTICE).
