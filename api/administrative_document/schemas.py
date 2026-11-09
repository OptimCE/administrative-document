"""Request/response models for the administrative-document routes outside
``/admin`` (``api/admin/schemas.py`` has the procedures').

Wire conventions match the other OptimCE annexes: snake_case fields, enums sent
as their integer values, dates as ISO-8601.
"""

import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from shared.const import Region, RenderState

# --- sharing operations (read-only mirror of the CRM) ------------------------


class SharingOperationOut(BaseModel):
    """A sharing operation of the community. Sourced from the CRM."""

    id: int
    name: str


# --- generation ---------------------------------------------------


class PrefillWarningOut(BaseModel):
    """One thing the reviewer should fix in the CRM before filing.

    Machine-readable on purpose: the client renders the sentence from ``code``
    and turns ``subject_type``/``subject_id`` into a link to the record that is
    wrong, so the correction lands in the CRM instead of being typed over in the
    dialog and frozen into the snapshot.
    """

    code: str
    subject_type: Literal["meter", "member", "community", "key", "sharing_operation"]
    #: EAN, member/key/operation id, or None for a community-level field. Always a
    #: string — an 18-digit EAN must never round-trip through an int.
    subject_id: str | None = None
    #: Interpolation values, notably `field` for the snapshot key concerned.
    params: dict[str, str] = Field(default_factory=dict)
    #: The render is refused (422, code 2366) until this is fixed in the CRM.
    blocking: bool = False


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
    upgrade_template: bool = Field(
        default=False,
        description=(
            "Render with the form in force today rather than the one the piece was "
            "pinned to when its procedure was opened"
        ),
    )


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
    #: ProcedureDocumentStatus: the piece's status, READY once a render landed.
    status: int


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


# --------------------------------------------------------------------------- #
# "What has been filed about me" — the member-facing read
#
# Never a version's `data_snapshot_json`: it names every participant of the
# community, and a member-facing DTO that carried it would leak the rest of the
# community in the network response even if the UI only rendered one row
# (decision B4).
# --------------------------------------------------------------------------- #


class MyFilingDossierOut(BaseModel):
    """Document-level context. Safe to show: it identifies the filing, not people."""

    id: int
    #: ProcedureStatus, the engine's common status.
    status: int
    family: str
    #: The regulator plugin's step code.
    step: str
    title: str | None = None
    external_ref: str | None = None
    submitted_at: datetime.datetime | None = None


class MyFilingDocumentOut(BaseModel):
    id: int
    doc_type: str
    #: ProcedureDocumentStatus.
    status: int
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
