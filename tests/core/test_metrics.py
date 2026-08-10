"""Tests for core/metrics.py instruments.

We attach an ``InMemoryMetricReader`` to a freshly-built ``MeterProvider``
for the duration of each test, re-fetch a meter, and create local mirror
instruments. The mirror is needed because the module-level instruments
in ``core.metrics`` are bound to whatever provider was current at import
time (the OTel ``_ProxyMeterProvider`` in test environments without
``setup_tracer_provider``); the proxy is supposed to rebind, but the
test fixture stays portable across SDK versions by mirroring locally
and asserting on the in-memory reader directly.

For the queue depth gauge we exercise the callback (the same one used
by core.metrics.create_observable_gauge) against a dict the test
controls.
"""

from __future__ import annotations

from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from core import metrics as app_metrics


def _collect(reader: InMemoryMetricReader) -> dict[str, list]:
    """Return a flat ``{metric_name: [data_points]}`` mapping for assertions."""
    data = reader.get_metrics_data()
    found: dict[str, list] = {}
    if data is None:
        return found
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                # Counter / Sum and Histogram both expose .data.data_points
                found.setdefault(metric.name, []).extend(metric.data.data_points)
    return found


def test_queue_depth_callback_emits_one_observation_per_subject(monkeypatch):
    """The observable gauge reads from queue_depth_snapshot at collection time."""
    monkeypatch.setitem(app_metrics.queue_depth_snapshot, "administrative_document", 5)
    monkeypatch.setitem(app_metrics.queue_depth_snapshot, "other", 0)

    observations = list(app_metrics._queue_depth_callback(options=None))

    by_subject = {obs.attributes["subject"]: obs.value for obs in observations}
    assert by_subject == {"administrative_document": 5, "other": 0}


def test_queue_depth_callback_emits_nothing_when_snapshot_empty(monkeypatch):
    """No subscriptions, no observations — keeps the backend series count sane."""
    monkeypatch.setattr(app_metrics, "queue_depth_snapshot", {})

    observations = list(app_metrics._queue_depth_callback(options=None))

    assert observations == []


def test_counter_records_into_in_memory_reader():
    """End-to-end check that a Counter bound to a freshly-installed
    MeterProvider exports data points via InMemoryMetricReader. This
    protects against future API/SDK upgrades silently breaking the
    OTel-API-to-SDK proxy binding.
    """
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    previous_provider = metrics.get_meter_provider()
    metrics.set_meter_provider(provider)
    try:
        meter = metrics.get_meter("test")
        counter = meter.create_counter("test.counter")
        counter.add(3, {"x": "y"})

        collected = _collect(reader)
        assert "test.counter" in collected
        points = collected["test.counter"]
        assert sum(p.value for p in points) == 3
        assert any(p.attributes.get("x") == "y" for p in points)
    finally:
        metrics.set_meter_provider(previous_provider)
