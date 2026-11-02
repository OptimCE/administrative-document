"""The summary email's setting, per community (phase 4).

Off by default. An admin turns it on and picks its rhythm and its recipients
among the community's admins and managers; every manager reads it. The worker
sends it (``worker/digest.py``) on the dates ``engine/digest_schedule.py``
computes.

Saving moves the schedule's anchors as follows:

- turned on: the rhythm starts now (``enabled_at``), and so does the first
  summary's period (``period_from``); no date before it is sent;
- the rhythm changed while on: it starts now, the period goes on, so the next
  summary still covers everything since the last one;
- turned off, or only the recipients changed: the anchors stay.
"""

from __future__ import annotations

import datetime
from collections.abc import Sequence
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.errors.errors import ErrorException
from core.notifications import MANAGER_ROLES, Channel
from core.notifications.repository import NotificationRepository
from engine.digest_schedule import Schedule, coming_on, first_dates
from shared.const import DigestCadence
from shared.custom_errors import errors
from shared.models.local_models import DigestSettingModel

#: The preference prefix of the summary: a recipient's email OFF stops it.
DIGEST_PREFIX = "admin_digest"
_OFF = 3
_WEEKDAYS = range(1, 8)
CADENCES: dict[str, DigestCadence] = {
    "working_days": DigestCadence.WORKING_DAYS,
    "weekly": DigestCadence.WEEKLY,
    "biweekly": DigestCadence.BIWEEKLY,
    "monthly": DigestCadence.MONTHLY,
}
_NAMES = {cadence: name for name, cadence in CADENCES.items()}
#: What the form starts from while nothing was saved.
_DEFAULT = Schedule(DigestCadence.WEEKLY, weekday=1)


class DigestSettingsService:
    def __init__(self, *, local_session: AsyncSession, crm_session: AsyncSession) -> None:
        self._local = local_session
        self._crm = crm_session

    async def get(self, id_community: int) -> dict[str, Any]:
        row = await self._local.get(DigestSettingModel, id_community)
        return await self._view(id_community, row)

    async def save(
        self,
        id_community: int,
        *,
        enabled: bool,
        cadence: str,
        weekday: int | None,
        first_on: datetime.date | None,
        recipient_ids: Sequence[int],
        actor: str,
    ) -> dict[str, Any]:
        now = self._now()
        row = await self._local.get(DigestSettingModel, id_community)
        schedule = self._schedule(cadence, weekday, first_on, row=row, now=now)
        recipients = sorted(set(recipient_ids))
        if enabled and not recipients:
            raise _invalid()
        if recipients:
            managers = set(
                await NotificationRepository(self._crm).find_community_recipient_ids(
                    id_community, roles=MANAGER_ROLES
                )
            )
            if not managers.issuperset(recipients):
                raise _invalid()
        if row is None:
            row = DigestSettingModel(id_community=id_community, cadence=int(schedule.cadence))
            self._local.add(row)
        was_on = bool(row.enabled)
        changed = (row.cadence, row.weekday, row.first_on) != (
            int(schedule.cadence),
            schedule.weekday,
            schedule.first_on,
        )
        if enabled and not was_on:
            row.enabled_at = now
            row.period_from = now
            row.last_due_on = None
        elif enabled and changed:
            row.enabled_at = now
        row.enabled = enabled
        row.cadence = int(schedule.cadence)
        row.weekday = schedule.weekday
        row.first_on = schedule.first_on
        row.recipient_ids = recipients
        row.updated_at = now
        row.updated_by = actor
        await self._local.commit()
        return await self._view(id_community, row)

    def _schedule(
        self,
        cadence: str,
        weekday: int | None,
        first_on: datetime.date | None,
        *,
        row: DigestSettingModel | None,
        now: datetime.datetime,
    ) -> Schedule:
        """The rhythm asked, checked. Working days take no weekday; every two
        weeks starts on one of the two next dates offered, or keeps the one
        it already had."""
        kind = CADENCES.get(cadence)
        if kind is None:
            raise _invalid()
        if kind == DigestCadence.WORKING_DAYS:
            return Schedule(kind)
        if weekday is None or weekday not in _WEEKDAYS:
            raise _invalid()
        if kind != DigestCadence.BIWEEKLY:
            return Schedule(kind, weekday=weekday)
        kept = (
            row is not None
            and row.cadence == int(kind)
            and row.weekday == weekday
            and row.first_on == first_on
        )
        offered = first_dates(weekday, now=now, hour=settings.DEADLINE_SWEEP_HOUR_LOCAL)
        if first_on is None or not (kept or first_on in offered):
            raise _invalid()
        return Schedule(kind, weekday=weekday, first_on=first_on)

    async def _view(self, id_community: int, row: DigestSettingModel | None) -> dict[str, Any]:
        now = self._now()
        hour = settings.DEADLINE_SWEEP_HOUR_LOCAL
        schedule = (
            Schedule(DigestCadence(row.cadence), weekday=row.weekday, first_on=row.first_on)
            if row is not None
            else _DEFAULT
        )
        recipients = list(row.recipient_ids) if row is not None else []
        next_on = (
            coming_on(
                schedule,
                now=now,
                since=row.enabled_at,
                last_due_on=row.last_due_on,
                hour=hour,
            )
            if row is not None and row.enabled and row.enabled_at is not None
            else None
        )
        return {
            "enabled": bool(row.enabled) if row is not None else False,
            "cadence": _NAMES[schedule.cadence],
            "weekday": schedule.weekday,
            "first_on": schedule.first_on,
            "recipient_ids": recipients,
            "opted_out": await self._opted_out(recipients),
            "next_on": next_on,
            "first_dates": [first_dates(day, now=now, hour=hour) for day in _WEEKDAYS],
            "updated_at": row.updated_at if row is not None else None,
            "updated_by": row.updated_by if row is not None else None,
        }

    async def _opted_out(self, recipients: list[int]) -> list[int]:
        """The recipients who stopped the summary in their profile."""
        if not recipients:
            return []
        modes = await NotificationRepository(self._crm).find_preferences(recipients, DIGEST_PREFIX)
        return sorted(
            user_id
            for (user_id, channel), mode in modes.items()
            if channel == int(Channel.EMAIL) and mode == _OFF
        )

    @staticmethod
    def _now() -> datetime.datetime:
        return datetime.datetime.now(datetime.UTC)


def _invalid() -> ErrorException:
    return ErrorException(errors.admin.DIGEST_SETTING_INVALID, status_code=422)
