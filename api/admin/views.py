"""What the ``/admin`` reads return, built from rows and the plugin's catalogue.

Labels come from the version of the catalogue a procedure was opened under,
never the one in force: a procedure keeps its definition. "Due in N working
days" is counted here, on the plugin's calendar, so every client shows the
same number (acceptance criterion 1.5).
"""

from __future__ import annotations

import datetime
from collections.abc import Mapping, Sequence
from typing import Any

from engine.calendar import CalendarNotCoveredError, WorkingCalendar
from engine.catalogue import CatalogueEntry, ProcedureDefinition
from shared.const import (
    ChangeStatus,
    DeadlineStatus,
    DocumentKind,
    ProcedureDocumentStatus,
    ProcedureStatus,
)
from shared.models.local_models import (
    ChangeModel,
    CorrespondenceModel,
    DeadlineModel,
    DocumentModel,
    DossierModel,
    StatusEventModel,
)

# A procedure in one of these is finished; it stays on the dashboard, under
# "done", for RECENT_DAYS.
FINISHED = (
    int(ProcedureStatus.IN_FORCE),
    int(ProcedureStatus.REFUSED),
    int(ProcedureStatus.CANCELLED),
    int(ProcedureStatus.LAPSED),
)
RECENT_DAYS = 30
BUCKETS = ("todo", "waiting", "planned", "done")

_PIECE_THERE = (int(ProcedureDocumentStatus.READY), int(ProcedureDocumentStatus.SIGNED))


def due_in(calendar: WorkingCalendar, today: datetime.date, due: datetime.date) -> int | None:
    try:
        return calendar.working_days_until(today, due)
    except CalendarNotCoveredError:
        return None


def piece_counts(pieces: Sequence[DocumentModel]) -> tuple[int, int]:
    """(ready, required) over the pieces the community supplies."""
    needed = [p for p in pieces if p.required and p.kind != int(DocumentKind.RECEIVED)]
    return sum(1 for p in needed if p.status in _PIECE_THERE), len(needed)


def item(
    dossier: DossierModel,
    definition: ProcedureDefinition,
    calendar: WorkingCalendar,
    *,
    today: datetime.date,
    operation_names: Mapping[int, str],
    changes_count: int,
    pieces: tuple[int, int],
) -> dict[str, Any]:
    step = definition.step(str(dossier.step))
    due = dossier.next_action_due
    return {
        "id": dossier.id,
        "code": definition.code,
        "version": definition.version,
        "family": definition.family,
        "purpose": definition.purpose,
        "labels": definition.labels,
        "status": dossier.status,
        "step": step.code,
        "step_labels": step.labels,
        "bucket": str(step.bucket),
        "variant": dossier.variant,
        "recipient": definition.recipient,
        "id_sharing_operation": dossier.id_sharing_operation,
        "operation_name": operation_names.get(dossier.id_sharing_operation or 0),
        "grd_ref": dossier.grd_ref,
        "external_ref": dossier.external_ref,
        "next_action": {"code": str(step.next_action.code), "labels": step.next_action.labels},
        "next_action_due": due,
        "due_in_working_days": due_in(calendar, today, due) if due else None,
        "overdue": bool(due and due < today and dossier.status not in FINISHED),
        "changes_count": changes_count,
        "pieces_ready": pieces[0],
        "pieces_required": pieces[1],
        "updated_at": dossier.updated_at,
    }


def deadline_item(
    deadline: DeadlineModel,
    dossier: DossierModel,
    definition: ProcedureDefinition,
    calendar: WorkingCalendar,
    *,
    today: datetime.date,
    operation_names: Mapping[int, str],
) -> dict[str, Any]:
    rule = next((r for r in definition.deadline_rules if r.code == deadline.deadline_type), None)
    is_open = deadline.status == int(DeadlineStatus.OPEN)
    return {
        "id": deadline.id,
        "id_dossier": dossier.id,
        "procedure_code": definition.code,
        "procedure_labels": definition.labels,
        "code": deadline.deadline_type,
        "labels": rule.labels if rule else None,
        "due_date": deadline.due_date,
        "party": deadline.party,
        "kind": deadline.kind,
        "status": deadline.status,
        "due_in_working_days": due_in(calendar, today, deadline.due_date) if is_open else None,
        "overdue": is_open and deadline.due_date < today,
        "id_sharing_operation": dossier.id_sharing_operation,
        "operation_name": operation_names.get(dossier.id_sharing_operation or 0),
    }


def definition_out(entry: CatalogueEntry, *, today: datetime.date) -> dict[str, Any]:
    dumped = entry.definition.model_dump(mode="json", by_alias=True)
    return {
        **{k: dumped[k] for k in ("code", "version", "valid_from", "purpose", "scope")},
        **{k: dumped[k] for k in ("recipient", "channel", "family", "labels", "variants")},
        **{k: dumped[k] for k in ("default_variant", "initial_step", "steps", "transitions")},
        **{k: dumped[k] for k in ("response_types", "deadline_rules", "documents")},
        "valid_to": entry.valid_to,
        "in_force": entry.in_force_on(today),
    }


