"""One summary email per manager per day (spec, Notifications; criterion 4.1).

The deadline pass tells each rung of the reminder ladder in-app, one
notification per deadline. Email would multiply that by every deadline and
every community a manager follows; instead the daily job ends with ONE email
per manager (``admin_digest.summary``), across every community they manage:

- what is new today: the rungs reached (J-14, J-7, J-2), the deadlines that
  went missed, the third parties found late (the deadline pass), the
  procedures the effects pass put in force, and the answers of the day;
- what stays open: the community's overdue deadlines, the third parties late,
  and the deadlines due within 14 days.

The answers are read here, when the summary is built, not told by the jobs: a
manager records most of them through the API. They are the inbound
correspondence created since yesterday's scheduled run
(``DEADLINE_SWEEP_HOUR_LOCAL``, Brussels) up to this run, and the mails the
reception address attached to a procedure in that time that nobody has
qualified yet (one qualified is its correspondence). Windows that start at
each tick lose nothing between two runs; a late run (a catch-up) tells what
came since the tick, which the next morning may tell again.

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
from dataclasses import dataclass, field, replace
from typing import Any

from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.notifications import (
    MANAGER_ROLES,
    Channel,
    NotificationCategory,
    NotificationService,
    NotificationTypes,
    UserTarget,
)
from core.notifications.repository import NotificationRepository
from engine.baseline import brussels_today
from engine.catalogue import ProcedureDefinition
from engine.contracts import RegulatorPlugin
from engine.events import BRUSSELS
from engine.jobs import FINISHED
from ports.crm_core_sqlalchemy import SqlAlchemyCrmCoreRead
from shared.const import (
    CorrespondenceDirection,
    DeadlineKind,
    DeadlineParty,
    DeadlineStatus,
    InboundMailStatus,
)
from shared.models.local_models import (
    CorrespondenceModel,
    DeadlineModel,
    DossierModel,
    InboundMailModel,
    ProcedureDefinitionModel,
)

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
    """Something new for a community today. A deadline the day's pass told
    (``deadline_id``: reminder, with its rung; missed; late), or a procedure
    (``dossier_id``) that went in force (``in_force``, with its effect date)
    or got an answer (``answer``, with its response type's code; None for a
    mail attached to it and not qualified yet)."""

    kind: str  # "reminder" | "missed" | "late" | "in_force" | "answer"
    deadline_id: int | None = None
    rung: int | None = None
    dossier_id: int | None = None
    effective_date: datetime.date | None = None
    response: str | None = None


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
    now: datetime.datetime,
) -> int:
    """Publish the day's summaries on ``notify`` (the CRM session). Returns
    how many were published; the caller commits, then flushes."""
    if not days:
        return 0
    today = brussels_today(now)
    answers = await _answers(
        local, [day.id_community for day in days], since=answers_since(today), until=now
    )
    by_id = {
        day.id_community: replace(day, news=[*day.news, *answers.get(day.id_community, [])])
        for day in days
    }
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


def answers_since(today: datetime.date) -> datetime.datetime:
    """Where the day's answers start: yesterday's scheduled run."""
    return datetime.datetime.combine(
        today - datetime.timedelta(days=1),
        datetime.time(settings.DEADLINE_SWEEP_HOUR_LOCAL),
        BRUSSELS,
    )


async def _answers(
    local: AsyncSession,
    communities: Sequence[int],
    *,
    since: datetime.datetime,
    until: datetime.datetime,
) -> dict[int, list[News]]:
    """Each community's answers in [since, until): the inbound correspondence
    recorded (by hand or from a mail), then the mails attached to a procedure
    and still to qualify, each in the order it came."""
    recorded = await local.execute(
        select(
            CorrespondenceModel.id_community,
            CorrespondenceModel.id_dossier,
            CorrespondenceModel.type,
        )
        .where(
            CorrespondenceModel.id_community.in_(communities),
            CorrespondenceModel.direction == int(CorrespondenceDirection.INBOUND),
            CorrespondenceModel.created_at >= since,
            CorrespondenceModel.created_at < until,
        )
        .order_by(CorrespondenceModel.created_at, CorrespondenceModel.id)
    )
    # Attached by the worker on reception, or by a manager (``handled_at``).
    attached_at = func.coalesce(InboundMailModel.handled_at, InboundMailModel.received_at)
    received = await local.execute(
        select(InboundMailModel.id_community, InboundMailModel.id_dossier)
        .where(
            InboundMailModel.id_community.in_(communities),
            InboundMailModel.status == int(InboundMailStatus.ATTACHED),
            InboundMailModel.id_dossier.is_not(None),
            attached_at >= since,
            attached_at < until,
        )
        .order_by(attached_at, InboundMailModel.id)
    )
    answers: dict[int, list[News]] = {}
    for id_community, dossier_id, response in recorded.tuples():
        answers.setdefault(int(id_community), []).append(
            News("answer", dossier_id=int(dossier_id), response=str(response))
        )
    for community, attached_to in received.tuples():
        answers.setdefault(int(community), []).append(News("answer", dossier_id=attached_to))
    return answers


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
    """One community's part of the summary: its news (the deadlines', then the
    procedures'), its overdue and its coming deadlines, each with its labels."""
    horizon = today + datetime.timedelta(days=HORIZON_DAYS)
    community = int(DeadlineParty.COMMUNITY)
    news_of = {n.deadline_id: n for n in day.news if n.deadline_id is not None}
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
                DeadlineModel.id.in_(list(news_of)),
            ),
        )
        .order_by(DeadlineModel.due_date, DeadlineModel.id)
    )
    operations = {
        op.id: op.name
        for op in await SqlAlchemyCrmCoreRead(crm).list_sharing_operations(day.id_community)
    }
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
    section["news"].extend(
        await _procedure_news(
            local, day, [n for n in day.news if n.dossier_id is not None], operations
        )
    )
    return section


