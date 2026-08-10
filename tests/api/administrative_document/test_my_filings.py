"""`GET /filings/mine` — the one member-reachable read in this service.

Decision B4: a member sees THEIR OWN rows out of a filing's frozen snapshot and
nobody else's, filtered server-side. Every test here is about that boundary, or
about the ways it could quietly stop holding.

Modelled on `billing/tests/billing/test_my_invoices.py`, which settles the same
questions for invoices — including "an unlinked user gets 200 [], not 403".
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import text

import tests.factories.crm_participant_factory as f
from domain import prefill
from tests.conftest import gateway_headers

pytestmark = pytest.mark.asyncio

ALICE_USER = "auth-user-alice"
BOB_USER = "auth-user-bob"
STRANGER_USER = "auth-user-stranger"


def member_headers_for(auth_community_id: str, user_id: str) -> dict[str, str]:
    return gateway_headers(auth_community_id, role="MEMBER", user_id=user_id)


async def make_dossier(client, headers, sharing_operation) -> int:
    response = await client.post(
        "/dossiers",
        json={"dossier_type": 1, "title": "Dossier", "id_sharing_operation": sharing_operation},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return int(response.json()["data"]["id"])


async def make_document(client, headers, dossier_id, doc_type="annex6_sharing_form") -> int:
    response = await client.post(
        f"/dossiers/{dossier_id}/documents",
        json={"doc_type": doc_type, "title": "Annexe"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return int(response.json()["data"]["id"])


async def freeze_version(db_session, *, document_id: int, id_community: int, snapshot: dict) -> int:
    """Land a CURRENT version carrying `snapshot`, as the worker does on success.

    Written directly rather than driven through docgen because what is under
    test is the read: the only thing that matters is that a current version
    exists with a frozen snapshot on it.
    """
    version_id = int(
        (
            await db_session.execute(
                text(
                    """
                    INSERT INTO document_version
                        (id_community, id_document, version_no, file_ref, data_snapshot_json,
                         generated_by)
                    VALUES (:cid, :doc, 1, 's3://bucket/key', CAST(:snap AS jsonb), 'auth-manager')
                    RETURNING id
                    """
                ),
                {"cid": id_community, "doc": document_id, "snap": json.dumps(snapshot)},
            )
        ).scalar_one()
    )
    await db_session.execute(
        text("UPDATE document SET current_version_id = :v WHERE id = :d"),
        {"v": version_id, "d": document_id},
    )
    await db_session.flush()
    return version_id


async def seed_two_members(db_session, community) -> tuple[int, int]:
    """Alice and Bob, each with a portal account; only Alice's is linked here."""
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
    alice_user = await f.create_app_user(db_session, auth_user_id=ALICE_USER)
    await f.link_user_to_member(db_session, id_user=alice_user, id_member=alice)
    await db_session.flush()
    return alice, bob


def snapshot_naming(alice: int, bob: int) -> dict:
    return {
        "community_name": "Test Community",
        "participants": [
            {prefill.ROW_MEMBER_ID: alice, "ean": "EAN-A", "nom": "Dupont", "prenom": "Alice"},
            {prefill.ROW_MEMBER_ID: bob, "ean": "EAN-B", "nom": "Martin", "prenom": "Bob"},
        ],
        "installations": [
            {prefill.ROW_MEMBER_ID: bob, "ean": "EAN-B", "puissance_kva": 5.0},
        ],
    }


class TestOwnRowsOnly:
    async def test_returns_only_the_callers_rows(
        self, client, manager_headers, db_session, community, sharing_operation
    ):
        alice, bob = await seed_two_members(db_session, community)
        dossier_id = await make_dossier(client, manager_headers, sharing_operation)
        document_id = await make_document(client, manager_headers, dossier_id)
        await freeze_version(
            db_session,
            document_id=document_id,
            id_community=community.id,
            snapshot=snapshot_naming(alice, bob),
        )

        response = await client.get(
            "/filings/mine", headers=member_headers_for(community.auth_community_id, ALICE_USER)
        )

        assert response.status_code == 200, response.text
        filings = response.json()["data"]
        assert len(filings) == 1
        rows = filings[0]["my_rows"]
        assert [row["ean"] for row in rows["participants"]] == ["EAN-A"]
        # Bob's installation row must not ride along on a filing Alice can see.
        assert rows["installations"] == []

    async def test_the_response_body_never_contains_another_member(
        self, client, manager_headers, db_session, community, sharing_operation
    ):
        alice, bob = await seed_two_members(db_session, community)
        dossier_id = await make_dossier(client, manager_headers, sharing_operation)
        document_id = await make_document(client, manager_headers, dossier_id)
        await freeze_version(
            db_session,
            document_id=document_id,
            id_community=community.id,
            snapshot=snapshot_naming(alice, bob),
        )

        response = await client.get(
            "/filings/mine", headers=member_headers_for(community.auth_community_id, ALICE_USER)
        )

        # Asserted on the RAW BODY, not on the parsed rows. A frontend filter
        # would satisfy every other assertion in this file while shipping the
        # whole community in the network response — which is exactly what B4
        # says must not happen.
        assert "Martin" not in response.text
        assert "EAN-B" not in response.text

    async def test_a_row_with_no_member_id_is_returned_to_nobody(
        self, client, manager_headers, db_session, community, sharing_operation
    ):
        alice, _bob = await seed_two_members(db_session, community)
        dossier_id = await make_dossier(client, manager_headers, sharing_operation)
        document_id = await make_document(client, manager_headers, dossier_id)
        # A filing frozen before identity stamping existed: it names Alice, but
        # only by display name. Guessing from that would collide on homonyms, so
        # the filing is simply not hers to see.
        await freeze_version(
            db_session,
            document_id=document_id,
            id_community=community.id,
            snapshot={"participants": [{"ean": "EAN-A", "nom": "Dupont", "prenom": "Alice"}]},
        )

        response = await client.get(
            "/filings/mine", headers=member_headers_for(community.auth_community_id, ALICE_USER)
        )

        assert response.status_code == 200
        assert response.json()["data"] == []


