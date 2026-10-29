"""engine.routing on the fake regulator: events filed into procedures."""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy import func, select, text

from engine.lifecycle import context_for, move_to_step, open_procedure
from engine.routing import DuplicateEventError
from shared.const import (
    ChangeStatus,
    DocumentKind,
    EventOutcome,
    OpenedBy,
    ProcedureDocumentStatus,
    ProcedureStatus,
    SubjectType,
)
from shared.models.local_models import (
    ChangeModel,
    DossierModel,
    ModuleActivationModel,
    ProcessedEventModel,
)
from tests.engine.world import (
    EAN,
    NOW,
    active_changes,
    changes,
    dossier,
    event,
    journal,
    make_world,
    meter,
    pieces,
    procedures,
    route,
    set_subscription,
)

D = datetime.date
LATER = NOW + datetime.timedelta(hours=1)


async def _count(session, model) -> int:
    return int(await session.scalar(select(func.count()).select_from(model)) or 0)


async def _outcome(session, core_event) -> int:
    row = await session.get(ProcessedEventModel, core_event.event_id)
    assert row is not None
    return int(row.outcome)


class TestAChangeIsFiled:
    async def test_an_added_meter_opens_a_draft(self, db_session):
        world = await make_world(db_session)
        added = meter(world, "operation.meter_added", EAN[0], member=5)

        result = await route(world, added)

        assert result.outcome == EventOutcome.ROUTED
        (dossier_id,) = result.dossiers
        draft = await dossier(world, dossier_id)
        assert (draft.family, draft.step, draft.status) == ("FAKE_CHANGE", "draft", 1)
        assert (draft.id_sharing_operation, draft.opened_by) == (world.operation, OpenedBy.SYSTEM)
        assert draft.variant == "full"  # an added meter escalates the route
        assert draft.grd_ref == "ORES"
        assert draft.next_action_code == "complete_documents"
        (change,) = await changes(world, dossier_id)
        assert (change.kind, change.subject_ref, change.status) == (
            "meter_added",
            EAN[0],
            ChangeStatus.ATTACHED,
        )
        assert change.source_event_id == added.event_id
        assert change.payload_json["grd"] == "ORES"
        (birth,) = await journal(world, SubjectType.DOSSIER, dossier_id)
        assert (birth.to_status, birth.to_step, birth.actor_id) == (1, "draft", "system:engine")
        assert await _outcome(db_session, added) == EventOutcome.ROUTED

    async def test_its_pieces_are_created_missing(self, db_session):
        world = await make_world(db_session)
        result = await route(world, meter(world, "operation.meter_added", EAN[0]))
        listed = [
            (p.requirement_code, p.kind, p.required, p.status)
            for p in await pieces(world, result.dossiers[0])
        ]
        assert listed == [
            ("change_form", DocumentKind.GENERATED, True, ProcedureDocumentStatus.MISSING),
            # The fake's condition is not in the context: listed, not required.
            ("key_description", DocumentKind.GENERATED, False, ProcedureDocumentStatus.MISSING),
        ]

    async def test_three_added_and_one_removed_make_one_procedure_of_four_changes(self, db_session):
        """Acceptance criterion 1.1, on the fake: one draft, escalated route."""
        world = await make_world(db_session)
        await route(world, meter(world, "operation.meter_removed", EAN[3]))
        for ean in EAN[:3]:
            await route(world, meter(world, "operation.meter_added", ean))

        (draft,) = await procedures(world)
        await db_session.refresh(draft)
        assert draft.variant == "full"
        assert len(await active_changes(world, draft.id)) == 4

    async def test_a_removal_alone_keeps_the_light_route(self, db_session):
        world = await make_world(db_session)
        result = await route(world, meter(world, "operation.meter_removed", EAN[0]))
        assert (await dossier(world, result.dossiers[0])).variant == "light"

    async def test_a_removal_of_a_meter_nobody_tracks_is_still_filed(self, db_session):
        """Before the baseline is confirmed the annexe cannot know the meter was
        absent: a removal is never skipped as already reflected."""
        world = await make_world(db_session)
        removed = meter(world, "operation.meter_removed", EAN[0])
        assert (await route(world, removed)).outcome == EventOutcome.ROUTED


