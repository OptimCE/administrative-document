"""Request/response models for the administrative-document API.

Wire conventions match the other OptimCE annexes: snake_case fields, enums sent
as their integer values, dates as ISO-8601. Statuses are never writable
directly — they change only through a transition endpoint, which is what keeps
the journal authoritative.
"""

import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from shared.const import (
    AcknowledgementResult,
    DeadlineStatus,
    DocOrigin,
    DocumentStatus,
    DossierStatus,
    DossierType,
    OffsetUnit,
    Region,
    RenderState,
)

# --- sharing operations (read-only mirror of the CRM) ------------------------


class SharingOperationOut(BaseModel):
    """A sharing operation a dossier can be filed for. Sourced from the CRM."""

    id: int
    name: str


# --- dossiers ---------------------------------------------------------------


class DossierCreate(BaseModel):
    dossier_type: DossierType
    id_sharing_operation: int = Field(
        description=(
            "The CRM sharing operation this dossier is filed for. Required: a "
            "community running several operations files one dossier per operation."
        )
    )
    title: str | None = Field(default=None, max_length=255)
    external_ref: str | None = Field(
        default=None, max_length=128, description="CWaPE / DSO file reference, once known."
    )
    metadata: dict[str, Any] = Field(default_factory=dict)


class DossierUpdate(BaseModel):
    """Only the free-form fields.

    Status changes go through /transition, and the sharing operation is fixed at
    creation — re-pointing a filed dossier at a different operation would
    invalidate everything already recorded against it.
    """

    title: str | None = Field(default=None, max_length=255)
    external_ref: str | None = Field(default=None, max_length=128)
    metadata: dict[str, Any] | None = None


class DossierOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    dossier_type: DossierType
    status: DossierStatus
    region: Region
    id_sharing_operation: int
    title: str | None = None
    external_ref: str | None = None
    submitted_at: datetime.datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime.datetime
    updated_at: datetime.datetime


# --- documents --------------------------------------------------------------


class DocumentCreate(BaseModel):
    doc_type: str = Field(max_length=64)
    title: str | None = Field(default=None, max_length=255)


class DocumentVersionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    version_no: int
    content_sha256: str | None = None
    content_type: str | None = None
    byte_size: int | None = None
    original_filename: str | None = None
    id_template: int | None = None
    generated_by: str | None = None
    #: The frozen render input. NULL for uploaded versions; for generated ones it
    #: is exactly what was sent to the renderer (spec R2).
    data_snapshot_json: dict[str, Any] | None = None
    docgen_request_id: str | None = None
    created_at: datetime.datetime


class DocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    id_dossier: int
    doc_type: str
    origin: DocOrigin
    status: DocumentStatus
    title: str | None = None
    current_version_id: int | None = None
    version_count: int = 0
    created_at: datetime.datetime
    updated_at: datetime.datetime


class DocumentDetailOut(DocumentOut):
    versions: list[DocumentVersionOut] = Field(default_factory=list)


# --- generation (Phase 2) ---------------------------------------------------


class PrefillWarningOut(BaseModel):
    """One thing the reviewer should fix in the CRM before filing.

    Machine-readable on purpose: the client renders the sentence from ``code``
    and turns ``subject_type``/``subject_id`` into a link to the record that is
    wrong, so the correction lands in the CRM instead of being typed over in the
    dialog and frozen into the snapshot.
    """

    code: str
    subject_type: Literal["meter", "member", "community"]
    #: EAN, member id, or None for a community-level field. Always a string — an
    #: 18-digit EAN must never round-trip through an int.
    subject_id: str | None = None
    #: Interpolation values, notably `field` for the snapshot key concerned.
    params: dict[str, str] = Field(default_factory=dict)


class PrefillOut(BaseModel):
    """What the review form renders, before anything is persisted."""

    data: dict[str, Any]
    #: Things the reviewer should fix before filing — e.g. a delivery point with
    #: no member attribution, which cannot be listed under a name.
    warnings: list[PrefillWarningOut] = Field(default_factory=list)


class GenerateRequest(BaseModel):
    """The reviewed payload.

    ``data`` is what the user actually corrected; it is merged over the
    CRM-derived snapshot, and a list REPLACES the derived list rather than being
    merged row-by-row (a deleted participant must stay deleted).
    """

    data: dict[str, Any] = Field(default_factory=dict)


class GenerateAccepted(BaseModel):
    document_id: int
    docgen_request_id: str
    render_state: RenderState


class RenderStatusOut(BaseModel):
    """Uncached poll target. ``render_state`` is null when nothing is in flight."""

    document_id: int
    render_state: RenderState | None = None
    render_error: dict[str, Any] | None = None
    requested_at: datetime.datetime | None = None
    current_version_id: int | None = None
    status: DocumentStatus


# --- transitions ------------------------------------------------------------


class TransitionRequest(BaseModel):
    """Generic transition. ``context`` carries whatever the target status requires."""

    to_status: int
    context: dict[str, Any] = Field(default_factory=dict)


class MarkSentRequest(BaseModel):
    submission_date: datetime.date = Field(
        description="The date the document was actually transmitted; drives derived deadlines."
    )
    note: str | None = None


class AcknowledgeRequest(BaseModel):
    acknowledged_date: datetime.date
    authority_file_ref: str = Field(
        max_length=128, description="The CWaPE / DSO reference for the acknowledged filing."
    )
    result: AcknowledgementResult = AcknowledgementResult.COMPLETE
    note: str | None = None


