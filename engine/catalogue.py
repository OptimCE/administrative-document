"""A regulator's catalogue of procedures: the model, and its YAML loader.

A plugin ships one file per procedure VERSION, ``<CODE>.v<N>.yaml``. A
procedure keeps the version it was opened under, so a released file is never
edited: a change of behaviour is a new file, ``<CODE>.v<N+1>.yaml``, whose
``valid_from`` closes the previous version the day before. The catalogue sync
(``engine.catalogue_sync``) enforces that against what the database holds.

What "changed" means is ``content_sha256``: the SHA-256 of the parsed document,
canonicalised, **without its labels**. Comments and formatting never count,
and neither does wording: a label is how a step is shown, not what the
regulator requires, so a typo is fixed in place and the sync refreshes it.
Steps, transitions, deadlines, pieces and variants are the regulatory content.

The engine interprets everything here except ``escalation``, which is the
plugin's own data for its ``variant()`` method, stored and hashed as is.

Event references (a rule's ``anchor`` and ``closes_on``) are ``step:<code>``,
entering that step, or ``response:<code>``, recording that response.

Always ``yaml.safe_load``: a catalogue is data, and ``yaml.load`` would let a
file construct arbitrary Python objects.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import re
from collections import Counter, deque
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError, model_validator

from shared.const import (
    Bucket,
    Channel,
    CorrespondenceDirection,
    Counterparty,
    DeadlineKind,
    DeadlineParty,
    DocumentKind,
    NextActionCode,
    OffsetUnit,
    ProcedureScope,
    ProcedureStatus,
    Recipient,
    RuleKind,
)

LANGUAGES = ("fr", "en", "nl", "de")

_FILE_NAME = re.compile(r"^(?P<code>[A-Z][A-Z0-9_]*)\.v(?P<version>[1-9][0-9]*)\.yaml$")
_EVENT = re.compile(r"^(step|response):[a-z][a-z0-9_]*$")


class CatalogueError(Exception):
    """A catalogue that cannot be loaded. The message names the file."""


def _four_languages(labels: dict[str, str]) -> dict[str, str]:
    missing = [language for language in LANGUAGES if not labels.get(language, "").strip()]
    unknown = sorted(set(labels) - set(LANGUAGES))
    if missing or unknown:
        raise ValueError(
            f"labels need exactly {', '.join(LANGUAGES)}"
            + (f"; missing {', '.join(missing)}" if missing else "")
            + (f"; unknown {', '.join(unknown)}" if unknown else "")
        )
    return labels


Labels = Annotated[dict[str, str], AfterValidator(_four_languages)]
LowerCode = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=64)]
UpperCode = Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]*$", max_length=64)]
EventRef = Annotated[str, Field(pattern=_EVENT.pattern, max_length=74)]
Variants = tuple[LowerCode, ...] | None  # None: every variant

StatusName = Literal["draft", "ready", "sent", "in_force", "refused", "cancelled", "lapsed"]


def step_event(code: str) -> str:
    return f"step:{code}"


def response_event(code: str) -> str:
    return f"response:{code}"


def _applies(restriction: Variants, variant: str | None) -> bool:
    return restriction is None or variant in restriction


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class NextAction(_Model):
    """The dashboard's button on a step: what it does, and what it says."""

    code: NextActionCode
    labels: Labels | None = None

    @model_validator(mode="after")
    def _labelled(self) -> NextAction:
        if self.code != NextActionCode.NONE and self.labels is None:
            raise ValueError(f"next action {self.code} needs labels")
        return self


class Step(_Model):
    code: LowerCode
    status: StatusName
    bucket: Bucket
    next_action: NextAction
    variants: Variants = None
    labels: Labels

    @property
    def common_status(self) -> ProcedureStatus:
        return ProcedureStatus[self.status.upper()]


class Transition(_Model):
    source: LowerCode = Field(alias="from")
    target: LowerCode = Field(alias="to")
    # Applied by the engine itself (pieces complete, effect date reached). Any
    # edge can still be taken by hand, with a reason.
    automatic: bool = False
    variants: Variants = None


class ResponseType(_Model):
    code: LowerCode
    direction: Literal["inbound", "outbound"]
    counterparty: Literal["grd", "regulator", "participant", "other"]
    to_step: LowerCode | None = None  # the transition it proposes; None: informative
    variants: Variants = None
    labels: Labels

    @property
    def correspondence_direction(self) -> CorrespondenceDirection:
        return CorrespondenceDirection[self.direction.upper()]

    @property
    def counterparty_code(self) -> Counterparty:
        return Counterparty[self.counterparty.upper()]


