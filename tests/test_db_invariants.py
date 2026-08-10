"""Database-enforced invariants (spec R1/R2/R4).

The service is careful, but "careful" is not a guarantee: an evidentiary record
has to be unforgeable even against a direct SQL client. Two triggers do that
work, and these tests exercise them directly.

Note on the deferred trigger: it fires at COMMIT, and the test harness never
commits (each test runs in a transaction that is rolled back). ``SET CONSTRAINTS
ALL IMMEDIATE`` forces the check to happen where the test can observe it — this
is the standard way to test a deferred constraint.
"""

import datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

pytestmark = pytest.mark.asyncio

DOCUMENT, DOSSIER = 1, 2


async def _insert_dossier(session, *, status: int = 1) -> int:
    result = await session.execute(
        text(
            "INSERT INTO dossier "
            "(id_community, id_sharing_operation, dossier_type, status, region) "
            "VALUES (1, 1, 1, :status, 1) RETURNING id"
        ),
        {"status": status},
    )
    return int(result.scalar_one())


async def _journal(session, *, subject_type: int, subject_id: int, to_status: int) -> int:
    result = await session.execute(
        text(
            "INSERT INTO status_event "
            "(id_community, subject_type, subject_id, from_status, to_status) "
            "VALUES (1, :subject_type, :subject_id, NULL, :to_status) RETURNING id"
        ),
        {"subject_type": subject_type, "subject_id": subject_id, "to_status": to_status},
    )
    return int(result.scalar_one())


async def _check_now(session) -> None:
    """Force deferred constraint triggers to run at this point.

    Call this AT MOST ONCE per test, as the last statement: the setting persists
    for the rest of the transaction, so any later write would be checked
    immediately (and, on failure, abort the transaction anyway).
    """
    await session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))


class TestStatusIsBackedByTheJournal:
    async def test_a_status_backed_by_a_journal_entry_is_accepted(self, db_session):
        dossier_id = await _insert_dossier(db_session, status=1)
        await _journal(db_session, subject_type=DOSSIER, subject_id=dossier_id, to_status=1)
        await _check_now(db_session)  # must not raise

    async def test_a_dossier_without_any_journal_entry_is_rejected(self, db_session):
        """A row cannot come into existence with a status nobody recorded."""
        await _insert_dossier(db_session, status=1)
        with pytest.raises(DBAPIError) as exc:
            await _check_now(db_session)
        assert "status_event" in str(exc.value)

    async def test_a_status_that_disagrees_with_the_journal_is_rejected(self, db_session):
        """The exact thing the journal exists to stop: a silent UPDATE."""
        dossier_id = await _insert_dossier(db_session, status=1)
        await _journal(db_session, subject_type=DOSSIER, subject_id=dossier_id, to_status=1)
        # Move the status without journaling it — the row now lies about itself.
        await db_session.execute(
            text("UPDATE dossier SET status = 3 WHERE id = :id"), {"id": dossier_id}
        )
        with pytest.raises(DBAPIError) as exc:
            await _check_now(db_session)
        assert "journal head" in str(exc.value)

    async def test_the_journal_head_is_the_latest_entry_not_just_any_entry(self, db_session):
        """Status must match the LAST transition, not merely appear in history."""
        dossier_id = await _insert_dossier(db_session, status=1)
        await _journal(db_session, subject_type=DOSSIER, subject_id=dossier_id, to_status=1)
        await _journal(db_session, subject_type=DOSSIER, subject_id=dossier_id, to_status=2)
        # status is still 1, but the head now says 2.
        with pytest.raises(DBAPIError) as exc:
            await _check_now(db_session)
        assert "journal head" in str(exc.value)

    async def test_a_document_status_is_checked_against_its_own_subject_type(self, db_session):
        """A dossier's journal must not satisfy a document's status."""
        dossier_id = await _insert_dossier(db_session, status=1)
        await _journal(db_session, subject_type=DOSSIER, subject_id=dossier_id, to_status=1)
        result = await db_session.execute(
            text(
                "INSERT INTO document (id_community, id_dossier, doc_type, origin, status) "
                "VALUES (1, :id_dossier, 'annex6', 2, 1) RETURNING id"
            ),
            {"id_dossier": dossier_id},
        )
        document_id = int(result.scalar_one())
        # Journal the DOSSIER subject type with the document's id — wrong subject,
        # so the document itself still has no journal entry.
        await _journal(db_session, subject_type=DOSSIER, subject_id=document_id, to_status=1)
        with pytest.raises(DBAPIError):
            await _check_now(db_session)


