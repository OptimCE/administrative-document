"""Notifications this service writes into the shared CRM ``notification`` table.

Three producers: ``admin_dossier.acknowledged`` on the acknowledge edge, and
``admin_deadline.missed`` + ``admin_deadline.due_soon`` from the deadline sweep.
All target the community's managers, ride on the caller's CRM session, and are
staged before the CRM commit so they share one transaction with the audit row.

``get_crm_session`` and ``get_local_session`` both resolve to ``db_session`` in
tests, so rows are visible without a commit.
"""

import datetime

import pytest
from sqlalchemy import select, text, update

from core.notifications import Channel, NotificationCategory
from core.notifications.repository import NotificationRepository
from core.notifications.service import EmailRecipient, NotificationService
from shared.const import DeadlineStatus
from shared.models.crm_models import NotificationModel
from shared.models.local_models import DeadlineModel, StatusEventModel
from tests.api.administrative_document.test_transitions import make_document, make_dossier

pytestmark = pytest.mark.asyncio

SUBMITTED = 2
COMPLETE = 3


async def seed_roster(db_session, community, roster: dict[str, str]) -> dict[str, int]:
    """Create an ``app_user`` + ``community_user`` row per ``{auth_id: role}``."""
    ids: dict[str, int] = {}
    for auth_id, role in roster.items():
        result = await db_session.execute(
            text("INSERT INTO app_user (auth_user_id, email) VALUES (:a, :e) RETURNING id"),
            {"a": auth_id, "e": f"{auth_id}@example.be"},
        )
        uid = int(result.scalar_one())
        await db_session.execute(
            text("INSERT INTO community_user (id_community, id_user, role) " "VALUES (:c, :u, :r)"),
            {"c": community.id, "u": uid, "r": role},
        )
        ids[auth_id] = uid
    return ids


async def notifications_of(db_session, type_: str | None = None) -> list[NotificationModel]:
    stmt = select(NotificationModel).order_by(NotificationModel.id)
    if type_ is not None:
        stmt = stmt.where(NotificationModel.type == type_)
    return list((await db_session.execute(stmt)).scalars().all())


async def acknowledge(client, headers, document_id: int, result: str = "complete"):
    await client.post(f"/documents/{document_id}/mark-ready", headers=headers)
    await client.post(
        f"/documents/{document_id}/mark-sent",
        json={"submission_date": "2026-09-07"},
        headers=headers,
    )
    return await client.post(
        f"/documents/{document_id}/acknowledge",
        json={
            "acknowledged_date": "2026-09-15",
            "authority_file_ref": "CWAPE-2026-0042",
            "result": result,
        },
        headers=headers,
    )


ROSTER = {"admin-sub": "ADMIN", "manager-sub": "MANAGER", "member-sub": "MEMBER"}


