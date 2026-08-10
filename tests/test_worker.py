"""The generation worker: request out, result in.

The interesting behaviour is not the happy path but the redelivery matrix — a
NATS consumer sees every message at least once, and a regulatory document must
never be attached twice or attributed to the wrong tenant.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from ports.document_generation import DocgenRequest
from tests.api.administrative_document.test_generation import register_bundle
from tests.api.administrative_document.test_transitions import make_document, make_dossier
from worker import docgen_results, generate

pytestmark = pytest.mark.asyncio

_URI = "s3://optimce-documents/administrative-document/1/1/req/annexe6-notification.xlsx"


class FakeDocGen:
    """Records what would have gone on the wire."""

    def __init__(self) -> None:
        self.requests: list[DocgenRequest] = []

    async def request_render(self, request: DocgenRequest) -> None:
        self.requests.append(request)


async def _document_with_pending_render(client, headers, db_session, sharing_operation):
    await register_bundle(db_session)
    dossier_id = await make_dossier(client, headers, sharing_operation)
    document_id = await make_document(client, headers, dossier_id)
    response = await client.post(
        f"/documents/{document_id}/generate", json={"data": {}}, headers=headers
    )
    assert response.status_code == 200, response.text
    return document_id, response.json()["data"]["docgen_request_id"]


def _success(request_id: str, *, document_id: int, tenant: int, uri: str = _URI) -> dict:
    """A body shaped exactly like ``GenerationResult.to_json_bytes()``.

    Note there is no top-level ``tenant_id``: the model is ``extra="forbid"`` and
    declares none, so it can only arrive through ``metadata``.
    """
    return {
        "request_id": request_id,
        "status": "success",
        "artifacts": [{"format": "xlsx", "uri": uri, "size_bytes": 4096, "sha256": "abc123"}],
        "template_version": "2026.02.25",
        "generated_at": "2026-07-26T10:00:00Z",
        "metadata": {"tenant_id": str(tenant), "document_id": document_id},
    }


class TestRequestSide:
    async def test_the_published_request_carries_what_the_result_will_need(
        self, client, manager_headers, db_session, community, sharing_operation
    ):
        document_id, request_id = await _document_with_pending_render(
            client, manager_headers, db_session, sharing_operation
        )
        fake = FakeDocGen()

        await generate.process_generate(document_id, doc_port=fake, local_session=db_session)

        assert len(fake.requests) == 1
        request = fake.requests[0]
        assert request.request_id == request_id
        assert request.template_uri.endswith("/annex6_notification/v1/")
        assert request.reply_to == "docgen.result.administrative_document"
        # tenant_id must ride in metadata: GenerationResult echoes nothing else.
        assert request.metadata["tenant_id"] == str(community.id)
        assert request.metadata["document_id"] == document_id

    async def test_the_object_key_is_derived_from_the_request_id(
        self, client, manager_headers, db_session, sharing_operation
    ):
        """That is what makes a redelivered request overwrite instead of litter."""
        document_id, request_id = await _document_with_pending_render(
            client, manager_headers, db_session, sharing_operation
        )
        fake = FakeDocGen()

        await generate.process_generate(document_id, doc_port=fake, local_session=db_session)
        await generate.process_generate(document_id, doc_port=fake, local_session=db_session)

        assert fake.requests[0].key_prefix == fake.requests[1].key_prefix
        assert request_id in fake.requests[0].key_prefix

    async def test_the_frozen_snapshot_is_what_is_sent(
        self, client, manager_headers, db_session, community, sharing_operation
    ):
        await register_bundle(db_session)
        dossier_id = await make_dossier(client, manager_headers, sharing_operation)
        document_id = await make_document(client, manager_headers, dossier_id)
        await client.post(
            f"/documents/{document_id}/generate",
            json={"data": {"community_legal_name": "Corrected ASBL"}},
            headers=manager_headers,
        )
        fake = FakeDocGen()

        await generate.process_generate(document_id, doc_port=fake, local_session=db_session)

        assert fake.requests[0].data["community_legal_name"] == "Corrected ASBL"

    async def test_nothing_is_published_when_the_render_already_landed(
        self, client, manager_headers, db_session, sharing_operation
    ):
        """The row is deleted on success, so a redelivery has nothing to do."""
        document_id, _ = await _document_with_pending_render(
            client, manager_headers, db_session, sharing_operation
        )
        await db_session.execute(
            text("DELETE FROM document_render WHERE id_document = :doc"), {"doc": document_id}
        )
        fake = FakeDocGen()

        published = await generate.process_generate(
            document_id, doc_port=fake, local_session=db_session
        )

        assert published is False
        assert fake.requests == []


class TestResultSide:
    async def test_a_success_becomes_the_current_version(
        self, client, manager_headers, db_session, community, sharing_operation
    ):
        document_id, request_id = await _document_with_pending_render(
            client, manager_headers, db_session, sharing_operation
        )

        outcome = await docgen_results.process_docgen_result(
            _success(request_id, document_id=document_id, tenant=community.id),
            local_session=db_session,
            crm_session=db_session,
        )

        assert outcome == docgen_results.ATTACHED
        detail = (await client.get(f"/documents/{document_id}", headers=manager_headers)).json()[
            "data"
        ]
        assert detail["current_version_id"] is not None
        # origin describes how the current version was produced.
        assert detail["origin"] == 1  # GENERATED
        version = detail["versions"][0]
        assert version["version_no"] == 1
        assert version["content_sha256"] == "abc123"
        assert version["id_template"] is not None

    async def test_the_artifacts_own_filename_is_recorded(
        self, client, manager_headers, db_session, community, sharing_operation
    ):
        """Without it the download arrives extensionless and will not open."""
        document_id, request_id = await _document_with_pending_render(
            client, manager_headers, db_session, sharing_operation
        )

        await docgen_results.process_docgen_result(
            _success(request_id, document_id=document_id, tenant=community.id),
            local_session=db_session,
            crm_session=db_session,
        )

        detail = (await client.get(f"/documents/{document_id}", headers=manager_headers)).json()[
            "data"
        ]
        # The bundle named it; the URI's last segment is that name.
        assert detail["versions"][0]["original_filename"] == "annexe6-notification.xlsx"

    async def test_an_upload_over_a_generated_document_flips_origin_back(
        self, client, manager_headers, db_session, community, sharing_operation, fake_storage
    ):
        document_id, request_id = await _document_with_pending_render(
            client, manager_headers, db_session, sharing_operation
        )
        await docgen_results.process_docgen_result(
            _success(request_id, document_id=document_id, tenant=community.id),
            local_session=db_session,
            crm_session=db_session,
        )

        await client.post(
            f"/documents/{document_id}/versions",
            files={"file": ("manual.pdf", b"a hand-corrected filing", "application/pdf")},
            headers=manager_headers,
        )

        detail = (await client.get(f"/documents/{document_id}", headers=manager_headers)).json()[
            "data"
        ]
        assert detail["origin"] == 2  # UPLOADED

    async def test_the_snapshot_travels_onto_the_version(
        self, client, manager_headers, db_session, community, sharing_operation
    ):
        """Spec R2: the filed version records the state at generation time — and
        the result echoes only metadata, so it can only come from the render row."""
        await register_bundle(db_session)
        dossier_id = await make_dossier(client, manager_headers, sharing_operation)
        document_id = await make_document(client, manager_headers, dossier_id)
        request_id = (
            await client.post(
                f"/documents/{document_id}/generate",
                json={"data": {"community_legal_name": "Filed As This"}},
                headers=manager_headers,
            )
        ).json()["data"]["docgen_request_id"]

        await docgen_results.process_docgen_result(
            _success(request_id, document_id=document_id, tenant=community.id),
            local_session=db_session,
            crm_session=db_session,
        )

        snapshot = (
            await db_session.execute(
                text("SELECT data_snapshot_json FROM document_version " "WHERE id_document = :doc"),
                {"doc": document_id},
            )
        ).scalar_one()
        assert snapshot["community_legal_name"] == "Filed As This"

    async def test_success_clears_the_render_slot(
        self, client, manager_headers, db_session, community, sharing_operation
    ):
        document_id, request_id = await _document_with_pending_render(
            client, manager_headers, db_session, sharing_operation
        )

        await docgen_results.process_docgen_result(
            _success(request_id, document_id=document_id, tenant=community.id),
            local_session=db_session,
            crm_session=db_session,
        )

        status = (
            await client.get(f"/documents/{document_id}/render-status", headers=manager_headers)
        ).json()["data"]
        assert status["render_state"] is None

    async def test_generating_does_not_change_the_document_status(
        self, client, manager_headers, db_session, community, sharing_operation
    ):
        """A render is technical. Journaling it would corrupt the status cache."""
        document_id, request_id = await _document_with_pending_render(
            client, manager_headers, db_session, sharing_operation
        )

        await docgen_results.process_docgen_result(
            _success(request_id, document_id=document_id, tenant=community.id),
            local_session=db_session,
            crm_session=db_session,
        )

        detail = (await client.get(f"/documents/{document_id}", headers=manager_headers)).json()[
            "data"
        ]
        assert detail["status"] == 1  # DRAFT

    async def test_a_redelivered_success_does_not_attach_twice(
        self, client, manager_headers, db_session, community, sharing_operation
    ):
        """JetStream delivers at least once; a second version would be a forged
        record of a filing that never happened."""
        document_id, request_id = await _document_with_pending_render(
            client, manager_headers, db_session, sharing_operation
        )
        body = _success(request_id, document_id=document_id, tenant=community.id)

        first = await docgen_results.process_docgen_result(
            body, local_session=db_session, crm_session=db_session
        )
        second = await docgen_results.process_docgen_result(
            body, local_session=db_session, crm_session=db_session
        )

        assert (first, second) == (docgen_results.ATTACHED, docgen_results.ATTACHED)
        count = (
            await db_session.execute(
                text("SELECT COUNT(*) FROM document_version WHERE id_document = :doc"),
                {"doc": document_id},
            )
        ).scalar_one()
        assert count == 1

    async def test_a_permanent_failure_is_recorded_and_acked(
        self, client, manager_headers, db_session, community, sharing_operation
    ):
        document_id, request_id = await _document_with_pending_render(
            client, manager_headers, db_session, sharing_operation
        )

        outcome = await docgen_results.process_docgen_result(
            {
                "request_id": request_id,
                "status": "failed",
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": "participants: too many items",
                    "permanent": True,
                },
                "generated_at": "2026-07-26T10:00:00Z",
                "metadata": {"tenant_id": str(community.id), "document_id": document_id},
            },
            local_session=db_session,
            crm_session=db_session,
        )

        assert outcome == docgen_results.RENDER_FAILED
        status = (
            await client.get(f"/documents/{document_id}/render-status", headers=manager_headers)
        ).json()["data"]
        assert status["render_state"] == 2  # FAILED
        assert status["render_error"]["code"] == "VALIDATION_ERROR"

    async def test_a_transient_failure_asks_for_redelivery(
        self, client, manager_headers, db_session, community, sharing_operation
    ):
        document_id, request_id = await _document_with_pending_render(
            client, manager_headers, db_session, sharing_operation
        )

        outcome = await docgen_results.process_docgen_result(
            {
                "request_id": request_id,
                "status": "failed",
                "error": {"code": "STORAGE_ERROR", "message": "s3 5xx", "permanent": False},
                "generated_at": "2026-07-26T10:00:00Z",
                "metadata": {"tenant_id": str(community.id), "document_id": document_id},
            },
            local_session=db_session,
            crm_session=db_session,
        )

        assert outcome == docgen_results.TRANSIENT

    async def test_a_success_without_an_artifact_is_transient(
        self, client, manager_headers, db_session, community, sharing_operation
    ):
        """ "Succeeded" with nothing to attach is a docgen bug, not a user error."""
        document_id, request_id = await _document_with_pending_render(
            client, manager_headers, db_session, sharing_operation
        )
        body = _success(request_id, document_id=document_id, tenant=community.id)
        body["artifacts"] = []

        outcome = await docgen_results.process_docgen_result(
            body, local_session=db_session, crm_session=db_session
        )

        assert outcome == docgen_results.TRANSIENT

    async def test_an_unknown_request_is_dropped(self, db_session):
        outcome = await docgen_results.process_docgen_result(
            _success("never-issued", document_id=1, tenant=1),
            local_session=db_session,
            crm_session=db_session,
        )
        assert outcome == docgen_results.DROP

    async def test_a_result_naming_the_wrong_document_is_refused(
        self, client, manager_headers, db_session, community, sharing_operation
    ):
        """metadata is data that round-tripped through another service; a
        mismatch means a replay or a bug, never something to act on."""
        document_id, request_id = await _document_with_pending_render(
            client, manager_headers, db_session, sharing_operation
        )
        body = _success(request_id, document_id=document_id + 999, tenant=community.id)

        outcome = await docgen_results.process_docgen_result(
            body, local_session=db_session, crm_session=db_session
        )

        assert outcome == docgen_results.DROP
        remaining = (
            await db_session.execute(
                text("SELECT COUNT(*) FROM document_version WHERE id_document = :doc"),
                {"doc": document_id},
            )
        ).scalar_one()
        assert remaining == 0

    async def test_the_tenant_comes_from_the_database_not_the_message(
        self, client, manager_headers, db_session, community, other_community, sharing_operation
    ):
        """A forged tenant_id must not move a document into another community."""
        document_id, request_id = await _document_with_pending_render(
            client, manager_headers, db_session, sharing_operation
        )
        body = _success(request_id, document_id=document_id, tenant=other_community.id)

        outcome = await docgen_results.process_docgen_result(
            body, local_session=db_session, crm_session=db_session
        )

        assert outcome == docgen_results.ATTACHED
        version = (
            await db_session.execute(
                text("SELECT id_community FROM document_version WHERE id_document = :doc"),
                {"doc": document_id},
            )
        ).scalar_one()
        assert version == community.id  # the row's community, not the message's

    async def test_a_body_without_a_request_id_is_dropped(self, db_session):
        outcome = await docgen_results.process_docgen_result(
            {"status": "success", "artifacts": []},
            local_session=db_session,
            crm_session=db_session,
        )
        assert outcome == docgen_results.DROP


class TestRegeneration:
    async def test_a_second_render_becomes_version_two(
        self, client, manager_headers, db_session, community, sharing_operation
    ):
        document_id, first_request = await _document_with_pending_render(
            client, manager_headers, db_session, sharing_operation
        )
        await docgen_results.process_docgen_result(
            _success(first_request, document_id=document_id, tenant=community.id),
            local_session=db_session,
            crm_session=db_session,
        )
        second_request = (
            await client.post(
                f"/documents/{document_id}/generate",
                json={"data": {"community_legal_name": "Second Pass"}},
                headers=manager_headers,
            )
        ).json()["data"]["docgen_request_id"]

        await docgen_results.process_docgen_result(
            _success(
                second_request,
                document_id=document_id,
                tenant=community.id,
                uri=_URI.replace("/req/", "/req2/"),
            ),
            local_session=db_session,
            crm_session=db_session,
        )

        versions = (
            await db_session.execute(
                text(
                    "SELECT version_no, data_snapshot_json FROM document_version "
                    "WHERE id_document = :doc ORDER BY version_no"
                ),
                {"doc": document_id},
            )
        ).all()
        assert [v[0] for v in versions] == [1, 2]
        # The point of the snapshot: v1 still shows what v1 was filed with.
        assert versions[0][1]["community_legal_name"] != "Second Pass"
        assert versions[1][1]["community_legal_name"] == "Second Pass"

    async def test_the_first_versions_bytes_are_never_repointed(
        self, client, manager_headers, db_session, community, sharing_operation
    ):
        document_id, first_request = await _document_with_pending_render(
            client, manager_headers, db_session, sharing_operation
        )
        await docgen_results.process_docgen_result(
            _success(first_request, document_id=document_id, tenant=community.id),
            local_session=db_session,
            crm_session=db_session,
        )

        stored = (
            await db_session.execute(
                text("SELECT file_ref FROM document_version WHERE id_document = :doc"),
                {"doc": document_id},
            )
        ).scalar_one()
        assert stored == _URI


class TestModelWiring:
    async def test_a_render_row_and_its_version_share_the_request_id(
        self, client, manager_headers, db_session, community, sharing_operation
    ):
        """Without this link, "already handled" and "stale" are indistinguishable."""
        document_id, request_id = await _document_with_pending_render(
            client, manager_headers, db_session, sharing_operation
        )
        render = (
            await db_session.execute(
                text("SELECT docgen_request_id FROM document_render WHERE id_document = :doc"),
                {"doc": document_id},
            )
        ).scalar_one()
        assert render == request_id

        await docgen_results.process_docgen_result(
            _success(request_id, document_id=document_id, tenant=community.id),
            local_session=db_session,
            crm_session=db_session,
        )

        version = (
            await db_session.execute(
                text("SELECT docgen_request_id FROM document_version WHERE id_document = :doc"),
                {"doc": document_id},
            )
        ).scalar_one()
        assert version == request_id
