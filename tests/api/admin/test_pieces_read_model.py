"""What the procedure's detail says of its pieces (phase 2), the fiche de saisie,
and the archive of the finals.

The detail is what the screens act on: which version is current and at which
stage, what renders, which form a piece renders with, whether everything is
final; the fiche is a form piece's values in the form's order; the archive is
what gets attached.
"""

from __future__ import annotations

import hashlib
import io
import zipfile

import pytest

from engine.routing import route_event
from ports.crm_engine import SqlEngineCrmRead
from shared.const import CheckOutcome, RenderPurpose, RenderState, VersionStage
from tests.api.admin.test_generation_stages import (
    TestAPieceKeepsItsForm,
    _drafted,
    _finalize,
    _generate,
    _land,
    _render,
)
from tests.api.admin.test_procedure_routes import _file, _get, _headers, _piece, _world
from tests.engine.world import EAN, meter
from tests.factories import crm_participant_factory as f
from tests.procedures import open_procedure

pytestmark = pytest.mark.asyncio


async def _store_and_land(db_session, world, store, request, document_id, content: bytes):
    """docgen's answer, with the bytes it stored where it said it did."""
    store[("optimce-documents", f"x/{request.request_id}/form.pdf")] = content
    return await _land(
        db_session, world, request, document_id, sha256=hashlib.sha256(content).hexdigest()
    )


async def _finals(client, db_session, world, headers, dossier_id, pieces, store) -> None:
    await _finalize(client, headers, dossier_id)
    for code, document_id in pieces.items():
        await _store_and_land(
            db_session,
            world,
            store,
            await _render(db_session, document_id),
            document_id,
            f"%PDF-1.4 final {code}".encode(),
        )


class TestAPieceSaysWhereItStands:
    async def test_its_current_version_and_stage(self, client, db_session):
        _world_, headers, dossier_id, pieces = await _drafted(client, db_session)

        piece = _piece(await _get(client, headers, dossier_id), "annex6_sharing_form")

        current = piece["current_version"]
        assert current["id"] == piece["current_version_id"]
        assert current["stage"] == int(VersionStage.DRAFT)
        assert current["generated"] is True
        assert current["original_filename"] == "form-brouillon.pdf"
        assert piece["render"] is None

    async def test_a_render_in_flight_survives_a_reload(self, client, db_session):
        """The screen rebuilds what renders from the server, not from memory."""
        _world_, headers, dossier_id, pieces = await _drafted(client, db_session)
        await _finalize(client, headers, dossier_id)

        detail = await _get(client, headers, dossier_id)

        render = _piece(detail, "annex6_sharing_form")["render"]
        assert render["state"] == int(RenderState.PENDING)
        assert render["purpose"] == int(RenderPurpose.RENDER)
        assert render["stage"] == int(VersionStage.FINAL)
        assert detail["finals_ready"] is False

    async def test_finals_ready_once_every_final_landed(self, client, db_session, admin_storage):
        world, headers, dossier_id, pieces = await _drafted(client, db_session)
        assert (await _get(client, headers, dossier_id))["finals_ready"] is False

        await _finals(client, db_session, world, headers, dossier_id, pieces, admin_storage)

        assert (await _get(client, headers, dossier_id))["finals_ready"] is True

    async def test_the_form_it_renders_with_comes_from_the_registry(self, client, db_session):
        """The key description has no template in the catalogue file, but one is
        registered: it can be generated."""
        world = await _world(db_session)
        headers = await _headers(db_session, world)
        dossier_id = await _file(world, "operation.meter_removed", EAN[0])

        detail = await _get(client, headers, dossier_id)

        key = _piece(detail, "key_description")
        assert (key["template"], key["template_version"]) == ("key_description", 1)
        assert _piece(detail, "dso_amendment")["template"] is None  # received: never rendered

    async def test_a_newer_form_in_force_is_flagged(self, client, db_session):
        world = await _world(db_session)
        headers = await _headers(db_session, world)
        dossier_id = await _file(world, "operation.meter_removed", EAN[0])
        await TestAPieceKeepsItsForm()._revise(db_session, "sharing_modification")

        piece = _piece(await _get(client, headers, dossier_id), "sharing_modification")

        assert (piece["template_version"], piece["template_superseded"]) == (1, True)

    async def test_the_last_check_of_its_current_version(self, client, db_session, admin_storage):
        world, headers, dossier_id, pieces = await _drafted(client, db_session)
        await _finals(client, db_session, world, headers, dossier_id, pieces, admin_storage)
        piece = _piece(await _get(client, headers, dossier_id), "sharing_modification")
        response = await client.post(
            f"/admin/dossiers/{dossier_id}/documents/{piece['id']}/versions/"
            f"{piece['current_version_id']}/verify",
            headers=headers,
        )
        assert response.status_code == 202, response.text
        await _land(
            db_session,
            world,
            await _render(db_session, piece["id"]),
            piece["id"],
            sha256=piece["current_version"]["content_sha256"],
        )

        check = _piece(await _get(client, headers, dossier_id), "sharing_modification")[
            "last_check"
        ]

        assert check["outcome"] == int(CheckOutcome.IDENTICAL)

    async def test_a_per_member_piece_names_its_participant(self, client, db_session):
        world = await _world(db_session)
        headers = await _headers(db_session, world)
        alice = await f.create_member(
            db_session, id_community=world.id_community, name="Dupont", first_name="Alice"
        )
        result = await route_event(
            meter(world, "operation.meter_added", EAN[0], member=alice),
            local=world.session,
            crm=SqlEngineCrmRead(world.session),
            registry=world.registry,
        )

        detail = await _get(client, headers, result.dossiers[0])

        declaration = _piece(detail, "annex8_sworn_declaration")
        assert (declaration["id_member"], declaration["member_name"]) == (alice, "Alice Dupont")


