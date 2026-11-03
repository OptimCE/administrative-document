"""A participant's annexe 8 signed in the app, through the signature annexe (2026-11-03).

A manager asks (``POST …/signature-request``): the piece's TO_SIGN version goes
out on ``signature.request.create`` with the CWaPE form's own fields; the
service's events move ``piece_signature``; ``completed`` makes the sealed PDF
the piece's FINAL version and the piece SIGNED, as an upload would.
"""

from __future__ import annotations

import contextlib
import hashlib
import uuid
from typing import Any

import pytest
from sqlalchemy import select, text

from main import app
from ports.providers import get_event_publisher
from shared.const import ProcedureDocumentStatus, VersionStage
from shared.models.local_models import PieceSignatureModel
from tests.api.admin.test_generation_stages import _current, _generate, _land, _render
from tests.api.admin.test_procedure_routes import _file, _get, _headers, _piece, _world
from tests.engine.world import EAN
from worker.signature_events import process_signature_event

pytestmark = pytest.mark.asyncio

PIECE = "annex8_sworn_declaration"


class RecordingPublisher:
    """Records what goes out; the render requests share the publisher."""

    def __init__(self, *, fail: bool = False) -> None:
        self.published: list[tuple[str, Any]] = []
        self.fail = fail

    async def publish(self, subject: str, event: Any) -> None:
        if self.fail and subject == "signature.request.create":
            raise OSError("broker down")
        self.published.append((subject, event))

    @property
    def signature_requests(self) -> list[Any]:
        return [e for s, e in self.published if s == "signature.request.create"]


@pytest.fixture
def publisher():
    recording = RecordingPublisher()
    app.dependency_overrides[get_event_publisher] = lambda: recording
    yield recording
    app.dependency_overrides.pop(get_event_publisher, None)


async def _member(db_session) -> int:
    member = await db_session.scalar(
        text(
            "INSERT INTO member (name, member_type, status, id_community) "
            "SELECT 'Martin', 1, 1, id FROM community ORDER BY id DESC LIMIT 1 RETURNING id"
        )
    )
    await db_session.execute(
        text("INSERT INTO individual (id, first_name) VALUES (:m, 'Alice')"), {"m": member}
    )
    return int(member)


async def _to_sign(client, db_session, *, module: bool = True, account: bool = True):
    """A procedure whose annexe 8 of a real member is rendered to sign."""
    world = await _world(db_session)
    headers = await _headers(db_session, world)
    member = await _member(db_session)
    if module:
        await db_session.execute(
            text(
                "INSERT INTO community_subscription (id_community, feature, is_active) "
                "VALUES (:c, 'signature', TRUE)"
            ),
            {"c": world.id_community},
        )
    if account:
        user = await db_session.scalar(
            text(
                "INSERT INTO app_user (auth_user_id, email) VALUES (:s, 'alice@example.test') "
                "RETURNING id"
            ),
            {"s": str(uuid.uuid4())},
        )
        await db_session.execute(
            text("INSERT INTO user_member_link (id_user, id_member) VALUES (:u, :m)"),
            {"u": user, "m": member},
        )
    dossier_id = await _file(world, "operation.meter_added", EAN[0], member=member)
    piece = _piece(await _get(client, headers, dossier_id), PIECE)
    assert piece["id_member"] == member
    await _generate(client, headers, piece["id"])
    await _land(db_session, world, await _render(db_session, piece["id"]), piece["id"])
    assert (await _current(db_session, piece["id"])).stage == int(VersionStage.TO_SIGN)
    await db_session.commit()
    return world, headers, dossier_id, piece["id"]


def _ask(client, headers, dossier_id: int, piece_id: int):
    return client.post(
        f"/admin/dossiers/{dossier_id}/documents/{piece_id}/signature-request", headers=headers
    )


class _SameSession:
    """``process_signature_event``'s session factory, on the test's session."""

    def __init__(self, session) -> None:
        self._session = session

    def __call__(self):
        @contextlib.asynccontextmanager
        async def _cm():
            yield self._session

        return _cm()


