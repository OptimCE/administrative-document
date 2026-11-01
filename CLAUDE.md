# administrative-document — working notes for Claude

OptimCE administrative annexe (FastAPI + NATS worker, async SQLAlchemy 2.0).
Files the CRM's changes into the regulator's procedures (a generic engine and
regulator plugins; CWaPE today), tracks them through a journaled lifecycle,
derives their deadlines, and **generates the mandated forms** by rendering registered template
bundles through `document-generation`. Read `README.md` for the domain.

## Layout
- `api/admin/` — the procedures routes (`/admin`, see below). `api/administrative_document/` — what stays outside them: generation (prefill, generate, render status, a version's file), `/filings/mine`, the template registry, the operations; `routes` → `service` → `repository` + `mappers`/`schemas`/`deps`.
- `domain/` — pure, session-free: `prefill` (CRM → render payload), `cwape_labels` (CRM codes → the regulator's exact vocabulary), `regions`.
- `ports/` — `crm_core*` (read-only CRM), `document_generation*` (async NATS render), `events`, `providers` (which adapter backs the port — framework-free on purpose, see `worker/` below).
- `worker/` — `dispatcher` (consumers) → `core_events`, `generate.process_generate`, `docgen_results.process_docgen_result`; all callable directly in tests via injected sessions; `scheduler` = the 06:00 Brussels tick, which runs `engine_jobs`.
  - **`Dockerfile.worker` has NO fastapi/uvicorn/starlette** (it installs `requirements/worker.txt`) **and no `api/`**: nothing the worker runs lives there since the July sweep went. Providers come from `ports/providers.py`, and `UPLOAD_MAX_BODY_BYTES` lives in `shared/const.py` because `core/middleware/request_limits.py` imports starlette. This is invisible locally, where `.venv` has everything; it shipped once and crash-looped 62 times. `tests/test_worker_import_graph.py` pins the import graph, refuses a worker import of `api`, and checks the Dockerfile's `COPY` list.
- `document-templates/` — the bundles (official CWaPE files + manifests). See its README.
- `scripts/sql/schema.sql` — raw DDL (NO Alembic). `shared/models/local_models.py` mirrors it.
  Released, so a change also needs a forward migration in `scripts/sql/migrations/`
  (read its README: `schema.sql` self-inserts version 1 only, on purpose).
- Procedures engine (espace administratif, `docs/specs/espace-administratif.md` in
  the monorepo): `procedure_definition` is a projection of the plugin's YAML;
  `change`, `processed_event`, `module_activation`, `working_calendar`,
  `correspondence`. July dossiers were converted by migration 0003, and 0004
  made every dossier, piece and deadline engine-shaped (NOT NULLs, strict
  CHECKs); the July lifecycle code is gone. The July columns that 0003 (and the
  migrator's released 002) still touch on a fresh install stay, dead:
  `dossier.dossier_type`, `deadline.recurring`, `deadline_rule`'s July columns.
- `engine/` is generic and **never imports `regulators/`** nor names a regulator
  (`tests/engine/test_boundary.py`: AST, a subprocess import with `regulators`
  refused, and a grep). The contract is `engine/contracts.py`; plugins import
  it; `regulators/registry.py` lists them and the entry points hand `REGISTRY`
  in. Engine tests use `regulators/testing/fake.py`, never a real plugin.
  `regulators/README.md` explains adding a regulator. CWaPE (`regulators/cwape/`)
  is the only real one; its tests live in `tests/regulators/cwape/` (not in
  the plugin: the images copy `regulators/`, and the DB fixtures are here).
  Its catalogue must keep agreeing with migration 0003's reservations:
  `tests/test_migration_0003.py::TestTheCatalogueSyncsOntoIt` syncs the real
  plugin onto a converted July world.
- **Plugin files are the source, the DB their projection.** One YAML per
  procedure VERSION (`<CODE>.v<N>.yaml`) plus `calendar/<REGION>.yaml`.
  `engine/catalogue_sync.py` runs at startup in the API AND the worker
  (advisory xact lock `0x0AD30002`): it inserts, fills a row 0003 reserved,
  refreshes labels in place, and REFUSES a released version whose content
  changed — `content_sha256` excludes labels, so a typo fix is not a bump. A
  refusal stops the process on purpose. Runtime reads the in-memory catalogue
  and calendar; the tables are for FKs and SQL.
- **Core events → procedures** (`engine/routing.py`): `worker/core_events.py`
  binds ONE durable to the core's `CORE_EVENTS` stream (`core.evt.>`, owned by
  crm-outbox-relay; never declared here), `max_ack_pending=1` so events file in
  stream order across replicas. Every event lands in `processed_event` (the
  idempotency key), even when nothing is filed. Activation is read from the
  CRM on EVERY event (`ports/crm_engine.py`), no cache; `module_activation`
  periods are reconciled from it (`engine/activation.py`). A change counts as
  filed only while a LIVE procedure (draft, ready, sent) or a deferral carries
  it: a meter added again after a refusal, or after its removal went in force,
  is filed again. A removal is "already reflected" only on positive evidence
  (the meter's latest addition refused or lapsed, and no live episode in the
  core): before the baseline the annexe cannot know the meter was absent. A
  later `key_changed` in a draft supersedes the earlier one. A system draft
  emptied by compensation is cancelled (catalogue edge `draft -> cancelled`,
  `automatic: true`). A converted July draft keeps its
  variant. `engine/lifecycle.py` holds the step/deadline/piece operations the
  router, the API and the jobs share; realtime hints go out after the commit.
- **Realtime topics** (`admin.change.detected`, `admin.dossier.updated`,
  `admin.document.ready` when a render ends, `hint.status` ready/failed,
  `admin.deadline.due`) must be in `core/realtime/envelope.py` TOPICS, which is
  byte-identical in the 4 sibling producers and mirrored by crm-backend's
  `realtime.topics.ts` (the monorepo's `scripts/check-realtime-parity.sh`
  compares all of them). An unknown topic is not an error anywhere: the
  envelope is None and nothing is published. `tests/test_realtime_topics.py`
  scans every `emit(topic=...)` here. Clients branch on `hint.status` only.
- **Baseline** (`engine/baseline.py`, `/admin/baseline`): a period opening
  (worker or API, serialised by advisory xact lock `0x0AD30003` per community)
  photographs operations/meters/keys into `module_activation.baseline_json` (a
  meter the CRM has waiting too, `not_notified` by default, and called active
  only with its date) and
  opens the plugin's `purpose: baseline` procedure (the one task). Confirming
  is core FIRST (`ports/core_effects_sqlalchemy.py`: upsert by (origin,
  subject), EXCLUDE pre-checked so a 409 lists every EAN, audit row written
  there because `AuditLogService` swallows), then local, then the episodes'
  `regulatory_ref`; each step idempotent. An "already sent" procedure is BORN
  at its sent step (`open_procedure(step=...)`): passing through draft would
  collide with the family's open draft.
- **`/admin` routes** (`api/admin/`, managers only, module active):
  `procedures.py` holds the use cases, `views.py` the read model (labels from
  the procedure's OWN catalogue version; "due in N working days" counted here
  on the plugin's calendar). The dashboard (`/inbox`) keeps a finished
  procedure for 30 days; `GET /admin/dossiers` lists them all, newest first,
  paginated (the front's "Toutes les démarches"). After `frozen_at` a
  procedure takes no change and no new version of a piece it was sent with (a
  RECEIVED piece still arrives).
  A response's `dry_run` writes nothing. A deferred change moves (the row, not
  a copy: UNIQUE(source_event_id, procedure_code)) into the family's next
  draft when this one is sent or cancelled. Generation (outside `/admin`)
  takes a piece that is MISSING, STALE or READY and not frozen, and a landed
  render makes it READY (`engine.filing.piece_rendered`). The routes taking a
  file (`…/versions`, `…/send`, `…/correspondence`) get the upload body cap in
  `core/middleware/request_limits.py`; a new one must be added there.
- **Daily jobs** (`engine/jobs.py` decides, `worker/engine_jobs.py` runs, session
  advisory lock `0x0AD3_0004` on a connection of its own: a session hands its
  connection back at each commit). Communities: the CRM's active subscriptions
  plus local periods still open (reconciling closes them); module off or no
  plugin = nothing, no notification. Per community: baseline + calendar
  procedures (once per family and `period_key` year, only between
  `opens_annually` and the annual due date) committed LOCAL first (journaled);
  then the deadline pass and its notifications CRM first (deadline rows only, no
  deferred check). A community's past-due deadline goes MISSED but stays
  UNRESOLVED (`lifecycle.UNRESOLVED`: still overdue on the dashboard, still met
  late, superseded by a new clock); a third party's stays OPEN and is told once
  (`reminded_at`, in-app only); a finished procedure's closes CANCELLED
  silently; EFFECT dates are the effect job's (below). Opening a
  calendar procedure cancels the converted July deadline it takes over (open,
  on a MIGRATION dossier, unlinked, same code/party/kind, and no rule of that
  code in its own procedure). Nothing after the commits may read ORM
  state: a rollback expires it, and async lazy loads raise `MissingGreenlet`.
- Holidays are data: `engine/calendar.py` raises `CalendarNotCoveredError`
  past the file's years instead of assuming none. workalendar is a TEST
  dependency only: the independent oracle the plugins' calendar files are
  checked against.

## Effects: the core's regulatory badges (phase 3)
- `engine/effects.py` keeps the core's `regulatory_link_status` in step with
  the procedures. An addition (meter, installation, key) OWNS a row keyed by
  the procedure where it was first filed (the baseline's for one the baseline
  filed); a removal MOVES the row of what it removes; a key taking effect ends
  the previous one. Each change pins its row once in `payload_json["episode"]`
  (a deferral moves the change, never the row). `plan()` folds every change on
  a subject into one absolute value per row and returns only what differs
  (to notify, the plugin's pending/authorised, authorised from the effect date,
  active, ended the day before a removal's effect, refused, withdrawn 9). An
  APPLIED change is the core's truth; `_written_ahead` keeps what a procedure's
  own in-force write put there while its step is not taken yet.
- The plugin only maps its "sent" steps (`RegulatorPlugin.episode_status`);
  `build_registry` refuses one left unmapped.
- **Badges are a consequence**: `effects.sync()` runs AFTER the caller's local
  commit (worker routing; API defer, exclude, send, respond, transition,
  refile) and never fails the action; a refusal (one EAN in two operations) or
  a down core stays on the procedure (`effect_error_json`, kind "sync").
- **In force is core first**: `effects.go_in_force()` (pins committed, then
  the core and its audit row, then the step with `effects_written=True`).
  `move_to_step` refuses IN_FORCE with a tracked change otherwise
  (`EffectsNotWrittenError`), and entering a step settles the changes
  (refused, excluded when cancelled, applied in force). The daily job applies
  the effect dates that have come, then re-syncs every procedure that may
  still move; a blocked effect keeps `effect_error_json` kind "in_force" and is
  retried the next day. `python -m worker.engine_jobs` runs the day now.
- The effect date is the EFFECT deadline (`lifecycle.start_rules`), never
  before `requested_effective_date`, and lands in `dossier.effective_date`.
- After a refusal (or a lapse) the detail offers `refusal`; `POST
  /admin/dossiers/{id}/refile` files the refused changes again as NEW changes
  (`source_event_id` NULL, `refiled_from`) into the family's open draft.
- `valid_to` is the last day in the operation (like `meter_data.end_date`).
- **One status per meter** (2026-11-01): the CRM shows the procedure's status
  in place of its own, and its own follows. An episode written ACTIVE turns
  the meter's waiting `meter_data` rows of that operation ACTIVE from
  `valid_from` — a crm-backend trigger (`align_meter_data_on_activation`,
  SECURITY DEFINER; mirrored in `tests/sql/crm_test_schema.sql`), not this
  service, which still cannot write `meter_data`. crm-backend refuses a hand
  status change while the procedure drives it (409).

## Two DB-enforced invariants
- **The journal writes the status.** `status_event` is append-only, and
  appending a row COPIES its `(to_status, to_step)` onto the subject
  (`propagate_journal_head`). A direct write of `status`/`step` is refused on
  the spot (`forbid_direct_status_write`, trigger depth 1), and a DEFERRED
  constraint trigger rejects at COMMIT a subject born with a status nobody
  journaled. So: never assign `.status`/`.step` on a model — append the event,
  flush, `refresh(subject, ["status"])`. Several changes per transaction are
  fine (the deferred check reads the final row).
- **Two levels of status.** `dossier.status` is the engine's common
  `ProcedureStatus` (draft, ready, sent, in force, refused, cancelled, lapsed);
  `dossier.step` is the regulator plugin's string, validated by the plugin. No
  regulator code is ever a DB enum. July codes 3/4 (dossier) and 1/3/4
  (document) are retired; they survive only in the journal's history.
- **A version is evidence.** `document_version` and `correspondence` are
  append-only; `file_ref` is content-addressed. There is no update path; never
  add one.
- **A sent piece is final** (migration 0005, `tests/test_db_final_pieces.py`).
  Once its procedure is frozen, a piece takes no new version (a RECEIVED piece
  excepted), is never deleted (nor is its procedure: `trg_dossier_sent_guard`,
  since a cascade reaches the piece after the procedure row is gone), and
  changes nothing but its journaled status. A version's `stage` (draft, final,
  to sign) and `watermark` agree by CHECK. A template a version or a render in
  flight used keeps its bundle (file_ref, version, region, doc_type, format,
  scope) and is never deleted; `valid_to` and `label` stay editable. The
  released bundle bytes are pinned by `tests/test_released_bundles.py`: a
  revised form is a new `v<n>` folder plus a new row, and the lock gains it.
- **A CHECK that evaluates to NULL passes.** `kind IN (1, 2)` on a NULL kind
  lets the row through. The multi-branch CHECKs are wrapped in
  `COALESCE(…, FALSE)`; keep doing that.

## Generation
- Three steps: `GET /prefill` (CRM → payload, persists nothing) → `POST /generate`
  (freeze the snapshot, claim the slot, commit, publish) → `GET /render-status` (poll).
- **`document_render` is the in-flight slot**, not columns on `document`.
  `UNIQUE (id_document)` makes "one render at a time" an `INSERT … ON CONFLICT DO
  NOTHING` (rowcount 0 → 409). Success DELETEs the row; the version is the record.
- **Never journal a render.** It is a technical retry, not a regulatory
  transition — a `status_event` for it would become the journal head and break
  every later status write. The piece it completes is journaled instead
  (READY, by `engine.filing.piece_rendered`).
- **The snapshot is captured at REQUEST time.** `GenerationResult` is
  `extra="forbid"` and echoes only `metadata`, never `data`, so re-reading the
  CRM when the result lands would defeat the whole point (spec R2).
- **Correlate results by `docgen_request_id`, never by `metadata`** — metadata is
  data that round-tripped through another service. Tenancy comes from the row,
  which forces `_find_render_unscoped` to be the one unscoped read in the service.
- **Drafts, finals, to sign** (phase 2). A generated piece renders as a DRAFT
  stamped with `plugin.draft_watermark` (and `-brouillon` in its file name); a
  collected one as TO_SIGN, unstamped, and stays MISSING until the signed upload
  (FINAL, SIGNED). `POST /admin/dossiers/{id}/finalize` renders each reviewed
  draft again from ITS OWN snapshot and template, unstamped; sending (and any
  step change into SENT) refuses with 2385 while a supplied piece's current
  version is not FINAL or a render is in flight. An upload is always FINAL.
  The result handler refuses a draft whose result does not echo the watermark
  (an older docgen ignores unknown options) and drops a result for a sent
  procedure.
- **Pre-fill** (`domain/prefill.build_snapshot`): a per-member annex 8 is that
  participant's (identity, their own EANs only); every AcroForm field is seeded,
  in the form's order, a checkbox as False (`ACROFORM_FIELDS`, kept in step with
  the manifests and PDFs by `tests/test_template_bundles.py`); the DSO
  conventions take the procedure's DSO, requested date and key; the key
  description reads the operation's latest approved key
  (`get_operation_key`). `key_description/v1` is the one bundle that is
  OptimCE's own layout (jinja-html), not a regulator file.
- **A piece keeps its template**: pinned at birth (`engine/templates.py`,
  `sync_pieces`) or at first render; `upgrade_template` re-pins explicitly.
- **One click** (`POST /admin/dossiers/{id}/regenerate`): a fresh pre-fill plus
  the reviewer's own edits, i.e. what the current version's snapshot holds and
  its `prefill_json` did not (`domain/prefill.carry_over`). No `prefill_json`
  (an upload, an older render) answers `needs_review`.
- **What the detail says of a piece**: its current version (stage, sha256,
  generated or not), the render holding its slot (rebuilt from the server, so
  it survives a reload), the last check of its current version, the template
  it renders with from the REGISTRY (pinned or in force, `template_superseded`)
  and a per-member piece's participant; the procedure says `finals_ready` and
  `has_entry_sheet`.
- **Fiche de saisie** (`GET /admin/dossiers/{id}/entry-sheet`): a plugin's
  `entry_sheets/<piece>.yaml` (`engine/entry_sheets.py`, contract
  `entry_sheet(code)`), filled from the piece's current version. CWaPE has the
  two community forms; `tests/regulators/cwape/test_entry_sheets.py` keeps each
  key one its bundle binds.
- **Finals archive** (`GET /admin/dossiers/{id}/finals.zip`, binary, so KrakenD
  no-op): every supplied piece's final, named `NN-<file>[-<member>]`, each
  checked against its sha256 on the way out (2390 when the stored bytes
  changed), plus `SHA256SUMS.txt`; a deterministic zip. 409 (2385) while a
  piece is a draft or rendering.
- **Verify** (`…/versions/{vid}/verify`, criterion 2.1): the render slot with
  `purpose=VERIFY` re-sends exactly what the version froze; the result lands in
  `version_check` (identical, different with the renderer, failed), never as a
  version. Allowed on a sent procedure.
- `origin` describes how the CURRENT version was produced, so both the upload and
  the render path set it.

## Conventions
- Tenant column is `id_community`; scope every owned-DB SELECT with
  `with_community_scope`. `with_community_scope` types SELECTs only — a DELETE
  needs the predicate by hand.
- **The template registry is two-tier, and reads and writes scope differently.**
  `document_template` uses `id_community IS NULL` for a platform default, so it
  can't use `with_community_scope`. (Deadline rules are the catalogue's only,
  never overridden.) Read through
  `_visible_row` (own override OR default), write through `_own_row` (own
  override only). `Role.ADMIN` is community-scoped, so a getter feeding a PATCH
  that accepts a default would let any community edit the CWaPE registry for
  every tenant.
- Cross-DB refs (`id_sharing_operation`, `id_member`, `ean`) are plain columns, never FKs.
- Errors: `ErrorException(errors.admin.X, status_code=...)`; every key needs all
  four locales (`tests/test_locales.py` enforces it). Generation block is 2360-2365.
- CRM `address.number` is a **VARCHAR(32)** since 2026-08-30 (`12A` is a real
  Belgian house number). The port still coerces via `_as_optional_str`, which is
  now a no-op — keep it, so the port survives either column type.

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
`.venv/Scripts/python.exe -m ruff check .` · `... -m ruff format --check .` ·
`... -m mypy .` (what CI runs: tests included).
