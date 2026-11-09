"""engine.effects: what the core shows of each filed subject, on the fake regulator.

One Postgres holds the annexe's schema and the CRM's in tests
(``tests/engine/world.py``); each plan is applied by the real effects port, so
the core's own constraints (the EXCLUDE, the validity CHECK) run on it.
"""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy import text

from api.admin.service import AdminService
from engine import effects
from engine.catalogue import response_event
from engine.effects import StepOverride
from engine.filing import context_of_procedure, refile_change
from engine.lifecycle import EffectsNotWrittenError, close_rules, move_to_step, start_rules
from ports.core_effects import EffectConflictError, EpisodeWrite
from ports.core_effects_sqlalchemy import SqlCoreEffects
from ports.crm_engine import SqlEngineCrmRead
from shared.const import ChangeStatus, EventOutcome, ProcedureStatus, RegulatoryStatus
from shared.models.local_models import DossierModel
from tests.conftest import create_sharing_operation
from tests.engine.world import (
    EAN,
    NOW,
    World,
    add_meter,
    changes,
    dossier,
    event,
    make_world,
    meter,
    route,
)

DAY = NOW.date()
ONE = datetime.timedelta(days=1)
LATER = DAY + datetime.timedelta(days=90)  # a second procedure, signed well after the first


# -- helpers -----------------------------------------------------------------------


async def _plan(world: World, *ids: int, **kw) -> list:
    return await effects.plan(
        world.session,
        SqlEngineCrmRead(world.session),
        world.registry,
        world.id_community,
        ids,
        **kw,
    )


async def _sync(world: World, *ids: int, **kw) -> list:
    writes = await _plan(world, *ids, **kw)
    await SqlCoreEffects(world.session).apply(
        world.id_community, writes, audit_payload={"test": "effects"}
    )
    return writes


async def _rows(world: World) -> list[dict]:
    rows = await world.session.execute(
        text(
            "SELECT subject_type, subject_ref, id_dossier_origin, regulatory_status, "
            " valid_from, valid_to, regulatory_ref FROM regulatory_link_status "
            "WHERE id_community = :c ORDER BY id"
        ),
        {"c": world.id_community},
    )
    return [dict(row._mapping) for row in rows]


async def _row(world: World, ref: str, *, origin: int | None = None) -> dict:
    found = [
        r
        for r in await _rows(world)
        if r["subject_ref"] == ref and (origin is None or r["id_dossier_origin"] == origin)
    ]
    assert len(found) == 1, found
    return found[0]


def _state(row: dict) -> tuple:
    return (row["regulatory_status"], row["valid_from"], row["valid_to"], row["regulatory_ref"])


async def _filed(world: World, core_event) -> DossierModel:
    result = await route(world, core_event)
    assert result.outcome == EventOutcome.ROUTED, result
    return await dossier(world, result.dossiers[0])


async def _step(world: World, procedure: DossierModel, to_step: str, **kw) -> None:
    ctx = await context_of_procedure(world.session, world.plugin, procedure)
    await move_to_step(world.session, ctx, procedure, to_step, day=DAY, **kw)


async def _answer(
    world: World, procedure: DossierModel, code: str, *, day: datetime.date = DAY
) -> None:
    """A response as ``procedures.respond`` records it: its clocks, then its step."""
    ctx = await context_of_procedure(world.session, world.plugin, procedure)
    name = response_event(code)
    await close_rules(world.session, ctx, procedure, name)
    await start_rules(world.session, ctx, procedure, name, anchor=day)
    target = ctx.definition.response_type(code).to_step
    if target:
        await move_to_step(world.session, ctx, procedure, target, day=day)


async def _to_signed(
    world: World, procedure: DossierModel, *, day: datetime.date = DAY
) -> datetime.date:
    """Sent, reviewed when its route has the review, accepted, signed on
    ``day``: the effect date is set. Returns it."""
    await _step(world, procedure, "ready")
    await _step(world, procedure, "sent")
    if procedure.variant == "full":
        await _answer(world, procedure, "review_ack")
    await _answer(world, procedure, "approval")
    await _answer(world, procedure, "signed_return", day=day)
    assert procedure.effective_date is not None
    return procedure.effective_date