class TestCompensation:
    async def test_added_then_removed_before_sending_leaves_nothing(self, db_session):
        """Acceptance criterion 1.2, on the fake."""
        world = await make_world(db_session)
        first = await route(world, meter(world, "operation.meter_added", EAN[0]))
        await route(world, meter(world, "operation.meter_removed", EAN[0], at=LATER))

        assert [c.status for c in await changes(world)] == [
            ChangeStatus.CANCELLED,
            ChangeStatus.CANCELLED,
        ]
        draft = await dossier(world, first.dossiers[0])
        assert (draft.status, draft.step) == (ProcedureStatus.CANCELLED, "cancelled")
        last = (await journal(world, SubjectType.DOSSIER, draft.id))[-1]
        assert last.context_json == {"reason": "compensated"}

    async def test_only_the_inverse_is_cancelled(self, db_session):
        world = await make_world(db_session)
        first = await route(world, meter(world, "operation.meter_added", EAN[0]))
        await route(world, meter(world, "operation.meter_added", EAN[1]))
        await route(world, meter(world, "operation.meter_removed", EAN[0], at=LATER))

        draft = await dossier(world, first.dossiers[0])
        assert draft.status == ProcedureStatus.DRAFT
        assert [c.subject_ref for c in await active_changes(world, draft.id)] == [EAN[1]]

    async def test_a_draft_a_person_opened_is_not_cancelled_when_emptied(self, db_session):
        world = await make_world(db_session)
        first = await route(world, meter(world, "operation.meter_added", EAN[0]))
        await db_session.execute(
            text("UPDATE dossier SET opened_by = :user WHERE id = :id"),
            {"user": int(OpenedBy.USER), "id": first.dossiers[0]},
        )
        await route(world, meter(world, "operation.meter_removed", EAN[0], at=LATER))
        draft = await dossier(world, first.dossiers[0])
        assert draft.status == ProcedureStatus.DRAFT
        assert await active_changes(world, draft.id) == []


async def _send(world, dossier_id: int) -> None:
    draft = await dossier(world, dossier_id)
    ctx = await context_for(world.session, world.plugin, "FAKE_CHANGE", 1)
    await move_to_step(world.session, ctx, draft, "ready", day=NOW.date())
    await move_to_step(world.session, ctx, draft, "sent", day=NOW.date())


class TestAfterSending:
    async def test_a_change_after_sending_opens_a_new_draft(self, db_session):
        """Acceptance criterion 1.3, on the fake."""
        world = await make_world(db_session)
        first = await route(world, meter(world, "operation.meter_added", EAN[0]))
        await _send(world, first.dossiers[0])

        second = await route(world, meter(world, "operation.meter_added", EAN[1], at=LATER))

        assert second.dossiers != first.dossiers
        assert (await dossier(world, first.dossiers[0])).status == ProcedureStatus.SENT
        assert [c.subject_ref for c in await active_changes(world, first.dossiers[0])] == [EAN[0]]
        assert (await dossier(world, second.dossiers[0])).status == ProcedureStatus.DRAFT

    async def test_a_removal_after_sending_is_not_compensated(self, db_session):
        world = await make_world(db_session)
        first = await route(world, meter(world, "operation.meter_added", EAN[0]))
        await _send(world, first.dossiers[0])

        second = await route(world, meter(world, "operation.meter_removed", EAN[0], at=LATER))

        assert [c.status for c in await changes(world, first.dossiers[0])] == [
            ChangeStatus.ATTACHED
        ]
        assert [c.kind for c in await active_changes(world, second.dossiers[0])] == [
            "meter_removed"
        ]


