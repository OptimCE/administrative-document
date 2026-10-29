"""The CWaPE catalogue: the seven procedure types of the spec, as data."""

from __future__ import annotations

import datetime
import re
from pathlib import Path

import pytest

from engine.contracts import ProcedureFacts
from regulators.cwape.plugin import CwapePlugin, _condition, _members
from shared.const import Channel, ProcedureScope, Recipient

D = datetime.date
SEEDS = Path(__file__).resolve().parents[3] / "scripts" / "sql" / "seeds"
PLUGIN = CwapePlugin()
CATALOGUE = PLUGIN.catalogue()
DEFINITIONS = {e.definition.code: e.definition for e in CATALOGUE.entries}


def test_six_codes_one_version_each():
    assert sorted(DEFINITIONS) == [
        "CE_ACTUALISATION",
        "CE_NOTIFICATION",
        "CE_RAPPORT_ANNUEL",
        "PARTAGE_AUTORISATION",
        "PARTAGE_FIN",
        "PARTAGE_MODIF",
    ]
    assert all(e.definition.version == 1 and e.valid_to is None for e in CATALOGUE.entries)


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
            ("operation_created", []),  # PARTAGE_AUTORISATION: phase 4
            ("community_created", []),  # CE_NOTIFICATION: phase 4
            ("member_joined", []),
            ("member_left", []),
        ],
    )
    def test_which_procedure_a_change_feeds(self, kind, codes):
        handling = CATALOGUE.handling(kind, D(2026, 11, 2))
        assert [e.definition.code for e in handling] == codes

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
