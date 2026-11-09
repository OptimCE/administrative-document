"""engine.contracts: the plugin contract, the registry, and the fake regulator."""

from __future__ import annotations

import datetime
import json
from pathlib import Path

import pytest

from engine.contracts import (
    ChangeFact,
    ProcedureFacts,
    RegistryError,
    RegulatorPlugin,
    build_registry,
    plugin_for,
)
from regulators.registry import REGISTRY
from regulators.testing.fake import FakeRegulator
from shared.const import ChangeSubjectType, DocumentKind, ProcedureStatus, RegulatoryStatus

D = datetime.date
REGULATORS_JSON = Path(__file__).resolve().parents[1] / "fixtures" / "regulators.json"


def _meter(kind: str, ean: str = "541448800000000001") -> ChangeFact:
    return ChangeFact(kind=kind, subject_type=ChangeSubjectType.METER, subject_ref=ean)


class TestTheRegistry:
    def test_the_fake_implements_the_contract(self):
        assert isinstance(FakeRegulator(), RegulatorPlugin)

    def test_it_is_keyed_by_regulator_and_read_only(self):
        plugin = FakeRegulator()
        registry = build_registry([plugin])
        assert dict(registry) == {"TEST-FAKE": plugin}
        with pytest.raises(TypeError):
            registry["OTHER"] = plugin

    def test_two_plugins_cannot_claim_one_regulator(self):
        with pytest.raises(RegistryError, match="two plugins claim regulator TEST-FAKE"):
            build_registry([FakeRegulator(), FakeRegulator()])

    def test_two_plugins_cannot_claim_one_region(self):
        with pytest.raises(RegistryError, match="both claim region TST"):
            build_registry([FakeRegulator(), FakeRegulator(code="TEST-OTHER")])

    def test_a_plugin_owns_its_catalogue(self):
        class Borrowing(FakeRegulator):
            def catalogue(self):
                return FakeRegulator(code="TEST-OTHER").catalogue()

        with pytest.raises(RegistryError, match="returns the catalogue of TEST-OTHER"):
            build_registry([Borrowing()])

    def test_a_plugin_counts_on_its_own_calendar(self):
        plugin = FakeRegulator()
        plugin.region = "XYZ"
        with pytest.raises(RegistryError, match="counts on region XYZ"):
            build_registry([plugin])

    def test_anything_else_is_refused(self):
        with pytest.raises(RegistryError, match="does not implement RegulatorPlugin"):
            build_registry([object()])

    def test_a_community_without_a_plugin_gets_none(self):
        registry = build_registry([FakeRegulator()])
        assert plugin_for(registry, "TEST-FAKE") is registry["TEST-FAKE"]
        assert plugin_for(registry, "BE-BRU-BRUGEL") is None
        assert plugin_for(registry, None) is None
        assert plugin_for(registry, "") is None


class TestTheProductionRegistry:
    def test_registers_no_test_double(self):
        assert not [p for p in REGISTRY.values() if isinstance(p, FakeRegulator)]

    def test_registers_only_active_regulators(self):
        active = {e["code"] for e in json.loads(REGULATORS_JSON.read_text("utf-8")) if e["active"]}
        assert set(REGISTRY) <= active


class TestTheFakeRegulator:
    plugin = FakeRegulator()
    change = next(
        e.definition for e in plugin.catalogue().entries if e.definition.code == "FAKE_CHANGE"
    )

    def test_an_added_meter_escalates_the_route(self):
        assert self.plugin.variant(self.change, [_meter("meter_removed")]) == "light"
        assert self.plugin.variant(
            self.change, [_meter("meter_removed"), _meter("meter_added")]
        ) == ("full")
        assert self.plugin.variant(self.change, []) == "light"

    def _facts(self, variant: str, **context) -> ProcedureFacts:
        return ProcedureFacts(
            definition=self.change,
            variant=variant,
            id_community=1,
            id_sharing_operation=7,
            context=context,
        )

    def test_conditional_and_per_member_pieces(self):
        light = self.plugin.required_documents(self._facts("light"))
        assert [(r.code, r.required) for r in light] == [
            ("change_form", True),
            ("key_description", False),
        ]
        full = self.plugin.required_documents(
            self._facts("full", members=[11, 12], conditions=["non_standard_key"])
        )
        assert [(r.code, r.id_member, r.required) for r in full] == [
            ("change_form", None, True),
            ("member_declaration", 11, True),
            ("member_declaration", 12, True),
            ("key_description", None, True),
        ]
        assert full[1].kind == DocumentKind.COLLECTED

    def test_its_sent_steps_mean_a_status_for_the_core(self):
        statuses = {
            step.code: self.plugin.episode_status(self.change, step.code)
            for step in self.change.steps
            if step.common_status == ProcedureStatus.SENT
        }
        assert statuses == {
            "sent": RegulatoryStatus.PENDING_GRD,
            "missing_info": RegulatoryStatus.PENDING_GRD,
            "reviewed": RegulatoryStatus.PENDING_REGULATOR,
            "accepted": RegulatoryStatus.AUTHORIZED,
            "signed": RegulatoryStatus.AUTHORIZED,
        }


class TestTheEpisodeStatuses:
    def test_a_sent_step_the_plugin_does_not_map_is_refused(self):
        class Forgetful(FakeRegulator):
            def episode_status(self, definition, step):
                return None if step == "reviewed" else super().episode_status(definition, step)

        with pytest.raises(RegistryError, match="step reviewed means None"):
            build_registry([Forgetful()])

    def test_a_sent_step_cannot_mean_active(self):
        class Hasty(FakeRegulator):
            def episode_status(self, definition, step):
                return RegulatoryStatus.ACTIVE

        with pytest.raises(RegistryError, match="not one of"):
            build_registry([Hasty()])

    def test_a_procedure_filing_no_tracked_change_is_not_asked(self):
        asked = []

        class Recording(FakeRegulator):
            def episode_status(self, definition, step):
                asked.append(definition.code)
                return super().episode_status(definition, step)

        build_registry([Recording()])
        assert set(asked) == {"FAKE_CHANGE"}

    def test_every_cwape_sent_step_of_a_sharing_procedure_is_mapped(self):
        build_registry(REGISTRY.values())  # raises if one is not
