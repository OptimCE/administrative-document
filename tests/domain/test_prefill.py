"""Snapshot assembly and the CWaPE label vocabulary.

Pure-domain tests: no database, no broker, no template bundle.
"""

from __future__ import annotations

import dataclasses
import datetime

import pytest

from domain import cwape_labels as labels
from domain import prefill
from ports.crm_core import (
    CommunityContext,
    Installation,
    MeterPoint,
    Participant,
    PostalAddress,
)
from shared.const import Region

_COMMUNITY = CommunityContext(
    id_community=1,
    name="CE du Condroz",
    regulator="BE-WAL-CWAPE",
    region=Region.WAL,
    legal_name="Communauté d'énergie du Condroz ASBL",
    vat_number="BE0123456789",
    address=PostalAddress(street="Rue Haute", number="12", postcode="5000", city="Namur"),
)

_ALICE = Participant(
    id_member=10,
    name="Dupont",
    member_type=labels.MemberType.INDIVIDUAL,
    first_name="Alice",
    email="alice@example.be",
    address=PostalAddress(street="Rue Basse", number="3", postcode="5000", city="Namur"),
    meters=(
        MeterPoint(
            ean="541448000000000001",
            meter_number="M-1",
            grd="ores",
            client_type=labels.ClientType.RESIDENTIAL,
            address=PostalAddress(street="Rue Basse", number="3", postcode="5000", city="Namur"),
        ),
        MeterPoint(
            ean="541448000000000002",
            grd="ORES",
            client_type=labels.ClientType.PROFESSIONAL,
        ),
    ),
)

_ACME = Participant(
    id_member=11,
    name="ACME SRL",
    member_type=labels.MemberType.COMPANY,
    vat_number="BE0987654321",
    address=PostalAddress(street="Chaussée", number="99", postcode="4000", city="Liège"),
    meters=(MeterPoint(ean="541448000000000003", grd="RESA"),),
)

_PV = Installation(
    ean="541448000000000009",
    meter_number="P-1",
    grd="RESA",
    id_member=11,
    member_name="ACME SRL",
    member_type=labels.MemberType.COMPANY,
    injection_status=labels.InjectionStatus.AUTOPROD_OWNER,
    production_chain=labels.ProductionChain.PHOTOVOLTAIC,
    generating_capacity=9.5,
    commissioned_on=datetime.date(2026, 3, 1),
    address=PostalAddress(street="Chaussée", number="99", postcode="4000", city="Liège"),
)


# ---------------------------------------------------------------------------
# Label mapping
# ---------------------------------------------------------------------------


class TestLabels:
    @pytest.mark.parametrize("raw", ["ORES", "ores", " Ores "])
    def test_grd_is_normalised_to_the_accepted_spelling(self, raw):
        assert labels.grd_label(raw) == "ORES"

    def test_an_unknown_grd_becomes_none_rather_than_a_guess(self):
        """A DSO the form does not list would show as a validation error."""
        assert labels.grd_label("Sibelga") is None
        assert labels.grd_label(None) is None

    def test_only_the_residential_segment_is_oui(self):
        assert labels.residential_label(labels.ClientType.RESIDENTIAL) == "OUI"
        assert labels.residential_label(labels.ClientType.PROFESSIONAL) == "NON"
        assert labels.residential_label(labels.ClientType.INDUSTRIAL) == "NON"
        assert labels.residential_label(None) is None

    def test_producer_status_uses_the_sharing_form_vocabulary(self):
        assert (
            labels.producer_status_label(labels.InjectionStatus.AUTOPROD_RIGHTS)
            == "Autoproducteur - droit de jouissance"
        )

    def test_notification_form_uses_a_different_producer_vocabulary(self):
        """The two annex-6 forms validate this column against different lists."""
        status = labels.InjectionStatus.AUTOPROD_RIGHTS
        assert labels.community_installation_status_label(status) == (
            "CE titulaire d'un droit de jouissance sur l'installation"
        )
        assert labels.producer_status_label(status) != (
            labels.community_installation_status_label(status)
        )

    def test_other_production_chain_has_no_label(self):
        """The forms offer no catch-all, so OTHER must not be invented."""
        assert labels.production_chain_label(labels.ProductionChain.OTHER) is None
        assert labels.production_chain_label(labels.ProductionChain.WIND) == "Eolien"

    def test_display_name_joins_a_first_name_only_for_individuals(self):
        assert labels.display_name("Dupont", "Alice", labels.MemberType.INDIVIDUAL) == (
            "Alice Dupont"
        )
        assert labels.display_name("ACME SRL", "Alice", labels.MemberType.COMPANY) == "ACME SRL"
        assert labels.display_name("Dupont", None, labels.MemberType.INDIVIDUAL) == "Dupont"

    def test_every_subcategory_key_is_an_offered_category(self):
        assert set(labels.MEMBER_SUBCATEGORY_CHOICES) <= set(labels.MEMBER_CATEGORY_CHOICES)


