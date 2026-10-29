from enum import IntEnum, StrEnum


class Region(IntEnum):
    """Regulatory region a dossier/template/rule belongs to.

    Resolved from the CRM ``community.regulator`` code (see
    ``reference/regulators.json``): ``BE-WAL-CWAPE`` → ``WAL``. v1 ships Wallonia
    only; BRU/VLA are reserved so the template + rule sets can be added as data.
    """

    WAL = 1
    BRU = 2
    VLA = 3


class DossierType(IntEnum):
    """Administrative dossier kinds (spec §5.3)."""

    CREATION_NOTIFICATION = 1  # → CWaPE, via Mon Espace
    MODIFICATION = 2  # → CWaPE, 15-business-day notification
    ANNUAL_REPORT = 3  # → CWaPE, recurring
    SHARING_AUTHORIZATION = 4  # → DSO, then opinion → CWaPE
    SHARING_MODIFICATION = 5  # → DSO
    CESSATION = 6  # → CWaPE (via modification)


class DossierStatus(IntEnum):
    """Dossier lifecycle. Transitions are journaled in ``status_event``."""

    IN_PREPARATION = 1
    SUBMITTED = 2
    COMPLETE = 3
    CLOSED = 4
    LAPSED = 5


class DocumentStatus(IntEnum):
    """Document lifecycle. Transitions are journaled in ``status_event``."""

    DRAFT = 1
    READY = 2
    SENT = 3
    ACKNOWLEDGED = 4
    OBSOLETE = 5


class DocOrigin(IntEnum):
    """How a document's versions are produced.

    ``GENERATED`` versions come from docgen rendering a registered template
    against a frozen data snapshot; ``UPLOADED`` ones are files a human supplied.
    """

    GENERATED = 1
    UPLOADED = 2


class RenderState(IntEnum):
    """State of an IN-FLIGHT generation, held in ``document_render``.

    Deliberately NOT a ``DocumentStatus``: that enum is the *regulatory*
    lifecycle, every transition of which is written to the immutable journal and
    checked by a deferred trigger. A render is a technical operation that may be
    retried or fail for infrastructure reasons, so journaling it would both
    pollute the evidentiary record and force fake transitions through the state
    machine. A generated document stays DRAFT until a human marks it READY.

    There is no SUCCEEDED member on purpose: a successful render deletes its
    ``document_render`` row, and the resulting ``document_version`` is the record.
    """

    PENDING = 1
    FAILED = 2


class SubjectType(IntEnum):
    """What a ``status_event`` row is about."""

    DOCUMENT = 1
    DOSSIER = 2


class OffsetUnit(IntEnum):
    """How a ``deadline_rule`` offset is applied to the trigger's base date."""

    BUSINESS_DAYS = 1  # jours ouvres (Mon-Fri minus Belgian public holidays)
    MONTHS = 2  # calendar months (relativedelta)
    CALENDAR_DAYS = 3


class DeadlineStatus(IntEnum):
    """Derived-deadline lifecycle."""

    OPEN = 1
    MET = 2
    MISSED = 3
    CANCELLED = 4


class TriggerEvent(StrEnum):
    """Keys that link a state transition to the ``deadline_rule`` rows it fires.

    Stored as text in ``deadline_rule.trigger_event`` so a new rule is pure data.
    """

    DOSSIER_SUBMITTED = "dossier.submitted"
    DOSSIER_COMPLETE = "dossier.complete"
    DOSSIER_MODIFICATION = "dossier.modification"
    DOCUMENT_ACKNOWLEDGED_INCOMPLETE = "document.acknowledged.incomplete"


class DeadlineType(StrEnum):
    """Keys stored in ``deadline.deadline_type`` / ``deadline_rule.deadline_type``."""

    COMPLETENESS_CHECK = "completeness_check"  # +10 business days
    LAPSE = "lapse"  # +6 months
    MODIFICATION_NOTIFICATION = "modification_notification"  # +15 business days
    ANNUAL_REPORT = "annual_report"  # recurring, +12 months


class AcknowledgementResult(StrEnum):
    """``context_json.result`` supplied when acknowledging a document."""

    COMPLETE = "complete"
    INCOMPLETE = "incomplete"


# --- Procedures engine (espace administratif) --------------------------------
# Generic enums are coded integers, as everywhere in this schema; a code that
# belongs to one regulator (a step, a response type, a variant, a piece code)
# is a string the plugin validates and never appears here.


class ProcedureStatus(IntEnum):
    """The engine's common statuses, held in ``dossier.status``.

    The regulator plugin's own step (incomplete, non-conformity, accepted…) is
    ``dossier.step``. Codes 1, 2 and 5 keep the meaning they had for the July
    ``DossierStatus``; 3 (COMPLETE) and 4 (CLOSED) are retired and only survive
    in the journal's history.
    """

    DRAFT = 1
    SENT = 2
    LAPSED = 5
    READY = 6
    IN_FORCE = 7
    REFUSED = 8
    CANCELLED = 9


