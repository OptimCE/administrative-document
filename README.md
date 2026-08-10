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
(`worker.main`) that runs the generation and result consumers plus the deadline
scheduler — built around four ideas:

- **A journaled state machine.** Documents move `draft → ready → sent →
  acknowledged` (plus `obsolete`); dossiers move `in_preparation → submitted →
  complete → closed` (plus `lapsed`). A status is never edited in place: each
  change appends an immutable `status_event` (actor, timestamp, from/to, context)
  and the status column is a cache the database validates against that journal.
  Correcting a mistake is a new, traced corrective transition — history is never
  rewritten.
- **Derived deadlines.** Regulatory clocks are configuration, not code. A
  `deadline_rule` row says "when *this* happens, a *that* deadline falls due N
  business days / months later"; a transition evaluates the matching rules and
  materialises the deadlines. Business-day arithmetic respects Belgian public
  holidays, including the movable feasts.
- **Immutable versions.** Every uploaded file becomes a numbered, content-addressed
  version. Re-uploading creates a new version; a version that has been sent can
  never be altered, so you can always retrieve exactly what was transmitted.
- **Generated filings.** The mandated forms are produced from the community's own
  data rather than filled by hand: a prefill step builds the payload from the CRM,
  generation freezes that payload as a snapshot and renders the registered
  template bundle through the platform's document-generation service, and the
  result lands as a new immutable version. The snapshot is captured when the
  request is made, so what was filed is always reproducible. Only one render can
  be in flight per document.

Two **PostgreSQL** databases are used: the CRM database (read-only source for
communities, members, meters, and subscriptions) and a local database owning the
dossiers, documents, versions, status journal, and deadlines. Stored files live in
an **S3-compatible object store** (MinIO in development). Status and deadline
events are published on **NATS JetStream** for other services to observe. Traces,
metrics, and logs are emitted through **OpenTelemetry**.

## Repository Structure

| Path | Description |
|---|---|
| `api/` | HTTP layer — health and administrative-document routes, schemas, service and repository logic |
| `domain/` | Pure domain logic — the state machine, the deadline rule engine, and the Belgian business-day calendar |
| `ports/` | Adapters to the outside world — read-only CRM access, NATS event publishing, and the document-generation render port |
| `worker/` | NATS consumers for generation requests and results, plus the daily deadline scheduler |
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
role, writes to the template and deadline-rule registries require the
administrator role, and `GET /filings/mine` is the one member-facing endpoint —
it carries no role gate because the service reduces each dossier to the caller's
own rows before building the response.

| Method | Path | Purpose |
|---|---|---|
| `GET` / `POST` | `/dossiers` | List (paginated, filterable) or open a dossier |
| `GET` / `PATCH` | `/dossiers/{id}` | Read a dossier with its documents and deadlines, or edit its reference/metadata |
| `POST` | `/dossiers/{id}/transition` · `rollback` | Record a dossier status change, or a corrective one |
| `GET` | `/dossiers/{id}/timeline` | The immutable status journal for the dossier and its documents |
| `GET` | `/dossiers/{id}/deadlines` | The deadlines derived for one dossier |
| `GET` / `POST` | `/dossiers/{id}/documents` | List or add a document to the dossier |
| `GET` | `/documents/{id}` | Read a document with its versions |
| `POST` | `/documents/{id}/versions` | Upload a new immutable version (`multipart/form-data`) |
| `GET` | `/documents/{id}/versions/{versionId}/file` | Download a stored version |
| `POST` | `/documents/{id}/transition` | Record a document status change |
| `POST` | `/documents/{id}/mark-ready` · `mark-sent` · `acknowledge` · `rollback` | Ergonomic aliases for the common transitions |
| `GET` | `/documents/{id}/prefill` | Build the render payload from the CRM, persisting nothing |
| `POST` | `/documents/{id}/generate` | Freeze the snapshot and request the render (`409` if one is already in flight) |
| `GET` | `/documents/{id}/render-status` | Poll the in-flight render |
| `GET` | `/deadlines` | Cross-dossier deadline dashboard, sorted by due date |
| `POST` | `/deadlines/{id}` | Mark a deadline met or cancelled |
| `GET` | `/filings/mine` | The caller's own filings, reduced to their rows before the payload is built |
| `GET` | `/sharing-operations` | The CRM sharing operations a dossier can be attached to |
| `GET` / `POST` | `/templates`, `/deadline-rules` | List the registries (own overrides and platform defaults) or add an override |
| `PATCH` | `/templates/{id}`, `/deadline-rules/{id}` | Edit the community's own registry override |
| `POST` | `/maintenance/deadline-sweep` | Run the deadline sweep out of band |

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
psql "$LOCAL_DATABASE_URL" -f scripts/sql/seeds/0001_wal_deadline_rules.sql
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
`scripts/sql/migrations/`. Regional reference data (deadline rules and templates)
ships as seeds under `scripts/sql/seeds/`, so adapting to a revised regulatory
form or deadline is a data change, not a deployment.

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