# ---------------------------------------------------------------------------
# Snapshot assembly
# ---------------------------------------------------------------------------


class TestSnapshot:
    def test_community_header_is_present_for_every_doc_type(self):
        snapshot = prefill.build_snapshot("something_unregistered", community=_COMMUNITY)
        assert snapshot["community_legal_name"] == "Communauté d'énergie du Condroz ASBL"
        assert snapshot["community_vat_number"] == "BE0123456789"
        assert snapshot["community_address"] == "Rue Haute 12, 5000 Namur"

    def test_legal_name_falls_back_to_the_display_name(self):
        anonymous = CommunityContext(
            id_community=2, name="CE Test", regulator=None, region=None, legal_name=None
        )
        assert prefill.build_snapshot("x", community=anonymous)["community_legal_name"] == (
            "CE Test"
        )

    def test_sharing_form_lists_one_row_per_delivery_point(self):
        """This sheet is a list of EANs, so a member with two meters appears twice."""
        snapshot = prefill.build_snapshot(
            prefill.DOC_TYPE_ANNEX6_SHARING_FORM,
            community=_COMMUNITY,
            participants=[_ALICE, _ACME],
            production=[_PV],
        )
        rows = snapshot["participants"]
        assert len(rows) == 3
        assert rows[0]["ean"] == "541448000000000001"
        assert rows[0]["nom"] == "Dupont"
        assert rows[0]["prenom"] == "Alice"
        assert rows[0]["denomination"] is None
        assert rows[0]["grd"] == "ORES"
        assert rows[0]["client_residentiel"] == "OUI"

    def test_a_company_fills_denomination_not_nom(self):
        snapshot = prefill.build_snapshot(
            prefill.DOC_TYPE_ANNEX6_SHARING_FORM, community=_COMMUNITY, participants=[_ACME]
        )
        row = snapshot["participants"][0]
        assert row["denomination"] == "ACME SRL"
        assert row["nom"] is None and row["prenom"] is None

    def test_notification_form_lists_one_row_per_member(self):
        """Unlike the sharing form: this sheet is a list of people, not meters."""
        snapshot = prefill.build_snapshot(
            prefill.DOC_TYPE_ANNEX6_NOTIFICATION,
            community=_COMMUNITY,
            participants=[_ALICE, _ACME],
            production=[_PV],
        )
        rows = snapshot["members"]
        assert len(rows) == 2  # not 3, despite Alice's two meters
        assert rows[0]["nom"] == "Alice Dupont"
        assert rows[0]["categorie"] == "Personne physique"
        assert rows[1]["categorie"] == "Entreprise"
        assert rows[1]["numero_entreprise"] == "BE0987654321"

    def test_installation_status_vocabulary_follows_the_form(self):
        notification = prefill.build_snapshot(
            prefill.DOC_TYPE_ANNEX6_NOTIFICATION, community=_COMMUNITY, production=[_PV]
        )["installations"][0]
        sharing = prefill.build_snapshot(
            prefill.DOC_TYPE_ANNEX6_SHARING_FORM, community=_COMMUNITY, production=[_PV]
        )["installations"][0]

        assert notification["statut_producteur"] == "CE propriétaire de l'installation"
        assert sharing["statut_producteur"] == "Autoproducteur - propriétaire"
        assert sharing["filiere"] == "Photovoltaïque"
        assert sharing["puissance_kva"] == 9.5
        assert sharing["date_mise_en_service"] == "2026-03-01"

    def test_fields_the_crm_cannot_know_are_blank_not_guessed(self):
        snapshot = prefill.build_snapshot(
            prefill.DOC_TYPE_ANNEX6_SHARING_FORM,
            community=_COMMUNITY,
            participants=[_ALICE],
            production=[_PV],
        )
        assert snapshot["participants"][0]["part_allouee"] is None
        assert snapshot["installations"][0]["statut_installation"] is None

    def test_acroform_documents_get_counts(self):
        snapshot = prefill.build_snapshot(
            prefill.DOC_TYPE_ANNEX8_SWORN_DECLARATION,
            community=_COMMUNITY,
            participants=[_ALICE, _ACME],
            production=[_PV],
        )
        assert snapshot["participant_count"] == 3
        assert snapshot["installation_count"] == 1

    def test_a_dso_convention_seeds_every_field_the_contract_needs(self):
        """docxtpl runs with StrictUndefined: a key that is absent, rather than
        blank, is a render that fails the first time anyone uses it."""
        snapshot = prefill.build_snapshot(
            prefill.DOC_TYPE_DSO_AGREEMENT_COMMUNITY, community=_COMMUNITY
        )
        for field in prefill._DSO_AGREEMENT_FIELDS:
            assert field in snapshot
        assert snapshot["community_address"] == "Rue Haute 12, 5000 Namur"
        # The community is the one party the CRM does know.
        assert snapshot["representative_name"] == "Communauté d'énergie du Condroz ASBL"
        # Negotiated terms are blank for the reviewer, never guessed.
        assert snapshot["distribution_key"] is None
        assert snapshot["start_date"] is None

    def test_the_building_convention_has_its_own_extra_clause(self):
        """It splits the preamble in two; the community one states it in one."""
        building = prefill.build_snapshot(
            prefill.DOC_TYPE_DSO_AGREEMENT_BUILDING, community=_COMMUNITY
        )
        community = prefill.build_snapshot(
            prefill.DOC_TYPE_DSO_AGREEMENT_COMMUNITY, community=_COMMUNITY
        )
        assert "unmet_condition" in building
        assert "unmet_condition" not in community
        assert "community_address" in community

    def test_storage_is_empty_because_the_crm_does_not_model_it(self):
        snapshot = prefill.build_snapshot(
            prefill.DOC_TYPE_ANNEX6_SHARING_FORM, community=_COMMUNITY, participants=[_ALICE]
        )
        assert snapshot["storage"] == []