class TestDossierAcknowledged:
    async def test_acknowledging_a_document_notifies_managers(
        self, client, manager_headers, sharing_operation, db_session, community
    ):
        users = await seed_roster(db_session, community, ROSTER)
        dossier_id = await make_dossier(client, manager_headers, sharing_operation)
        document_id = await make_document(client, manager_headers, dossier_id)

        response = await acknowledge(client, manager_headers, document_id)
        assert response.status_code == 200, response.text

        rows = await notifications_of(db_session, "admin_dossier.acknowledged")
        # ADMIN outranks MANAGER, so both are "community managers"; a plain
        # MEMBER is not.
        assert {n.id_user for n in rows} == {users["admin-sub"], users["manager-sub"]}
        assert all(n.id_community == community.id for n in rows)
        assert all(
            n.data
            == {
                "document_id": document_id,
                "dossier_id": dossier_id,
                "dossier_type": 1,
                "result": "complete",
            }
            for n in rows
        )

    async def test_other_transitions_do_not_notify(
        self, client, manager_headers, sharing_operation, db_session, community
    ):
        """_transition is the funnel for every status change — guard it."""
        await seed_roster(db_session, community, ROSTER)
        dossier_id = await make_dossier(client, manager_headers, sharing_operation)
        document_id = await make_document(client, manager_headers, dossier_id)

        await client.post(f"/documents/{document_id}/mark-ready", headers=manager_headers)
        await client.post(
            f"/dossiers/{dossier_id}/transition",
            json={"to_status": SUBMITTED, "context": {"submission_date": "2026-09-07"}},
            headers=manager_headers,
        )

        assert await notifications_of(db_session) == []

    async def test_acknowledged_is_in_app_only(
        self, client, manager_headers, sharing_operation, db_session, community, monkeypatch
    ):
        """The step-3 email seam must not be reached for an INAPP-only type."""
        await seed_roster(db_session, community, {"manager-sub": "MANAGER"})
        dossier_id = await make_dossier(client, manager_headers, sharing_operation)
        document_id = await make_document(client, manager_headers, dossier_id)

        calls: list[dict[str, object]] = []

        async def _spy(self: NotificationService, **kwargs: object) -> None:
            calls.append(kwargs)

        monkeypatch.setattr(NotificationService, "_enqueue_email", _spy)

        await acknowledge(client, manager_headers, document_id)
        assert len(await notifications_of(db_session, "admin_dossier.acknowledged")) == 1
        assert calls == []

    async def test_notification_failure_does_not_abort_the_transition(
        self, client, manager_headers, sharing_operation, db_session, community, monkeypatch
    ):
        """The SAVEPOINT is what protects the transition, not the swallow.

        The journal row and the deferred constraint trigger are the invariants
        this service is built on; a failed notification must leave both intact.
        """
        await seed_roster(db_session, community, {"manager-sub": "MANAGER"})
        dossier_id = await make_dossier(client, manager_headers, sharing_operation)
        document_id = await make_document(client, manager_headers, dossier_id)

        async def _boom(self: NotificationRepository, rows: list[NotificationModel]) -> None:
            raise RuntimeError("notification store unavailable")

        monkeypatch.setattr(NotificationRepository, "insert_many", _boom)

        response = await acknowledge(client, manager_headers, document_id)
        assert response.status_code == 200, response.text
        assert response.json()["data"]["status"] == 4  # ACKNOWLEDGED

        # The append-only journal recorded the transition ...
        events = await db_session.execute(
            select(StatusEventModel.to_status).where(
                StatusEventModel.subject_id == document_id,
                StatusEventModel.subject_type == 1,  # SubjectType.DOCUMENT
            )
        )
        assert 4 in list(events.scalars().all())
        # ... and the rolled-back savepoint left nothing behind.
        assert await notifications_of(db_session) == []


class TestDeadlineMissed:
    async def test_sweep_notifies_one_row_per_missed_deadline(
        self, client, manager_headers, sharing_operation, db_session, community
    ):
        users = await seed_roster(db_session, community, {"manager-sub": "MANAGER"})
        dossier_id = await make_dossier(client, manager_headers, sharing_operation)
        # A submission far in the past leaves an already-overdue deadline.
        await client.post(
            f"/dossiers/{dossier_id}/transition",
            json={"to_status": SUBMITTED, "context": {"submission_date": "2020-01-06"}},
            headers=manager_headers,
        )

        response = await client.post("/maintenance/deadline-sweep", headers=manager_headers)
        assert response.status_code == 200, response.text
        missed = response.json()["data"]["missed"]
        assert missed >= 1

        rows = await notifications_of(db_session, "admin_deadline.missed")
        assert len(rows) == missed
        assert {n.id_user for n in rows} == {users["manager-sub"]}
        for n in rows:
            assert n.id_community == community.id
            assert n.data["dossier_id"] == dossier_id
            # A date has to be stringified for JSONB: json.dumps raises on
            # datetime.date, and that raise happens inside publish's savepoint
            # and its blanket except, so it would silently drop the row.
            assert isinstance(n.data["due_date"], str)
            assert isinstance(n.data["deadline_type"], str)

    async def test_second_sweep_notifies_nobody(
        self, client, manager_headers, sharing_operation, db_session, community
    ):
        """The rows are already MISSED, so a re-run matches nothing."""
        await seed_roster(db_session, community, {"manager-sub": "MANAGER"})
        dossier_id = await make_dossier(client, manager_headers, sharing_operation)
        await client.post(
            f"/dossiers/{dossier_id}/transition",
            json={"to_status": SUBMITTED, "context": {"submission_date": "2020-01-06"}},
            headers=manager_headers,
        )
        first = await client.post("/maintenance/deadline-sweep", headers=manager_headers)
        before = len(await notifications_of(db_session, "admin_deadline.missed"))
        assert before == first.json()["data"]["missed"]

        second = await client.post("/maintenance/deadline-sweep", headers=manager_headers)
        assert second.json()["data"]["missed"] == 0
        assert len(await notifications_of(db_session, "admin_deadline.missed")) == before

    async def test_missed_requests_the_email_channel(
        self, client, manager_headers, sharing_operation, db_session, community, monkeypatch
    ):
        """`_enqueue_email` is the step-3 seam: reached, resolved, does nothing.

        The non-None `id_notification` proves the in-app rows were flushed first,
        which is what step 3's `outbound_message.id_notification` needs.
        """
        await seed_roster(db_session, community, {"manager-sub": "MANAGER"})
        dossier_id = await make_dossier(client, manager_headers, sharing_operation)
        await client.post(
            f"/dossiers/{dossier_id}/transition",
            json={"to_status": SUBMITTED, "context": {"submission_date": "2020-01-06"}},
            headers=manager_headers,
        )

        calls: list[dict[str, object]] = []

        async def _spy(self: NotificationService, **kwargs: object) -> None:
            calls.append(kwargs)

        monkeypatch.setattr(NotificationService, "_enqueue_email", _spy)
        await client.post("/maintenance/deadline-sweep", headers=manager_headers)

        assert calls
        assert all(c["type"] == "admin_deadline.missed" for c in calls)
        assert all(c["category"] is NotificationCategory.TRANSACTIONAL for c in calls)
        recipients = [r for c in calls for r in c["recipients"]]
        assert recipients
        assert all(
            isinstance(r, EmailRecipient) and r.id_notification is not None for r in recipients
        )


