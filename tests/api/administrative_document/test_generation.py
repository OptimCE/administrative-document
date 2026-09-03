"""Prepare → review → generate (spec R2, user stories 1/4/5).

The promise is that a mandated form comes back pre-filled from the CRM, that the
reviewer's corrections are what gets filed, and that the exact payload behind a
filed version is frozen at generation time.
"""

import pytest
from sqlalchemy import text

from tests.api.administrative_document.test_transitions import make_document, make_dossier
from tests.factories import crm_participant_factory as f

pytestmark = pytest.mark.asyncio

PENDING, FAILED = 1, 2
_BUNDLE = "s3://optimce-templates/administrative-document/annex6_notification/v1/"


async def register_bundle(db_session, *, doc_type="annex6_notification", uri=_BUNDLE) -> None:
    """Give the in-force template a bundle URI, as seed 0003 does in production."""
    await db_session.execute(
        text(
            "UPDATE document_template SET file_ref = :uri "
            "WHERE doc_type = :doc_type AND valid_to IS NULL"
        ),
        {"uri": uri, "doc_type": doc_type},
    )
    await db_session.flush()


async def seed_participants(db_session, community, sharing_operation) -> None:
    home = await f.create_address(db_session, street="Rue Basse", number="3")
    alice = await f.create_member(
        db_session,
        id_community=community.id,
        name="Dupont",
        member_type=1,
        first_name="Alice",
        id_home_address=home,
    )
    await f.create_meter(db_session, ean="541448000000000001", id_community=community.id)
    await f.attribute_meter(
        db_session,
        ean="541448000000000001",
        id_community=community.id,
        id_sharing_operation=sharing_operation,
        id_member=alice,
        grd="ORES",
    )


class TestPrefill:
    async def test_prefill_returns_the_crm_state_without_persisting(
        self, client, manager_headers, db_session, community, sharing_operation
    ):
        await seed_participants(db_session, community, sharing_operation)
        dossier_id = await make_dossier(client, manager_headers, sharing_operation)
        document_id = await make_document(client, manager_headers, dossier_id)

        response = await client.get(f"/documents/{document_id}/prefill", headers=manager_headers)

        assert response.status_code == 200, response.text
        data = response.json()["data"]["data"]
        assert data["community_name"] == community.name
        assert [m["nom"] for m in data["members"]] == ["Alice Dupont"]

        # Nothing was written: a reviewer may open the form as often as they like.
        status = await client.get(
            f"/documents/{document_id}/render-status", headers=manager_headers
        )
        assert status.json()["data"]["render_state"] is None

    async def test_an_unowned_meter_is_surfaced_as_a_warning(
        self, client, manager_headers, db_session, community, sharing_operation
    ):
        await f.create_meter(db_session, ean="541448000000000007", id_community=community.id)
        await f.attribute_meter(
            db_session,
            ean="541448000000000007",
            id_community=community.id,
            id_sharing_operation=sharing_operation,
            id_member=None,
        )
        dossier_id = await make_dossier(client, manager_headers, sharing_operation)
        document_id = await make_document(client, manager_headers, dossier_id)

        response = await client.get(f"/documents/{document_id}/prefill", headers=manager_headers)

        warnings = response.json()["data"]["warnings"]
        meter_warnings = [w for w in warnings if w["subject_type"] == "meter"]
        # Structured so the dialog can link to the meter rather than describing it.
        assert meter_warnings == [
            {
                "code": "meter.no_member_attribution",
                "subject_type": "meter",
                "subject_id": "541448000000000007",
                "params": {},
            }
        ]

    async def test_a_member_cannot_prefill(
        self, client, manager_headers, member_headers, sharing_operation
    ):
        dossier_id = await make_dossier(client, manager_headers, sharing_operation)
        document_id = await make_document(client, manager_headers, dossier_id)

        response = await client.get(f"/documents/{document_id}/prefill", headers=member_headers)

        assert response.status_code == 403


