"""Database-enforced invariants (spec R1/R2/R4).

The service is careful, but "careful" is not a guarantee: an evidentiary record
has to be unforgeable even against a direct SQL client. Two triggers do that
work, and these tests exercise them directly.

Note on the deferred trigger: it fires at COMMIT, and the test harness never
commits (each test runs in a transaction that is rolled back). ``SET CONSTRAINTS
ALL IMMEDIATE`` forces the check to happen where the test can observe it — this
is the standard way to test a deferred constraint.
"""

import itertools

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

pytestmark = pytest.mark.asyncio

DOCUMENT, DOSSIER = 1, 2


_TYPES = itertools.count(1)


async def _insert_dossier(session, *, status: int = 1) -> int:
    """A procedure NOT yet journaled (the caller journals its birth), of a type
    of its own so that two drafts in one test never share a drawer."""
    code = f"TYPE_{next(_TYPES)}"
    definition = await _definition(session, code=code)
    result = await session.execute(
        text(
            "INSERT INTO dossier "
            "(id_community, id_sharing_operation, status, step, region, "
            " id_procedure_definition, regulator, family, scope, opened_by) "
            "VALUES (1, 1, :status, 'draft', 1, :definition, 'TEST-REG', :code, 2, 1) "
            "RETURNING id"
        ),
        {"status": status, "definition": definition, "code": code},
    )
    return int(result.scalar_one())


async def _journal(
    session,
    *,
    subject_type: int,
    subject_id: int,
    to_status: int,
    to_step: str | None = None,
    id_community: int = 1,
) -> int:
    if subject_type == DOSSIER and to_step is None:
        to_step = "draft"  # a dossier's step is NOT NULL, and the journal writes it
    result = await session.execute(
        text(
            "INSERT INTO status_event "
            "(id_community, subject_type, subject_id, from_status, to_status, to_step) "
            "VALUES (:id_community, :subject_type, :subject_id, NULL, :to_status, :to_step) "
            "RETURNING id"
        ),
        {
            "id_community": id_community,
            "subject_type": subject_type,
            "subject_id": subject_id,
            "to_status": to_status,
            "to_step": to_step,
        },
    )
    return int(result.scalar_one())


async def _definition(session, *, code: str = "PARTAGE_MODIF", scope: int = 2) -> int:
    """A catalogue entry of a made-up regulator, so no real catalogue row is touched."""
    result = await session.execute(
        text(
            "INSERT INTO procedure_definition "
            "(regulator, code, version, valid_from, scope, recipient, channel, family) "
            "VALUES ('TEST-REG', :code, 1, DATE '2026-01-01', :scope, 1, 1, :code) "
            "RETURNING id"
        ),
        {"code": code, "scope": scope},
    )
    return int(result.scalar_one())


async def _procedure(
    session,
    *,
    id_definition: int,
    family: str = "PARTAGE_MODIF",
    scope: int = 2,
    operation: int | None = 1,
    status: int = 1,
    step: str = "draft",
    period_key: str | None = None,
) -> int:
    """An engine procedure (no July dossier_type), born through the journal."""
    result = await session.execute(
        text(
            "INSERT INTO dossier "
            "(id_community, id_sharing_operation, status, step, region, "
            " id_procedure_definition, regulator, family, scope, opened_by, period_key) "
            "VALUES (1, :operation, :status, :step, 1, :definition, 'TEST-REG', :family, "
            "        :scope, 1, :period_key) RETURNING id"
        ),
        {
            "operation": operation,
            "status": status,
            "step": step,
            "definition": id_definition,
            "family": family,
            "scope": scope,
            "period_key": period_key,
        },
    )
    dossier_id = int(result.scalar_one())
    await _journal(
        session, subject_type=DOSSIER, subject_id=dossier_id, to_status=status, to_step=step
    )
    return dossier_id