async def _in_force(world: World, procedure: DossierModel) -> None:
    """What the effect job does: the core first, then the step."""
    await _sync(
        world,
        procedure.id,
        overrides={procedure.id: StepOverride("in_force", procedure.effective_date)},
    )
    await _step(world, procedure, "in_force", effects_written=True)


def _key(world: World, id_key: int):
    return event(
        world,
        "operation.key_assigned",
        payload={"id_key": id_key, "key_name": f"Clé {id_key}", "start_date": DAY.isoformat()},
    )


@pytest.fixture
async def world(db_session) -> World:
    return await make_world(db_session)


# -- an addition -------------------------------------------------------------------


class TestAnAddition:
    async def test_once_filed_it_is_to_notify_and_its_row_is_pinned(self, world):
        draft = await _filed(world, meter(world, "operation.meter_added", EAN[0]))

        await _sync(world, draft.id)

        row = await _row(world, EAN[0])
        assert row["id_dossier_origin"] == draft.id
        assert _state(row) == (int(RegulatoryStatus.TO_NOTIFY), None, None, str(draft.id))
        (change,) = await changes(world, draft.id)
        assert change.payload_json["episode"] == {"origin": draft.id}

    async def test_it_follows_its_procedure_to_authorised_from_the_effect_date(self, world):
        procedure = await _filed(world, meter(world, "operation.meter_added", EAN[0]))
        ctx = await context_of_procedure(world.session, world.plugin, procedure)
        seen = []
        await _step(world, procedure, "ready")
        await _step(world, procedure, "sent")
        await _sync(world, procedure.id)
        seen.append(_state(await _row(world, EAN[0]))[:3])
        await _answer(world, procedure, "review_ack")
        await _sync(world, procedure.id)
        seen.append(_state(await _row(world, EAN[0]))[:3])
        await _answer(world, procedure, "approval")
        await _sync(world, procedure.id)
        seen.append(_state(await _row(world, EAN[0]))[:3])
        await _answer(world, procedure, "signed_return")
        await _sync(world, procedure.id)
        seen.append(_state(await _row(world, EAN[0]))[:3])

        effective = ctx.calendar.add_working_days(DAY, 20)
        assert procedure.effective_date == effective
        assert seen == [
            (int(RegulatoryStatus.PENDING_GRD), None, None),
            (int(RegulatoryStatus.PENDING_REGULATOR), None, None),
            (int(RegulatoryStatus.AUTHORIZED), None, None),
            (int(RegulatoryStatus.AUTHORIZED), effective, None),
        ]

    async def test_the_date_the_community_asked_for_wins_when_later(self, world):
        procedure = await _filed(world, meter(world, "operation.meter_added", EAN[0]))
        later = DAY + datetime.timedelta(days=120)
        procedure.requested_effective_date = later

        assert await _to_signed(world, procedure) == later

    async def test_in_force_it_is_active_from_the_effect_date_and_the_core_keeps_it(self, world):
        procedure = await _filed(world, meter(world, "operation.meter_added", EAN[0]))
        effective = await _to_signed(world, procedure)

        await _in_force(world, procedure)

        assert _state(await _row(world, EAN[0])) == (
            int(RegulatoryStatus.ACTIVE),
            effective,
            None,
            str(procedure.id),
        )
        (change,) = await changes(world, procedure.id)
        assert change.status == int(ChangeStatus.APPLIED)
        assert await _plan(world, procedure.id) == []

    async def test_it_cannot_go_in_force_before_the_core_has_it(self, world):
        procedure = await _filed(world, meter(world, "operation.meter_added", EAN[0]))
        await _to_signed(world, procedure)

        with pytest.raises(EffectsNotWrittenError):
            await _step(world, procedure, "in_force")

    async def test_a_refusal_shows_it_refused(self, world):
        procedure = await _filed(world, meter(world, "operation.meter_added", EAN[0]))
        await _step(world, procedure, "ready")
        await _step(world, procedure, "sent")
        await _answer(world, procedure, "review_ack")
        await _sync(world, procedure.id)

        await _answer(world, procedure, "refusal")
        await _sync(world, procedure.id)

        (change,) = await changes(world, procedure.id)
        assert change.status == int(ChangeStatus.REFUSED)
        assert _state(await _row(world, EAN[0]))[0] == int(RegulatoryStatus.REFUSED)

    async def test_a_cancelled_draft_withdraws_it(self, world):
        draft = await _filed(world, meter(world, "operation.meter_added", EAN[0]))
        await _sync(world, draft.id)

        await _step(world, draft, "cancelled")
        await _sync(world, draft.id)

        (change,) = await changes(world, draft.id)
        assert change.status == int(ChangeStatus.EXCLUDED)
        assert change.payload_json["excluded"]["reason"] == "procedure_cancelled"
        assert _state(await _row(world, EAN[0]))[0] == int(RegulatoryStatus.WITHDRAWN)

    async def test_compensated_before_sending_it_is_withdrawn(self, world):
        draft = await _filed(world, meter(world, "operation.meter_added", EAN[0]))
        await _sync(world, draft.id)

        await route(world, meter(world, "operation.meter_removed", EAN[0]))
        await _sync(world, draft.id)

        assert {c.status for c in await changes(world)} == {int(ChangeStatus.CANCELLED)}
        assert _state(await _row(world, EAN[0]))[0] == int(RegulatoryStatus.WITHDRAWN)

    async def test_one_the_core_never_saw_leaves_no_row_once_compensated(self, world):
        draft = await _filed(world, meter(world, "operation.meter_added", EAN[0]))
        await route(world, meter(world, "operation.meter_removed", EAN[0]))

        assert await _sync(world, draft.id) == []
        assert await _rows(world) == []

    async def test_planning_twice_writes_nothing_the_second_time(self, world):
        draft = await _filed(world, meter(world, "operation.meter_added", EAN[0]))

        assert len(await _sync(world, draft.id)) == 1
        assert await _sync(world, draft.id) == []

    async def test_a_dry_run_pins_nothing(self, world):
        draft = await _filed(world, meter(world, "operation.meter_added", EAN[0]))

        writes = await _plan(world, draft.id, persist_pins=False)

        assert [w.regulatory_status for w in writes] == [RegulatoryStatus.TO_NOTIFY]
        (change,) = await changes(world, draft.id)
        assert "episode" not in change.payload_json

    async def test_deferred_it_keeps_the_row_of_the_procedure_that_filed_it(self, world):
        first = await _filed(world, meter(world, "operation.meter_added", EAN[0]))
        await _sync(world, first.id)
        (change,) = await changes(world, first.id)
        change.status = int(ChangeStatus.DEFERRED)
        await _step(world, first, "ready")
        await _step(world, first, "sent")

        nxt = await refile_change(
            world.session, world.plugin, change, day=DAY, region_of=lambda _code: 1
        )
        await _sync(world, nxt.id)

        assert nxt.id != first.id
        row = await _row(world, EAN[0])
        assert row["id_dossier_origin"] == first.id
        assert _state(row) == (int(RegulatoryStatus.TO_NOTIFY), None, None, str(nxt.id))


