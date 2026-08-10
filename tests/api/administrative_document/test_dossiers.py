"""Dossier CRUD, access control, and tenant isolation."""

import pytest

pytestmark = pytest.mark.asyncio


async def _create_dossier(client, headers, sharing_operation=None, **overrides):
    payload = {"dossier_type": 1, "title": "Notification", **overrides}
    if sharing_operation is not None:
        payload["id_sharing_operation"] = sharing_operation
    return await client.post("/dossiers", json=payload, headers=headers)


class TestCreateDossier:
    async def test_creates_in_preparation_with_the_region_from_the_community(
        self, client, manager_headers, sharing_operation
    ):
        response = await _create_dossier(client, manager_headers, sharing_operation)
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["status"] == 1  # IN_PREPARATION
        assert data["region"] == 1  # WAL, resolved from community.regulator
        assert data["dossier_type"] == 1
        assert data["id_sharing_operation"] == sharing_operation
        assert data["submitted_at"] is None

    async def test_metadata_round_trips(self, client, manager_headers, sharing_operation):
        response = await _create_dossier(
            client, manager_headers, sharing_operation, metadata={"k": "v"}
        )
        assert response.json()["data"]["metadata"] == {"k": "v"}

    async def test_duplicate_external_ref_for_the_same_type_is_rejected(
        self, client, manager_headers, sharing_operation
    ):
        first = await _create_dossier(
            client, manager_headers, sharing_operation, external_ref="CWAPE-1"
        )
        assert first.status_code == 200
        second = await _create_dossier(
            client, manager_headers, sharing_operation, external_ref="CWAPE-1"
        )
        assert second.status_code == 409

    async def test_same_external_ref_on_a_different_dossier_type_is_allowed(
        self, client, manager_headers, sharing_operation
    ):
        await _create_dossier(
            client, manager_headers, sharing_operation, external_ref="REF-2", dossier_type=1
        )
        other = await _create_dossier(
            client, manager_headers, sharing_operation, external_ref="REF-2", dossier_type=2
        )
        assert other.status_code == 200


class TestSharingOperationScoping:
    """Every dossier is filed for exactly one sharing operation."""

    async def test_creating_without_an_operation_is_rejected(self, client, manager_headers):
        response = await _create_dossier(client, manager_headers)
        assert response.status_code == 422

    async def test_an_unknown_operation_is_rejected(self, client, manager_headers):
        response = await _create_dossier(client, manager_headers, 999999)
        assert response.status_code == 422

    async def test_another_communitys_operation_cannot_be_attached(
        self, client, manager_headers, other_sharing_operation
    ):
        """The operation lookup is community-scoped, so this reads as unknown."""
        response = await _create_dossier(client, manager_headers, other_sharing_operation)
        assert response.status_code == 422

    async def test_a_community_files_one_dossier_per_operation(
        self, client, manager_headers, sharing_operation, second_sharing_operation
    ):
        first = await _create_dossier(client, manager_headers, sharing_operation)
        second = await _create_dossier(client, manager_headers, second_sharing_operation)
        assert first.status_code == 200
        assert second.status_code == 200
        assert (
            first.json()["data"]["id_sharing_operation"]
            != second.json()["data"]["id_sharing_operation"]
        )

    async def test_list_can_be_filtered_to_one_operation(
        self, client, manager_headers, sharing_operation, second_sharing_operation
    ):
        await _create_dossier(client, manager_headers, sharing_operation, external_ref="OP1")
        await _create_dossier(client, manager_headers, second_sharing_operation, external_ref="OP2")

        response = await client.get(
            f"/dossiers?id_sharing_operation={sharing_operation}", headers=manager_headers
        )
        rows = response.json()["data"]
        assert [r["id_sharing_operation"] for r in rows] == [sharing_operation]

    async def test_the_operation_is_fixed_at_creation(
        self, client, manager_headers, sharing_operation, second_sharing_operation
    ):
        """Re-pointing a filed dossier would invalidate everything recorded on it."""
        created = await _create_dossier(client, manager_headers, sharing_operation)
        dossier_id = created.json()["data"]["id"]

        await client.patch(
            f"/dossiers/{dossier_id}",
            json={"id_sharing_operation": second_sharing_operation},
            headers=manager_headers,
        )
        after = await client.get(f"/dossiers/{dossier_id}", headers=manager_headers)
        assert after.json()["data"]["id_sharing_operation"] == sharing_operation