async def _procedure_news(
    local: AsyncSession, day: CommunityDay, news: list[News], operations: Mapping[int, str]
) -> list[dict[str, Any]]:
    """The procedures' news, in force first, then the answers, each with the
    labels of the procedure's own catalogue version. Finished or not: in force
    is finished."""
    if not news:
        return []
    rows = await local.execute(
        select(DossierModel.id, DossierModel.id_sharing_operation, ProcedureDefinitionModel)
        .join(
            ProcedureDefinitionModel,
            ProcedureDefinitionModel.id == DossierModel.id_procedure_definition,
        )
        .where(
            DossierModel.id_community == day.id_community,
            DossierModel.id.in_({n.dossier_id for n in news}),
        )
    )
    procedures = {int(i): (operation, row) for i, operation, row in rows.tuples()}
    items = []
    for n in sorted(news, key=lambda n: n.kind != "in_force"):
        if n.dossier_id not in procedures:
            continue
        operation, definition_row = procedures[n.dossier_id]
        entry = day.plugin.catalogue().get(definition_row.code, definition_row.version)
        definition = entry.definition if entry else None
        response = (
            next((r for r in definition.response_types if r.code == n.response), None)
            if definition is not None and n.response is not None
            else None
        )
        items.append(
            {
                "dossier_id": n.dossier_id,
                "path": f"/administrative-document/procedures/{n.dossier_id}",
                "procedure": _labels(
                    definition.labels if definition else None, definition_row.code
                ),
                "operation": operations.get(operation or 0),
                "news": n.kind,
                "effective_date": n.effective_date.isoformat() if n.effective_date else None,
                "effective_display": _display(n.effective_date) if n.effective_date else None,
                "response": (
                    _labels(response.labels if response else None, n.response)
                    if n.response is not None
                    else None
                ),
            }
        )
    return items


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
