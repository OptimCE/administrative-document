"""Owned administrative-document tables (LOCAL database).

Mirrors scripts/sql/schema.sql, which is the single source of truth for DDL
(constraints, partial indexes, triggers). When changing a model, update the SQL
file and add a migration under scripts/sql/migrations/.

Multi-tenancy: tenant tables carry a denormalised ``id_community`` int so the
``with_community_scope`` guard can filter without a cross-DB join. References
into the CRM core (``id_sharing_operation``) are plain columns — never foreign
keys — because the CRM lives in a separate database.

Three tables are APPEND-ONLY and have no ``updated_at``: ``StatusEventModel`` (the
journal), ``DocumentVersionModel`` and ``CorrespondenceModel`` (the evidentiary
record). Database triggers reject UPDATE/DELETE on all three; never add a
mutating code path for them.

``status`` (and a dossier's ``step``) is written by the database when a journal
row is appended: never assign it on a loaded model, refresh it after the flush.
"""

import datetime
import uuid
from typing import Any

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from core.database.database import LocalBase


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


class _TimestampMixin:
    """created_at + updated_at for mutable tables (updated_at bumped by trigger)."""

    created_at: Mapped[datetime.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )


# ---------------------------------------------------------------------------
# Reference data (region-keyed). ``id_community`` is nullable here and ONLY
# here: NULL = platform default for the region, non-null = community override.
# ---------------------------------------------------------------------------


class DocumentTemplateModel(_TimestampMixin, LocalBase):
    """A regulatory form, versioned as data.

    A revision of an official form is a new row (new ``version``), not a code
    change. ``valid_to IS NULL`` marks the one currently in force.
    """

    __tablename__ = "document_template"
    __table_args__ = (
        Index(
            "uq_document_template_version",
            "id_community",
            "region",
            "doc_type",
            "version",
            unique=True,
            postgresql_nulls_not_distinct=True,
        ),
        Index(
            "uq_document_template_current",
            "id_community",
            "region",
            "doc_type",
            unique=True,
            postgresql_nulls_not_distinct=True,
            postgresql_where=text("valid_to IS NULL"),
        ),
        Index("ix_document_template_lookup", "region", "doc_type"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    id_community: Mapped[int | None] = mapped_column(Integer, nullable=True)
    region: Mapped[int] = mapped_column(Integer, nullable=False)  # Region
    doc_type: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    valid_from: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    valid_to: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    file_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    mapping_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    output_format: Mapped[str] = mapped_column(String(16), nullable=False, default="pdf")
    label: Mapped[str | None] = mapped_column(String(255), nullable=True)


class ProcedureDefinitionModel(_TimestampMixin, LocalBase):
    """One versioned entry of a regulator's catalogue (a type of procedure).

    A projection of the plugin's YAML, written by the catalogue sync and never
    by hand. ``content_sha256`` NULL marks a row reserved by a data migration
    and not yet filled by the sync.
    """

    __tablename__ = "procedure_definition"
    __table_args__ = (
        UniqueConstraint("regulator", "code", "version", name="uq_procedure_definition_version"),
        Index(
            "uq_procedure_definition_current",
            "regulator",
            "code",
            unique=True,
            postgresql_where=text("valid_to IS NULL"),
        ),
        CheckConstraint(
            "valid_to IS NULL OR valid_from <= valid_to", name="ck_procedure_definition_validity"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    regulator: Mapped[str] = mapped_column(String(32), nullable=False)
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    valid_from: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    valid_to: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    scope: Mapped[int] = mapped_column(SmallInteger, nullable=False)  # ProcedureScope
    recipient: Mapped[int] = mapped_column(SmallInteger, nullable=False)  # Recipient
    channel: Mapped[int] = mapped_column(SmallInteger, nullable=False)  # Channel
    family: Mapped[str] = mapped_column(String(64), nullable=False)
    handles_change_kinds: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, default=list, server_default=text("'{}'")
    )
    escalation_rules_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    required_documents_json: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    transitions_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    response_types_json: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    labels_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    content_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    synced_at: Mapped[datetime.datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )


class DeadlineRuleModel(_TimestampMixin, LocalBase):
    """A regulatory clock, as data: a rule of one procedure definition, named
    by ``code``, written by the catalogue sync.

    The July columns (``region`` … ``description``) outlive the July code: on a
    fresh install the released reference data inserts July rules into them,
    which migrations 0003 and 0004 delete. Nothing reads them; the sync only
    writes their defaults.
    """

    __tablename__ = "deadline_rule"
    __table_args__ = (
        Index(
            "uq_deadline_rule",
            "id_community",
            "region",
            "dossier_type",
            "trigger_event",
            "deadline_type",
            unique=True,
            postgresql_nulls_not_distinct=True,
            postgresql_where=text("id_procedure_definition IS NULL"),
        ),
        Index("ix_deadline_rule_lookup", "region", "dossier_type", "trigger_event"),
        Index(
            "uq_deadline_rule_code",
            "id_procedure_definition",
            "code",
            unique=True,
            postgresql_where=text("id_procedure_definition IS NOT NULL"),
        ),
        CheckConstraint("NOT recurring OR recur_months IS NOT NULL", name="ck_deadline_rule_recur"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    id_community: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # July columns: NULL on every catalogue row.
    region: Mapped[int | None] = mapped_column(Integer, nullable=True)
    dossier_type: Mapped[int | None] = mapped_column(Integer, nullable=True)
    trigger_event: Mapped[str | None] = mapped_column(String(64), nullable=True)
    deadline_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    offset_value: Mapped[int | None] = mapped_column(Integer, nullable=True)
    offset_unit: Mapped[int | None] = mapped_column(Integer, nullable=True)  # OffsetUnit
    recurring: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    recur_months: Mapped[int | None] = mapped_column(Integer, nullable=True)
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)
    id_procedure_definition: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("procedure_definition.id", ondelete="CASCADE"), nullable=True
    )
    code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    kind: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)  # RuleKind
    anchor_event: Mapped[str | None] = mapped_column(String(64), nullable=True)
    party: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)  # DeadlineParty
    deadline_kind: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)  # DeadlineKind
    annual_month: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    annual_day: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)