class TestSharingOperationsEndpoint:
    async def test_lists_the_communitys_operations(
        self, client, manager_headers, sharing_operation, second_sharing_operation
    ):
        response = await client.get("/sharing-operations", headers=manager_headers)
        assert response.status_code == 200
        ids = {row["id"] for row in response.json()["data"]}
        assert ids == {sharing_operation, second_sharing_operation}

    async def test_does_not_leak_another_communitys_operations(
        self, client, manager_headers, sharing_operation, other_sharing_operation
    ):
        response = await client.get("/sharing-operations", headers=manager_headers)
        ids = {row["id"] for row in response.json()["data"]}
        assert other_sharing_operation not in ids

    async def test_member_cannot_read_the_operations(
        self, client, member_headers, sharing_operation
    ):
        # Inverted, not deleted. This asserted 200 until the router's reads were
        # gated: `minRole MANAGER` in the annexe catalogue is frontend metadata,
        # so the API was the only boundary and it was open. A member's way into
        # this service is `/filings/mine` and nothing else.
        response = await client.get("/sharing-operations", headers=member_headers)
        assert response.status_code == 403


class TestAccessControl:
    async def test_member_cannot_create_a_dossier(self, client, member_headers, sharing_operation):
        response = await _create_dossier(client, member_headers, sharing_operation)
        assert response.status_code == 403

    async def test_member_cannot_read_dossiers(
        self, client, manager_headers, member_headers, sharing_operation
    ):
        await _create_dossier(client, manager_headers, sharing_operation)
        response = await client.get("/dossiers", headers=member_headers)
        assert response.status_code == 403

    async def test_member_cannot_transition_a_dossier(
        self, client, manager_headers, member_headers, sharing_operation
    ):
        created = await _create_dossier(client, manager_headers, sharing_operation)
        dossier_id = created.json()["data"]["id"]
        response = await client.post(
            f"/dossiers/{dossier_id}/transition",
            json={"to_status": 2, "context": {"submission_date": "2026-09-07"}},
            headers=member_headers,
        )
        assert response.status_code == 403

    async def test_request_without_gateway_headers_is_rejected(self, client):
        response = await client.get("/dossiers")
        assert response.status_code in (401, 403)


class TestTenantIsolation:
    async def test_another_community_cannot_read_the_dossier(
        self, client, manager_headers, other_manager_headers, sharing_operation
    ):
        created = await _create_dossier(client, manager_headers, sharing_operation)
        dossier_id = created.json()["data"]["id"]

        response = await client.get(f"/dossiers/{dossier_id}", headers=other_manager_headers)
        assert response.status_code == 404

    async def test_another_community_sees_an_empty_list(
        self, client, manager_headers, other_manager_headers, sharing_operation
    ):
        await _create_dossier(client, manager_headers, sharing_operation)
        response = await client.get("/dossiers", headers=other_manager_headers)
        assert response.json()["data"] == []


class TestListAndUpdate:
    async def test_list_is_paginated(self, client, manager_headers, sharing_operation):
        for index in range(3):
            await _create_dossier(
                client, manager_headers, sharing_operation, external_ref=f"R{index}"
            )
        response = await client.get("/dossiers?page=1&limit=2", headers=manager_headers)
        body = response.json()
        assert len(body["data"]) == 2
        assert body["pagination"]["total"] == 3
        assert body["pagination"]["total_pages"] == 2

    async def test_filter_by_type(self, client, manager_headers, sharing_operation):
        await _create_dossier(client, manager_headers, sharing_operation, dossier_type=1)
        await _create_dossier(client, manager_headers, sharing_operation, dossier_type=2)
        response = await client.get("/dossiers?dossier_type=2", headers=manager_headers)
        assert [d["dossier_type"] for d in response.json()["data"]] == [2]

    async def test_unknown_sort_column_falls_back_instead_of_erroring(
        self, client, manager_headers, sharing_operation
    ):
        """The sort string is an allow-list lookup, never interpolated SQL."""
        await _create_dossier(client, manager_headers, sharing_operation)
        response = await client.get(
            "/dossiers?sort=id;DROP TABLE dossier--", headers=manager_headers
        )
        assert response.status_code == 200

    async def test_patch_updates_reference_but_not_status(
        self, client, manager_headers, sharing_operation
    ):
        created = await _create_dossier(client, manager_headers, sharing_operation)
        dossier_id = created.json()["data"]["id"]
        response = await client.patch(
            f"/dossiers/{dossier_id}",
            json={"external_ref": "CWAPE-2026-42", "title": "Renamed"},
            headers=manager_headers,
        )
        data = response.json()["data"]
        assert data["external_ref"] == "CWAPE-2026-42"
        assert data["title"] == "Renamed"
        assert data["status"] == 1  # unchanged: status only moves via /transition

    async def test_get_unknown_dossier_is_404(self, client, manager_headers):
        assert (await client.get("/dossiers/999999", headers=manager_headers)).status_code == 404
