"""Application metrics for the administrative-document service.

All instruments are created at module import. The OTel API ships a
``_ProxyMeterProvider`` until ``metrics.set_meter_provider(...)`` runs
(inside ``core.tracing.setup_tracer_provider``), and instruments created
against the proxy rebind to the real provider when it is installed —
so import order between this module and tracing setup does not matter.

In LOCAL ``setup_tracer_provider`` returns early and the proxy stays a
no-op, meaning every ``.add(...)``/``.record(...)`` call below becomes
a cheap function dispatch with no side effects. Tests that want to
observe values use ``InMemoryMetricReader`` + a fresh ``MeterProvider``
and re-fetch instruments from that provider.

Naming follows OTel semantic conventions: dotted lowercase, ``.total``
suffix on monotonically increasing counters, units in seconds for
duration histograms. The backend translates these to
``dossiers_created_total`` etc.
"""

from __future__ import annotations

from collections.abc import Iterable

from opentelemetry import metrics
from opentelemetry.metrics import CallbackOptions, Observation

_meter = metrics.get_meter("administrative-document")

dossiers_created = _meter.create_counter(
    name="dossiers.created.total",
    description="Administrative dossiers opened, labelled by dossier_type",
    unit="1",
)

status_transitions = _meter.create_counter(
    name="status.transitions.total",
    description="Journaled state transitions, labelled by subject_type/to_status/corrective",
    unit="1",
)

document_versions_stored = _meter.create_counter(
    name="document.versions.stored.total",
    description="Immutable document versions persisted, labelled by origin",
    unit="1",
)

document_generations_requested = _meter.create_counter(
    name="document.generations.requested.total",
    description="Generation requests accepted by the API, labelled by doc_type",
    unit="1",
)

document_renders = _meter.create_counter(
    name="document.renders.total",
    description="Docgen results processed, labelled by outcome (attached/failed/dropped)",
    unit="1",
)

deadlines_created = _meter.create_counter(
    name="deadlines.created.total",
    description="Deadlines derived from a status transition, labelled by deadline_type",
    unit="1",
)

deadlines_missed = _meter.create_counter(
    name="deadlines.missed.total",
    description="Open deadlines flipped to missed by the sweep, labelled by deadline_type",
    unit="1",
)

worker_messages = _meter.create_counter(
    name="worker.messages.total",
    description="NATS message handler outcomes, labelled by outcome",
    unit="1",
)

health_checks = _meter.create_counter(
    name="health.checks.total",
    description="Readiness probe component outcomes",
    unit="1",
)

# Latest queue depth per subject. Reserved for the Phase-2 generation worker's
# background poller; the observable gauge below reads from this dict on every
# metric collection cycle.
queue_depth_snapshot: dict[str, int] = {}


def _queue_depth_callback(
    options: CallbackOptions,
) -> Iterable[Observation]:
    """Emit one observation per subject with its current pending count."""
    return [
        Observation(value, {"subject": subject}) for subject, value in queue_depth_snapshot.items()
    ]


_meter.create_observable_gauge(
    name="nats.queue.depth",
    callbacks=[_queue_depth_callback],
    description="JetStream consumer num_pending per subject",
    unit="1",
)
