<p align="center">
  <img src="docs/logo.svg" alt="OptimCE logo" width="160">
</p>

# OptimCE — Administrative Document

[![Website](https://img.shields.io/badge/Website-optimce.be-2e7d32.svg)](https://www.optimce.be/en/)
[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![en](https://img.shields.io/badge/lang-en-43a047.svg)](README.md)
[![fr](https://img.shields.io/badge/lang-fr-lightgrey.svg)](docs/README.fr.md)
[![de](https://img.shields.io/badge/lang-de-lightgrey.svg)](docs/README.de.md)
[![nl](https://img.shields.io/badge/lang-nl-lightgrey.svg)](docs/README.nl.md)

**Administrative Document** is the regulatory-dossier microservice of the OptimCE
platform. It is the administrative **source of truth** for the documents an energy
community must produce, submit, and track across its lifecycle — creation
notification, annual reporting, energy-sharing authorisations, modifications.

It answers the questions a paper process cannot: *what exactly was sent, when, with
which data, and what is due next?* Every status change is an immutable journal
entry, every stored file is an immutable version, and regulatory deadlines are
derived automatically from those transitions instead of being tracked in a
spreadsheet.

OptimCE is an open-source platform for managing renewable energy communities,
built for the Belgian energy-sharing context. To learn more about the project,
visit [www.optimce.be](https://www.optimce.be/en/). This service is normally run
as part of the full platform: see the
[development monorepo](https://github.com/OptimCE/monorepo), which aggregates all
OptimCE services and provides the Docker Compose environment to run them
together.

## How It Works

The service ships two deployables from one codebase — a
[FastAPI](https://fastapi.tiangolo.com/) API (`main:app`) and a NATS worker
(`worker.main`) that files the core's events into procedures, runs the generation
and result consumers, and the daily jobs — built around four ideas:

- **Procedures from the regulator's catalogue.** A dossier is a *procedure* of
  the community's regulator: its type, steps, responses, pieces and clocks come
  from a versioned YAML catalogue in a regulator plugin (`regulators/`; CWaPE is
  the only one today, see `regulators/README.md`). The engine (`engine/`) knows
  only common statuses — draft, ready, sent, in force, refused, cancelled,
  lapsed — and the plugin's step travels with them. A status is never edited in
  place: each change appends an immutable `status_event`, the database copies
  the journal head onto the procedure and refuses a direct write. Correcting a
  mistake is a new, traced step change — history is never rewritten.
- **Changes in, deadlines out.** The CRM publishes its changes (a meter added, a
  key approved…) as events; the worker files each one into the open draft of
  the right type, and the daily jobs open the calendar procedures (the annual
  report) and chase their deadlines. A clock is catalogue data — "N working days
  or months after this step or that response" — counted on the plugin's holiday
  calendar, movable feasts included.
- **Immutable versions.** Every uploaded or generated file becomes a numbered,
  content-addressed version, and so does every message sent or received. Once a
  procedure is sent its pieces are frozen, so you can always retrieve exactly
  what was transmitted.
- **Generated filings.** The mandated forms are produced from the community's own
  data rather than filled by hand: a prefill step builds the payload from the CRM,
  generation freezes that payload as a snapshot and renders the registered
  template bundle through the platform's document-generation service, and the
  result lands as a new immutable version. The snapshot is captured when the
  request is made, so what was filed is always reproducible. Only one render can
  be in flight per piece.

Two **PostgreSQL** databases are used: the CRM database (communities, members,
meters and subscriptions, read-only, plus the regulatory status this service
writes back) and a local database owning the procedures, their pieces, versions,
correspondence, status journal and deadlines. Stored files live in an
**S3-compatible object store** (MinIO in development). The CRM's events arrive on
**NATS JetStream**, and realtime hints tell the managers' open pages to refresh.
Traces, metrics, and logs are emitted through **OpenTelemetry**.

## Repository Structure

| Path | Description |
|---|---|
| `api/` | HTTP layer — the procedures routes (`/admin`), generation, filings and templates, health; schemas, services and repositories |
| `domain/` | Pure domain logic — the CRM-to-form prefill and the regulator's exact vocabulary |
| `engine/` | The procedures engine — catalogue model and sync, lifecycle, routing of the CRM's events, deadlines, daily jobs; never imports a regulator |
| `regulators/` | Regulator plugins (CWaPE) — catalogue, holiday calendar, plugin contract; see `regulators/README.md` |
| `ports/` | Adapters to the outside world — read-only CRM access, NATS event publishing, and the document-generation render port |
| `worker/` | NATS consumers for the CRM's events, generation requests and their results, plus the daily jobs |
| `document-templates/` | The registered template bundles — the regulator's own forms and their manifests |
| `shared/` | Constants, error catalogue, ORM models, helpers |
| `core/` | Cross-cutting infrastructure — config, database, queue, storage, security, middleware, i18n, tracing, metrics, logging |
| `tests/` | Test suite (pytest) |
| `locales/` | Translated API error messages (en, fr, nl, de) |
| `scripts/` | Utilities — OpenAPI export, SQL schema, migrations and reference seeds |

## API

All endpoints require authentication and an active community subscription (see
[Authentication](#authentication)). The gateway supplies the external
`/administrative-document` prefix, so the in-service paths are relative.

Access is manager-gated by default: every route requires at least the manager
role, writes to the template registry require the administrator role, and
`GET /filings/mine` is the one member-facing endpoint — it carries no role gate
because the service reduces each filing to the caller's own rows before building
the response.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/admin/status` | The module for this community: active, regulator followed or not, starting state to confirm |
| `GET` / `POST` | `/admin/baseline` · `/admin/baseline/confirm` | The starting state to confirm, and its confirmation (writes the effects to the CRM) |
| `GET` | `/admin/inbox` | The dashboard: procedures by bucket with their next action and its due date, and the three soonest deadlines |
| `GET` | `/admin/deadlines` | The agenda: every open deadline, or those of a period (`from`, `to`) |
| `GET` / `POST` | `/admin/dossiers` | Every procedure (paginated, filterable), or open one by hand from the catalogue |
| `GET` | `/admin/dossiers/{id}` | One procedure: steps, changes, pieces, deadlines, thread |
| `POST` | `/admin/dossiers/{id}/changes/{changeId}/defer` · `exclude` | Defer a change to the next procedure, or exclude it with a reason |
| `POST` | `/admin/dossiers/{id}/documents/{pieceId}/versions` | Upload a piece as a new immutable version (`multipart/form-data`) |
| `POST` | `/admin/dossiers/{id}/send` | Mark as sent (date, channel, optional .eml or receipt); freezes the procedure |
| `POST` | `/admin/dossiers/{id}/correspondence` | Record a response; `dry_run=true` says what it would do and writes nothing |
| `GET` | `/admin/dossiers/{id}/correspondence/{messageId}/file` | A message's file, as recorded |
| `POST` | `/admin/dossiers/{id}/transition` | A step change by hand along the catalogue, with its reason |
| `GET` | `/admin/procedure-definitions` | The regulator's catalogue, with labels in four languages |
| `GET` | `/documents/{id}/prefill` | Build a piece's render payload from the CRM, persisting nothing |
| `POST` | `/documents/{id}/generate` | Freeze the snapshot and request the render (`409` if one is already in flight) |
| `GET` | `/documents/{id}/render-status` | Poll the in-flight render |
| `GET` | `/documents/{id}/versions/{versionId}/file` | Download a stored version |
| `GET` | `/filings/mine` | The caller's own filings, reduced to their rows before the payload is built |
| `GET` | `/sharing-operations` | The community's sharing operations, from the CRM |
| `GET` / `POST` | `/templates` | List the template registry (own overrides and platform defaults) or add an override |
| `PATCH` | `/templates/{id}` | Edit the community's own override |

Health endpoints are served under `/health`:

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health/liveness` | Process is alive |
| `GET` | `/health/readiness` | Dependencies (CRM database, NATS) are reachable |
| `GET` | `/health/health` | Alias of readiness |

Interactive OpenAPI docs (`/docs`, `/redoc`, `/openapi.json`) are enabled only
when `ENV=local`.

### Authentication

The service performs no login of its own. In the OptimCE platform a
[KrakenD](https://www.krakend.io/) gateway and [Keycloak](https://www.keycloak.org/)
authenticate the request and inject identity headers (`x-user-id`,
`x-community-id`, `x-user-groups`, `x-user-orgs`). The caller's role is resolved
for the active community, and access to the feature is gated by an active
community subscription.

## Getting Started

### Prerequisites

- Docker and Docker Compose (recommended), **or** Python 3.12 for standalone
  local development

### Running via the OptimCE Stack (recommended)

This service depends on NATS, MinIO, and PostgreSQL. The simplest way to run it
with everything wired together is the development monorepo:

```bash
git clone --recurse-submodules https://github.com/OptimCE/monorepo.git
cd monorepo
./docker-stack.sh start
```

The service runs as `administrative-document`; check it with:

```bash
curl http://localhost:8006/health/readiness
```

### Running Standalone

```bash
git clone https://github.com/OptimCE/administrative-document.git
cd administrative-document
python -m venv .venv
# Windows: .venv\Scripts\activate  |  Unix: source .venv/bin/activate
pip install -r requirements/testing.txt
cp .env.exemple .env
```

Apply the database schema, then start the API (it still needs reachable NATS,
MinIO, and PostgreSQL — the monorepo stack is the easiest way to provide them):

```bash
psql "$LOCAL_DATABASE_URL" -f scripts/sql/schema.sql
psql "$LOCAL_DATABASE_URL" -f scripts/sql/seeds/0002_wal_templates.sql
psql "$LOCAL_DATABASE_URL" -f scripts/sql/seeds/0003_wal_templates_phase2.sql
uvicorn main:app --reload      # API on http://localhost:8000
```

## Configuration

Configuration is read from the environment; `.env.exemple` documents every
variable. The main groups are:

- **CRM database** (`CRM_DATABASE_URL`, `CRM_DB_*` pool settings)
- **Local database** (`LOCAL_DATABASE_URL`, `LOCAL_DB_*` pool settings)
- **Messaging** (`NATS_URL`)
- **Object storage** (`STORAGE_ENDPOINT`, `STORAGE_BUCKET`, `STORAGE_ACCESS_KEY`,
  `STORAGE_SECRET_KEY`, `STORAGE_REGION`, `OUTPUT_BUCKET`)
- **CORS** (`ALLOW_ORIGIN`)
- **Observability** (`LOGGING_TOKEN`, `LOGGING_TRACES_URL`, `LOGGING_LOGS_URL`,
  `LOGGING_METRICS_URL`)
- **Environment selector** (`ENV`: `local`, `test`, `staging`, `production`)

## Database Schema

There is no migration runner. `scripts/sql/schema.sql` is the single source of
truth for the local database and is mirrored by `shared/models/local_models.py`;
incremental changes are added as forward-only files under
`scripts/sql/migrations/`. The template catalogue ships as seeds under
`scripts/sql/seeds/`, and each regulator plugin carries its procedure catalogue
and holiday calendar as YAML (`regulators/<plugin>/`), synced to the database at
startup — so adapting to a revised form or delay is a data change, not a code
change.

## Testing

The test suite uses [pytest](https://docs.pytest.org/); a PostgreSQL container
is started automatically via `pytest-docker`:

```bash
pytest             # run the test suite
ruff check .       # lint
ruff format --check .
mypy .             # type checking
```

## Internationalization

API error messages are translated under `locales/` in **English**, **French**,
**Dutch**, and **German**. The response language is selected from the request's
`Accept-Language` header.

## Contributing

Contributions are welcome! Please read the
[contributing guidelines](CONTRIBUTING.md) and our
[Code of Conduct](CODE_OF_CONDUCT.md) before opening an issue or pull request.

## Security

To report a security vulnerability, please follow the
[security policy](SECURITY.md) — do not open a public issue.

## License

This project is licensed under the [Apache License 2.0](LICENSE).

One exception: the blank CWaPE forms bundled under `document-templates/` are the
regulator's own documents, redistributed unmodified, and are not covered by that
grant. See [NOTICE](NOTICE).
