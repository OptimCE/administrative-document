"""The CWaPE (Wallonia) plugin.

Data first: the seven procedure types are ``catalogue/*.yaml`` (six codes,
``PARTAGE_MODIF`` carrying its two routes) and the holidays
``calendar/WAL.yaml``. This module holds only what that data cannot say:

- ``variant``: which route a sharing modification takes, from its changes;
- ``required_documents``: which pieces a procedure needs, from its changes and
  the facts the engine read for it;
- ``render_context`` and ``effects``: phases 2 and 3 (skeletons for now).

The facts it reads in ``ProcedureFacts.context`` (an absent key is unknown,
never false):

- ``key_is_standard``: whether the operation's key is on the CWaPE's list of
  standard keys; False makes ``key_description`` required.
- ``new_installation``: overrides what the changes say (a creation has none).
- ``community_info_changed``: the community's own data changed, so its update
  form travels with the sharing request ("actualisation jointe").
- ``has_delegate``, ``statutes_changed``, ``internal_rules_changed``: CE pieces.
- ``operation_kind``: ``"community"`` or ``"same_building"``, which GRD
  agreement applies.
- ``members``: the participants who sign a sworn declaration; by default the
  members of the meters the procedure adds.
- ``legal_entity_members``: the members that are companies or associations
  (annex 7).

A conditional piece is required when its condition holds, offered as optional
when it is unknown, and left out when it is known not to hold.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from engine.calendar import CalendarData, load_calendar
from engine.catalogue import Catalogue, DocumentSpec, ProcedureDefinition, load_catalogue
from engine.contracts import ChangeFact, DocumentRequirement, Effect, ProcedureFacts
from engine.entry_sheets import EntrySheet, load_entry_sheets

CODE = "BE-WAL-CWAPE"
HERE = Path(__file__).resolve().parent

# The piece each per-member list feeds, and where the list comes from.
_MEMBER_LISTS = {
    "annex8_sworn_declaration": "members",
    "annex7_legal_entity": "legal_entity_members",
}


def _is_producer(change: ChangeFact) -> bool:
    """A meter with an injection status carries an installation (the CRM has
    no separate installation: spec, "Modèle de données")."""
    return change.payload.get("injection_status") is not None


def _known(context: Mapping[str, Any], key: str) -> bool | None:
    value = context.get(key)
    return value if isinstance(value, bool) else None


def _condition(name: str, procedure: ProcedureFacts) -> bool | None:
    """Whether a piece's condition holds: True, False, or None for unknown."""
    context = procedure.context
    kinds = {change.kind for change in procedure.changes}
    known_changes = bool(procedure.changes)
    operation_kind = context.get("operation_kind")

    if name == "non_standard_key":
        standard = _known(context, "key_is_standard")
        return None if standard is None else not standard
    if name == "new_installation":
        stated = _known(context, "new_installation")
        if stated is not None:
            return stated
        if not known_changes:
            return None
        return "installation_added" in kinds or any(
            c.kind == "meter_added" and _is_producer(c) for c in procedure.changes
        )
    if name == "representative_changed":
        return "representative_changed" in kinds if known_changes else None
    if name == "community_update_attached":
        return _known(context, "community_info_changed")
    if name in ("has_delegate", "statutes_changed", "internal_rules_changed"):
        return _known(context, name)
    if name == "community_operation":
        return None if operation_kind is None else operation_kind == "community"
    if name == "same_building_operation":
        return None if operation_kind is None else operation_kind == "same_building"
    raise ValueError(f"CWaPE piece condition {name!r} has no rule in the plugin")


def _members(spec: DocumentSpec, procedure: ProcedureFacts) -> list[int] | None:
    """The members a per-member piece repeats for, or None when unknown."""
    key = _MEMBER_LISTS.get(spec.code)
    if key is None:
        raise ValueError(f"CWaPE per-member piece {spec.code!r} has no member list")
    listed = procedure.context.get(key)
    if listed is not None:
        return [int(member) for member in listed]
    if key == "members" and procedure.changes:
        added = [c.payload.get("id_member") for c in procedure.changes if c.kind == "meter_added"]
        return list(dict.fromkeys(int(m) for m in added if m is not None))
    return None


class CwapePlugin:
    code = CODE
    region = "WAL"
    # The CWaPE files its forms in French.
    draft_watermark = "BROUILLON"

    def __init__(self) -> None:
        self._catalogue = load_catalogue(HERE / "catalogue", regulator=CODE)
        self._calendar = load_calendar(HERE / "calendar" / "WAL.yaml")
        self._sheets = load_entry_sheets(HERE / "entry_sheets")

    def catalogue(self) -> Catalogue:
        return self._catalogue

    def calendar(self) -> CalendarData:
        return self._calendar

    def variant(self, definition: ProcedureDefinition, changes: Sequence[ChangeFact]) -> str | None:
        """Mixed procedure rule: one change that needs the CWaPE's authorization
        puts the whole draft on that route (``escalation`` in the YAML)."""
        if not definition.variants:
            return None
        escalation = definition.escalation
        when_any = set(escalation.get("when_any", ()))
        when_producer = set(escalation.get("when_producer", ()))
        for change in changes:
            if change.kind in when_any or (change.kind in when_producer and _is_producer(change)):
                return str(escalation["escalate_to"])
        return definition.default_variant

    def required_documents(self, procedure: ProcedureFacts) -> list[DocumentRequirement]:
        requirements: list[DocumentRequirement] = []
        for spec in procedure.definition.documents_for(procedure.variant):
            holds: bool | None = True
            if spec.condition is not None:
                holds = _condition(spec.condition, procedure)
                if holds is False:
                    continue
            condition = (
                {"condition": spec.condition, "known": holds is not None}
                if spec.condition
                else None
            )
            required = spec.required and holds is True
            members: list[int | None] = [None]
            if spec.per_member:
                listed = _members(spec, procedure)
                if listed is None:
                    required = False  # nobody to name yet: offered, not required
                elif not listed:
                    continue  # known: nobody signs one
                else:
                    members = list(listed)
            requirements.extend(
                DocumentRequirement(
                    code=spec.code,
                    kind=spec.document_kind,
                    required=required,
                    id_member=member,
                    condition=condition,
                )
                for member in members
            )
        return requirements

    def render_context(self, requirement_code: str, snapshot: Mapping[str, Any]) -> dict[str, Any]:
        """Phase 2. The July prefill (``domain/prefill.py``) already builds the
        payload each CWaPE bundle binds, so the snapshot passes through."""
        return dict(snapshot)

    def effects(self, procedure: ProcedureFacts) -> list[Effect]:
        """Phase 3: what a procedure in force writes to the core. Nothing yet;
        in phase 1 only the baseline confirmation writes effects."""
        return []

    def entry_sheet(self, code: str) -> EntrySheet | None:
        """The CWaPE takes its community forms as files a representative fills in
        (or online): their fiche de saisie, in the forms' own order."""
        return self._sheets.get(code)
