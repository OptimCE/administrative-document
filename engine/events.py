"""The core's events, and the CRM changes they are.

The core writes each event to its outbox in the transaction of the change and
its relay publishes it on ``core.evt.<event_type>``
(crm-backend ``src/shared/outbox/core-events.ts``). This module reads that
envelope and turns an event into a *change*: a CRM fact, the same for every
regulator, which the catalogues then route (spec, "Moteur événements →
démarches").

An envelope it cannot read is ``EventError``: deterministic, so the consumer
parks it rather than retrying it.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from zoneinfo import ZoneInfo

from engine.contracts import ChangeFact
from shared.const import ChangeSubjectType

# Bumped by the core only on a breaking change of the envelope.
SCHEMA_VERSION = 1

BRUSSELS = ZoneInfo("Europe/Brussels")


class EventError(Exception):
    """An envelope this consumer cannot read."""


@dataclass(frozen=True)
class CoreEvent:
    event_id: uuid.UUID
    event_type: str
    occurred_at: datetime.datetime  # aware
    id_community: int
    aggregate_type: str
    aggregate_id: str
    payload: Mapping[str, Any] = field(default_factory=dict)

    @property
    def day(self) -> datetime.date:
        """The Brussels calendar day it happened: what deadlines count from."""
        return self.occurred_at.astimezone(BRUSSELS).date()

    @classmethod
    def parse(cls, body: Mapping[str, Any]) -> CoreEvent:
        if body.get("schema_version") != SCHEMA_VERSION:
            raise EventError(f"schema_version {body.get('schema_version')!r}, not {SCHEMA_VERSION}")
        try:
            aggregate = body["aggregate"]
            payload = body.get("payload") or {}
            occurred_at = datetime.datetime.fromisoformat(str(body["occurred_at"]))
            event = cls(
                event_id=uuid.UUID(str(body["event_id"])),
                event_type=str(body["event_type"]),
                occurred_at=occurred_at,
                id_community=int(body["id_community"]),
                aggregate_type=str(aggregate["type"]),
                aggregate_id=str(aggregate["id"]),
                payload=dict(payload),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise EventError(f"malformed core event: {exc!r}") from exc
        if occurred_at.tzinfo is None:
            raise EventError("occurred_at carries no time zone")
        return event


@dataclass(frozen=True)
class DetectedChange:
    """What one event changed in the CRM."""

    kind: str
    subject_type: ChangeSubjectType
    subject_ref: str
    id_sharing_operation: int | None
    payload: Mapping[str, Any]

    def fact(self) -> ChangeFact:
        return ChangeFact(
            kind=self.kind,
            subject_type=self.subject_type,
            subject_ref=self.subject_ref,
            payload=self.payload,
        )


def _operation(event: CoreEvent) -> str:
    return event.aggregate_id


def _community(event: CoreEvent) -> str:
    return str(event.id_community)


def _field(name: str) -> Callable[[CoreEvent], str]:
    def read(event: CoreEvent) -> str:
        value = event.payload.get(name)
        if value is None or value == "":
            raise EventError(f"{event.event_type} {event.event_id} has no {name}")
        return str(value)

    return read


# event type -> (change kind, subject, how to read the subject's reference).
# The reserved events (installation_*, operation.closed, representative,
# statutes, internal rules) have no emitter in the core yet.
_CHANGES: dict[str, tuple[str, ChangeSubjectType, Callable[[CoreEvent], str]]] = {
    "operation.created": ("operation_created", ChangeSubjectType.OPERATION, _operation),
    "operation.closed": ("operation_closed", ChangeSubjectType.OPERATION, _operation),
    # A hard delete is not a closure: nothing says the operation was ever
    # authorised. No catalogue routes it; ending a sharing is filed by hand.
    "operation.deleted": ("operation_deleted", ChangeSubjectType.OPERATION, _operation),
    "operation.meter_added": ("meter_added", ChangeSubjectType.METER, _field("ean")),
    "operation.meter_removed": ("meter_removed", ChangeSubjectType.METER, _field("ean")),
    "operation.installation_added": (
        "installation_added",
        ChangeSubjectType.METER,
        _field("ean"),
    ),
    "operation.installation_removed": (
        "installation_removed",
        ChangeSubjectType.METER,
        _field("ean"),
    ),
    "operation.key_assigned": ("key_changed", ChangeSubjectType.KEY, _field("id_key")),
    "community.created": ("community_created", ChangeSubjectType.COMMUNITY, _community),
    "community.representative_changed": (
        "representative_changed",
        ChangeSubjectType.COMMUNITY,
        _community,
    ),
    "community.statutes_changed": ("statutes_changed", ChangeSubjectType.COMMUNITY, _community),
    "community.roi_changed": ("statutes_changed", ChangeSubjectType.COMMUNITY, _community),
    "community.member_joined": ("member_joined", ChangeSubjectType.MEMBER, _field("id_member")),
    "community.member_left": ("member_left", ChangeSubjectType.MEMBER, _field("id_member")),
}

# A change and the one that undoes it, for compensation before sending.
INVERSE: dict[str, str] = {
    "meter_added": "meter_removed",
    "meter_removed": "meter_added",
    "installation_added": "installation_removed",
    "installation_removed": "installation_added",
    "member_joined": "member_left",
    "member_left": "member_joined",
}


def change_of(event: CoreEvent) -> DetectedChange | None:
    """The change ``event`` is, or None for an event type this engine does not
    know (a newer core): nothing to file."""
    mapping = _CHANGES.get(event.event_type)
    if mapping is None:
        return None
    kind, subject_type, reference = mapping
    operation = None
    if event.aggregate_type == "operation":
        try:
            operation = int(event.aggregate_id)
        except ValueError:
            raise EventError(f"operation id {event.aggregate_id!r} is not a number") from None
    payload = {
        **event.payload,
        "event_type": event.event_type,
        "occurred_at": event.occurred_at.isoformat(),
    }
    return DetectedChange(
        kind=kind,
        subject_type=subject_type,
        subject_ref=reference(event)[:64],
        id_sharing_operation=operation,
        payload=payload,
    )