class TestOverrides:
    def test_a_scalar_correction_wins_over_the_crm(self):
        snapshot = prefill.build_snapshot(
            prefill.DOC_TYPE_ANNEX6_NOTIFICATION, community=_COMMUNITY, participants=[_ALICE]
        )
        merged = prefill.merge_overrides(snapshot, {"community_vat_number": "BE0000000000"})
        assert merged["community_vat_number"] == "BE0000000000"
        assert merged["community_name"] == "CE du Condroz"  # untouched

    def test_a_submitted_list_replaces_rather_than_merges(self):
        """Row-wise merging would resurrect a participant the reviewer deleted."""
        snapshot = prefill.build_snapshot(
            prefill.DOC_TYPE_ANNEX6_NOTIFICATION,
            community=_COMMUNITY,
            participants=[_ALICE, _ACME],
        )
        merged = prefill.merge_overrides(snapshot, {"members": [{"nom": "Only One"}]})
        assert merged["members"] == [{"nom": "Only One"}]

    def test_merging_does_not_mutate_the_source_snapshot(self):
        snapshot = prefill.build_snapshot(
            prefill.DOC_TYPE_ANNEX6_NOTIFICATION, community=_COMMUNITY, participants=[_ALICE]
        )
        before = snapshot["members"][0]["nom"]
        merged = prefill.merge_overrides(snapshot, {"members": [{"nom": "Changed"}]})
        merged["members"][0]["nom"] = "Changed again"
        assert snapshot["members"][0]["nom"] == before

    def test_no_overrides_is_a_faithful_copy(self):
        snapshot = prefill.build_snapshot(
            prefill.DOC_TYPE_ANNEX6_NOTIFICATION, community=_COMMUNITY, participants=[_ALICE]
        )
        assert prefill.merge_overrides(snapshot, None) == snapshot


