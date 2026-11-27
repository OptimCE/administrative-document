"""The administrative-document part of a data export (api/data_export.py).

Through the kit's own contract check (`run_contract_check`): the answer matches the contract,
every file listed was written, and each JSON file is checked here against its item schema
(core/export/schemas/files/administrative-document/).
"""

from __future__ import annotations

import json
import pathlib
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

import tests.factories.crm_participant_factory as f
from api.data_export import TABLES, export_handler
from core.config import settings
from core.export import ExportHandler, run_contract_check
from domain import prefill
from shared.const import ProcedureStatus
from tests.conftest import gateway_headers
from tests.procedures import make_piece, open_procedure

SERVICE = "administrative-document"
SCHEMAS = pathlib.Path(__file__).resolve().parents[1] / f"core/export/schemas/files/{SERVICE}"
ALICE = "auth-user-alice"
ADMIN = "auth-user-admin"
BUCKET = settings.OUTPUT_BUCKET


def handler_for(session: AsyncSession, objects: dict[tuple[str, str], int]) -> ExportHandler:
    @asynccontextmanager
    async def shared() -> AsyncIterator[AsyncSession]:
        yield session

    async def size_of(bucket: str, key: str) -> int | None:
        return objects.get((bucket, key))

    return export_handler(local=shared, crm=shared, size_of=size_of, buckets=frozenset({BUCKET}))


def _validators() -> dict[str, Draft202012Validator]:
    schemas = [json.loads(p.read_text(encoding="utf-8")) for p in SCHEMAS.glob("*.schema.json")]
    registry: Registry = Registry().with_resources(
        (schema["$id"], Resource.from_contents(schema)) for schema in schemas
    )
    return {
        schema["title"].split("/")[1].removesuffix(".json item"): Draft202012Validator(
            schema, registry=registry, format_checker=FormatChecker()
        )
        for schema in schemas
        if schema.get("title", "").endswith(" item")
    }


async def export(
    session: AsyncSession,
    *,
    scope: dict[str, str],
    requester: str,
    objects: dict[tuple[str, str], int] | None = None,
) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    """Runs one part; returns part.done and each JSON file's items, checked against its schema."""
    result = await run_contract_check(
        handler_for(session, objects or {}), service=SERVICE, scope=scope, requester_id=requester
    )
    assert result.outcome == "done", result.message
    validators = _validators()
    files: dict[str, list[dict[str, Any]]] = {}
    for file in result.message["files"]:
        document = json.loads(result.store.text(file["key"]))
        entity = document["entity"]
        errors = [
            f"{entity}[{i}]: {error.message}"
            for i, item in enumerate(document["items"])
            for error in validators[entity].iter_errors(item)
        ]
        assert not errors, errors
        files[entity] = document["items"]
    return result.message, files


def counters(done: dict[str, Any]) -> dict[str, int]:
    prefix = f"export.{SERVICE}."
    return {item["labelKey"].removeprefix(prefix): item["count"] for item in done["summary"]}


def user_scope(sub: str) -> dict[str, Any]:
    return {"scope": {"type": "user", "id": sub}, "requester": sub}


def community_scope(community: Any, sub: str) -> dict[str, Any]:
    return {"scope": {"type": "community", "id": community.auth_community_id}, "requester": sub}


async def seed_alice_and_bob(db_session: AsyncSession, community: Any) -> tuple[int, int]:
    """Alice and Bob, members; only Alice has an account, linked to her member."""
    address = await f.create_address(db_session)
    alice = await f.create_member(
        db_session,
        id_community=community.id,
        name="Dupont",
        member_type=1,
        first_name="Alice",
        id_home_address=address,
    )
    bob = await f.create_member(
        db_session,
        id_community=community.id,
        name="Martin",
        member_type=1,
        first_name="Bob",
        id_home_address=address,
    )
    alice_user = await f.create_app_user(db_session, auth_user_id=ALICE)
    await f.link_user_to_member(db_session, id_user=alice_user, id_member=alice)
    await db_session.flush()
    return alice, bob


async def add_version(
    db_session: AsyncSession,
    *,
    community: Any,
    document_id: int,
    file_ref: str,
    snapshot: dict[str, Any] | None = None,
    stage: int = 2,
    version_no: int = 1,
) -> int:
    """A current version of the piece (stage 2 = FINAL), as the worker or an upload lands it."""
    version_id = int(
        (
            await db_session.execute(
                text(
                    """
                    INSERT INTO document_version
                        (id_community, id_document, version_no, file_ref, data_snapshot_json,
                         content_type, original_filename, generated_by, stage, docgen_request_id)
                    VALUES (:cid, :doc, :no, :ref, CAST(:snap AS jsonb), 'application/pdf',
                            'annexe-8.pdf', 'auth-manager', :stage, :request)
                    RETURNING id
                    """
                ),
                {
                    "cid": community.id,
                    "doc": document_id,
                    "no": version_no,
                    "ref": file_ref,
                    "snap": json.dumps(snapshot) if snapshot is not None else None,
                    "stage": stage,
                    # A form to sign is always a render (ck_document_version_stage).
                    "request": f"req-{uuid.uuid4().hex[:12]}" if stage == 3 else None,
                },
            )
        ).scalar_one()
    )
    await db_session.execute(
        text("UPDATE document SET current_version_id = :v WHERE id = :d"),
        {"v": version_id, "d": document_id},
    )
    await db_session.flush()
    return version_id


