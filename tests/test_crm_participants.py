"""The Phase-2 CRM read port, against a real Postgres.

Unit tests cover the mapping; these cover the SQL — the joins, the scoping, and
the integer/string boundary that a fake would silently paper over.
"""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy import text

from ports.crm_core import WARNING_METER_NO_MEMBER, PrefillWarning
from ports.crm_core_sqlalchemy import SqlAlchemyCrmCoreRead
from tests.factories import crm_participant_factory as f

pytestmark = pytest.mark.asyncio


async def _alice_with_two_meters(db_session, community, sharing_operation) -> int:
    home = await f.create_address(db_session, street="Rue Basse", number="3")
    id_member = await f.create_member(
        db_session,
        id_community=community.id,
        name="Dupont",
        member_type=1,
        first_name="Alice",
        email="alice@example.be",
        id_home_address=home,
    )
    site = await f.create_address(db_session, street="Rue du Site", number="7", city="Jambes")
    for ean, address in (("541448000000000001", site), ("541448000000000002", None)):
        await f.create_meter(
            db_session,
            ean=ean,
            id_community=community.id,
            meter_number=f"M-{ean[-1]}",
            id_address=address,
        )
        await f.attribute_meter(
            db_session,
            ean=ean,
            id_community=community.id,
            id_sharing_operation=sharing_operation,
            id_member=id_member,
            grd="ORES",
        )
    return id_member


class TestParticipants:
    async def test_a_member_is_returned_once_with_all_their_meters(
        self, db_session, community, sharing_operation
    ):
        await _alice_with_two_meters(db_session, community, sharing_operation)

        result = await SqlAlchemyCrmCoreRead(db_session).get_operation_participants(
            community.id, sharing_operation
        )

        assert len(result.participants) == 1
        alice = result.participants[0]
        assert alice.name == "Dupont"
        assert alice.first_name == "Alice"
        assert alice.email == "alice@example.be"
        assert {m.ean for m in alice.meters} == {
            "541448000000000001",
            "541448000000000002",
        }

    async def test_the_house_number_arrives_as_a_string(
        self, db_session, community, sharing_operation
    ):
        """The port owes a string whatever the column is. `address.number` was an
        INTEGER until 2026-08-30 and is a VARCHAR(32) now; this exact coercion bug
        once broke billing's whole issue pipeline, so the guarantee is asserted
        rather than assumed."""
        await _alice_with_two_meters(db_session, community, sharing_operation)

        result = await SqlAlchemyCrmCoreRead(db_session).get_operation_participants(
            community.id, sharing_operation
        )

        assert result.participants[0].address.number == "3"
        assert isinstance(result.participants[0].address.number, str)

    async def test_a_meter_uses_its_own_address_and_falls_back_to_the_members(
        self, db_session, community, sharing_operation
    ):
        await _alice_with_two_meters(db_session, community, sharing_operation)

        result = await SqlAlchemyCrmCoreRead(db_session).get_operation_participants(
            community.id, sharing_operation
        )

        meters = {m.ean: m for m in result.participants[0].meters}
        assert meters["541448000000000001"].address.city == "Jambes"  # own address
        assert meters["541448000000000002"].address.city == "Namur"  # member's home

    async def test_a_company_carries_its_vat_number(self, db_session, community, sharing_operation):
        id_member = await f.create_member(
            db_session,
            id_community=community.id,
            name="ACME SRL",
            member_type=2,
            vat_number="BE0987654321",
        )
        await f.create_meter(db_session, ean="541448000000000003", id_community=community.id)
        await f.attribute_meter(
            db_session,
            ean="541448000000000003",
            id_community=community.id,
            id_sharing_operation=sharing_operation,
            id_member=id_member,
        )

        result = await SqlAlchemyCrmCoreRead(db_session).get_operation_participants(
            community.id, sharing_operation
        )

        assert result.participants[0].vat_number == "BE0987654321"
        assert result.participants[0].member_type == 2


