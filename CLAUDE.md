# administrative-document — working notes for Claude

OptimCE administrative annexe (FastAPI + NATS worker, async SQLAlchemy 2.0).
Tracks regulatory dossiers through a journaled state machine, derives CWaPE
deadlines, and **generates the mandated forms** by rendering registered template
bundles through `document-generation`. Read `README.md` for the domain.

## Layout
- `api/administrative_document/` — `routes` → `service` (orchestration) → `repository` (owned DB) + `mappers`/`schemas`/`deps`.
- `domain/` — pure, session-free: `statemachine`, `deadlines`, `calendar`, `regions`, `prefill` (CRM → render payload), `cwape_labels` (CRM codes → the regulator's exact vocabulary).
- `ports/` — `crm_core*` (read-only CRM), `document_generation*` (async NATS render), `events`, `providers` (which adapter backs the port — framework-free on purpose, see `worker/` below).
- `worker/` — `dispatcher` (consumers) → `generate.process_generate`, `docgen_results.process_docgen_result`; both callable directly in tests via injected sessions; `scheduler`/`sweeps` = the 06:00 Brussels deadline tick.
  - **`Dockerfile.worker` has NO fastapi/uvicorn/starlette** (it installs `requirements/worker.txt`). The worker may import `api.*` — that package IS copied, because the deadline sweep runs the real `AdministrativeDocumentService` — but nothing there may pull the HTTP stack at runtime: providers come from `ports/providers.py`, `service.py` imports `UploadFile` under `TYPE_CHECKING`, and `UPLOAD_MAX_BODY_BYTES` lives in `shared/const.py` because `core/middleware/request_limits.py` imports starlette. This is invisible locally, where `.venv` has everything; it shipped once and crash-looped 62 times. `tests/test_worker_import_graph.py` pins both the import graph and the Dockerfile's `COPY` list.
- `document-templates/` — the bundles (official CWaPE files + manifests). See its README.
- `scripts/sql/schema.sql` — raw DDL (NO Alembic). `shared/models/local_models.py` mirrors it.

## Two DB-enforced invariants
- **The journal is the truth.** `status_event` is append-only and a DEFERRED
  constraint trigger rejects any `status` not backed by a journal row. Make
  exactly ONE status change per subject per transaction.
- **A version is evidence.** `document_version` is append-only, `file_ref` is
  NOT NULL and content-addressed. There is no update path; never add one.

## Generation
- Three steps: `GET /prefill` (CRM → payload, persists nothing) → `POST /generate`
  (freeze the snapshot, claim the slot, commit, publish) → `GET /render-status` (poll).
- **`document_render` is the in-flight slot**, not columns on `document`.
  `UNIQUE (id_document)` makes "one render at a time" an `INSERT … ON CONFLICT DO
  NOTHING` (rowcount 0 → 409). Success DELETEs the row; the version is the record.
- **Never journal a render.** It is a technical retry, not a regulatory
  transition — a `status_event` for it would become the journal head and break
  every later status write. A generated document stays DRAFT.
- **The snapshot is captured at REQUEST time.** `GenerationResult` is
  `extra="forbid"` and echoes only `metadata`, never `data`, so re-reading the
  CRM when the result lands would defeat the whole point (spec R2).
- **Correlate results by `docgen_request_id`, never by `metadata`** — metadata is
  data that round-tripped through another service. Tenancy comes from the row,
  which forces `_find_render_unscoped` to be the one unscoped read in the service.
- `origin` describes how the CURRENT version was produced, so both the upload and
  the render path set it.

## Conventions
- Tenant column is `id_community`; scope every owned-DB SELECT with
  `with_community_scope`. `with_community_scope` types SELECTs only — a DELETE
  needs the predicate by hand.
- **The two registries are two-tier, and reads and writes scope differently.**
  `document_template` and `deadline_rule` use `id_community IS NULL` for a
  platform default, so they can't use `with_community_scope`. Read through
  `_visible_row` (own override OR default), write through `_own_row` (own
  override only). `Role.ADMIN` is community-scoped, so a getter feeding a PATCH
  that accepts a default would let any community edit the CWaPE registry for
  every tenant.
- Cross-DB refs (`id_sharing_operation`, `id_member`, `ean`) are plain columns, never FKs.
- Errors: `ErrorException(errors.admin.X, status_code=...)`; every key needs all
  four locales (`tests/test_locales.py` enforces it). Generation block is 2360-2365.
- CRM `address.number` is an **INTEGER**. Coerce at the port boundary.

## Gotchas
- **`api/administrative_document/routes.py` must NOT `from __future__ import
  annotations`.** `with_default_error` resolves string annotations against its own
  module globals, so stringified Pydantic body types get demoted to query params
  (FastAPI 422 `loc:[query,body]`).
- **`streams.json` order is load-bearing** and JSON cannot say so:
  `ADMIN_DOCUMENT_EVENTS` must come first because it used to own
  `optimce.administrative_document.>` and has to give those subjects up before
  ADMIN_DOCUMENT / ADMIN_DOCUMENT_DLQ can claim theirs. JetStream rejects overlap.
- **`CREATE TABLE IF NOT EXISTS` never adds a column.** While unreleased,
  `schema.sql` IS the migration, so an added column also needs an idempotent
  `ALTER TABLE … ADD COLUMN IF NOT EXISTS` beside it — otherwise re-applying the
  file silently does nothing and it surfaces as an UndefinedColumnError at runtime.
- **Bundle edits need a version bump.** `document-generation` caches bundles on
  disk keyed by URI with no invalidation; re-uploading to the same versioned
  prefix has no effect until `up --force-recreate document-generation`.
- **Nothing under `tests/` may read outside the repo.** `domain/regions.py` falls
  back to the monorepo's shared `reference/regulators.json` (`parents[2]`), which
  does not exist in the standalone checkout CI builds — so the suite vendors a
  copy at `tests/fixtures/regulators.json` and `conftest.py` exports
  `REGULATORS_CONFIG_PATH` to it. Skipping that made 128 tests pass on a dev
  machine and fail in CI, all as one swallowed 2303. `tests/domain/test_regions.py`
  keeps the copy honest against the shared file when it IS checked out.
- CWaPE workbooks enforce data-validation dropdowns, so a label that is not
  character-for-character right shows as a validation error. `cwape_labels.py`
  holds them and `tests/test_template_bundles.py` re-reads the shipped workbooks
  to prove they still agree.

## Verify
`cd administrative-document` then: `ENV=test DOCKER_CONTEXT=desktop-linux
.venv/Scripts/python.exe -m pytest -q` (needs Docker Postgres on 5433) ·
`.venv/Scripts/python.exe -m ruff check .` · `... -m mypy api domain ports shared worker`.
