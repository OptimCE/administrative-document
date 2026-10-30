"""CwapePlugin: the route of a sharing modification, and the pieces it needs."""

from __future__ import annotations

from typing import Any

import pytest

from engine.contracts import ChangeFact, ProcedureFacts, RegulatorPlugin
from regulators.cwape.plugin import CwapePlugin
from regulators.registry import REGISTRY
from shared.const import ChangeSubjectType, DocumentKind, ProcedureStatus, RegulatoryStatus

PLUGIN = CwapePlugin()
DEFINITIONS = {e.definition.code: e.definition for e in PLUGIN.catalogue().entries}
MODIFICATION = DEFINITIONS["PARTAGE_MODIF"]


def _meter(kind: str, *, member: int | None = None, injection: int | None = None) -> ChangeFact:
    return ChangeFact(
        kind=kind,
        subject_type=ChangeSubjectType.METER,
        subject_ref=f"5414488000000000{member or 0:02d}",
        payload={"id_member": member, "injection_status": injection},
    )


def _other(kind: str) -> ChangeFact:
    return ChangeFact(kind=kind, subject_type=ChangeSubjectType.KEY, subject_ref="9")


def test_it_is_registered_under_the_community_regulator_code():
    assert isinstance(REGISTRY["BE-WAL-CWAPE"], CwapePlugin)
    assert isinstance(PLUGIN, RegulatorPlugin)
    assert PLUGIN.region == "WAL"


class TestTheRoute:
    @pytest.mark.parametrize(
        ("changes", "route"),
        [
            ([_meter("meter_removed")], "simple"),
            ([_other("key_changed")], "simple"),
            ([_other("representative_changed")], "simple"),
            ([], "simple"),
            ([_meter("meter_added")], "authorization"),
            # A producer's meter carries an installation: removing it needs the CWaPE.
            ([_meter("meter_removed", injection=3)], "authorization"),
            ([_other("installation_removed")], "authorization"),
            # Mixed: one change that needs the authorization moves them all.
            (
                [_meter("meter_removed"), _other("key_changed"), _meter("meter_added")],
                "authorization",
            ),
        ],
    )
    def test_follows_the_changes(self, changes, route):
        assert PLUGIN.variant(MODIFICATION, changes) == route

    def test_a_procedure_without_routes_has_none(self):
        assert PLUGIN.variant(DEFINITIONS["PARTAGE_FIN"], [_meter("meter_added")]) is None


def _facts(code: str, *changes: ChangeFact, variant: str | None = None, **context: Any):
    definition = DEFINITIONS[code]
    return ProcedureFacts(
        definition=definition,
        variant=variant if variant is not None else PLUGIN.variant(definition, changes),
        id_community=1,
        id_sharing_operation=7 if definition.scope == "operation" else None,
        changes=changes,
        context=context,
    )


def _pieces(facts: ProcedureFacts) -> list[tuple[str, int | None, bool]]:
    return [(r.code, r.id_member, r.required) for r in PLUGIN.required_documents(facts)]


class TestPiecesOfAModification:
    def test_a_simple_key_change_with_a_standard_key(self):
        facts = _facts("PARTAGE_MODIF", _other("key_changed"), key_is_standard=True)
        assert _pieces(facts) == [
            ("sharing_modification", None, True),
            ("annex6_sharing_form", None, True),
            # Whether the community's own data changed is not known: offered.
            ("community_modification", None, False),
            ("dso_amendment", None, False),  # arrives after the sending
        ]

    def test_a_non_standard_key_needs_its_description(self):
        facts = _facts("PARTAGE_MODIF", _other("key_changed"), key_is_standard=False)
        assert ("key_description", None, True) in _pieces(facts)

    def test_an_unknown_key_offers_the_description_without_requiring_it(self):
        facts = _facts("PARTAGE_MODIF", _other("key_changed"))
        (requirement,) = [
            r for r in PLUGIN.required_documents(facts) if r.code == "key_description"
        ]
        assert not requirement.required
        assert requirement.condition == {"condition": "non_standard_key", "known": False}

    def test_a_new_representative_proves_their_authority(self):
        facts = _facts("PARTAGE_MODIF", _other("representative_changed"), key_is_standard=True)
        assert ("representative_power_proof", None, True) in _pieces(facts)

    def test_added_meters_need_a_declaration_per_member_and_the_producer_proof(self):
        facts = _facts(
            "PARTAGE_MODIF",
            _meter("meter_added", member=5),
            _meter("meter_added", member=6, injection=1),
            _meter("meter_added", member=5),  # one declaration per member, not per meter
            key_is_standard=True,
        )
        assert facts.variant == "authorization"
        pieces = _pieces(facts)
        assert [p for p in pieces if p[0] == "annex8_sworn_declaration"] == [
            ("annex8_sworn_declaration", 5, True),
            ("annex8_sworn_declaration", 6, True),
        ]
        assert ("producer_status_proof", None, True) in pieces

    def test_consumers_only_need_no_producer_proof(self):
        facts = _facts("PARTAGE_MODIF", _meter("meter_added", member=5), key_is_standard=True)
        assert "producer_status_proof" not in [p[0] for p in _pieces(facts)]

    def test_the_community_update_travels_with_the_request_when_its_data_changed(self):
        changed = _facts("PARTAGE_MODIF", _other("key_changed"), community_info_changed=True)
        unchanged = _facts("PARTAGE_MODIF", _other("key_changed"), community_info_changed=False)
        assert ("community_modification", None, True) in _pieces(changed)
        assert "community_modification" not in [p[0] for p in _pieces(unchanged)]

    def test_a_declaration_kind_is_collected(self):
        facts = _facts("PARTAGE_MODIF", _meter("meter_added", member=5))
        kinds = {r.code: r.kind for r in PLUGIN.required_documents(facts)}
        assert kinds["annex8_sworn_declaration"] == DocumentKind.COLLECTED
        assert kinds["dso_amendment"] == DocumentKind.RECEIVED


