"""Copies of the fake regulator's catalogue that a test may edit."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml

from regulators.testing.fake import CATALOGUE_DIR


def fake_document(name: str = "FAKE_CHANGE.v1.yaml") -> dict[str, Any]:
    """A fresh, mutable copy of one of the fake's catalogue files, parsed."""
    document = yaml.safe_load((CATALOGUE_DIR / name).read_text(encoding="utf-8"))
    return copy.deepcopy(document)


def write_document(directory: Path, name: str, document: dict[str, Any]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(yaml.safe_dump(document, allow_unicode=True, sort_keys=False), "utf-8")
    return path


def fake_catalogue_copy(
    directory: Path, replacements: dict[str, dict[str, Any] | None] | None = None
) -> Path:
    """The fake's catalogue, written to ``directory``. ``replacements`` maps a
    file name to the document to write instead (or in addition), or to None to
    leave that file out."""
    replacements = replacements or {}
    names = {path.name for path in CATALOGUE_DIR.glob("*.yaml")} | set(replacements)
    for name in sorted(names):
        document = replacements[name] if name in replacements else fake_document(name)
        if document is not None:
            write_document(directory, name, document)
    return directory