class TestInstallations:
    async def test_an_injecting_meter_becomes_a_production_installation(
        self, db_session, community, sharing_operation
    ):
        id_member = await f.create_member(
            db_session, id_community=community.id, name="ACME SRL", member_type=2
        )
        await f.create_meter(
            db_session, ean="541448000000000009", id_community=community.id, meter_number="P-1"
        )
        await f.attribute_meter(
            db_session,
            ean="541448000000000009",
            id_community=community.id,
            id_sharing_operation=sharing_operation,
            id_member=id_member,
            injection_status=1,
            production_chain=1,
            generating_capacity=9.5,
            grd="RESA",
            start_date=datetime.date(2026, 3, 1),
        )

        result = await SqlAlchemyCrmCoreRead(db_session).get_operation_participants(
            community.id, sharing_operation
        )

        assert len(result.production) == 1
        pv = result.production[0]
        assert (pv.ean, pv.grd, pv.production_chain) == ("541448000000000009", "RESA", 1)
        assert pv.generating_capacity == 9.5
        assert pv.commissioned_on == datetime.date(2026, 3, 1)

    async def test_a_consumption_only_meter_is_not_an_installation(
        self, db_session, community, sharing_operation
    ):
        await _alice_with_two_meters(db_session, community, sharing_operation)

        result = await SqlAlchemyCrmCoreRead(db_session).get_operation_participants(
            community.id, sharing_operation
        )

        assert result.production == ()

    async def test_storage_is_empty_because_the_crm_does_not_model_it(
        self, db_session, community, sharing_operation
    ):
        await _alice_with_two_meters(db_session, community, sharing_operation)

        result = await SqlAlchemyCrmCoreRead(db_session).get_operation_participants(
            community.id, sharing_operation
        )

        assert result.storage == ()


class TestScopingAndWarnings:
    async def test_another_operations_meters_are_excluded(
        self, db_session, community, sharing_operation, second_sharing_operation
    ):
        await _alice_with_two_meters(db_session, community, sharing_operation)

        result = await SqlAlchemyCrmCoreRead(db_session).get_operation_participants(
            community.id, second_sharing_operation
        )

        assert result.participants == ()

    async def test_no_operation_reads_the_whole_community(
        self, db_session, community, other_community, sharing_operation, second_sharing_operation
    ):
        """A community-scope procedure (CE_NOTIFICATION…) has no operation: its
        forms list every participant of every operation, and still no one else's."""
        await _alice_with_two_meters(db_session, community, sharing_operation)
        bob = await f.create_member(
            db_session, id_community=community.id, name="Martin", member_type=1
        )
        await f.create_meter(db_session, ean="541448000000000005", id_community=community.id)
        await f.attribute_meter(
            db_session,
            ean="541448000000000005",
            id_community=community.id,
            id_sharing_operation=second_sharing_operation,
            id_member=bob,
        )

        result = await SqlAlchemyCrmCoreRead(db_session).get_operation_participants(
            community.id, None
        )
        elsewhere = await SqlAlchemyCrmCoreRead(db_session).get_operation_participants(
            other_community.id, None
        )

        assert sorted(p.name for p in result.participants) == ["Dupont", "Martin"]
        assert elsewhere.participants == ()

    async def test_another_communitys_data_is_never_returned(
        self, db_session, community, other_community, sharing_operation
    ):
        await _alice_with_two_meters(db_session, community, sharing_operation)

        result = await SqlAlchemyCrmCoreRead(db_session).get_operation_participants(
            other_community.id, sharing_operation
        )

        assert result.participants == ()

    async def test_an_inactive_attribution_is_excluded(
        self, db_session, community, sharing_operation
    ):
        await f.create_meter(db_session, ean="541448000000000004", id_community=community.id)
        await f.attribute_meter(
            db_session,
            ean="541448000000000004",
            id_community=community.id,
            id_sharing_operation=sharing_operation,
            id_member=None,
            status=2,  # INACTIVE
        )

        result = await SqlAlchemyCrmCoreRead(db_session).get_operation_participants(
            community.id, sharing_operation
        )

        assert result.participants == () and result.warnings == ()

    @pytest.mark.parametrize("status", [3, 4])  # waiting for the GRD, for the manager
    async def test_a_waiting_meter_is_listed_for_its_operation_only(
        self, db_session, community, sharing_operation, status
    ):
        """Phase 4 (one status per meter): a meter being added waits until its
        procedure makes it active, and it is what that procedure's forms list.
        The community-wide lists (the annual report) keep the active ones."""
        bob = await f.create_member(
            db_session, id_community=community.id, name="Martin", member_type=1
        )
        await f.create_meter(db_session, ean="541448000000000006", id_community=community.id)
        await f.attribute_meter(
            db_session,
            ean="541448000000000006",
            id_community=community.id,
            id_sharing_operation=sharing_operation,
            id_member=bob,
            status=status,
        )
        read = SqlAlchemyCrmCoreRead(db_session)

        in_operation = await read.get_operation_participants(community.id, sharing_operation)
        community_wide = await read.get_operation_participants(community.id, None)

        assert [p.name for p in in_operation.participants] == ["Martin"]
        assert community_wide.participants == ()

    async def test_an_unowned_meter_is_reported_not_silently_dropped(
        self, db_session, community, sharing_operation
    ):
        """A delivery point nobody owns cannot be listed under a name, but
        vanishing from a legal form without a word is worse."""
        await f.create_meter(db_session, ean="541448000000000005", id_community=community.id)
        await f.attribute_meter(
            db_session,
            ean="541448000000000005",
            id_community=community.id,
            id_sharing_operation=sharing_operation,
            id_member=None,
        )

        result = await SqlAlchemyCrmCoreRead(db_session).get_operation_participants(
            community.id, sharing_operation
        )

        assert result.participants == ()
        # Structured, not a sentence: the EAN is what lets the client link the
        # reviewer straight to the meter that needs a holder.
        assert result.warnings == (
            PrefillWarning(
                code=WARNING_METER_NO_MEMBER,
                subject_type="meter",
                subject_id="541448000000000005",
            ),
        )


