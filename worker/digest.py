"""The summary email, per community, when its admin turned it on (spec,
Notifications; criterion 4.1).

The deadline pass tells each rung of the reminder ladder in-app, one
notification per deadline. Email would multiply that by every deadline and
every community a manager follows; instead a community's admin may turn on a
summary (``digest_setting``, off by default), choose its rhythm (every working
day, a weekday every week or every two weeks, or the month's first such
weekday) and who receives it among the community's admins and managers.

The daily job ends here. A community is due when a scheduled date has come
since the last one it handled (``engine/digest_schedule.py``); its part of
the email tells:

- what is new over its period, read from the database: the procedures put in
  force (by the effects pass or by hand), the answers recorded and the mails
  attached to a procedure to qualify, the deadlines that went missed and the
  third parties found late;
- what stays open: the overdue deadlines and the third parties late;
- what is coming: the community's deadlines due before the next summary, and
  at least the next 14 days.

The period runs from where the last one ended (or from when the summary was
turned on) to the moment the summary is built: what this run recorded is in
it. A community with nothing to tell is skipped; its period moves on all the
same, since it held nothing.

Each recipient gets ONE email (``admin_digest.summary``) with a part per
community due that day, if they are still an admin or a manager of it and did
not turn the summary off in their profile (``admin_digest`` email OFF). The
``dedupe_key`` holds the user and the day, so a second run of the day, or a
crash and a rerun, cannot send a second email.

Labels come from each procedure's own catalogue version, in the four
languages; the template of each locale picks its own. Nothing here commits:
the caller owns both transactions, the CRM's first.
"""

from __future__ import annotations

import datetime
import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
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
from engine.digest_schedule import Schedule, due_on, next_on
from engine.events import BRUSSELS
from engine.jobs import FINISHED
from ports.crm_core_sqlalchemy import SqlAlchemyCrmCoreRead
from shared.const import (
    CorrespondenceDirection,
    DeadlineKind,
    DeadlineParty,
    DeadlineStatus,
    DigestCadence,
    InboundMailStatus,
    ProcedureStatus,
    SubjectType,
)
from shared.models.local_models import (
    CorrespondenceModel,
    DeadlineModel,
    DigestSettingModel,
    DossierModel,
    InboundMailModel,
    ProcedureDefinitionModel,
    StatusEventModel,
)

logger = logging.getLogger(__name__)

#: The preference prefix of the summary (``admin_digest.summary``): a
#: recipient's email OFF there stops it for them.
DIGEST_PREFIX = "admin_digest"
_OFF = 3
#: How far ahead the summary lists what is coming, at least, in calendar days.
HORIZON_DAYS = 14
_LANGUAGES = ("fr", "en", "nl", "de")


@dataclass(frozen=True)
class News:
    """Something new for a procedure of the community: in force (with its
    effect date), or an answer (with its response type's code; None for a
    mail attached to it and not qualified yet)."""

    kind: str  # "in_force" | "answer"
    dossier_id: int
    effective_date: datetime.date | None = None
    response: str | None = None


@dataclass(frozen=True)
class Period:
    """What a community's summary covers: the news in [since, until), what is
    due up to ``horizon``."""

    since: datetime.datetime
    until: datetime.datetime
    horizon: datetime.date


def schedule_of(setting: DigestSettingModel) -> Schedule:
    return Schedule(
        cadence=DigestCadence(setting.cadence),
        weekday=setting.weekday,
        first_on=setting.first_on,
    )


def horizon_of(schedule: Schedule, today: datetime.date) -> datetime.date:
    """Up to the next summary, and at least ``HORIZON_DAYS`` ahead."""
    floor = today + datetime.timedelta(days=HORIZON_DAYS)
    following = next_on(schedule, today)
    return max(floor, following) if following is not None else floor