def detail(
    dossier: DossierModel,
    definition: ProcedureDefinition,
    calendar: WorkingCalendar,
    *,
    today: datetime.date,
    operation_names: Mapping[int, str],
    changes: Sequence[ChangeModel],
    pieces: Sequence[DocumentModel],
    versions: Mapping[int, int],
    deadlines: Sequence[DeadlineModel],
    journal: Sequence[StatusEventModel],
    correspondence: Sequence[CorrespondenceModel],
    version_rows: Sequence[tuple[int, int, datetime.datetime, str | None]],
) -> dict[str, Any]:
    active = [c for c in changes if c.status == int(ChangeStatus.ATTACHED)]
    view = item(
        dossier,
        definition,
        calendar,
        today=today,
        operation_names=operation_names,
        changes_count=len(active),
        pieces=piece_counts(pieces),
    )
    step = str(dossier.step)
    visited = {e.to_step for e in journal if e.to_step}
    docs = {d.code: d for d in definition.documents}

    thread: list[dict[str, Any]] = []
    for event in journal:
        thread.append(
            {
                "at": event.occurred_at,
                "kind": "step",
                "actor": event.actor_id,
                "from_step": event.from_step,
                "to_step": event.to_step,
                "reason": (event.context_json or {}).get("reason"),
            }
        )
    for message in correspondence:
        thread.append(
            {
                "at": message.occurred_at,
                "kind": "correspondence",
                "actor": message.recorded_by,
                "id_correspondence": message.id,
                "type": message.type,
                "direction": message.direction,
                "subject": message.subject,
                "has_file": message.file_ref is not None,
            }
        )
    for document_id, version_no, created_at, generated_by in version_rows:
        thread.append(
            {
                "at": created_at,
                "kind": "version",
                "actor": generated_by,
                "id_document": document_id,
                "version_no": version_no,
            }
        )
    thread.sort(key=lambda entry: entry["at"])

    operation = operation_names.get(dossier.id_sharing_operation or 0)
    reference = f"[OPT-{dossier.id}]"
    return {
        **view,
        "channel": definition.channel,
        "scope": definition.scope,
        "opened_by": dossier.opened_by,
        "requested_effective_date": dossier.requested_effective_date,
        "effective_date": dossier.effective_date,
        "frozen_at": dossier.frozen_at,
        "effect_error": dossier.effect_error_json,
        "timeline": [
            {
                "code": s.code,
                "status": int(s.common_status),
                "bucket": str(s.bucket),
                "labels": s.labels,
                "current": s.code == step,
                "visited": s.code in visited,
            }
            for s in definition.steps_for(dossier.variant)
        ],
        "transitions": [
            {
                "to_step": e.target,
                "labels": definition.step(e.target).labels,
                "automatic": e.automatic,
            }
            for e in definition.transitions
            if e.source == step and definition.allows(step, e.target, dossier.variant)
        ],
        "response_types": [
            {
                "code": r.code,
                "direction": r.direction,
                "counterparty": r.counterparty,
                "to_step": r.to_step,
                "labels": r.labels,
                "applicable_now": r.to_step is None
                or r.to_step == step
                or definition.allows(step, r.to_step, dossier.variant),
            }
            for r in definition.response_types
            if r.variants is None or dossier.variant in r.variants
        ],
        "changes": [
            {
                "id": c.id,
                "kind": c.kind,
                "subject_type": c.subject_type,
                "subject_ref": c.subject_ref,
                "status": c.status,
                "detected_at": c.detected_at,
                "id_member": c.payload_json.get("id_member"),
                "grd": c.payload_json.get("grd"),
                "injection_status": c.payload_json.get("injection_status"),
                "effective_date": c.payload_json.get("effective_date"),
                "excluded_reason": (c.payload_json.get("excluded") or {}).get("reason"),
            }
            for c in changes
        ],
        "pieces": [
            {
                "id": p.id,
                "code": p.requirement_code or p.doc_type,
                "labels": docs[p.requirement_code].labels if p.requirement_code in docs else None,
                "kind": p.kind,
                "required": p.required,
                "status": p.status,
                "id_member": p.id_member,
                "condition": p.condition_json,
                "current_version_id": p.current_version_id,
                "versions": versions.get(p.id, 0),
                "template": docs[p.requirement_code].template
                if p.requirement_code in docs
                else None,
            }
            for p in pieces
        ],
        "deadlines": [
            deadline_item(
                d, dossier, definition, calendar, today=today, operation_names=operation_names
            )
            for d in deadlines
        ],
        "thread": thread,
        "send_preview": {
            "reference": reference,
            "subjects": {
                language: f"{reference} {label}" + (f" — {operation}" if operation else "")
                for language, label in definition.labels.items()
            },
            "recipient": definition.recipient,
            "grd_ref": dossier.grd_ref,
            "channel": definition.channel,
        },
    }
