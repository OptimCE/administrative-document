import logging

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.trace import Status, StatusCode

from core.config import Environment, settings
from core.context_vars import current_request_id, current_user_id, current_user_role
from core.logging import RequestIdFilter

logger = logging.getLogger(__name__)

EXPORTER_TIMEOUT_MS = 5000


def setup_tracer_provider() -> None:
    """
    Configures OpenTelemetry logs and metrics.
    Only runs in staging/production where a collector is available.
    In local, the default no-op providers are used.
    """
    if settings.ENV == Environment.LOCAL:
        return

    resource = Resource.create(
        {"service.name": "administrative-document-backend", "env": settings.ENV}
    )
    headers = {"Authorization": f"Bearer {settings.LOGGING_TOKEN}"}

    # --- Logs ---
    log_exporter = OTLPLogExporter(
        endpoint=settings.LOGGING_LOGS_URL,
        headers=headers,
        timeout=EXPORTER_TIMEOUT_MS,
    )
    log_provider = LoggerProvider(resource=resource)
    log_provider.add_log_record_processor(BatchLogRecordProcessor(log_exporter))
    handler = LoggingHandler(level=logging.INFO, logger_provider=log_provider)
    # Stamp request_id / user_id / community_id / user_role onto every record
    # before it's serialised and exported. Without this, prod logs lose
    # the per-request context that staging/local gain from configure_logging().
    handler.addFilter(RequestIdFilter())
    logging.getLogger().addHandler(handler)

    # --- Metrics ---
    metric_exporter = OTLPMetricExporter(
        endpoint=settings.LOGGING_METRICS_URL,
        headers=headers,
        timeout=EXPORTER_TIMEOUT_MS,
    )
    metric_reader = PeriodicExportingMetricReader(metric_exporter, export_interval_millis=15000)
    meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
    metrics.set_meter_provider(meter_provider)

    logger.info("OpenTelemetry telemetry configured (logs, metrics)")


tracer = trace.get_tracer(__name__)


async def enrich_span():
    """
    FastAPI global dependency — enriches the active OpenTelemetry span with
    user context from ContextVars.

    Must be registered after set_auth_context so ContextVars are already populated.

    Sets:
        user.id    → current_user_id
        user.email → current_user_email
        user.role  → current_user_role

    Automatically sets span status to OK on clean exit if status was not
    already set to ERROR by the request handler.
    """
    span = trace.get_current_span()

    if span and span.is_recording():
        span.set_attribute("user.id", current_user_id.get() or "")
        span.set_attribute("user.role", current_user_role.get() or "")
        span.set_attribute("request.id", current_request_id.get() or "")

    try:
        yield
    finally:
        if span and span.is_recording() and span.status.status_code == StatusCode.UNSET:
            span.set_status(Status(StatusCode.OK))


def add_attribute(key: str, value) -> None:
    """Safely adds a key-value attribute to the current span."""
    span = trace.get_current_span()
    if span and span.is_recording():
        span.set_attribute(key, value)


def add_event(name: str, attributes: dict | None = None) -> None:
    """Safely adds a named event with optional attributes to the current span."""
    span = trace.get_current_span()
    if span and span.is_recording():
        span.add_event(name, attributes or {})


def add_exception(exception: Exception) -> None:
    """
    Records an exception on the current span and sets status to ERROR.
    Call this in exception handlers when you want the trace to reflect the failure.
    """
    span = trace.get_current_span()
    if span and span.is_recording():
        span.record_exception(exception)
        span.set_status(Status(StatusCode.ERROR, str(exception)))


def set_span_status(status: Status) -> None:
    """Safely sets the status of the current span."""
    span = trace.get_current_span()
    if span and span.is_recording():
        span.set_status(status)