class Offset(_Model):
    value: int = Field(ge=0, le=3650)
    unit: Literal["working_days", "calendar_days", "months"]

    @property
    def offset_unit(self) -> OffsetUnit:
        return {
            "working_days": OffsetUnit.BUSINESS_DAYS,
            "calendar_days": OffsetUnit.CALENDAR_DAYS,
            "months": OffsetUnit.MONTHS,
        }[self.unit]


class AnnualDate(_Model):
    month: int = Field(ge=1, le=12)
    day: int = Field(ge=1, le=31)

    @model_validator(mode="after")
    def _every_year(self) -> AnnualDate:
        # A non-leap year: 29 February would not exist three years in four.
        try:
            datetime.date(2025, self.month, self.day)
        except ValueError:
            raise ValueError(
                f"{self.day:02d}/{self.month:02d} is not a day of every year"
            ) from None
        return self

    def in_year(self, year: int) -> datetime.date:
        return datetime.date(year, self.month, self.day)


class DeadlineRule(_Model):
    code: LowerCode
    kind: Literal["relative", "annual"]
    anchor: EventRef | None = None  # relative: the event that starts the clock
    offset: Offset | None = None  # relative
    annual: AnnualDate | None = None  # annual: the day, in the procedure's period
    party: Literal["community", "grd", "regulator"]
    deadline_kind: Literal["submission", "response", "lapse", "effect"]
    closes_on: tuple[EventRef, ...] = ()  # events that meet the deadline
    variants: Variants = None
    labels: Labels

    @model_validator(mode="after")
    def _shape(self) -> DeadlineRule:
        if self.kind == "relative" and (
            self.anchor is None or self.offset is None or self.annual is not None
        ):
            raise ValueError(f"rule {self.code}: a relative rule has an anchor and an offset only")
        if self.kind == "annual" and (
            self.annual is None or self.anchor is not None or self.offset is not None
        ):
            raise ValueError(f"rule {self.code}: an annual rule has an annual date only")
        return self

    @property
    def rule_kind(self) -> RuleKind:
        return RuleKind[self.kind.upper()]

    @property
    def deadline_party(self) -> DeadlineParty:
        return DeadlineParty[self.party.upper()]

    @property
    def kind_of_deadline(self) -> DeadlineKind:
        return DeadlineKind[self.deadline_kind.upper()]


class DocumentSpec(_Model):
    code: LowerCode
    kind: Literal["generated", "collected", "received"]
    required: bool = True
    # A named condition the plugin's required_documents() evaluates; the piece
    # is then required only when it holds.
    condition: LowerCode | None = None
    per_member: bool = False  # one piece per participant (a sworn declaration)
    template: str | None = Field(default=None, max_length=128)
    variants: Variants = None
    labels: Labels

    @property
    def document_kind(self) -> DocumentKind:
        return DocumentKind[self.kind.upper()]


def _duplicates(codes: Iterable[str]) -> list[str]:
    return sorted(code for code, count in Counter(codes).items() if count > 1)