class TestJournalIsAppendOnly:
    async def test_a_journal_entry_cannot_be_updated(self, db_session):
        dossier_id = await _insert_dossier(db_session)
        event_id = await _journal(
            db_session, subject_type=DOSSIER, subject_id=dossier_id, to_status=1
        )
        with pytest.raises(DBAPIError) as exc:
            await db_session.execute(
                text("UPDATE status_event SET to_status = 9 WHERE id = :id"), {"id": event_id}
            )
        assert "append-only" in str(exc.value)

    async def test_a_journal_entry_cannot_be_deleted(self, db_session):
        dossier_id = await _insert_dossier(db_session)
        event_id = await _journal(
            db_session, subject_type=DOSSIER, subject_id=dossier_id, to_status=1
        )
        with pytest.raises(DBAPIError) as exc:
            await db_session.execute(
                text("DELETE FROM status_event WHERE id = :id"), {"id": event_id}
            )
        assert "append-only" in str(exc.value)


class TestVersionsAreAppendOnly:
    async def _insert_version(self, session) -> int:
        dossier_id = await _insert_dossier(session)
        await _journal(session, subject_type=DOSSIER, subject_id=dossier_id, to_status=1)
        result = await session.execute(
            text(
                "INSERT INTO document (id_community, id_dossier, doc_type, origin, status) "
                "VALUES (1, :id_dossier, 'annex6', 2, 1) RETURNING id"
            ),
            {"id_dossier": dossier_id},
        )
        document_id = int(result.scalar_one())
        await _journal(session, subject_type=DOCUMENT, subject_id=document_id, to_status=1)
        result = await session.execute(
            text(
                "INSERT INTO document_version "
                "(id_community, id_document, version_no, file_ref) "
                "VALUES (1, :id_document, 1, 's3://bucket/key') RETURNING id"
            ),
            {"id_document": document_id},
        )
        return int(result.scalar_one())

    async def test_a_stored_version_cannot_be_repointed(self, db_session):
        """Swapping file_ref would silently change what "was filed"."""
        version_id = await self._insert_version(db_session)
        with pytest.raises(DBAPIError) as exc:
            await db_session.execute(
                text("UPDATE document_version SET file_ref = 's3://b/other' WHERE id = :id"),
                {"id": version_id},
            )
        assert "append-only" in str(exc.value)

    async def test_a_stored_version_cannot_be_deleted(self, db_session):
        version_id = await self._insert_version(db_session)
        with pytest.raises(DBAPIError) as exc:
            await db_session.execute(
                text("DELETE FROM document_version WHERE id = :id"), {"id": version_id}
            )
        assert "append-only" in str(exc.value)

    async def test_version_numbers_are_unique_per_document(self, db_session):
        version_id = await self._insert_version(db_session)
        document_id = (
            await db_session.execute(
                text("SELECT id_document FROM document_version WHERE id = :id"),
                {"id": version_id},
            )
        ).scalar_one()
        with pytest.raises(DBAPIError):
            await db_session.execute(
                text(
                    "INSERT INTO document_version "
                    "(id_community, id_document, version_no, file_ref) "
                    "VALUES (1, :id_document, 1, 's3://bucket/dup')"
                ),
                {"id_document": document_id},
            )