# ---------------------------------------------------------------------------
# Tenant data
# ---------------------------------------------------------------------------


class DossierModel(_TimestampMixin, LocalBase):
    """A procedure — one sending of one catalogue type to one recipient.

    An operation-scope procedure concerns exactly one sharing operation; a
    community-scope one concerns none (``id_sharing_operation`` NULL).

    ``status`` (a ``ProcedureStatus``) and ``step`` (the plugin's) are a copy of
    the ``status_event`` journal head, written by the database when a journal row
    is appended. Never assign them: append the event, flush, refresh.

    Every row is a procedure of a catalogue (migration 0004 made the procedure
    columns NOT NULL). The July ``dossier_type`` column survives in the table,
    unmapped: migration 0003 reads it on a fresh install.
    """

    __tablename__ = "dossier"
    __table_args__ = (
        Index("ix_dossier_status", "id_community", "status"),
        Index("ix_dossier_operation", "id_community", "id_sharing_operation"),
        Index(
            "uq_dossier_external_ref_family",
            "id_community",
            "family",
            "external_ref",
            unique=True,
            postgresql_where=text("external_ref IS NOT NULL AND family IS NOT NULL"),
        ),
        # One open draft per operation (or community), family and period.
        Index(
            "uq_dossier_open_draft",
            "id_community",
            "id_sharing_operation",
            "family",
            "period_key",
            unique=True,
            postgresql_nulls_not_distinct=True,
            postgresql_where=text("status IN (1, 6) AND family IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    id_community: Mapped[int] = mapped_column(Integer, nullable=False)
    # Cross-DB reference to the CRM sharing operation — plain column, no FK.
    # Validated against the CRM (scoped to the community) when the dossier is
    # created. NULL for a community-scope procedure.
    id_sharing_operation: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[int] = mapped_column(Integer, nullable=False)  # ProcedureStatus
    step: Mapped[str] = mapped_column(String(64), nullable=False)
    region: Mapped[int] = mapped_column(Integer, nullable=False)  # Region
    external_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    submitted_at: Mapped[datetime.datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    id_procedure_definition: Mapped[int] = mapped_column(
        Integer, ForeignKey("procedure_definition.id"), nullable=False
    )
    regulator: Mapped[str] = mapped_column(String(32), nullable=False)
    family: Mapped[str] = mapped_column(String(64), nullable=False)
    scope: Mapped[int] = mapped_column(SmallInteger, nullable=False)  # ProcedureScope
    grd_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    variant: Mapped[str | None] = mapped_column(String(32), nullable=True)
    requested_effective_date: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    effective_date: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    opened_by: Mapped[int] = mapped_column(SmallInteger, nullable=False)  # OpenedBy
    frozen_at: Mapped[datetime.datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    period_key: Mapped[str | None] = mapped_column(String(16), nullable=True)
    next_action_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    next_action_due: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    effect_error_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)


class DocumentModel(_TimestampMixin, LocalBase):
    """One piece of a procedure: generated, collected or received.

    ``status`` (a ``ProcedureDocumentStatus``) is a copy of the journal head
    (see DossierModel). ``requirement_code`` is the plugin's piece code; for
    CWaPE it equals the July ``doc_type``.
    """

    __tablename__ = "document"
    __table_args__ = (
        Index("ix_document_dossier", "id_community", "id_dossier"),
        Index("ix_document_status", "id_community", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    id_community: Mapped[int] = mapped_column(Integer, nullable=False)
    id_dossier: Mapped[int] = mapped_column(
        Integer, ForeignKey("dossier.id", ondelete="CASCADE"), nullable=False
    )
    doc_type: Mapped[str] = mapped_column(String(64), nullable=False)
    origin: Mapped[int] = mapped_column(Integer, nullable=False)  # DocOrigin
    status: Mapped[int] = mapped_column(Integer, nullable=False)  # ProcedureDocumentStatus
    current_version_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("document_version.id"), nullable=True
    )
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    kind: Mapped[int] = mapped_column(SmallInteger, nullable=False)  # DocumentKind
    required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("TRUE")
    )
    requirement_code: Mapped[str] = mapped_column(String(64), nullable=False)
    condition_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # Cross-DB reference to the participant who supplies or signs the piece.
    id_member: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # The template the piece renders with, pinned at birth (or first render):
    # the procedure keeps its creation's form version unless a render upgrades.
    id_template: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("document_template.id"), nullable=True
    )


class DocumentVersionModel(LocalBase):
    """An immutable version of a document. APPEND-ONLY.

    No ``updated_at`` and no update path: a database trigger raises on UPDATE and
    DELETE. ``file_ref`` points at a content-addressed object key, so the bytes
    behind a version can never be swapped either.
    """

    __tablename__ = "document_version"
    __table_args__ = (
        UniqueConstraint("id_document", "version_no", name="uq_document_version_no"),
        Index("ix_document_version_doc", "id_community", "id_document"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    id_community: Mapped[int] = mapped_column(Integer, nullable=False)
    id_document: Mapped[int] = mapped_column(
        Integer, ForeignKey("document.id", ondelete="CASCADE"), nullable=False
    )
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    file_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    content_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    content_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    byte_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    original_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Set only for generated versions (Phase 2).
    id_template: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("document_template.id"), nullable=True
    )
    data_snapshot_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    generated_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Which render produced this version. Append-only, so it survives the next
    # regeneration reusing DocumentRenderModel.docgen_request_id — that is what
    # lets the result handler distinguish an already-handled redelivery from a
    # stale result rather than guessing.
    docgen_request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    stage: Mapped[int] = mapped_column(SmallInteger, nullable=False)  # VersionStage
    # The stamp docgen applied; drafts only (a CHECK ties it to the stage).
    watermark: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # The CRM's pre-fill before the reviewer's edits (see schema.sql).
    prefill_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    template_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    renderer_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )
    immutable_from: Mapped[datetime.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )


class DocumentRenderModel(_TimestampMixin, LocalBase):
    """The in-flight state of one generation. Exists only while pending or failed.

    A successful render deletes this row: the resulting ``DocumentVersionModel``
    is the record. ``data_snapshot_json`` is captured at *request* time because
    the docgen result echoes only ``metadata``, never ``data`` — re-reading the
    CRM when the result lands would defeat the purpose of a snapshot.

    ``UNIQUE (id_document)`` is what enforces one render in flight per document,
    via ``INSERT … ON CONFLICT DO NOTHING`` (no SELECT-then-UPDATE race).
    """

    __tablename__ = "document_render"
    __table_args__ = (
        UniqueConstraint("id_document", name="document_render_id_document_key"),
        # NOT led by id_community, unlike every other index in this module: the
        # result handler holds a request_id and does not yet know the tenant.
        Index("uq_document_render_request", "docgen_request_id", unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    id_community: Mapped[int] = mapped_column(Integer, nullable=False)
    id_document: Mapped[int] = mapped_column(
        Integer, ForeignKey("document.id", ondelete="CASCADE"), nullable=False
    )
    docgen_request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    render_state: Mapped[int] = mapped_column(Integer, nullable=False)  # RenderState
    render_error_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    id_template: Mapped[int] = mapped_column(
        Integer, ForeignKey("document_template.id"), nullable=False
    )
    data_snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    requested_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    requested_at: Mapped[datetime.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )
    purpose: Mapped[int] = mapped_column(SmallInteger, nullable=False)  # RenderPurpose
    stage: Mapped[int] = mapped_column(SmallInteger, nullable=False)  # VersionStage
    watermark: Mapped[str | None] = mapped_column(String(40), nullable=True)
    prefill_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # The version a VERIFY render regenerates; NULL for a RENDER.
    id_document_version: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("document_version.id"), nullable=True
    )


class VersionCheckModel(LocalBase):
    """One regeneration of a version from its own snapshot. APPEND-ONLY.

    ``outcome`` (CheckOutcome) says whether docgen produced the same bytes; a
    different sha256 comes with the renderer that produced it, which is what
    tells a reproducibility bug from an upgraded library.
    """

    __tablename__ = "version_check"
    __table_args__ = (
        UniqueConstraint("docgen_request_id", name="uq_version_check_request"),
        Index("ix_version_check_version", "id_community", "id_document_version", "id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    id_community: Mapped[int] = mapped_column(Integer, nullable=False)
    id_document_version: Mapped[int] = mapped_column(
        Integer, ForeignKey("document_version.id"), nullable=False
    )
    docgen_request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    outcome: Mapped[int] = mapped_column(SmallInteger, nullable=False)  # CheckOutcome
    content_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    renderer_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    error_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    requested_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    requested_at: Mapped[datetime.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    checked_at: Mapped[datetime.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )


class StatusEventModel(LocalBase):
    """The immutable status journal — the source of truth for every transition.

    APPEND-ONLY (a trigger raises on UPDATE/DELETE). ``subject_id`` is
    polymorphic over document/dossier and deliberately carries no foreign key:
    the audit trail must outlive the row it describes.
    """

    __tablename__ = "status_event"
    __table_args__ = (
        Index(
            "ix_status_event_subject",
            "id_community",
            "subject_type",
            "subject_id",
            text("id DESC"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    id_community: Mapped[int] = mapped_column(Integer, nullable=False)
    subject_type: Mapped[int] = mapped_column(Integer, nullable=False)  # SubjectType
    subject_id: Mapped[int] = mapped_column(Integer, nullable=False)
    from_status: Mapped[int | None] = mapped_column(Integer, nullable=True)  # NULL on birth
    to_status: Mapped[int] = mapped_column(Integer, nullable=False)
    # The plugin's step, journaled with the common status. Dossiers only.
    from_step: Mapped[str | None] = mapped_column(String(64), nullable=True)
    to_step: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_corrective: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    actor_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    occurred_at: Mapped[datetime.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )
    context_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )


class DeadlineModel(_TimestampMixin, LocalBase):
    """A clock of a procedure, started by a catalogue rule (``id_deadline_rule``)
    when its anchor event happened; ``deadline_type`` is the rule's code."""

    __tablename__ = "deadline"
    __table_args__ = (
        Index("ix_deadline_dashboard", "id_community", "status", "due_date"),
        Index("ix_deadline_dossier", "id_dossier"),
        # Idempotency: re-processing the same event cannot duplicate a deadline.
        Index(
            "uq_deadline_event",
            "id_dossier",
            "deadline_type",
            "derived_from_event_id",
            unique=True,
            postgresql_where=text("derived_from_event_id IS NOT NULL"),
        ),
        # The reminder sweep: open, un-reminded, due inside the window.
        Index(
            "ix_deadline_reminder",
            "id_community",
            "due_date",
            postgresql_where=text("status = 1 AND reminded_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    id_community: Mapped[int] = mapped_column(Integer, nullable=False)
    id_dossier: Mapped[int] = mapped_column(
        Integer, ForeignKey("dossier.id", ondelete="CASCADE"), nullable=False
    )
    deadline_type: Mapped[str] = mapped_column(String(64), nullable=False)  # rule code
    due_date: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    status: Mapped[int] = mapped_column(Integer, nullable=False, default=1)  # DeadlineStatus
    recurring: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # NULL for a rolled recurring occurrence (it has no originating transition).
    derived_from_event_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("status_event.id"), nullable=True
    )
    id_deadline_rule: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("deadline_rule.id"), nullable=True
    )
    resolved_at: Mapped[datetime.datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    # When the "due soon" reminder was emitted for THIS occurrence, and the
    # reason the reminder sweep is idempotent. `outbound_message.dedupe_key`
    # protects the email but nothing protects the in-app notification, so a daily
    # sweep would otherwise add a bell entry every day until the due date. A
    # rolled recurring occurrence is a fresh INSERT, so it starts NULL and earns
    # its own reminder.
    reminded_at: Mapped[datetime.datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    # The reminder ladder's rungs already told (14, 7, 2 days before). Assign a
    # new list: an in-place append is not tracked.
    reminders_sent: Mapped[list[int]] = mapped_column(
        ARRAY(SmallInteger), nullable=False, default=list, server_default=text("'{}'")
    )
    party: Mapped[int] = mapped_column(SmallInteger, nullable=False)  # DeadlineParty
    kind: Mapped[int] = mapped_column(SmallInteger, nullable=False)  # DeadlineKind
    derived_from_correspondence_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("correspondence.id"), nullable=True
    )


class CorrespondenceModel(LocalBase):
    """A message sent or received for a procedure, with its archived file.

    APPEND-ONLY, like the journal: a trigger raises on UPDATE and DELETE, so a
    correction is a new row. ``type`` is the plugin's code.
    """

    __tablename__ = "correspondence"
    __table_args__ = (
        Index("ix_correspondence_dossier", "id_community", "id_dossier", "occurred_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    id_community: Mapped[int] = mapped_column(Integer, nullable=False)
    id_dossier: Mapped[int] = mapped_column(Integer, ForeignKey("dossier.id"), nullable=False)
    direction: Mapped[int] = mapped_column(SmallInteger, nullable=False)  # CorrespondenceDirection
    counterparty: Mapped[int] = mapped_column(SmallInteger, nullable=False)  # Counterparty
    channel: Mapped[int] = mapped_column(SmallInteger, nullable=False)  # Channel
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    occurred_at: Mapped[datetime.datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    subject: Mapped[str | None] = mapped_column(String(255), nullable=True)
    body_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    external_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    file_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    content_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    content_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    byte_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    original_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    context_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    recorded_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )


class InboundAddressModel(LocalBase):
    """A community's reception address code (phase 4). One live per community."""

    __tablename__ = "inbound_address"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    id_community: Mapped[int] = mapped_column(Integer, nullable=False)
    token: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )
    created_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    revoked_at: Mapped[datetime.datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )


class InboundMailModel(LocalBase):
    """A mail the reception address received (phase 4). ``status`` is
    ``InboundMailStatus``, a working state; the file is the evidence."""

    __tablename__ = "inbound_mail"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    id_community: Mapped[int] = mapped_column(Integer, nullable=False)
    id_inbound_address: Mapped[int] = mapped_column(
        Integer, ForeignKey("inbound_address.id"), nullable=False
    )
    message_id: Mapped[str | None] = mapped_column(String(998), nullable=True)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    file_ref: Mapped[str] = mapped_column(Text, nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    from_address: Mapped[str | None] = mapped_column(String(320), nullable=True)
    subject: Mapped[str | None] = mapped_column(String(998), nullable=True)
    sent_at: Mapped[datetime.datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    body_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    received_at: Mapped[datetime.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )
    status: Mapped[int] = mapped_column(SmallInteger, nullable=False)  # InboundMailStatus
    id_dossier: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("dossier.id", ondelete="SET NULL"), nullable=True
    )
    match_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    suggested_response: Mapped[str | None] = mapped_column(String(64), nullable=True)
    id_correspondence: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("correspondence.id"), nullable=True
    )
    handled_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    handled_at: Mapped[datetime.datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )


class InboundMailAttachmentModel(LocalBase):
    """An attachment of a received mail. APPEND-ONLY (trigger)."""

    __tablename__ = "inbound_mail_attachment"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    id_inbound_mail: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("inbound_mail.id"), nullable=False
    )
    id_community: Mapped[int] = mapped_column(Integer, nullable=False)
    position: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(255), nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    file_ref: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )


# ---------------------------------------------------------------------------
# Procedures engine
# ---------------------------------------------------------------------------


class ChangeModel(_TimestampMixin, LocalBase):
    """A gap between the CRM and what the authorities know, born from one event.

    ``kind`` is the engine's vocabulary, the same for every regulator; one event
    yields one change per procedure the catalogue routes it to, and per
    operation for a community event feeding an operation procedure, hence
    ``UNIQUE (source_event_id, procedure_code, COALESCE(id_sharing_operation, 0))``.
    """

    __tablename__ = "change"
    __table_args__ = (
        Index(
            "uq_change_source_operation",
            "source_event_id",
            "procedure_code",
            text("COALESCE(id_sharing_operation, 0)"),
            unique=True,
        ),
        Index("ix_change_dossier", "id_community", "id_dossier"),
        Index(
            "ix_change_subject",
            "id_community",
            "id_sharing_operation",
            "subject_type",
            "subject_ref",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    id_community: Mapped[int] = mapped_column(Integer, nullable=False)
    id_sharing_operation: Mapped[int | None] = mapped_column(Integer, nullable=True)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    subject_type: Mapped[int] = mapped_column(SmallInteger, nullable=False)  # ChangeSubjectType
    subject_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    source_event_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    procedure_code: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[int] = mapped_column(SmallInteger, nullable=False)  # ChangeStatus
    id_dossier: Mapped[int | None] = mapped_column(Integer, ForeignKey("dossier.id"), nullable=True)
    detected_at: Mapped[datetime.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )


class ProcessedEventModel(LocalBase):
    """A core event the consumer handled — its idempotency key.

    Written for every event, including those acknowledged without creating
    anything; ``outcome`` says which (an ``EventOutcome``).
    """

    __tablename__ = "processed_event"
    __table_args__ = (Index("ix_processed_event_community", "id_community", "processed_at"),)

    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    id_community: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    outcome: Mapped[int] = mapped_column(SmallInteger, nullable=False)  # EventOutcome
    processed_at: Mapped[datetime.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )


class ModuleActivationModel(_TimestampMixin, LocalBase):
    """One period during which the module was active for a community.

    The switch itself is the CRM's ``community_subscription``; this row dates the
    start of the tracking and carries the baseline photographed at that start.
    """

    __tablename__ = "module_activation"
    __table_args__ = (
        Index(
            "uq_module_activation_open",
            "id_community",
            unique=True,
            postgresql_where=text("disabled_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    id_community: Mapped[int] = mapped_column(Integer, nullable=False)
    enabled_at: Mapped[datetime.datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    disabled_at: Mapped[datetime.datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    baseline_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    baseline_confirmed_at: Mapped[datetime.datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    confirmed_by: Mapped[str | None] = mapped_column(String(255), nullable=True)


class WorkingCalendarModel(LocalBase):
    """A public holiday of a region (``region`` is the plugin's string, 'WAL')."""

    __tablename__ = "working_calendar"

    region: Mapped[str] = mapped_column(String(8), primary_key=True)
    day: Mapped[datetime.date] = mapped_column(Date, primary_key=True)
    label: Mapped[str] = mapped_column(String(128), nullable=False)