# ---------------------------------------------------------------------------
# Prefill warnings
# ---------------------------------------------------------------------------


def _fields(warnings) -> list[str]:
    return [w.params["field"] for w in warnings]


class TestCommunityWarnings:
    def test_a_complete_community_warns_about_nothing(self):
        assert prefill.community_warnings(_COMMUNITY) == ()

    def test_a_missing_legal_name_is_reported(self):
        """The warning that earns this function.

        `community_block` falls back to the DISPLAY name when there is no legal
        name, so the form fills in and looks correct while carrying the wrong
        legal identity onto a document filed with the regulator.
        """
        community = dataclasses.replace(_COMMUNITY, legal_name=None)

        warnings = prefill.community_warnings(community)

        assert _fields(warnings) == ["community_legal_name"]
        assert warnings[0].code == "community.field_missing"
        assert warnings[0].subject_type == "community"
        assert warnings[0].subject_id is None
        # And the snapshot really does hide it, which is why the warning matters.
        assert prefill.community_block(community)["community_legal_name"] == community.name

    def test_a_blank_string_counts_as_missing(self):
        community = dataclasses.replace(_COMMUNITY, vat_number="   ")
        assert _fields(prefill.community_warnings(community)) == ["community_vat_number"]

    def test_a_missing_address_reports_every_cell_it_feeds(self):
        community = dataclasses.replace(_COMMUNITY, address=None)
        assert _fields(prefill.community_warnings(community)) == [
            "community_street",
            "community_number",
            "community_postcode",
            "community_city",
        ]


class TestParticipantWarnings:
    def test_a_doc_type_without_a_members_sheet_warns_about_nothing(self):
        # Warning about a member field on a form that never asks for one is
        # noise, and noise buries the two warnings that matter.
        assert prefill.participant_warnings(prefill.DOC_TYPE_ANNEX6_SHARING_FORM, [_ACME]) == ()
        assert prefill.participant_warnings(prefill.DOC_TYPE_DSO_AGREEMENT_COMMUNITY, [_ACME]) == ()

    def test_complete_participants_warn_about_nothing(self):
        assert (
            prefill.participant_warnings(prefill.DOC_TYPE_ANNEX6_NOTIFICATION, [_ALICE, _ACME])
            == ()
        )

    def test_a_company_without_a_vat_number_is_reported(self):
        company = dataclasses.replace(_ACME, vat_number=None)

        warnings = prefill.participant_warnings(prefill.DOC_TYPE_ANNEX6_NOTIFICATION, [company])

        assert _fields(warnings) == ["numero_entreprise"]
        assert warnings[0].subject_type == "member"
        assert warnings[0].subject_id == str(company.id_member)

    def test_an_individual_without_a_vat_number_is_not_reported(self):
        # `numero_entreprise` is a company cell; the form leaves it blank for an
        # individual by design.
        individual = dataclasses.replace(_ALICE, vat_number=None)
        assert (
            prefill.participant_warnings(prefill.DOC_TYPE_ANNEX6_NOTIFICATION, [individual]) == ()
        )

    def test_a_missing_member_address_is_reported(self):
        individual = dataclasses.replace(_ALICE, address=None)

        warnings = prefill.participant_warnings(prefill.DOC_TYPE_ANNEX6_NOTIFICATION, [individual])

        assert _fields(warnings) == ["rue", "code_postal", "localite"]
        assert {w.subject_id for w in warnings} == {str(individual.id_member)}


