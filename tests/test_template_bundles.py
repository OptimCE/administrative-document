"""Every shipped bundle must agree with the official file it wraps.

These are the only tests that open the CWaPE documents themselves. They catch the
two failure modes that would otherwise surface as a mid-render error in
production, long after the bundle was published:

* a manifest naming an AcroForm field or a worksheet anchor the document does not
  have (a typo, or a regulator revision that moved things);
* a label in ``domain.cwape_labels`` that is no longer in the workbook's
  data-validation list, which shows up as a validation error the moment the
  regulator opens the file.

They are skipped, not failed, when the render libraries are absent — this service
does not render anything itself.
"""

from __future__ import annotations

import json
import pathlib
import re

import pytest

from domain import cwape_labels as labels

openpyxl = pytest.importorskip("openpyxl", reason="workbook inspection needs openpyxl")
pypdf = pytest.importorskip("pypdf", reason="AcroForm inspection needs pypdf")

BUNDLE_ROOT = (
    pathlib.Path(__file__).resolve().parents[1] / "document-templates" / "administrative-document"
)

BUNDLES = sorted(p for p in BUNDLE_ROOT.glob("*/v1") if (p / "manifest.json").is_file())
BUNDLE_IDS = [p.parent.name for p in BUNDLES]


def _manifest(bundle: pathlib.Path) -> dict:
    manifest: dict = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    return manifest


@pytest.fixture(params=BUNDLES, ids=BUNDLE_IDS)
def bundle(request) -> pathlib.Path:
    path: pathlib.Path = request.param
    return path


def test_there_is_a_bundle_for_every_registered_form():
    """A tripwire: silently losing a bundle would silently lose a doc_type."""
    assert len(BUNDLES) == 11, [p.parent.name for p in BUNDLES]


class TestDocxTemplatesAreTheOfficialConvention:
    """The DSO conventions are contracts: only the fill-in markers may differ."""

    def test_every_marker_became_an_expression(self, bundle):
        manifest = _manifest(bundle)
        if manifest["engine"] != "docx":
            pytest.skip("not a docx bundle")
        docx = pytest.importorskip("docx", reason="needs python-docx")

        body = "\n".join(
            p.text for p in docx.Document(str(bundle / manifest["entrypoint"])).paragraphs
        )
        # "[Annexe 1 : ...]" is the convention's own text, not a fill-in.
        leftover = [m for m in re.findall(r"\[[^\]]*\]", body) if "Annexe" not in m]
        assert not leftover, f"unreplaced fill-in markers: {leftover}"

    def test_every_expression_is_a_required_field(self, bundle):
        """docxtpl runs with StrictUndefined, so an expression the schema does not
        require is a render that fails the first time someone uses it."""
        manifest = _manifest(bundle)
        if manifest["engine"] != "docx":
            pytest.skip("not a docx bundle")
        docx = pytest.importorskip("docx", reason="needs python-docx")

        body = "\n".join(
            p.text for p in docx.Document(str(bundle / manifest["entrypoint"])).paragraphs
        )
        used = set(re.findall(r"\{\{\s*data\.(\w+)\s*\}\}", body))
        required = set(manifest["required_fields"]["required"])
        assert used, "a docx template with no expressions fills nothing in"
        assert used <= required, f"expressions missing from required_fields: {used - required}"


