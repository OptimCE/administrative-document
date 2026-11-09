"""What the ``/admin`` reads return, built from rows and the plugin's catalogue.

Labels come from the version of the catalogue a procedure was opened under,
never the one in force: a procedure keeps its definition. "Due in N working
days" is counted here, on the plugin's calendar, so every client shows the
same number (acceptance criterion 1.5).
"""

from __future__ import annotations

import datetime
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from engine.calendar import CalendarNotCoveredError, WorkingCalendar
from engine.catalogue import CatalogueEntry, ProcedureDefinition
from engine.lifecycle import UNRESOLVED
from engine.signatures import signature_out
from ports.crm_engine import Episode
from shared.const import (
    ChangeStatus,
    DeadlineKind,
    DeadlineStatus,
    DocumentKind,
    ProcedureDocumentStatus,
    ProcedureStatus,
    VersionStage,
)
from shared.models.local_models import (
    ChangeModel,
    CorrespondenceModel,
    DeadlineModel,
    DocumentModel,
    DocumentRenderModel,
    DocumentTemplateModel,
    DocumentVersionModel,
    DossierModel,
    PieceSignatureModel,
    StatusEventModel,
    VersionCheckModel,
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


@dataclass
class PieceFacts:
    """What a procedure's pieces stand on, beyond their own rows: the current
    versions, the renders holding a slot, the last check of each current
    version, the template each renders with (and whether a newer one is in
    force), and who a per-member piece is for."""

    current: dict[int, DocumentVersionModel] = field(default_factory=dict)  # by piece
    renders: dict[int, DocumentRenderModel] = field(default_factory=dict)  # by piece
    checks: dict[int, VersionCheckModel] = field(default_factory=dict)  # by version
    templates: dict[int, tuple[DocumentTemplateModel, bool]] = field(default_factory=dict)
    member_names: dict[int, str] = field(default_factory=dict)
    # The signature annexe (2026-11-03): each piece's latest request, and
    # whether a manager can ask for one now (module on, an account to sign with).
    signatures: dict[int, PieceSignatureModel] = field(default_factory=dict)
    signature_requestable: dict[int, bool] = field(default_factory=dict)

    def finals_ready(self, pieces: Sequence[DocumentModel]) -> bool:
        """What sending asks: every supplied piece final, nothing rendering."""
        if self.renders:
            return False
        return all(
            self.current[p.id].stage == int(VersionStage.FINAL)
            for p in pieces
            if p.kind != int(DocumentKind.RECEIVED)
            and p.status in _PIECE_THERE
            and p.id in self.current
        )


def version_out(version: DocumentVersionModel) -> dict[str, Any]:
    return {
        "id": version.id,
        "version_no": version.version_no,
        "stage": version.stage,
        "created_at": version.created_at,
        "content_sha256": version.content_sha256,
        "original_filename": version.original_filename,
        "generated": version.docgen_request_id is not None,
    }


def piece_out(
    piece: DocumentModel, definition: ProcedureDefinition, facts: PieceFacts, versions: int
) -> dict[str, Any]:
    docs = {d.code: d for d in definition.documents}
    template, superseded = facts.templates.get(piece.id, (None, False))
    current = facts.current.get(piece.id)
    render = facts.renders.get(piece.id)
    check = facts.checks.get(current.id) if current is not None else None
    return {
        "id": piece.id,
        "code": piece.requirement_code or piece.doc_type,
        "labels": docs[piece.requirement_code].labels if piece.requirement_code in docs else None,
        "kind": piece.kind,
        "required": piece.required,
        "status": piece.status,
        "id_member": piece.id_member,
        "member_name": facts.member_names.get(piece.id_member) if piece.id_member else None,
        "condition": piece.condition_json,
        "current_version_id": piece.current_version_id,
        "versions": versions,
        # What can render it, from the registry: a form registered without a
        # bundle renders nothing.
        "template": template.doc_type if template is not None and template.file_ref else None,
        "template_version": template.version if template is not None else None,
        "template_superseded": superseded,
        "current_version": version_out(current) if current is not None else None,
        "render": {
            "state": render.render_state,
            "purpose": render.purpose,
            "stage": render.stage,
            "requested_at": render.requested_at,
            "error": render.render_error_json,
        }
        if render is not None
        else None,
        "last_check": {
            "outcome": check.outcome,
            "checked_at": check.checked_at,
            "content_sha256": check.content_sha256,
        }
        if check is not None
        else None,
        "signature": signature_out(facts.signatures.get(piece.id)),
        "signature_requestable": facts.signature_requestable.get(piece.id, False),
    }


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
    is_open = deadline.status in UNRESOLVED
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


def _regulatory(episode: Episode | None) -> dict[str, Any]:
    """What the core shows of a change's subject: its episode's effect columns."""
    if episode is None:
        return {}
    return {
        "regulatory_status": episode.regulatory_status,
        "valid_from": episode.valid_from,
        "valid_to": episode.valid_to,
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
    facts: PieceFacts | None = None,
    has_entry_sheet: bool = False,
    episodes: Mapping[int, Episode] | None = None,
    refusal: Mapping[str, Any] | None = None,
    inbound: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    facts = facts or PieceFacts()
    episodes = episodes or {}
    present = (refusal or {}).get("present", {})
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
    for correction in (dossier.metadata_json or {}).get("effective_date_corrections") or ():
        thread.append(
            {
                "at": datetime.datetime.fromisoformat(correction["at"]),
                "kind": "effective_date",
                "actor": correction.get("actor"),
                "reason": correction.get("reason"),
                "effective_date": correction.get("to"),
                "previous_effective_date": correction.get("from"),
            }
        )
    for mail in inbound:
        # Received at the reception address, attached here, to qualify.
        thread.append(
            {
                "at": mail["received_at"],
                "kind": "inbound",
                "actor": None,
                "id_inbound_mail": mail["id"],
                "subject": mail["subject"],
            }
        )
    thread.sort(key=lambda entry: entry["at"])

    operation = operation_names.get(dossier.id_sharing_operation or 0)
    reference = f"[OPT-{dossier.id}]"
    return {
        **view,
        "inbound": list(inbound),
        "channel": definition.channel,
        "scope": definition.scope,
        "opened_by": dossier.opened_by,
        "requested_effective_date": dossier.requested_effective_date,
        "effective_date": dossier.effective_date,
        # Phase 4: an effect still to come may be corrected by hand.
        "effective_date_correctable": dossier.status != int(ProcedureStatus.IN_FORCE)
        and any(
            d.kind == int(DeadlineKind.EFFECT) and d.status == int(DeadlineStatus.OPEN)
            for d in deadlines
        ),
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
                **_regulatory(episodes.get(c.id)),
                "refiled_from": c.payload_json.get("refiled_from"),
                "refiled_into": (c.payload_json.get("refiled") or {}).get("into"),
                "in_operation": present.get(c.id),
            }
            for c in changes
        ],
        "pieces": [piece_out(p, definition, facts, versions.get(p.id, 0)) for p in pieces],
        "finals_ready": facts.finals_ready(pieces),
        "has_entry_sheet": has_entry_sheet,
        "refusal": (
            {k: refusal[k] for k in ("changes", "id_sharing_operation", "draft_id")}
            if refusal
            else None
        ),
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
