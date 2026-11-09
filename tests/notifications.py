"""Helpers for the tests that read the notifications this service writes into
the shared CRM ``notification`` table (the daily jobs' reminders)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.crm_models import NotificationModel


async def seed_roster(
    db_session: AsyncSession, community: Any, roster: dict[str, str]
) -> dict[str, int]:
    """Create an ``app_user`` + ``community_user`` row per ``{auth_id: role}``."""
    ids: dict[str, int] = {}
    for auth_id, role in roster.items():
        result = await db_session.execute(
            text("INSERT INTO app_user (auth_user_id, email) VALUES (:a, :e) RETURNING id"),
            {"a": auth_id, "e": f"{auth_id}@example.be"},
        )
        uid = int(result.scalar_one())
        await db_session.execute(
            text("INSERT INTO community_user (id_community, id_user, role) VALUES (:c, :u, :r)"),
            {"c": community.id, "u": uid, "r": role},
        )
        ids[auth_id] = uid
    return ids


async def notifications_of(
    db_session: AsyncSession, type_: str | None = None
) -> list[NotificationModel]:
    stmt = select(NotificationModel).order_by(NotificationModel.id)
    if type_ is not None:
        stmt = stmt.where(NotificationModel.type == type_)
    return list((await db_session.execute(stmt)).scalars().all())
