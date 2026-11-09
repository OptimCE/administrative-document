"""Open real CWaPE procedures for the tests that need a piece of one.

Every dossier is a procedure of a catalogue now, and every document a piece
the catalogue requires: a test that generates, downloads or files something
opens the procedure that carries that piece, the way a manager does
(``POST /admin/dossiers``), on the root ``community`` fixture (regulator
BE-WAL-CWAPE, module subscribed).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from engine.catalogue_sync import sync_plugin
from regulators.cwape.plugin import CwapePlugin

# A procedure that requires each template, for the tests. Community scope where
# one exists, so no operation is needed.
PROCEDURE_OF = {
    "annex6_notification": "CE_NOTIFICATION",
    "community_notification": "CE_NOTIFICATION",
    "annex7_legal_entity": "CE_NOTIFICATION",
    "annual_participant_list": "CE_RAPPORT_ANNUEL",
}


async def baseline_all_done(world: Any) -> None:
    """Confirm a test world's baseline as a community where everything asked
    about was already done (phase 4: the creation's notification, each
    operation's authorisation), so the world starts with no task."""
    from api.admin.service import AdminService
    from engine.baseline import Choice, ChoiceIn
    from ports.core_effects_sqlalchemy import SqlCoreEffects
    from ports.crm_engine import SqlEngineCrmRead

    service = AdminService(
        local_session=world.session,
        crm_session=world.session,
        crm_read=SqlEngineCrmRead(world.session),
        effects=SqlCoreEffects(world.session),
        registry=world.registry,
        region_of=lambda _code: 1,
    )
    view = await service.baseline(world.id_community)
    done = [
        ChoiceIn(item["key"], Choice.ACTIVE)
        for item in view["items"]
        if item["subject_type"] == "procedure"
    ]
    await service.confirm_baseline(world.id_community, done, actor="test")


async def open_procedure(
    client: Any,
    headers: dict[str, str],
    db_session: AsyncSession,
    *,
    code: str,
    id_sharing_operation: int | None = None,
) -> dict[str, Any]:
    """Create a procedure by hand; return its detail (``ProcedureDetailOut``)."""
    await sync_plugin(db_session, CwapePlugin())
    body: dict[str, Any] = {"code": code}
    if id_sharing_operation is not None:
        body["id_sharing_operation"] = id_sharing_operation
    response = await client.post("/admin/dossiers", json=body, headers=headers)
    assert response.status_code == 200, response.text
    detail: dict[str, Any] = response.json()["data"]
    return detail


async def make_piece(
    client: Any,
    headers: dict[str, str],
    db_session: AsyncSession,
    *,
    template: str = "annex6_notification",
    id_sharing_operation: int | None = None,
) -> tuple[int, int]:
    """A procedure carrying a piece generated from ``template``:
    ``(dossier id, piece id)``."""
    detail = await open_procedure(
        client,
        headers,
        db_session,
        code=PROCEDURE_OF[template],
        id_sharing_operation=id_sharing_operation,
    )
    piece = next((p for p in detail["pieces"] if p["template"] == template), None)
    assert piece is not None, f"{detail['code']} has no {template} piece: {detail['pieces']}"
    return detail["id"], piece["id"]