class RollbackRequest(BaseModel):
    """A traced corrective transition. History is appended to, never rewritten.

    ``reason`` is mandatory for every corrective edge, but some of them also
    carry the target status' own requirement — rolling a document back *to* SENT
    still needs the submission date it is being restored to. ``context`` carries
    those, exactly as it does on the generic transition endpoint.
    """

    to_status: int
    reason: str = Field(min_length=1, max_length=512)
    context: dict[str, Any] = Field(default_factory=dict)


class StatusEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    subject_type: int
    subject_id: int
    from_status: int | None = None
    to_status: int
    is_corrective: bool
    actor_id: str | None = None
    occurred_at: datetime.datetime
    context: dict[str, Any] = Field(default_factory=dict)


# --- deadlines --------------------------------------------------------------


class DeadlineOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    id_dossier: int
    deadline_type: str
    due_date: datetime.date
    status: DeadlineStatus
    recurring: bool
    derived_from_event_id: int | None = None
    id_deadline_rule: int | None = None
    resolved_at: datetime.datetime | None = None
    created_at: datetime.datetime


class DeadlineResolveRequest(BaseModel):
    """Resolve an open deadline. Only met/cancelled are caller-settable —
    ``missed`` is derived by the sweep, never asserted by a user."""

    status: DeadlineStatus = Field(description="2 = met, 4 = cancelled")


class DeadlineSweepOut(BaseModel):
    missed: int
    rolled: int


# --- dossier detail (composed) ----------------------------------------------


class DossierDetailOut(DossierOut):
    documents: list[DocumentOut] = Field(default_factory=list)
    deadlines: list[DeadlineOut] = Field(default_factory=list)


# --- registries -------------------------------------------------------------


class TemplateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    id_community: int | None = None
    region: Region
    doc_type: str
    version: int
    valid_from: datetime.date
    valid_to: datetime.date | None = None
    file_ref: str | None = None
    output_format: str
    label: str | None = None


class TemplateCreate(BaseModel):
    region: Region
    doc_type: str = Field(max_length=64)
    version: int = Field(ge=1)
    valid_from: datetime.date
    valid_to: datetime.date | None = None
    file_ref: str | None = Field(default=None, max_length=512)
    mapping: dict[str, Any] | None = None
    output_format: str = Field(default="pdf", max_length=16)
    label: str | None = Field(default=None, max_length=255)


class TemplateUpdate(BaseModel):
    valid_to: datetime.date | None = None
    file_ref: str | None = Field(default=None, max_length=512)
    mapping: dict[str, Any] | None = None
    label: str | None = Field(default=None, max_length=255)


class DeadlineRuleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    id_community: int | None = None
    region: Region
    dossier_type: DossierType
    trigger_event: str
    deadline_type: str
    offset_value: int
    offset_unit: OffsetUnit
    recurring: bool
    recur_months: int | None = None
    description: str | None = None


class DeadlineRuleCreate(BaseModel):
    region: Region
    dossier_type: DossierType
    trigger_event: str = Field(max_length=64)
    deadline_type: str = Field(max_length=64)
    offset_value: int = Field(ge=0)
    offset_unit: OffsetUnit
    recurring: bool = False
    recur_months: int | None = Field(default=None, ge=1)
    description: str | None = Field(default=None, max_length=255)


class DeadlineRuleUpdate(BaseModel):
    offset_value: int | None = Field(default=None, ge=0)
    offset_unit: OffsetUnit | None = None
    recurring: bool | None = None
    recur_months: int | None = Field(default=None, ge=1)
    description: str | None = Field(default=None, max_length=255)


# --------------------------------------------------------------------------- #
# "What has been filed about me" — the member-facing read
#
# A separate schema tree from DocumentDetailOut on purpose. That one exposes the
# WHOLE `data_snapshot_json`, which names every participant of the community; a
# member-facing DTO that reused it would leak the rest of the community in the
# network response even if the UI only rendered one row (decision B4).
# --------------------------------------------------------------------------- #


class MyFilingDossierOut(BaseModel):
    """Document-level context. Safe to show: it identifies the filing, not people."""

    id: int
    dossier_type: DossierType
    status: DossierStatus
    title: str | None = None
    external_ref: str | None = None
    submitted_at: datetime.datetime | None = None


class MyFilingDocumentOut(BaseModel):
    id: int
    doc_type: str
    status: DocumentStatus
    title: str | None = None


class MyFilingVersionOut(BaseModel):
    """Which version this is, and when it was frozen — the "as of this date".

    Deliberately without `generated_by`: it is a Keycloak subject id, i.e. which
    manager pressed the button, which is none of a member's business.
    """

    id: int
    version_no: int
    created_at: datetime.datetime


class MyFilingRowsOut(BaseModel):
    """The caller's OWN rows, per snapshot block. Never anybody else's."""

    members: list[dict[str, Any]] = Field(default_factory=list)
    participants: list[dict[str, Any]] = Field(default_factory=list)
    installations: list[dict[str, Any]] = Field(default_factory=list)
    storage: list[dict[str, Any]] = Field(default_factory=list)


class MyFilingOut(BaseModel):
    dossier: MyFilingDossierOut
    document: MyFilingDocumentOut
    template_label: str | None = None
    version: MyFilingVersionOut
    my_rows: MyFilingRowsOut
