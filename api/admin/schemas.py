"""Request and response bodies of the ``/admin`` routes."""

from __future__ import annotations

import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class BaselineItemOut(BaseModel):
    """One meter or key of the starting state, and what was or may be said of it."""

    key: str = Field(description="Stable id of the item, to send back with its choice")
    subject_type: Literal["meter", "key"]
    subject_ref: str = Field(description="The EAN, or the key's id")
    id_sharing_operation: int
    operation_name: str | None = None
    change: Literal["present", "added", "removed"]
    choices: list[str]
    default_choice: str
    label: str | None = Field(default=None, description="A key's name")
    id_member: int | None = None
    grd: str | None = None
    injection_status: int | None = None
    start_date: datetime.date | None = None
    # Once confirmed: what the admin chose.
    chosen: str | None = None
    effective_date: datetime.date | None = None
    sent_date: datetime.date | None = None


class BaselineOut(BaseModel):
    status: Literal["not_supported", "pending", "confirmed"]
    regulator: str | None = None
    kind: Literal["first", "reactivation"] | None = None
    taken_at: datetime.datetime | None = None
    confirmed_at: datetime.datetime | None = None
    confirmed_by: str | None = None
    id_dossier: int | None = Field(default=None, description="The baseline procedure")
    items: list[BaselineItemOut] = Field(default_factory=list)


class BaselineChoiceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    choice: Literal["active", "not_notified", "in_progress", "ended"]
    effective_date: datetime.date | None = Field(
        default=None, description="Active: since when, if known. Ended: since when."
    )
    sent_date: datetime.date | None = Field(
        default=None, description="In progress: the day it was sent (required)"
    )


class BaselineConfirmIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    choices: list[BaselineChoiceIn] = Field(
        default_factory=list, description="An item left out takes its default choice"
    )


# ---------------------------------------------------------------------------
# Procedures
# ---------------------------------------------------------------------------

Labels = dict[str, str]  # fr, en, nl, de, from the regulator's catalogue


class NextActionOut(BaseModel):
    code: str = Field(
        description="complete_documents | send | record_response | confirm_baseline | none"
    )
    labels: Labels | None = None


class DeadlineItemOut(BaseModel):
    id: int
    id_dossier: int
    procedure_code: str
    procedure_labels: Labels
    code: str
    labels: Labels | None = None
    due_date: datetime.date
    party: int | None = Field(
        default=None, description="DeadlineParty: 1 community, 2 grd, 3 regulator"
    )
    kind: int | None = Field(
        default=None, description="DeadlineKind: 1 submission, 2 response, 3 lapse, 4 effect"
    )
    status: int = Field(description="DeadlineStatus: 1 open, 2 met, 3 missed, 4 cancelled")
    due_in_working_days: int | None = Field(
        default=None, description="Working days from today; negative once past. Counted server-side"
    )
    overdue: bool = False
    id_sharing_operation: int | None = None
    operation_name: str | None = None


class ProcedureItemOut(BaseModel):
    """One procedure as the dashboard lists it: what to do, by when."""

    id: int
    code: str
    version: int
    family: str
    purpose: str
    labels: Labels
    status: int = Field(description="ProcedureStatus, the engine's common status")
    step: str
    step_labels: Labels
    bucket: Literal["todo", "waiting", "planned", "done"]
    variant: str | None = None
    recipient: str
    id_sharing_operation: int | None = None
    operation_name: str | None = None
    grd_ref: str | None = None
    external_ref: str | None = None
    next_action: NextActionOut
    next_action_due: datetime.date | None = None
    due_in_working_days: int | None = None
    overdue: bool = False
    changes_count: int = 0
    pieces_ready: int = 0
    pieces_required: int = 0
    updated_at: datetime.datetime


class InboxGroupsOut(BaseModel):
    todo: list[ProcedureItemOut] = Field(default_factory=list)
    waiting: list[ProcedureItemOut] = Field(default_factory=list)
    planned: list[ProcedureItemOut] = Field(default_factory=list)
    done: list[ProcedureItemOut] = Field(default_factory=list)


class InboxOut(BaseModel):
    today: datetime.date
    banner: list[DeadlineItemOut] = Field(description="The three soonest open deadlines")
    groups: InboxGroupsOut


class StatusOut(BaseModel):
    active: bool
    regulator: str | None = None
    supported: bool = Field(description="False: the regulator has no plugin; nothing is tracked")
    tracking_since: datetime.datetime | None = None
    baseline: Literal["pending", "confirmed"] | None = None
    id_baseline_dossier: int | None = None