class TestDeadlineDueSoon:
    """The reminder that fires BEFORE a deadline is missed.

    This was the tripwire `test_no_due_soon_notifications_yet` guarded: the type
    was deferred until the delivery layer could make a re-run sweep idempotent,
    because a reminder that re-sends every day is worse than no reminder.
    """

    async def _dossier_with_upcoming_deadline(
        self, client, manager_headers, sharing_operation, db_session, community, *, days: int = 5
    ) -> int:
        """A submitted dossier whose open deadline falls `days` from today."""
        await seed_roster(db_session, community, {"manager-sub": "MANAGER"})
        dossier_id = await make_dossier(client, manager_headers, sharing_operation)
        await client.post(
            f"/dossiers/{dossier_id}/transition",
            json={"to_status": SUBMITTED, "context": {"submission_date": "2020-01-06"}},
            headers=manager_headers,
        )
        # The transition derives real deadlines from a 2020 submission date, so
        # they are all long past due. Pull them into the reminder window instead.
        target = datetime.date.today() + datetime.timedelta(days=days)
        await db_session.execute(
            update(DeadlineModel)
            .where(DeadlineModel.id_dossier == dossier_id)
            .values(due_date=target, status=int(DeadlineStatus.OPEN), reminded_at=None)
        )
        await db_session.flush()
        return dossier_id

    async def test_a_deadline_inside_the_window_reminds_the_managers(
        self, client, manager_headers, sharing_operation, db_session, community
    ):
        await self._dossier_with_upcoming_deadline(
            client, manager_headers, sharing_operation, db_session, community
        )

        await client.post("/maintenance/deadline-sweep", headers=manager_headers)

        reminders = await notifications_of(db_session, "admin_deadline.due_soon")
        assert reminders
        for reminder in reminders:
            assert set(reminder.data) == {
                "deadline_id",
                "dossier_id",
                "deadline_type",
                "due_date",
                "as_of",
            }
            # Both dates are stringified: json.dumps raises on datetime.date, and
            # that raise happens inside publish's savepoint and its blanket
            # except, so it would silently drop the row behind a 200.
            assert isinstance(reminder.data["due_date"], str)
            assert isinstance(reminder.data["as_of"], str)

    async def test_a_second_sweep_reminds_nobody_again(
        self, client, manager_headers, sharing_operation, db_session, community
    ):
        """`reminded_at` is what makes a DAILY sweep safe.

        `outbound_message.dedupe_key` protects the email, but nothing protects
        the in-app notification — without the marker a manager would collect one
        bell entry per day until the due date.
        """
        await self._dossier_with_upcoming_deadline(
            client, manager_headers, sharing_operation, db_session, community
        )
        await client.post("/maintenance/deadline-sweep", headers=manager_headers)
        first = len(await notifications_of(db_session, "admin_deadline.due_soon"))
        assert first

        await client.post("/maintenance/deadline-sweep", headers=manager_headers)
        assert len(await notifications_of(db_session, "admin_deadline.due_soon")) == first

    async def test_a_deadline_beyond_the_window_is_left_alone(
        self, client, manager_headers, sharing_operation, db_session, community
    ):
        await self._dossier_with_upcoming_deadline(
            client, manager_headers, sharing_operation, db_session, community, days=365
        )
        await client.post("/maintenance/deadline-sweep", headers=manager_headers)
        assert await notifications_of(db_session, "admin_deadline.due_soon") == []

    async def test_a_past_due_deadline_is_missed_not_reminded(
        self, client, manager_headers, sharing_operation, db_session, community
    ):
        """One message, not two.

        The MISSED pass runs first and the reminder query filters on
        `due_date >= as_of`, so a deadline that is both past due and nominally
        "within N days" cannot collect both.
        """
        await self._dossier_with_upcoming_deadline(
            client, manager_headers, sharing_operation, db_session, community, days=-1
        )
        await client.post("/maintenance/deadline-sweep", headers=manager_headers)

        assert await notifications_of(db_session, "admin_deadline.missed")
        assert await notifications_of(db_session, "admin_deadline.due_soon") == []

    async def test_the_reminder_is_informational_and_carries_an_explicit_dedupe_key(
        self, client, manager_headers, sharing_operation, db_session, community, monkeypatch
    ):
        """The only producer in the platform that passes its own dedupe key.

        This sweep does not mutate what it reads, so the derived key — a hash of
        `data` — could not tell two occurrences of the same deadline apart. The
        occurrence date has to be in the key.
        """
        await self._dossier_with_upcoming_deadline(
            client, manager_headers, sharing_operation, db_session, community
        )
        calls: list[dict[str, object]] = []
        original = NotificationService.publish

        async def _spy(self: NotificationService, **kwargs: object) -> int:
            calls.append(kwargs)
            return int(await original(self, **kwargs))

        monkeypatch.setattr(NotificationService, "publish", _spy)
        await client.post("/maintenance/deadline-sweep", headers=manager_headers)

        reminders = [c for c in calls if c["type"] == "admin_deadline.due_soon"]
        assert reminders
        for call in reminders:
            # A reminder must be mutable by its recipient (§1.6); `missed` must
            # not be.
            assert call["category"] is NotificationCategory.INFORMATIONAL
            assert Channel.EMAIL in call["channels"]
            key = call["dedupe_key"]
            assert isinstance(key, str)
            assert key.startswith("admin_deadline.due_soon:")
            # The occurrence date, so tomorrow's rolled deadline is a new message.
            assert key.count(":") == 2

    async def test_a_rolled_recurring_occurrence_starts_unreminded(
        self, client, manager_headers, sharing_operation, db_session, community
    ):
        """Pins the invariant `reminded_at` depends on.

        A rolled occurrence is a fresh INSERT, and there is no UPDATE path for
        `due_date` anywhere in the service — so a stale marker can never suppress
        a legitimately rescheduled reminder.
        """
        await seed_roster(db_session, community, {"manager-sub": "MANAGER"})
        # Completing a dossier is what schedules the genuinely recurring
        # `annual_report` obligation, with a rule that carries recur_months.
        dossier_id = await make_dossier(client, manager_headers, sharing_operation)
        await client.post(
            f"/dossiers/{dossier_id}/transition",
            json={"to_status": SUBMITTED, "context": {"submission_date": "2026-01-06"}},
            headers=manager_headers,
        )
        await client.post(
            f"/dossiers/{dossier_id}/transition",
            json={"to_status": COMPLETE, "context": {}},
            headers=manager_headers,
        )
        # Force it past due AND already reminded, so the sweep rolls it.
        await db_session.execute(
            update(DeadlineModel)
            .where(
                DeadlineModel.id_dossier == dossier_id,
                DeadlineModel.deadline_type == "annual_report",
            )
            .values(
                due_date=datetime.date.today() - datetime.timedelta(days=1),
                reminded_at=datetime.datetime.now(datetime.UTC),
            )
        )
        await db_session.flush()

        await client.post("/maintenance/deadline-sweep", headers=manager_headers)

        rolled = (
            (
                await db_session.execute(
                    select(DeadlineModel).where(
                        DeadlineModel.id_dossier == dossier_id,
                        DeadlineModel.deadline_type == "annual_report",
                        DeadlineModel.status == int(DeadlineStatus.OPEN),
                    )
                )
            )
            .scalars()
            .all()
        )
        assert rolled, "the recurring obligation should have rolled to a new occurrence"
        assert all(row.reminded_at is None for row in rolled)
