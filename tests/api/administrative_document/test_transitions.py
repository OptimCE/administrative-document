"""The journaled state machine over HTTP (spec R1).

The point of these tests is the guarantee the module is sold on: what was sent,
when, and by whom is recoverable — and a correction adds to that record instead
of quietly replacing it.
"""

import pytest

pytestmark = pytest.mark.asyncio

DRAFT, READY, SENT, ACKNOWLEDGED, OBSOLETE = 1, 2, 3, 4, 5
IN_PREPARATION, SUBMITTED, COMPLETE, CLOSED, LAPSED = 1, 2, 3, 4, 5


async def make_dossier(client, headers, sharing_operation, **overrides) -> int:
    """Create a dossier. Every dossier is filed for one sharing operation."""
    payload = {
        "dossier_type": 1,
        "title": "Notification",
        "id_sharing_operation": sharing_operation,
        **overrides,
    }
    response = await client.post("/dossiers", json=payload, headers=headers)
    assert response.status_code == 200, response.text
    return int(response.json()["data"]["id"])


async def make_document(client, headers, dossier_id: int, doc_type="annex6_notification") -> int:
    response = await client.post(
        f"/dossiers/{dossier_id}/documents",
        json={"doc_type": doc_type, "title": "Annexe 6"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return int(response.json()["data"]["id"])


class TestDocumentLifecycle:
    async def test_document_is_born_as_a_draft(self, client, manager_headers, sharing_operation):
        dossier_id = await make_dossier(client, manager_headers, sharing_operation)
        document_id = await make_document(client, manager_headers, dossier_id)
        response = await client.get(f"/documents/{document_id}", headers=manager_headers)
        assert response.json()["data"]["status"] == DRAFT

    async def test_full_forward_path(self, client, manager_headers, sharing_operation):
        dossier_id = await make_dossier(client, manager_headers, sharing_operation)
        document_id = await make_document(client, manager_headers, dossier_id)

        ready = await client.post(f"/documents/{document_id}/mark-ready", headers=manager_headers)
        assert ready.json()["data"]["status"] == READY

        sent = await client.post(
            f"/documents/{document_id}/mark-sent",
            json={"submission_date": "2026-09-07"},
            headers=manager_headers,
        )
        assert sent.json()["data"]["status"] == SENT

        acked = await client.post(
            f"/documents/{document_id}/acknowledge",
            json={
                "acknowledged_date": "2026-09-15",
                "authority_file_ref": "CWAPE-2026-0042",
                "result": "complete",
            },
            headers=manager_headers,
        )
        assert acked.json()["data"]["status"] == ACKNOWLEDGED

    async def test_draft_cannot_jump_to_acknowledged(
        self, client, manager_headers, sharing_operation
    ):
        """A document cannot be recorded as acknowledged without a paper trail."""
        dossier_id = await make_dossier(client, manager_headers, sharing_operation)
        document_id = await make_document(client, manager_headers, dossier_id)
        response = await client.post(
            f"/documents/{document_id}/transition",
            json={
                "to_status": ACKNOWLEDGED,
                "context": {
                    "acknowledged_date": "2026-09-15",
                    "authority_file_ref": "CWAPE-1",
                },
            },
            headers=manager_headers,
        )
        assert response.status_code == 409

    async def test_mark_sent_without_a_submission_date_is_rejected(
        self, client, manager_headers, sharing_operation
    ):
        dossier_id = await make_dossier(client, manager_headers, sharing_operation)
        document_id = await make_document(client, manager_headers, dossier_id)
        await client.post(f"/documents/{document_id}/mark-ready", headers=manager_headers)

        response = await client.post(
            f"/documents/{document_id}/transition",
            json={"to_status": SENT, "context": {}},
            headers=manager_headers,
        )
        assert response.status_code == 422

    async def test_acknowledge_without_an_authority_reference_is_rejected(
        self, client, manager_headers, sharing_operation
    ):
        dossier_id = await make_dossier(client, manager_headers, sharing_operation)
        document_id = await make_document(client, manager_headers, dossier_id)
        await client.post(f"/documents/{document_id}/mark-ready", headers=manager_headers)
        await client.post(
            f"/documents/{document_id}/mark-sent",
            json={"submission_date": "2026-09-07"},
            headers=manager_headers,
        )
        response = await client.post(
            f"/documents/{document_id}/transition",
            json={"to_status": ACKNOWLEDGED, "context": {"acknowledged_date": "2026-09-15"}},
            headers=manager_headers,
        )
        assert response.status_code == 422


class TestRollback:
    async def test_rollback_moves_the_status_back(self, client, manager_headers, sharing_operation):
        dossier_id = await make_dossier(client, manager_headers, sharing_operation)
        document_id = await make_document(client, manager_headers, dossier_id)
        await client.post(f"/documents/{document_id}/mark-ready", headers=manager_headers)
        await client.post(
            f"/documents/{document_id}/mark-sent",
            json={"submission_date": "2026-09-07"},
            headers=manager_headers,
        )

        response = await client.post(
            f"/documents/{document_id}/rollback",
            json={"to_status": READY, "reason": "sent the wrong annex"},
            headers=manager_headers,
        )
        assert response.status_code == 200
        assert response.json()["data"]["status"] == READY

    async def test_rollback_requires_a_reason(self, client, manager_headers, sharing_operation):
        dossier_id = await make_dossier(client, manager_headers, sharing_operation)
        document_id = await make_document(client, manager_headers, dossier_id)
        await client.post(f"/documents/{document_id}/mark-ready", headers=manager_headers)

        response = await client.post(
            f"/documents/{document_id}/transition",
            json={"to_status": DRAFT, "context": {}},
            headers=manager_headers,
        )
        assert response.status_code == 422

    async def test_rollback_appends_to_history_instead_of_erasing_it(
        self, client, manager_headers, sharing_operation
    ):
        """The whole point: the mistake and its correction are both on record."""
        dossier_id = await make_dossier(client, manager_headers, sharing_operation)
        document_id = await make_document(client, manager_headers, dossier_id)
        await client.post(f"/documents/{document_id}/mark-ready", headers=manager_headers)
        await client.post(
            f"/documents/{document_id}/mark-sent",
            json={"submission_date": "2026-09-07"},
            headers=manager_headers,
        )
        await client.post(
            f"/documents/{document_id}/rollback",
            json={"to_status": READY, "reason": "wrong annex"},
            headers=manager_headers,
        )

        timeline = await client.get(f"/dossiers/{dossier_id}/timeline", headers=manager_headers)
        document_events = [
            e
            for e in timeline.json()["data"]
            if e["subject_type"] == 1  # DOCUMENT
        ]
        transitions = [(e["from_status"], e["to_status"]) for e in document_events]
        # birth, ready, sent, and the correction — the "sent" is still there.
        assert transitions == [(None, DRAFT), (DRAFT, READY), (READY, SENT), (SENT, READY)]
        assert document_events[-1]["is_corrective"] is True
        assert document_events[-1]["context"]["reason"] == "wrong annex"
        assert document_events[-2]["is_corrective"] is False

    async def test_rollback_carries_the_target_status_own_requirements(
        self, client, manager_headers, sharing_operation
    ):
        """ACKNOWLEDGED -> SENT is corrective *and* lands on a status that demands
        a submission date. Both requirements have to travel or the edge is
        unreachable — an acknowledgment recorded by mistake could never be undone.
        """
        dossier_id = await make_dossier(client, manager_headers, sharing_operation)
        document_id = await make_document(client, manager_headers, dossier_id)
        await client.post(f"/documents/{document_id}/mark-ready", headers=manager_headers)
        await client.post(
            f"/documents/{document_id}/mark-sent",
            json={"submission_date": "2026-09-07"},
            headers=manager_headers,
        )
        await client.post(
            f"/documents/{document_id}/acknowledge",
            json={
                "acknowledged_date": "2026-09-15",
                "authority_file_ref": "CWAPE-2026-0042",
                "result": "complete",
            },
            headers=manager_headers,
        )

        response = await client.post(
            f"/documents/{document_id}/rollback",
            json={
                "to_status": SENT,
                "reason": "acknowledged the wrong dossier",
                "context": {"submission_date": "2026-09-07"},
            },
            headers=manager_headers,
        )
        assert response.status_code == 200, response.text
        assert response.json()["data"]["status"] == SENT

        timeline = await client.get(f"/dossiers/{dossier_id}/timeline", headers=manager_headers)
        correction = [e for e in timeline.json()["data"] if e["subject_type"] == 1][-1]
        assert (correction["from_status"], correction["to_status"]) == (ACKNOWLEDGED, SENT)
        assert correction["is_corrective"] is True
        assert correction["context"]["submission_date"] == "2026-09-07"
        assert correction["context"]["reason"] == "acknowledged the wrong dossier"

    async def test_rollback_without_the_target_status_requirements_is_rejected(
        self, client, manager_headers, sharing_operation
    ):
        """A reason alone is not enough on an edge that also needs a date."""
        dossier_id = await make_dossier(client, manager_headers, sharing_operation)
        document_id = await make_document(client, manager_headers, dossier_id)
        await client.post(f"/documents/{document_id}/mark-ready", headers=manager_headers)
        await client.post(
            f"/documents/{document_id}/mark-sent",
            json={"submission_date": "2026-09-07"},
            headers=manager_headers,
        )
        await client.post(
            f"/documents/{document_id}/acknowledge",
            json={
                "acknowledged_date": "2026-09-15",
                "authority_file_ref": "CWAPE-2026-0042",
                "result": "complete",
            },
            headers=manager_headers,
        )

        response = await client.post(
            f"/documents/{document_id}/rollback",
            json={"to_status": SENT, "reason": "acknowledged the wrong dossier"},
            headers=manager_headers,
        )
        assert response.status_code == 422

    async def test_dossier_rollback_carries_its_context_too(
        self, client, manager_headers, sharing_operation
    ):
        """The dossier twin of the edge above: COMPLETE -> SUBMITTED."""
        dossier_id = await make_dossier(client, manager_headers, sharing_operation)
        for to_status, context in ((SUBMITTED, {"submission_date": "2026-09-07"}), (COMPLETE, {})):
            await client.post(
                f"/dossiers/{dossier_id}/transition",
                json={"to_status": to_status, "context": context},
                headers=manager_headers,
            )

        response = await client.post(
            f"/dossiers/{dossier_id}/rollback",
            json={
                "to_status": SUBMITTED,
                "reason": "the authority came back with questions",
                "context": {"submission_date": "2026-09-07"},
            },
            headers=manager_headers,
        )
        assert response.status_code == 200, response.text
        assert response.json()["data"]["status"] == SUBMITTED

    async def test_rollback_context_cannot_override_the_validated_reason(
        self, client, manager_headers, sharing_operation
    ):
        """`reason` is length-validated by the schema; a duplicate in `context`
        must not be what lands in the journal."""
        dossier_id = await make_dossier(client, manager_headers, sharing_operation)
        document_id = await make_document(client, manager_headers, dossier_id)
        await client.post(f"/documents/{document_id}/mark-ready", headers=manager_headers)

        response = await client.post(
            f"/documents/{document_id}/rollback",
            json={"to_status": DRAFT, "reason": "the real reason", "context": {"reason": ""}},
            headers=manager_headers,
        )
        assert response.status_code == 200, response.text

        timeline = await client.get(f"/dossiers/{dossier_id}/timeline", headers=manager_headers)
        correction = [e for e in timeline.json()["data"] if e["subject_type"] == 1][-1]
        assert correction["context"]["reason"] == "the real reason"


class TestTimeline:
    async def test_birth_event_is_journaled(self, client, manager_headers, sharing_operation):
        dossier_id = await make_dossier(client, manager_headers, sharing_operation)
        timeline = await client.get(f"/dossiers/{dossier_id}/timeline", headers=manager_headers)
        events = timeline.json()["data"]
        assert len(events) == 1
        assert events[0]["from_status"] is None
        assert events[0]["to_status"] == IN_PREPARATION

    async def test_timeline_records_the_actor_and_the_declared_context(
        self, client, manager_headers, sharing_operation
    ):
        dossier_id = await make_dossier(client, manager_headers, sharing_operation)
        document_id = await make_document(client, manager_headers, dossier_id)
        await client.post(f"/documents/{document_id}/mark-ready", headers=manager_headers)
        await client.post(
            f"/documents/{document_id}/mark-sent",
            json={"submission_date": "2026-09-07", "note": "posted via Mon Espace"},
            headers=manager_headers,
        )

        timeline = await client.get(f"/dossiers/{dossier_id}/timeline", headers=manager_headers)
        sent_event = next(e for e in timeline.json()["data"] if e["to_status"] == SENT)
        assert sent_event["actor_id"] == "auth-user-1"
        assert sent_event["context"]["submission_date"] == "2026-09-07"
        assert sent_event["context"]["note"] == "posted via Mon Espace"

    async def test_timeline_covers_the_dossier_and_its_documents(
        self, client, manager_headers, sharing_operation
    ):
        dossier_id = await make_dossier(client, manager_headers, sharing_operation)
        await make_document(client, manager_headers, dossier_id)
        timeline = await client.get(f"/dossiers/{dossier_id}/timeline", headers=manager_headers)
        subject_types = {e["subject_type"] for e in timeline.json()["data"]}
        assert subject_types == {1, 2}  # DOCUMENT and DOSSIER


class TestDossierTransitions:
    async def test_submit_records_the_submission_timestamp(
        self, client, manager_headers, sharing_operation
    ):
        dossier_id = await make_dossier(client, manager_headers, sharing_operation)
        response = await client.post(
            f"/dossiers/{dossier_id}/transition",
            json={"to_status": SUBMITTED, "context": {"submission_date": "2026-09-07"}},
            headers=manager_headers,
        )
        assert response.json()["data"]["submitted_at"] is not None

    async def test_in_preparation_cannot_jump_to_complete(
        self, client, manager_headers, sharing_operation
    ):
        dossier_id = await make_dossier(client, manager_headers, sharing_operation)
        response = await client.post(
            f"/dossiers/{dossier_id}/transition",
            json={"to_status": COMPLETE, "context": {}},
            headers=manager_headers,
        )
        assert response.status_code == 409

    async def test_closed_is_terminal(self, client, manager_headers, sharing_operation):
        dossier_id = await make_dossier(client, manager_headers, sharing_operation)
        for to_status in (SUBMITTED, COMPLETE, CLOSED):
            context = {"submission_date": "2026-09-07"} if to_status == SUBMITTED else {}
            await client.post(
                f"/dossiers/{dossier_id}/transition",
                json={"to_status": to_status, "context": context},
                headers=manager_headers,
            )
        response = await client.post(
            f"/dossiers/{dossier_id}/transition",
            json={"to_status": SUBMITTED, "context": {"submission_date": "2026-10-01"}},
            headers=manager_headers,
        )
        assert response.status_code == 409
