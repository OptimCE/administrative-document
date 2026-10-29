"""engine.events: the core's envelope, and the change each event is."""

from __future__ import annotations

import datetime
import uuid

import pytest

from engine.events import CoreEvent, EventError, change_of
from shared.const import ChangeSubjectType

EVENT_ID = "0b6c2a1e-9b7f-4a7d-9a52-2f8e7c3c1d10"


def _envelope(**changes) -> dict:
    """What crm-backend's relay publishes (src/shared/outbox/relay.ts, toEnvelope)."""
    body = {
        "event_id": EVENT_ID,
        "event_type": "operation.meter_added",
        "schema_version": 1,
        "occurred_at": "2026-11-01T23:30:00.000Z",
        "id_community": 10,
        "aggregate": {"type": "operation", "id": "7"},
        "payload": {"ean": "541448800000000001", "id_member": 5, "grd": "ORES"},
    }
    body.update(changes)
    return body


class TestTheEnvelope:
    def test_reads_what_the_relay_publishes(self):
        event = CoreEvent.parse(_envelope())
        assert event.event_id == uuid.UUID(EVENT_ID)
        assert (event.event_type, event.id_community) == ("operation.meter_added", 10)
        assert (event.aggregate_type, event.aggregate_id) == ("operation", "7")
        assert event.occurred_at == datetime.datetime(2026, 11, 1, 23, 30, tzinfo=datetime.UTC)

    def test_its_day_is_the_brussels_day(self):
        """23:30 UTC on 1 November is already 2 November in Brussels."""
        assert CoreEvent.parse(_envelope()).day == datetime.date(2026, 11, 2)

    @pytest.mark.parametrize(
        ("changes", "message"),
        [
            ({"schema_version": 2}, "schema_version 2"),
            ({"event_id": "not-a-uuid"}, "malformed"),
            ({"occurred_at": "yesterday"}, "malformed"),
            ({"occurred_at": "2026-11-01T23:30:00"}, "no time zone"),
            ({"aggregate": None}, "malformed"),
            ({"id_community": "ten"}, "malformed"),
        ],
    )
    def test_refuses_what_it_cannot_read(self, changes, message):
        with pytest.raises(EventError, match=message):
            CoreEvent.parse(_envelope(**changes))


def _event(event_type: str, payload: dict | None = None, aggregate=("operation", "7")):
    return CoreEvent.parse(
        _envelope(
            event_type=event_type,
            payload=payload or {},
            aggregate=dict(zip(("type", "id"), aggregate, strict=True)),
        )
    )


class TestTheChange:
    @pytest.mark.parametrize(
        ("event_type", "payload", "aggregate", "kind", "subject_type", "ref", "operation"),
        [
            (
                "operation.meter_added",
                {"ean": "E1"},
                ("operation", "7"),
                "meter_added",
                ChangeSubjectType.METER,
                "E1",
                7,
            ),
            (
                "operation.meter_removed",
                {"ean": "E1"},
                ("operation", "7"),
                "meter_removed",
                ChangeSubjectType.METER,
                "E1",
                7,
            ),
            (
                "operation.key_assigned",
                {"id_key": 42},
                ("operation", "7"),
                "key_changed",
                ChangeSubjectType.KEY,
                "42",
                7,
            ),
            (
                "operation.created",
                {},
                ("operation", "7"),
                "operation_created",
                ChangeSubjectType.OPERATION,
                "7",
                7,
            ),
            (
                "operation.deleted",
                {},
                ("operation", "7"),
                "operation_deleted",
                ChangeSubjectType.OPERATION,
                "7",
                7,
            ),
            (
                "community.created",
                {},
                ("community", "10"),
                "community_created",
                ChangeSubjectType.COMMUNITY,
                "10",
                None,
            ),
            (
                "community.member_joined",
                {"id_member": 5},
                ("community", "10"),
                "member_joined",
                ChangeSubjectType.MEMBER,
                "5",
                None,
            ),
            (
                "community.member_left",
                {"id_member": 5},
                ("community", "10"),
                "member_left",
                ChangeSubjectType.MEMBER,
                "5",
                None,
            ),
            (
                "community.roi_changed",
                {},
                ("community", "10"),
                "statutes_changed",
                ChangeSubjectType.COMMUNITY,
                "10",
                None,
            ),
        ],
    )
    def test_each_event_is_one_change(
        self, event_type, payload, aggregate, kind, subject_type, ref, operation
    ):
        change = change_of(_event(event_type, payload, aggregate))
        assert change is not None
        assert (change.kind, change.subject_type, change.subject_ref) == (kind, subject_type, ref)
        assert change.id_sharing_operation == operation

    def test_the_payload_is_the_snapshot(self):
        change = change_of(_event("operation.meter_added", {"ean": "E1", "grd": "ORES"}))
        assert change is not None
        assert change.payload["grd"] == "ORES"
        assert change.payload["event_type"] == "operation.meter_added"

    def test_an_event_type_this_engine_does_not_know_is_no_change(self):
        assert change_of(_event("operation.renamed")) is None

    def test_a_meter_event_without_its_ean_is_unreadable(self):
        with pytest.raises(EventError, match="has no ean"):
            change_of(_event("operation.meter_added", {}))
