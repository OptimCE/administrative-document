"""Versioned document storage (spec R2/R4).

The promise is that the exact bytes filed with the authority can always be
retrieved, and that a later edit never rewrites what was already sent.
"""

import hashlib

import pytest

from tests.api.administrative_document.test_transitions import make_document, make_dossier

pytestmark = pytest.mark.asyncio

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


async def upload(client, headers, document_id, content=b"first revision", filename="annex6.xlsx"):
    return await client.post(
        f"/documents/{document_id}/versions",
        files={"file": (filename, content, XLSX)},
        headers=headers,
    )


class TestUpload:
    async def test_first_upload_creates_version_one(
        self, client, manager_headers, sharing_operation, fake_storage
    ):
        dossier_id = await make_dossier(client, manager_headers, sharing_operation)
        document_id = await make_document(client, manager_headers, dossier_id)

        response = await upload(client, manager_headers, document_id)
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert data["version_no"] == 1
        assert data["original_filename"] == "annex6.xlsx"
        assert data["byte_size"] == len(b"first revision")

    async def test_content_hash_is_recorded(
        self, client, manager_headers, sharing_operation, fake_storage
    ):
        dossier_id = await make_dossier(client, manager_headers, sharing_operation)
        document_id = await make_document(client, manager_headers, dossier_id)
        content = b"the annex as filed"

        response = await upload(client, manager_headers, document_id, content=content)
        assert response.json()["data"]["content_sha256"] == hashlib.sha256(content).hexdigest()

    async def test_upload_becomes_the_current_version(
        self, client, manager_headers, sharing_operation, fake_storage
    ):
        dossier_id = await make_dossier(client, manager_headers, sharing_operation)
        document_id = await make_document(client, manager_headers, dossier_id)
        version_id = (await upload(client, manager_headers, document_id)).json()["data"]["id"]

        document = await client.get(f"/documents/{document_id}", headers=manager_headers)
        assert document.json()["data"]["current_version_id"] == version_id

    async def test_empty_file_is_rejected(
        self, client, manager_headers, sharing_operation, fake_storage
    ):
        dossier_id = await make_dossier(client, manager_headers, sharing_operation)
        document_id = await make_document(client, manager_headers, dossier_id)
        response = await upload(client, manager_headers, document_id, content=b"")
        assert response.status_code == 422

    async def test_member_cannot_upload(
        self, client, manager_headers, sharing_operation, member_headers, fake_storage
    ):
        dossier_id = await make_dossier(client, manager_headers, sharing_operation)
        document_id = await make_document(client, manager_headers, dossier_id)
        response = await upload(client, member_headers, document_id)
        assert response.status_code == 403


class TestVersioning:
    async def test_reupload_creates_a_new_version_and_keeps_the_old_one(
        self, client, manager_headers, sharing_operation, fake_storage
    ):
        dossier_id = await make_dossier(client, manager_headers, sharing_operation)
        document_id = await make_document(client, manager_headers, dossier_id)

        await upload(client, manager_headers, document_id, content=b"v1")
        second = await upload(client, manager_headers, document_id, content=b"v2")
        assert second.json()["data"]["version_no"] == 2

        document = await client.get(f"/documents/{document_id}", headers=manager_headers)
        versions = document.json()["data"]["versions"]
        assert [v["version_no"] for v in versions] == [1, 2]
        assert document.json()["data"]["version_count"] == 2

    async def test_each_version_keeps_its_own_bytes(
        self, client, manager_headers, sharing_operation, fake_storage
    ):
        """Retrieving an old version returns what was filed, not the latest edit."""
        dossier_id = await make_dossier(client, manager_headers, sharing_operation)
        document_id = await make_document(client, manager_headers, dossier_id)

        first = await upload(client, manager_headers, document_id, content=b"as filed")
        await upload(client, manager_headers, document_id, content=b"revised later")

        response = await client.get(
            f"/documents/{document_id}/versions/{first.json()['data']['id']}/file",
            headers=manager_headers,
        )
        assert response.status_code == 200
        assert response.content == b"as filed"

    async def test_identical_content_still_gets_its_own_version_number(
        self, client, manager_headers, sharing_operation, fake_storage
    ):
        dossier_id = await make_dossier(client, manager_headers, sharing_operation)
        document_id = await make_document(client, manager_headers, dossier_id)
        await upload(client, manager_headers, document_id, content=b"same")
        second = await upload(client, manager_headers, document_id, content=b"same")
        assert second.json()["data"]["version_no"] == 2