class TestTheOperationsKey:
    """The key the key description lays out: the latest approved one, its
    iterations in order, each with its consumers."""

    async def _key(self, db_session, community, name: str) -> int:
        return int(
            (
                await db_session.execute(
                    text(
                        "INSERT INTO allocation_key (name, description, id_community) "
                        "VALUES (:name, 'Par itérations', :c) RETURNING id"
                    ),
                    {"name": name, "c": community.id},
                )
            ).scalar_one()
        )

    async def _apply(self, db_session, community, operation, key, start, status=1) -> None:
        await db_session.execute(
            text(
                "INSERT INTO sharing_operation_key "
                "(id_sharing_operation, id_key, start_date, status, id_community) "
                "VALUES (:op, :key, :start, :status, :c)"
            ),
            {
                "op": operation,
                "key": key,
                "start": datetime.date.fromisoformat(start),
                "status": status,
                "c": community.id,
            },
        )

    async def _iteration(self, db_session, community, key, number, share, consumers) -> None:
        iteration = (
            await db_session.execute(
                text(
                    "INSERT INTO iteration (number, energy_allocated_percentage, id_key, "
                    " id_community) VALUES (:n, :share, :key, :c) RETURNING id"
                ),
                {"n": number, "share": share, "key": key, "c": community.id},
            )
        ).scalar_one()
        for name, part in consumers:
            await db_session.execute(
                text(
                    "INSERT INTO consumer (name, energy_allocated_percentage, id_iteration, "
                    " id_community) VALUES (:name, :part, :it, :c)"
                ),
                {"name": name, "part": part, "it": iteration, "c": community.id},
            )

    async def test_the_latest_approved_key_comes_with_its_iterations(
        self, db_session, community, sharing_operation
    ):
        old = await self._key(db_session, community, "Ancienne clé")
        new = await self._key(db_session, community, "Nouvelle clé")
        draft = await self._key(db_session, community, "Simulation")
        await self._apply(db_session, community, sharing_operation, old, "2026-01-01")
        await self._apply(db_session, community, sharing_operation, new, "2027-01-01")
        await self._apply(db_session, community, sharing_operation, draft, "2028-01-01", status=2)
        await self._iteration(db_session, community, new, 2, 0.3, [("Ménages", 1.0)])
        await self._iteration(db_session, community, new, 1, 0.7, [("Mairie", 0.4), ("École", 0.6)])

        key = await SqlAlchemyCrmCoreRead(db_session).get_operation_key(
            community.id, sharing_operation
        )

        assert key is not None
        assert (key.name, key.since) == ("Nouvelle clé", datetime.date(2027, 1, 1))
        assert [(it.number, it.share) for it in key.iterations] == [(1, 0.7), (2, 0.3)]
        assert [(c.name, c.share) for c in key.iterations[0].consumers] == [
            ("Mairie", 0.4),
            ("École", 0.6),
        ]

    async def test_an_operation_without_an_approved_key_has_none(
        self, db_session, community, sharing_operation
    ):
        assert (
            await SqlAlchemyCrmCoreRead(db_session).get_operation_key(
                community.id, sharing_operation
            )
            is None
        )

    async def test_another_communitys_key_is_not_read(
        self, db_session, community, other_community, sharing_operation
    ):
        key = await self._key(db_session, community, "Clé")
        await self._apply(db_session, community, sharing_operation, key, "2026-01-01")

        assert (
            await SqlAlchemyCrmCoreRead(db_session).get_operation_key(
                other_community.id, sharing_operation
            )
            is None
        )