async def send_digests(
    local: AsyncSession,
    crm: AsyncSession,
    notify: NotificationService,
    communities: Mapping[int, RegulatorPlugin],
    *,
    now: datetime.datetime,
    until: datetime.datetime | None = None,
) -> int:
    """Publish the summaries due today on ``notify`` (the CRM session), and
    move each due community's setting on. Returns how many were published;
    the caller commits, the CRM first, then flushes. ``until`` ends the
    periods: the clock, unless a test holds it."""
    if not communities:
        return 0
    today = brussels_today(now)
    until = until or datetime.datetime.now(datetime.UTC)
    due = await _due_settings(local, communities, today=today)
    if not due:
        return 0
    repository = NotificationRepository(crm)
    names = await _community_names(crm, [setting.id_community for setting, _ in due])
    recipients: dict[int, list[dict[str, Any]]] = {}
    for setting, due_date in due:
        id_community = setting.id_community
        schedule = schedule_of(setting)
        period = Period(
            since=setting.period_from or setting.enabled_at or until,
            until=until,
            horizon=horizon_of(schedule, today),
        )
        # Handled, sent or not: the next period starts here.
        setting.last_due_on = due_date
        setting.period_from = until
        section = await _section(
            local,
            crm,
            id_community,
            communities[id_community],
            name=names.get(id_community),
            period=period,
            today=today,
        )
        if not (section["news"] or section["overdue"] or section["upcoming"]):
            continue
        current = set(
            await repository.find_community_recipient_ids(id_community, roles=MANAGER_ROLES)
        )
        for user_id in setting.recipient_ids:
            if user_id in current:
                recipients.setdefault(user_id, []).append(section)
    await local.flush()
    if not recipients:
        return 0
    off = {
        user_id
        for (user_id, channel), mode in (
            await repository.find_preferences(list(recipients), DIGEST_PREFIX)
        ).items()
        if channel == int(Channel.EMAIL) and mode == _OFF
    }
    sent = 0
    for user_id in sorted(recipients):
        if user_id in off:
            continue
        sections = sorted(recipients[user_id], key=lambda s: (s["name"] or "", s["id"]))
        await notify.publish(
            type=NotificationTypes.ADMIN_DIGEST_SUMMARY,
            target=UserTarget(user_id=user_id),
            # INFORMATIONAL: the recipient may stop it in their profile.
            category=NotificationCategory.INFORMATIONAL,
            channels=(Channel.EMAIL,),
            data={
                "as_of": today.isoformat(),
                "as_of_display": _display(today),
                "counts": {
                    key: sum(len(section[key]) for section in sections)
                    for key in ("news", "overdue", "upcoming")
                },
                "communities": sections,
            },
            # One per user and day, whatever reruns the day.
            dedupe_key=f"{NotificationTypes.ADMIN_DIGEST_SUMMARY}:u{user_id}:{today.isoformat()}",
        )
        sent += 1
    return sent


async def _due_settings(
    local: AsyncSession, communities: Iterable[int], *, today: datetime.date
) -> list[tuple[DigestSettingModel, datetime.date]]:
    """The settings turned on whose scheduled date has come, with that date."""
    rows = await local.scalars(
        select(DigestSettingModel)
        .where(
            DigestSettingModel.id_community.in_(list(communities)),
            DigestSettingModel.enabled.is_(True),
        )
        .order_by(DigestSettingModel.id_community)
    )
    due = []
    for setting in rows:
        if setting.enabled_at is None:
            continue
        on = due_on(
            schedule_of(setting),
            today,
            since=setting.enabled_at,
            last_due_on=setting.last_due_on,
            hour=settings.DEADLINE_SWEEP_HOUR_LOCAL,
        )
        if on is not None:
            due.append((setting, on))
    return due


async def _answers(
    local: AsyncSession,
    id_community: int,
    *,
    since: datetime.datetime,
    until: datetime.datetime,
) -> list[News]:
    """The community's answers in [since, until): the inbound correspondence
    recorded (by hand or from a mail), then the mails attached to a procedure
    and still to qualify (one qualified is its correspondence), each in the
    order it came."""
    recorded = await local.execute(
        select(CorrespondenceModel.id_dossier, CorrespondenceModel.type)
        .where(
            CorrespondenceModel.id_community == id_community,
            CorrespondenceModel.direction == int(CorrespondenceDirection.INBOUND),
            CorrespondenceModel.created_at >= since,
            CorrespondenceModel.created_at < until,
        )
        .order_by(CorrespondenceModel.created_at, CorrespondenceModel.id)
    )
    # Attached by the worker on reception, or by a manager (``handled_at``).
    attached_at = func.coalesce(InboundMailModel.handled_at, InboundMailModel.received_at)
    received = await local.scalars(
        select(InboundMailModel.id_dossier)
        .where(
            InboundMailModel.id_community == id_community,
            InboundMailModel.status == int(InboundMailStatus.ATTACHED),
            InboundMailModel.id_dossier.is_not(None),
            attached_at >= since,
            attached_at < until,
        )
        .order_by(attached_at, InboundMailModel.id)
    )
    return [
        *(
            News("answer", dossier_id=int(dossier_id), response=str(response))
            for dossier_id, response in recorded.tuples()
        ),
        *(News("answer", dossier_id=int(dossier_id)) for dossier_id in received if dossier_id),
    ]


async def _in_force(
    local: AsyncSession,
    id_community: int,
    *,
    since: datetime.datetime,
    until: datetime.datetime,
) -> list[News]:
    """The procedures put in force in [since, until), by the effects pass or
    by hand, as the journal recorded them. Not the baseline, nor what its
    confirmation recorded as already done: the starting state, no news."""
    context = StatusEventModel.context_json
    rows = await local.execute(
        select(StatusEventModel.subject_id, DossierModel.effective_date)
        .join(DossierModel, DossierModel.id == StatusEventModel.subject_id)
        .where(
            StatusEventModel.id_community == id_community,
            StatusEventModel.subject_type == int(SubjectType.DOSSIER),
            StatusEventModel.to_status == int(ProcedureStatus.IN_FORCE),
            StatusEventModel.occurred_at >= since,
            StatusEventModel.occurred_at < until,
            or_(
                context["reason"].as_string() == "effect_date",
                context["manual"].as_boolean().is_(True),
            ),
        )
        .order_by(StatusEventModel.occurred_at, StatusEventModel.id)
    )
    return [
        News("in_force", dossier_id=int(dossier_id), effective_date=effective)
        for dossier_id, effective in rows.tuples()
    ]


