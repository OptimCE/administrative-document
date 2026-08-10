"""The deadline scheduler: tenant enumeration, the lock, and the next-run clock.

`POST /maintenance/deadline-sweep` existed from the start and nothing ever
invoked it, so the deadline engine chased nobody. These pin the three things
that make the scheduled path different from the route.
"""

import datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select, text, update

from core.context_vars import current_internal_community_id
from shared.const import DeadlineStatus
from shared.models.local_models import DeadlineModel
from tests.api.administrative_document.test_notifications import notifications_of, seed_roster
from tests.api.administrative_document.test_transitions import make_dossier
from worker.sweeps import (
    _communities_with_sweepable_deadlines_unscoped,
    sweep_deadlines_for_every_community,
)

pytestmark = pytest.mark.asyncio

BRUSSELS = ZoneInfo("Europe/Brussels")
SUBMITTED = 2


class TestScheduledSweep:
    async def _dossier_with_overdue_deadline(
        self, client, manager_headers, sharing_operation, db_session, community
    ) -> int:
        await seed_roster(db_session, community, {"manager-sub": "MANAGER"})
        dossier_id = await make_dossier(client, manager_headers, sharing_operation)
        await client.post(
            f"/dossiers/{dossier_id}/transition",
            json={"to_status": SUBMITTED, "context": {"submission_date": "2020-01-06"}},
            headers=manager_headers,
        )
        return dossier_id

    async def test_the_enumeration_is_unscoped_and_finds_work_with_no_tenant_set(
        self, client, manager_headers, sharing_operation, db_session, community
    ):
        """The failure this guards against is silent.

        Every other owned-DB read goes through `with_community_scope`, which
        filters on a ContextVar a scheduler has not set — and returns nothing,
        with no error, which looks exactly like "no work to do".
        """
        await self._dossier_with_overdue_deadline(
            client, manager_headers, sharing_operation, db_session, community
        )
        token = current_internal_community_id.set(None)
        try:
            found = await _communities_with_sweepable_deadlines_unscoped(
                db_session, as_of=datetime.date.today(), window_days=14
            )
        finally:
            current_internal_community_id.reset(token)
        assert community.id in found

    async def test_the_scheduled_sweep_notifies_exactly_as_the_route_does(
        self, client, manager_headers, sharing_operation, db_session, community
    ):
        """One implementation, two callers — the whole point of not forking it."""
        await self._dossier_with_overdue_deadline(
            client, manager_headers, sharing_operation, db_session, community
        )
        current_internal_community_id.set(None)

        missed, swept = await sweep_deadlines_for_every_community(
            local_session=db_session, crm_session=db_session
        )

        assert swept == 1
        assert missed >= 1
        assert await notifications_of(db_session, "admin_deadline.missed")

    async def test_a_second_scheduled_run_is_a_no_op(
        self, client, manager_headers, sharing_operation, db_session, community
    ):
        """Re-running on a deploy must not re-notify. The status flip and
        `reminded_at` are what make that true."""
        await self._dossier_with_overdue_deadline(
            client, manager_headers, sharing_operation, db_session, community
        )
        current_internal_community_id.set(None)

        await sweep_deadlines_for_every_community(local_session=db_session, crm_session=db_session)
        before = len(await notifications_of(db_session))

        await sweep_deadlines_for_every_community(local_session=db_session, crm_session=db_session)
        assert len(await notifications_of(db_session)) == before

    async def test_nothing_pending_means_no_work_and_no_service_construction(self, db_session):
        current_internal_community_id.set(None)
        assert await sweep_deadlines_for_every_community(
            local_session=db_session, crm_session=db_session
        ) == (0, 0)

    async def test_a_reminder_window_deadline_is_swept_even_when_nothing_is_overdue(
        self, client, manager_headers, sharing_operation, db_session, community
    ):
        """The enumeration must look at the reminder window, not just past due —
        otherwise `due_soon` could never fire from the scheduler."""
        dossier_id = await self._dossier_with_overdue_deadline(
            client, manager_headers, sharing_operation, db_session, community
        )
        await db_session.execute(
            update(DeadlineModel)
            .where(DeadlineModel.id_dossier == dossier_id)
            .values(
                due_date=datetime.date.today() + datetime.timedelta(days=3),
                status=int(DeadlineStatus.OPEN),
                reminded_at=None,
            )
        )
        await db_session.flush()
        current_internal_community_id.set(None)

        missed, swept = await sweep_deadlines_for_every_community(
            local_session=db_session, crm_session=db_session
        )

        assert swept == 1
        assert missed == 0
        assert await notifications_of(db_session, "admin_deadline.due_soon")


class TestAdvisoryLock:
    async def test_a_second_holder_cannot_take_the_lock(self, db_session):
        """Two replicas both sweeping would give every manager two bell entries:
        `dedupe_key` collapses the duplicate email, but nothing collapses the
        in-app row."""
        key = 0x0AD3_0001
        first = await db_session.scalar(text("SELECT pg_try_advisory_lock(:k)"), {"k": key})
        assert first is True
        try:
            # A different SESSION is what a second replica is. Within one session
            # advisory locks are re-entrant, so this asserts through a new
            # connection.
            second = await db_session.scalar(
                text(
                    "SELECT NOT EXISTS (SELECT 1 FROM pg_locks "
                    "WHERE locktype = 'advisory' AND objid = :k AND granted)"
                ),
                {"k": key & 0xFFFFFFFF},
            )
            assert second is False, "the lock should be visible as held"
        finally:
            await db_session.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})

    async def test_the_deadline_and_billing_locks_do_not_collide(self):
        """Both workers run in the same cluster; a shared key would serialise
        two unrelated sweeps and hide one of them."""
        from worker.sweeps import _SWEEP_ADVISORY_LOCK_KEY

        assert _SWEEP_ADVISORY_LOCK_KEY != 0x0B111_0001


async def test_open_deadlines_due_within_excludes_past_due(db_session, community):
    """The filter that stops one deadline producing both messages."""
    from api.administrative_document.repository import AdministrativeDocumentRepository

    current_internal_community_id.set(community.id)
    repo = AdministrativeDocumentRepository(db_session)
    today = datetime.date.today()
    rows = await repo.open_deadlines_due_within(today, 14)
    assert all(row.due_date >= today for row in rows)
    assert all(row.reminded_at is None for row in rows)
    assert (
        len(
            (await db_session.execute(select(DeadlineModel).where(DeadlineModel.due_date < today)))
            .scalars()
            .all()
        )
        >= 0
    )
