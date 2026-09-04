"""The Phase-2 CRM read port, against a real Postgres.

Unit tests cover the mapping; these cover the SQL — the joins, the scoping, and
the integer/string boundary that a fake would silently paper over.
"""

from __future__ import annotations

import datetime

import pytest

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
