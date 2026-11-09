"""A piece's versions (spec R2/R4): uploaded through /admin, read back here.

The promise is that the exact bytes filed with the authority can always be
retrieved, and that a later edit never rewrites what was already sent. The
freeze after sending is the /admin suite's (``TestAfterSending``).
"""

import hashlib

import pytest
from sqlalchemy import select

from shared.models.local_models import DocumentVersionModel
from tests.procedures import make_piece

pytestmark = pytest.mark.asyncio

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


async def upload(
    client, headers, dossier_id, piece_id, content=b"first revision", filename="annex6.xlsx"
):
    return await client.post(
        f"/admin/dossiers/{dossier_id}/documents/{piece_id}/versions",
        files={"file": (filename, content, XLSX)},
        headers=headers,
    )


async def current_version(response, piece_id: int) -> int:
    assert response.status_code == 200, response.text
    piece = next(p for p in response.json()["data"]["pieces"] if p["id"] == piece_id)
    return int(piece["current_version_id"])


async def versions_of(db_session, piece_id: int) -> list[DocumentVersionModel]:
    rows = await db_session.scalars(
        select(DocumentVersionModel)
        .where(DocumentVersionModel.id_document == piece_id)
        .order_by(DocumentVersionModel.version_no)
    )
    return list(rows)


class TestUpload:
    async def test_first_upload_creates_version_one(
        self, client, manager_headers, db_session, fake_storage
    ):
        dossier_id, piece_id = await make_piece(client, manager_headers, db_session)
        content = b"the annex as filed"

        response = await upload(client, manager_headers, dossier_id, piece_id, content=content)

        version_id = await current_version(response, piece_id)
        (version,) = await versions_of(db_session, piece_id)
        assert (version.id, version.version_no) == (version_id, 1)
        assert version.original_filename == "annex6.xlsx"
        assert version.byte_size == len(content)
        assert version.content_sha256 == hashlib.sha256(content).hexdigest()

    async def test_empty_file_is_rejected(self, client, manager_headers, db_session, fake_storage):
        dossier_id, piece_id = await make_piece(client, manager_headers, db_session)
        response = await upload(client, manager_headers, dossier_id, piece_id, content=b"")
        assert response.status_code == 422

    async def test_member_cannot_upload(
        self, client, manager_headers, member_headers, db_session, fake_storage
    ):
        dossier_id, piece_id = await make_piece(client, manager_headers, db_session)
        response = await upload(client, member_headers, dossier_id, piece_id)
        assert response.status_code == 403


class TestVersioning:
    async def test_reupload_creates_a_new_version_and_keeps_the_old_one(
        self, client, manager_headers, db_session, fake_storage
    ):
        dossier_id, piece_id = await make_piece(client, manager_headers, db_session)

        await upload(client, manager_headers, dossier_id, piece_id, content=b"v1")
        second = await upload(client, manager_headers, dossier_id, piece_id, content=b"v2")

        latest = await current_version(second, piece_id)
        versions = await versions_of(db_session, piece_id)
        assert [v.version_no for v in versions] == [1, 2]
        assert versions[-1].id == latest

    async def test_each_version_keeps_its_own_bytes(
        self, client, manager_headers, db_session, fake_storage
    ):
        """Retrieving an old version returns what was filed, not the latest edit."""
        dossier_id, piece_id = await make_piece(client, manager_headers, db_session)
        first = await upload(client, manager_headers, dossier_id, piece_id, content=b"as filed")
        first_id = await current_version(first, piece_id)
        await upload(client, manager_headers, dossier_id, piece_id, content=b"revised later")

        response = await client.get(
            f"/documents/{piece_id}/versions/{first_id}/file", headers=manager_headers
        )
        assert response.status_code == 200
        assert response.content == b"as filed"

    async def test_identical_content_still_gets_its_own_version_number(
        self, client, manager_headers, db_session, fake_storage
    ):
        dossier_id, piece_id = await make_piece(client, manager_headers, db_session)
        await upload(client, manager_headers, dossier_id, piece_id, content=b"same")
        await upload(client, manager_headers, dossier_id, piece_id, content=b"same")
        assert [v.version_no for v in await versions_of(db_session, piece_id)] == [1, 2]


class TestDownload:
    async def test_download_sets_a_filename(
        self, client, manager_headers, db_session, fake_storage
    ):
        dossier_id, piece_id = await make_piece(client, manager_headers, db_session)
        version_id = await current_version(
            await upload(client, manager_headers, dossier_id, piece_id, filename="annexe 6.xlsx"),
            piece_id,
        )

        response = await client.get(
            f"/documents/{piece_id}/versions/{version_id}/file", headers=manager_headers
        )
        assert 'filename="annexe 6.xlsx"' in response.headers["content-disposition"]

    async def test_an_extension_is_not_doubled_when_the_name_already_has_one(
        self, client, manager_headers, db_session, fake_storage
    ):
        dossier_id, piece_id = await make_piece(client, manager_headers, db_session)
        version_id = await current_version(
            await upload(client, manager_headers, dossier_id, piece_id, filename="annexe6.xlsx"),
            piece_id,
        )

        response = await client.get(
            f"/documents/{piece_id}/versions/{version_id}/file", headers=manager_headers
        )

        assert 'filename="annexe6.xlsx"' in response.headers["content-disposition"]
        assert ".xlsx.xlsx" not in response.headers["content-disposition"]

    async def test_member_cannot_download(
        self, client, manager_headers, member_headers, db_session, fake_storage
    ):
        # A filed version IS the regulatory artifact: it names every participant
        # with their address and EAN, so handing it to any member of the
        # community defeats decision B4. `/filings/mine` is the member's read.
        dossier_id, piece_id = await make_piece(client, manager_headers, db_session)
        version_id = await current_version(
            await upload(client, manager_headers, dossier_id, piece_id), piece_id
        )

        response = await client.get(
            f"/documents/{piece_id}/versions/{version_id}/file", headers=member_headers
        )
        assert response.status_code == 403

    async def test_unknown_version_is_404(self, client, manager_headers, db_session, fake_storage):
        _, piece_id = await make_piece(client, manager_headers, db_session)
        response = await client.get(
            f"/documents/{piece_id}/versions/999999/file", headers=manager_headers
        )
        assert response.status_code == 404

    async def test_another_community_cannot_download(
        self, client, manager_headers, other_manager_headers, db_session, fake_storage
    ):
        dossier_id, piece_id = await make_piece(client, manager_headers, db_session)
        version_id = await current_version(
            await upload(client, manager_headers, dossier_id, piece_id), piece_id
        )

        response = await client.get(
            f"/documents/{piece_id}/versions/{version_id}/file", headers=other_manager_headers
        )
        assert response.status_code == 404
