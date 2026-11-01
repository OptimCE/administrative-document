"""One summary email per manager per day (spec, Notifications; criterion 4.1).

The deadline pass tells each rung of the reminder ladder in-app, one
notification per deadline. Email would multiply that by every deadline and
every community a manager follows; instead the daily job ends with ONE email
per manager (``admin_digest.summary``), across every community they manage:

- what is new today: the rungs reached (J-14, J-7, J-2), the deadlines that
  went missed, the third parties found late;
- what stays open: the community's overdue deadlines, the third parties late,
  and the deadlines due within 14 days.

Each manager chooses (profile, notification preferences, ``admin_digest``):
daily (the default: sent on a day with news), weekly (Mondays: everything open
and the day's news) or none (``mode`` OFF, which ``publish`` honours anyway).
The ``dedupe_key`` holds the user and the day, so a second run of the day, or
a crash and a rerun, cannot send a second email: at most one per day, whatever
the number of deadlines.

Labels come from each procedure's own catalogue version, in the four
languages; the template of each locale picks its own. Nothing here commits:
the caller owns the CRM transaction.
"""

from __future__ import annotations

import datetime
import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import and_, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from core.notifications import (
    MANAGER_ROLES,
    Channel,
    NotificationCategory,
    NotificationService,
    NotificationTypes,
    UserTarget,
)
from core.notifications.repository import NotificationRepository
from engine.catalogue import ProcedureDefinition
from engine.contracts import RegulatorPlugin
from engine.jobs import FINISHED
from ports.crm_core_sqlalchemy import SqlAlchemyCrmCoreRead
from shared.const import DeadlineKind, DeadlineParty, DeadlineStatus
from shared.models.local_models import DeadlineModel, DossierModel, ProcedureDefinitionModel

logger = logging.getLogger(__name__)

#: The preference prefix of the summary (``admin_digest.summary``).
DIGEST_PREFIX = "admin_digest"
#: ``notification_preference.mode`` for a weekly summary (crm-backend
#: ``PreferenceMode.WEEKLY``); 3 is OFF, anything else daily.
WEEKLY = 4
_OFF = 3
#: How far ahead the summary lists what is coming, in calendar days.
HORIZON_DAYS = 14
_MONDAY = 1
_LANGUAGES = ("fr", "en", "nl", "de")


@dataclass(frozen=True)
class News:
    """Something the day's deadline pass told a community."""

    deadline_id: int
    kind: str  # "reminder" | "missed" | "late"
    rung: int | None = None


@dataclass
class CommunityDay:
    """One community's day, as the daily job leaves it for the summary."""

    id_community: int
    plugin: RegulatorPlugin
    news: list[News] = field(default_factory=list)


def wants_summary(mode: int | None, *, today: datetime.date, has_news: bool) -> bool:
    """Whether a manager gets a summary today, by their ``admin_digest`` email
    preference: weekly on Mondays, none never, daily (the default) when the day
    brought news."""
    if mode == _OFF:
        return False
    if mode == WEEKLY:
        return today.isoweekday() == _MONDAY
    return has_news


async def send_digests(
    local: AsyncSession,
    crm: AsyncSession,
    notify: NotificationService,
    days: Sequence[CommunityDay],
    *,
    today: datetime.date,
) -> int:
    """Publish the day's summaries on ``notify`` (the CRM session). Returns
    how many were published; the caller commits, then flushes."""
    if not days:
        return 0
    by_id = {day.id_community: day for day in days}
    repository = NotificationRepository(crm)
    managers: dict[int, list[int]] = {}
    for id_community in by_id:
        for user_id in await repository.find_community_recipient_ids(
            id_community, roles=MANAGER_ROLES
        ):
            managers.setdefault(user_id, []).append(id_community)
    if not managers:
        return 0
    modes = {
        user_id: mode
        for (user_id, channel), mode in (
            await repository.find_preferences(list(managers), DIGEST_PREFIX)
        ).items()
        if channel == int(Channel.EMAIL)
    }
    names = await _community_names(crm, by_id)
    sections: dict[int, dict[str, Any]] = {}
    sent = 0
    for user_id in sorted(managers):
        mode = modes.get(user_id)
        if mode == _OFF:
            continue
        weekly = mode == WEEKLY
        communities = []
        for id_community in sorted(managers[user_id], key=lambda c: (names.get(c, ""), c)):
            day = by_id[id_community]
            if not weekly and not day.news:
                continue
            if id_community not in sections:
                sections[id_community] = await _section(
                    local, crm, day, name=names.get(id_community), today=today
                )
            section = sections[id_community]
            if section["news"] or section["overdue"] or section["upcoming"]:
                communities.append(section)
        has_news = any(section["news"] for section in communities)
        if not communities or not wants_summary(mode, today=today, has_news=has_news):
            continue
        await notify.publish(
            type=NotificationTypes.ADMIN_DIGEST_SUMMARY,
            target=UserTarget(user_id=user_id),
            # INFORMATIONAL: the manager chose this email, and may stop it.
            category=NotificationCategory.INFORMATIONAL,
            channels=(Channel.EMAIL,),
            data={
                "as_of": today.isoformat(),
                "as_of_display": _display(today),
                "period": "weekly" if weekly else "daily",
                "counts": {
                    key: sum(len(section[key]) for section in communities)
                    for key in ("news", "overdue", "upcoming")
                },
                "communities": communities,
            },
            # One per user and day, whatever reruns the day.
            dedupe_key=f"{NotificationTypes.ADMIN_DIGEST_SUMMARY}:u{user_id}:{today.isoformat()}",
        )
        sent += 1
    return sent