# -- a removal ---------------------------------------------------------------------


async def _active(world: World, ean: str) -> tuple[DossierModel, datetime.date]:
    procedure = await _filed(world, meter(world, "operation.meter_added", ean))
    effective = await _to_signed(world, procedure)
    await _in_force(world, procedure)
    return procedure, effective


class TestARemoval:
    async def test_it_moves_the_row_of_what_it_removes(self, world):
        added, effective = await _active(world, EAN[0])

        removal = await _filed(world, meter(world, "operation.meter_removed", EAN[0]))
        await _sync(world, removal.id)

        assert removal.id != added.id
        row = await _row(world, EAN[0])
        assert row["id_dossier_origin"] == added.id
        assert _state(row) == (int(RegulatoryStatus.LEAVING), effective, None, str(removal.id))

    async def test_in_force_it_ends_the_day_before_its_effect_date(self, world):
        added, since = await _active(world, EAN[0])
        removal = await _filed(world, meter(world, "operation.meter_removed", EAN[0]))
        leaving = await _to_signed(world, removal, day=LATER)
        await _sync(world, removal.id)
        assert _state(await _row(world, EAN[0]))[:3] == (
            int(RegulatoryStatus.LEAVING),
            since,
            leaving - ONE,
        )

        await _in_force(world, removal)

        assert _state(await _row(world, EAN[0])) == (
            int(RegulatoryStatus.ENDED),
            since,
            leaving - ONE,
            str(removal.id),
        )
        assert await _plan(world, removal.id, added.id) == []

    @pytest.mark.parametrize("outcome", ["cancelled", "refused"])
    async def test_cancelled_or_refused_the_meter_is_back_as_it_was(self, world, outcome):
        added, since = await _active(world, EAN[0])
        removal = await _filed(world, meter(world, "operation.meter_removed", EAN[0]))
        await _sync(world, removal.id)
        if outcome == "refused":
            removal.variant = "full"  # only the review route has a refusal
            await _step(world, removal, "ready")
            await _step(world, removal, "sent")
            await _answer(world, removal, "review_ack")
            await _answer(world, removal, "refusal")
        else:
            await _step(world, removal, "cancelled")

        await _sync(world, removal.id)

        assert _state(await _row(world, EAN[0])) == (
            int(RegulatoryStatus.ACTIVE),
            since,
            None,
            str(added.id),
        )

    async def test_a_meter_the_regulator_refused_has_nothing_to_leave(self, world):
        procedure = await _filed(world, meter(world, "operation.meter_added", EAN[0]))
        await _step(world, procedure, "ready")
        await _step(world, procedure, "sent")
        await _answer(world, procedure, "review_ack")
        await _answer(world, procedure, "refusal")
        await _sync(world, procedure.id)

        removed = await route(world, meter(world, "operation.meter_removed", EAN[0]))

        assert removed.outcome == EventOutcome.ALREADY_REFLECTED
        assert _state(await _row(world, EAN[0]))[0] == int(RegulatoryStatus.REFUSED)


