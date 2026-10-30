"""The contract between the engine and the regulator plugins.

It belongs to the engine: a plugin imports this module, never the reverse, so
the engine names what it needs without knowing who provides it. The entry
points build the registry from ``regulators.registry`` and hand it in.

Data first. A plugin's catalogue and calendar are files the engine reads
(``catalogue()``, ``calendar()``); its methods cover only what data cannot say:
switching the variant, the conditions on pieces, the mapping to templates, and
what each of its "sent" steps means for the core's regulatory status. Which
rows of the core a procedure touches, and with which dates, is the engine's
(``engine.effects``): the same for every regulator.
"""

from __future__ import annotations

import datetime
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable

from engine.calendar import CalendarData
from engine.catalogue import Catalogue, ProcedureDefinition
from engine.entry_sheets import EntrySheet
from shared.const import ChangeSubjectType, DocumentKind, ProcedureStatus, RegulatoryStatus


@dataclass(frozen=True)
class ChangeFact:
    """A change as a plugin sees it: a CRM fact, the same for every regulator."""

    kind: str  # 'meter_added', 'key_changed'…
    subject_type: ChangeSubjectType
    subject_ref: str  # an EAN, a key id…
    payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProcedureFacts:
    """What a plugin sees of one procedure (the spec's ``Dossier``)."""

    definition: ProcedureDefinition
    variant: str | None
    id_community: int
    id_sharing_operation: int | None
    changes: tuple[ChangeFact, ...] = ()
    # Facts the engine read for the plugin: the operation's members, whether
    # its key is standard…
    context: Mapping[str, Any] = field(default_factory=dict)
    requested_effective_date: datetime.date | None = None
    effective_date: datetime.date | None = None


@dataclass(frozen=True)
class DocumentRequirement:
    """One piece a procedure needs, as the plugin decides it."""

    code: str  # a DocumentSpec code of the definition
    kind: DocumentKind
    required: bool = True
    id_member: int | None = None  # set on a per-member piece
    condition: Mapping[str, Any] | None = None  # why it is required, for display


# What a step of common status "sent" may mean for a subject it carries.
SENT_STATUSES = (
    RegulatoryStatus.PENDING_GRD,
    RegulatoryStatus.PENDING_REGULATOR,
    RegulatoryStatus.AUTHORIZED,
)


@runtime_checkable
class RegulatorPlugin(Protocol):
    code: str  # the value of community.regulator: what selects the plugin
    region: str  # whose working calendar its deadlines count on
    # Stamped across every page of a working draft (docgen's watermark), in the
    # regulator's language; the final, unstamped, is what is sent.
    draft_watermark: str

    def catalogue(self) -> Catalogue: ...

    def calendar(self) -> CalendarData: ...

    def variant(
        self, definition: ProcedureDefinition, changes: Sequence[ChangeFact]
    ) -> str | None: ...

    def required_documents(self, procedure: ProcedureFacts) -> list[DocumentRequirement]: ...

    def render_context(
        self, requirement_code: str, snapshot: Mapping[str, Any]
    ) -> dict[str, Any]: ...

    def episode_status(self, definition: ProcedureDefinition, step: str) -> RegulatoryStatus | None:
        """What the core shows of a subject while its procedure is on ``step``,
        a step of common status "sent": waiting for the GRD, waiting for the
        regulator, or authorised (one of ``SENT_STATUSES``; None for a step it
        does not know, which ``build_registry`` refuses). The engine maps the
        other common statuses itself."""
        ...

    def entry_sheet(self, code: str) -> EntrySheet | None:
        """The entry sheet of the form piece ``code`` (its values in the order of
        the regulator's form), or None when the piece has none."""
        ...


PluginRegistry = Mapping[str, RegulatorPlugin]


class RegistryError(Exception):
    """Plugins that cannot be registered together."""


def build_registry(plugins: Iterable[RegulatorPlugin]) -> PluginRegistry:
    """A read-only ``{community.regulator: plugin}``, checked.

    Each plugin must own its catalogue and its calendar, no two plugins may
    share a code or a region (the region's calendar has one writer), and every
    "sent" step of a procedure that files a tracked change must mean one of
    ``SENT_STATUSES`` (``episode_status``).
    """
    registry: dict[str, RegulatorPlugin] = {}
    regions: dict[str, str] = {}
    for plugin in plugins:
        if not isinstance(plugin, RegulatorPlugin):
            raise RegistryError(f"{plugin!r} does not implement RegulatorPlugin")
        if plugin.code in registry:
            raise RegistryError(f"two plugins claim regulator {plugin.code}")
        if plugin.region in regions:
            raise RegistryError(
                f"{plugin.code} and {regions[plugin.region]} both claim region {plugin.region}"
            )
        catalogue_owner = plugin.catalogue().regulator
        if catalogue_owner != plugin.code:
            raise RegistryError(f"{plugin.code} returns the catalogue of {catalogue_owner}")
        calendar_region = plugin.calendar().region
        if calendar_region != plugin.region:
            raise RegistryError(
                f"{plugin.code} counts on region {plugin.region} "
                f"but returns the calendar of {calendar_region}"
            )
        _check_episode_statuses(plugin)
        registry[plugin.code] = plugin
        regions[plugin.region] = plugin.code
    return MappingProxyType(registry)


# The change kinds whose subject the core tracks (``engine.effects``), and how
# the core names that subject: an addition owns its episode, a removal moves one.
ADDITION_KINDS: Mapping[str, ChangeSubjectType] = MappingProxyType(
    {
        "meter_added": ChangeSubjectType.METER,
        "installation_added": ChangeSubjectType.METER,
        "key_changed": ChangeSubjectType.KEY,
    }
)
REMOVAL_KINDS: Mapping[str, ChangeSubjectType] = MappingProxyType(
    {
        "meter_removed": ChangeSubjectType.METER,
        "installation_removed": ChangeSubjectType.METER,
    }
)
TRACKED_CHANGE_KINDS = frozenset({*ADDITION_KINDS, *REMOVAL_KINDS})


def _check_episode_statuses(plugin: RegulatorPlugin) -> None:
    for entry in plugin.catalogue().entries:
        definition = entry.definition
        if not TRACKED_CHANGE_KINDS & set(definition.handles_change_kinds):
            continue
        for step in definition.steps:
            if step.common_status != ProcedureStatus.SENT:
                continue
            status = plugin.episode_status(definition, step.code)
            if status not in SENT_STATUSES:
                raise RegistryError(
                    f"{plugin.code} {definition.code}: step {step.code} means {status!r} "
                    f"for the core, not one of {[s.name for s in SENT_STATUSES]}"
                )


def plugin_for(registry: PluginRegistry, regulator: str | None) -> RegulatorPlugin | None:
    """The plugin of a community's regulator, or None: not supported (yet)."""
    if not regulator:
        return None
    return registry.get(regulator)