class ProcedureDocumentStatus(IntEnum):
    """Status of a piece of a procedure, held in ``document.status``.

    READY and OBSOLETE keep their July ``DocumentStatus`` codes; 1 (DRAFT), 3
    (SENT) and 4 (ACKNOWLEDGED) are retired. "Generating" is never a status:
    it is read from ``document_render``.
    """

    READY = 2
    OBSOLETE = 5
    MISSING = 6
    STALE = 7
    SIGNED = 8
    REJECTED = 9


class ProcedureScope(IntEnum):
    """What a procedure is about: the community itself, or one operation."""

    COMMUNITY = 1
    OPERATION = 2


class Recipient(IntEnum):
    """Who a procedure is sent to."""

    GRD_REF = 1  # the operation's reference DSO (SPOC when several)
    REGULATOR = 2


class Channel(IntEnum):
    """How a procedure is sent, or a correspondence travelled."""

    EMAIL = 1
    ONLINE_FORM = 2
    POST = 3
    UPLOAD = 4  # correspondence only: a file someone added by hand


class OpenedBy(IntEnum):
    """Who opened a procedure."""

    SYSTEM = 1  # the event router or a daily job
    USER = 2
    MIGRATION = 3  # a July dossier converted by migration 0003


class ChangeSubjectType(IntEnum):
    """What a ``change`` row is about. 1 and 2 match the core's
    ``regulatory_link_status.subject_type``."""

    METER = 1
    KEY = 2
    MEMBER = 3
    OPERATION = 4
    COMMUNITY = 5


class ChangeStatus(IntEnum):
    """Lifecycle of a ``change``."""

    PENDING = 1  # detected, not filed yet (baseline awaiting confirmation)
    ATTACHED = 2  # filed in a procedure
    DEFERRED = 3  # postponed by the admin to a later procedure
    EXCLUDED = 4  # the admin decided it needs no procedure
    CANCELLED = 5  # compensated by its inverse before sending
    APPLIED = 6  # its effect reached the core
    REFUSED = 7  # the authority refused it


class EventOutcome(IntEnum):
    """What the consumer did with a core event (``processed_event.outcome``).

    The lower-cased name is the ``reason`` label of
    ``admin_events_skipped_total``.
    """

    ROUTED = 1
    MODULE_DISABLED = 2
    BEFORE_ACTIVATION = 3
    ALREADY_REFLECTED = 4
    REGULATOR_NOT_SUPPORTED = 5
    NO_PROCEDURE = 6


class CorrespondenceDirection(IntEnum):
    OUTBOUND = 1
    INBOUND = 2


class Counterparty(IntEnum):
    """The other side of a correspondence."""

    GRD = 1
    REGULATOR = 2
    PARTICIPANT = 3
    OTHER = 4


class DocumentKind(IntEnum):
    """Where a piece comes from."""

    GENERATED = 1  # rendered from a template (annex, amendment)
    COLLECTED = 2  # supplied by the community or a participant (signed declaration)
    RECEIVED = 3  # sent back by the recipient


class DeadlineParty(IntEnum):
    """Who a deadline binds."""

    COMMUNITY = 1
    GRD = 2
    REGULATOR = 3


class DeadlineKind(IntEnum):
    """What a deadline is for."""

    SUBMISSION = 1  # the community must send something
    RESPONSE = 2  # the recipient must answer
    LAPSE = 3  # past it the procedure lapses
    EFFECT = 4  # the change takes effect


class RuleKind(IntEnum):
    """How a catalogue ``deadline_rule`` computes its date."""

    RELATIVE = 1  # an offset after an anchor event
    ANNUAL = 2  # a fixed day of the year


class Bucket(StrEnum):
    """The dashboard group a procedure's current step puts it in. Derived from
    the catalogue, never stored; the value is what the API returns."""

    TODO = "todo"  # the community has something to do
    WAITING = "waiting"  # the GRD or the regulator has to answer
    PLANNED = "planned"  # scheduled: an annual filing, an effect to come
    DONE = "done"


class NextActionCode(StrEnum):
    """What the dashboard's one button does, stored in ``dossier.next_action_code``.

    Generic on purpose: the frontend knows these and nothing else, so adding a
    regulator never touches it. What the button SAYS comes from the catalogue.
    """

    COMPLETE_DOCUMENTS = "complete_documents"
    SEND = "send"
    RECORD_RESPONSE = "record_response"
    NONE = "none"