# -- what counts as filed ------------------------------------------------------------


class TestWhatCountsAsFiled:
    async def test_after_a_refusal_the_meter_added_again_is_filed_again(self, world):
        refused = await _filed(world, meter(world, "operation.meter_added", EAN[0]))
        await _step(world, refused, "ready")
        await _step(world, refused, "sent")
        await _answer(world, refused, "review_ack")
        await _answer(world, refused, "refusal")
        await _sync(world, refused.id)

        again = await _filed(world, meter(world, "operation.meter_added", EAN[0]))
        await _sync(world, again.id)

        assert again.id != refused.id
        assert _state(await _row(world, EAN[0], origin=refused.id))[0] == int(
            RegulatoryStatus.REFUSED
        )
        assert _state(await _row(world, EAN[0], origin=again.id))[0] == int(
            RegulatoryStatus.TO_NOTIFY
        )

    async def test_after_its_removal_went_in_force_the_meter_added_again_is_filed(self, world):
        await _active(world, EAN[0])
        removal = await _filed(world, meter(world, "operation.meter_removed", EAN[0]))
        await _to_signed(world, removal)
        await _in_force(world, removal)

        again = await route(world, meter(world, "operation.meter_added", EAN[0]))

        assert again.outcome == EventOutcome.ROUTED


# -- keys ------------------------------------------------------------------------------


class TestAKey:
    async def test_taking_effect_it_ends_the_previous_key(self, world):
        first = await _filed(world, _key(world, 11))
        first_since = await _to_signed(world, first)
        await _in_force(world, first)

        second = await _filed(world, _key(world, 12))
        effective = await _to_signed(world, second, day=LATER)
        await _sync(world, second.id)
        assert _state(await _row(world, "11"))[0] == int(
            RegulatoryStatus.ACTIVE
        )  # until it takes effect

        await _in_force(world, second)

        assert _state(await _row(world, "12")) == (
            int(RegulatoryStatus.ACTIVE),
            effective,
            None,
            str(second.id),
        )
        assert _state(await _row(world, "11")) == (
            int(RegulatoryStatus.ENDED),
            first_since,
            effective - ONE,
            str(second.id),
        )

    async def test_a_refused_key_leaves_the_previous_one_active(self, world):
        first = await _filed(world, _key(world, 11))
        await _to_signed(world, first)
        await _in_force(world, first)
        second = await _filed(world, _key(world, 12))
        second.variant = "full"
        await _step(world, second, "ready")
        await _step(world, second, "sent")
        await _answer(world, second, "review_ack")
        await _sync(world, second.id)

        await _answer(world, second, "refusal")
        await _sync(world, second.id)

        assert _state(await _row(world, "12"))[0] == int(RegulatoryStatus.REFUSED)
        assert _state(await _row(world, "11"))[0] == int(RegulatoryStatus.ACTIVE)

    async def test_a_later_key_in_the_same_draft_replaces_the_earlier(self, world):
        draft = await _filed(world, _key(world, 11))
        await _sync(world, draft.id)

        again = await _filed(world, _key(world, 12))
        await _sync(world, draft.id)

        assert again.id == draft.id
        older, newer = await changes(world, draft.id)
        assert older.status == int(ChangeStatus.CANCELLED)
        assert older.payload_json["superseded_by"] == newer.id
        assert _state(await _row(world, "11"))[0] == int(RegulatoryStatus.WITHDRAWN)
        assert _state(await _row(world, "12"))[0] == int(RegulatoryStatus.TO_NOTIFY)
        assert draft.status == int(ProcedureStatus.DRAFT)


