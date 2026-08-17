import os
from enum import StrEnum

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# CORS contract:
# - Local/test: ALLOW_ORIGIN may default to "*".
# - Staging/production: ALLOW_ORIGIN is REQUIRED, must not contain "*", and may
#   be a comma-separated list (e.g. "https://app.example.com,https://admin.example.com").
# Enforced in Settings.validate_env_config below; example values live in
# .env.staging.exemple and .env.production.exemple.
class Environment(StrEnum):
    LOCAL = "local"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


def _get_env_file() -> str:
    env = os.getenv("ENV", "local").strip()
    return f".env.{env}"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_get_env_file(),
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # ---- Core ----
    ENV: Environment = Environment.LOCAL

    # ---- CRM Database ----
    CRM_DATABASE_URL: str  # postgresql+asyncpg://...
    CRM_DB_POOL_SIZE: int = 20
    CRM_DB_MAX_OVERFLOW: int = 10
    CRM_DB_POOL_RECYCLE: int = 3600  # seconds — recycle connections after 1 hour
    CRM_DB_POOL_TIMEOUT: int = 30  # seconds — wait for available connection
    CRM_DB_SSL: bool = False  # enable SSL/TLS for database connection

    # ---- LOCAL Database ----
    LOCAL_DATABASE_URL: str  # postgresql+asyncpg://...
    LOCAL_DB_POOL_SIZE: int = 20
    LOCAL_DB_MAX_OVERFLOW: int = 10
    LOCAL_DB_POOL_RECYCLE: int = 3600  # seconds — recycle connections after 1 hour
    LOCAL_DB_POOL_TIMEOUT: int = 30  # seconds — wait for available connection
    LOCAL_DB_SSL: bool = False  # enable SSL/TLS for database connection
    # ---- NATS ----
    NATS_URL: str = ""

    # ---- Storage (S3-compatible, MinIO in dev) ----
    # Endpoint URL of the S3-compatible storage server (e.g. http://minio:9000).
    # Empty in local/test where the storage module is mocked.
    STORAGE_ENDPOINT: str = ""
    STORAGE_BUCKET: str = "crm-files"
    STORAGE_ACCESS_KEY: str = ""
    STORAGE_SECRET_KEY: str = ""
    # MinIO ignores region but botocore still requires it to sign requests.
    STORAGE_REGION: str = "us-east-1"
    # Bucket holding stored document versions. Shared with the platform's other
    # rendered documents; this service reads/writes only under its own key
    # prefix (shared.const.STORAGE_KEY_PREFIX). Requires read+write.
    OUTPUT_BUCKET: str = "optimce-documents"
    # Bucket holding the rendering bundles. This service never reads it — the
    # document_template.file_ref URIs point into it and document-generation
    # fetches them — but it is declared here so a misconfigured stack fails at
    # boot rather than on the first render.
    TEMPLATES_BUCKET: str = "optimce-templates"

    # ---- document-generation (async render over NATS) ----
    # Subjects owned by the document-generation service. The request is a FLAT
    # body (no Event envelope) and the result comes back on our own reply_to.
    DOCGEN_REQUEST_SUBJECT: str = "docgen.request"
    DOCGEN_RESULT_SUBJECT: str = "docgen.result.administrative_document"
    DOCGEN_PRESIGN_TTL: int = 3600
    # How long a PENDING render may sit before a new request may supersede it.
    # Without this a result lost to docgen's DLQ would wedge the document
    # permanently, with nothing in the UI able to clear it.
    RENDER_STALE_AFTER_SECONDS: int = 1800

    # ---- Deadline sweep ----
    # How far ahead `sweep_deadlines` looks when emitting `admin_deadline.due_soon`.
    # A single window, not a ladder: `reminded_at` makes the reminder at-most-once
    # per occurrence, so a second window would need a second marker column.
    DEADLINE_REMINDER_DAYS: int = 14
    # The sweep is driven from the worker rather than by a caller. 0 disables it,
    # which is what the test environment uses.
    DEADLINE_SWEEP_HOUR_LOCAL: int = 6
    DEADLINE_SWEEP_ENABLED: bool = True

    # ---- Regulator registry ----
    # Points at the SHARED reference/regulators.json, which maps a community's
    # `regulator` code to its geographic region — that is what selects the
    # region-scoped deadline rules and templates. Empty in the dev monorepo
    # (falls back to ../reference/regulators.json); REQUIRED in a container
    # (mount the file and set this).
    REGULATORS_CONFIG_PATH: str = ""

    # ---- Realtime (Redis pub/sub) ----
    # Fire-and-forget SSE hints. Deliberately NOT in validate_env_config's
    # required set (contrast NATS_URL below): realtime is optional by design, and
    # making it mandatory would turn a broker outage into a boot failure. An
    # empty URL makes core.realtime.emit() a silent no-op.
    REALTIME_ENABLED: bool = False
    REALTIME_REDIS_URL: str = ""

    # ---- CORS ----
    ALLOW_ORIGIN: str = "*"

    LOGGING_TOKEN: str = ""
    LOGGING_TRACES_URL: str = ""
    LOGGING_LOGS_URL: str = ""
    LOGGING_METRICS_URL: str = ""

    @model_validator(mode="after")
    def validate_env_config(self) -> "Settings":
        if self.ENV != Environment.LOCAL:
            origins = [o.strip() for o in self.ALLOW_ORIGIN.split(",") if o.strip()]
            if not origins:
                raise ValueError(
                    "ALLOW_ORIGIN is required when ENV is not local; "
                    "set it explicitly in .env.{env} (no implicit fallback to '*')"
                )
            if "*" in self.ALLOW_ORIGIN:
                raise ValueError("Wildcard CORS not allowed in staging/production")
            if not self.CRM_DATABASE_URL.strip():
                raise ValueError("CRM_DATABASE_URL is required when ENV is not local")
            if not self.LOCAL_DATABASE_URL.strip():
                raise ValueError("LOCAL_DATABASE_URL is required when ENV is not local")
        if self.ENV in (Environment.STAGING, Environment.PRODUCTION):
            if not self.NATS_URL.strip():
                raise ValueError("NATS_URL is required in staging/production")
            if not self.STORAGE_ENDPOINT.strip():
                raise ValueError("STORAGE_ENDPOINT is required in staging/production")
            if not self.STORAGE_ACCESS_KEY.strip():
                raise ValueError("STORAGE_ACCESS_KEY is required in staging/production")
            if not self.STORAGE_SECRET_KEY.strip():
                raise ValueError("STORAGE_SECRET_KEY is required in staging/production")
            if not self.OUTPUT_BUCKET.strip():
                raise ValueError("OUTPUT_BUCKET is required in staging/production")
            if not self.TEMPLATES_BUCKET.strip():
                raise ValueError("TEMPLATES_BUCKET is required in staging/production")
            if not self.DOCGEN_REQUEST_SUBJECT.strip():
                raise ValueError("DOCGEN_REQUEST_SUBJECT is required in staging/production")
            if not self.DOCGEN_RESULT_SUBJECT.strip():
                raise ValueError("DOCGEN_RESULT_SUBJECT is required in staging/production")
        if self.ENV == Environment.PRODUCTION:
            if not self.LOGGING_TOKEN:
                raise ValueError("LOGGING_TOKEN required for staging/production")
            if not self.LOGGING_LOGS_URL:
                raise ValueError("LOGGING_LOGS_URL required for staging/production")
            if not self.LOGGING_METRICS_URL:
                raise ValueError("LOGGING_METRICS_URL required for staging/production")
        return self


settings = Settings()
