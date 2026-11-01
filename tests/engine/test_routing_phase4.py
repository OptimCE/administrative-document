"""engine.routing, phase 4: a community change once per operation, an opening
procedure absorbing its operation's first changes, a deleted operation undoing
its request. On the fake regulator, with an opening procedure added."""

from __future__ import annotations

from pathlib import Path

import pytest

from engine.lifecycle import context_for, move_to_step
from engine.routing import DuplicateEventError
from regulators.testing.fake import FakeRegulator
from shared.const import ChangeStatus, EventOutcome, ProcedureStatus
from tests.conftest import create_sharing_operation
from tests.engine.catalogue_files import fake_catalogue_copy, fake_document
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
    procedures,
    route,
)


def _opening_plugin(directory: Path) -> FakeRegulator:
    """The fake plus FAKE_OPENING: an operation's opening request, which takes
    the operation's meters, key and representative while its draft is open."""
    document = fake_document("FAKE_COMMUNITY.v1.yaml")
    document.update(
        code="FAKE_OPENING",
        family="FAKE_OPENING",
        scope="operation",
        recipient="grd_ref",
        handles_change_kinds=["operation_created", "operation_deleted"],
        absorbs_change_kinds=[
            "meter_added",
            "meter_removed",
            "key_changed",
            "representative_changed",
        ],
    )
    return FakeRegulator(
        catalogue_dir=fake_catalogue_copy(directory, {"FAKE_OPENING.v1.yaml": document})
    )


async def _operation(world: World, name: str) -> int:
    return await create_sharing_operation(world.session, id_community=world.id_community, name=name)


async def _send(world: World, dossier_id: int, code: str) -> None:
    draft = await dossier(world, dossier_id)
    ctx = await context_for(world.session, world.plugin, code, 1)
    await move_to_step(world.session, ctx, draft, "ready", day=NOW.date())
    await move_to_step(world.session, ctx, draft, "sent", day=NOW.date())


def _created(world: World, operation: int):
    return event(
        world,
        "operation.created",
        payload={"id_sharing_operation": operation, "name": "Nouvelle", "type": 2},
        aggregate=("operation", str(operation)),
    )


class TestACommunityChangeOncePerOperation:
    async def test_every_operation_holding_meters_gets_its_own(self, db_session):
        """Spec: a new representative is PARTAGE_MODIF per operation plus
        CE_ACTUALISATION. An operation without meters has nothing to notify."""
        world = await make_world(db_session)
        second = await _operation(world, "Op Nord")
        await _operation(world, "Op vide")
        await add_meter(world, EAN[0])
        await add_meter(world, EAN[1], operation=second)
        await add_meter(world, EAN[2], operation=second, status=3)  # waiting: there too

        changed = event(world, "community.representative_changed", payload={"fields": ["email"]})
        result = await route(world, changed)

        drafts = await procedures(world)
        assert sorted((d.family, d.id_sharing_operation) for d in drafts) == [
            ("FAKE_CHANGE", world.operation),
            ("FAKE_CHANGE", second),
            ("FAKE_COMMUNITY", None),
        ]
        assert result.outcome == EventOutcome.ROUTED
        assert set(result.dossiers) == {d.id for d in drafts}
        filed = await changes(world)
        assert {c.source_event_id for c in filed} == {changed.event_id}
        assert sorted(
            (c.procedure_code, c.id_sharing_operation, c.status) for c in filed
        ) == sorted(
            [
                ("FAKE_CHANGE", world.operation, ChangeStatus.ATTACHED),
                ("FAKE_CHANGE", second, ChangeStatus.ATTACHED),
                ("FAKE_COMMUNITY", None, ChangeStatus.ATTACHED),
            ]
        )

    async def test_it_is_still_processed_once(self, db_session):
        world = await make_world(db_session)
        await add_meter(world, EAN[0])
        changed = event(world, "community.representative_changed")
        await route(world, changed)

        with pytest.raises(DuplicateEventError):
            await route(world, changed)
        assert len(await changes(world)) == 2

    async def test_a_community_without_meters_files_the_community_procedure_only(self, db_session):
        world = await make_world(db_session)
        await route(world, event(world, "community.representative_changed"))
        (draft,) = await procedures(world)
        assert draft.family == "FAKE_COMMUNITY"