class StepOut(BaseModel):
    code: str
    status: int
    bucket: str
    labels: Labels
    current: bool = False
    visited: bool = False


class TransitionOptionOut(BaseModel):
    to_step: str
    labels: Labels
    automatic: bool


class ResponseTypeOut(BaseModel):
    code: str
    direction: Literal["inbound", "outbound"]
    counterparty: str
    to_step: str | None = None
    labels: Labels
    applicable_now: bool = Field(
        description="Recording it now is possible (its step change is allowed)"
    )


class ChangeOut(BaseModel):
    id: int
    kind: str
    subject_type: int
    subject_ref: str
    status: int = Field(description="ChangeStatus")
    detected_at: datetime.datetime
    id_member: int | None = None
    grd: str | None = None
    injection_status: int | None = None
    effective_date: str | None = None
    excluded_reason: str | None = None
    # What the core shows of the subject (its regulatory_link_status row).
    regulatory_status: int | None = Field(
        default=None, description="RegulatoryStatus of its episode in the core; None: none yet"
    )
    valid_from: datetime.date | None = None
    valid_to: datetime.date | None = Field(
        default=None, description="Its last day in the operation"
    )
    refiled_from: int | None = Field(default=None, description="The refused change it refiles")
    refiled_into: int | None = Field(
        default=None, description="The procedure this refused change was refiled into"
    )


class EffectErrorOut(BaseModel):
    """Why the core refused the procedure's effects, kept until they land."""

    kind: Literal["sync", "in_force"] = Field(
        description="sync: the badges; in_force: the effect date (retried each day)"
    )
    code: Literal["exclude", "unavailable"]
    subjects: list[str] = Field(
        default_factory=list, description="The EANs tracked in another operation"
    )
    at: datetime.datetime


class PieceVersionOut(BaseModel):
    """A piece's current version: what is downloaded, and what would be sent."""

    id: int
    version_no: int
    stage: int = Field(description="VersionStage: 1 draft (stamped), 2 final, 3 to sign")
    created_at: datetime.datetime
    content_sha256: str | None = None
    original_filename: str | None = None
    generated: bool = Field(description="Rendered by OptimCE (so it can be verified)")


class PieceRenderOut(BaseModel):
    """A render of the piece in flight, or failed (the slot it holds)."""

    state: int = Field(description="RenderState: 1 pending, 2 failed")
    purpose: int = Field(description="RenderPurpose: 1 render, 2 verify")
    stage: int = Field(description="VersionStage it renders")
    requested_at: datetime.datetime
    error: dict | None = None


class PieceCheckOut(BaseModel):
    """The last regeneration of the current version from its snapshot."""

    outcome: int = Field(description="CheckOutcome: 1 identical, 2 different, 3 failed")
    checked_at: datetime.datetime
    content_sha256: str | None = None


class PieceOut(BaseModel):
    id: int
    code: str
    labels: Labels | None = None
    kind: int | None = Field(
        default=None, description="DocumentKind: 1 generated, 2 collected, 3 received"
    )
    required: bool
    status: int = Field(description="ProcedureDocumentStatus")
    id_member: int | None = None
    member_name: str | None = Field(default=None, description="Who a per-member piece is for")
    condition: dict | None = None
    current_version_id: int | None = None
    versions: int = 0
    template: str | None = Field(
        default=None,
        description="The registered template the piece renders with (pinned, or in "
        "force); None when nothing can render it",
    )
    template_version: int | None = None
    template_superseded: bool = Field(
        default=False, description="A newer version of the form is in force"
    )
    current_version: PieceVersionOut | None = None
    render: PieceRenderOut | None = None
    last_check: PieceCheckOut | None = None


class ThreadEntryOut(BaseModel):
    at: datetime.datetime
    kind: Literal["step", "correspondence", "version"]
    actor: str | None = None
    # step
    from_step: str | None = None
    to_step: str | None = None
    reason: str | None = None
    # correspondence
    id_correspondence: int | None = None
    type: str | None = None
    direction: int | None = None
    subject: str | None = None
    has_file: bool = False
    # version
    id_document: int | None = None
    version_no: int | None = None


class SendPreviewOut(BaseModel):
    reference: str = Field(
        description="[OPT-<id>]: the tag that ties a reply back to this procedure"
    )
    subjects: Labels
    recipient: str = Field(description="grd_ref | regulator")
    grd_ref: str | None = None
    channel: str


