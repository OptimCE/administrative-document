"""Projecting each plugin's catalogue and calendar into the database.

The plugin's files are the source; ``procedure_definition``, the catalogue rows
of ``deadline_rule`` and ``working_calendar`` are their projection, which the
engine needs for foreign keys and SQL reads. The sync runs at startup in the
API AND the worker, so neither process ever runs on a catalogue the database
does not hold; a transaction-scoped advisory lock serialises the two, and the
second finds nothing to do.

Per definition version, against the database:

- absent: inserted, with its rules;
- reserved (``content_sha256`` NULL, written by migration 0003 for the July
  dossiers): filled, after checking scope, recipient, channel and family agree
  with the file — the converted dossiers already rely on them;
- same content: left alone, except the derived columns (labels, ``valid_to``),
  refreshed in place when they differ;
- different content: REFUSED. A released version is evidence of what a running
  procedure follows; changing it is a new version.

A version the database holds and the files do not is refused too (a released
file was deleted), unless it is newer than every file of its code: then a newer
image synced it, and this process leaves that code alone rather than regress it.

Last, the converted July deadlines are linked to the catalogue rule they name:
migration 0003 left them ``id_deadline_rule`` NULL with the rule's code in
``deadline_type``, the rules not existing yet.

Platform data, not tenant data: nothing here is community-scoped, and the
deadline relink deliberately spans every community.
"""

from __future__ import annotations

import datetime
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from engine.calendar import CalendarData
from engine.catalogue import CatalogueEntry, DeadlineRule
from engine.contracts import PluginRegistry, RegulatorPlugin
from shared.models.local_models import (
    DeadlineModel,
    DeadlineRuleModel,
    DossierModel,
    ProcedureDefinitionModel,
    WorkingCalendarModel,
)

logger = logging.getLogger(__name__)

# Arbitrary but stable, namespaced by service like the deadline sweep's
# (worker/sweeps.py). Transaction-scoped: released at COMMIT or ROLLBACK.
_SYNC_ADVISORY_LOCK_KEY = 0x0AD3_0002

# What a reserved row already promised the dossiers converted onto it.
_RESERVED_IDENTITY = ("scope", "recipient", "channel", "family")

# deadline_rule.description is VARCHAR(255).
_DESCRIPTION_MAX = 255


class CatalogueSyncError(Exception):
    """The catalogue files and the database disagree in a way only a person can settle."""


@dataclass
class SyncReport:
    regulator: str
    inserted: list[str] = field(default_factory=list)
    filled: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    refreshed: list[str] = field(default_factory=list)
    newer_in_database: list[str] = field(default_factory=list)
    rules_written: int = 0
    deadlines_relinked: int = 0
    holidays_added: int = 0
    holidays_removed: int = 0
    holidays_relabelled: int = 0

    @property
    def wrote_anything(self) -> bool:
        return bool(
            self.inserted
            or self.filled
            or self.refreshed
            or self.rules_written
            or self.deadlines_relinked
            or self.holidays_added
            or self.holidays_removed
            or self.holidays_relabelled
        )


def _name(code: str, version: int) -> str:
    return f"{code} v{version}"


def _projection(entry: CatalogueEntry) -> dict[str, Any]:
    """The ``procedure_definition`` columns one catalogue entry sets."""
    definition = entry.definition
    dumped = definition.model_dump(mode="json", by_alias=True)
    return {
        "valid_from": definition.valid_from,
        "valid_to": entry.valid_to,
        "scope": int(definition.procedure_scope),
        "recipient": int(definition.recipient_code),
        "channel": int(definition.channel_code),
        "family": definition.family,
        "handles_change_kinds": list(definition.handles_change_kinds),
        "escalation_rules_json": {
            "variants": dumped["variants"],
            "default_variant": dumped["default_variant"],
            "escalation": dumped["escalation"],
        },
        "required_documents_json": dumped["documents"],
        "transitions_json": {
            "initial_step": dumped["initial_step"],
            "steps": dumped["steps"],
            "transitions": dumped["transitions"],
            "opens_annually": dumped["opens_annually"],
        },
        "response_types_json": dumped["response_types"],
        "labels_json": dumped["labels"],
    }


