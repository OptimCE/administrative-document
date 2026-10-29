"""worker.core_events: the consumer of the core's events, and its ack matrix."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from engine.contracts import build_registry
from regulators.cwape.plugin import CwapePlugin
from regulators.testing.fake import FakeRegulator
from shared.const import CORE_EVENTS_SUBJECT
from tests.engine.world import EAN, NOW, make_world
from tests.fake_jetstream import FakeJetStream
from worker import core_events


def _body(world, event_type: str = "operation.meter_added", **changes) -> bytes:
    envelope = {
        "event_id": "6a1d1f5e-0d3b-4a5c-9f7e-1b2c3d4e5f60",
        "event_type": event_type,
        "schema_version": 1,
        "occurred_at": NOW.isoformat(),
        "id_community": world.id_community,
        "aggregate": {"type": "operation", "id": str(world.operation)},
        "payload": {"ean": EAN[0], "id_member": 5, "grd": "ORES"},
    }
    envelope.update(changes)
    return json.dumps(envelope).encode()


async def _world(db_session, **kw):
    # A real plugin: the worker resolves the region from regulators.json, which
    # knows BE-WAL-CWAPE and not the fake.
    return await make_world(db_session, plugin=CwapePlugin(), **kw)


async def _process(world, data: bytes) -> str:
    return await core_events.process_core_event(
        data, registry=world.registry, local_session=world.session, crm_session=world.session
    )


class TestProcessing:
    async def test_files_an_event(self, db_session):
        world = await _world(db_session)
        assert await _process(world, _body(world)) == core_events.FILED

    async def test_a_replay_is_a_duplicate(self, db_session):
        world = await _world(db_session)
        await _process(world, _body(world))
        assert await _process(world, _body(world)) == core_events.DUPLICATE

    async def test_an_event_it_files_nothing_for_is_skipped(self, db_session):
        world = await _world(db_session, active=False)
        assert await _process(world, _body(world)) == core_events.SKIPPED

    @pytest.mark.parametrize(
        "data",
        [b"not json", b"[]", json.dumps({"schema_version": 2}).encode()],
    )
    async def test_an_unreadable_envelope_is_poison(self, db_session, data):
        world = await _world(db_session)
        assert await _process(world, data) == core_events.POISON

    async def test_a_meter_event_without_its_meter_is_poison(self, db_session):
        world = await _world(db_session)
        assert await _process(world, _body(world, payload={})) == core_events.POISON


class _Msg:
    def __init__(self, delivered: int = 1) -> None:
        self.data = b"{}"
        self.subject = "core.evt.operation.meter_added"
        self.metadata = SimpleNamespace(num_delivered=delivered)
        self.acked = False
        self.nak_delay: float | None = None

    async def ack(self) -> None:
        self.acked = True

    async def nak(self, delay: float | None = None) -> None:
        self.nak_delay = delay


async def _handler(monkeypatch, outcome):
    """The subscription callback, with process_core_event replaced."""

    async def fake_process(_data, **_kw):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(core_events, "process_core_event", fake_process)
    js = FakeJetStream({"CORE_EVENTS": [CORE_EVENTS_SUBJECT]})
    parked: list[_Msg] = []

    async def on_dlq(msg):
        parked.append(msg)

    await core_events.subscribe(js, registry=build_registry([FakeRegulator()]), on_dlq=on_dlq)
    return js.subscriptions[0]["cb"], parked


class TestTheAckMatrix:
    @pytest.mark.parametrize(
        "outcome", [core_events.FILED, core_events.SKIPPED, core_events.DUPLICATE]
    )
    async def test_handled_is_acked(self, monkeypatch, outcome):
        handle, parked = await _handler(monkeypatch, outcome)
        msg = _Msg()
        await handle(msg)
        assert msg.acked
        assert parked == []

    async def test_poison_is_parked_then_acked(self, monkeypatch):
        handle, parked = await _handler(monkeypatch, core_events.POISON)
        msg = _Msg()
        await handle(msg)
        assert (msg.acked, parked) == (True, [msg])

    async def test_a_failure_is_retried_later(self, monkeypatch):
        handle, parked = await _handler(monkeypatch, RuntimeError("database down"))
        msg = _Msg(delivered=1)
        await handle(msg)
        assert (msg.acked, msg.nak_delay, parked) == (
            False,
            core_events._NAK_RETRY_DELAY_SECONDS,
            [],
        )

    async def test_the_last_delivery_is_parked(self, monkeypatch):
        handle, parked = await _handler(monkeypatch, RuntimeError("database down"))
        msg = _Msg(delivered=core_events._MAX_DELIVER)
        await handle(msg)
        assert (msg.acked, parked) == (True, [msg])


async def test_one_event_at_a_time_and_the_dlq_first():
    js = FakeJetStream({"CORE_EVENTS": [CORE_EVENTS_SUBJECT]})

    async def on_dlq(_msg):
        return None

    await core_events.subscribe(js, registry=build_registry([FakeRegulator()]), on_dlq=on_dlq)

    stored = js.stored(CORE_EVENTS_SUBJECT, core_events._DURABLE)
    assert stored.max_ack_pending == 1  # stream order, across every replica
    assert stored.max_deliver == core_events._MAX_DELIVER + 1
    assert js.subscriptions[0]["stream"] == "CORE_EVENTS"