async def _event(db_session, world, kind: str, data: dict, store: dict[str, bytes] | None = None):
    objects: dict[str, bytes] = store if store is not None else {}

    async def fetch(key: str) -> bytes:
        return objects[key]

    async def put(key: str, content: bytes, content_type: str) -> str:
        objects[key] = content
        return f"s3://optimce-documents/{key}"

    return await process_signature_event(
        f"signature.event.administrative_document.{kind}",
        data,
        registry=world.registry,
        session_factory=_SameSession(db_session),
        fetch=fetch,
        store=put,
    )


class TestAsking:
    async def test_needs_the_module_and_an_account(self, client, db_session, publisher):
        _world_, headers, dossier_id, piece_id = await _to_sign(
            client, db_session, module=False, account=False
        )
        piece = _piece(await _get(client, headers, dossier_id), PIECE)
        assert piece["signature_requestable"] is False
        off = await _ask(client, headers, dossier_id, piece_id)
        assert (off.status_code, off.json()["error_code"]) == (409, 2600)
        await db_session.execute(
            text(
                "INSERT INTO community_subscription (id_community, feature, is_active) "
                "VALUES (:c, 'signature', TRUE)"
            ),
            {"c": _world_.id_community},
        )
        await db_session.commit()
        no_account = await _ask(client, headers, dossier_id, piece_id)
        assert (no_account.status_code, no_account.json()["error_code"]) == (409, 2602)
        assert publisher.signature_requests == []

    async def test_a_generated_piece_is_not_signable(self, client, db_session, publisher):
        _world_, headers, dossier_id, _piece_id = await _to_sign(client, db_session)
        other = _piece(await _get(client, headers, dossier_id), "annex6_sharing_form")
        assert other["signature_requestable"] is False
        response = await _ask(client, headers, dossier_id, other["id"])
        assert (response.status_code, response.json()["error_code"]) == (409, 2601)

    async def test_asking_publishes_the_forms_own_fields(self, client, db_session, publisher):
        world, headers, dossier_id, piece_id = await _to_sign(client, db_session)
        assert _piece(await _get(client, headers, dossier_id), PIECE)["signature_requestable"]

        response = await _ask(client, headers, dossier_id, piece_id)

        assert response.status_code == 202, response.text
        (event,) = publisher.signature_requests
        data = event.data
        version = await _current(db_session, piece_id)
        assert data["source_service"] == "administrative_document"
        assert data["community_id"] == world.id_community
        assert data["document_key"] == version.file_ref.removeprefix("s3://optimce-documents/")
        assert data["idempotency_key"] == response.json()["data"]["idempotency_key"]
        (signer,) = data["signers"]
        assert signer["slot"] == {"field": "Champ de signature 1"}
        assert [
            (f["name"], f["type"], f["slot"]["field"], f["required"]) for f in signer["fields"]
        ] == [
            ("place", "TEXT", "Champ de texte 21", True),
            ("date", "DATE", "Champ de texte 22", False),
        ]
        assert data["title"].endswith("Alice Martin")
        piece = _piece(await _get(client, headers, dossier_id), PIECE)
        assert piece["signature"]["status"] == "requested"
        assert piece["signature_requestable"] is False
        again = await _ask(client, headers, dossier_id, piece_id)
        assert (again.status_code, again.json()["error_code"]) == (409, 2603)

    async def test_a_broker_that_refuses_leaves_it_askable(self, client, db_session):
        _world_, headers, dossier_id, piece_id = await _to_sign(client, db_session)
        app.dependency_overrides[get_event_publisher] = lambda: RecordingPublisher(fail=True)
        try:
            response = await _ask(client, headers, dossier_id, piece_id)
        finally:
            app.dependency_overrides.pop(get_event_publisher, None)
        assert (response.status_code, response.json()["error_code"]) == (503, 2604)
        piece = _piece(await _get(client, headers, dossier_id), PIECE)
        assert piece["signature"]["status"] == "failed"
        assert piece["signature_requestable"] is True