async def _community_names(crm: AsyncSession, ids: Iterable[int]) -> dict[int, str]:
    rows = await crm.execute(
        text("SELECT id, name FROM community WHERE id = ANY(:ids)"), {"ids": list(ids)}
    )
    return {int(row.id): str(row.name) for row in rows}


def _within(at: datetime.datetime | None, period: Period) -> bool:
    return at is not None and period.since <= at < period.until


async def _section(
    local: AsyncSession,
    crm: AsyncSession,
    id_community: int,
    plugin: RegulatorPlugin,
    *,
    name: str | None,
    period: Period,
    today: datetime.date,
) -> dict[str, Any]:
    """One community's part of the summary: its news (the deadlines', then the
    procedures'), its overdue and its coming deadlines, each with its labels."""
    community = int(DeadlineParty.COMMUNITY)
    open_, missed = int(DeadlineStatus.OPEN), int(DeadlineStatus.MISSED)
    rows = await local.execute(
        select(DeadlineModel, DossierModel, ProcedureDefinitionModel)
        .join(DossierModel, DossierModel.id == DeadlineModel.id_dossier)
        .join(
            ProcedureDefinitionModel,
            ProcedureDefinitionModel.id == DossierModel.id_procedure_definition,
        )
        .where(
            DeadlineModel.id_community == id_community,
            DeadlineModel.kind != int(DeadlineKind.EFFECT),
            or_(
                and_(
                    DossierModel.status.not_in(FINISHED),
                    or_(
                        DeadlineModel.status == missed,
                        and_(
                            DeadlineModel.status == open_,
                            DeadlineModel.due_date <= period.horizon,
                        ),
                    ),
                ),
                and_(
                    DeadlineModel.status == missed,
                    DeadlineModel.resolved_at >= period.since,
                    DeadlineModel.resolved_at < period.until,
                ),
                and_(
                    func.coalesce(DeadlineModel.party, community) != community,
                    DeadlineModel.reminded_at >= period.since,
                    DeadlineModel.reminded_at < period.until,
                ),
            ),
        )
        .order_by(DeadlineModel.due_date, DeadlineModel.id)
    )
    operations = {
        op.id: op.name
        for op in await SqlAlchemyCrmCoreRead(crm).list_sharing_operations(id_community)
    }
    section: dict[str, Any] = {
        "id": id_community,
        "name": name,
        "since": period.since.isoformat(),
        "since_display": _display(period.since.astimezone(BRUSSELS).date()),
        "until": period.horizon.isoformat(),
        "until_display": _display(period.horizon),
        "news": [],
        "overdue": [],
        "upcoming": [],
    }
    for deadline, dossier, definition_row in rows.tuples():
        entry = plugin.catalogue().get(definition_row.code, definition_row.version)
        definition = entry.definition if entry else None
        item = _item(deadline, dossier, definition, operations, today=today)
        ours = deadline.party in (None, community)
        if deadline.status == missed and _within(deadline.resolved_at, period):
            section["news"].append({**item, "news": "missed"})
        elif not ours and _within(deadline.reminded_at, period):
            section["news"].append({**item, "news": "late"})
        if dossier.status in FINISHED:
            continue
        if deadline.status == missed or (
            not ours and deadline.status == open_ and deadline.due_date < today
        ):
            section["overdue"].append(item)
        elif ours and deadline.status == open_ and deadline.due_date >= today:
            section["upcoming"].append(item)
    news = [
        *await _in_force(local, id_community, since=period.since, until=period.until),
        *await _answers(local, id_community, since=period.since, until=period.until),
    ]
    section["news"].extend(await _procedure_news(local, id_community, plugin, news, operations))
    return section


async def _procedure_news(
    local: AsyncSession,
    id_community: int,
    plugin: RegulatorPlugin,
    news: Sequence[News],
    operations: Mapping[int, str],
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
            DossierModel.id_community == id_community,
            DossierModel.id.in_({n.dossier_id for n in news}),
        )
    )
    procedures = {int(i): (operation, row) for i, operation, row in rows.tuples()}
    items = []
    for n in sorted(news, key=lambda n: n.kind != "in_force"):
        if n.dossier_id not in procedures:
            continue
        operation, definition_row = procedures[n.dossier_id]
        entry = plugin.catalogue().get(definition_row.code, definition_row.version)
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