class ProcedureDefinition(_Model):
    """One version of one type of procedure."""

    code: UpperCode
    version: int = Field(ge=1)
    valid_from: datetime.date
    scope: Literal["community", "operation"]
    recipient: Literal["grd_ref", "regulator"]
    channel: Literal["email", "online_form", "post"]
    family: UpperCode  # one open draft per family
    handles_change_kinds: tuple[LowerCode, ...] = ()
    opens_annually: AnnualDate | None = None  # a calendar procedure, one per year
    variants: tuple[LowerCode, ...] = ()
    default_variant: LowerCode | None = None
    escalation: dict[str, Any] = Field(default_factory=dict)  # the plugin's, opaque here
    initial_step: LowerCode
    steps: tuple[Step, ...] = Field(min_length=1)
    transitions: tuple[Transition, ...] = ()
    response_types: tuple[ResponseType, ...] = ()
    deadline_rules: tuple[DeadlineRule, ...] = ()
    documents: tuple[DocumentSpec, ...] = ()
    labels: Labels

    @model_validator(mode="after")
    def _coherent(self) -> ProcedureDefinition:
        problems: list[str] = []
        for what, codes in (
            ("step", [s.code for s in self.steps]),
            ("response type", [r.code for r in self.response_types]),
            ("deadline rule", [r.code for r in self.deadline_rules]),
            ("document", [d.code for d in self.documents]),
            ("change kind", list(self.handles_change_kinds)),
            ("variant", list(self.variants)),
        ):
            for code in _duplicates(codes):
                problems.append(f"{what} {code} is declared twice")

        steps = {s.code: s for s in self.steps}
        responses = {r.code for r in self.response_types}
        initial = steps.get(self.initial_step)
        if initial is None:
            problems.append(f"initial step {self.initial_step} is not a step")
        elif initial.common_status != ProcedureStatus.DRAFT:
            problems.append(f"initial step {self.initial_step} must have status draft")

        for edge in self.transitions:
            for end in (edge.source, edge.target):
                if end not in steps:
                    problems.append(f"transition {edge.source} -> {edge.target}: no step {end}")
        for pair in _duplicates(f"{e.source} -> {e.target}" for e in self.transitions):
            problems.append(f"transition {pair} is declared twice")

        for response in self.response_types:
            if response.to_step is not None and response.to_step not in steps:
                problems.append(
                    f"response {response.code} leads to unknown step {response.to_step}"
                )

        def _known(event: str) -> bool:
            family, _, code = event.partition(":")
            return code in (steps if family == "step" else responses)

        for rule in self.deadline_rules:
            for event in (rule.anchor, *rule.closes_on):
                if event is not None and not _known(event):
                    problems.append(f"rule {rule.code} refers to unknown {event}")

        if self.variants:
            if self.default_variant not in self.variants:
                problems.append(f"default variant {self.default_variant} is not a variant")
        elif self.default_variant is not None:
            problems.append("a default variant needs a list of variants")
        declared = set(self.variants)
        restricted: list[tuple[str, Variants]] = [
            *((f"step {s.code}", s.variants) for s in self.steps),
            *((f"transition {e.source} -> {e.target}", e.variants) for e in self.transitions),
            *((f"response {r.code}", r.variants) for r in self.response_types),
            *((f"rule {r.code}", r.variants) for r in self.deadline_rules),
            *((f"document {d.code}", d.variants) for d in self.documents),
        ]
        for where, restriction in restricted:
            for variant in sorted(set(restriction or ()) - declared):
                problems.append(f"{where} names unknown variant {variant}")

        if initial is not None:
            unreachable = sorted(set(steps) - self._reachable_from(self.initial_step))
            if unreachable:
                problems.append(f"no transition reaches {', '.join(unreachable)}")

        if problems:
            raise ValueError("; ".join(problems))
        return self

    def _reachable_from(self, start: str) -> set[str]:
        seen = {start}
        queue = deque([start])
        while queue:
            current = queue.popleft()
            for edge in self.transitions:
                if edge.source == current and edge.target not in seen:
                    seen.add(edge.target)
                    queue.append(edge.target)
        return seen

    # -- coded values -----------------------------------------------------

    @property
    def procedure_scope(self) -> ProcedureScope:
        return ProcedureScope[self.scope.upper()]

    @property
    def recipient_code(self) -> Recipient:
        return Recipient[self.recipient.upper()]

    @property
    def channel_code(self) -> Channel:
        return Channel[self.channel.upper()]

    # -- lookups ----------------------------------------------------------

    def step(self, code: str) -> Step:
        for step in self.steps:
            if step.code == code:
                return step
        raise KeyError(f"{self.code} v{self.version} has no step {code!r}")

    def response_type(self, code: str) -> ResponseType:
        for response in self.response_types:
            if response.code == code:
                return response
        raise KeyError(f"{self.code} v{self.version} has no response type {code!r}")

    def allows(self, source: str, target: str, variant: str | None) -> bool:
        return any(
            e.source == source and e.target == target and _applies(e.variants, variant)
            for e in self.transitions
        )

    def steps_for(self, variant: str | None) -> tuple[Step, ...]:
        return tuple(s for s in self.steps if _applies(s.variants, variant))

    def rules_started_by(self, event: str, variant: str | None) -> tuple[DeadlineRule, ...]:
        return tuple(
            r for r in self.deadline_rules if r.anchor == event and _applies(r.variants, variant)
        )

    def rules_closed_by(self, event: str, variant: str | None) -> tuple[DeadlineRule, ...]:
        return tuple(
            r for r in self.deadline_rules if event in r.closes_on and _applies(r.variants, variant)
        )

    def annual_rules(self, variant: str | None) -> tuple[DeadlineRule, ...]:
        return tuple(
            r for r in self.deadline_rules if r.kind == "annual" and _applies(r.variants, variant)
        )

    def documents_for(self, variant: str | None) -> tuple[DocumentSpec, ...]:
        return tuple(d for d in self.documents if _applies(d.variants, variant))


