"""engine.baseline through the admin service, on the fake regulator.

The service is where the order of the commits across the two databases is
decided (core, then annexe, then the episodes' references); one Postgres holds
both in tests, and each commit releases a savepoint of the test transaction.
"""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy import func, select, text

from api.admin.service import AdminService, ModuleInactiveError
from engine.baseline import (
    BaselineError,
    Choice,
    ChoiceIn,
    brussels_today,
    episode_writes,
    resolve,
)
from ports.core_effects import EffectConflictError
from ports.core_effects_sqlalchemy import SqlCoreEffects
from ports.crm_engine import SqlEngineCrmRead
from shared.const import ChangeStatus, EventOutcome, ProcedureStatus, RegulatoryStatus
from shared.models.local_models import ChangeModel, DeadlineModel, ModuleActivationModel
from tests.conftest import create_sharing_operation
from tests.engine.world import (
    EAN,
    NOW,
    active_changes,
    add_key,
    add_meter,
    baseline_procedure,
    make_world,
    meter,
    procedures,
    route,
    set_subscription,
)

D = datetime.date
UTC = datetime.UTC


def _service(world) -> AdminService:
    return AdminService(
        local_session=world.session,
        crm_session=world.session,
        crm_read=SqlEngineCrmRead(world.session),
        effects=SqlCoreEffects(world.session),
        registry=world.registry,
        region_of=lambda _code: 1,
    )


async def _world(db_session, **kw):
    world = await make_world(db_session, **kw)
    await add_meter(world, EAN[0], member=5)
    await add_meter(world, EAN[1], member=6, injection=1)
    key = await add_key(world)
    return world, key


async def _episodes(world) -> dict[str, dict]:
    rows = await world.session.execute(
        text(
            "SELECT subject_type, subject_ref, regulatory_status, valid_from, valid_to, "
            " id_dossier_origin, regulatory_ref, id FROM regulatory_link_status "
            "WHERE id_community = :c ORDER BY id"
        ),
        {"c": world.id_community},
    )
    return {row.subject_ref: dict(row._mapping) for row in rows}


async def _period(world) -> ModuleActivationModel:
    periods: list[ModuleActivationModel] = list(
        await world.session.scalars(
            select(ModuleActivationModel)
            .where(ModuleActivationModel.id_community == world.id_community)
            .order_by(ModuleActivationModel.id)
        )
    )
    for period in periods:
        await world.session.refresh(period)
    return periods[-1]


def _key_of(view, ref: str) -> str:
    return str(next(item["key"] for item in view["items"] if item["subject_ref"] == ref))


class TestFirstActivation:
    async def test_the_starting_state_is_the_one_task_and_nothing_older_is_replayed(
        self, db_session
    ):
        """Acceptance criterion 1.8."""
        world, key = await _world(db_session)
        older = meter(
            world, "operation.meter_added", EAN[2], at=datetime.datetime(2025, 12, 1, tzinfo=UTC)
        )

        assert (await route(world, older)).outcome == EventOutcome.BEFORE_ACTIVATION

        assert await procedures(world) == []
        task = await baseline_procedure(world)
        assert task is not None
        assert (task.step, task.status, task.next_action_code) == (
            "draft",
            ProcedureStatus.DRAFT,
            "confirm_baseline",
        )
        baseline = (await _period(world)).baseline_json
        assert baseline is not None
        listed = sorted(
            (i["subject_type"], i["subject_ref"], i["change"]) for i in baseline["items"]
        )
        assert listed == [(1, EAN[0], "present"), (1, EAN[1], "present"), (2, str(key), "present")]
        assert {i["default"] for i in baseline["items"]} == {"active"}

    async def test_an_addition_the_photo_holds_is_already_covered(self, db_session):
        world, _key = await _world(db_session)
        covered = await route(world, meter(world, "operation.meter_added", EAN[0]))
        new = await route(world, meter(world, "operation.meter_added", EAN[3]))
        assert covered.outcome == EventOutcome.ALREADY_REFLECTED
        assert new.outcome == EventOutcome.ROUTED

    async def test_nothing_to_confirm_is_confirmed_at_once(self, db_session):
        world = await make_world(db_session)
        await route(world, meter(world, "operation.meter_added", EAN[0]))
        task = await baseline_procedure(world)
        assert task is not None
        assert task.status == ProcedureStatus.IN_FORCE
        period = await _period(world)
        assert (period.baseline_confirmed_at is not None, period.confirmed_by) == (
            True,
            "system:engine",
        )


