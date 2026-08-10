"""The document and dossier state machines (spec R1).

Two ideas are encoded here and nowhere else:

1. **Which edges exist.** The adjacency tables below are the whole truth about
   legal transitions. Anything not listed is rejected — a document can never jump
   from ``draft`` straight to ``acknowledged``.

2. **Corrections are edges, not erasures.** Undoing a mistake is a *backward
   edge* that is travelled forward: it appends a new journal entry marked
   ``is_corrective`` and requires a reason. There is no code path anywhere that
   rewrites or deletes history, which is what makes the journal evidentiary.

This module is pure: no database, no request context. The service layer calls
``assert_transition_allowed`` / ``assert_requirements`` before writing anything.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.errors.errors import ErrorException
from shared.const import DocumentStatus, DossierStatus, SubjectType
from shared.custom_errors import errors

# --- Status a subject is born with -----------------------------------------
INITIAL_DOCUMENT_STATUS = DocumentStatus.DRAFT
INITIAL_DOSSIER_STATUS = DossierStatus.IN_PREPARATION


# --- Allowed edges ----------------------------------------------------------
# Backward (corrective) edges are marked in _CORRECTIVE_EDGES below.
DOCUMENT_TRANSITIONS: Mapping[DocumentStatus, frozenset[DocumentStatus]] = {
    DocumentStatus.DRAFT: frozenset({DocumentStatus.READY, DocumentStatus.OBSOLETE}),
    DocumentStatus.READY: frozenset(
        {DocumentStatus.SENT, DocumentStatus.DRAFT, DocumentStatus.OBSOLETE}
    ),
    DocumentStatus.SENT: frozenset(
        {DocumentStatus.ACKNOWLEDGED, DocumentStatus.READY, DocumentStatus.OBSOLETE}
    ),
    DocumentStatus.ACKNOWLEDGED: frozenset({DocumentStatus.OBSOLETE, DocumentStatus.SENT}),
    # Terminal: an obsolete document is superseded; create a new one instead.
    DocumentStatus.OBSOLETE: frozenset(),
}

DOSSIER_TRANSITIONS: Mapping[DossierStatus, frozenset[DossierStatus]] = {
    DossierStatus.IN_PREPARATION: frozenset({DossierStatus.SUBMITTED, DossierStatus.LAPSED}),
    DossierStatus.SUBMITTED: frozenset(
        {DossierStatus.COMPLETE, DossierStatus.IN_PREPARATION, DossierStatus.LAPSED}
    ),
    DossierStatus.COMPLETE: frozenset({DossierStatus.CLOSED, DossierStatus.SUBMITTED}),
    # Terminal.
    DossierStatus.CLOSED: frozenset(),
    # A lapsed dossier can be re-opened, but only as an explicit correction.
    DossierStatus.LAPSED: frozenset({DossierStatus.IN_PREPARATION}),
}

# Edges that walk the lifecycle backwards. Travelling one is legitimate — it is
# how a mistake is corrected — but it is always journaled as corrective and
# always requires a reason.
_CORRECTIVE_EDGES: frozenset[tuple[SubjectType, int, int]] = frozenset(
    {
        (SubjectType.DOCUMENT, DocumentStatus.READY, DocumentStatus.DRAFT),
        (SubjectType.DOCUMENT, DocumentStatus.SENT, DocumentStatus.READY),
        (SubjectType.DOCUMENT, DocumentStatus.ACKNOWLEDGED, DocumentStatus.SENT),
        (SubjectType.DOSSIER, DossierStatus.SUBMITTED, DossierStatus.IN_PREPARATION),
        (SubjectType.DOSSIER, DossierStatus.COMPLETE, DossierStatus.SUBMITTED),
        (SubjectType.DOSSIER, DossierStatus.LAPSED, DossierStatus.IN_PREPARATION),
    }
)

# --- Context each transition must carry -------------------------------------
# Recording a submission without its date, or an acknowledgment without the
# authority's file reference, would defeat the point of the journal.
TRANSITION_REQUIREMENTS: Mapping[tuple[SubjectType, int], tuple[str, ...]] = {
    (SubjectType.DOCUMENT, DocumentStatus.SENT): ("submission_date",),
    (SubjectType.DOCUMENT, DocumentStatus.ACKNOWLEDGED): (
        "acknowledged_date",
        "authority_file_ref",
    ),
    (SubjectType.DOSSIER, DossierStatus.SUBMITTED): ("submission_date",),
}

# Every corrective transition must say why.
CORRECTIVE_REQUIREMENTS: tuple[str, ...] = ("reason",)


def _transitions_for(subject_type: SubjectType) -> Mapping[int, frozenset[int]]:
    if subject_type == SubjectType.DOCUMENT:
        return DOCUMENT_TRANSITIONS  # type: ignore[return-value]
    return DOSSIER_TRANSITIONS  # type: ignore[return-value]


def allowed_targets(subject_type: SubjectType, from_status: int) -> frozenset[int]:
    """The statuses reachable in one step from ``from_status``."""
    return _transitions_for(subject_type).get(from_status, frozenset())


def is_corrective(subject_type: SubjectType, from_status: int, to_status: int) -> bool:
    """True when this edge walks the lifecycle backwards (a traced correction)."""
    return (subject_type, from_status, to_status) in _CORRECTIVE_EDGES


def assert_transition_allowed(subject_type: SubjectType, from_status: int, to_status: int) -> None:
    """Reject an edge the state machine does not have.

    Raises 409 rather than 422: the request is well-formed, it just conflicts
    with the subject's current state.
    """
    if to_status == from_status:
        raise ErrorException(errors.admin.ILLEGAL_TRANSITION, status_code=409)
    if to_status not in allowed_targets(subject_type, from_status):
        raise ErrorException(errors.admin.ILLEGAL_TRANSITION, status_code=409)


def required_context_fields(
    subject_type: SubjectType, from_status: int, to_status: int
) -> tuple[str, ...]:
    """Context keys this specific transition must supply."""
    fields = TRANSITION_REQUIREMENTS.get((subject_type, to_status), ())
    if is_corrective(subject_type, from_status, to_status):
        fields = fields + CORRECTIVE_REQUIREMENTS
    return fields


def assert_requirements(
    subject_type: SubjectType,
    from_status: int,
    to_status: int,
    context: Mapping[str, Any] | None,
) -> None:
    """Reject a transition whose mandatory context is missing or blank.

    Raises 422: the caller must supply more information.
    """
    required = required_context_fields(subject_type, from_status, to_status)
    if not required:
        return
    supplied = context or {}
    for field in required:
        value = supplied.get(field)
        if value is None or (isinstance(value, str) and not value.strip()):
            raise ErrorException(errors.admin.MISSING_TRANSITION_CONTEXT, status_code=422)