class RegulatoryStatus(IntEnum):
    """``regulatory_link_status.regulatory_status`` in the CORE database.

    A mirror of crm-backend's ``RegulatoryStatus``
    (src/modules/regulatory/domain/regulatory-link-status.models.ts); the core
    owns the codes and its CHECK accepts 1 to 8.
    """

    TO_NOTIFY = 1
    PENDING_GRD = 2
    PENDING_REGULATOR = 3
    AUTHORIZED = 4
    ACTIVE = 5
    LEAVING = 6
    ENDED = 7
    REFUSED = 8


class FeatureName(StrEnum):
    """CRM subscription feature gate key (see ``require_feature``)."""

    ADMINISTRATIVE_DOCUMENT = "administrative-document"


# --- NATS JetStream streams (declared in core/queue/streams.json) -----------
# Three streams with DISJOINT subjects, because JetStream stream names AND
# subjects are a global namespace on the shared broker and it rejects overlap.
# ADMIN_DOCUMENT_EVENTS therefore lists its subjects explicitly rather than
# claiming `optimce.administrative_document.>` — which also means a typo'd
# subject now fails loudly at publish instead of being stored in a stream nobody
# consumes.
ADMIN_DOCUMENT_EVENTS_STREAM = "ADMIN_DOCUMENT_EVENTS"  # limits: fire-and-forget notifications
ADMIN_DOCUMENT_STREAM = "ADMIN_DOCUMENT"  # work_queue: generation requests
ADMIN_DOCUMENT_DLQ_STREAM = "ADMIN_DOCUMENT_DLQ"  # limits: poison messages

# --- Subjects the API publishes --------------------------------------------
SUBJECT_DOCUMENT_STATUS_CHANGED = "optimce.administrative_document.document.status_changed"
SUBJECT_DOSSIER_STATUS_CHANGED = "optimce.administrative_document.dossier.status_changed"
SUBJECT_DEADLINE_CREATED = "optimce.administrative_document.deadline.created"
SUBJECT_DEADLINE_MISSED = "optimce.administrative_document.deadline.missed"
SUBJECT_DOCUMENT_RENDERED = "optimce.administrative_document.document.rendered"

# --- Work + dead-letter subjects (worker) ----------------------------------
SUBJECT_GENERATE_REQUESTED = "optimce.administrative_document.document.generate.requested"
SUBJECT_DLQ_GENERATE = "optimce.administrative_document.dlq.generate"
SUBJECT_DLQ_DOCGEN_RESULT = "optimce.administrative_document.dlq.docgen_result"

# --- Event.type values carried in the envelope (spec R6) -------------------
EVENT_DOCUMENT_STATUS_CHANGED = "admin.document.status_changed"
EVENT_DOSSIER_STATUS_CHANGED = "admin.dossier.status_changed"
EVENT_DEADLINE_CREATED = "admin.dossier.deadline_created"
EVENT_DEADLINE_MISSED = "admin.dossier.deadline_missed"
EVENT_DOCUMENT_RENDERED = "admin.document.rendered"
EVENT_GENERATE_REQUESTED = "admin.document.generate_requested"

# Object-key prefix for stored document versions inside OUTPUT_BUCKET. Keys are
# content-addressed (…/{sha256}) so a stored version is never overwritten.
STORAGE_KEY_PREFIX = "administrative-document"

# --- Upload size cap --------------------------------------------------------
# 50 MB, the ceiling for one document version held in memory. Covers scanned
# statutes / signed agreements and the mandated XLSX annexes. Bump this only
# after moving the upload to a streaming put_object.
#
# Enforced twice, which is why it lives here rather than beside the middleware:
# `core/middleware/request_limits.py` screens Content-Length at the HTTP edge,
# and `AdministrativeDocumentService._read_within_cap` bounds the bytes as they
# actually arrive (a chunked upload declares no Content-Length and slips past the
# first check). The middleware is starlette-importing and API-only; the service
# is reached by the worker too, so the number cannot live in that module.
UPLOAD_MAX_BODY_BYTES = 50 * 1024 * 1024

# --- Artifact media types ---------------------------------------------------
# The formats document-generation can produce, mapped both ways. A downloaded
# file MUST carry an extension: an .xlsx saved as "document-3-v1" is a file the
# operating system cannot open, which makes the whole generation feature useless
# at the last step.
CONTENT_TYPE_BY_FORMAT: dict[str, str] = {
    "pdf": "application/pdf",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "html": "text/html; charset=utf-8",
}

EXTENSION_BY_CONTENT_TYPE: dict[str, str] = {
    content_type.split(";")[0].strip(): f".{fmt}"
    for fmt, content_type in CONTENT_TYPE_BY_FORMAT.items()
}


def extension_for_content_type(content_type: str | None) -> str:
    """File extension for a stored media type, or '' when unknown.

    Used to give a download a usable name when the version has none of its own
    (an uploaded file always brings one; a generated one is named by its bundle).
    """
    if not content_type:
        return ""
    return EXTENSION_BY_CONTENT_TYPE.get(content_type.split(";")[0].strip(), "")