class TestGenerate:
    async def test_generate_accepts_and_reports_pending(
        self, client, manager_headers, db_session, community, sharing_operation
    ):
        await register_bundle(db_session)
        await seed_participants(db_session, community, sharing_operation)
        dossier_id = await make_dossier(client, manager_headers, sharing_operation)
        document_id = await make_document(client, manager_headers, dossier_id)

        response = await client.post(
            f"/documents/{document_id}/generate", json={"data": {}}, headers=manager_headers
        )

        assert response.status_code == 200, response.text
        assert response.json()["data"]["render_state"] == PENDING
        assert response.json()["data"]["docgen_request_id"]

        status = await client.get(
            f"/documents/{document_id}/render-status", headers=manager_headers
        )
        assert status.json()["data"]["render_state"] == PENDING

    async def test_the_reviewers_correction_is_what_gets_frozen(
        self, client, manager_headers, db_session, community, sharing_operation
    ):
        """This is the whole point of review-and-edit: the CRM value loses."""
        await register_bundle(db_session)
        await seed_participants(db_session, community, sharing_operation)
        dossier_id = await make_dossier(client, manager_headers, sharing_operation)
        document_id = await make_document(client, manager_headers, dossier_id)

        await client.post(
            f"/documents/{document_id}/generate",
            json={"data": {"community_legal_name": "Corrected ASBL"}},
            headers=manager_headers,
        )

        snapshot = (
            await db_session.execute(
                text("SELECT data_snapshot_json FROM document_render WHERE id_document = :doc"),
                {"doc": document_id},
            )
        ).scalar_one()
        assert snapshot["community_legal_name"] == "Corrected ASBL"
        # Untouched keys still come from the CRM.
        assert snapshot["community_name"] == community.name

    async def test_a_submitted_list_replaces_the_derived_one(
        self, client, manager_headers, db_session, community, sharing_operation
    ):
        """A participant the reviewer removed must not reappear in the filing."""
        await register_bundle(db_session)
        await seed_participants(db_session, community, sharing_operation)
        dossier_id = await make_dossier(client, manager_headers, sharing_operation)
        document_id = await make_document(client, manager_headers, dossier_id)

        await client.post(
            f"/documents/{document_id}/generate",
            json={"data": {"members": []}},
            headers=manager_headers,
        )

        snapshot = (
            await db_session.execute(
                text("SELECT data_snapshot_json FROM document_render WHERE id_document = :doc"),
                {"doc": document_id},
            )
        ).scalar_one()
        assert snapshot["members"] == []

    async def test_a_second_generate_while_pending_is_refused(
        self, client, manager_headers, db_session, community, sharing_operation
    ):
        await register_bundle(db_session)
        dossier_id = await make_dossier(client, manager_headers, sharing_operation)
        document_id = await make_document(client, manager_headers, dossier_id)

        first = await client.post(
            f"/documents/{document_id}/generate", json={"data": {}}, headers=manager_headers
        )
        second = await client.post(
            f"/documents/{document_id}/generate", json={"data": {}}, headers=manager_headers
        )

        assert first.status_code == 200
        assert second.status_code == 409
        assert second.json()["error_code"] == 2364

    async def test_a_failed_render_can_be_retried_immediately(
        self, client, manager_headers, db_session, community, sharing_operation
    ):
        """A FAILED render is not "in flight"; refusing a retry would strand it."""
        await register_bundle(db_session)
        dossier_id = await make_dossier(client, manager_headers, sharing_operation)
        document_id = await make_document(client, manager_headers, dossier_id)
        await client.post(
            f"/documents/{document_id}/generate", json={"data": {}}, headers=manager_headers
        )
        await db_session.execute(
            text("UPDATE document_render SET render_state = 2 WHERE id_document = :doc"),
            {"doc": document_id},
        )
        await db_session.flush()

        retry = await client.post(
            f"/documents/{document_id}/generate", json={"data": {}}, headers=manager_headers
        )

        assert retry.status_code == 200, retry.text

    async def test_a_sent_document_cannot_be_regenerated(
        self, client, manager_headers, db_session, sharing_operation
    ):
        """Same rule as upload: roll back first, so the correction is traced."""
        await register_bundle(db_session)
        dossier_id = await make_dossier(client, manager_headers, sharing_operation)
        document_id = await make_document(client, manager_headers, dossier_id)
        await client.post(f"/documents/{document_id}/mark-ready", headers=manager_headers)
        await client.post(
            f"/documents/{document_id}/mark-sent",
            json={"submission_date": "2026-07-01"},
            headers=manager_headers,
        )

        response = await client.post(
            f"/documents/{document_id}/generate", json={"data": {}}, headers=manager_headers
        )

        assert response.status_code == 409
        assert response.json()["error_code"] == 2360

    async def test_a_doc_type_without_a_bundle_is_refused(
        self, client, manager_headers, db_session, sharing_operation
    ):
        """A form can be registered without being renderable.

        Every Walloon doc_type ships a bundle today, so the state is created
        here rather than borrowed from the catalogue — which also keeps the test
        honest if a future form is registered ahead of its bundle.
        """
        await db_session.execute(
            text(
                "UPDATE document_template SET file_ref = NULL "
                "WHERE doc_type = :doc_type AND valid_to IS NULL"
            ),
            {"doc_type": "dso_agreement_community"},
        )
        await db_session.flush()

        dossier_id = await make_dossier(client, manager_headers, sharing_operation)
        document_id = await make_document(
            client, manager_headers, dossier_id, doc_type="dso_agreement_community"
        )

        response = await client.post(
            f"/documents/{document_id}/generate", json={"data": {}}, headers=manager_headers
        )

        assert response.status_code == 422
        assert response.json()["error_code"] == 2361

    async def test_an_unregistered_doc_type_is_refused(
        self, client, manager_headers, sharing_operation
    ):
        """A doc_type with no catalogue row at all fails the same way."""
        dossier_id = await make_dossier(client, manager_headers, sharing_operation)
        document_id = await make_document(
            client, manager_headers, dossier_id, doc_type="not_a_real_form"
        )

        response = await client.post(
            f"/documents/{document_id}/generate", json={"data": {}}, headers=manager_headers
        )

        assert response.status_code == 422
        assert response.json()["error_code"] == 2361

    async def test_generating_does_not_move_the_document_status(
        self, client, manager_headers, db_session, sharing_operation
    ):
        """A render is technical; only a human marks a document READY."""
        await register_bundle(db_session)
        dossier_id = await make_dossier(client, manager_headers, sharing_operation)
        document_id = await make_document(client, manager_headers, dossier_id)

        await client.post(
            f"/documents/{document_id}/generate", json={"data": {}}, headers=manager_headers
        )

        document = await client.get(f"/documents/{document_id}", headers=manager_headers)
        assert document.json()["data"]["status"] == 1  # still DRAFT

    async def test_a_member_cannot_generate(
        self, client, manager_headers, member_headers, db_session, sharing_operation
    ):
        await register_bundle(db_session)
        dossier_id = await make_dossier(client, manager_headers, sharing_operation)
        document_id = await make_document(client, manager_headers, dossier_id)

        response = await client.post(
            f"/documents/{document_id}/generate", json={"data": {}}, headers=member_headers
        )

        assert response.status_code == 403


