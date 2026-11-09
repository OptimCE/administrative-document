"""engine.catalogue: the model a plugin's YAML must satisfy, and its loader."""

from __future__ import annotations

import datetime
from collections.abc import Callable
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from engine.catalogue import (
    CatalogueError,
    ProcedureDefinition,
    content_sha256,
    load_catalogue,
    response_event,
    step_event,
)
from regulators.testing.fake import CATALOGUE_DIR
from shared.const import Bucket, NextActionCode, ProcedureScope, ProcedureStatus
from tests.engine.catalogue_files import fake_catalogue_copy, fake_document, write_document

D = datetime.date
Mutation = Callable[[dict[str, Any]], None]


def _rule(document: dict[str, Any], code: str) -> dict[str, Any]:
    return next(rule for rule in document["deadline_rules"] if rule["code"] == code)


class TestTheFakeCatalogue:
    def test_loads_every_file(self):
        catalogue = load_catalogue(CATALOGUE_DIR, regulator="TEST-FAKE")
        assert catalogue.regulator == "TEST-FAKE"
        assert [e.source for e in catalogue.entries] == [
            "FAKE_BASELINE.v1.yaml",
            "FAKE_CHANGE.v1.yaml",
            "FAKE_COMMUNITY.v1.yaml",
            "FAKE_YEARLY.v1.yaml",
        ]

    def test_coded_values(self):
        entry = load_catalogue(CATALOGUE_DIR, regulator="X").get("FAKE_CHANGE", 1)
        assert entry is not None
        definition = entry.definition
        assert definition.procedure_scope == ProcedureScope.OPERATION
        assert definition.step("in_force").common_status == ProcedureStatus.IN_FORCE
        assert definition.step("sent").bucket == Bucket.WAITING
        assert definition.step("ready").next_action.code == NextActionCode.SEND


class TestLookups:
    catalogue = load_catalogue(CATALOGUE_DIR, regulator="TEST-FAKE")
    change = next(e.definition for e in catalogue.entries if e.definition.code == "FAKE_CHANGE")

    def test_a_version_by_number(self):
        assert self.catalogue.get("FAKE_CHANGE", 1) is not None
        assert self.catalogue.get("FAKE_CHANGE", 2) is None

    def test_nothing_is_in_force_before_its_valid_from(self):
        assert self.catalogue.in_force("FAKE_CHANGE", D(2025, 12, 31)) is None
        assert self.catalogue.in_force("FAKE_CHANGE", D(2026, 1, 1)) is not None

    @pytest.mark.parametrize(
        ("kind", "codes"),
        [
            ("meter_added", ["FAKE_CHANGE"]),
            ("representative_changed", ["FAKE_CHANGE", "FAKE_COMMUNITY"]),
            ("member_joined", []),
        ],
    )
    def test_which_procedures_a_change_feeds(self, kind, codes):
        found = self.catalogue.handling(kind, D(2026, 6, 1))
        assert [e.definition.code for e in found] == codes

    def test_edges_and_steps_follow_the_variant(self):
        definition = self.change
        assert definition.allows("sent", "accepted", "light")
        assert not definition.allows("sent", "accepted", "full")
        assert "reviewed" not in [s.code for s in definition.steps_for("light")]
        assert "reviewed" in [s.code for s in definition.steps_for("full")]

    def test_rules_started_and_closed_by_an_event(self):
        definition = self.change
        assert [r.code for r in definition.rules_started_by(step_event("sent"), "light")] == [
            "grd_answer"
        ]
        # The decision clock exists on the full route only.
        assert definition.rules_started_by(response_event("review_ack"), "light") == ()
        assert [
            r.code for r in definition.rules_started_by(response_event("review_ack"), "full")
        ] == ["decision"]
        assert [r.code for r in definition.rules_closed_by(response_event("approval"), "full")] == [
            "grd_answer",
            "decision",
        ]

    def test_pieces_follow_the_variant(self):
        definition = self.change
        assert "member_declaration" not in [d.code for d in definition.documents_for("light")]
        assert "member_declaration" in [d.code for d in definition.documents_for("full")]


def _set(path: str, value: Any) -> Mutation:
    def mutate(document: dict[str, Any]) -> None:
        *parents, last = path.split(".")
        target: Any = document
        for key in parents:
            target = target[int(key)] if key.isdigit() else target[key]
        target[int(last) if last.isdigit() else last] = value

    return mutate


def _drop_offset(document: dict[str, Any]) -> None:
    del _rule(document, "grd_answer")["offset"]