class TestSortWarnings:
    def test_orders_deterministically_so_the_panel_does_not_reshuffle(self):
        community = dataclasses.replace(_COMMUNITY, legal_name=None, vat_number=None)
        unordered = (
            *prefill.participant_warnings(
                prefill.DOC_TYPE_ANNEX6_NOTIFICATION,
                [dataclasses.replace(_ACME, vat_number=None)],
            ),
            *prefill.community_warnings(community),
        )

        first = prefill.sort_warnings(unordered)
        second = prefill.sort_warnings(reversed(list(unordered)))

        assert first == second
        # Grouped by subject: community rows before member rows.
        assert [w.subject_type for w in first] == ["community", "community", "member"]


class TestRowIdentity:
    """`ROW_MEMBER_ID` is what makes `GET /filings/mine` possible at all.

    It is stamped at freeze time so a member's own rows can be found later
    without re-reading the CRM (which would show today's data, not what was
    filed) and without matching on a display name (which collides on homonyms).
    """

    def test_every_member_bearing_block_carries_the_member_id(self):
        sharing = prefill.build_snapshot(
            prefill.DOC_TYPE_ANNEX6_SHARING_FORM,
            community=_COMMUNITY,
            participants=[_ALICE, _ACME],
            production=[_PV],
        )
        notification = prefill.build_snapshot(
            prefill.DOC_TYPE_ANNEX6_NOTIFICATION,
            community=_COMMUNITY,
            participants=[_ALICE, _ACME],
            production=[_PV],
        )

        assert {row[prefill.ROW_MEMBER_ID] for row in sharing["participants"]} == {10, 11}
        assert {row[prefill.ROW_MEMBER_ID] for row in notification["members"]} == {10, 11}
        assert all(prefill.ROW_MEMBER_ID in row for row in sharing["installations"])

    def test_the_key_is_not_a_rendered_field(self):
        snapshot = prefill.build_snapshot(
            prefill.DOC_TYPE_ANNEX6_NOTIFICATION, community=_COMMUNITY, participants=[_ALICE]
        )
        # Underscore-prefixed and absent from every manifest, so the renderers —
        # which project only their declared columns — cannot put it on a form.
        assert prefill.ROW_MEMBER_ID.startswith("_")
        assert prefill.ROW_MEMBER_ID not in prefill.member_rows([_ALICE])[0].keys() - {
            prefill.ROW_MEMBER_ID
        }
        assert snapshot["members"][0][prefill.ROW_MEMBER_ID] == 10


class TestRowsForMembers:
    def test_returns_only_the_requested_members_rows(self):
        snapshot = prefill.build_snapshot(
            prefill.DOC_TYPE_ANNEX6_SHARING_FORM,
            community=_COMMUNITY,
            participants=[_ALICE, _ACME],
        )

        rows = prefill.rows_for_members(snapshot, {10})

        # One row per EAN, not per member, so Alice's two meters give two rows —
        # and ACME's is absent, which is the point.
        assert {row[prefill.ROW_MEMBER_ID] for row in rows["participants"]} == {10}
        assert len(rows["participants"]) == len(_ALICE.meters)

    def test_a_row_without_an_id_belongs_to_nobody(self):
        # A snapshot frozen before identity stamping existed.
        legacy = {"participants": [{"ean": "541448000000000001", "nom": "Dupont"}]}

        assert prefill.rows_for_members(legacy, {10})["participants"] == []

    def test_an_unparseable_id_is_not_a_match(self):
        hostile = {"participants": [{prefill.ROW_MEMBER_ID: "not-a-number", "ean": "X"}]}

        # The reviewer can post arbitrary JSON into an override, so the id is
        # not trustworthy input — it must fail closed rather than raise.
        assert prefill.rows_for_members(hostile, {10})["participants"] == []

    def test_no_member_ids_matches_nothing(self):
        snapshot = prefill.build_snapshot(
            prefill.DOC_TYPE_ANNEX6_SHARING_FORM, community=_COMMUNITY, participants=[_ALICE]
        )

        assert prefill.rows_for_members(snapshot, set())["participants"] == []

    def test_a_missing_snapshot_is_not_an_error(self):
        assert prefill.rows_for_members(None, {10}) == {
            block: [] for block in prefill.MEMBER_ROW_BLOCKS
        }