class ProcedureDetailOut(ProcedureItemOut):
    channel: str
    scope: str
    opened_by: int | None = None
    requested_effective_date: datetime.date | None = None
    effective_date: datetime.date | None = None
    frozen_at: datetime.datetime | None = None
    effect_error: EffectErrorOut | None = None
    timeline: list[StepOut]
    transitions: list[TransitionOptionOut]
    response_types: list[ResponseTypeOut]
    changes: list[ChangeOut]
    pieces: list[PieceOut]
    deadlines: list[DeadlineItemOut]
    thread: list[ThreadEntryOut]
    send_preview: SendPreviewOut
    finals_ready: bool = Field(
        description="Every supplied piece is final and nothing renders: it can be sent"
    )
    has_entry_sheet: bool = Field(description="A piece has a fiche de saisie (GET .../entry-sheet)")


class EntryFieldOut(BaseModel):
    key: str
    labels: Labels
    value: Any = None


class EntrySectionOut(BaseModel):
    labels: Labels
    fields: list[EntryFieldOut]


class EntrySheetOut(BaseModel):
    """A form piece's values in the order of the regulator's form, from its
    current version (what was reviewed; once sent, what was filed)."""

    piece_id: int
    code: str
    labels: Labels
    version: PieceVersionOut | None = None
    sections: list[EntrySectionOut]


class ProcedureDefinitionOut(BaseModel):
    """One version of one type of procedure, as the plugin's catalogue says."""

    code: str
    version: int
    valid_from: datetime.date
    valid_to: datetime.date | None = None
    in_force: bool
    purpose: str
    scope: str
    recipient: str
    channel: str
    family: str
    labels: Labels
    variants: list[str] = Field(default_factory=list)
    default_variant: str | None = None
    initial_step: str
    steps: list[dict] = Field(default_factory=list)
    transitions: list[dict] = Field(default_factory=list)
    response_types: list[dict] = Field(default_factory=list)
    deadline_rules: list[dict] = Field(default_factory=list)
    documents: list[dict] = Field(default_factory=list)


class ProcedureCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(description="A type of the catalogue in force, e.g. PARTAGE_FIN")
    id_sharing_operation: int | None = Field(
        default=None, description="Required for an operation-scope type"
    )
    requested_effective_date: datetime.date | None = None


class ChangeExcludeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=500)


class TransitionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    to_step: str
    reason: str = Field(min_length=1, max_length=500)
    on: datetime.date | None = Field(
        default=None, description="The day it happened; today if omitted"
    )


class ProposalDeadlineOut(BaseModel):
    code: str
    labels: Labels | None = None
    due_date: datetime.date
    party: int
    kind: int


class EffectPreviewOut(BaseModel):
    """What the core would show of a subject once the response is recorded."""

    subject_type: int
    subject_ref: str
    regulatory_status: int = Field(description="RegulatoryStatus")
    valid_from: datetime.date | None = None
    valid_to: datetime.date | None = None


class CorrespondenceProposalOut(BaseModel):
    """What recording this response would do, before it is recorded."""

    type: str
    labels: Labels
    direction: Literal["inbound", "outbound"]
    from_step: str
    to_step: str | None = None
    to_step_labels: Labels | None = None
    to_status: int | None = Field(
        default=None, description="ProcedureStatus of the step it moves to"
    )
    transition_allowed: bool
    deadlines_started: list[ProposalDeadlineOut]
    deadlines_met: list[str]
    effective_date: datetime.date | None = Field(
        default=None, description="The effect date it would set (the 20th working day…)"
    )
    effects: list[EffectPreviewOut] = Field(
        default_factory=list, description="What the CRM would show differently"
    )


class FinalizeOut(BaseModel):
    """The finals asked of docgen (piece ids); a piece with a render in flight
    is left for the client to retry. Each landing is signalled in realtime
    (``admin.document.ready``)."""

    requested: list[int]
    in_flight: list[int]


class RegenerateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_ids: list[int] | None = Field(
        default=None, description="The pieces to regenerate; every stale one if omitted"
    )


class RegenerateOut(BaseModel):
    """``needs_review``: the current version cannot say what the reviewer typed
    (an upload, an older render), so the review form is the way."""

    requested: list[int]
    needs_review: list[int]
    in_flight: list[int]


class VerifyAccepted(BaseModel):
    """The regeneration is asked of docgen; its outcome lands on the version
    (``last_check`` in the procedure's detail) and is signalled in realtime."""

    docgen_request_id: str