class TestConfirmation:
    async def test_by_default_everything_is_active(self, db_session):
        world, key = await _world(db_session)
        service = _service(world)
        pending = await service.baseline(world.id_community)
        assert pending["status"] == "pending"

        view = await service.confirm_baseline(world.id_community, [], actor="user-1")

        assert (view["status"], view["confirmed_by"]) == ("confirmed", "user-1")
        origin = view["id_dossier"]
        episodes = await _episodes(world)
        assert {ref: e["regulatory_status"] for ref, e in episodes.items()} == {
            EAN[0]: RegulatoryStatus.ACTIVE,
            EAN[1]: RegulatoryStatus.ACTIVE,
            str(key): RegulatoryStatus.ACTIVE,
        }
        assert {e["id_dossier_origin"] for e in episodes.values()} == {origin}
        assert {e["regulatory_ref"] for e in episodes.values()} == {str(origin)}
        task = await baseline_procedure(world)
        assert task is not None and task.status == ProcedureStatus.IN_FORCE
        applied = await db_session.scalar(
            select(func.count()).where(
                ChangeModel.id_dossier == origin, ChangeModel.status == int(ChangeStatus.APPLIED)
            )
        )
        assert applied == 3
        audit = await db_session.execute(
            text(
                "SELECT entity_id, payload FROM audit_log WHERE id_community = :c "
                "AND action = 'administrative_document.regulatory_effect.applied'"
            ),
            {"c": world.id_community},
        )
        (row,) = audit.all()
        assert row.entity_id == str(origin)
        assert len(row.payload["episodes"]) == 3

    async def test_each_choice_has_its_consequence(self, db_session):
        world, key = await _world(db_session)
        service = _service(world)
        view = await service.baseline(world.id_community)

        await service.confirm_baseline(
            world.id_community,
            [
                ChoiceIn(_key_of(view, EAN[0]), Choice.NOT_NOTIFIED),
                ChoiceIn(_key_of(view, EAN[1]), Choice.IN_PROGRESS, sent_date=D(2026, 10, 15)),
                ChoiceIn(_key_of(view, str(key)), Choice.ACTIVE, effective_date=D(2026, 9, 1)),
            ],
            actor="user-1",
        )

        draft, sent = await procedures(world)
        assert (draft.status, sent.status, sent.step) == (
            ProcedureStatus.DRAFT,
            ProcedureStatus.SENT,
            "sent",
        )
        assert [c.subject_ref for c in await active_changes(world, draft.id)] == [EAN[0]]
        assert [c.subject_ref for c in await active_changes(world, sent.id)] == [EAN[1]]
        assert sent.frozen_at is not None
        assert brussels_today(sent.frozen_at) == D(2026, 10, 15)
        answer = await db_session.scalar(
            select(DeadlineModel).where(
                DeadlineModel.id_dossier == sent.id, DeadlineModel.deadline_type == "grd_answer"
            )
        )
        assert answer is not None and answer.due_date == D(2026, 10, 29)  # 10 working days

        episodes = await _episodes(world)
        assert (episodes[EAN[0]]["regulatory_status"], episodes[EAN[0]]["regulatory_ref"]) == (
            RegulatoryStatus.TO_NOTIFY,
            str(draft.id),
        )
        assert (episodes[EAN[1]]["regulatory_status"], episodes[EAN[1]]["regulatory_ref"]) == (
            RegulatoryStatus.PENDING_GRD,
            str(sent.id),
        )
        assert (episodes[str(key)]["regulatory_status"], episodes[str(key)]["valid_from"]) == (
            RegulatoryStatus.ACTIVE,
            D(2026, 9, 1),
        )

    async def test_confirming_twice_changes_nothing(self, db_session):
        world, _key = await _world(db_session)
        service = _service(world)
        first = await service.confirm_baseline(world.id_community, [], actor="user-1")
        counts = (len(await _episodes(world)), len(await procedures(world, with_baseline=True)))

        again = await service.confirm_baseline(
            world.id_community,
            [ChoiceIn(_key_of(first, EAN[0]), Choice.NOT_NOTIFIED)],
            actor="user-2",
        )

        assert (again["status"], again["confirmed_by"]) == ("confirmed", "user-1")
        assert (len(await _episodes(world)), len(await procedures(world, with_baseline=True))) == (
            counts
        )

    async def test_a_crash_between_the_core_and_the_annexe_is_completed_by_a_replay(
        self, db_session
    ):
        world, _key = await _world(db_session)
        service = _service(world)
        await service.baseline(world.id_community)
        period = await _period(world)
        assert period.baseline_json is not None
        # The core's commit landed, the annexe's did not.
        decisions = resolve(period.baseline_json, [])
        writes = episode_writes(period.baseline_json, decisions, today=NOW.date())
        await SqlCoreEffects(db_session).apply(world.id_community, writes, audit_payload={})
        await db_session.commit()

        view = await service.confirm_baseline(world.id_community, [], actor="user-1")

        assert view["status"] == "confirmed"
        assert len(await _episodes(world)) == 3  # the same three, not six

    async def test_the_core_refuses_an_ean_active_in_another_operation(self, db_session):
        world, _key = await _world(db_session)
        elsewhere = await create_sharing_operation(
            db_session, id_community=world.id_community, name="Op Sud"
        )
        await db_session.execute(
            text(
                "INSERT INTO regulatory_link_status (id_community, id_sharing_operation, "
                " subject_type, subject_ref, id_dossier_origin, regulatory_status, valid_from) "
                "VALUES (:c, :o, 1, :ean, 999, 5, DATE '2025-01-01')"
            ),
            {"c": world.id_community, "o": elsewhere, "ean": EAN[0]},
        )
        service = _service(world)

        with pytest.raises(EffectConflictError) as refused:
            await service.confirm_baseline(world.id_community, [], actor="user-1")

        assert refused.value.subjects == [EAN[0]]
        period = await _period(world)
        assert period.baseline_confirmed_at is None
        task = await baseline_procedure(world)
        assert task is not None and task.status == ProcedureStatus.DRAFT
        assert set(await _episodes(world)) == {EAN[0]}  # only the one that was there

    @pytest.mark.parametrize(
        ("choice", "message"),
        [
            (ChoiceIn("1:0:nowhere", Choice.ACTIVE), "is not on the baseline"),
            (ChoiceIn("EAN0", Choice.ENDED), "ended is not an option"),
            (ChoiceIn("EAN0", Choice.IN_PROGRESS), "needs the date it was sent"),
        ],
    )
    async def test_choices_that_do_not_fit_are_refused(self, db_session, choice, message):
        world, _key = await _world(db_session)
        service = _service(world)
        view = await service.baseline(world.id_community)
        if choice.key == "EAN0":
            choice = ChoiceIn(_key_of(view, EAN[0]), choice.choice)
        with pytest.raises(BaselineError, match=message):
            await service.confirm_baseline(world.id_community, [choice], actor="user-1")
        assert (await _period(world)).baseline_confirmed_at is None