class TestOverridesPreserveIdentity:
    """The reviewer's corrections must not strip the rows' identity.

    The generate dialog renders the derived rows and posts them back as plain
    JSON, with no reason to echo an internal id. Without re-stamping, editing a
    single cell would hide the whole filing from every member it names.
    """

    def test_restores_the_id_by_ean(self):
        snapshot = prefill.build_snapshot(
            prefill.DOC_TYPE_ANNEX6_SHARING_FORM, community=_COMMUNITY, participants=[_ALICE]
        )

        merged = prefill.merge_overrides(
            snapshot, {"participants": [{"ean": "541448000000000001", "localite": "Jambes"}]}
        )

        assert merged["participants"][0][prefill.ROW_MEMBER_ID] == 10
        assert merged["participants"][0]["localite"] == "Jambes"

    def test_restores_the_id_by_name_where_there_is_no_ean(self):
        snapshot = prefill.build_snapshot(
            prefill.DOC_TYPE_ANNEX6_NOTIFICATION, community=_COMMUNITY, participants=[_ALICE]
        )
        derived_name = snapshot["members"][0]["nom"]

        merged = prefill.merge_overrides(
            snapshot, {"members": [{"nom": derived_name, "localite": "Jambes"}]}
        )

        assert merged["members"][0][prefill.ROW_MEMBER_ID] == 10

    def test_a_row_the_reviewer_invented_gets_no_id(self):
        snapshot = prefill.build_snapshot(
            prefill.DOC_TYPE_ANNEX6_SHARING_FORM, community=_COMMUNITY, participants=[_ALICE]
        )

        merged = prefill.merge_overrides(
            snapshot, {"participants": [{"ean": "541448000000000099", "nom": "Inconnu"}]}
        )

        # Attributing an unrecognised row to somebody would be worse than
        # leaving it unattributed: it would show one member another's data.
        assert prefill.ROW_MEMBER_ID not in merged["participants"][0]

    def test_a_homonym_is_left_unattributed_rather_than_guessed(self):
        twin = dataclasses.replace(_ALICE, id_member=99, meters=())
        snapshot = prefill.build_snapshot(
            prefill.DOC_TYPE_ANNEX6_NOTIFICATION, community=_COMMUNITY, participants=[_ALICE, twin]
        )
        derived_name = snapshot["members"][0]["nom"]

        merged = prefill.merge_overrides(snapshot, {"members": [{"nom": derived_name}]})

        assert prefill.ROW_MEMBER_ID not in merged["members"][0]

    def test_an_explicit_id_in_the_override_is_kept(self):
        snapshot = prefill.build_snapshot(
            prefill.DOC_TYPE_ANNEX6_SHARING_FORM, community=_COMMUNITY, participants=[_ALICE]
        )

        merged = prefill.merge_overrides(
            snapshot,
            {"participants": [{prefill.ROW_MEMBER_ID: 10, "ean": "541448000000000001"}]},
        )

        assert merged["participants"][0][prefill.ROW_MEMBER_ID] == 10