def _duplicate_first_step(document: dict[str, Any]) -> None:
    document["steps"].append(dict(document["steps"][0]))


def _strand_lapsed(document: dict[str, Any]) -> None:
    document["transitions"] = [t for t in document["transitions"] if t["to"] != "lapsed"]


def _add_edge_to_nowhere(document: dict[str, Any]) -> None:
    document["transitions"].append({"from": "sent", "to": "limbo"})


class TestADefinitionMustBeCoherent:
    @pytest.mark.parametrize(
        ("mutation", "message"),
        [
            (_duplicate_first_step, "step draft is declared twice"),
            (_set("initial_step", "ready"), "initial step ready must have status draft"),
            (_set("initial_step", "nowhere"), "initial step nowhere is not a step"),
            (_add_edge_to_nowhere, "transition sent -> limbo: no step limbo"),
            (_set("response_types.0.to_step", "limbo"), "leads to unknown step limbo"),
            (_set("deadline_rules.0.anchor", "response:telegram"), "unknown response:telegram"),
            (_set("deadline_rules.0.anchor", "sent"), "String should match pattern"),
            (_drop_offset, "a relative rule has an anchor and an offset only"),
            (_set("default_variant", "medium"), "default variant medium is not a variant"),
            (_set("steps.4.variants", ["huge"]), "step reviewed names unknown variant huge"),
            (_strand_lapsed, "no transition reaches lapsed"),
            (_set("labels", {"fr": "a", "en": "b", "nl": "c"}), "missing de"),
            (_set("labels", {"fr": "a", "en": "b", "nl": "c", "de": "d", "it": "e"}), "unknown it"),
            (_set("labels.de", " "), "missing de"),
            (_set("owner", "me"), "Extra inputs are not permitted"),
            (_set("code", "fake_change"), "String should match pattern"),
            (_set("steps.0.next_action", {"code": "send"}), "next action send needs labels"),
            (_set("steps.0.bucket", "later"), "Input should be"),
            (_set("steps.1.status", "in_review"), "Input should be"),
            (_set("channel", "pigeon"), "Input should be"),
        ],
    )
    def test_an_incoherent_change_procedure_is_refused(self, mutation, message):
        document = fake_document()
        mutation(document)
        with pytest.raises(ValidationError, match=message):
            ProcedureDefinition.model_validate(document)

    @pytest.mark.parametrize(
        ("mutation", "message"),
        [
            (
                _set("deadline_rules.0.anchor", "step:filed"),
                "an annual rule has an annual date only",
            ),
            (_set("deadline_rules.0.annual", {"month": 2, "day": 29}), "29/02 is not a day"),
            (_set("deadline_rules.0.annual", {"month": 4, "day": 31}), "31/04 is not a day"),
            (_set("opens_annually", {"month": 13, "day": 1}), "less than or equal to 12"),
        ],
    )
    def test_an_incoherent_yearly_procedure_is_refused(self, mutation, message):
        document = fake_document("FAKE_YEARLY.v1.yaml")
        mutation(document)
        with pytest.raises(ValidationError, match=message):
            ProcedureDefinition.model_validate(document)

    def test_a_default_variant_needs_variants(self):
        document = fake_document("FAKE_COMMUNITY.v1.yaml")
        document["default_variant"] = "light"
        with pytest.raises(ValidationError, match="a default variant needs a list of variants"):
            ProcedureDefinition.model_validate(document)


class TestContentHash:
    def test_formatting_order_and_comments_do_not_count(self, tmp_path):
        document = fake_document()
        reordered = dict(reversed(list(document.items())))
        path = tmp_path / "x.yaml"
        path.write_text(
            "# a comment\n" + yaml.safe_dump(reordered, default_flow_style=True, width=60),
            encoding="utf-8",
        )
        reread = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert content_sha256(reread) == content_sha256(document)

    @pytest.mark.parametrize(
        "mutation",
        [
            _set("labels.fr", "Autre libellé"),
            _set("steps.0.labels.en", "Rough draft"),
            _set("steps.0.next_action.labels.nl", "Iets anders"),
            _set("deadline_rules.0.labels.de", "Andere Frist"),
        ],
    )
    def test_wording_does_not_count(self, mutation):
        document = fake_document()
        before = content_sha256(document)
        mutation(document)
        assert content_sha256(document) == before

    @pytest.mark.parametrize(
        "mutation",
        [
            _set("deadline_rules.0.offset.value", 11),
            _set("escalation.when_any", ["meter_added", "key_changed"]),
            _set("steps.2.bucket", "todo"),
            _set("valid_from", D(2026, 2, 1)),
        ],
    )
    def test_regulatory_content_does(self, mutation):
        document = fake_document()
        before = content_sha256(document)
        mutation(document)
        assert content_sha256(document) != before


