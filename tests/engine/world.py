"""A community the engine can file events for, and the events.

One Postgres holds both schemas in tests, so the per-test session serves as
both the annexe's session and the CRM's.
"""

from __future__ import annotations

import datetime
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from engine.catalogue_sync import sync_plugin
from engine.contracts import PluginRegistry, RegulatorPlugin, build_registry
from engine.events import CoreEvent
from engine.routing import RoutingResult, route_event
from ports.crm_engine import SqlEngineCrmRead
from regulators.testing.fake import FakeRegulator
from shared.const import ChangeStatus
from shared.models.local_models import ChangeModel, DocumentModel, DossierModel, StatusEventModel
from tests.conftest import create_sharing_operation
from tests.factories.subscription_factory import create_community, create_subscription

UTC = datetime.UTC
# Aware: a naive value would be read in the connection's local time zone.
ACTIVATED = datetime.datetime(2026, 1, 1, tzinfo=UTC)
NOW = datetime.datetime(2026, 11, 2, 9, 0, tzinfo=UTC)

EAN = [f"5414488{n:011d}" for n in range(1, 10)]


@dataclass
class World:
    session: AsyncSession
    id_community: int
    operation: int
    plugin: RegulatorPlugin
    registry: PluginRegistry


async def make_world(
    session: AsyncSession,
    *,
    plugin: RegulatorPlugin | None = None,
    regulator: str | None = None,
    active: bool = True,
    activated_at: datetime.datetime = ACTIVATED,
) -> World:
    plugin = plugin or FakeRegulator()
    community = await create_community(session)
    await session.execute(
        text("UPDATE community SET regulator = :regulator WHERE id = :id"),
        {"regulator": regulator or plugin.code, "id": community.id},
    )
    await create_subscription(
        session, id_community=community.id, is_active=active, updated_at=activated_at
    )
    operation = await create_sharing_operation(session, id_community=community.id, name="Op Centre")
    await sync_plugin(session, plugin)
    return World(session, community.id, operation, plugin, build_registry([plugin]))


async def set_subscription(world: World, *, active: bool, at: datetime.datetime) -> None:
    await world.session.execute(
        text(
            "UPDATE community_subscription SET is_active = :active, updated_at = :at "
            "WHERE id_community = :id"
        ),
        {"active": active, "at": at, "id": world.id_community},
    )


def event(
    world: World,
    event_type: str,
    *,
    payload: dict[str, Any] | None = None,
    aggregate: tuple[str, str] | None = None,
    at: datetime.datetime = NOW,
    event_id: uuid.UUID | None = None,
) -> CoreEvent:
    if aggregate is None:
        aggregate = (
            ("operation", str(world.operation))
            if event_type.startswith("operation.")
            else ("community", str(world.id_community))
        )
    return CoreEvent(
        event_id=event_id or uuid.uuid4(),
        event_type=event_type,
        occurred_at=at,
        id_community=world.id_community,
        aggregate_type=aggregate[0],
        aggregate_id=aggregate[1],
        payload=payload or {},
    )


def meter(
    world: World,
    event_type: str,
    ean: str,
    *,
    member: int = 5,
    grd: str = "ORES",
    injection: int | None = None,
    at: datetime.datetime = NOW,
) -> CoreEvent:
    return event(
        world,
        event_type,
        payload={
            "ean": ean,
            "effective_date": at.date().isoformat(),
            "id_member": member,
            "grd": grd,
            "injection_status": injection,
            "status": 1,
            "id_sharing_operation": world.operation,
            "reason": "configuration",
        },
        at=at,
    )


async def route(world: World, core_event: CoreEvent) -> RoutingResult:
    return await route_event(
        core_event,
        local=world.session,
        crm=SqlEngineCrmRead(world.session),
        registry=world.registry,
        region_of=lambda _code: 1,
        now=NOW + datetime.timedelta(days=1),
    )


async def dossier(world: World, dossier_id: int) -> DossierModel:
    row = await world.session.get(DossierModel, dossier_id)
    assert row is not None
    await world.session.refresh(row)
    return row


async def procedures(world: World) -> list[DossierModel]:
    rows = await world.session.scalars(
        select(DossierModel)
        .where(DossierModel.id_community == world.id_community)
        .order_by(DossierModel.id)
    )
    return list(rows)


async def changes(world: World, dossier_id: int | None = None) -> list[ChangeModel]:
    stmt = select(ChangeModel).where(ChangeModel.id_community == world.id_community)
    if dossier_id is not None:
        stmt = stmt.where(ChangeModel.id_dossier == dossier_id)
    rows = list(await world.session.scalars(stmt.order_by(ChangeModel.id)))
    for row in rows:
        await world.session.refresh(row)
    return rows


async def active_changes(world: World, dossier_id: int) -> list[ChangeModel]:
    return [c for c in await changes(world, dossier_id) if c.status == int(ChangeStatus.ATTACHED)]


async def pieces(world: World, dossier_id: int) -> list[DocumentModel]:
    rows = list(
        await world.session.scalars(
            select(DocumentModel)
            .where(DocumentModel.id_dossier == dossier_id)
            .order_by(DocumentModel.id)
        )
    )
    for row in rows:
        await world.session.refresh(row)
    return rows


async def journal(world: World, subject_type: int, subject_id: int) -> list[StatusEventModel]:
    return list(
        await world.session.scalars(
            select(StatusEventModel)
            .where(
                StatusEventModel.subject_type == subject_type,
                StatusEventModel.subject_id == subject_id,
            )
            .order_by(StatusEventModel.id)
        )
    )