def _rule_projection(rule: DeadlineRule) -> dict[str, Any]:
    """The ``deadline_rule`` columns one catalogue rule sets. The legacy July
    columns stay NULL (``ck_deadline_rule_generation``)."""
    return {
        "kind": int(rule.rule_kind),
        "anchor_event": rule.anchor,
        "offset_value": rule.offset.value if rule.offset else None,
        "offset_unit": int(rule.offset.offset_unit) if rule.offset else None,
        "annual_month": rule.annual.month if rule.annual else None,
        "annual_day": rule.annual.day if rule.annual else None,
        "party": int(rule.deadline_party),
        "deadline_kind": int(rule.kind_of_deadline),
        "recurring": False,
        "description": rule.labels["fr"][:_DESCRIPTION_MAX],
    }


def _assign(row: object, values: dict[str, Any]) -> bool:
    """Set the attributes that differ; True when one did. An equal value is not
    assigned, so an unchanged row emits no UPDATE."""
    changed = False
    for column, value in values.items():
        if getattr(row, column) != value:
            setattr(row, column, value)
            changed = True
    return changed


async def _sync_rules(session: AsyncSession, id_definition: int, entry: CatalogueEntry) -> int:
    existing = {
        row.code: row
        for row in await session.scalars(
            select(DeadlineRuleModel).where(
                DeadlineRuleModel.id_procedure_definition == id_definition
            )
        )
    }
    written = 0
    for rule in entry.definition.deadline_rules:
        values = _rule_projection(rule)
        row = existing.get(rule.code)
        if row is None:
            session.add(
                DeadlineRuleModel(id_procedure_definition=id_definition, code=rule.code, **values)
            )
            written += 1
        elif _assign(row, values):
            written += 1
    await session.flush()
    return written


def _check_reserved(row: ProcedureDefinitionModel, values: dict[str, Any]) -> None:
    disagree = [
        f"{column} {getattr(row, column)!r} vs {values[column]!r}"
        for column in _RESERVED_IDENTITY
        if getattr(row, column) != values[column]
    ]
    if disagree:
        raise CatalogueSyncError(
            f"{row.regulator} {_name(row.code, row.version)} was reserved by a data migration "
            f"with other values than its file ({'; '.join(disagree)}): the converted "
            f"dossiers rely on the reserved ones"
        )


def _check_versions_kept(
    plugin: RegulatorPlugin, rows: list[ProcedureDefinitionModel], report: SyncReport
) -> set[str]:
    """Refuse a released version whose file is gone; return the codes a newer
    image already synced, which this process must not touch."""
    catalogue = plugin.catalogue()
    newer_codes: set[str] = set()
    for row in rows:
        if catalogue.get(row.code, row.version) is not None:
            continue
        versions = [e.definition.version for e in catalogue.versions(row.code)]
        if versions and row.version > max(versions):
            report.newer_in_database.append(_name(row.code, row.version))
            newer_codes.add(row.code)
            continue
        if row.content_sha256 is None:
            raise CatalogueSyncError(
                f"{plugin.code} {_name(row.code, row.version)} was reserved by a data "
                f"migration, but the plugin has no such file"
            )
        raise CatalogueSyncError(
            f"{plugin.code} {_name(row.code, row.version)} is in the database but its file "
            f"is gone: a released version stays in the catalogue"
        )
    return newer_codes


async def _sync_definition(
    session: AsyncSession,
    plugin: RegulatorPlugin,
    entry: CatalogueEntry,
    row: ProcedureDefinitionModel | None,
    report: SyncReport,
) -> None:
    definition = entry.definition
    name = _name(definition.code, definition.version)
    values = _projection(entry)
    now = datetime.datetime.now(datetime.UTC)

    if row is None:
        row = ProcedureDefinitionModel(
            regulator=plugin.code,
            code=definition.code,
            version=definition.version,
            content_sha256=entry.content_sha256,
            synced_at=now,
            **values,
        )
        session.add(row)
        report.inserted.append(name)
    elif row.content_sha256 is None:
        _check_reserved(row, values)
        _assign(row, values)
        row.content_sha256 = entry.content_sha256
        row.synced_at = now
        report.filled.append(name)
    elif row.content_sha256 != entry.content_sha256:
        raise CatalogueSyncError(
            f"{plugin.code} {name} ({entry.source}) changed since it was synced, and "
            f"running procedures follow it. Revert the file, or add "
            f"{definition.code}.v{definition.version + 1}.yaml"
        )
    elif _assign(row, values):
        row.synced_at = now
        report.refreshed.append(name)
    else:
        report.unchanged.append(name)

    # Flushed one version at a time, oldest first: closing v1 (valid_to) must
    # reach the database before v2 is inserted, or uq_procedure_definition_
    # current sees two versions in force.
    await session.flush()
    report.rules_written += await _sync_rules(session, row.id, entry)


