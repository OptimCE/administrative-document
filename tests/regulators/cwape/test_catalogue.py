"""The CWaPE catalogue: the seven procedure types of the spec, as data."""

from __future__ import annotations

import datetime
import re
from pathlib import Path

import pytest

from engine.contracts import ChangeFact, ProcedureFacts
from regulators.cwape.plugin import CwapePlugin, _condition, _members
from shared.const import ChangeSubjectType, Channel, ProcedureScope, Recipient

D = datetime.date
SEEDS = Path(__file__).resolve().parents[3] / "scripts" / "sql" / "seeds"
PLUGIN = CwapePlugin()
CATALOGUE = PLUGIN.catalogue()
# The latest version of each code (entries run by code, then version).
DEFINITIONS = {e.definition.code: e.definition for e in CATALOGUE.entries}
PHASE_FOUR = D(2026, 11, 1)
# The phase-4 triggers: a second version of each, in force from 1 November 2026.
REVISED = ["CE_ACTUALISATION", "CE_NOTIFICATION", "PARTAGE_AUTORISATION", "PARTAGE_FIN"]


def test_six_codes_and_the_baseline_in_their_versions():
    assert sorted(DEFINITIONS) == [
        "CE_ACTUALISATION",
        "CE_NOTIFICATION",
        "CE_RAPPORT_ANNUEL",
        "ETAT_DE_DEPART",
        "PARTAGE_AUTORISATION",
        "PARTAGE_FIN",
        "PARTAGE_MODIF",
    ]
    versions = {
        code: [e.definition.version for e in CATALOGUE.versions(code)] for code in DEFINITIONS
    }
    assert versions == {code: [1, 2] if code in REVISED else [1] for code in DEFINITIONS}
    for code in REVISED:
        first, second = CATALOGUE.versions(code)
        assert first.valid_to == PHASE_FOUR - datetime.timedelta(days=1)
        assert (second.definition.valid_from, second.valid_to) == (PHASE_FOUR, None)


def test_the_modification_has_its_two_routes():
    modification = DEFINITIONS["PARTAGE_MODIF"]
    assert modification.variants == ("simple", "authorization")
    assert modification.default_variant == "simple"
    # The simple route skips the CWaPE: the GRD's check leads to the amendment.
    assert modification.allows("sent", "accepted", "simple")
    assert not modification.allows("sent", "accepted", "authorization")
    assert "at_regulator" not in [s.code for s in modification.steps_for("simple")]


@pytest.mark.parametrize(
    ("code", "scope", "recipient", "channel"),
    [
        ("PARTAGE_MODIF", ProcedureScope.OPERATION, Recipient.GRD_REF, Channel.EMAIL),
        ("PARTAGE_AUTORISATION", ProcedureScope.OPERATION, Recipient.GRD_REF, Channel.EMAIL),
        ("PARTAGE_FIN", ProcedureScope.OPERATION, Recipient.GRD_REF, Channel.EMAIL),
        ("CE_NOTIFICATION", ProcedureScope.COMMUNITY, Recipient.REGULATOR, Channel.ONLINE_FORM),
        ("CE_ACTUALISATION", ProcedureScope.COMMUNITY, Recipient.REGULATOR, Channel.ONLINE_FORM),
        ("CE_RAPPORT_ANNUEL", ProcedureScope.COMMUNITY, Recipient.REGULATOR, Channel.ONLINE_FORM),
    ],
)
def test_who_receives_what_and_how(code, scope, recipient, channel):
    definition = DEFINITIONS[code]
    assert (definition.procedure_scope, definition.recipient_code, definition.channel_code) == (
        scope,
        recipient,
        channel,
    )
    assert definition.family == code  # v1: one family per type


class TestPhaseOneTriggers:
    """Only PARTAGE_MODIF reacts to CRM events in phase 1; the annual report is
    opened by the calendar and PARTAGE_FIN by hand (spec, "Phase 1")."""

    @pytest.mark.parametrize(
        ("kind", "codes"),
        [
            ("meter_added", ["PARTAGE_MODIF"]),
            ("meter_removed", ["PARTAGE_MODIF"]),
            ("key_changed", ["PARTAGE_MODIF"]),
            ("representative_changed", ["PARTAGE_MODIF"]),
            ("installation_added", ["PARTAGE_MODIF"]),
            ("operation_closed", ["PARTAGE_FIN"]),  # no emitter yet
            ("operation_created", []),
            ("community_created", []),
            ("member_joined", []),
            ("member_left", []),
        ],
    )
    def test_which_procedure_a_change_feeds(self, kind, codes):
        handling = CATALOGUE.handling(kind, D(2026, 10, 31))
        assert [e.definition.code for e in handling] == codes


