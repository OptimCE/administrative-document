"""A regulator nobody has, for the engine's own tests.

The engine's suite runs against this plugin, never a real one, so that what it
proves is the engine's behaviour and not a regulator's. Its catalogue
(``catalogue/``) uses every feature the engine reads: two variants with an
escalation, per-variant steps and edges, informative and transition-bearing
responses, relative rules in working days, calendar days and months, an annual
procedure, conditional and per-member pieces. Its codes look like no real
regulator's, so a test cannot pass by leaning on one.

The methods are deliberately plain: each does the obvious thing with the data,
which is what a test needs to predict the result.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from engine.calendar import CalendarData, load_calendar
from engine.catalogue import Catalogue, ProcedureDefinition, load_catalogue
from engine.contracts import ChangeFact, DocumentRequirement, Effect, ProcedureFacts
from shared.const import ChangeSubjectType, RegulatoryStatus

HERE = Path(__file__).resolve().parent
CATALOGUE_DIR = HERE / "catalogue"
CALENDAR_FILE = HERE / "calendar" / "TST.yaml"


class FakeRegulator:
    """``RegulatorPlugin`` over the files next to this module, or over copies a
    test edited (``catalogue_dir``, ``calendar_file``)."""

    def __init__(
        self,
        *,
        code: str = "TEST-FAKE",
        catalogue_dir: Path = CATALOGUE_DIR,
        calendar_file: Path = CALENDAR_FILE,
    ) -> None:
        self.code = code
        self._catalogue = load_catalogue(catalogue_dir, regulator=code)
        self._calendar = load_calendar(calendar_file)
        self.region = self._calendar.region

    def catalogue(self) -> Catalogue:
        return self._catalogue

    def calendar(self) -> CalendarData:
        return self._calendar

    def variant(self, definition: ProcedureDefinition, changes: Sequence[ChangeFact]) -> str | None:
        """``escalation: {escalate_to: <variant>, when_any: [<change kind>…]}``."""
        escalate_to = definition.escalation.get("escalate_to")
        when_any = set(definition.escalation.get("when_any", ()))
        if escalate_to and any(change.kind in when_any for change in changes):
            return str(escalate_to)
        return definition.default_variant

    def required_documents(self, procedure: ProcedureFacts) -> list[DocumentRequirement]:
        """A conditional piece is required when its condition is listed in
        ``context["conditions"]``; a per-member piece repeats for each id in
        ``context["members"]``."""
        conditions = set(procedure.context.get("conditions", ()))
        members = list(procedure.context.get("members", ()))
        requirements: list[DocumentRequirement] = []
        for spec in procedure.definition.documents_for(procedure.variant):
            required = spec.required and (spec.condition is None or spec.condition in conditions)
            condition = {"condition": spec.condition} if spec.condition else None
            for id_member in members if spec.per_member else [None]:
                requirements.append(
                    DocumentRequirement(
                        code=spec.code,
                        kind=spec.document_kind,
                        required=required,
                        id_member=id_member,
                        condition=condition,
                    )
                )
        return requirements

    def render_context(self, requirement_code: str, snapshot: Mapping[str, Any]) -> dict[str, Any]:
        return {"requirement": requirement_code, **snapshot}

    def effects(self, procedure: ProcedureFacts) -> list[Effect]:
        """An added meter becomes active and a removed one ended, on the
        effective date; a changed key becomes active."""
        if procedure.id_sharing_operation is None:
            return []
        on = procedure.effective_date
        statuses = {
            "meter_added": (ChangeSubjectType.METER, RegulatoryStatus.ACTIVE),
            "meter_removed": (ChangeSubjectType.METER, RegulatoryStatus.ENDED),
            "key_changed": (ChangeSubjectType.KEY, RegulatoryStatus.ACTIVE),
        }
        effects: list[Effect] = []
        for change in procedure.changes:
            if change.kind not in statuses:
                continue
            subject_type, status = statuses[change.kind]
            ended = status == RegulatoryStatus.ENDED
            effects.append(
                Effect(
                    subject_type=subject_type,
                    subject_ref=change.subject_ref,
                    id_sharing_operation=procedure.id_sharing_operation,
                    regulatory_status=status,
                    valid_from=None if ended else on,
                    valid_to=on if ended else None,
                )
            )
        return effects
