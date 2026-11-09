"""Drafts, finals, one-click regeneration and verification (phase 2).

A generated piece is worked on as stamped drafts; what is sent is a final,
rendered unstamped from the reviewed draft's own snapshot and template; a sent
final regenerates identically from what it froze (criterion 2.1). The render
itself is document-generation's: here its request is captured off the wire
(``FakeDocGen``) and its result fed back the way it arrives.
"""

from __future__ import annotations

import datetime
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from engine.baseline import brussels_today
from ports.document_generation import DocgenRequest
from regulators.cwape.plugin import CwapePlugin
from shared.const import CheckOutcome, ProcedureDocumentStatus, VersionStage
from shared.models.local_models import (
    DocumentModel,
    DocumentRenderModel,
    DocumentTemplateModel,
    DocumentVersionModel,
    VersionCheckModel,
)
from tests.api.admin.test_procedure_routes import _file, _get, _headers, _piece, _world
from tests.engine.world import EAN, add_key
from tests.test_worker import FakeDocGen, _success
from worker import docgen_results, generate

pytestmark = pytest.mark.asyncio

TODAY = brussels_today(datetime.datetime.now(datetime.UTC))
# The two generated pieces of a simple modification (a meter removed).
FORMS = ("sharing_modification", "annex6_sharing_form")
MARK = CwapePlugin.draft_watermark


async def _render(db_session, document_id: int) -> DocgenRequest:
    """Take the render the API committed to the wire, as the worker does."""
    docgen = FakeDocGen()
    assert await generate.process_generate(document_id, doc_port=docgen, local_session=db_session)
    (request,) = docgen.requests
    return request


async def _land(
    db_session,
    world,
    request: DocgenRequest,
    document_id: int,
    *,
    sha256: str | None = None,
    echo: bool = True,
) -> str:
    """Feed docgen's answer back. A current docgen echoes the stamp it applied."""
    body = _success(
        request.request_id,
        document_id=document_id,
        tenant=world.id_community,
        uri=f"s3://optimce-documents/x/{request.request_id}/form.pdf",
        watermark=request.watermark if echo else None,
        sha256=sha256 or f"sha-{request.request_id}",
    )
    return await docgen_results.process_docgen_result(
        body, local_session=db_session, crm_session=db_session
    )


async def _generate(client, headers, document_id: int, **body: Any) -> None:
    response = await client.post(
        f"/documents/{document_id}/generate", json={"data": {}, **body}, headers=headers
    )
    assert response.status_code == 200, response.text


async def _drafted(client, db_session) -> tuple[Any, dict[str, str], int, dict[str, int]]:
    """A simple modification whose two forms are generated drafts: it is ready."""
    world = await _world(db_session)
    headers = await _headers(db_session, world)
    dossier_id = await _file(world, "operation.meter_removed", EAN[0])
    detail = await _get(client, headers, dossier_id)
    pieces = {code: _piece(detail, code)["id"] for code in FORMS}
    for document_id in pieces.values():
        await _generate(client, headers, document_id)
        assert await _land(db_session, world, await _render(db_session, document_id), document_id)
    assert (await _get(client, headers, dossier_id))["step"] == "ready"
    return world, headers, dossier_id, pieces


async def _current(db_session: AsyncSession, document_id: int) -> DocumentVersionModel:
    piece = await db_session.get(DocumentModel, document_id)
    assert piece is not None
    await db_session.refresh(piece)
    version = await db_session.get(DocumentVersionModel, piece.current_version_id)
    assert version is not None
    return version


async def _finalize(client, headers, dossier_id: int) -> dict:
    response = await client.post(f"/admin/dossiers/{dossier_id}/finalize", headers=headers)
    assert response.status_code == 202, response.text
    body: dict = response.json()["data"]
    return body


async def _finals(client, db_session, world, headers, dossier_id, pieces) -> None:
    outcome = await _finalize(client, headers, dossier_id)
    assert sorted(outcome["requested"]) == sorted(pieces.values())
    for document_id in pieces.values():
        await _land(db_session, world, await _render(db_session, document_id), document_id)