class TestTheEntrySheet:
    async def _ce_update(self, client, manager_headers, db_session) -> dict:
        return await open_procedure(client, manager_headers, db_session, code="CE_ACTUALISATION")

    async def test_a_form_piece_has_its_fiche_in_the_forms_order(
        self, client, db_session, manager_headers, community
    ):
        detail = await self._ce_update(client, manager_headers, db_session)
        assert detail["has_entry_sheet"] is True

        response = await client.get(
            f"/admin/dossiers/{detail['id']}/entry-sheet", headers=manager_headers
        )

        assert response.status_code == 200, response.text
        (sheet,) = response.json()["data"]
        assert sheet["code"] == "community_modification"
        assert sheet["version"] is None  # nothing reviewed yet
        assert [s["labels"]["fr"] for s in sheet["sections"]][:2] == [
            "1. Référence de la communauté d'énergie",
            "2.1. Modification des coordonnées de la communauté d'énergie",
        ]
        first = sheet["sections"][0]["fields"][0]
        assert (first["key"], first["value"]) == ("cwape_reference_digits", None)

    async def test_its_values_are_the_reviewed_versions(
        self, client, db_session, manager_headers, community
    ):
        detail = await self._ce_update(client, manager_headers, db_session)
        form = next(p for p in detail["pieces"] if p["code"] == "community_modification")
        await _generate(
            client,
            manager_headers,
            form["id"],
            data={"representative_name": "Alice Dupont", "cwape_reference_digits": "0042"},
        )
        world = type("World", (), {"id_community": community.id})()
        await _land(db_session, world, await _render(db_session, form["id"]), form["id"])

        (sheet,) = (
            await client.get(f"/admin/dossiers/{detail['id']}/entry-sheet", headers=manager_headers)
        ).json()["data"]

        values = {
            field["key"]: field["value"]
            for section in sheet["sections"]
            for field in section["fields"]
        }
        assert sheet["version"]["stage"] == int(VersionStage.DRAFT)
        assert values["representative_name"] == "Alice Dupont"
        assert values["cwape_reference_digits"] == "0042"

    async def test_a_procedure_without_form_pieces_has_none(
        self, client, db_session, manager_headers, community
    ):
        detail = await open_procedure(client, manager_headers, db_session, code="CE_RAPPORT_ANNUEL")

        response = await client.get(
            f"/admin/dossiers/{detail['id']}/entry-sheet", headers=manager_headers
        )

        assert detail["has_entry_sheet"] is False
        assert response.json()["data"] == []


class TestTheFinalsArchive:
    async def _zip(self, client, headers, dossier_id):
        return await client.get(f"/admin/dossiers/{dossier_id}/finals.zip", headers=headers)

    async def test_it_holds_every_final_and_their_sums(self, client, db_session, admin_storage):
        world, headers, dossier_id, pieces = await _drafted(client, db_session)
        await _finals(client, db_session, world, headers, dossier_id, pieces, admin_storage)

        response = await self._zip(client, headers, dossier_id)

        assert response.status_code == 200, response.text
        assert response.headers["content-type"] == "application/zip"
        assert f'filename="OPT-{dossier_id}-pieces.zip"' in response.headers["content-disposition"]
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            names = archive.namelist()
            sums = archive.read("SHA256SUMS.txt").decode()
            contents = {name: archive.read(name) for name in names if name != "SHA256SUMS.txt"}
        assert len(contents) == 2
        assert all(name.endswith("-form.pdf") for name in contents)
        for name, content in contents.items():
            assert f"{hashlib.sha256(content).hexdigest()}  {name}" in sums

    async def test_the_same_finals_zip_to_the_same_bytes(self, client, db_session, admin_storage):
        world, headers, dossier_id, pieces = await _drafted(client, db_session)
        await _finals(client, db_session, world, headers, dossier_id, pieces, admin_storage)

        first = await self._zip(client, headers, dossier_id)
        second = await self._zip(client, headers, dossier_id)

        assert first.content == second.content

    async def test_it_waits_for_the_finals(self, client, db_session):
        _world_, headers, dossier_id, _pieces = await _drafted(client, db_session)

        response = await self._zip(client, headers, dossier_id)

        assert response.status_code == 409
        assert response.json()["error_code"] == 2385

    async def test_a_stored_file_that_changed_is_not_handed_out(
        self, client, db_session, admin_storage
    ):
        world, headers, dossier_id, pieces = await _drafted(client, db_session)
        await _finals(client, db_session, world, headers, dossier_id, pieces, admin_storage)
        key = next(k for k in admin_storage if k[0] == "optimce-documents")
        admin_storage[key] = b"%PDF-1.4 swapped behind our back"

        response = await self._zip(client, headers, dossier_id)

        assert response.status_code == 502
        assert response.json()["error_code"] == 2390


async def test_archive_names_sort_as_the_pieces_and_never_collide():
    from api.admin.procedures import archive_name

    assert archive_name(3, "annexe8.pdf", "Alice Dupont") == "03-annexe8-alice-dupont.pdf"
    assert archive_name(4, "annexe8.pdf", "Élodie D'Aout") == "04-annexe8-elodie-d-aout.pdf"
    assert archive_name(12, "notes", None) == "12-notes"