class TestTheDraftFollowsItsChanges:
    async def test_a_ready_draft_goes_back_to_draft(self, db_session):
        world = await make_world(db_session)
        first = await route(world, meter(world, "operation.meter_added", EAN[0]))
        draft = await dossier(world, first.dossiers[0])
        ctx = await context_for(db_session, world.plugin, "FAKE_CHANGE", 1)
        await move_to_step(db_session, ctx, draft, "ready", day=NOW.date())

        await route(world, meter(world, "operation.meter_added", EAN[1], at=LATER))

        draft = await dossier(world, draft.id)
        assert (draft.status, draft.step) == (ProcedureStatus.DRAFT, "draft")
        assert (await journal(world, SubjectType.DOSSIER, draft.id))[-1].context_json == {
            "reason": "new_change"
        }

    async def test_a_generated_piece_goes_stale(self, db_session):
        world = await make_world(db_session)
        first = await route(world, meter(world, "operation.meter_added", EAN[0]))
        form = (await pieces(world, first.dossiers[0]))[0]
        version = await db_session.execute(
            text(
                "INSERT INTO document_version (id_community, id_document, version_no, file_ref) "
                "VALUES (:c, :d, 1, 'x/1') RETURNING id"
            ),
            {"c": world.id_community, "d": form.id},
        )
        await db_session.execute(
            text("UPDATE document SET current_version_id = :v WHERE id = :d"),
            {"v": version.scalar_one(), "d": form.id},
        )
        await db_session.execute(
            text(
                "INSERT INTO status_event (id_community, subject_type, subject_id, "
                " from_status, to_status) VALUES (:c, 1, :d, 6, 2)"
            ),
            {"c": world.id_community, "d": form.id},
        )
        # Raw SQL bypassed the identity map; the worker's session is fresh per event.
        db_session.expire_all()

        await route(world, meter(world, "operation.meter_added", EAN[1], at=LATER))

        assert (await pieces(world, first.dossiers[0]))[0].status == ProcedureDocumentStatus.STALE

    async def test_a_converted_july_draft_keeps_its_route(self, db_session):
        world = await make_world(db_session)
        ctx = await context_for(db_session, world.plugin, "FAKE_CHANGE", 1)
        converted = await open_procedure(
            db_session,
            ctx,
            id_community=world.id_community,
            id_sharing_operation=world.operation,
            region=1,
            variant="full",
            opened_by=OpenedBy.MIGRATION,
            day=D(2026, 9, 1),
        )

        result = await route(world, meter(world, "operation.meter_removed", EAN[0]))

        assert result.dossiers == (converted.id,)
        assert (await dossier(world, converted.id)).variant == "full"

    async def test_a_community_event_feeds_only_community_procedures(self, db_session):
        """representative_changed feeds FAKE_CHANGE (per operation) and
        FAKE_COMMUNITY: without an operation, only the second can take it."""
        world = await make_world(db_session)
        result = await route(world, event(world, "community.representative_changed"))
        (draft,) = await procedures(world)
        assert result.dossiers == (draft.id,)
        assert (draft.family, draft.id_sharing_operation) == ("FAKE_COMMUNITY", None)


