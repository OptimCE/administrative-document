"""Entry sheets load from a plugin's data, or refuse to, naming the file."""

from __future__ import annotations

from pathlib import Path

import pytest

from engine.catalogue import CatalogueError
from engine.entry_sheets import load_entry_sheets

_LABELS = "{fr: Nom, en: Name, nl: Naam, de: Name}"


def _sheet(directory: Path, name: str, body: str) -> None:
    (directory / f"{name}.yaml").write_text(body, encoding="utf-8")


def _body(piece: str, keys: list[str], labels: str = _LABELS) -> str:
    fields = "\n".join(f"      - key: {key}\n        labels: {labels}" for key in keys)
    return (
        f"piece: {piece}\nlabels: {_LABELS}\nsections:\n  - labels: {_LABELS}\n"
        f"    fields:\n{fields}\n"
    )


def test_a_sheet_fills_its_fields_in_order_from_a_snapshot(tmp_path):
    _sheet(tmp_path, "form", _body("form", ["b", "a"]))

    sheet = load_entry_sheets(tmp_path)["form"]

    (section,) = sheet.filled({"a": "A", "b": None, "other": 1})
    assert [(f["key"], f["value"]) for f in section["fields"]] == [("b", None), ("a", "A")]
    assert sheet.filled(None)[0]["fields"][0]["value"] is None


def test_no_directory_is_no_sheet(tmp_path):
    assert load_entry_sheets(tmp_path / "absent") == {}


@pytest.mark.parametrize(
    ("name", "body", "message"),
    [
        ("other", _body("form", ["a"]), "name the file after it"),
        ("form", _body("form", ["a", "a"]), "a key appears twice"),
        ("form", _body("form", ["a"], labels="{fr: Nom}"), "labels need exactly"),
        ("form", "piece: form\nlabels: " + _LABELS + "\nsections: []\n", "sections"),
    ],
)
def test_a_bad_sheet_is_refused_by_name(tmp_path, name, body, message):
    _sheet(tmp_path, name, body)

    with pytest.raises(CatalogueError) as exc:
        load_entry_sheets(tmp_path)

    assert f"{name}.yaml" in str(exc.value)
    assert message in str(exc.value)
