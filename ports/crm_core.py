"""Read-only view of the CRM core, expressed as a Protocol plus value objects.

The CRM database is owned by crm-backend; this service only ever SELECTs from
it. Going through a Protocol keeps the coupling to CRM table layout in exactly
one implementation module and lets tests supply a plain fake.

Phase 1 needs the community's regulatory identity (the regulator code decides
which region's deadline rules and templates apply, and the legal identity is
what a dossier is filed under) plus its sharing operations, because every
dossier is filed for exactly one operation. Phase 2 adds the participant /
installation / storage reads that pre-fill the generated annexes.

Coded CRM values (``member_type``, ``client_type``, ``injection_status``,
``production_chain``) are carried through as integers and translated to the
regulator's own vocabulary in ``domain.cwape_labels`` — a port describes the CRM,
not the form.
"""

from __future__ import annotations

import datetime
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol

from shared.const import Region


@dataclass(frozen=True)
class PostalAddress:
    """A CRM address, normalised to strings.

    ``number`` is an INTEGER column in the CRM; it is converted at the adapter
    boundary so downstream string formatting never has to care.
    """

    street: str | None = None
    number: str | None = None
    postcode: str | None = None
    city: str | None = None
    supplement: str | None = None


@dataclass(frozen=True)
class CommunityContext:
    """Who the community is, for the purposes of filing paperwork."""

    id_community: int
    name: str
    regulator: str | None
    region: Region | None
    legal_name: str | None = None
    vat_number: str | None = None
    address: PostalAddress | None = None


@dataclass(frozen=True)
class SharingOperation:
    """A CRM energy-sharing operation — the thing a dossier is filed for."""

    id: int
    name: str
    id_community: int


@dataclass(frozen=True)
class Participant:
    """A member taking part in an operation, with the meters attributed to them.

    One row per member, not per meter: the CWaPE participant sheets list a person
    once and repeat their delivery points, so the aggregation belongs here rather
    than in the caller.
    """

    id_member: int
    name: str
    member_type: int | None  # MemberType: 1=individual, 2=company
    first_name: str | None = None
    vat_number: str | None = None
    email: str | None = None
    address: PostalAddress | None = None
    meters: tuple[MeterPoint, ...] = ()


@dataclass(frozen=True)
class MeterPoint:
    """One EAN attributed to a participant inside an operation."""

    ean: str
    meter_number: str | None = None
    grd: str | None = None  # distribution-system operator, free text in the CRM
    client_type: int | None = None  # ClientType: 1=residential, 2=pro, 3=industrial
    injection_status: int | None = None  # InjectionStatus 1..4; NULL = pure consumer
    address: PostalAddress | None = None


@dataclass(frozen=True)
class Installation:
    """A production (or storage) unit used by an operation."""

    ean: str
    meter_number: str | None = None
    grd: str | None = None
    id_member: int | None = None
    member_name: str | None = None
    member_first_name: str | None = None
    member_type: int | None = None
    injection_status: int | None = None
    production_chain: int | None = None  # ProductionChain 1..7
    generating_capacity: float | None = None  # kVA
    commissioned_on: datetime.date | None = None
    address: PostalAddress | None = None


@dataclass(frozen=True)
class PrefillWarning:
    """One thing the reviewer should fix in the CRM before filing.

    ``code`` is a stable machine key; nothing here is ever a rendered sentence.
    The text lives in the frontend, which already owns four locales and the field
    labels these warnings name — the same split as notification types, where the
    backend sends ``<feature>.<event>`` and the client renders it.

    ``subject_id`` is a string for every kind of subject: an EAN is an 18-digit
    identifier that must never become an int, and a member id is only ever used
    to build ``/members/{id}``.
    """

    code: str
    subject_type: str  # "meter" | "member" | "community"
    subject_id: str | None = None
    params: Mapping[str, str] = field(default_factory=dict)


#: A delivery point in the operation that no member owns. It cannot be listed
#: under a name on a legal form, so it is surfaced rather than dropped.
WARNING_METER_NO_MEMBER = "meter.no_member_attribution"
#: A community identity field the form needs and the CRM does not have.
WARNING_COMMUNITY_FIELD_MISSING = "community.field_missing"
#: A member field the form needs and the CRM does not have.
WARNING_MEMBER_FIELD_MISSING = "member.field_missing"


@dataclass(frozen=True)
class OperationParticipants:
    """Everything one sharing operation contributes to an annexe."""

    participants: tuple[Participant, ...] = ()
    production: tuple[Installation, ...] = ()
    storage: tuple[Installation, ...] = ()
    warnings: tuple[PrefillWarning, ...] = field(default_factory=tuple)


class CrmCoreReadPort(Protocol):
    async def get_community_context(self, id_community: int) -> CommunityContext | None:
        """Return the community's regulatory identity, or None if it is unknown."""
        ...

    async def get_sharing_operation(
        self, id_community: int, id_sharing_operation: int
    ) -> SharingOperation | None:
        """Return the operation, or None if it does not exist in THIS community.

        Scoping the lookup by community is what stops a caller attaching a
        dossier to another tenant's sharing operation.
        """
        ...

    async def list_sharing_operations(self, id_community: int) -> list[SharingOperation]:
        """Every sharing operation of the community, for pickers and filters."""
        ...

    async def get_operation_participants(
        self, id_community: int, id_sharing_operation: int
    ) -> OperationParticipants:
        """Participants, production units and storage units of one operation.

        A single call because the three lists come from the same ``meter_data``
        scan and every annexe needs at least two of them; splitting it would mean
        three round trips over the same rows.
        """
        ...

    async def member_ids_for_user(self, *, id_community: int, auth_user_id: str) -> list[int]:
        """The member id(s) the authenticated user represents in this community.

        A list, not a single id: `user_member_link` has no uniqueness constraint
        per user, and one portal account legitimately represents several members
        (a household plus a company, a mandated representative).

        Community scope comes from `member.id_community` — the link table itself
        carries none, so a query that joined only `user_member_link` would span
        every community the user belongs to.
        """
        ...