async def _community_names(crm: AsyncSession, ids: Iterable[int]) -> dict[int, str]:
    rows = await crm.execute(
        text("SELECT id, name FROM community WHERE id = ANY(:ids)"), {"ids": list(ids)}
    )
    return {int(row.id): str(row.name) for row in rows}


async def _section(
    local: AsyncSession,
    crm: AsyncSession,
    day: CommunityDay,
    *,
    name: str | None,
    today: datetime.date,
) -> dict[str, Any]:
    """One community's part of the summary: its news, its overdue and its
    coming deadlines, each with its labels."""
    horizon = today + datetime.timedelta(days=HORIZON_DAYS)
    community = int(DeadlineParty.COMMUNITY)
    rows = await local.execute(
        select(DeadlineModel, DossierModel, ProcedureDefinitionModel)
        .join(DossierModel, DossierModel.id == DeadlineModel.id_dossier)
        .join(
            ProcedureDefinitionModel,
            ProcedureDefinitionModel.id == DossierModel.id_procedure_definition,
        )
        .where(
            DeadlineModel.id_community == day.id_community,
            DeadlineModel.kind != int(DeadlineKind.EFFECT),
            DossierModel.status.not_in(FINISHED),
            or_(
                DeadlineModel.status == int(DeadlineStatus.MISSED),
                and_(
                    DeadlineModel.status == int(DeadlineStatus.OPEN),
                    DeadlineModel.due_date <= horizon,
                ),
                DeadlineModel.id.in_([n.deadline_id for n in day.news]),
            ),
        )
        .order_by(DeadlineModel.due_date, DeadlineModel.id)
    )
    operations = {
        op.id: op.name
        for op in await SqlAlchemyCrmCoreRead(crm).list_sharing_operations(day.id_community)
    }
    news_of = {n.deadline_id: n for n in day.news}
    section: dict[str, Any] = {
        "id": day.id_community,
        "name": name,
        "news": [],
        "overdue": [],
        "upcoming": [],
    }
    for deadline, dossier, definition_row in rows.tuples():
        entry = day.plugin.catalogue().get(definition_row.code, definition_row.version)
        definition = entry.definition if entry else None
        item = _item(deadline, dossier, definition, operations, today=today)
        if (news := news_of.get(deadline.id)) is not None:
            section["news"].append({**item, "news": news.kind, "rung": news.rung})
        ours = deadline.party in (None, community)
        if deadline.status == int(DeadlineStatus.MISSED) or (
            not ours and deadline.due_date < today
        ):
            section["overdue"].append(item)
        elif ours and deadline.status == int(DeadlineStatus.OPEN) and deadline.due_date >= today:
            section["upcoming"].append(item)
    return section


def _labels(labels: Mapping[str, str] | None, fallback: str) -> dict[str, str]:
    labels = labels or {}
    return {language: labels.get(language) or fallback for language in _LANGUAGES}


def _item(
    deadline: DeadlineModel,
    dossier: DossierModel,
    definition: ProcedureDefinition | None,
    operations: Mapping[int, str],
    *,
    today: datetime.date,
) -> dict[str, Any]:
    rule = (
        next((r for r in definition.deadline_rules if r.code == deadline.deadline_type), None)
        if definition is not None
        else None
    )
    code = definition.code if definition is not None else ""
    party = DeadlineParty(deadline.party).name.lower() if deadline.party else "community"
    return {
        "deadline_id": deadline.id,
        "dossier_id": dossier.id,
        "path": f"/administrative-document/procedures/{dossier.id}",
        "procedure": _labels(definition.labels if definition else None, code),
        "deadline": _labels(rule.labels if rule else None, deadline.deadline_type),
        "operation": operations.get(dossier.id_sharing_operation or 0),
        "due_date": deadline.due_date.isoformat(),
        "due_display": _display(deadline.due_date),
        "days_left": (deadline.due_date - today).days,
        "party": party,
    }


def _display(day: datetime.date) -> str:
    """dd/mm/yyyy, as Belgian French, Dutch and German write it."""
    return day.strftime("%d/%m/%Y")
