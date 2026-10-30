"""The contract between the engine and the regulator plugins.

It belongs to the engine: a plugin imports this module, never the reverse, so
the engine names what it needs without knowing who provides it. The entry
points build the registry from ``regulators.registry`` and hand it in.

Data first. A plugin's catalogue and calendar are files the engine reads
(``catalogue()``, ``calendar()``); its methods cover only what data cannot say:
switching the variant, the conditions on pieces, the mapping to templates, and
the effects on the core.
"""

from __future__ import annotations

import datetime
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable

from engine.calendar import CalendarData
from engine.catalogue import Catalogue, ProcedureDefinition
from shared.const import ChangeSubjectType, DocumentKind, RegulatoryStatus


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


@dataclass(frozen=True)
class Effect:
    """What a procedure in force writes to the core's ``regulatory_link_status``."""

    subject_type: ChangeSubjectType  # METER or KEY: the only subjects the core tracks
    subject_ref: str
    id_sharing_operation: int
    regulatory_status: RegulatoryStatus
    valid_from: datetime.date | None = None
    valid_to: datetime.date | None = None


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

    def effects(self, procedure: ProcedureFacts) -> list[Effect]: ...


PluginRegistry = Mapping[str, RegulatorPlugin]


class RegistryError(Exception):
    """Plugins that cannot be registered together."""


def build_registry(plugins: Iterable[RegulatorPlugin]) -> PluginRegistry:
    """A read-only ``{community.regulator: plugin}``, checked.

    Each plugin must own its catalogue and its calendar, and no two plugins may
    share a code or a region (the region's calendar has one writer).
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
        registry[plugin.code] = plugin
        regions[plugin.region] = plugin.code
    return MappingProxyType(registry)


def plugin_for(registry: PluginRegistry, regulator: str | None) -> RegulatorPlugin | None:
    """The plugin of a community's regulator, or None: not supported (yet)."""
    if not regulator:
        return None
    return registry.get(regulator)