async def _relink_deadlines_unscoped(session: AsyncSession, regulator: str) -> int:
    result = await session.execute(
        update(DeadlineModel)
        .where(
            DeadlineModel.id_deadline_rule.is_(None),
            DeadlineModel.id_dossier == DossierModel.id,
            DossierModel.regulator == regulator,
            DeadlineRuleModel.id_procedure_definition == DossierModel.id_procedure_definition,
            DeadlineRuleModel.code == DeadlineModel.deadline_type,
        )
        .values(id_deadline_rule=DeadlineRuleModel.id)
        .execution_options(synchronize_session=False)
    )
    return int(result.rowcount or 0)


async def _sync_calendar(session: AsyncSession, data: CalendarData, report: SyncReport) -> None:
    wanted = {holiday.day: holiday.label for holiday in data.holidays}
    rows = {
        row.day: row
        for row in await session.scalars(
            select(WorkingCalendarModel).where(WorkingCalendarModel.region == data.region)
        )
    }
    for day, row in rows.items():
        if day not in wanted:
            await session.delete(row)
            report.holidays_removed += 1
        elif row.label != wanted[day]:
            row.label = wanted[day]
            report.holidays_relabelled += 1
    for day, label in wanted.items():
        if day not in rows:
            session.add(WorkingCalendarModel(region=data.region, day=day, label=label))
            report.holidays_added += 1
    await session.flush()


async def sync_plugin(session: AsyncSession, plugin: RegulatorPlugin) -> SyncReport:
    """Project one plugin into the database, in the caller's transaction."""
    await session.execute(select(func.pg_advisory_xact_lock(_SYNC_ADVISORY_LOCK_KEY)))
    report = SyncReport(regulator=plugin.code)

    rows = list(
        await session.scalars(
            select(ProcedureDefinitionModel).where(
                ProcedureDefinitionModel.regulator == plugin.code
            )
        )
    )
    newer_codes = _check_versions_kept(plugin, rows, report)
    by_key = {(row.code, row.version): row for row in rows}
    entries = sorted(
        plugin.catalogue().entries, key=lambda e: (e.definition.code, e.definition.version)
    )
    for entry in entries:
        if entry.definition.code in newer_codes:
            continue
        row = by_key.get((entry.definition.code, entry.definition.version))
        await _sync_definition(session, plugin, entry, row, report)

    report.deadlines_relinked = await _relink_deadlines_unscoped(session, plugin.code)
    await _sync_calendar(session, plugin.calendar(), report)
    return report


async def sync_registry(
    session_factory: Callable[[], AsyncSession], registry: PluginRegistry
) -> list[SyncReport]:
    """Project every registered plugin, in one transaction. Raises on a conflict,
    which is meant to stop the process: it must not run on a catalogue the
    database contradicts."""
    async with session_factory() as session, session.begin():
        reports = [await sync_plugin(session, plugin) for plugin in registry.values()]
    for report in reports:
        if report.newer_in_database:
            logger.warning(
                "catalogue %s: the database holds newer versions than this image "
                "(%s); left them alone",
                report.regulator,
                ", ".join(report.newer_in_database),
            )
        logger.info(
            "catalogue %s synced: %d inserted, %d filled, %d refreshed, %d unchanged; "
            "%d rules written, %d deadlines relinked; holidays +%d -%d ~%d",
            report.regulator,
            len(report.inserted),
            len(report.filled),
            len(report.refreshed),
            len(report.unchanged),
            report.rules_written,
            report.deadlines_relinked,
            report.holidays_added,
            report.holidays_removed,
            report.holidays_relabelled,
        )
    return reports
