"""Audit log action codes.

Action codes follow the ``domain.entity.verb`` convention used by
``crm-backend`` (e.g. ``crm.allocation_key.created``). They are stored as
``VARCHAR(128)`` and the ``AuditAction`` type stays open-ended so call sites
can introduce new codes without round-tripping this module.
"""

from typing import Final

AuditAction = str


class AuditActions:
    """Known action codes emitted by ``administrative-document``.

    Every journaled state transition (spec R1/R6) is mirrored here in addition to
    the immutable ``status_event`` row, so the platform-wide audit trail in the
    CRM ``audit_log`` table stays the single place to answer "who changed what".
    """

    # ---- dossiers ----
    DOSSIER_CREATED: Final[AuditAction] = "administrative_document.dossier.created"
    DOSSIER_UPDATED: Final[AuditAction] = "administrative_document.dossier.updated"
    DOSSIER_STATUS_CHANGED: Final[AuditAction] = "administrative_document.dossier.status_changed"

    # ---- documents ----
    DOCUMENT_CREATED: Final[AuditAction] = "administrative_document.document.created"
    DOCUMENT_STATUS_CHANGED: Final[AuditAction] = "administrative_document.document.status_changed"
    DOCUMENT_VERSION_ADDED: Final[AuditAction] = "administrative_document.document_version.added"
    DOCUMENT_GENERATION_REQUESTED: Final[AuditAction] = (
        "administrative_document.document.generation_requested"
    )
    DOCUMENT_RENDERED: Final[AuditAction] = "administrative_document.document.rendered"
    DOCUMENT_RENDER_FAILED: Final[AuditAction] = "administrative_document.document.render_failed"

    # ---- deadlines ----
    DEADLINE_CREATED: Final[AuditAction] = "administrative_document.deadline.created"
    DEADLINE_RESOLVED: Final[AuditAction] = "administrative_document.deadline.resolved"
    DEADLINE_MISSED: Final[AuditAction] = "administrative_document.deadline.missed"
    DEADLINE_REMINDED: Final[AuditAction] = "administrative_document.deadline.reminded"

    # ---- registries (reference data edited as data, not deployed) ----
    TEMPLATE_SAVED: Final[AuditAction] = "administrative_document.template.saved"
    DEADLINE_RULE_SAVED: Final[AuditAction] = "administrative_document.deadline_rule.saved"