async def two_pieces(
    client: Any, headers: dict[str, str], db_session: AsyncSession
) -> tuple[int, int]:
    """Two pieces of one procedure (a community has one draft of a procedure at a time)."""
    detail = await open_procedure(client, headers, db_session, code="CE_NOTIFICATION")
    by_template = {p["template"]: p["id"] for p in detail["pieces"]}
    return by_template["annex6_notification"], by_template["annex7_legal_entity"]


def snapshot_naming(alice: int, bob: int) -> dict[str, Any]:
    return {
        "community_name": "Test Community",
        "participants": [
            {prefill.ROW_MEMBER_ID: alice, "ean": "EAN-A", "nom": "Dupont", "prenom": "Alice"},
            {prefill.ROW_MEMBER_ID: bob, "ean": "EAN-B", "nom": "Martin", "prenom": "Bob"},
        ],
        "installations": [{prefill.ROW_MEMBER_ID: bob, "ean": "EAN-B", "puissance_kva": 5.0}],
    }


async def make_admin(db_session: AsyncSession, community: Any, role: str = "ADMIN") -> str:
    sub = f"auth-user-{role.lower()}-{uuid.uuid4().hex[:6]}"
    user = await f.create_app_user(db_session, auth_user_id=sub)
    await db_session.execute(
        text("INSERT INTO community_user (id_community, id_user, role) VALUES (:c, :u, :r)"),
        {"c": community.id, "u": user, "r": role},
    )
    await db_session.flush()
    return sub


class TestUserExport:
    async def test_filings_are_the_app_s_own_read_and_never_bob_s_rows(
        self, client, manager_headers, db_session, community, sharing_operation
    ):
        alice, bob = await seed_alice_and_bob(db_session, community)
        _dossier, document_id = await make_piece(client, manager_headers, db_session)
        await add_version(
            db_session,
            community=community,
            document_id=document_id,
            file_ref=f"s3://{BUCKET}/forms/notification.pdf",
            snapshot=snapshot_naming(alice, bob),
        )
        mine = await client.get(
            "/filings/mine",
            headers=gateway_headers(community.auth_community_id, role="MEMBER", user_id=ALICE),
        )
        assert mine.status_code == 200, mine.text

        done, files = await export(db_session, **user_scope(ALICE))

        (filing,) = files["filings"]
        assert filing["id_community"] == community.id
        assert {k: v for k, v in filing.items() if k not in ("id_community", "community")} == (
            mine.json()["data"][0]
        )
        assert [row["ean"] for row in filing["my_rows"]["participants"]] == ["EAN-A"]
        assert "EAN-B" not in json.dumps(files)
        # A community-wide form names everybody: its file is never in her export.
        assert files["member-pieces"] == [] and done["references"] == []
        assert counters(done) == {"filings": 1, "member_pieces": 0, "files": 0}

    async def test_her_member_s_final_piece_comes_with_its_file(
        self, client, manager_headers, db_session, community, sharing_operation
    ):
        alice, bob = await seed_alice_and_bob(db_session, community)
        alice_piece, bob_piece = await two_pieces(client, manager_headers, db_session)
        for piece, member in ((alice_piece, alice), (bob_piece, bob)):
            await db_session.execute(
                text("UPDATE document SET id_member = :m WHERE id = :d"), {"m": member, "d": piece}
            )
        key = f"signed/{uuid.uuid4().hex}.pdf"
        version = await add_version(
            db_session,
            community=community,
            document_id=alice_piece,
            file_ref=f"s3://{BUCKET}/{key}",
        )
        await add_version(
            db_session, community=community, document_id=bob_piece, file_ref=f"s3://{BUCKET}/b.pdf"
        )

        done, files = await export(
            db_session, **user_scope(ALICE), objects={(BUCKET, key): 2048, (BUCKET, "b.pdf"): 1}
        )

        (piece,) = files["member-pieces"]
        assert piece["id_member"] == alice and piece["document"]["id"] == alice_piece
        assert piece["version"]["stage"] == "FINAL"
        path = f"files/versions/{version}-annexe-8.pdf"
        assert piece["file"] == {"path": f"{SERVICE}/{path}", "file_missing": False}
        (reference,) = done["references"]
        assert reference == {
            "bucket": BUCKET,
            "key": key,
            "targetPath": f"{SERVICE}/{path}",
            "size": 2048,
            "contentType": "application/pdf",
        }

    async def test_a_draft_or_a_form_to_sign_is_not_handed_over(
        self, client, manager_headers, db_session, community, sharing_operation
    ):
        alice, _bob = await seed_alice_and_bob(db_session, community)
        _dossier, piece = await make_piece(client, manager_headers, db_session)
        await db_session.execute(
            text("UPDATE document SET id_member = :m WHERE id = :d"), {"m": alice, "d": piece}
        )
        await add_version(
            db_session,
            community=community,
            document_id=piece,
            file_ref=f"s3://{BUCKET}/to-sign.pdf",
            stage=3,
        )
        done, files = await export(
            db_session, **user_scope(ALICE), objects={(BUCKET, "to-sign.pdf"): 10}
        )
        (item,) = files["member-pieces"]
        assert item["version"]["stage"] == "TO_SIGN" and item["file"] is None
        assert done["references"] == []

    async def test_the_module_off_hides_nothing(
        self, client, manager_headers, db_session, community, sharing_operation
    ):
        alice, bob = await seed_alice_and_bob(db_session, community)
        _dossier, document_id = await make_piece(client, manager_headers, db_session)
        await add_version(
            db_session,
            community=community,
            document_id=document_id,
            file_ref=f"s3://{BUCKET}/forms/x.pdf",
            snapshot=snapshot_naming(alice, bob),
        )
        await db_session.execute(
            text("UPDATE community_subscription SET is_active = false WHERE id_community = :c"),
            {"c": community.id},
        )
        _done, files = await export(db_session, **user_scope(ALICE))
        assert len(files["filings"]) == 1

    async def test_someone_linked_to_nobody_gets_empty_files(self, db_session, community):
        await f.create_app_user(db_session, auth_user_id="auth-user-stranger")
        done, files = await export(db_session, **user_scope("auth-user-stranger"))
        assert files == {"filings": [], "member-pieces": []}
        assert done["references"] == []


