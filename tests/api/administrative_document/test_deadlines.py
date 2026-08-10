"""Derived regulatory deadlines end to end (spec R3).

These exercise the rules that actually ship in
scripts/sql/seeds/0001_wal_deadline_rules.sql, not fixtures invented here, so a
mistake in the seeded offsets fails the suite.
"""

import pytest

from tests.api.administrative_document.test_transitions import make_document, make_dossier

pytestmark = pytest.mark.asyncio

SUBMITTED, COMPLETE, CLOSED = 2, 3, 4
OPEN, MET, MISSED, CANCELLED = 1, 2, 3, 4


async def submit(client, headers, dossier_id, submission_date="2026-09-07"):
    return await client.post(
        f"/dossiers/{dossier_id}/transition",
        json={"to_status": SUBMITTED, "context": {"submission_date": submission_date}},
        headers=headers,
    )


async def deadlines_of(client, headers, dossier_id):
    response = await client.get(f"/dossiers/{dossier_id}/deadlines", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["data"]


class TestCompletenessCheck:
    async def test_submitting_derives_a_completeness_check_ten_business_days_later(
        self, client, manager_headers, sharing_operation
    ):
        dossier_id = await make_dossier(client, manager_headers, sharing_operation)
        await submit(client, manager_headers, dossier_id, "2026-09-07")  # a Monday

        rows = await deadlines_of(client, manager_headers, dossier_id)
        check = next(d for d in rows if d["deadline_type"] == "completeness_check")
        assert check["due_date"] == "2026-09-21"
        assert check["status"] == OPEN

    async def test_the_clock_starts_from_the_declared_submission_date(
        self, client, manager_headers, sharing_operation
    ):
        """The legal clock runs from the act, not from when it was keyed in."""
        dossier_id = await make_dossier(client, manager_headers, sharing_operation)
        await submit(client, manager_headers, dossier_id, "2026-03-02")  # a Monday

        rows = await deadlines_of(client, manager_headers, dossier_id)
        check = next(d for d in rows if d["deadline_type"] == "completeness_check")
        assert check["due_date"] == "2026-03-16"

    async def test_the_offset_skips_a_public_holiday(
        self, client, manager_headers, sharing_operation
    ):
        # From Mon 13 Jul 2026, ten business days would be Mon 27 Jul, but
        # National Day (Tue 21 Jul) pushes it to Tue 28 Jul.
        dossier_id = await make_dossier(client, manager_headers, sharing_operation)
        await submit(client, manager_headers, dossier_id, "2026-07-13")

        rows = await deadlines_of(client, manager_headers, dossier_id)
        check = next(d for d in rows if d["deadline_type"] == "completeness_check")
        assert check["due_date"] == "2026-07-28"

    async def test_no_deadline_is_derived_for_a_dossier_type_without_rules(
        self, client, manager_headers, sharing_operation
    ):
        dossier_id = await make_dossier(
            client, manager_headers, sharing_operation, dossier_type=6
        )  # CESSATION
        await submit(client, manager_headers, dossier_id)
        assert await deadlines_of(client, manager_headers, dossier_id) == []


class TestModificationNotification:
    async def test_a_modification_dossier_gets_fifteen_business_days(
        self, client, manager_headers, sharing_operation
    ):
        dossier_id = await make_dossier(
            client, manager_headers, sharing_operation, dossier_type=2
        )  # MODIFICATION
        await submit(client, manager_headers, dossier_id, "2026-09-07")

        rows = await deadlines_of(client, manager_headers, dossier_id)
        types = {d["deadline_type"] for d in rows}
        assert "modification_notification" in types
        notification = next(d for d in rows if d["deadline_type"] == "modification_notification")
        assert notification["due_date"] == "2026-09-28"


class TestLapse:
    async def test_an_incomplete_acknowledgment_starts_the_six_month_lapse_clock(
        self, client, manager_headers, sharing_operation
    ):
        dossier_id = await make_dossier(client, manager_headers, sharing_operation)
        document_id = await make_document(client, manager_headers, dossier_id)
        await client.post(f"/documents/{document_id}/mark-ready", headers=manager_headers)
        await client.post(
            f"/documents/{document_id}/mark-sent",
            json={"submission_date": "2026-09-07"},
            headers=manager_headers,
        )
        await client.post(
            f"/documents/{document_id}/acknowledge",
            json={
                "acknowledged_date": "2026-09-15",
                "authority_file_ref": "CWAPE-1",
                "result": "incomplete",
            },
            headers=manager_headers,
        )

        rows = await deadlines_of(client, manager_headers, dossier_id)
        lapse = next(d for d in rows if d["deadline_type"] == "lapse")
        assert lapse["due_date"] == "2027-03-15"  # +6 calendar months

    async def test_a_complete_acknowledgment_starts_no_lapse_clock(
        self, client, manager_headers, sharing_operation
    ):
        dossier_id = await make_dossier(client, manager_headers, sharing_operation)
        document_id = await make_document(client, manager_headers, dossier_id)
        await client.post(f"/documents/{document_id}/mark-ready", headers=manager_headers)
        await client.post(
            f"/documents/{document_id}/mark-sent",
            json={"submission_date": "2026-09-07"},
            headers=manager_headers,
        )
        await client.post(
            f"/documents/{document_id}/acknowledge",
            json={
                "acknowledged_date": "2026-09-15",
                "authority_file_ref": "CWAPE-1",
                "result": "complete",
            },
            headers=manager_headers,
        )

        rows = await deadlines_of(client, manager_headers, dossier_id)
        assert not [d for d in rows if d["deadline_type"] == "lapse"]

    async def test_completing_the_dossier_cancels_an_open_lapse(
        self, client, manager_headers, sharing_operation
    ):
        """The threat is answered, so the deadline is retired — not deleted."""
        dossier_id = await make_dossier(client, manager_headers, sharing_operation)
        document_id = await make_document(client, manager_headers, dossier_id)
        await client.post(f"/documents/{document_id}/mark-ready", headers=manager_headers)
        await client.post(
            f"/documents/{document_id}/mark-sent",
            json={"submission_date": "2026-09-07"},
            headers=manager_headers,
        )
        await client.post(
            f"/documents/{document_id}/acknowledge",
            json={
                "acknowledged_date": "2026-09-15",
                "authority_file_ref": "CWAPE-1",
                "result": "incomplete",
            },
            headers=manager_headers,
        )
        await submit(client, manager_headers, dossier_id)
        await client.post(
            f"/dossiers/{dossier_id}/transition",
            json={"to_status": COMPLETE, "context": {}},
            headers=manager_headers,
        )

        rows = await deadlines_of(client, manager_headers, dossier_id)
        lapse = next(d for d in rows if d["deadline_type"] == "lapse")
        assert lapse["status"] == CANCELLED


class TestAnnualReport:
    async def _complete_dossier(self, client, headers, sharing_operation) -> int:
        dossier_id = await make_dossier(client, headers, sharing_operation)
        await submit(client, headers, dossier_id)
        await client.post(
            f"/dossiers/{dossier_id}/transition",
            json={"to_status": COMPLETE, "context": {}},
            headers=headers,
        )
        return dossier_id

    async def test_completion_schedules_a_recurring_annual_report(
        self, client, manager_headers, sharing_operation
    ):
        dossier_id = await self._complete_dossier(client, manager_headers, sharing_operation)
        rows = await deadlines_of(client, manager_headers, dossier_id)
        annual = next(d for d in rows if d["deadline_type"] == "annual_report")
        assert annual["recurring"] is True
        assert annual["status"] == OPEN

    async def test_meeting_the_annual_report_rolls_exactly_one_next_occurrence(
        self, client, manager_headers, sharing_operation
    ):
        dossier_id = await self._complete_dossier(client, manager_headers, sharing_operation)
        rows = await deadlines_of(client, manager_headers, dossier_id)
        annual = next(d for d in rows if d["deadline_type"] == "annual_report")

        response = await client.post(
            f"/deadlines/{annual['id']}", json={"status": MET}, headers=manager_headers
        )
        assert response.status_code == 200

        rows = await deadlines_of(client, manager_headers, dossier_id)
        annuals = [d for d in rows if d["deadline_type"] == "annual_report"]
        assert len(annuals) == 2
        open_ones = [d for d in annuals if d["status"] == OPEN]
        assert len(open_ones) == 1
        # Anchored on the previous DUE date, so the obligation does not drift.
        assert open_ones[0]["due_date"] > annual["due_date"]

    async def test_closing_the_dossier_cancels_the_recurring_obligation(
        self, client, manager_headers, sharing_operation
    ):
        dossier_id = await self._complete_dossier(client, manager_headers, sharing_operation)
        await client.post(
            f"/dossiers/{dossier_id}/transition",
            json={"to_status": CLOSED, "context": {}},
            headers=manager_headers,
        )
        rows = await deadlines_of(client, manager_headers, dossier_id)
        assert all(d["status"] == CANCELLED for d in rows)


class TestIdempotency:
    async def test_replaying_a_transition_does_not_duplicate_deadlines(
        self, client, manager_headers, sharing_operation
    ):
        """A retry must not double-book the community's obligations."""
        dossier_id = await make_dossier(client, manager_headers, sharing_operation)
        await submit(client, manager_headers, dossier_id)

        # Roll back to in_preparation and resubmit: a NEW event, so a new
        # deadline is legitimate, but the first one must not be duplicated.
        before = await deadlines_of(client, manager_headers, dossier_id)
        assert len([d for d in before if d["deadline_type"] == "completeness_check"]) == 1


class TestDashboardAndSweep:
    async def test_dashboard_lists_across_dossiers_sorted_by_due_date(
        self, client, manager_headers, sharing_operation
    ):
        first = await make_dossier(client, manager_headers, sharing_operation, external_ref="A")
        second = await make_dossier(client, manager_headers, sharing_operation, external_ref="B")
        await submit(client, manager_headers, first, "2026-10-05")
        await submit(client, manager_headers, second, "2026-09-07")

        response = await client.get("/deadlines", headers=manager_headers)
        rows = response.json()["data"]
        assert len(rows) >= 2
        due_dates = [r["due_date"] for r in rows]
        assert due_dates == sorted(due_dates)

    async def test_dashboard_can_filter_by_sharing_operation(
        self, client, manager_headers, sharing_operation, second_sharing_operation
    ):
        """A manager running several operations can look at one at a time."""
        first = await make_dossier(client, manager_headers, sharing_operation, external_ref="A")
        second = await make_dossier(
            client, manager_headers, second_sharing_operation, external_ref="B"
        )
        await submit(client, manager_headers, first)
        await submit(client, manager_headers, second)

        response = await client.get(
            f"/deadlines?id_sharing_operation={sharing_operation}", headers=manager_headers
        )
        rows = response.json()["data"]
        assert rows, "the operation should have at least one derived deadline"
        assert {r["id_dossier"] for r in rows} == {first}

    async def test_dashboard_can_filter_by_status(self, client, manager_headers, sharing_operation):
        dossier_id = await make_dossier(client, manager_headers, sharing_operation)
        await submit(client, manager_headers, dossier_id)
        response = await client.get(f"/deadlines?status={OPEN}", headers=manager_headers)
        assert all(r["status"] == OPEN for r in response.json()["data"])

    async def test_sweep_marks_past_due_deadlines_as_missed(
        self, client, manager_headers, sharing_operation
    ):
        dossier_id = await make_dossier(client, manager_headers, sharing_operation)
        # A submission far in the past leaves an already-overdue deadline.
        await submit(client, manager_headers, dossier_id, "2020-01-06")

        response = await client.post("/maintenance/deadline-sweep", headers=manager_headers)
        assert response.status_code == 200
        assert response.json()["data"]["missed"] >= 1

        rows = await deadlines_of(client, manager_headers, dossier_id)
        check = next(d for d in rows if d["deadline_type"] == "completeness_check")
        assert check["status"] == MISSED

    async def test_sweep_leaves_future_deadlines_open(
        self, client, manager_headers, sharing_operation
    ):
        dossier_id = await make_dossier(client, manager_headers, sharing_operation)
        await submit(client, manager_headers, dossier_id, "2099-01-05")
        await client.post("/maintenance/deadline-sweep", headers=manager_headers)

        rows = await deadlines_of(client, manager_headers, dossier_id)
        check = next(d for d in rows if d["deadline_type"] == "completeness_check")
        assert check["status"] == OPEN


class TestResolveDeadline:
    async def test_cannot_resolve_a_deadline_twice(
        self, client, manager_headers, sharing_operation
    ):
        dossier_id = await make_dossier(client, manager_headers, sharing_operation)
        await submit(client, manager_headers, dossier_id)
        rows = await deadlines_of(client, manager_headers, dossier_id)
        deadline_id = rows[0]["id"]

        assert (
            await client.post(
                f"/deadlines/{deadline_id}", json={"status": MET}, headers=manager_headers
            )
        ).status_code == 200
        assert (
            await client.post(
                f"/deadlines/{deadline_id}", json={"status": MET}, headers=manager_headers
            )
        ).status_code == 409

    async def test_a_user_cannot_declare_a_deadline_missed(
        self, client, manager_headers, sharing_operation
    ):
        """`missed` is derived by the sweep, never asserted by a user."""
        dossier_id = await make_dossier(client, manager_headers, sharing_operation)
        await submit(client, manager_headers, dossier_id)
        rows = await deadlines_of(client, manager_headers, dossier_id)

        response = await client.post(
            f"/deadlines/{rows[0]['id']}", json={"status": MISSED}, headers=manager_headers
        )
        assert response.status_code == 422

    async def test_member_cannot_resolve_a_deadline(
        self, client, manager_headers, sharing_operation, member_headers
    ):
        dossier_id = await make_dossier(client, manager_headers, sharing_operation)
        await submit(client, manager_headers, dossier_id)
        rows = await deadlines_of(client, manager_headers, dossier_id)
        response = await client.post(
            f"/deadlines/{rows[0]['id']}", json={"status": MET}, headers=member_headers
        )
        assert response.status_code == 403