class TestPiecesOfACreation:
    def test_nothing_known_yet_offers_what_cannot_be_decided(self):
        pieces = _pieces(_facts("PARTAGE_AUTORISATION"))
        assert ("sharing_notification", None, True) in pieces
        # Nobody to name, unknown key, unknown operation kind: offered, not required.
        assert ("annex8_sworn_declaration", None, False) in pieces
        assert ("key_description", None, False) in pieces
        assert ("dso_agreement_community", None, False) in pieces
        assert ("dso_agreement_building", None, False) in pieces

    def test_the_operation_kind_picks_the_grd_agreement(self):
        pieces = [p[0] for p in _pieces(_facts("PARTAGE_AUTORISATION", operation_kind="community"))]
        assert "dso_agreement_community" in pieces
        assert "dso_agreement_building" not in pieces

    def test_every_participant_declares(self):
        pieces = _pieces(_facts("PARTAGE_AUTORISATION", members=[3, 4], new_installation=True))
        assert ("annex8_sworn_declaration", 3, True) in pieces
        assert ("annex8_sworn_declaration", 4, True) in pieces
        assert ("producer_status_proof", None, True) in pieces


class TestPiecesOfTheCommunity:
    def test_one_annex_7_per_company_member(self):
        pieces = _pieces(_facts("CE_NOTIFICATION", legal_entity_members=[8, 9], has_delegate=False))
        assert [p for p in pieces if p[0] == "annex7_legal_entity"] == [
            ("annex7_legal_entity", 8, True),
            ("annex7_legal_entity", 9, True),
        ]
        assert "delegate_agreement" not in [p[0] for p in pieces]

    def test_no_company_member_no_annex_7(self):
        pieces = _pieces(_facts("CE_NOTIFICATION", legal_entity_members=[]))
        assert "annex7_legal_entity" not in [p[0] for p in pieces]

    def test_an_update_asks_for_what_changed(self):
        pieces = _pieces(
            _facts("CE_ACTUALISATION", statutes_changed=True, internal_rules_changed=False)
        )
        codes = [p[0] for p in pieces]
        assert ("statutes", None, True) in pieces
        assert "internal_rules" not in codes

    def test_the_annual_report(self):
        assert _pieces(_facts("CE_RAPPORT_ANNUEL")) == [
            ("annual_participant_list", None, True),
            ("annual_activity_report", None, True),
        ]


class TestWhatTheCoreShows:
    def test_each_sent_step_of_a_sharing_modification(self):
        definition = PLUGIN.catalogue().get("PARTAGE_MODIF", 1).definition
        statuses = {
            step.code: PLUGIN.episode_status(definition, step.code)
            for step in definition.steps
            if step.common_status == ProcedureStatus.SENT
        }
        assert statuses == {
            # with the GRD
            "sent": RegulatoryStatus.PENDING_GRD,
            "incomplete": RegulatoryStatus.PENDING_GRD,
            "in_review": RegulatoryStatus.PENDING_GRD,
            # with the CWaPE
            "at_regulator": RegulatoryStatus.PENDING_REGULATOR,
            "non_conformity": RegulatoryStatus.PENDING_REGULATOR,
            # authorised: the amendment remains
            "granted": RegulatoryStatus.AUTHORIZED,
            "accepted": RegulatoryStatus.AUTHORIZED,
            "amendment_signed": RegulatoryStatus.AUTHORIZED,
        }


class TestTheRest:
    def test_the_render_context_passes_the_snapshot_through(self):
        snapshot = {"community": {"name": "CER"}}
        assert PLUGIN.render_context("annex6_sharing_form", snapshot) == snapshot