class TestPhaseFourTriggers:
    """From 1 November 2026: PARTAGE_AUTORISATION, CE_NOTIFICATION and
    CE_ACTUALISATION have their triggers (spec, "Phase 4")."""

    DAY = D(2026, 11, 2)

    @pytest.mark.parametrize(
        ("kind", "codes"),
        [
            ("meter_added", ["PARTAGE_MODIF"]),
            ("meter_removed", ["PARTAGE_MODIF"]),
            ("key_changed", ["PARTAGE_MODIF"]),
            # Spec: PARTAGE_MODIF per operation + CE_ACTUALISATION.
            ("representative_changed", ["CE_ACTUALISATION", "PARTAGE_MODIF"]),
            ("statutes_changed", ["CE_ACTUALISATION"]),
            ("operation_created", ["PARTAGE_AUTORISATION"]),
            ("operation_deleted", ["PARTAGE_AUTORISATION"]),  # undoes a draft only
            ("operation_closed", ["PARTAGE_FIN"]),
            ("community_created", ["CE_NOTIFICATION"]),
            # The annual report covers the members (spec: "selon le catalogue").
            ("member_joined", []),
            ("member_left", []),
        ],
    )
    def test_which_procedure_a_change_feeds(self, kind, codes):
        handling = CATALOGUE.handling(kind, self.DAY)
        assert sorted(e.definition.code for e in handling) == codes

    @pytest.mark.parametrize(
        "kind",
        [
            "meter_added",
            "meter_removed",
            "key_changed",
            "representative_changed",
            "installation_added",
            "installation_removed",
        ],
    )
    def test_a_new_operation_s_request_takes_its_first_changes(self, kind):
        assert [e.definition.code for e in CATALOGUE.absorbing(kind, self.DAY)] == [
            "PARTAGE_AUTORISATION"
        ]

    def test_the_baseline_asks_about_the_creation_and_each_authorisation(self):
        asked = CATALOGUE.asked_at_baseline(self.DAY)
        assert sorted(e.definition.code for e in asked) == [
            "CE_NOTIFICATION",
            "PARTAGE_AUTORISATION",
        ]
        assert CATALOGUE.asked_at_baseline(D(2026, 10, 31)) == ()

    def test_the_end_of_a_sharing_ends_its_operation(self):
        assert DEFINITIONS["PARTAGE_FIN"].ends_operation
        assert not CATALOGUE.versions("PARTAGE_FIN")[0].definition.ends_operation


def _facts(code: str, *changes: ChangeFact, **context) -> ProcedureFacts:
    return ProcedureFacts(
        definition=DEFINITIONS[code],
        variant=DEFINITIONS[code].default_variant,
        id_community=1,
        id_sharing_operation=7 if DEFINITIONS[code].scope == "operation" else None,
        changes=changes,
        context=context,
    )


def _community_change(event_type: str, kind: str) -> ChangeFact:
    return ChangeFact(
        kind=kind,
        subject_type=ChangeSubjectType.COMMUNITY,
        subject_ref="1",
        payload={"event_type": event_type},
    )


class TestPiecesFromTheChanges:
    def test_new_statutes_require_the_statutes_and_not_the_internal_rules(self):
        facts = _facts(
            "CE_ACTUALISATION", _community_change("community.statutes_changed", "statutes_changed")
        )
        assert _condition("statutes_changed", facts) is True
        assert _condition("internal_rules_changed", facts) is False

    def test_new_internal_rules_require_them(self):
        facts = _facts(
            "CE_ACTUALISATION", _community_change("community.roi_changed", "statutes_changed")
        )
        assert (
            _condition("statutes_changed", facts),
            _condition("internal_rules_changed", facts),
        ) == (
            False,
            True,
        )

    def test_a_procedure_opened_by_hand_offers_them(self):
        facts = _facts("CE_ACTUALISATION")
        assert _condition("statutes_changed", facts) is None
        assert _condition("internal_rules_changed", facts) is None

    def test_what_the_engine_states_wins(self):
        facts = _facts(
            "CE_ACTUALISATION",
            _community_change("community.statutes_changed", "statutes_changed"),
            statutes_changed=False,
        )
        assert _condition("statutes_changed", facts) is False

    @pytest.mark.parametrize(
        ("operation_type", "community", "building"),
        [(1, False, True), (2, True, False), (3, True, False), (None, None, None)],
    )
    def test_the_operation_s_type_picks_the_grd_agreement(
        self, operation_type, community, building
    ):
        created = ChangeFact(
            kind="operation_created",
            subject_type=ChangeSubjectType.OPERATION,
            subject_ref="7",
            payload={"type": operation_type},
        )
        facts = _facts("PARTAGE_AUTORISATION", created)
        assert _condition("community_operation", facts) is community
        assert _condition("same_building_operation", facts) is building

    def test_the_annual_report_opens_on_1_june_and_is_due_on_1_september(self):
        report = DEFINITIONS["CE_RAPPORT_ANNUEL"]
        assert report.opens_annually is not None
        assert report.opens_annually.in_year(2026) == D(2026, 6, 1)
        (submission,) = report.annual_rules(None)
        assert submission.annual is not None
        assert submission.annual.in_year(2026) == D(2026, 9, 1)


def _july_doc_types() -> set[str]:
    """Every doc_type the July template registry seeds."""
    found: set[str] = set()
    for seed in SEEDS.glob("*.sql"):
        found |= set(re.findall(r"\(NULL, 1, '([a-z0-9_]+)'", seed.read_text(encoding="utf-8")))
    return found


def test_every_july_template_is_a_piece_of_some_procedure():
    """Converted July documents keep their doc_type as requirement_code, so a
    template without a piece would leave them outside the catalogue."""
    july = _july_doc_types()
    assert len(july) >= 11, f"the seed parser found only {sorted(july)}"
    pieces = {d.code for definition in DEFINITIONS.values() for d in definition.documents}
    assert july <= pieces, f"no procedure lists {sorted(july - pieces)}"


def test_every_template_named_is_a_july_template():
    july = _july_doc_types()
    named = {
        d.template
        for definition in DEFINITIONS.values()
        for d in definition.documents
        if d.template
    }
    assert named <= july, f"no template is registered for {sorted(named - july)}"


@pytest.mark.parametrize("code", sorted(DEFINITIONS))
def test_the_plugin_knows_every_condition_and_member_list(code):
    definition = DEFINITIONS[code]
    facts = ProcedureFacts(
        definition=definition,
        variant=definition.default_variant,
        id_community=1,
        id_sharing_operation=None,
    )
    for spec in definition.documents:
        if spec.condition is not None:
            _condition(spec.condition, facts)  # raises on a name it has no rule for
        if spec.per_member:
            _members(spec, facts)