class TestReactivation:
    async def test_only_what_changed_while_off_is_listed(self, db_session):
        world, _key = await _world(db_session)
        service = _service(world)
        await service.confirm_baseline(world.id_community, [], actor="user-1")
        before = await _episodes(world)

        await set_subscription(world, active=False, at=datetime.datetime(2026, 10, 20, tzinfo=UTC))
        with pytest.raises(ModuleInactiveError):
            await service.baseline(world.id_community)
        await db_session.execute(
            text("UPDATE meter_data SET end_date = DATE '2026-10-21' WHERE ean = :ean"),
            {"ean": EAN[1]},
        )
        await add_meter(world, EAN[2], member=7, start=D(2026, 10, 22))
        await set_subscription(world, active=True, at=datetime.datetime(2026, 10, 25, tzinfo=UTC))

        view = await service.baseline(world.id_community)

        assert (view["status"], view["kind"]) == ("pending", "reactivation")
        listed = sorted((i["subject_ref"], i["change"], i["default_choice"]) for i in view["items"])
        assert listed == [(EAN[1], "removed", "not_notified"), (EAN[2], "added", "not_notified")]

        await service.confirm_baseline(world.id_community, [], actor="user-1")

        after = await _episodes(world)
        assert after[EAN[1]]["id"] == before[EAN[1]]["id"]  # the same episode, moved
        assert after[EAN[1]]["regulatory_status"] == RegulatoryStatus.LEAVING
        assert after[EAN[2]]["regulatory_status"] == RegulatoryStatus.TO_NOTIFY
        assert after[EAN[0]] == before[EAN[0]]  # unchanged, untouched
        (draft,) = await procedures(world)
        kinds = sorted((c.kind, c.subject_ref) for c in await active_changes(world, draft.id))
        assert kinds == [("meter_added", EAN[2]), ("meter_removed", EAN[1])]