async def _dossier_status(session, dossier_id: int) -> tuple[int, str | None]:
    row = (
        await session.execute(
            text("SELECT status, step FROM dossier WHERE id = :id"), {"id": dossier_id}
        )
    ).one()
    return int(row.status), row.step


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

    async def test_a_direct_status_write_is_refused_on_the_spot(self, db_session):
        """The exact thing the journal exists to stop: a silent UPDATE. It no
        longer waits for COMMIT — the statement itself fails."""
        dossier_id = await _insert_dossier(db_session, status=1)
        await _journal(db_session, subject_type=DOSSIER, subject_id=dossier_id, to_status=1)
        with pytest.raises(DBAPIError) as exc:
            await db_session.execute(
                text("UPDATE dossier SET status = 3 WHERE id = :id"), {"id": dossier_id}
            )
        assert "never directly" in str(exc.value)

    async def test_appending_to_the_journal_moves_the_status(self, db_session):
        """The journal head IS the status: the latest entry, not just any entry."""
        dossier_id = await _insert_dossier(db_session, status=1)
        await _journal(db_session, subject_type=DOSSIER, subject_id=dossier_id, to_status=1)
        await _journal(db_session, subject_type=DOSSIER, subject_id=dossier_id, to_status=2)
        assert await _dossier_status(db_session, dossier_id) == (2, "draft")
        await _check_now(db_session)  # must not raise

    async def test_a_document_status_is_checked_against_its_own_subject_type(self, db_session):
        """A dossier's journal must not satisfy a document's status."""
        dossier_id = await _insert_dossier(db_session, status=1)
        await _journal(db_session, subject_type=DOSSIER, subject_id=dossier_id, to_status=1)
        result = await db_session.execute(
            text(
                "INSERT INTO document (id_community, id_dossier, doc_type, origin, status, "
                " kind, requirement_code) "
                "VALUES (1, :id_dossier, 'annex6', 2, 1, 2, 'annex6') RETURNING id"
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
                "INSERT INTO document (id_community, id_dossier, doc_type, origin, status, "
                " kind, requirement_code) "
                "VALUES (1, :id_dossier, 'annex6', 2, 1, 2, 'annex6') RETURNING id"
            ),
            {"id_dossier": dossier_id},
        )
        document_id = int(result.scalar_one())
        await _journal(session, subject_type=DOCUMENT, subject_id=document_id, to_status=1)
        result = await session.execute(
            text(
                "INSERT INTO document_version "
                "(id_community, id_document, version_no, file_ref, stage) "
                "VALUES (1, :id_document, 1, 's3://bucket/key', 2) RETURNING id"
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
                    "(id_community, id_document, version_no, file_ref, stage) "
                    "VALUES (1, :id_document, 1, 's3://bucket/dup', 2)"
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
                "INSERT INTO document (id_community, id_dossier, doc_type, origin, status, "
                " kind, requirement_code) "
                "VALUES (1, :id_dossier, 'annex6_notification', 1, 1, 1, 'annex6_notification') "
                "RETURNING id"
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
                " id_template, data_snapshot_json, purpose, stage) "
                "VALUES (1, :doc, :rid, 1, :tpl, '{}'::jsonb, 1, 2) RETURNING id"
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
            "(id_community, id_dossier, deadline_type, due_date, status, party, kind, "
            " derived_from_event_id) "
            "VALUES (1, :id_dossier, 'completeness', DATE '2026-09-21', 1, 2, 2, :event_id)"
        )
        params = {"id_dossier": dossier_id, "event_id": event_id}
        await db_session.execute(insert, params)
        with pytest.raises(DBAPIError):
            await db_session.execute(insert, params)


# ---------------------------------------------------------------------------
# Procedures engine (migration 0003)
# ---------------------------------------------------------------------------


class TestTheJournalWritesTheStatus:
    """status and step are a copy of the journal head, written by the database."""

    async def test_the_plugin_step_travels_with_the_status(self, db_session):
        definition = await _definition(db_session)
        dossier_id = await _procedure(db_session, id_definition=definition)
        await _journal(
            db_session, subject_type=DOSSIER, subject_id=dossier_id, to_status=2, to_step="sent"
        )
        await _journal(
            db_session,
            subject_type=DOSSIER,
            subject_id=dossier_id,
            to_status=2,
            to_step="in_review",
        )
        assert await _dossier_status(db_session, dossier_id) == (2, "in_review")
        await _check_now(db_session)

    async def test_a_direct_step_write_is_refused(self, db_session):
        """The step is plugin vocabulary, but it is journaled like the status."""
        definition = await _definition(db_session)
        dossier_id = await _procedure(db_session, id_definition=definition)
        with pytest.raises(DBAPIError) as exc:
            await db_session.execute(
                text("UPDATE dossier SET step = 'accepted' WHERE id = :id"), {"id": dossier_id}
            )
        assert "never directly" in str(exc.value)

    async def test_re_sending_the_current_status_is_not_a_write(self, db_session):
        """An ORM flush may repeat an unchanged status; that must stay harmless."""
        definition = await _definition(db_session)
        dossier_id = await _procedure(db_session, id_definition=definition)
        await db_session.execute(
            text("UPDATE dossier SET status = 1, step = 'draft', title = 'x' WHERE id = :id"),
            {"id": dossier_id},
        )
        await _check_now(db_session)

    async def test_several_moves_in_one_transaction_are_accepted(self, db_session):
        """The July rule "one status change per transaction" is lifted: the
        deferred check reads the final row, not the row of each queued event."""
        definition = await _definition(db_session)
        dossier_id = await _procedure(db_session, id_definition=definition)
        for status, step in ((6, "ready"), (2, "sent"), (2, "incomplete"), (2, "sent")):
            await _journal(
                db_session,
                subject_type=DOSSIER,
                subject_id=dossier_id,
                to_status=status,
                to_step=step,
            )
        assert await _dossier_status(db_session, dossier_id) == (2, "sent")
        await _check_now(db_session)

    async def test_another_tenants_journal_row_moves_nothing(self, db_session):
        """Subject ids are global serials; the community scopes the write."""
        dossier_id = await _insert_dossier(db_session, status=1)
        await _journal(db_session, subject_type=DOSSIER, subject_id=dossier_id, to_status=1)
        await _journal(
            db_session, subject_type=DOSSIER, subject_id=dossier_id, to_status=5, id_community=2
        )
        assert await _dossier_status(db_session, dossier_id) == (1, "draft")
        await _check_now(db_session)

    async def test_a_document_status_is_written_by_its_journal(self, db_session):
        dossier_id = await _insert_dossier(db_session)
        await _journal(db_session, subject_type=DOSSIER, subject_id=dossier_id, to_status=1)
        document_id = int(
            (
                await db_session.execute(
                    text(
                        "INSERT INTO document (id_community, id_dossier, doc_type, origin, status, "
                        " kind, requirement_code) "
                        "VALUES (1, :id_dossier, 'annex6', 2, 6, 2, 'annex6') RETURNING id"
                    ),
                    {"id_dossier": dossier_id},
                )
            ).scalar_one()
        )
        await _journal(db_session, subject_type=DOCUMENT, subject_id=document_id, to_status=6)
        await _journal(db_session, subject_type=DOCUMENT, subject_id=document_id, to_status=2)
        status = (
            await db_session.execute(
                text("SELECT status FROM document WHERE id = :id"), {"id": document_id}
            )
        ).scalar_one()
        assert status == 2
        with pytest.raises(DBAPIError) as exc:
            await db_session.execute(
                text("UPDATE document SET status = 7 WHERE id = :id"), {"id": document_id}
            )
        assert "never directly" in str(exc.value)

    async def test_only_a_dossier_has_steps(self, db_session):
        with pytest.raises(DBAPIError):
            await _journal(
                db_session, subject_type=DOCUMENT, subject_id=1, to_status=2, to_step="ready"
            )


class TestCorrespondenceIsAppendOnly:
    async def _insert(self, session) -> int:
        dossier_id = await _insert_dossier(session)
        await _journal(session, subject_type=DOSSIER, subject_id=dossier_id, to_status=1)
        result = await session.execute(
            text(
                "INSERT INTO correspondence "
                "(id_community, id_dossier, direction, counterparty, channel, type, occurred_at) "
                "VALUES (1, :id_dossier, 2, 1, 1, 'completeness_ack', NOW()) RETURNING id"
            ),
            {"id_dossier": dossier_id},
        )
        return int(result.scalar_one())

    async def test_a_message_cannot_be_rewritten(self, db_session):
        """What the GRD answered, and when, is evidence."""
        correspondence_id = await self._insert(db_session)
        with pytest.raises(DBAPIError) as exc:
            await db_session.execute(
                text("UPDATE correspondence SET type = 'decision_granted' WHERE id = :id"),
                {"id": correspondence_id},
            )
        assert "append-only" in str(exc.value)

    async def test_a_message_cannot_be_deleted(self, db_session):
        correspondence_id = await self._insert(db_session)
        with pytest.raises(DBAPIError) as exc:
            await db_session.execute(
                text("DELETE FROM correspondence WHERE id = :id"), {"id": correspondence_id}
            )
        assert "append-only" in str(exc.value)


class TestOneOpenDraftPerFamily:
    """uq_dossier_open_draft: the drawer every incoming change is filed into."""

    async def test_a_second_open_draft_of_the_family_is_refused(self, db_session):
        definition = await _definition(db_session)
        await _procedure(db_session, id_definition=definition)
        with pytest.raises(DBAPIError):
            await _procedure(db_session, id_definition=definition, status=6, step="ready")

    async def test_a_sent_procedure_leaves_room_for_a_new_draft(self, db_session):
        """A change after sending opens a new draft (criterion 1.3)."""
        definition = await _definition(db_session)
        await _procedure(db_session, id_definition=definition, status=2, step="sent")
        await _procedure(db_session, id_definition=definition)
        await _check_now(db_session)

    async def test_each_operation_has_its_own_draft(self, db_session):
        definition = await _definition(db_session)
        await _procedure(db_session, id_definition=definition, operation=1)
        await _procedure(db_session, id_definition=definition, operation=2)
        await _check_now(db_session)

    async def test_community_scope_drafts_collide_although_their_operation_is_null(
        self, db_session
    ):
        """NULLS NOT DISTINCT: without it a NULL operation would never collide."""
        definition = await _definition(db_session, code="CE_ACTUALISATION", scope=1)
        await _procedure(
            db_session, id_definition=definition, family="CE_ACTUALISATION", scope=1, operation=None
        )
        with pytest.raises(DBAPIError):
            await _procedure(
                db_session,
                id_definition=definition,
                family="CE_ACTUALISATION",
                scope=1,
                operation=None,
            )

    async def test_calendar_procedures_have_one_draft_per_period(self, db_session):
        definition = await _definition(db_session, code="CE_RAPPORT_ANNUEL", scope=1)
        for year in ("2026", "2027"):
            await _procedure(
                db_session,
                id_definition=definition,
                family="CE_RAPPORT_ANNUEL",
                scope=1,
                operation=None,
                period_key=year,
            )
        await _check_now(db_session)


class TestAProcedureIsWhole:
    async def test_a_community_scope_procedure_has_no_operation(self, db_session):
        definition = await _definition(db_session, code="CE_NOTIFICATION", scope=1)
        with pytest.raises(DBAPIError):
            await _procedure(
                db_session, id_definition=definition, family="CE_NOTIFICATION", scope=1, operation=7
            )

    async def test_an_operation_scope_procedure_has_one(self, db_session):
        definition = await _definition(db_session)
        with pytest.raises(DBAPIError):
            await _procedure(db_session, id_definition=definition, operation=None)

    async def test_a_procedure_without_a_definition_is_refused(self, db_session):
        """Every row is a procedure of a catalogue (migration 0004)."""
        with pytest.raises(DBAPIError):
            await db_session.execute(
                text(
                    "INSERT INTO dossier (id_community, id_sharing_operation, status, region) "
                    "VALUES (1, 1, 1, 1)"
                )
            )

    async def test_a_procedure_without_a_scope_is_refused(self, db_session):
        """`scope IN (1, 2)` on a NULL scope is NULL, and a NULL CHECK passes —
        the constraint must fail closed instead."""
        definition = await _definition(db_session)
        with pytest.raises(DBAPIError):
            await db_session.execute(
                text(
                    "INSERT INTO dossier (id_community, id_sharing_operation, status, region, "
                    " id_procedure_definition, regulator, family, scope, opened_by) "
                    "VALUES (1, 1, 1, 1, :definition, 'TEST-REG', 'PARTAGE_MODIF', NULL, 1)"
                ),
                {"definition": definition},
            )

    async def test_a_july_shaped_row_is_refused(self, db_session):
        """A July type and an operation no longer make a dossier."""
        with pytest.raises(DBAPIError):
            await db_session.execute(
                text(
                    "INSERT INTO dossier (id_community, id_sharing_operation, dossier_type, "
                    " status, region) VALUES (1, 1, 1, 1, 1)"
                )
            )


class TestCatalogueRules:
    async def _rule(self, session, *, definition: int, code: str = "completeness", **extra):
        values = {
            "id_community": None,
            "kind": 1,
            "anchor_event": "sent",
            "offset_value": 10,
            "offset_unit": 1,
            "annual_month": None,
            "annual_day": None,
            **extra,
        }
        await session.execute(
            text(
                "INSERT INTO deadline_rule "
                "(id_community, id_procedure_definition, code, kind, anchor_event, party, "
                " deadline_kind, offset_value, offset_unit, annual_month, annual_day) "
                "VALUES (:id_community, :definition, :code, :kind, :anchor_event, 2, 2, "
                "        :offset_value, :offset_unit, :annual_month, :annual_day)"
            ),
            {"definition": definition, "code": code, **values},
        )

    async def test_two_procedures_may_use_the_same_rule_code(self, db_session):
        """The legacy unique index must not see catalogue rows (all-NULL columns)."""
        await self._rule(db_session, definition=await _definition(db_session, code="A"))
        await self._rule(db_session, definition=await _definition(db_session, code="B"))

    async def test_a_rule_code_is_unique_inside_its_procedure(self, db_session):
        definition = await _definition(db_session)
        await self._rule(db_session, definition=definition)
        with pytest.raises(DBAPIError):
            await self._rule(db_session, definition=definition)

    async def test_a_catalogue_rule_is_never_a_community_override(self, db_session):
        with pytest.raises(DBAPIError):
            await self._rule(db_session, definition=await _definition(db_session), id_community=1)

    async def test_an_annual_rule_needs_its_day(self, db_session):
        with pytest.raises(DBAPIError):
            await self._rule(
                db_session,
                definition=await _definition(db_session),
                kind=2,
                annual_month=9,
                annual_day=None,
            )

    async def test_a_catalogue_rule_needs_its_kind(self, db_session):
        with pytest.raises(DBAPIError):
            await self._rule(db_session, definition=await _definition(db_session), kind=None)

    @pytest.mark.parametrize(("party", "kind"), [(2, None), (None, 2), (None, None)])
    async def test_a_deadline_says_who_and_what(self, db_session, party, kind):
        """Party and kind are always there: no July deadline remains (0004)."""
        dossier_id = await _insert_dossier(db_session)
        with pytest.raises(DBAPIError):
            await db_session.execute(
                text(
                    "INSERT INTO deadline (id_community, id_dossier, deadline_type, due_date, "
                    " party, kind) VALUES (1, :id, 'completeness', DATE '2026-11-20', "
                    " :party, :kind)"
                ),
                {"id": dossier_id, "party": party, "kind": kind},
            )

    async def test_an_annual_rule_needs_no_offset(self, db_session):
        await self._rule(
            db_session,
            definition=await _definition(db_session),
            code="submission",
            kind=2,
            anchor_event=None,
            offset_value=None,
            offset_unit=None,
            annual_month=9,
            annual_day=1,
        )


class TestConsumerIdempotency:
    async def _change(self, session, *, event: str, procedure: str, status: int = 1, dossier=None):
        await session.execute(
            text(
                "INSERT INTO change "
                "(id_community, kind, subject_type, subject_ref, source_event_id, "
                " procedure_code, status, id_dossier) "
                "VALUES (1, 'meter_added', 1, '541448000000000001', CAST(:event AS uuid), "
                "        :procedure, :status, :dossier)"
            ),
            {"event": event, "procedure": procedure, "status": status, "dossier": dossier},
        )

    async def test_an_event_yields_one_change_per_target_procedure(self, db_session):
        event = "6f1c1b8e-0000-4000-8000-000000000001"
        await self._change(db_session, event=event, procedure="PARTAGE_MODIF")
        await self._change(db_session, event=event, procedure="CE_ACTUALISATION")
        with pytest.raises(DBAPIError):
            await self._change(db_session, event=event, procedure="PARTAGE_MODIF")

    async def test_an_attached_change_belongs_to_a_procedure(self, db_session):
        with pytest.raises(DBAPIError):
            await self._change(
                db_session,
                event="6f1c1b8e-0000-4000-8000-000000000002",
                procedure="PARTAGE_MODIF",
                status=2,
            )

    async def test_an_event_is_recorded_once(self, db_session):
        insert = text(
            "INSERT INTO processed_event (event_id, id_community, event_type, outcome) "
            "VALUES (CAST('6f1c1b8e-0000-4000-8000-000000000003' AS uuid), 1, "
            "        'operation.meter_added', 2)"
        )
        await db_session.execute(insert)
        with pytest.raises(DBAPIError):
            await db_session.execute(insert)

    async def test_a_community_has_one_open_activation_period(self, db_session):
        insert = text("INSERT INTO module_activation (id_community, enabled_at) VALUES (1, NOW())")
        await db_session.execute(insert)
        with pytest.raises(DBAPIError):
            await db_session.execute(insert)
