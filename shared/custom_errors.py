from core.errors.errors import Error


# ---------------------------------------------------------------------------
# Auth (no domain code — use xxx)
# ---------------------------------------------------------------------------
class _AuthErrors:
    UNAUTHORIZED = Error(code=1, key="ERRORS.AUTH.UNAUTHORIZED")
    FORBIDDEN = Error(code=2, key="ERRORS.AUTH.FORBIDDEN")
    RATE_LIMITED = Error(code=3, key="ERRORS.AUTH.RATE_LIMITED")
    AUTHORIZATION_MISSING = Error(code=4, key="ERRORS.AUTH.AUTHORIZATION_MISSING")


class _SubscriptionErrors:
    NOT_SUBSCRIBED = Error(code=1003, key="ERRORS.SUBSCRIPTION.NOT_SUBSCRIBED")


class _AdminErrors:
    """Administrative-document domain errors (2300 block)."""

    # ---- dossiers ----
    GET_DOSSIERS = Error(code=2300, key="ERRORS.ADMIN.GET_DOSSIERS")
    DOSSIER_NOT_FOUND = Error(code=2302, key="ERRORS.ADMIN.DOSSIER_NOT_FOUND")
    # Another procedure of the same family already carries this external reference.
    DUPLICATE_EXTERNAL_REF = Error(code=2305, key="ERRORS.ADMIN.DUPLICATE_EXTERNAL_REF")
    # The caller's community has no regulator, or one this service has no region for.
    REGION_NOT_RESOLVED = Error(code=2306, key="ERRORS.ADMIN.REGION_NOT_RESOLVED")
    # An operation-scope procedure names an operation that does not exist, or
    # belongs to a different community.
    SHARING_OPERATION_NOT_FOUND = Error(code=2307, key="ERRORS.ADMIN.SHARING_OPERATION_NOT_FOUND")

    # ---- documents ----
    GET_DOCUMENTS = Error(code=2310, key="ERRORS.ADMIN.GET_DOCUMENTS")
    GET_DOCUMENT = Error(code=2311, key="ERRORS.ADMIN.GET_DOCUMENT")
    DOCUMENT_NOT_FOUND = Error(code=2312, key="ERRORS.ADMIN.DOCUMENT_NOT_FOUND")

    # ---- versions (R2/R4) ----
    VERSION_NOT_FOUND = Error(code=2331, key="ERRORS.ADMIN.VERSION_NOT_FOUND")
    UPLOAD_VERSION_FAILED = Error(code=2332, key="ERRORS.ADMIN.UPLOAD_VERSION_FAILED")
    INVALID_FILE = Error(code=2334, key="ERRORS.ADMIN.INVALID_FILE")
    # Body exceeds UPLOAD_MAX_BODY_BYTES; raised by the bounded read the
    # request-limits middleware can't pre-screen (chunked / no Content-Length). → 413.
    FILE_TOO_LARGE = Error(code=2335, key="ERRORS.ADMIN.FILE_TOO_LARGE")
    STORAGE_UPLOAD_FAILED = Error(code=2336, key="ERRORS.ADMIN.STORAGE_UPLOAD_FAILED")
    STORAGE_DOWNLOAD_FAILED = Error(code=2337, key="ERRORS.ADMIN.STORAGE_DOWNLOAD_FAILED")

    # ---- template registry (R5) ----
    GET_TEMPLATES = Error(code=2350, key="ERRORS.ADMIN.GET_TEMPLATES")
    TEMPLATE_NOT_FOUND = Error(code=2351, key="ERRORS.ADMIN.TEMPLATE_NOT_FOUND")
    SAVE_TEMPLATE = Error(code=2352, key="ERRORS.ADMIN.SAVE_TEMPLATE")

    # ---- generation (Phase 2) ----
    # A piece is generated while it is missing, stale or ready, and never once
    # its procedure was sent (what was sent is frozen with it). → 409.
    GENERATION_NOT_ALLOWED = Error(code=2360, key="ERRORS.ADMIN.GENERATION_NOT_ALLOWED")
    # No template in force for this (region, doc_type), or it has no bundle URI
    # yet — registering one is a SQL/seed change, not a user action. → 422.
    TEMPLATE_NOT_REGISTERED = Error(code=2361, key="ERRORS.ADMIN.TEMPLATE_NOT_REGISTERED")
    GENERATION_FAILED = Error(code=2362, key="ERRORS.ADMIN.GENERATION_FAILED")
    PREFILL_FAILED = Error(code=2363, key="ERRORS.ADMIN.PREFILL_FAILED")
    # A render is already pending for this document. → 409.
    RENDER_ALREADY_IN_FLIGHT = Error(code=2364, key="ERRORS.ADMIN.RENDER_ALREADY_IN_FLIGHT")
    # More rows than the official form reserves. Permanent: splitting a filing is
    # not something CWaPE defines, and truncating would file incomplete legal
    # data. → 422.
    TOO_MANY_ROWS = Error(code=2365, key="ERRORS.ADMIN.TOO_MANY_ROWS")
    # The CRM lacks what makes the piece mean anything (a key description of an
    # operation with no key, or a key with no iteration): the pre-fill's blocking
    # warnings say what to fix. → 422.
    GENERATION_BLOCKED = Error(code=2366, key="ERRORS.ADMIN.GENERATION_BLOCKED")

    # ---- procedures engine: the baseline (2370 block) ----
    GET_BASELINE = Error(code=2370, key="ERRORS.ADMIN.GET_BASELINE")
    CONFIRM_BASELINE = Error(code=2371, key="ERRORS.ADMIN.CONFIRM_BASELINE")
    # A choice names an item that is not on the baseline, or an option that item
    # does not have, or "in progress" without its sending date. -> 422.
    BASELINE_INVALID = Error(code=2372, key="ERRORS.ADMIN.BASELINE_INVALID")
    # The core refused: an EAN would be pending, authorised or active in two
    # operations at once. The response lists them. -> 409.
    BASELINE_CONFLICT = Error(code=2373, key="ERRORS.ADMIN.BASELINE_CONFLICT")

    # ---- procedures engine: the /admin routes ----
    GET_PROCEDURES = Error(code=2374, key="ERRORS.ADMIN.GET_PROCEDURES")
    PROCEDURE_NOT_FOUND = Error(code=2375, key="ERRORS.ADMIN.PROCEDURE_NOT_FOUND")
    # Not a type of the community's catalogue in force, or not one created by hand. -> 422.
    UNKNOWN_PROCEDURE_TYPE = Error(code=2376, key="ERRORS.ADMIN.UNKNOWN_PROCEDURE_TYPE")
    # A draft of this type is already open for this operation (one per family). -> 409.
    DRAFT_EXISTS = Error(code=2377, key="ERRORS.ADMIN.DRAFT_EXISTS")
    # Sent: its changes and the pieces it was sent with no longer change. -> 409.
    PROCEDURE_FROZEN = Error(code=2378, key="ERRORS.ADMIN.PROCEDURE_FROZEN")
    # The catalogue has no such step change from the current step. -> 409.
    TRANSITION_NOT_ALLOWED = Error(code=2379, key="ERRORS.ADMIN.TRANSITION_NOT_ALLOWED")
    UNKNOWN_RESPONSE_TYPE = Error(code=2380, key="ERRORS.ADMIN.UNKNOWN_RESPONSE_TYPE")
    CHANGE_NOT_FOUND = Error(code=2381, key="ERRORS.ADMIN.CHANGE_NOT_FOUND")
    PIECE_NOT_FOUND = Error(code=2382, key="ERRORS.ADMIN.PIECE_NOT_FOUND")
    CORRESPONDENCE_NOT_FOUND = Error(code=2383, key="ERRORS.ADMIN.CORRESPONDENCE_NOT_FOUND")
    PROCEDURE_ACTION_FAILED = Error(code=2384, key="ERRORS.ADMIN.PROCEDURE_ACTION_FAILED")
    # Phase 2: draft and final pieces. Sending wants every supplied piece final.
    FINAL_VERSIONS_REQUIRED = Error(code=2385, key="ERRORS.ADMIN.FINAL_VERSIONS_REQUIRED")
    FINALIZE_NOT_ALLOWED = Error(code=2386, key="ERRORS.ADMIN.FINALIZE_NOT_ALLOWED")
    # An upload (or a render older than phase 2) has nothing to regenerate from.
    VERSION_NOT_REGENERABLE = Error(code=2387, key="ERRORS.ADMIN.VERSION_NOT_REGENERABLE")
    REVIEW_REQUIRED = Error(code=2388, key="ERRORS.ADMIN.REVIEW_REQUIRED")
    # trg_document_template_in_use: the bundle of a used template is evidence.
    TEMPLATE_IN_USE = Error(code=2389, key="ERRORS.ADMIN.TEMPLATE_IN_USE")
    # The bytes behind a version no longer match its recorded sha256.
    STORED_FILE_ALTERED = Error(code=2390, key="ERRORS.ADMIN.STORED_FILE_ALTERED")
    # Phase 3: effects. Going in force, the core's one-operation constraint
    # refused (the 409 lists the EANs). -> 409.
    EFFECT_CONFLICT = Error(code=2391, key="ERRORS.ADMIN.EFFECT_CONFLICT")
    # Refiling: the change is not refused (or lapsed), or already refiled. -> 409.
    NOT_REFILABLE = Error(code=2392, key="ERRORS.ADMIN.NOT_REFILABLE")
    # Refiling: the CRM no longer has it so (meter left, key replaced). -> 409.
    SUBJECT_NOT_IN_OPERATION = Error(code=2393, key="ERRORS.ADMIN.SUBJECT_NOT_IN_OPERATION")
    # Phase 4: the reception address is not configured (no template or host). -> 409.
    INBOUND_DISABLED = Error(code=2394, key="ERRORS.ADMIN.INBOUND_DISABLED")
    # A received mail (or one of its attachments) of this community does not exist. -> 404.
    INBOUND_MAIL_NOT_FOUND = Error(code=2395, key="ERRORS.ADMIN.INBOUND_MAIL_NOT_FOUND")
    # A received mail already recorded or discarded. -> 409.
    INBOUND_MAIL_HANDLED = Error(code=2396, key="ERRORS.ADMIN.INBOUND_MAIL_HANDLED")
    # Phase 4: the effect date is corrected only while it is still to come (an
    # open effect deadline). -> 409.
    EFFECTIVE_DATE_NOT_OPEN = Error(code=2397, key="ERRORS.ADMIN.EFFECTIVE_DATE_NOT_OPEN")
    # Phase 4: the summary email's setting does not hold together (rhythm,
    # weekday, first send, or a recipient who is no admin or manager). -> 422.
    DIGEST_SETTING_INVALID = Error(code=2398, key="ERRORS.ADMIN.DIGEST_SETTING_INVALID")

    # ---- the signature annexe (2026-11-03): a 2600 block, 2399 being the
    # 2300 block's last code. -> 409 unless said otherwise. ----
    # The community's signature module is off.
    SIGNATURE_MODULE_OFF = Error(code=2600, key="ERRORS.ADMIN.SIGNATURE_MODULE_OFF")
    # Not a per-member collected piece its regulator lets sign in the app, or
    # its current version is not the one to sign, or its procedure was sent.
    SIGNATURE_NOT_APPLICABLE = Error(code=2601, key="ERRORS.ADMIN.SIGNATURE_NOT_APPLICABLE")
    # The participant has no account: they cannot sign in the app.
    SIGNATURE_NO_ACCOUNT = Error(code=2602, key="ERRORS.ADMIN.SIGNATURE_NO_ACCOUNT")
    SIGNATURE_ALREADY_REQUESTED = Error(code=2603, key="ERRORS.ADMIN.SIGNATURE_ALREADY_REQUESTED")
    # The broker did not take the request: nothing was asked. -> 503.
    SIGNATURE_UNAVAILABLE = Error(code=2604, key="ERRORS.ADMIN.SIGNATURE_UNAVAILABLE")


class _Errors:
    auth = _AuthErrors()
    subscription = _SubscriptionErrors()
    admin = _AdminErrors()


errors = _Errors()
