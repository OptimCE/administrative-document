"""Owned administrative-document tables (LOCAL database).

Mirrors scripts/sql/schema.sql, which is the single source of truth for DDL
(constraints, partial indexes, triggers). When changing a model, update the SQL
file and add a migration under scripts/sql/migrations/.

Multi-tenancy: tenant tables carry a denormalised ``id_community`` int so the
``with_community_scope`` guard can filter without a cross-DB join. References
into the CRM core (``id_sharing_operation``) are plain columns — never foreign
keys — because the CRM lives in a separate database.

Two tables are APPEND-ONLY and have no ``updated_at``: ``StatusEventModel`` (the
journal) and ``DocumentVersionModel`` (the evidentiary record). Database triggers
reject UPDATE/DELETE on both; never add a mutating code path for them.
"""

import datetime
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
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
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


class DeadlineRuleModel(_TimestampMixin, LocalBase):
    """A regulatory clock, as data.

    "When ``trigger_event`` happens on a ``dossier_type`` in ``region``, a
    ``deadline_type`` deadline falls due ``offset_value`` ``offset_unit`` later."
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
        ),
        Index("ix_deadline_rule_lookup", "region", "dossier_type", "trigger_event"),
        CheckConstraint("NOT recurring OR recur_months IS NOT NULL", name="ck_deadline_rule_recur"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    id_community: Mapped[int | None] = mapped_column(Integer, nullable=True)
    region: Mapped[int] = mapped_column(Integer, nullable=False)  # Region
    dossier_type: Mapped[int] = mapped_column(Integer, nullable=False)  # DossierType
    trigger_event: Mapped[str] = mapped_column(String(64), nullable=False)  # TriggerEvent
    deadline_type: Mapped[str] = mapped_column(String(64), nullable=False)  # DeadlineType
    offset_value: Mapped[int] = mapped_column(Integer, nullable=False)
    offset_unit: Mapped[int] = mapped_column(Integer, nullable=False)  # OffsetUnit
    recurring: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    recur_months: Mapped[int | None] = mapped_column(Integer, nullable=True)
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)


# ---------------------------------------------------------------------------
# Tenant data
# ---------------------------------------------------------------------------


class DossierModel(_TimestampMixin, LocalBase):
    """An administrative dossier — a submission to the CWaPE or a DSO.

    A dossier always concerns exactly one sharing operation; a community running
    several files one dossier per operation.

    ``status`` is a cache of the ``status_event`` journal head; a deferred
    constraint trigger rejects any value not backed by a journal row.
    """

    __tablename__ = "dossier"
    __table_args__ = (
        Index("ix_dossier_status", "id_community", "status"),
        Index("ix_dossier_type", "id_community", "dossier_type"),
        Index("ix_dossier_operation", "id_community", "id_sharing_operation"),
        Index(
            "uq_dossier_external_ref",
            "id_community",
            "dossier_type",
            "external_ref",
            unique=True,
            postgresql_where=text("external_ref IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    id_community: Mapped[int] = mapped_column(Integer, nullable=False)
    # Cross-DB reference to the CRM sharing operation — plain column, no FK.
    # Validated against the CRM (scoped to the community) when the dossier is created.
    id_sharing_operation: Mapped[int] = mapped_column(Integer, nullable=False)
    dossier_type: Mapped[int] = mapped_column(Integer, nullable=False)  # DossierType
    status: Mapped[int] = mapped_column(Integer, nullable=False)  # DossierStatus
    region: Mapped[int] = mapped_column(Integer, nullable=False)  # Region
    external_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    submitted_at: Mapped[datetime.datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )


class DocumentModel(_TimestampMixin, LocalBase):
    """One document inside a dossier, generated or uploaded.

    ``status`` is a cache of the journal head (see DossierModel).
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
    status: Mapped[int] = mapped_column(Integer, nullable=False)  # DocumentStatus
    current_version_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("document_version.id"), nullable=True
    )
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)


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
    is_corrective: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    actor_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    occurred_at: Mapped[datetime.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )
    context_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )


class DeadlineModel(_TimestampMixin, LocalBase):
    """A regulatory deadline derived from a status transition."""

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
        # A recurring obligation has exactly one OPEN occurrence at a time.
        Index(
            "uq_deadline_open_recurring",
            "id_dossier",
            "deadline_type",
            unique=True,
            postgresql_where=text("status = 1 AND recurring = TRUE"),
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
    deadline_type: Mapped[str] = mapped_column(String(64), nullable=False)  # DeadlineType
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