class TestNothingIsFiled:
    async def test_while_the_module_is_inactive(self, db_session):
        """Acceptance criterion 1.6: nothing created, the event acknowledged."""
        world = await make_world(db_session, active=False)
        added = meter(world, "operation.meter_added", EAN[0])

        result = await route(world, added)

        assert result.outcome == EventOutcome.MODULE_DISABLED
        assert await procedures(world) == []
        assert await _count(db_session, ChangeModel) == 0
        assert await _outcome(db_session, added) == EventOutcome.MODULE_DISABLED

    async def test_from_the_event_after_a_deactivation(self, db_session):
        """Acceptance criterion 1.7: no cache, the very next event stops."""
        world = await make_world(db_session)
        assert (await route(world, meter(world, "operation.meter_added", EAN[0]))).outcome == (
            EventOutcome.ROUTED
        )
        await set_subscription(
            world, active=False, at=datetime.datetime(2026, 11, 2, 9, 30, tzinfo=datetime.UTC)
        )

        stopped = await route(world, meter(world, "operation.meter_added", EAN[1], at=LATER))

        assert stopped.outcome == EventOutcome.MODULE_DISABLED
        (period,) = await db_session.scalars(
            select(ModuleActivationModel).where(
                ModuleActivationModel.id_community == world.id_community
            )
        )
        assert period.disabled_at is not None

    async def test_for_an_event_older_than_the_activation(self, db_session):
        world = await make_world(
            db_session, activated_at=datetime.datetime(2026, 11, 2, 12, 0, tzinfo=datetime.UTC)
        )
        result = await route(world, meter(world, "operation.meter_added", EAN[0]))
        assert result.outcome == EventOutcome.BEFORE_ACTIVATION
        assert await procedures(world) == []

    async def test_a_reactivation_opens_a_new_period(self, db_session):
        world = await make_world(db_session, active=False)
        await route(world, meter(world, "operation.meter_added", EAN[0]))
        await set_subscription(
            world, active=True, at=datetime.datetime(2026, 11, 2, 9, 30, tzinfo=datetime.UTC)
        )

        result = await route(world, meter(world, "operation.meter_added", EAN[1], at=LATER))

        assert result.outcome == EventOutcome.ROUTED
        periods = list(
            await db_session.scalars(
                select(ModuleActivationModel).where(
                    ModuleActivationModel.id_community == world.id_community
                )
            )
        )
        assert len(periods) == 1  # never seen active before: only the new one
        assert periods[0].enabled_at == datetime.datetime(2026, 11, 2, 9, 30, tzinfo=datetime.UTC)

    async def test_for_a_regulator_without_a_plugin(self, db_session):
        """Acceptance criterion 1.9, on the fake's registry."""
        world = await make_world(db_session, regulator="BE-BRU-BRUGEL")
        result = await route(world, meter(world, "operation.meter_added", EAN[0]))
        assert result.outcome == EventOutcome.REGULATOR_NOT_SUPPORTED
        assert await procedures(world) == []

    @pytest.mark.parametrize("event_type", ["community.member_joined", "operation.renamed"])
    async def test_for_a_change_no_procedure_takes(self, db_session, event_type):
        world = await make_world(db_session)
        result = await route(world, event(world, event_type, payload={"id_member": 5}))
        assert result.outcome == EventOutcome.NO_PROCEDURE
        assert await _count(db_session, ChangeModel) == 0

    async def test_for_a_meter_the_core_already_tracks(self, db_session):
        world = await make_world(db_session)
        await db_session.execute(
            text(
                "INSERT INTO regulatory_link_status (id_community, id_sharing_operation, "
                " subject_type, subject_ref, id_dossier_origin, regulatory_status, valid_from) "
                "VALUES (:c, :o, 1, :ean, 999, 5, DATE '2026-01-01')"
            ),
            {"c": world.id_community, "o": world.operation, "ean": EAN[0]},
        )
        result = await route(world, meter(world, "operation.meter_added", EAN[0]))
        assert result.outcome == EventOutcome.ALREADY_REFLECTED

    async def test_an_episode_that_ended_does_not_reflect_a_new_addition(self, db_session):
        world = await make_world(db_session)
        await db_session.execute(
            text(
                "INSERT INTO regulatory_link_status (id_community, id_sharing_operation, "
                " subject_type, subject_ref, id_dossier_origin, regulatory_status, valid_from, "
                " valid_to) VALUES (:c, :o, 1, :ean, 999, 5, DATE '2026-01-01', DATE '2026-06-30')"
            ),
            {"c": world.id_community, "o": world.operation, "ean": EAN[0]},
        )
        result = await route(world, meter(world, "operation.meter_added", EAN[0]))
        assert result.outcome == EventOutcome.ROUTED


class TestIdempotency:
    async def test_a_replayed_event_files_nothing_twice(self, db_session):
        """Acceptance criterion 0.2, on the consumer's side."""
        world = await make_world(db_session)
        added = meter(world, "operation.meter_added", EAN[0])
        await route(world, added)
        before = (await _count(db_session, ChangeModel), await _count(db_session, DossierModel))

        with pytest.raises(DuplicateEventError):
            await route(world, added)

        assert (await _count(db_session, ChangeModel), await _count(db_session, DossierModel)) == (
            before
        )

    async def test_a_skipped_event_is_recorded_too(self, db_session):
        world = await make_world(db_session, active=False)
        skipped = meter(world, "operation.meter_added", EAN[0])
        await route(world, skipped)
        with pytest.raises(DuplicateEventError):
            await route(world, skipped)