class TestTheOpeningRequestAbsorbs:
    async def test_a_new_operation_opens_its_request(self, db_session, tmp_path):
        world = await make_world(db_session, plugin=_opening_plugin(tmp_path))
        result = await route(world, _created(world, world.operation))

        (draft,) = await procedures(world)
        assert (draft.family, draft.id_sharing_operation) == ("FAKE_OPENING", world.operation)
        assert result.dossiers == (draft.id,)

    async def test_its_first_meters_go_into_it_not_into_a_modification(self, db_session, tmp_path):
        world = await make_world(db_session, plugin=_opening_plugin(tmp_path))
        await route(world, _created(world, world.operation))
        await route(world, meter(world, "operation.meter_added", EAN[0]))

        (draft,) = await procedures(world)
        assert draft.family == "FAKE_OPENING"
        assert sorted(c.kind for c in await changes(world, draft.id)) == [
            "meter_added",
            "operation_created",
        ]

    async def test_added_then_removed_before_sending_cancels_both(self, db_session, tmp_path):
        world = await make_world(db_session, plugin=_opening_plugin(tmp_path))
        await route(world, _created(world, world.operation))
        await route(world, meter(world, "operation.meter_added", EAN[0]))
        await route(world, meter(world, "operation.meter_removed", EAN[0]))

        (draft,) = await procedures(world)
        statuses = {c.kind: c.status for c in await changes(world, draft.id)}
        assert statuses == {
            "operation_created": ChangeStatus.ATTACHED,
            "meter_added": ChangeStatus.CANCELLED,
            "meter_removed": ChangeStatus.CANCELLED,
        }

    async def test_once_sent_the_next_change_is_a_modification(self, db_session, tmp_path):
        world = await make_world(db_session, plugin=_opening_plugin(tmp_path))
        opened = await route(world, _created(world, world.operation))
        await _send(world, opened.dossiers[0], "FAKE_OPENING")

        await route(world, meter(world, "operation.meter_added", EAN[0]))

        families = sorted(d.family for d in await procedures(world))
        assert families == ["FAKE_CHANGE", "FAKE_OPENING"]

    async def test_only_its_own_operation(self, db_session, tmp_path):
        """A representative change: the operation still being opened takes it in
        its request, the other one in a modification, the community its own."""
        world = await make_world(db_session, plugin=_opening_plugin(tmp_path))
        second = await _operation(world, "Op Nord")
        await add_meter(world, EAN[0])
        await add_meter(world, EAN[1], operation=second)
        await route(world, _created(world, second))

        await route(world, event(world, "community.representative_changed"))

        placed = sorted(
            (c.procedure_code, c.id_sharing_operation, c.kind) for c in await changes(world)
        )
        assert placed == [
            ("FAKE_CHANGE", world.operation, "representative_changed"),
            ("FAKE_COMMUNITY", None, "representative_changed"),
            ("FAKE_OPENING", second, "operation_created"),
            ("FAKE_OPENING", second, "representative_changed"),
        ]


class TestADeletedOperation:
    async def test_cancels_a_request_that_holds_nothing_else(self, db_session, tmp_path):
        world = await make_world(db_session, plugin=_opening_plugin(tmp_path))
        opened = await route(world, _created(world, world.operation))
        deleted = event(
            world,
            "operation.deleted",
            payload={"id_sharing_operation": world.operation, "name": "Nouvelle"},
        )

        result = await route(world, deleted)

        request = await dossier(world, opened.dossiers[0])
        assert request.status == ProcedureStatus.CANCELLED
        assert result.outcome == EventOutcome.ROUTED
        assert {c.status for c in await changes(world)} == {ChangeStatus.CANCELLED}

    async def test_with_nothing_to_undo_files_nothing(self, db_session, tmp_path):
        world = await make_world(db_session, plugin=_opening_plugin(tmp_path))
        result = await route(
            world, event(world, "operation.deleted", payload={"id_sharing_operation": 1})
        )
        assert result.outcome == EventOutcome.NO_PROCEDURE
        assert await procedures(world) == []
        assert await changes(world) == []
