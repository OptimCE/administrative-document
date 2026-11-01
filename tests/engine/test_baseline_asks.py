"""The baseline asks about the procedures whose trigger always lands before the
module can be on (phase 4): an operation's opening request, once per operation,
and the community's announcement, once. On the fake regulator, with both added."""

from __future__ import annotations

import datetime
from pathlib import Path

from sqlalchemy import text

from engine.baseline import Choice, ChoiceIn, ask_key
from regulators.testing.fake import FakeRegulator
from shared.const import ChangeStatus, EventOutcome, ProcedureStatus
from tests.conftest import create_sharing_operation
from tests.engine.catalogue_files import fake_catalogue_copy, fake_document
from tests.engine.test_baseline import _service
from tests.engine.world import (
    EAN,
    World,
    add_key,
    add_meter,
    baseline_procedure,
    changes,
    event,
    make_world,
    procedures,
    route,
)

D = datetime.date


def _asking_plugin(directory: Path) -> FakeRegulator:
    """The fake plus FAKE_OPENING (an operation's opening request, which takes
    its operation's meters) and FAKE_ANNOUNCE (the community's creation), both
    asked at baseline."""
    opening = fake_document("FAKE_COMMUNITY.v1.yaml")
    opening.update(
        code="FAKE_OPENING",
        family="FAKE_OPENING",
        scope="operation",
        recipient="grd_ref",
        handles_change_kinds=["operation_created"],
        absorbs_change_kinds=["meter_added", "meter_removed", "key_changed"],
        asked_at_baseline=True,
    )
    announce = fake_document("FAKE_COMMUNITY.v1.yaml")
    announce.update(
        code="FAKE_ANNOUNCE",
        family="FAKE_ANNOUNCE",
        handles_change_kinds=["community_created"],
        asked_at_baseline=True,
    )
    return FakeRegulator(
        catalogue_dir=fake_catalogue_copy(
            directory, {"FAKE_OPENING.v1.yaml": opening, "FAKE_ANNOUNCE.v1.yaml": announce}
        )
    )


async def _world(db_session, tmp_path) -> tuple[World, int]:
    """Two operations: the world's, running (an active meter, a key), and a new
    one whose only meter waits for its GRD."""
    world = await make_world(db_session, plugin=_asking_plugin(tmp_path))
    new = await create_sharing_operation(
        db_session, id_community=world.id_community, name="Op Nouvelle"
    )
    await add_meter(world, EAN[0])
    await add_key(world)
    await add_meter(world, EAN[1], operation=new, status=3)
    return world, new


async def _episode_refs(world: World) -> list[str]:
    rows = await world.session.execute(
        text("SELECT subject_ref FROM regulatory_link_status WHERE id_community = :c ORDER BY 1"),
        {"c": world.id_community},
    )
    return [str(r[0]) for r in rows]


class TestThePhoto:
    async def test_asks_once_per_operation_and_once_for_the_community(self, db_session, tmp_path):
        world, new = await _world(db_session, tmp_path)
        view = await _service(world).baseline(world.id_community)

        asked = [i for i in view["items"] if i["subject_type"] == "procedure"]
        assert [
            (i["procedure_code"], i["id_sharing_operation"], i["default_choice"]) for i in asked
        ] == [
            ("FAKE_ANNOUNCE", None, "active"),
            ("FAKE_OPENING", world.operation, "active"),  # an active meter: authorised
            ("FAKE_OPENING", new, "not_notified"),  # nothing active yet
        ]
        assert view["items"][: len(asked)] == asked  # asked first
        assert asked[1]["choices"] == ["active", "not_notified", "in_progress"]
        assert asked[1]["procedure_labels"]["fr"]
        assert asked[1]["key"] == ask_key("FAKE_OPENING", world.operation)

    async def test_a_community_with_nothing_active_has_not_notified_its_creation(
        self, db_session, tmp_path
    ):
        world = await make_world(db_session, plugin=_asking_plugin(tmp_path))
        await add_meter(world, EAN[0], status=4)
        view = await _service(world).baseline(world.id_community)
        defaults = {
            i["procedure_code"]: i["default_choice"]
            for i in view["items"]
            if i["subject_type"] == "procedure"
        }
        assert defaults == {"FAKE_ANNOUNCE": "not_notified", "FAKE_OPENING": "not_notified"}

    async def test_an_opening_in_flight_is_covered_by_the_question(self, db_session, tmp_path):
        world, new = await _world(db_session, tmp_path)
        await _service(world).baseline(world.id_community)
        created = event(
            world,
            "operation.created",
            payload={"id_sharing_operation": new, "type": 2},
            aggregate=("operation", str(new)),
        )
        assert (await route(world, created)).outcome == EventOutcome.ALREADY_REFLECTED
        assert await procedures(world) == []


class TestConfirming:
    async def test_by_default_the_new_operation_s_request_opens_with_its_meter(
        self, db_session, tmp_path
    ):
        world, new = await _world(db_session, tmp_path)
        await _service(world).confirm_baseline(world.id_community, [], actor="user-1")

        (draft,) = await procedures(world)
        assert (draft.family, draft.id_sharing_operation, draft.status) == (
            "FAKE_OPENING",
            new,
            ProcedureStatus.DRAFT,
        )
        # The waiting meter is in the request, not in a modification.
        assert sorted((c.kind, c.subject_ref) for c in await changes(world, draft.id)) == [
            ("meter_added", EAN[1]),
            ("operation_created", str(new)),
        ]
        # What was already done is on the baseline's record, nothing to send.
        task = await baseline_procedure(world)
        assert task is not None
        done = {
            (c.kind, c.id_sharing_operation)
            for c in await changes(world, task.id)
            if c.status == ChangeStatus.APPLIED
        }
        assert {("community_created", None), ("operation_created", world.operation)} <= done
        # A procedure has no episode in the core: only meters and keys do.
        assert EAN[0] in await _episode_refs(world)
        assert str(new) not in await _episode_refs(world)

    async def test_the_creation_not_notified_opens_its_draft(self, db_session, tmp_path):
        world, _new = await _world(db_session, tmp_path)
        await _service(world).confirm_baseline(
            world.id_community,
            [ChoiceIn(ask_key("FAKE_ANNOUNCE", None), Choice.NOT_NOTIFIED)],
            actor="user-1",
        )
        families = sorted((d.family, d.id_sharing_operation) for d in await procedures(world))
        assert ("FAKE_ANNOUNCE", None) in families

    async def test_a_request_in_progress_is_born_sent_with_its_meters(self, db_session, tmp_path):
        world, new = await _world(db_session, tmp_path)
        view = await _service(world).baseline(world.id_community)
        meter_key = next(i["key"] for i in view["items"] if i["subject_ref"] == EAN[1])

        await _service(world).confirm_baseline(
            world.id_community,
            [
                ChoiceIn(
                    ask_key("FAKE_OPENING", new), Choice.IN_PROGRESS, sent_date=D(2026, 10, 1)
                ),
                # A later date: still the request's, sent with it.
                ChoiceIn(meter_key, Choice.IN_PROGRESS, sent_date=D(2026, 10, 20)),
            ],
            actor="user-1",
        )

        (sent,) = await procedures(world)
        assert (sent.family, sent.status, sent.step) == (
            "FAKE_OPENING",
            ProcedureStatus.SENT,
            "sent",
        )
        assert sent.submitted_at is not None
        assert sent.submitted_at.date() in (D(2026, 9, 30), D(2026, 10, 1))
        assert sorted(c.kind for c in await changes(world, sent.id)) == [
            "meter_added",
            "operation_created",
        ]
