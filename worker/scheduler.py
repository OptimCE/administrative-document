"""A once-a-day tick for the deadline sweep.

Fires at a fixed LOCAL time rather than "every N hours from process start". A
from-start interval re-runs the sweep on every deploy, and the sweep is a
user-visible event: `admin_deadline.missed` is TRANSACTIONAL and reaches every
manager's inbox. `reminded_at` and the MISSED status flip make a re-run
harmless, so this is hygiene rather than correctness — but "one email a day"
versus "an email every deploy" is the difference between a useful reminder and
one people filter.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import logging
from zoneinfo import ZoneInfo

from core.config import settings
from worker.sweeps import try_sweep_deadlines

logger = logging.getLogger(__name__)

BRUSSELS = ZoneInfo("Europe/Brussels")


def seconds_until_next_run(now: datetime.datetime, hour_local: int) -> float:
    """Seconds from ``now`` to the next occurrence of ``hour_local``.

    Pure, so the wrap-around and DST cases are unit-testable without waiting a
    day. ``now`` must be timezone-aware.
    """
    local_now = now.astimezone(BRUSSELS)
    target = local_now.replace(hour=hour_local, minute=0, second=0, microsecond=0)
    if target <= local_now:
        target += datetime.timedelta(days=1)
    return (target - local_now).total_seconds()


async def run_deadline_scheduler(shutdown: asyncio.Event) -> None:
    """Sleep until the next scheduled hour, sweep, repeat, until shutdown."""
    if not settings.DEADLINE_SWEEP_ENABLED:
        logger.info("deadline scheduler disabled")
        return
    while not shutdown.is_set():
        delay = seconds_until_next_run(
            datetime.datetime.now(datetime.UTC), settings.DEADLINE_SWEEP_HOUR_LOCAL
        )
        logger.info("next deadline sweep in %.0fs", delay)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(shutdown.wait(), timeout=delay)
        if shutdown.is_set():
            return
        try:
            await try_sweep_deadlines()
        except Exception:
            # One failed sweep must not take the worker down: the next tick
            # retries, and the sweep is idempotent by construction.
            logger.exception("deadline sweep failed")
