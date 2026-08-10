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
    GET_DOSSIER = Error(code=2301, key="ERRORS.ADMIN.GET_DOSSIER")
    DOSSIER_NOT_FOUND = Error(code=2302, key="ERRORS.ADMIN.DOSSIER_NOT_FOUND")
    CREATE_DOSSIER = Error(code=2303, key="ERRORS.ADMIN.CREATE_DOSSIER")
    UPDATE_DOSSIER = Error(code=2304, key="ERRORS.ADMIN.UPDATE_DOSSIER")
    # Another dossier of the same type already carries this external reference.
    DUPLICATE_EXTERNAL_REF = Error(code=2305, key="ERRORS.ADMIN.DUPLICATE_EXTERNAL_REF")
    # The caller's community has no regulator, or one this service has no region for.
    REGION_NOT_RESOLVED = Error(code=2306, key="ERRORS.ADMIN.REGION_NOT_RESOLVED")
    # Every dossier is filed for one sharing operation. Raised when the given
    # operation does not exist, or belongs to a different community.
    SHARING_OPERATION_NOT_FOUND = Error(code=2307, key="ERRORS.ADMIN.SHARING_OPERATION_NOT_FOUND")

    # ---- documents ----
    GET_DOCUMENTS = Error(code=2310, key="ERRORS.ADMIN.GET_DOCUMENTS")
    GET_DOCUMENT = Error(code=2311, key="ERRORS.ADMIN.GET_DOCUMENT")
    DOCUMENT_NOT_FOUND = Error(code=2312, key="ERRORS.ADMIN.DOCUMENT_NOT_FOUND")
    CREATE_DOCUMENT = Error(code=2313, key="ERRORS.ADMIN.CREATE_DOCUMENT")

    # ---- state machine (R1) ----
    # Target status is not reachable from the current one (e.g. draft → acknowledged).
    ILLEGAL_TRANSITION = Error(code=2320, key="ERRORS.ADMIN.ILLEGAL_TRANSITION")
    # The transition needs context the caller did not supply (submission_date,
    # acknowledged_date + authority_file_ref, corrective reason…).
    MISSING_TRANSITION_CONTEXT = Error(code=2321, key="ERRORS.ADMIN.MISSING_TRANSITION_CONTEXT")
    TRANSITION_FAILED = Error(code=2322, key="ERRORS.ADMIN.TRANSITION_FAILED")

    # ---- versions (R2/R4) ----
    GET_VERSIONS = Error(code=2330, key="ERRORS.ADMIN.GET_VERSIONS")
    VERSION_NOT_FOUND = Error(code=2331, key="ERRORS.ADMIN.VERSION_NOT_FOUND")
    UPLOAD_VERSION_FAILED = Error(code=2332, key="ERRORS.ADMIN.UPLOAD_VERSION_FAILED")
    # A new version may only be added while the document is draft or ready — a
    # sent/acknowledged document must be rolled back first (sent versions are immutable).
    VERSION_NOT_ALLOWED = Error(code=2333, key="ERRORS.ADMIN.VERSION_NOT_ALLOWED")
    INVALID_FILE = Error(code=2334, key="ERRORS.ADMIN.INVALID_FILE")
    # Body exceeds UPLOAD_MAX_BODY_BYTES; raised by the bounded read the
    # request-limits middleware can't pre-screen (chunked / no Content-Length). → 413.
    FILE_TOO_LARGE = Error(code=2335, key="ERRORS.ADMIN.FILE_TOO_LARGE")
    STORAGE_UPLOAD_FAILED = Error(code=2336, key="ERRORS.ADMIN.STORAGE_UPLOAD_FAILED")
    STORAGE_DOWNLOAD_FAILED = Error(code=2337, key="ERRORS.ADMIN.STORAGE_DOWNLOAD_FAILED")

    # ---- deadlines (R3) ----
    GET_DEADLINES = Error(code=2340, key="ERRORS.ADMIN.GET_DEADLINES")
    DEADLINE_NOT_FOUND = Error(code=2341, key="ERRORS.ADMIN.DEADLINE_NOT_FOUND")
    UPDATE_DEADLINE = Error(code=2342, key="ERRORS.ADMIN.UPDATE_DEADLINE")
    DEADLINE_SWEEP_FAILED = Error(code=2343, key="ERRORS.ADMIN.DEADLINE_SWEEP_FAILED")

    # ---- template + rule registries (R5) ----
    GET_TEMPLATES = Error(code=2350, key="ERRORS.ADMIN.GET_TEMPLATES")
    TEMPLATE_NOT_FOUND = Error(code=2351, key="ERRORS.ADMIN.TEMPLATE_NOT_FOUND")
    SAVE_TEMPLATE = Error(code=2352, key="ERRORS.ADMIN.SAVE_TEMPLATE")
    GET_DEADLINE_RULES = Error(code=2353, key="ERRORS.ADMIN.GET_DEADLINE_RULES")
    DEADLINE_RULE_NOT_FOUND = Error(code=2354, key="ERRORS.ADMIN.DEADLINE_RULE_NOT_FOUND")
    SAVE_DEADLINE_RULE = Error(code=2355, key="ERRORS.ADMIN.SAVE_DEADLINE_RULE")

    # ---- generation (Phase 2) ----
    # Same rule as VERSION_NOT_ALLOWED: a sent/acknowledged document must be
    # rolled back before it can be regenerated. → 409.
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


class _Errors:
    auth = _AuthErrors()
    subscription = _SubscriptionErrors()
    admin = _AdminErrors()


errors = _Errors()
