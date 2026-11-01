"""In-app notifications of the procedures, beyond the deadline pass (phase 4).

- ``admin_effect.applied``: a procedure went in force and its effects reached
  the CRM; to the community's managers (spec, "Prise d'effet appliquée").
- ``admin_meter.active``: a member's meter became active in an operation
  through it; to the users linked to that member, in the member portal.
- ``admin_correspondence.recorded``: a manager recorded an answer; to the
  other managers (spec, "Réponse enregistrée").

The worker (the effect job) and the API (a step by hand, an answer) both send
them, so they live here and not in ``api/``, which the worker image does not
ship. Each function stages on the caller's ``NotificationService``: the caller
commits the CRM session, then calls ``flush_realtime``. Reads use plain
columns, never ORM objects a commit may have expired.
"""

from __future__ import annotations

import datetime

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from core.notifications import (
    MANAGER_ROLES,
    Channel,
    CommunityTarget,
    NotificationCategory,
    NotificationService,
    NotificationTypes,
    UsersTarget,
)
from shared.const import ChangeStatus
from shared.models.local_models import ChangeModel, DossierModel, ProcedureDefinitionModel

_ADDITION = "meter_added"


async def _procedure(local: AsyncSession, dossier_id: int) -> tuple[int, int | None, str] | None:
    row = (
        await local.execute(
            select(
                DossierModel.id_community,
                DossierModel.id_sharing_operation,
                ProcedureDefinitionModel.code,
            )
            .join(
                ProcedureDefinitionModel,
                ProcedureDefinitionModel.id == DossierModel.id_procedure_definition,
            )
            .where(DossierModel.id == dossier_id)
        )
    ).first()
    return (int(row[0]), row[1], str(row[2])) if row is not None else None


async def effect_applied(
    notify: NotificationService,
    local: AsyncSession,
    crm: AsyncSession,
    dossier_id: int,
    *,
    effective_date: datetime.date,
) -> None:
    """A procedure in force: its managers are told; each member whose meter it
    made active is told about that meter."""
    procedure = await _procedure(local, dossier_id)
    if procedure is None:
        return
    id_community, id_operation, code = procedure
    await notify.publish(
        type=NotificationTypes.ADMIN_EFFECT_APPLIED,
        target=CommunityTarget(community_id=id_community, roles=MANAGER_ROLES),
        category=NotificationCategory.INFORMATIONAL,
        channels=(Channel.INAPP,),
        data={
            "dossier_id": dossier_id,
            "procedure_code": code,
            "effective_date": effective_date.isoformat(),
        },
    )
    additions = await local.execute(
        select(ChangeModel.subject_ref, ChangeModel.payload_json).where(
            ChangeModel.id_dossier == dossier_id,
            ChangeModel.kind == _ADDITION,
            ChangeModel.status == int(ChangeStatus.APPLIED),
        )
    )
    meters_of: dict[int, list[str]] = {}
    for ean, payload in additions.tuples():
        member = (payload or {}).get("id_member")
        if isinstance(member, int):
            meters_of.setdefault(member, []).append(str(ean))
    if not meters_of:
        return
    users_of = await _users_of_members(crm, id_community, list(meters_of))
    operation = await _operation_name(crm, id_operation)
    for member, eans in sorted(meters_of.items()):
        users = users_of.get(member)
        if not users:
            continue
        for ean in sorted(eans):
            await notify.publish(
                type=NotificationTypes.ADMIN_METER_ACTIVE,
                target=UsersTarget(user_ids=users, community_id=id_community),
                category=NotificationCategory.INFORMATIONAL,
                channels=(Channel.INAPP,),
                data={
                    "ean": ean,
                    "operation": operation,
                    "effective_date": effective_date.isoformat(),
                },
            )


async def correspondence_recorded(
    notify: NotificationService,
    *,
    id_community: int,
    dossier_id: int,
    procedure_code: str,
    response: str,
    actor: str,
) -> None:
    """An answer recorded by hand: the other managers are told."""
    await notify.publish(
        type=NotificationTypes.ADMIN_CORRESPONDENCE_RECORDED,
        target=CommunityTarget(
            community_id=id_community, roles=MANAGER_ROLES, exclude_auth_user_id=actor
        ),
        category=NotificationCategory.INFORMATIONAL,
        channels=(Channel.INAPP,),
        data={"dossier_id": dossier_id, "procedure_code": procedure_code, "response": response},
    )


async def _users_of_members(
    crm: AsyncSession, id_community: int, members: list[int]
) -> dict[int, list[int]]:
    rows = await crm.execute(
        text(
            "SELECT uml.id_member, uml.id_user FROM user_member_link uml "
            "JOIN member m ON m.id = uml.id_member "
            "WHERE m.id_community = :c AND uml.id_member = ANY(:members) "
            "ORDER BY uml.id_member, uml.id_user"
        ),
        {"c": id_community, "members": members},
    )
    users: dict[int, list[int]] = {}
    for member, user in rows.tuples():
        users.setdefault(int(member), []).append(int(user))
    return users


async def _operation_name(crm: AsyncSession, id_operation: int | None) -> str | None:
    if id_operation is None:
        return None
    name = await crm.scalar(
        text("SELECT name FROM sharing_operation WHERE id = :id"), {"id": id_operation}
    )
    return str(name) if name is not None else None