class TestRenderStateIsSeparateFromTheJournal:
    """document_render exists so a technical retry never touches the evidence."""

    async def _insert_document(self, session) -> int:
        dossier_id = await _insert_dossier(session)
        await _journal(session, subject_type=DOSSIER, subject_id=dossier_id, to_status=1)
        result = await session.execute(
            text(
                "INSERT INTO document (id_community, id_dossier, doc_type, origin, status) "
                "VALUES (1, :id_dossier, 'annex6_notification', 1, 1) RETURNING id"
            ),
            {"id_dossier": dossier_id},
        )
        document_id = int(result.scalar_one())
        await _journal(session, subject_type=DOCUMENT, subject_id=document_id, to_status=1)
        return document_id

    async def _template_id(self, session) -> int:
        return int(
            (
                await session.execute(
                    text("SELECT id FROM document_template WHERE valid_to IS NULL LIMIT 1")
                )
            ).scalar_one()
        )

    async def _insert_render(self, session, document_id: int, request_id: str) -> int:
        result = await session.execute(
            text(
                "INSERT INTO document_render "
                "(id_community, id_document, docgen_request_id, render_state, "
                " id_template, data_snapshot_json) "
                "VALUES (1, :doc, :rid, 1, :tpl, '{}'::jsonb) RETURNING id"
            ),
            {"doc": document_id, "rid": request_id, "tpl": await self._template_id(session)},
        )
        return int(result.scalar_one())

    async def test_only_one_render_can_be_in_flight_per_document(self, db_session):
        """This uniqueness IS the concurrency control for POST /generate."""
        document_id = await self._insert_document(db_session)
        await self._insert_render(db_session, document_id, "req-1")
        with pytest.raises(DBAPIError):
            await self._insert_render(db_session, document_id, "req-2")

    async def test_a_request_id_maps_to_exactly_one_render(self, db_session):
        """The result handler correlates on request_id; two rows would be ambiguous."""
        first = await self._insert_document(db_session)
        second = await self._insert_document(db_session)
        await self._insert_render(db_session, first, "req-shared")
        with pytest.raises(DBAPIError):
            await self._insert_render(db_session, second, "req-shared")

    async def test_render_state_changes_need_no_journal_entry(self, db_session):
        """A render is not a regulatory transition — the deferred trigger must
        not fire for it, or every retry would corrupt the status journal."""
        document_id = await self._insert_document(db_session)
        await self._insert_render(db_session, document_id, "req-1")
        await db_session.execute(
            text(
                "UPDATE document_render SET render_state = 2, "
                'render_error_json = \'{"code": "RENDER_ERROR"}\'::jsonb '
                "WHERE id_document = :doc"
            ),
            {"doc": document_id},
        )
        await _check_now(db_session)  # must not raise

    async def test_deleting_a_render_leaves_the_document_intact(self, db_session):
        """Success deletes the render row; the document and its journal survive."""
        document_id = await self._insert_document(db_session)
        await self._insert_render(db_session, document_id, "req-1")
        await db_session.execute(
            text("DELETE FROM document_render WHERE id_document = :doc"), {"doc": document_id}
        )
        still_there = (
            await db_session.execute(
                text("SELECT status FROM document WHERE id = :doc"), {"doc": document_id}
            )
        ).scalar_one()
        assert still_there == 1
        await _check_now(db_session)

    async def test_a_render_is_removed_with_its_document(self, db_session):
        document_id = await self._insert_document(db_session)
        await self._insert_render(db_session, document_id, "req-1")
        await db_session.execute(text("DELETE FROM document WHERE id = :doc"), {"doc": document_id})
        remaining = (
            await db_session.execute(
                text("SELECT COUNT(*) FROM document_render WHERE id_document = :doc"),
                {"doc": document_id},
            )
        ).scalar_one()
        assert remaining == 0


class TestDeadlineIdempotency:
    async def test_the_same_event_cannot_derive_the_same_deadline_twice(self, db_session):
        dossier_id = await _insert_dossier(db_session)
        event_id = await _journal(
            db_session, subject_type=DOSSIER, subject_id=dossier_id, to_status=1
        )
        insert = text(
            "INSERT INTO deadline "
            "(id_community, id_dossier, deadline_type, due_date, status, derived_from_event_id) "
            "VALUES (1, :id_dossier, 'completeness_check', DATE '2026-09-21', 1, :event_id)"
        )
        params = {"id_dossier": dossier_id, "event_id": event_id}
        await db_session.execute(insert, params)
        with pytest.raises(DBAPIError):
            await db_session.execute(insert, params)

    async def test_only_one_recurring_occurrence_may_be_open_at_a_time(self, db_session):
        dossier_id = await _insert_dossier(db_session)
        insert = text(
            "INSERT INTO deadline "
            "(id_community, id_dossier, deadline_type, due_date, status, recurring) "
            "VALUES (1, :id_dossier, 'annual_report', :due, 1, TRUE)"
        )
        await db_session.execute(
            insert, {"id_dossier": dossier_id, "due": datetime.date(2027, 9, 21)}
        )
        with pytest.raises(DBAPIError):
            await db_session.execute(
                insert, {"id_dossier": dossier_id, "due": datetime.date(2028, 9, 21)}
            )