class TestCommunityExport:
    async def test_every_table_of_the_community_codes_by_name_files_by_reference(
        self, client, manager_headers, db_session, community, sharing_operation
    ):
        admin = await make_admin(db_session, community)
        present, gone = await two_pieces(client, manager_headers, db_session)
        here = await add_version(
            db_session, community=community, document_id=present, file_ref=f"s3://{BUCKET}/a.pdf"
        )
        await add_version(
            db_session, community=community, document_id=gone, file_ref=f"s3://{BUCKET}/gone.pdf"
        )

        done, files = await export(
            db_session, **community_scope(community, admin), objects={(BUCKET, "a.pdf"): 99}
        )

        assert set(files) == {table.entity for table in TABLES}
        for table, entity in (("dossier", "procedures"), ("document", "documents")):
            stored = (
                await db_session.execute(
                    text(f"SELECT count(*) FROM {table} WHERE id_community = :c"),  # noqa: S608
                    {"c": community.id},
                )
            ).scalar_one()
            assert len(files[entity]) == stored > 0
        names = {member.name for member in ProcedureStatus}
        assert all(p["status"] in names and p["procedure_code"] for p in files["procedures"])
        assert all("id_community" not in item for items in files.values() for item in items)
        assert files["journal"], "every status the journal wrote"

        by_id = {v["id"]: v for v in files["document-versions"]}
        assert by_id[here]["file"] == {
            "path": f"{SERVICE}/files/versions/{here}-annexe-8.pdf",
            "file_missing": False,
        }
        assert [r["key"] for r in done["references"]] == ["a.pdf"]
        missing = [v for v in files["document-versions"] if v["file"]["file_missing"]]
        assert len(missing) == 1
        assert counters(done)["files_missing"] == 1 and counters(done)["files"] == 1

    async def test_a_file_outside_the_readable_buckets_is_missing_not_fatal(
        self, client, manager_headers, db_session, community, sharing_operation
    ):
        admin = await make_admin(db_session, community)
        _dossier, piece = await make_piece(client, manager_headers, db_session)
        await add_version(
            db_session, community=community, document_id=piece, file_ref="s3://elsewhere/x.pdf"
        )
        done, files = await export(
            db_session, **community_scope(community, admin), objects={("elsewhere", "x.pdf"): 1}
        )
        assert done["references"] == []
        assert [v["file"] for v in files["document-versions"]] == [
            {"path": None, "file_missing": True}
        ]

    @pytest.mark.parametrize("role", ["MANAGER", "MEMBER"])
    async def test_only_an_admin_exports_the_community(self, db_session, community, role):
        sub = await make_admin(db_session, community, role)
        result = await run_contract_check(
            handler_for(db_session, {}),
            service=SERVICE,
            scope={"type": "community", "id": community.auth_community_id},
            requester_id=sub,
        )
        assert result.outcome == "failed"
        assert result.message["errorCode"] == "forbidden"

    async def test_an_unknown_community_is_scope_not_found(self, db_session, community):
        admin = await make_admin(db_session, community)
        result = await run_contract_check(
            handler_for(db_session, {}),
            service=SERVICE,
            scope={"type": "community", "id": str(uuid.uuid4())},
            requester_id=admin,
        )
        assert result.outcome == "failed"
        assert result.message["errorCode"] == "scope_not_found"