class TestVersionsAreFrozenOnceSent:
    async def test_cannot_add_a_version_to_a_sent_document(
        self, client, manager_headers, sharing_operation, fake_storage
    ):
        dossier_id = await make_dossier(client, manager_headers, sharing_operation)
        document_id = await make_document(client, manager_headers, dossier_id)
        await upload(client, manager_headers, document_id)
        await client.post(f"/documents/{document_id}/mark-ready", headers=manager_headers)
        await client.post(
            f"/documents/{document_id}/mark-sent",
            json={"submission_date": "2026-09-07"},
            headers=manager_headers,
        )

        response = await upload(client, manager_headers, document_id, content=b"sneaky edit")
        assert response.status_code == 409

    async def test_a_rollback_reopens_the_document_for_a_new_version(
        self, client, manager_headers, sharing_operation, fake_storage
    ):
        """Correcting a filing is possible — but only through a traced rollback."""
        dossier_id = await make_dossier(client, manager_headers, sharing_operation)
        document_id = await make_document(client, manager_headers, dossier_id)
        await upload(client, manager_headers, document_id)
        await client.post(f"/documents/{document_id}/mark-ready", headers=manager_headers)
        await client.post(
            f"/documents/{document_id}/mark-sent",
            json={"submission_date": "2026-09-07"},
            headers=manager_headers,
        )
        await client.post(
            f"/documents/{document_id}/rollback",
            json={"to_status": 2, "reason": "wrong file"},
            headers=manager_headers,
        )

        response = await upload(client, manager_headers, document_id, content=b"corrected")
        assert response.status_code == 200
        assert response.json()["data"]["version_no"] == 2

    async def test_a_ready_document_still_accepts_versions(
        self, client, manager_headers, sharing_operation, fake_storage
    ):
        dossier_id = await make_dossier(client, manager_headers, sharing_operation)
        document_id = await make_document(client, manager_headers, dossier_id)
        await client.post(f"/documents/{document_id}/mark-ready", headers=manager_headers)
        assert (await upload(client, manager_headers, document_id)).status_code == 200


class TestDownload:
    async def test_download_sets_a_filename(
        self, client, manager_headers, sharing_operation, fake_storage
    ):
        dossier_id = await make_dossier(client, manager_headers, sharing_operation)
        document_id = await make_document(client, manager_headers, dossier_id)
        version_id = (
            await upload(client, manager_headers, document_id, filename="annexe 6.xlsx")
        ).json()["data"]["id"]

        response = await client.get(
            f"/documents/{document_id}/versions/{version_id}/file", headers=manager_headers
        )
        assert 'filename="annexe 6.xlsx"' in response.headers["content-disposition"]

    async def test_an_extension_is_not_doubled_when_the_name_already_has_one(
        self, client, manager_headers, sharing_operation, fake_storage
    ):
        dossier_id = await make_dossier(client, manager_headers, sharing_operation)
        document_id = await make_document(client, manager_headers, dossier_id)
        version_id = (
            await upload(client, manager_headers, document_id, filename="annexe6.xlsx")
        ).json()["data"]["id"]

        response = await client.get(
            f"/documents/{document_id}/versions/{version_id}/file", headers=manager_headers
        )

        assert 'filename="annexe6.xlsx"' in response.headers["content-disposition"]
        assert ".xlsx.xlsx" not in response.headers["content-disposition"]

    async def test_member_cannot_download(
        self, client, manager_headers, sharing_operation, member_headers, fake_storage
    ):
        # Inverted, not deleted. A filed version IS the regulatory artifact: it
        # names every participant with their address and EAN, so handing it to
        # any member of the community defeats decision B4 before the member view
        # is even reached. `/filings/mine` is the member's read.
        dossier_id = await make_dossier(client, manager_headers, sharing_operation)
        document_id = await make_document(client, manager_headers, dossier_id)
        version_id = (await upload(client, manager_headers, document_id)).json()["data"]["id"]

        response = await client.get(
            f"/documents/{document_id}/versions/{version_id}/file", headers=member_headers
        )
        assert response.status_code == 403

    async def test_unknown_version_is_404(
        self, client, manager_headers, sharing_operation, fake_storage
    ):
        dossier_id = await make_dossier(client, manager_headers, sharing_operation)
        document_id = await make_document(client, manager_headers, dossier_id)
        response = await client.get(
            f"/documents/{document_id}/versions/999999/file", headers=manager_headers
        )
        assert response.status_code == 404

    async def test_another_community_cannot_download(
        self, client, manager_headers, sharing_operation, other_manager_headers, fake_storage
    ):
        dossier_id = await make_dossier(client, manager_headers, sharing_operation)
        document_id = await make_document(client, manager_headers, dossier_id)
        version_id = (await upload(client, manager_headers, document_id)).json()["data"]["id"]

        response = await client.get(
            f"/documents/{document_id}/versions/{version_id}/file", headers=other_manager_headers
        )
        assert response.status_code == 404