class TestManifestMatchesItsDocument:
    def test_the_entrypoint_exists(self, bundle):
        manifest = _manifest(bundle)
        assert (bundle / manifest["entrypoint"]).is_file()

    def test_declared_engine_matches_the_entrypoint_kind(self, bundle):
        manifest = _manifest(bundle)
        suffix = pathlib.Path(manifest["entrypoint"]).suffix
        expected = {"xlsx": ".xlsx", "pdf-form": ".pdf", "docx": ".docx"}[manifest["engine"]]
        assert suffix == expected

    def test_every_mapped_acroform_field_exists(self, bundle):
        """The exact check the renderer makes — run here so a bad map fails in CI
        rather than as a permanent TEMPLATE_NOT_FOUND on a user's first render."""
        manifest = _manifest(bundle)
        if manifest["engine"] != "pdf-form":
            pytest.skip("not a pdf-form bundle")

        declared = pypdf.PdfReader(str(bundle / manifest["entrypoint"])).get_fields() or {}
        assert declared, "a pdf-form template must have AcroForm fields"

        missing = []
        for key, target in manifest["fields"].items():
            names = [target] if isinstance(target, str) else target
            missing += [(key, name) for name in names if name not in declared]
        assert not missing, f"manifest maps fields this PDF does not declare: {missing}"

    def test_every_block_anchor_resolves(self, bundle):
        manifest = _manifest(bundle)
        if manifest["engine"] != "xlsx":
            pytest.skip("not an xlsx bundle")

        workbook = openpyxl.load_workbook(bundle / manifest["entrypoint"])
        for block in manifest["blocks"]:
            sheet_name, separator, _coordinate = block["anchor"].rpartition("!")
            assert separator, f"anchor {block['anchor']!r} must name its sheet"
            assert (
                sheet_name in workbook.sheetnames
            ), f"anchor sheet {sheet_name!r} not in {workbook.sheetnames}"

    def test_block_capacity_matches_the_worksheet(self, bundle):
        """maxItems is what refuses an over-long filing, so it must be the real
        number of rows the form reserves — not an optimistic guess."""
        manifest = _manifest(bundle)
        if manifest["engine"] != "xlsx":
            pytest.skip("not an xlsx bundle")

        workbook = openpyxl.load_workbook(bundle / manifest["entrypoint"])
        properties = manifest["required_fields"]["properties"]
        for block in manifest["blocks"]:
            sheet_name, _sep, coordinate = block["anchor"].rpartition("!")
            sheet = workbook[sheet_name]
            first_row = openpyxl.utils.coordinate_to_tuple(coordinate)[0]
            declared = properties[block["source"]]["maxItems"]
            available = sheet.max_row - first_row + 1
            assert declared <= available, (
                f"{block['source']}: manifest allows {declared} rows but "
                f"{sheet_name!r} only provisions {available}"
            )


class TestLabelsStillMatchTheDropdowns:
    """``domain.cwape_labels`` is a contract with the form, not a display choice."""

    def _list_validations(self, bundle: pathlib.Path) -> list[str]:
        manifest = _manifest(bundle)
        workbook = openpyxl.load_workbook(bundle / manifest["entrypoint"])
        allowed: list[str] = []
        for sheet in workbook.worksheets:
            for validation in sheet.data_validations.dataValidation:
                formula = (validation.formula1 or "").strip()
                if not formula.startswith('"'):
                    continue  # a named range, resolved separately below
                allowed += [entry.strip() for entry in formula.strip('"').split(",")]
        # Named ranges used as list sources (the notification workbook's
        # `Statut`, `Catégories`, …) resolve to cell values.
        for _name, defined in workbook.defined_names.items():
            for sheet_name, coordinate in defined.destinations:
                cells = workbook[sheet_name][coordinate.replace("$", "")]
                rows = cells if isinstance(cells, tuple) else ((cells,),)
                for row in rows:
                    cells_in_row = row if isinstance(row, tuple) else (row,)
                    allowed += [
                        str(cell.value).strip()
                        for cell in cells_in_row
                        if isinstance(cell.value, str)
                    ]
        return allowed

    def test_grid_operators_are_all_offered(self):
        bundle = BUNDLE_ROOT / "annex6_sharing_form" / "v1"
        allowed = self._list_validations(bundle)
        assert set(labels.GRD_LABELS) <= set(allowed)

    def test_production_chains_are_all_offered(self):
        bundle = BUNDLE_ROOT / "annex6_sharing_form" / "v1"
        allowed = self._list_validations(bundle)
        assert set(labels.PRODUCTION_CHAIN_LABELS.values()) <= set(allowed)

    def test_producer_statuses_are_all_offered(self):
        bundle = BUNDLE_ROOT / "annex6_sharing_form" / "v1"
        allowed = self._list_validations(bundle)
        assert set(labels.PRODUCER_STATUS_LABELS.values()) <= set(allowed)

    def test_community_installation_statuses_are_all_offered(self):
        """The notification form uses a different vocabulary from the sharing one."""
        bundle = BUNDLE_ROOT / "annex6_notification" / "v1"
        allowed = self._list_validations(bundle)
        assert set(labels.COMMUNITY_INSTALLATION_STATUS_LABELS.values()) <= set(allowed)

    def test_member_categories_are_all_offered(self):
        bundle = BUNDLE_ROOT / "annex6_notification" / "v1"
        allowed = self._list_validations(bundle)
        assert set(labels.MEMBER_CATEGORY_CHOICES) <= set(allowed)

    def test_residential_answers_are_offered(self):
        bundle = BUNDLE_ROOT / "annex6_sharing_form" / "v1"
        allowed = self._list_validations(bundle)
        assert {labels.YES, labels.NO} <= set(allowed)

    def test_installation_states_are_offered(self):
        bundle = BUNDLE_ROOT / "annex6_sharing_form" / "v1"
        allowed = self._list_validations(bundle)
        assert {labels.INSTALLATION_IN_SERVICE, labels.INSTALLATION_TO_INSTALL} <= set(allowed)