def _v2(**changes: Any) -> dict[str, Any]:
    document = fake_document()
    document.update({"version": 2, "valid_from": D(2026, 7, 1), **changes})
    return document


class TestTheLoader:
    def test_a_later_version_closes_the_previous_one_the_day_before(self, tmp_path):
        directory = fake_catalogue_copy(tmp_path, {"FAKE_CHANGE.v2.yaml": _v2()})
        catalogue = load_catalogue(directory, regulator="TEST-FAKE")
        v1, v2 = catalogue.versions("FAKE_CHANGE")
        assert v1.valid_to == D(2026, 6, 30)
        assert v2.valid_to is None
        assert catalogue.in_force("FAKE_CHANGE", D(2026, 6, 30)) == v1
        assert catalogue.in_force("FAKE_CHANGE", D(2026, 7, 1)) == v2

    def test_versions_run_without_a_gap(self, tmp_path):
        directory = fake_catalogue_copy(tmp_path, {"FAKE_CHANGE.v3.yaml": _v2(version=3)})
        with pytest.raises(CatalogueError, match=r"found \[1, 3\]"):
            load_catalogue(directory, regulator="TEST-FAKE")

    def test_a_later_version_starts_later(self, tmp_path):
        directory = fake_catalogue_copy(
            tmp_path, {"FAKE_CHANGE.v2.yaml": _v2(valid_from=D(2026, 1, 1))}
        )
        with pytest.raises(CatalogueError, match="valid_from must come after v1's"):
            load_catalogue(directory, regulator="TEST-FAKE")

    def test_the_file_name_and_content_agree(self, tmp_path):
        write_document(tmp_path, "FAKE_OTHER.v1.yaml", fake_document())
        with pytest.raises(CatalogueError, match="file name and its content must agree"):
            load_catalogue(tmp_path, regulator="TEST-FAKE")

    @pytest.mark.parametrize("name", ["fake.yaml", "FAKE_CHANGE.yaml", "FAKE_CHANGE.v0.yaml"])
    def test_a_file_follows_the_naming_scheme(self, tmp_path, name):
        write_document(tmp_path, name, fake_document())
        with pytest.raises(CatalogueError, match=r"is named <CODE>\.v<N>\.yaml"):
            load_catalogue(tmp_path, regulator="TEST-FAKE")

    def test_a_yml_file_is_not_silently_skipped(self, tmp_path):
        fake_catalogue_copy(tmp_path)
        write_document(tmp_path, "FAKE_EXTRA.v1.yml", fake_document())
        with pytest.raises(CatalogueError, match="use .yaml, not .yml"):
            load_catalogue(tmp_path, regulator="TEST-FAKE")

    def test_an_empty_catalogue_is_refused(self, tmp_path):
        with pytest.raises(CatalogueError, match="no catalogue file"):
            load_catalogue(tmp_path, regulator="TEST-FAKE")

    @pytest.mark.parametrize(
        ("text", "message"), [("- a\n- b\n", "one mapping"), ("code: [", "FAKE_CHANGE.v1.yaml")]
    )
    def test_an_unreadable_file_names_itself(self, tmp_path, text, message):
        (tmp_path / "FAKE_CHANGE.v1.yaml").write_text(text, encoding="utf-8")
        with pytest.raises(CatalogueError, match=message):
            load_catalogue(tmp_path, regulator="TEST-FAKE")

    def test_a_validation_error_names_the_file(self, tmp_path):
        document = fake_document()
        document["initial_step"] = "nowhere"
        write_document(tmp_path, "FAKE_CHANGE.v1.yaml", document)
        with pytest.raises(
            CatalogueError, match=r"(?s)FAKE_CHANGE\.v1\.yaml.*initial step nowhere"
        ):
            load_catalogue(tmp_path, regulator="TEST-FAKE")

    def test_yaml_tags_cannot_build_objects(self, tmp_path):
        (tmp_path / "FAKE_CHANGE.v1.yaml").write_text(
            "code: !!python/object/apply:os.system ['echo pwned']\n", encoding="utf-8"
        )
        with pytest.raises(CatalogueError, match="FAKE_CHANGE.v1.yaml"):
            load_catalogue(tmp_path, regulator="TEST-FAKE")