class TestRenderStatus:
    async def test_status_is_idle_before_any_generation(
        self, client, manager_headers, sharing_operation
    ):
        dossier_id = await make_dossier(client, manager_headers, sharing_operation)
        document_id = await make_document(client, manager_headers, dossier_id)

        response = await client.get(
            f"/documents/{document_id}/render-status", headers=manager_headers
        )

        data = response.json()["data"]
        assert data["render_state"] is None
        assert data["current_version_id"] is None

    async def test_a_failure_is_reported_with_its_error(
        self, client, manager_headers, db_session, sharing_operation
    ):
        await register_bundle(db_session)
        dossier_id = await make_dossier(client, manager_headers, sharing_operation)
        document_id = await make_document(client, manager_headers, dossier_id)
        await client.post(
            f"/documents/{document_id}/generate", json={"data": {}}, headers=manager_headers
        )
        await db_session.execute(
            text(
                "UPDATE document_render SET render_state = 2, render_error_json = "
                '\'{"code": "VALIDATION_ERROR", "message": "missing field"}\'::jsonb '
                "WHERE id_document = :doc"
            ),
            {"doc": document_id},
        )
        await db_session.flush()

        response = await client.get(
            f"/documents/{document_id}/render-status", headers=manager_headers
        )

        data = response.json()["data"]
        assert data["render_state"] == FAILED
        assert data["render_error"]["code"] == "VALIDATION_ERROR"

    async def test_another_communitys_document_is_not_visible(
        self, client, manager_headers, other_manager_headers, sharing_operation
    ):
        dossier_id = await make_dossier(client, manager_headers, sharing_operation)
        document_id = await make_document(client, manager_headers, dossier_id)

        response = await client.get(
            f"/documents/{document_id}/render-status", headers=other_manager_headers
        )

        assert response.status_code == 404