class TestTheServicesEvents:
    async def test_completed_makes_the_signed_pdf_the_final_version(
        self, client, db_session, publisher
    ):
        world, headers, dossier_id, piece_id = await _to_sign(client, db_session)
        key = (await _ask(client, headers, dossier_id, piece_id)).json()["data"]["idempotency_key"]
        request_id = str(uuid.uuid4())

        assert (
            await _event(
                db_session, world, "accepted", {"idempotency_key": key, "request_id": request_id}
            )
        ).outcome == "updated"
        await _event(
            db_session, world, "prepared", {"idempotency_key": key, "request_id": request_id}
        )
        assert _piece(await _get(client, headers, dossier_id), PIECE)["signature"] == {
            "status": "open",
            "request_id": request_id,
            "reason": None,
            "updated_at": _piece(await _get(client, headers, dossier_id), PIECE)["signature"][
                "updated_at"
            ],
        }
        signed = b"%PDF-1.7 signed and sealed"
        final_key = f"signature/{world.id_community}/{request_id}/final.pdf"
        store = {final_key: signed}
        applied = await _event(
            db_session,
            world,
            "completed",
            {
                "idempotency_key": key,
                "request_id": request_id,
                "final_key": final_key,
                "final_sha256": hashlib.sha256(signed).hexdigest(),
                "evidence_pdf_key": f"signature/{world.id_community}/{request_id}/evidence.pdf",
                "evidence_json_key": f"signature/{world.id_community}/{request_id}/evidence.json",
            },
            store,
        )

        assert applied.outcome == "signed"
        version = await _current(db_session, piece_id)
        assert version.stage == int(VersionStage.FINAL)
        assert version.generated_by == "signature"
        assert version.content_sha256 == hashlib.sha256(signed).hexdigest()
        assert store[version.file_ref.removeprefix("s3://optimce-documents/")] == signed
        piece = _piece(await _get(client, headers, dossier_id), PIECE)
        assert piece["status"] == int(ProcedureDocumentStatus.SIGNED)
        assert piece["signature"]["status"] == "signed"
        # Delivered again: settled, nothing more happens.
        again = await _event(db_session, world, "completed", {"idempotency_key": key}, store)
        assert again.outcome == "stale"

    async def test_a_decline_settles_it_and_a_new_request_is_a_new_attempt(
        self, client, db_session, publisher
    ):
        world, headers, dossier_id, piece_id = await _to_sign(client, db_session)
        first = (await _ask(client, headers, dossier_id, piece_id)).json()["data"][
            "idempotency_key"
        ]
        await _event(
            db_session, world, "declined", {"idempotency_key": first, "reason": "EAN erroné"}
        )

        piece = _piece(await _get(client, headers, dossier_id), PIECE)
        assert (piece["signature"]["status"], piece["signature"]["reason"]) == (
            "declined",
            "EAN erroné",
        )
        assert piece["signature_requestable"] is True
        second = (await _ask(client, headers, dossier_id, piece_id)).json()["data"][
            "idempotency_key"
        ]
        assert second != first and second.endswith("-2")
        rows = (
            await db_session.scalars(
                select(PieceSignatureModel).where(PieceSignatureModel.id_document == piece_id)
            )
        ).all()
        assert len(rows) == 2

    async def test_a_signed_pdf_that_does_not_match_its_digest_is_refused(
        self, client, db_session, publisher
    ):
        world, headers, dossier_id, piece_id = await _to_sign(client, db_session)
        key = (await _ask(client, headers, dossier_id, piece_id)).json()["data"]["idempotency_key"]
        with pytest.raises(ValueError, match="digest"):
            await _event(
                db_session,
                world,
                "completed",
                {"idempotency_key": key, "final_key": "k", "final_sha256": "0" * 64},
                {"k": b"%PDF tampered"},
            )

    async def test_an_event_for_nobody_is_dropped(self, db_session):
        world = await _world(db_session)
        applied = await _event(db_session, world, "completed", {"idempotency_key": "nobody-1"})
        assert applied.outcome == "unknown"
