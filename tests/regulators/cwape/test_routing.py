"""Core events filed into CWaPE procedures: phase 1's acceptance criteria."""

from __future__ import annotations

import datetime

from engine.contracts import build_registry
from engine.lifecycle import context_for, move_to_step
from engine.routing import route_event
from ports.crm_engine import SqlEngineCrmRead
from regulators.cwape.plugin import CwapePlugin
from shared.const import EventOutcome, ProcedureStatus
from tests.engine.world import (
    EAN,
    NOW,
    active_changes,
    changes,
    dossier,
    event,
    make_world,
    meter,
    pieces,
    procedures,
)

LATER = NOW + datetime.timedelta(hours=1)


async def _cwape(db_session, **kw):
    return await make_world(db_session, plugin=CwapePlugin(), **kw)


async def _route(world, core_event):
    # The real region resolution: BE-WAL-CWAPE is Wallonia in regulators.json.
    return await route_event(
        core_event,
        local=world.session,
        crm=SqlEngineCrmRead(world.session),
        registry=world.registry,
        now=NOW + datetime.timedelta(days=1),
    )


class TestPhaseOneCriteria:
    async def test_three_meters_added_and_a_fourth_removed_make_one_authorization(self, db_session):
        """1.1: one procedure, on the authorization route, with four changes."""
        world = await _cwape(db_session)
        for ean in EAN[:3]:
            await _route(world, meter(world, "operation.meter_added", ean))
        await _route(world, meter(world, "operation.meter_removed", EAN[3]))

        (procedure,) = await procedures(world)
        await db_session.refresh(procedure)
        assert (procedure.family, procedure.variant) == ("PARTAGE_MODIF", "authorization")
        assert procedure.region == 1  # Wallonia, for the July templates
        assert len(await active_changes(world, procedure.id)) == 4

    async def test_a_meter_added_then_removed_before_sending_leaves_no_change(self, db_session):
        """1.2."""
        world = await _cwape(db_session)
        await _route(world, meter(world, "operation.meter_added", EAN[0]))
        await _route(world, meter(world, "operation.meter_removed", EAN[0], at=LATER))

        (procedure,) = await procedures(world)
        assert await active_changes(world, procedure.id) == []
        assert (await dossier(world, procedure.id)).status == ProcedureStatus.CANCELLED

    async def test_a_change_after_sending_opens_a_new_draft(self, db_session):
        """1.3."""
        world = await _cwape(db_session)
        first = await _route(world, meter(world, "operation.meter_removed", EAN[0]))
        sent = await dossier(world, first.dossiers[0])
        ctx = await context_for(db_session, world.plugin, "PARTAGE_MODIF", 1)
        await move_to_step(db_session, ctx, sent, "ready", day=NOW.date())
        await move_to_step(db_session, ctx, sent, "sent", day=NOW.date())

        second = await _route(world, meter(world, "operation.meter_removed", EAN[1], at=LATER))

        assert second.dossiers != first.dossiers
        new = await dossier(world, second.dossiers[0])
        assert (new.family, new.status) == ("PARTAGE_MODIF", ProcedureStatus.DRAFT)

    async def test_a_brugel_community_gets_nothing(self, db_session):
        """1.9: module active, regulator without a plugin."""
        world = await make_world(db_session, plugin=CwapePlugin(), regulator="BE-BRU-BRUGEL")
        result = await _route(world, meter(world, "operation.meter_added", EAN[0]))
        assert result.outcome == EventOutcome.REGULATOR_NOT_SUPPORTED
        assert await procedures(world) == []
        assert await changes(world) == []

    async def test_the_registry_is_the_shipped_one(self, db_session):
        world = await _cwape(db_session)
        assert dict(world.registry).keys() == dict(build_registry([CwapePlugin()])).keys()


class TestRoutes:
    async def test_removing_a_consumer_is_the_simple_route(self, db_session):
        world = await _cwape(db_session)
        result = await _route(world, meter(world, "operation.meter_removed", EAN[0]))
        assert (await dossier(world, result.dossiers[0])).variant == "simple"

    async def test_removing_a_producer_needs_the_authorization(self, db_session):
        world = await _cwape(db_session)
        removed = meter(world, "operation.meter_removed", EAN[0], injection=3)
        result = await _route(world, removed)
        assert (await dossier(world, result.dossiers[0])).variant == "authorization"

    async def test_a_new_key_is_a_simple_modification(self, db_session):
        world = await _cwape(db_session)
        assigned = event(
            world,
            "operation.key_assigned",
            payload={"id_key": 42, "key_name": "Égalitaire", "start_date": "2026-11-02"},
        )
        result = await _route(world, assigned)
        procedure = await dossier(world, result.dossiers[0])
        assert (procedure.family, procedure.variant) == ("PARTAGE_MODIF", "simple")

    async def test_a_new_member_files_nothing_in_phase_1(self, db_session):
        world = await _cwape(db_session)
        joined = event(world, "community.member_joined", payload={"id_member": 5})
        assert (await _route(world, joined)).outcome == EventOutcome.NO_PROCEDURE


class TestTheDraft:
    async def test_new_participants_each_owe_a_declaration(self, db_session):
        world = await _cwape(db_session)
        await _route(world, meter(world, "operation.meter_added", EAN[0], member=5))
        result = await _route(world, meter(world, "operation.meter_added", EAN[1], member=6))

        listed = {
            (p.requirement_code, p.id_member) for p in await pieces(world, result.dossiers[0])
        }
        assert ("annex8_sworn_declaration", 5) in listed
        assert ("annex8_sworn_declaration", 6) in listed
        assert ("sharing_modification", None) in listed

    async def test_the_reference_grd_comes_from_the_meters(self, db_session):
        world = await _cwape(db_session)
        result = await _route(world, meter(world, "operation.meter_removed", EAN[0], grd="RESA"))
        procedure = await dossier(world, result.dossiers[0])
        assert procedure.grd_ref == "RESA"
        assert procedure.next_action_code == "complete_documents"
