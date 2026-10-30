"""The CWaPE's fiches de saisie: one per community form, each key one its
bundle binds (a key the form does not have would always show blank)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from regulators.cwape.plugin import CwapePlugin

_BUNDLES = Path(__file__).resolve().parents[3] / "document-templates" / "administrative-document"
PLUGIN = CwapePlugin()


@pytest.mark.parametrize("code", ["community_notification", "community_modification"])
def test_each_community_form_has_a_sheet_of_keys_its_bundle_binds(code):
    sheet = PLUGIN.entry_sheet(code)
    assert sheet is not None
    manifest = json.loads((_BUNDLES / code / "v1" / "manifest.json").read_text(encoding="utf-8"))

    assert set(sheet.field_keys()) <= set(manifest["fields"])


def test_a_piece_without_a_form_has_no_sheet():
    assert PLUGIN.entry_sheet("annex6_sharing_form") is None


def test_every_sheet_names_a_piece_of_the_catalogue():
    pieces = {
        document.code
        for entry in PLUGIN.catalogue().entries
        for document in entry.definition.documents
    }
    for code in ("community_notification", "community_modification"):
        assert code in pieces