@dataclass(frozen=True)
class CatalogueEntry:
    definition: ProcedureDefinition
    content_sha256: str
    # Derived, never written in a file: the day before the next version's
    # valid_from. None for the latest version.
    valid_to: datetime.date | None
    source: str  # the file name, for messages

    def in_force_on(self, day: datetime.date) -> bool:
        return self.definition.valid_from <= day and (self.valid_to is None or day <= self.valid_to)


@dataclass(frozen=True)
class Catalogue:
    regulator: str
    entries: tuple[CatalogueEntry, ...]

    def get(self, code: str, version: int) -> CatalogueEntry | None:
        for entry in self.entries:
            if entry.definition.code == code and entry.definition.version == version:
                return entry
        return None

    def versions(self, code: str) -> tuple[CatalogueEntry, ...]:
        return tuple(e for e in self.entries if e.definition.code == code)

    def in_force(self, code: str, on: datetime.date) -> CatalogueEntry | None:
        return next((e for e in self.versions(code) if e.in_force_on(on)), None)

    def handling(self, change_kind: str, on: datetime.date) -> tuple[CatalogueEntry, ...]:
        """The procedures in force on ``on`` that a change of this kind feeds."""
        return tuple(
            e
            for e in self.entries
            if e.in_force_on(on) and change_kind in e.definition.handles_change_kinds
        )


# -- hashing ---------------------------------------------------------------


def _without_labels(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _without_labels(v) for k, v in value.items() if k != "labels"}
    if isinstance(value, list):
        return [_without_labels(v) for v in value]
    return value


def _json_default(value: Any) -> str:
    if isinstance(value, datetime.date):  # datetime is a date too
        return value.isoformat()
    raise TypeError(f"a catalogue holds no {type(value).__name__}")


def content_sha256(document: dict[str, Any]) -> str:
    """The SHA-256 of a parsed catalogue document, labels excluded."""
    canonical = json.dumps(
        _without_labels(document),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=_json_default,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# -- loading ---------------------------------------------------------------


def _load_file(path: Path) -> tuple[ProcedureDefinition, str]:
    match = _FILE_NAME.match(path.name)
    if match is None:
        raise CatalogueError(f"{path.name}: a catalogue file is named <CODE>.v<N>.yaml")
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise CatalogueError(f"{path.name}: {exc}") from exc
    if not isinstance(document, dict):
        raise CatalogueError(f"{path.name}: the file must hold one mapping")
    try:
        definition = ProcedureDefinition.model_validate(document)
    except ValidationError as exc:
        raise CatalogueError(f"{path.name}: {exc}") from exc
    if (definition.code, definition.version) != (match["code"], int(match["version"])):
        raise CatalogueError(
            f"{path.name} holds {definition.code} v{definition.version}: "
            f"the file name and its content must agree"
        )
    try:
        digest = content_sha256(document)
    except TypeError as exc:
        raise CatalogueError(f"{path.name}: {exc}") from exc
    return definition, digest


def load_catalogue(directory: Path, *, regulator: str) -> Catalogue:
    """Load every ``*.yaml`` in ``directory`` as ``regulator``'s catalogue."""
    strays = sorted(p.name for p in directory.glob("*.yml"))
    if strays:
        raise CatalogueError(f"{directory}: use .yaml, not .yml: {', '.join(strays)}")
    loaded = [(path.name, *_load_file(path)) for path in sorted(directory.glob("*.yaml"))]
    if not loaded:
        raise CatalogueError(f"{directory}: no catalogue file")

    entries: list[CatalogueEntry] = []
    for code in sorted({definition.code for _, definition, _ in loaded}):
        versions = sorted(
            (item for item in loaded if item[1].code == code), key=lambda item: item[1].version
        )
        numbers = [definition.version for _, definition, _ in versions]
        if numbers != list(range(1, len(numbers) + 1)):
            raise CatalogueError(f"{code}: versions must run 1, 2, 3…, found {numbers}")
        for (name, definition, digest), following in zip(
            versions, [*versions[1:], None], strict=True
        ):
            valid_to = None
            if following is not None:
                successor = following[1]
                if successor.valid_from <= definition.valid_from:
                    raise CatalogueError(
                        f"{following[0]}: valid_from must come after v{definition.version}'s"
                    )
                valid_to = successor.valid_from - datetime.timedelta(days=1)
            entries.append(CatalogueEntry(definition, digest, valid_to, name))
    return Catalogue(regulator=regulator, entries=tuple(entries))
