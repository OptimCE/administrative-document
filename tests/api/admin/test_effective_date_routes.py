"""Correcting the effect date by hand (phase 4; spec, « Réception de l'avenant »):
the GRD took effect from another day than the one the engine counted."""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy import select, text

from core.audit_log import AuditActions
from shared.const import DeadlineKind, DeadlineStatus, ProcedureStatus, RegulatoryStatus
from shared.models.local_models import DeadlineModel
from tests.api.admin.test_effects_routes import (
    EFFECTIVE,
    _core_row,
    _day,
    _file,
    _get,
    _headers,
    _procedure,
    _signed,
    _world,
)
from tests.engine.world import EAN

pytestmark = pytest.mark.asyncio

LATER = EFFECTIVE + datetime.timedelta(days=7)


async def _correct(
    client, headers, dossier_id: int, on: datetime.date, reason: str = "Avenant reçu le 7"
):
    return await client.post(
        f"/admin/dossiers/{dossier_id}/effective-date",
        json={"on": on.isoformat(), "reason": reason},
        headers=headers,
    )


async def _effect(db_session, dossier_id: int) -> DeadlineModel:
    deadline: DeadlineModel | None = await db_session.scalar(
        select(DeadlineModel).where(
            DeadlineModel.id_dossier == dossier_id,
            DeadlineModel.kind == int(DeadlineKind.EFFECT),
        )
    )
    assert deadline is not None
    await db_session.refresh(deadline)
    return deadline


class TestCorrectingTheEffectDate:
    async def test_moves_the_effect_its_badge_and_says_so_in_the_thread(
        self, client, db_session, admin_storage
    ):
        world = await _world(db_session)
        headers = await _headers(db_session, world)
        dossier_id = await _signed(client, db_session, world)
        assert (await _get(client, headers, dossier_id))["effective_date_correctable"] is True

        response = await _correct(client, headers, dossier_id, LATER)

        assert response.status_code == 200, response.text
        detail = response.json()["data"]
        assert detail["effective_date"] == LATER.isoformat()
        assert (await _effect(db_session, dossier_id)).due_date == LATER
        (entry,) = [e for e in detail["thread"] if e["kind"] == "effective_date"]
        assert (entry["previous_effective_date"], entry["effective_date"], entry["reason"]) == (
            EFFECTIVE.isoformat(),
            LATER.isoformat(),
            "Avenant reçu le 7",
        )
        # The authorised meter now dates from the corrected day.
        row = await _core_row(db_session, world, EAN[0])
        assert (row["regulatory_status"], row["valid_from"]) == (
            int(RegulatoryStatus.AUTHORIZED),
            LATER,
        )
        audited = await db_session.scalar(
            text(
                "SELECT payload->>'previous_effective_date' FROM audit_log "
                "WHERE id_community = :c AND action = :a AND entity_id = :d "
                "AND payload ? 'previous_effective_date'"
            ),
            {"c": world.id_community, "a": AuditActions.DOSSIER_UPDATED, "d": str(dossier_id)},
        )
        assert audited == EFFECTIVE.isoformat()

    async def test_the_daily_job_applies_it_on_the_corrected_day(
        self, client, db_session, admin_storage
    ):
        world = await _world(db_session)
        headers = await _headers(db_session, world)
        dossier_id = await _signed(client, db_session, world)
        assert (await _correct(client, headers, dossier_id, LATER)).status_code == 200

        before = await _day(world, EFFECTIVE)
        assert before.effects_applied == 0
        after = await _day(world, LATER)

        procedure = await _procedure(db_session, dossier_id)
        assert after.effects_applied == 1
        assert (procedure.status, procedure.effective_date) == (
            int(ProcedureStatus.IN_FORCE),
            LATER,
        )
        assert (await _effect(db_session, dossier_id)).status == int(DeadlineStatus.MET)
        assert (await _core_row(db_session, world, EAN[0]))["valid_from"] == LATER

    async def test_without_an_effect_to_come_it_is_a_409(self, client, db_session, admin_storage):
        world = await _world(db_session)
        headers = await _headers(db_session, world)
        draft = await _file(world, "operation.meter_added", EAN[1])
        assert (await _get(client, headers, draft))["effective_date_correctable"] is False

        response = await _correct(client, headers, draft, LATER)

        assert response.status_code == 409
        assert response.json()["error_code"] == 2397

    async def test_once_in_force_it_is_a_409(self, client, db_session, admin_storage):
        world = await _world(db_session)
        headers = await _headers(db_session, world)
        dossier_id = await _signed(client, db_session, world)
        await _day(world, EFFECTIVE)

        response = await _correct(client, headers, dossier_id, LATER)

        assert response.status_code == 409
        assert response.json()["error_code"] == 2397

    async def test_a_reason_is_required(self, client, db_session, admin_storage):
        world = await _world(db_session)
        headers = await _headers(db_session, world)
        dossier_id = await _signed(client, db_session, world)

        response = await _correct(client, headers, dossier_id, LATER, reason="")

        assert response.status_code == 422
