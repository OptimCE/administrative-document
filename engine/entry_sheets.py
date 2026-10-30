"""Entry sheets ("fiches de saisie"): a form piece's values, in the order of the
regulator's form, for a person to type or paste into it.

Where the regulator takes a form online, or as a file a person fills in, what
OptimCE hands over is not a document but the values to enter, field after
field, each one copyable. A sheet is a plugin's data (one YAML per form piece,
``entry_sheets/<piece code>.yaml``): which keys of the piece's snapshot, under
which labels, grouped as the form groups them. The values come from the piece's
current version, so the sheet is exactly what was reviewed (and, once sent,
what was filed); nothing here re-reads the CRM.

Generic: no regulator is named, and a plugin without sheets has none.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from engine.catalogue import CatalogueError, Labels, LowerCode


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EntryField(_Model):
    key: str = Field(min_length=1, max_length=64)  # a key of the piece's snapshot
    labels: Labels


class EntrySection(_Model):
    labels: Labels
    fields: tuple[EntryField, ...] = Field(min_length=1)


class EntrySheet(_Model):
    piece: LowerCode  # the catalogue piece code the sheet is for
    labels: Labels
    sections: tuple[EntrySection, ...] = Field(min_length=1)

    def field_keys(self) -> list[str]:
        return [field.key for section in self.sections for field in section.fields]

    def filled(self, snapshot: Mapping[str, Any] | None) -> list[dict[str, Any]]:
        """The sections with each field's value out of ``snapshot`` (None when
        the piece has no version yet, or the key was left out)."""
        values = snapshot or {}
        return [
            {
                "labels": section.labels,
                "fields": [
                    {"key": field.key, "labels": field.labels, "value": values.get(field.key)}
                    for field in section.fields
                ],
            }
            for section in self.sections
        ]


def load_entry_sheets(directory: Path) -> dict[str, EntrySheet]:
    """Every ``*.yaml`` in ``directory``, by piece code. A missing directory is
    no sheet at all."""
    if not directory.is_dir():
        return {}
    sheets: dict[str, EntrySheet] = {}
    for path in sorted(directory.glob("*.yaml")):
        try:
            sheet = EntrySheet.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        except (ValidationError, yaml.YAMLError) as exc:
            raise CatalogueError(f"{path.name}: {exc}") from exc
        if path.stem != sheet.piece:
            raise CatalogueError(
                f"{path.name}: names piece {sheet.piece!r}; name the file after it"
            )
        keys = sheet.field_keys()
        duplicates = sorted({k for k in keys if keys.count(k) > 1})
        if duplicates:
            raise CatalogueError(f"{path.name}: a key appears twice: {', '.join(duplicates)}")
        sheets[sheet.piece] = sheet
    return sheets
