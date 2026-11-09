"""A sent piece is final, in the database's own terms (phase 2, criterion 2.2).

"Une pièce finale ne peut être ni modifiée ni supprimée, vérifié au niveau de
la DB." The service refuses all of this first; these tests talk SQL directly,
as any client could, and expect the database itself to say no:

* a version is append-only (already true; see test_db_invariants);
* once its procedure is frozen (sent), a piece takes no new version, cannot be
  deleted (nor its procedure), and changes nothing but its status, which the
  journal writes. A RECEIVED piece still takes versions: the answer comes after;
* a version's stage and watermark agree, so a "final" can never be a stamped
  draft;
* a template a render used keeps its bundle, so a regeneration reaches it.

Each test ends on the one statement it expects refused: a refused statement
aborts the test's transaction.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from tests.test_db_invariants import DOCUMENT, DOSSIER, _insert_dossier, _journal

pytestmark = pytest.mark.asyncio

GENERATED, COLLECTED, RECEIVED = 1, 2, 3
DRAFT, FINAL, TO_SIGN = 1, 2, 3
READY, REJECTED = 2, 9


async def _procedure(session) -> int:
    dossier_id = await _insert_dossier(session)
    await _journal(session, subject_type=DOSSIER, subject_id=dossier_id, to_status=1)
    return dossier_id


async def _template(session, *, doc_type: str = "test_form") -> int:
    return int(
        (
            await session.execute(
                text(
                    "INSERT INTO document_template "
                    "(id_community, region, doc_type, version, valid_from, file_ref, "
                    " output_format) "
                    "VALUES (1, 1, :doc_type, 1, DATE '2026-01-01', :ref, 'pdf') "
                    "RETURNING id"
                ),
                {"doc_type": doc_type, "ref": f"s3://optimce-templates/test/{doc_type}/v1/"},
            )
        ).scalar_one()
    )


async def _piece(session, dossier_id: int, *, kind: int = GENERATED) -> int:
    result = await session.execute(
        text(
            "INSERT INTO document (id_community, id_dossier, doc_type, origin, status, "
            " kind, requirement_code) "
            "VALUES (1, :dossier, 'test_form', 1, :status, :kind, 'test_form') RETURNING id"
        ),
        {"dossier": dossier_id, "status": READY, "kind": kind},
    )
    piece_id = int(result.scalar_one())
    await _journal(session, subject_type=DOCUMENT, subject_id=piece_id, to_status=READY)
    return piece_id


async def _version(
    session,
    piece_id: int,
    *,
    version_no: int = 1,
    stage: int = FINAL,
    watermark: str | None = None,
    request: str | None = None,
    template: int | None = None,
) -> int:
    result = await session.execute(
        text(
            "INSERT INTO document_version "
            "(id_community, id_document, version_no, file_ref, stage, watermark, "
            " docgen_request_id, id_template) "
            "VALUES (1, :piece, :no, :ref, :stage, :watermark, :request, "
            "        :template) RETURNING id"
        ),
        {
            "piece": piece_id,
            "no": version_no,
            "ref": f"s3://bucket/{piece_id}/{version_no}",
            "stage": stage,
            "watermark": watermark,
            "request": request,
            "template": template,
        },
    )
    version_id = int(result.scalar_one())
    await session.execute(
        text("UPDATE document SET current_version_id = :v WHERE id = :d"),
        {"v": version_id, "d": piece_id},
    )
    return version_id


async def _send(session, dossier_id: int) -> None:
    await session.execute(
        text("UPDATE dossier SET frozen_at = NOW() WHERE id = :id"), {"id": dossier_id}
    )


async def _sent_piece(session, *, kind: int = GENERATED) -> tuple[int, int]:
    """(dossier, piece): a piece with a final version, in a procedure just sent."""
    dossier_id = await _procedure(session)
    piece_id = await _piece(session, dossier_id, kind=kind)
    await _version(session, piece_id)
    await _send(session, dossier_id)
    return dossier_id, piece_id


async def _refused(session, sql: str, params: dict, needle: str) -> None:
    with pytest.raises(DBAPIError) as exc:
        await session.execute(text(sql), params)
    assert needle in str(exc.value)


class TestASentPieceIsFinal:
    async def test_it_takes_no_new_version(self, db_session):
        _, piece = await _sent_piece(db_session)
        await _refused(
            db_session,
            "INSERT INTO document_version "
            "(id_community, id_document, version_no, file_ref, stage) "
            "VALUES (1, :piece, 2, 's3://bucket/2', 2)",
            {"piece": piece},
            "takes no new version",
        )

    async def test_it_cannot_be_repointed_to_another_version(self, db_session):
        _, piece = await _sent_piece(db_session)
        await _refused(
            db_session,
            "UPDATE document SET current_version_id = NULL WHERE id = :id",
            {"id": piece},
            "only its status can change",
        )

    @pytest.mark.parametrize(
        "assignment",
        [
            "doc_type = 'other'",
            "requirement_code = 'other'",
            "kind = 2",
            "required = FALSE",
            "id_member = 42",
            "origin = 2",
        ],
    )
    async def test_nothing_else_about_it_changes(self, db_session, assignment):
        _, piece = await _sent_piece(db_session)
        await _refused(
            db_session,
            # A parametrize constant, not input.
            f"UPDATE document SET {assignment} WHERE id = :id",  # noqa: S608
            {"id": piece},
            "only its status can change",
        )

    async def test_its_pinned_template_is_kept(self, db_session):
        _, piece = await _sent_piece(db_session)
        template = await _template(db_session)
        await _refused(
            db_session,
            "UPDATE document SET id_template = :t WHERE id = :id",
            {"id": piece, "t": template},
            "only its status can change",
        )

    async def test_it_cannot_be_deleted(self, db_session):
        _, piece = await _sent_piece(db_session)
        await _refused(
            db_session,
            "DELETE FROM document WHERE id = :id",
            {"id": piece},
            "cannot be deleted",
        )

    async def test_nor_can_its_procedure(self, db_session):
        dossier, _ = await _sent_piece(db_session)
        await _refused(
            db_session,
            "DELETE FROM dossier WHERE id = :id",
            {"id": dossier},
            "cannot be deleted",
        )

    async def test_a_sent_procedure_goes_nowhere_even_with_no_version_on_file(self, db_session):
        """Its pieces cascade from it after the row is gone: the guard is on the row."""
        dossier = await _procedure(db_session)
        await _piece(db_session, dossier)
        await _send(db_session, dossier)
        await _refused(
            db_session,
            "DELETE FROM dossier WHERE id = :id",
            {"id": dossier},
            "cannot be deleted",
        )

    async def test_its_status_still_moves_through_the_journal(self, db_session):
        """A recipient may reject a piece after the sending: that is a status."""
        _, piece = await _sent_piece(db_session)
        await _journal(db_session, subject_type=DOCUMENT, subject_id=piece, to_status=REJECTED)
        status = await db_session.execute(
            text("SELECT status FROM document WHERE id = :id"), {"id": piece}
        )
        assert status.scalar_one() == REJECTED

    async def test_a_received_piece_still_takes_the_answer(self, db_session):
        _, piece = await _sent_piece(db_session, kind=RECEIVED)
        second = await _version(db_session, piece, version_no=2)
        current = await db_session.execute(
            text("SELECT current_version_id FROM document WHERE id = :id"), {"id": piece}
        )
        assert current.scalar_one() == second

    async def test_but_is_not_deleted_either(self, db_session):
        _, piece = await _sent_piece(db_session, kind=RECEIVED)
        await _refused(
            db_session,
            "DELETE FROM document WHERE id = :id",
            {"id": piece},
            "cannot be deleted",
        )


class TestBeforeSendingAPieceMoves:
    async def test_it_takes_new_versions_and_is_repointed(self, db_session):
        dossier = await _procedure(db_session)
        piece = await _piece(db_session, dossier)
        await _version(db_session, piece)
        second = await _version(
            db_session, piece, version_no=2, stage=DRAFT, watermark="BROUILLON", request="r-2"
        )
        current = await db_session.execute(
            text("SELECT current_version_id FROM document WHERE id = :id"), {"id": piece}
        )
        assert current.scalar_one() == second

    async def test_a_piece_without_versions_can_go(self, db_session):
        dossier = await _procedure(db_session)
        piece = await _piece(db_session, dossier)
        await db_session.execute(text("DELETE FROM document WHERE id = :id"), {"id": piece})
        left = await db_session.execute(
            text("SELECT count(*) FROM document WHERE id = :id"), {"id": piece}
        )
        assert left.scalar_one() == 0


class TestAVersionsStageAndWatermarkAgree:
    async def _piece(self, session) -> int:
        return await _piece(session, await _procedure(session))

    @pytest.mark.parametrize(
        ("stage", "watermark", "request_id"),
        [
            (FINAL, None, None),  # an upload
            (FINAL, None, "r-1"),  # an unstamped render
            (DRAFT, "BROUILLON", "r-1"),
            (TO_SIGN, None, "r-1"),
        ],
    )
    async def test_the_consistent_ones_are_stored(self, db_session, stage, watermark, request_id):
        piece = await self._piece(db_session)
        await _version(db_session, piece, stage=stage, watermark=watermark, request=request_id)

    @pytest.mark.parametrize(
        ("stage", "watermark", "request_id"),
        [
            (FINAL, "BROUILLON", "r-1"),  # a stamped "final"
            (DRAFT, None, "r-1"),  # an unstamped "draft"
            (DRAFT, "BROUILLON", None),  # a "draft" nobody rendered
            (TO_SIGN, None, None),  # an uploaded form "to sign": the signed one is final
            (TO_SIGN, "BROUILLON", "r-1"),
            (4, None, None),
        ],
    )
    async def test_the_others_are_refused(self, db_session, stage, watermark, request_id):
        piece = await self._piece(db_session)
        with pytest.raises(DBAPIError) as exc:
            await _version(db_session, piece, stage=stage, watermark=watermark, request=request_id)
        assert "ck_document_version_stage" in str(exc.value)


class TestARenderSaysWhatItIsFor:
    async def _render(self, session, **columns) -> None:
        piece = await _piece(session, await _procedure(session))
        values = {
            "purpose": 1,
            "stage": FINAL,
            "watermark": None,
            "version": None,
            **columns,
        }
        await session.execute(
            text(
                "INSERT INTO document_render "
                "(id_community, id_document, docgen_request_id, render_state, id_template, "
                " data_snapshot_json, purpose, stage, watermark, id_document_version) "
                "VALUES (1, :piece, 'req', 1, :template, '{}'::jsonb, :purpose, :stage, "
                "        :watermark, :version)"
            ),
            {"piece": piece, "template": await _template(session), **values},
        )

    async def test_a_draft_render_carries_its_stamp(self, db_session):
        await self._render(db_session, stage=DRAFT, watermark="BROUILLON")

    async def test_an_unstamped_draft_is_refused(self, db_session):
        with pytest.raises(DBAPIError) as exc:
            await self._render(db_session, stage=DRAFT)
        assert "ck_document_render_stage" in str(exc.value)

    async def test_a_verification_names_the_version_it_checks(self, db_session):
        with pytest.raises(DBAPIError) as exc:
            await self._render(db_session, purpose=2)
        assert "ck_document_render_purpose" in str(exc.value)


class TestATemplateARenderUsedKeepsItsBundle:
    async def _used_template(self, session) -> int:
        template = await _template(session)
        piece = await _piece(session, await _procedure(session))
        await _version(session, piece, request="r-1", template=template)
        return template

    @pytest.mark.parametrize(
        "assignment",
        [
            "file_ref = 's3://optimce-templates/test/test_form/v2/'",
            "version = 2",
            "doc_type = 'other_form'",
            "region = 2",
            "output_format = 'xlsx'",
            "id_community = NULL",
        ],
    )
    async def test_what_selects_the_bundle_is_frozen(self, db_session, assignment):
        template = await self._used_template(db_session)
        await _refused(
            db_session,
            # A parametrize constant, not input.
            f"UPDATE document_template SET {assignment} WHERE id = :id",  # noqa: S608
            {"id": template},
            "a render used it",
        )

    async def test_it_can_still_be_closed_and_relabelled(self, db_session):
        """valid_to is how the next version comes into force."""
        template = await self._used_template(db_session)
        await db_session.execute(
            text(
                "UPDATE document_template SET valid_to = DATE '2026-12-31', label = 'old' "
                "WHERE id = :id"
            ),
            {"id": template},
        )

    async def test_it_cannot_be_deleted(self, db_session):
        template = await self._used_template(db_session)
        await _refused(
            db_session,
            "DELETE FROM document_template WHERE id = :id",
            {"id": template},
            "a render used it",
        )

    async def test_a_render_in_flight_counts_as_a_use(self, db_session):
        template = await _template(db_session)
        piece = await _piece(db_session, await _procedure(db_session))
        await db_session.execute(
            text(
                "INSERT INTO document_render "
                "(id_community, id_document, docgen_request_id, render_state, id_template, "
                " data_snapshot_json, purpose, stage) "
                "VALUES (1, :piece, 'req', 1, :template, '{}'::jsonb, 1, 2)"
            ),
            {"piece": piece, "template": template},
        )
        await _refused(
            db_session,
            "UPDATE document_template SET file_ref = 's3://elsewhere/' WHERE id = :id",
            {"id": template},
            "a render used it",
        )

    async def test_an_unused_one_can_still_be_corrected(self, db_session):
        template = await _template(db_session)
        await db_session.execute(
            text("UPDATE document_template SET file_ref = 's3://fixed/' WHERE id = :id"),
            {"id": template},
        )
        await db_session.execute(
            text("DELETE FROM document_template WHERE id = :id"), {"id": template}
        )


class TestVersionChecksAreAppendOnly:
    async def _check(self, session, *, outcome: int = 1, sha: str | None = "abc") -> int:
        piece = await _piece(session, await _procedure(session))
        version = await _version(session, piece, request="r-1")
        result = await session.execute(
            text(
                "INSERT INTO version_check "
                "(id_community, id_document_version, docgen_request_id, outcome, "
                " content_sha256, requested_at) "
                "VALUES (1, :version, 'verify-1', :outcome, :sha, NOW()) RETURNING id"
            ),
            {"version": version, "outcome": outcome, "sha": sha},
        )
        return int(result.scalar_one())

    async def test_a_check_cannot_be_rewritten(self, db_session):
        check = await self._check(db_session)
        await _refused(
            db_session,
            "UPDATE version_check SET outcome = 2 WHERE id = :id",
            {"id": check},
            "append-only",
        )

    async def test_a_check_cannot_be_deleted(self, db_session):
        check = await self._check(db_session)
        await _refused(
            db_session, "DELETE FROM version_check WHERE id = :id", {"id": check}, "append-only"
        )

    async def test_a_comparison_records_what_it_compared(self, db_session):
        with pytest.raises(DBAPIError) as exc:
            await self._check(db_session, outcome=1, sha=None)
        assert "ck_version_check_outcome" in str(exc.value)

    async def test_a_failure_records_why(self, db_session):
        with pytest.raises(DBAPIError) as exc:
            await self._check(db_session, outcome=3, sha=None)
        assert "ck_version_check_outcome" in str(exc.value)