async def _send(client, headers, dossier_id: int):
    return await client.post(
        f"/admin/dossiers/{dossier_id}/send",
        data={"sent_on": TODAY.isoformat()},
        headers=headers,
    )


class TestDraftsAreStamped:
    async def test_a_generated_piece_renders_as_a_stamped_draft(self, client, db_session):
        world, _headers_, _dossier, pieces = await _drafted(client, db_session)

        version = await _current(db_session, pieces["annex6_sharing_form"])

        assert version.stage == int(VersionStage.DRAFT)
        assert version.watermark == MARK == "BROUILLON"
        assert version.prefill_json is not None, "a one-click regeneration needs it"
        assert version.original_filename == "form-brouillon.pdf"
        assert version.template_version == "2026.02.25"
        assert version.renderer_json == {"python": "3.12.13", "zlib": "1.3.1", "openpyxl": "3.1.5"}

    async def test_the_request_asks_docgen_for_the_plugins_stamp(self, client, db_session):
        world = await _world(db_session)
        headers = await _headers(db_session, world)
        dossier_id = await _file(world, "operation.meter_removed", EAN[0])
        document_id = _piece(await _get(client, headers, dossier_id), "sharing_modification")["id"]

        await _generate(client, headers, document_id)

        assert (await _render(db_session, document_id)).watermark == "BROUILLON"

    async def test_a_draft_an_older_docgen_rendered_plain_never_lands(self, client, db_session):
        """An older worker ignores options it does not know: its "draft" would
        look final. No echo, no version."""
        world = await _world(db_session)
        headers = await _headers(db_session, world)
        dossier_id = await _file(world, "operation.meter_removed", EAN[0])
        document_id = _piece(await _get(client, headers, dossier_id), "sharing_modification")["id"]
        await _generate(client, headers, document_id)
        request = await _render(db_session, document_id)

        outcome = await _land(db_session, world, request, document_id, echo=False)

        assert outcome == docgen_results.RENDER_FAILED
        render = await db_session.scalar(
            select(DocumentRenderModel).where(DocumentRenderModel.id_document == document_id)
        )
        assert render is not None
        assert render.render_error_json["code"] == "WATERMARK_NOT_APPLIED"
        piece = await db_session.get(DocumentModel, document_id)
        await db_session.refresh(piece)
        assert piece.current_version_id is None

    async def test_a_collected_piece_renders_the_form_to_sign(
        self, client, db_session, admin_storage
    ):
        """Annex 8 is signed by the participant: pre-filled, unstamped, and the
        piece stays missing until the signed copy is uploaded."""
        world = await _world(db_session)
        headers = await _headers(db_session, world)
        dossier_id = await _file(world, "operation.meter_added", EAN[0])
        declaration = _piece(await _get(client, headers, dossier_id), "annex8_sworn_declaration")
        await _generate(client, headers, declaration["id"])
        request = await _render(db_session, declaration["id"])
        await _land(db_session, world, request, declaration["id"])

        assert request.watermark is None
        version = await _current(db_session, declaration["id"])
        assert version.stage == int(VersionStage.TO_SIGN)
        assert _piece(await _get(client, headers, dossier_id), "annex8_sworn_declaration")[
            "status"
        ] == int(ProcedureDocumentStatus.MISSING)

        response = await client.post(
            f"/admin/dossiers/{dossier_id}/documents/{declaration['id']}/versions",
            files={"file": ("signed.pdf", b"%PDF-1.4 signed", "application/pdf")},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        assert (await _current(db_session, declaration["id"])).stage == int(VersionStage.FINAL)
        assert _piece(response.json()["data"], "annex8_sworn_declaration")["status"] == int(
            ProcedureDocumentStatus.SIGNED
        )


class TestAPieceKeepsItsForm:
    async def _revise(self, db_session, doc_type: str) -> int:
        """A new version of the form comes into force: the old row closes."""
        await db_session.execute(
            text(
                "UPDATE document_template SET valid_to = CURRENT_DATE "
                "WHERE doc_type = :d AND valid_to IS NULL AND id_community IS NULL"
            ),
            {"d": doc_type},
        )
        template = DocumentTemplateModel(
            region=1,
            doc_type=doc_type,
            version=2,
            valid_from=TODAY,
            file_ref=f"s3://optimce-templates/administrative-document/{doc_type}/v2/",
            output_format="pdf",
        )
        db_session.add(template)
        await db_session.flush()
        return template.id

    async def test_it_is_pinned_when_it_is_born(self, client, db_session):
        world = await _world(db_session)
        headers = await _headers(db_session, world)
        dossier_id = await _file(world, "operation.meter_removed", EAN[0])
        document_id = _piece(await _get(client, headers, dossier_id), "sharing_modification")["id"]

        piece = await db_session.get(DocumentModel, document_id)
        in_force = await db_session.scalar(
            select(DocumentTemplateModel.id).where(
                DocumentTemplateModel.doc_type == "sharing_modification",
                DocumentTemplateModel.valid_to.is_(None),
            )
        )
        assert piece.id_template == in_force

    async def test_a_revised_form_does_not_move_it(self, client, db_session):
        world = await _world(db_session)
        headers = await _headers(db_session, world)
        dossier_id = await _file(world, "operation.meter_removed", EAN[0])
        document_id = _piece(await _get(client, headers, dossier_id), "sharing_modification")["id"]
        await self._revise(db_session, "sharing_modification")

        await _generate(client, headers, document_id)

        assert (await _render(db_session, document_id)).template_uri.endswith(
            "/sharing_modification/v1/"
        )

    async def test_an_explicit_upgrade_takes_the_form_in_force(self, client, db_session):
        world = await _world(db_session)
        headers = await _headers(db_session, world)
        dossier_id = await _file(world, "operation.meter_removed", EAN[0])
        document_id = _piece(await _get(client, headers, dossier_id), "sharing_modification")["id"]
        revised = await self._revise(db_session, "sharing_modification")

        await _generate(client, headers, document_id, upgrade_template=True)

        assert (await _render(db_session, document_id)).template_uri.endswith(
            "/sharing_modification/v2/"
        )
        piece = await db_session.get(DocumentModel, document_id)
        await db_session.refresh(piece)
        assert piece.id_template == revised


class TestFinals:
    async def test_each_draft_is_rendered_again_unstamped_from_its_own_snapshot(
        self, client, db_session
    ):
        world, headers, dossier_id, pieces = await _drafted(client, db_session)
        draft = await _current(db_session, pieces["annex6_sharing_form"])

        outcome = await _finalize(client, headers, dossier_id)
        request = await _render(db_session, pieces["annex6_sharing_form"])

        assert sorted(outcome["requested"]) == sorted(pieces.values())
        assert request.watermark is None
        assert request.data == draft.data_snapshot_json
        template = await db_session.get(DocumentTemplateModel, draft.id_template)
        assert request.template_uri == template.file_ref

        await _land(db_session, world, request, pieces["annex6_sharing_form"])
        final = await _current(db_session, pieces["annex6_sharing_form"])
        assert (final.stage, final.watermark) == (int(VersionStage.FINAL), None)
        assert final.data_snapshot_json == draft.data_snapshot_json
        assert final.original_filename == "form.pdf"

    async def test_a_piece_already_final_is_left_alone(self, client, db_session, admin_storage):
        world, headers, dossier_id, pieces = await _drafted(client, db_session)
        response = await client.post(
            f"/admin/dossiers/{dossier_id}/documents/{pieces['sharing_modification']}/versions",
            files={"file": ("form.pdf", b"%PDF-1.4 typed by hand", "application/pdf")},
            headers=headers,
        )
        assert response.status_code == 200, response.text

        outcome = await _finalize(client, headers, dossier_id)

        assert outcome == {"requested": [pieces["annex6_sharing_form"]], "in_flight": []}

    async def test_it_waits_for_complete_pieces(self, client, db_session):
        world = await _world(db_session)
        headers = await _headers(db_session, world)
        dossier_id = await _file(world, "operation.meter_removed", EAN[0])

        response = await client.post(f"/admin/dossiers/{dossier_id}/finalize", headers=headers)

        assert response.status_code == 409
        assert response.json()["error_code"] == 2386

    async def test_sending_waits_for_the_finals(self, client, db_session):
        world, headers, dossier_id, pieces = await _drafted(client, db_session)

        refused = await _send(client, headers, dossier_id)
        assert refused.status_code == 409
        assert refused.json()["error_code"] == 2385

        await _finals(client, db_session, world, headers, dossier_id, pieces)
        sent = await _send(client, headers, dossier_id)
        assert sent.status_code == 200, sent.text

    async def test_sending_waits_for_a_render_in_flight(self, client, db_session):
        world, headers, dossier_id, pieces = await _drafted(client, db_session)
        await _finalize(client, headers, dossier_id)
        await _land(
            db_session,
            world,
            await _render(db_session, pieces["sharing_modification"]),
            pieces["sharing_modification"],
        )  # the other one is still rendering

        response = await _send(client, headers, dossier_id)

        assert response.status_code == 409
        assert response.json()["error_code"] == 2385

    async def test_a_step_change_to_sent_by_hand_waits_too(self, client, db_session):
        _world_, headers, dossier_id, _pieces = await _drafted(client, db_session)

        response = await client.post(
            f"/admin/dossiers/{dossier_id}/transition",
            json={"to_step": "sent", "reason": "sent by post"},
            headers=headers,
        )

        assert response.status_code == 409
        assert response.json()["error_code"] == 2385

    async def test_a_render_landing_after_the_sending_is_dropped(self, client, db_session):
        world, headers, dossier_id, pieces = await _drafted(client, db_session)
        await _finalize(client, headers, dossier_id)
        request = await _render(db_session, pieces["annex6_sharing_form"])
        await db_session.execute(
            text("UPDATE dossier SET frozen_at = NOW() WHERE id = :id"), {"id": dossier_id}
        )

        outcome = await _land(db_session, world, request, pieces["annex6_sharing_form"])

        assert outcome == docgen_results.DROP
        assert (await _current(db_session, pieces["annex6_sharing_form"])).stage == int(
            VersionStage.DRAFT
        )


class TestOneClickRegeneration:
    async def test_a_stale_piece_comes_back_fresh_with_the_reviewers_edits(
        self, client, db_session
    ):
        world = await _world(db_session)
        headers = await _headers(db_session, world)
        dossier_id = await _file(world, "operation.meter_removed", EAN[0])
        document_id = _piece(await _get(client, headers, dossier_id), "sharing_modification")["id"]
        await _generate(client, headers, document_id, data={"representative_name": "Alice Dupont"})
        await _land(db_session, world, await _render(db_session, document_id), document_id)
        # The CRM moves on, and a second change outdates the form.
        await db_session.execute(
            text("UPDATE sharing_operation SET name = 'Op Centre-Ville' WHERE id = :id"),
            {"id": world.operation},
        )
        await _file(world, "operation.meter_removed", EAN[1])
        assert _piece(await _get(client, headers, dossier_id), "sharing_modification")[
            "status"
        ] == int(ProcedureDocumentStatus.STALE)

        response = await client.post(
            f"/admin/dossiers/{dossier_id}/regenerate", json={}, headers=headers
        )

        assert response.status_code == 202, response.text
        assert response.json()["data"]["requested"] == [document_id]
        request = await _render(db_session, document_id)
        assert request.data["representative_name"] == "Alice Dupont"  # typed: kept
        assert request.data["operation_name"] == "Op Centre-Ville"  # the CRM's: fresh
        assert request.watermark == MARK

    async def test_a_piece_whose_version_cannot_say_what_was_typed_needs_review(
        self, client, db_session, admin_storage
    ):
        world = await _world(db_session)
        headers = await _headers(db_session, world)
        dossier_id = await _file(world, "operation.meter_removed", EAN[0])
        document_id = _piece(await _get(client, headers, dossier_id), "sharing_modification")["id"]
        response = await client.post(
            f"/admin/dossiers/{dossier_id}/documents/{document_id}/versions",
            files={"file": ("form.pdf", b"%PDF-1.4 by hand", "application/pdf")},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        await _file(world, "operation.meter_removed", EAN[1])

        response = await client.post(
            f"/admin/dossiers/{dossier_id}/regenerate",
            json={"document_ids": [document_id]},
            headers=headers,
        )

        assert response.status_code == 202, response.text
        assert response.json()["data"] == {
            "requested": [],
            "needs_review": [document_id],
            "in_flight": [],
            "blocked": [],
        }

    async def test_a_piece_of_another_procedure_is_not_found(self, client, db_session):
        world = await _world(db_session)
        headers = await _headers(db_session, world)
        dossier_id = await _file(world, "operation.meter_removed", EAN[0])

        response = await client.post(
            f"/admin/dossiers/{dossier_id}/regenerate",
            json={"document_ids": [999_999]},
            headers=headers,
        )

        assert response.status_code == 404


class TestASentFinalRegeneratesIdentically:
    """Criterion 2.1: "Une version finale envoyée se régénère à l'identique
    depuis son snapshot". What the regeneration sends docgen is what the final
    was rendered from; the bytes are compared and the outcome kept."""

    async def _sent(self, client, db_session):
        world, headers, dossier_id, pieces = await _drafted(client, db_session)
        await _finalize(client, headers, dossier_id)
        originals = {}
        for document_id in pieces.values():
            originals[document_id] = await _render(db_session, document_id)
            await _land(
                db_session,
                world,
                originals[document_id],
                document_id,
                sha256=f"final-{document_id}",
            )
        assert (await _send(client, headers, dossier_id)).status_code == 200
        return world, headers, dossier_id, pieces, originals

    async def _verify(self, client, headers, dossier_id, document_id, version_id):
        return await client.post(
            f"/admin/dossiers/{dossier_id}/documents/{document_id}/versions/{version_id}/verify",
            headers=headers,
        )

    async def _checks(self, db_session, version_id: int) -> list[VersionCheckModel]:
        return list(
            await db_session.scalars(
                select(VersionCheckModel).where(VersionCheckModel.id_document_version == version_id)
            )
        )

    async def test_the_regeneration_asks_for_exactly_what_the_final_was_rendered_from(
        self, client, db_session
    ):
        world, headers, dossier_id, pieces, originals = await self._sent(client, db_session)
        document_id = pieces["annex6_sharing_form"]
        final = await _current(db_session, document_id)

        response = await self._verify(client, headers, dossier_id, document_id, final.id)
        assert response.status_code == 202, response.text
        again = await _render(db_session, document_id)

        original = originals[document_id]
        for field in ("template_uri", "output_format", "data", "watermark", "locale", "tenant_id"):
            assert getattr(again, field) == getattr(original, field), field
        assert again.request_id != original.request_id

    async def test_the_same_bytes_are_recorded_identical_and_no_version_is_made(
        self, client, db_session
    ):
        world, headers, dossier_id, pieces, _ = await self._sent(client, db_session)
        document_id = pieces["annex6_sharing_form"]
        final = await _current(db_session, document_id)
        await self._verify(client, headers, dossier_id, document_id, final.id)
        request = await _render(db_session, document_id)

        outcome = await _land(db_session, world, request, document_id, sha256=final.content_sha256)

        assert outcome == docgen_results.CHECKED
        (check,) = await self._checks(db_session, final.id)
        assert check.outcome == int(CheckOutcome.IDENTICAL)
        assert (await _current(db_session, document_id)).id == final.id
        versions = await db_session.scalar(
            text("SELECT count(*) FROM document_version WHERE id_document = :d"),
            {"d": document_id},
        )
        assert versions == 2  # the draft and the final, nothing more

    async def test_other_bytes_are_recorded_with_the_renderer_that_made_them(
        self, client, db_session
    ):
        world, headers, dossier_id, pieces, _ = await self._sent(client, db_session)
        document_id = pieces["sharing_modification"]
        final = await _current(db_session, document_id)
        await self._verify(client, headers, dossier_id, document_id, final.id)

        await _land(
            db_session, world, await _render(db_session, document_id), document_id, sha256="other"
        )

        (check,) = await self._checks(db_session, final.id)
        assert (check.outcome, check.content_sha256) == (int(CheckOutcome.DIFFERENT), "other")
        assert check.renderer_json is not None

    async def test_a_failed_regeneration_is_recorded_as_such(self, client, db_session):
        world, headers, dossier_id, pieces, _ = await self._sent(client, db_session)
        document_id = pieces["sharing_modification"]
        final = await _current(db_session, document_id)
        await self._verify(client, headers, dossier_id, document_id, final.id)
        request = await _render(db_session, document_id)

        outcome = await docgen_results.process_docgen_result(
            {
                "request_id": request.request_id,
                "status": "failed",
                "error": {"code": "TEMPLATE_NOT_FOUND", "message": "gone", "permanent": True},
                "generated_at": "2026-10-30T10:00:00Z",
                "metadata": {"document_id": document_id},
            },
            local_session=db_session,
            crm_session=db_session,
        )

        assert outcome == docgen_results.CHECKED
        (check,) = await self._checks(db_session, final.id)
        assert check.outcome == int(CheckOutcome.FAILED)
        assert check.error_json == {"code": "TEMPLATE_NOT_FOUND", "message": "gone"}

    async def test_a_redelivered_answer_is_recorded_once(self, client, db_session):
        world, headers, dossier_id, pieces, _ = await self._sent(client, db_session)
        document_id = pieces["sharing_modification"]
        final = await _current(db_session, document_id)
        await self._verify(client, headers, dossier_id, document_id, final.id)
        request = await _render(db_session, document_id)

        await _land(db_session, world, request, document_id, sha256=final.content_sha256)
        again = await _land(db_session, world, request, document_id, sha256=final.content_sha256)

        assert again == docgen_results.CHECKED
        assert len(await self._checks(db_session, final.id)) == 1

    async def test_an_upload_has_nothing_to_regenerate_from(
        self, client, db_session, admin_storage
    ):
        world, headers, dossier_id, pieces = await _drafted(client, db_session)
        response = await client.post(
            f"/admin/dossiers/{dossier_id}/documents/{pieces['sharing_modification']}/versions",
            files={"file": ("form.pdf", b"%PDF-1.4 by hand", "application/pdf")},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        uploaded = await _current(db_session, pieces["sharing_modification"])

        response = await self._verify(
            client, headers, dossier_id, pieces["sharing_modification"], uploaded.id
        )

        assert response.status_code == 409
        assert response.json()["error_code"] == 2387


class TestATemplateInUseKeepsItsBundle:
    async def test_changing_its_bundle_is_refused(self, client, db_session):
        world = await _world(db_session)
        headers = await _headers(db_session, world)
        # The community's own form (the only kind a PATCH may touch), in force
        # before the piece is born so that the piece is pinned to it.
        own = DocumentTemplateModel(
            id_community=world.id_community,
            region=1,
            doc_type="sharing_modification",
            version=1,
            valid_from=TODAY,
            file_ref="s3://optimce-templates/administrative-document/sharing_modification/v1/",
            output_format="pdf",
        )
        db_session.add(own)
        await db_session.flush()
        # A refused write rolls the session back, expiring every ORM object.
        own_id = own.id
        dossier_id = await _file(world, "operation.meter_removed", EAN[0])
        document_id = _piece(await _get(client, headers, dossier_id), "sharing_modification")["id"]
        await _generate(client, headers, document_id)
        await _land(db_session, world, await _render(db_session, document_id), document_id)
        admin = await _headers(db_session, world, role="ADMIN")

        refused = await client.patch(
            f"/templates/{own_id}",
            json={"file_ref": "s3://optimce-templates/elsewhere/"},
            headers=admin,
        )
        closed = await client.patch(
            f"/templates/{own_id}", json={"label": "replaced"}, headers=admin
        )

        assert refused.status_code == 409, refused.text
        assert refused.json()["error_code"] == 2389
        assert closed.status_code == 200, closed.text


class TestAKeyDescriptionNeedsAKey:
    """The key description describes the operation's key and nothing else: with
    no key, or a key with no iteration, the render is refused (2366) and the
    pre-fill says why; nothing is claimed nor published."""

    async def _piece_id(self, client, db_session):
        world = await _world(db_session)
        headers = await _headers(db_session, world)
        dossier_id = await _file(world, "operation.meter_removed", EAN[0])
        document_id = _piece(await _get(client, headers, dossier_id), "key_description")["id"]
        return world, headers, dossier_id, document_id

    @staticmethod
    async def _iterations(world, key_id: int) -> None:
        for number, share in ((1, 0.6), (2, 0.4)):
            iteration_id = (
                await world.session.execute(
                    text(
                        "INSERT INTO iteration (number, energy_allocated_percentage, id_key, "
                        "id_community) VALUES (:n, :s, :k, :c) RETURNING id"
                    ),
                    {"n": number, "s": share, "k": key_id, "c": world.id_community},
                )
            ).scalar_one()
            await world.session.execute(
                text(
                    "INSERT INTO consumer (name, energy_allocated_percentage, id_iteration, "
                    "id_community) VALUES ('Consumer 3', 1.0, :i, :c)"
                ),
                {"i": iteration_id, "c": world.id_community},
            )

    @staticmethod
    async def _no_render(db_session, document_id: int) -> None:
        render = await db_session.scalar(
            select(DocumentRenderModel).where(DocumentRenderModel.id_document == document_id)
        )
        assert render is None

    async def test_without_a_key_the_prefill_says_it_blocks(self, client, db_session):
        world, headers, _dossier_id, document_id = await self._piece_id(client, db_session)

        response = await client.get(f"/documents/{document_id}/prefill", headers=headers)

        assert response.status_code == 200, response.text
        (blocker,) = [w for w in response.json()["data"]["warnings"] if w["blocking"]]
        assert blocker["code"] == "key.missing"
        assert blocker["subject_type"] == "sharing_operation"
        assert blocker["subject_id"] == str(world.operation)

    async def test_without_a_key_the_render_is_refused(self, client, db_session):
        _world_, headers, _dossier_id, document_id = await self._piece_id(client, db_session)

        response = await client.post(
            f"/documents/{document_id}/generate", json={"data": {}}, headers=headers
        )

        assert response.status_code == 422, response.text
        assert response.json()["error_code"] == 2366
        await self._no_render(db_session, document_id)

    async def test_a_key_with_no_iteration_is_refused_and_named(self, client, db_session):
        world, headers, _dossier_id, document_id = await self._piece_id(client, db_session)
        key_id = await add_key(world, "Clé vide")

        prefilled = await client.get(f"/documents/{document_id}/prefill", headers=headers)
        response = await client.post(
            f"/documents/{document_id}/generate", json={"data": {}}, headers=headers
        )

        (blocker,) = [w for w in prefilled.json()["data"]["warnings"] if w["blocking"]]
        assert (blocker["code"], blocker["subject_id"]) == ("key.no_iteration", str(key_id))
        assert response.status_code == 422, response.text
        assert response.json()["error_code"] == 2366
        await self._no_render(db_session, document_id)

    async def test_a_key_with_iterations_renders(self, client, db_session):
        world, headers, _dossier_id, document_id = await self._piece_id(client, db_session)
        await self._iterations(world, await add_key(world, "Clé vent"))

        await _generate(client, headers, document_id)

        request = await _render(db_session, document_id)
        assert [row["part"] for row in request.data["iterations"]] == [60.0, 40.0]

    async def test_one_click_reports_a_piece_the_crm_emptied_as_blocked(self, client, db_session):
        world, headers, dossier_id, document_id = await self._piece_id(client, db_session)
        key_id = await add_key(world, "Clé vent")
        await self._iterations(world, key_id)
        await _generate(client, headers, document_id)
        await _land(db_session, world, await _render(db_session, document_id), document_id)
        await db_session.execute(text("DELETE FROM iteration WHERE id_key = :k"), {"k": key_id})

        response = await client.post(
            f"/admin/dossiers/{dossier_id}/regenerate",
            json={"document_ids": [document_id]},
            headers=headers,
        )

        assert response.status_code == 202, response.text
        assert response.json()["data"] == {
            "requested": [],
            "needs_review": [],
            "in_flight": [],
            "blocked": [document_id],
        }
        await self._no_render(db_session, document_id)