# -- the starting state and the core's constraint ------------------------------------


class TestTheStartingState:
    async def test_a_removal_moves_the_row_the_baseline_confirmed(self, world):
        await add_meter(world, EAN[0])
        service = AdminService(
            local_session=world.session,
            crm_session=world.session,
            crm_read=SqlEngineCrmRead(world.session),
            effects=SqlCoreEffects(world.session),
            registry=world.registry,
            region_of=lambda _code: 1,
        )
        await service.confirm_baseline(world.id_community, [], actor="user-1")
        confirmed = await _row(world, EAN[0])
        assert confirmed["regulatory_status"] == int(RegulatoryStatus.ACTIVE)

        removal = await _filed(world, meter(world, "operation.meter_removed", EAN[0]))
        await _sync(world, removal.id)
        leaving = await _row(world, EAN[0])
        await _step(world, removal, "cancelled")
        await _sync(world, removal.id)

        assert leaving["id_dossier_origin"] == confirmed["id_dossier_origin"]
        assert _state(leaving)[0] == int(RegulatoryStatus.LEAVING)
        assert _state(leaving)[3] == str(removal.id)
        assert _state(await _row(world, EAN[0])) == _state(confirmed)


class TestTheCoresConstraint:
    def _write(
        self, world: World, operation: int, origin: int, status: RegulatoryStatus
    ) -> EpisodeWrite:
        return EpisodeWrite(
            id_sharing_operation=operation,
            subject_type=1,
            subject_ref=EAN[0],
            id_dossier_origin=origin,
            regulatory_status=status,
            valid_from=DAY if status == RegulatoryStatus.ACTIVE else None,
        )

    async def test_a_meter_leaving_one_operation_for_another_in_one_write(self, world):
        other = await create_sharing_operation(
            world.session, id_community=world.id_community, name="Op Sud"
        )
        port = SqlCoreEffects(world.session)
        await port.apply(
            world.id_community,
            [self._write(world, world.operation, 1, RegulatoryStatus.ACTIVE)],
            audit_payload={},
        )

        # the entry listed first: the exit still goes first
        await port.apply(
            world.id_community,
            [
                self._write(world, other, 2, RegulatoryStatus.PENDING_GRD),
                self._write(world, world.operation, 1, RegulatoryStatus.LEAVING),
            ],
            audit_payload={},
        )

        assert [r["regulatory_status"] for r in await _rows(world)] == [
            int(RegulatoryStatus.LEAVING),
            int(RegulatoryStatus.PENDING_GRD),
        ]

    async def test_pending_in_a_second_operation_while_active_in_the_first_is_refused(self, world):
        other = await create_sharing_operation(
            world.session, id_community=world.id_community, name="Op Sud"
        )
        port = SqlCoreEffects(world.session)
        await port.apply(
            world.id_community,
            [self._write(world, world.operation, 1, RegulatoryStatus.ACTIVE)],
            audit_payload={},
        )

        with pytest.raises(EffectConflictError) as refused:
            await port.apply(
                world.id_community,
                [self._write(world, other, 2, RegulatoryStatus.PENDING_GRD)],
                audit_payload={},
            )
        assert refused.value.subjects == [EAN[0]]