class TestAnnex8CombBoxes:
    """The CWaPE sworn declaration's two EAN rows.

    16 one-character boxes each, preceded by a pre-printed "5 4" — so the form
    holds an 18-digit EAN only as the 16 digits that follow the prefix. The
    regulator's own body text on that PDF says the EAN is 18 digits.
    """

    def test_derives_the_sixteen_digits_after_the_printed_prefix(self):
        assert prefill.ean_comb_digits("541448200000000001") == "1448200000000001"

    def test_the_result_is_exactly_sixteen_characters(self):
        # The bundle manifest declares maxLength 16 and document-generation
        # validates `data` against it BEFORE rendering, so anything longer is a
        # permanent VALIDATION_ERROR rather than a filled form.
        assert len(prefill.ean_comb_digits("541448200000000001")) == 16

    @pytest.mark.parametrize(
        "ean",
        [
            "441448200000000001",  # 18 digits, wrong prefix: unprintable
            "5414482000000",  # legacy 13 digits: starts 54 and is STILL unprintable
            "54144820000000001",  # 17 digits
            "5414482000000000011",  # 19 digits
            "54144820000000000A",  # not all digits
            "",
            None,
            541448200000000001,  # an int must never be accepted
        ],
    )
    def test_returns_none_rather_than_truncating(self, ean):
        assert prefill.ean_comb_digits(ean) is None

    def test_tolerates_surrounding_whitespace(self):
        assert prefill.ean_comb_digits("  541448200000000001  ") == "1448200000000001"

    def test_projection_sets_the_comb_keys(self):
        snapshot = {"ean_delivery": "541448200000000001", "ean_injection": None}
        prefill.project_comb_fields(prefill.DOC_TYPE_ANNEX8_SWORN_DECLARATION, snapshot)
        assert snapshot["ean_delivery_digits"] == "1448200000000001"
        # Absent, not blank: pdf_form skips an unmapped key and leaves the
        # regulator's own boxes untouched and hand-fillable.
        assert "ean_injection_digits" not in snapshot

    def test_projection_removes_stale_digits_when_the_ean_becomes_invalid(self):
        # merge_overrides is shallow, so a reviewer editing the EAN would
        # otherwise leave the previous EAN's digits in the frozen snapshot.
        snapshot = {"ean_delivery": "441448200000000001", "ean_delivery_digits": "1448200000000001"}
        prefill.project_comb_fields(prefill.DOC_TYPE_ANNEX8_SWORN_DECLARATION, snapshot)
        assert "ean_delivery_digits" not in snapshot

    def test_projection_is_a_no_op_for_other_doc_types(self):
        snapshot = {"ean_delivery": "541448200000000001"}
        prefill.project_comb_fields(prefill.DOC_TYPE_ANNEX6_SHARING_FORM, snapshot)
        assert "ean_delivery_digits" not in snapshot

    def test_warns_once_per_unprintable_ean(self):
        warnings = prefill.form_representation_warnings(
            prefill.DOC_TYPE_ANNEX8_SWORN_DECLARATION,
            {"ean_delivery": "441448200000000001", "ean_injection": "541448200000000002"},
        )
        assert len(warnings) == 1
        assert warnings[0].code == "meter.ean_not_form_representable"
        assert warnings[0].subject_type == "meter"
        assert warnings[0].subject_id == "441448200000000001"
        assert warnings[0].params["field"] == "ean_delivery"

    def test_a_blank_ean_is_not_a_warning(self):
        # The reviewer can see an empty field; nagging would bury the warnings
        # that matter.
        assert (
            prefill.form_representation_warnings(
                prefill.DOC_TYPE_ANNEX8_SWORN_DECLARATION,
                {"ean_delivery": None, "ean_injection": "   "},
            )
            == ()
        )

    def test_silent_for_other_doc_types(self):
        assert (
            prefill.form_representation_warnings(
                prefill.DOC_TYPE_ANNEX6_SHARING_FORM, {"ean_delivery": "441448200000000001"}
            )
            == ()
        )

    def test_snapshot_seeds_the_ean_only_when_unambiguous(self):
        one_meter = dataclasses.replace(_ALICE, meters=(_ALICE.meters[0],))
        snapshot = prefill.build_snapshot(
            prefill.DOC_TYPE_ANNEX8_SWORN_DECLARATION,
            community=_COMMUNITY,
            participants=[one_meter],
            production=[_PV],
        )
        assert snapshot["ean_delivery"] == one_meter.meters[0].ean
        assert snapshot["ean_injection"] == _PV.ean

    def test_snapshot_leaves_the_ean_blank_when_there_is_a_choice(self):
        # A guessed EAN on a sworn declaration is worse than a blank.
        snapshot = prefill.build_snapshot(
            prefill.DOC_TYPE_ANNEX8_SWORN_DECLARATION,
            community=_COMMUNITY,
            participants=[_ALICE, _ACME],
            production=[_PV],
        )
        assert snapshot["ean_delivery"] is None