class TestReachability:
    async def test_an_unlinked_user_gets_an_empty_list_not_a_refusal(
        self, client, manager_headers, db_session, community, sharing_operation
    ):
        alice, bob = await seed_two_members(db_session, community)
        dossier_id = await make_dossier(client, manager_headers, sharing_operation)
        document_id = await make_document(client, manager_headers, dossier_id)
        await freeze_version(
            db_session,
            document_id=document_id,
            id_community=community.id,
            snapshot=snapshot_naming(alice, bob),
        )

        response = await client.get(
            "/filings/mine", headers=member_headers_for(community.auth_community_id, STRANGER_USER)
        )

        # 403 would leak whether the caller represents anybody at all, and
        # "you appear in nothing" is a real answer to the question asked.
        assert response.status_code == 200
        assert response.json()["data"] == []

    async def test_a_manager_may_also_read_their_own_filings(
        self, client, manager_headers, db_session, community, sharing_operation
    ):
        alice, bob = await seed_two_members(db_session, community)
        dossier_id = await make_dossier(client, manager_headers, sharing_operation)
        document_id = await make_document(client, manager_headers, dossier_id)
        await freeze_version(
            db_session,
            document_id=document_id,
            id_community=community.id,
            snapshot=snapshot_naming(alice, bob),
        )

        # A manager is a member of their community too; the route is gated on
        # subscription, not on role, and the row filter applies to everyone.
        response = await client.get("/filings/mine", headers=manager_headers)
        assert response.status_code == 200

    async def test_a_version_with_no_snapshot_is_not_a_filing(
        self, client, manager_headers, db_session, community, sharing_operation
    ):
        alice, _bob = await seed_two_members(db_session, community)
        dossier_id = await make_dossier(client, manager_headers, sharing_operation)
        document_id = await make_document(client, manager_headers, dossier_id)
        version_id = int(
            (
                await db_session.execute(
                    text(
                        """
                        INSERT INTO document_version
                            (id_community, id_document, version_no, file_ref)
                        VALUES (:cid, :doc, 1, 's3://bucket/uploaded') RETURNING id
                        """
                    ),
                    {"cid": community.id, "doc": document_id},
                )
            ).scalar_one()
        )
        await db_session.execute(
            text("UPDATE document SET current_version_id = :v WHERE id = :d"),
            {"v": version_id, "d": document_id},
        )
        await db_session.flush()

        response = await client.get(
            "/filings/mine", headers=member_headers_for(community.auth_community_id, ALICE_USER)
        )

        # An uploaded PDF says nothing about who is in it, so it cannot answer
        # "what has been filed about me" and must not appear as if it did.
        assert response.json()["data"] == []


class TestTenancy:
    async def test_another_communitys_filing_is_invisible(
        self,
        client,
        manager_headers,
        other_manager_headers,
        db_session,
        community,
        other_community,
        sharing_operation,
        other_sharing_operation,
    ):
        alice, bob = await seed_two_members(db_session, community)
        # The SAME member ids appear in a filing owned by the other community.
        other_dossier = await make_dossier(client, other_manager_headers, other_sharing_operation)
        other_document = await make_document(client, other_manager_headers, other_dossier)
        await freeze_version(
            db_session,
            document_id=other_document,
            id_community=other_community.id,
            snapshot=snapshot_naming(alice, bob),
        )

        response = await client.get(
            "/filings/mine", headers=member_headers_for(community.auth_community_id, ALICE_USER)
        )

        # `with_community_scope` on the version read is what stops this, and it
        # matters because member ids are plain ints with no cross-DB FK: a
        # collision between tenants is arithmetic, not a hypothetical.
        assert response.json()["data"] == []


class TestPayload:
    async def test_exposes_the_filing_but_not_who_generated_it(
        self, client, manager_headers, db_session, community, sharing_operation
    ):
        alice, bob = await seed_two_members(db_session, community)
        dossier_id = await make_dossier(client, manager_headers, sharing_operation)
        document_id = await make_document(client, manager_headers, dossier_id)
        await freeze_version(
            db_session,
            document_id=document_id,
            id_community=community.id,
            snapshot=snapshot_naming(alice, bob),
        )

        response = await client.get(
            "/filings/mine", headers=member_headers_for(community.auth_community_id, ALICE_USER)
        )

        filing = response.json()["data"][0]
        assert filing["document"]["doc_type"] == "annex6_sharing_form"
        assert filing["version"]["version_no"] == 1
        assert filing["dossier"]["id"] == dossier_id
        # `generated_by` is a Keycloak subject — which manager pressed the
        # button is not part of what was filed about the member.
        assert "generated_by" not in json.dumps(filing)
        assert "auth-manager" not in response.text
