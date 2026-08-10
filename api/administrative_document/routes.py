"""HTTP routes for the administrative-document annexe.

NOTE: this module must NOT use ``from __future__ import annotations``. The
``with_default_error`` wrapper resolves string annotations against its own module
globals, so stringified Pydantic body types become invisible and FastAPI demotes
them to query parameters (422 "Field required, loc: [query, body]"). Keep real
annotation objects here.

Access model: the router requires an authenticated caller with an active
subscription. Reads are available to any member of the community; anything that
changes state additionally requires MANAGER, and the reference registries
require ADMIN.
"""

import math
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Query, Response, UploadFile

from api.administrative_document.deps import get_administrative_document_service
from api.administrative_document.mappers import (
    to_deadline_out,
    to_deadline_rule_out,
    to_document_detail_out,
    to_document_out,
    to_dossier_detail_out,
    to_dossier_out,
    to_my_filing_out,
    to_prefill_warning_out,
    to_status_event_out,
    to_template_out,
    to_version_out,
)
from api.administrative_document.schemas import (
    AcknowledgeRequest,
    DeadlineOut,
    DeadlineResolveRequest,
    DeadlineRuleCreate,
    DeadlineRuleOut,
    DeadlineRuleUpdate,
    DeadlineSweepOut,
    DocumentCreate,
    DocumentDetailOut,
    DocumentOut,
    DocumentVersionOut,
    DossierCreate,
    DossierDetailOut,
    DossierOut,
    DossierUpdate,
    GenerateAccepted,
    GenerateRequest,
    MarkSentRequest,
    MyFilingOut,
    PrefillOut,
    RenderStatusOut,
    RollbackRequest,
    SharingOperationOut,
    StatusEventOut,
    TemplateCreate,
    TemplateOut,
    TemplateUpdate,
    TransitionRequest,
)
from api.administrative_document.service import AdministrativeDocumentService
from core.api_response import ApiResponse, ApiResponsePaginated, Pagination
from core.context_vars import current_internal_community_id
from core.errors.errors import ErrorException
from core.errors.with_default_error import with_default_error
from core.security.community_scope import resolve_internal_community
from core.security.dependencies import require_feature, require_min_role
from core.security.user_context import Role
from shared.const import (
    DocumentStatus,
    FeatureName,
    RenderState,
    extension_for_content_type,
)
from shared.custom_errors import errors

administrative_document_routes = APIRouter(
    dependencies=[
        Depends(resolve_internal_community),
        Depends(require_feature(FeatureName.ADMINISTRATIVE_DOCUMENT)),
    ]
)

manager_only = Depends(require_min_role(Role.MANAGER))
admin_only = Depends(require_min_role(Role.ADMIN))

ServiceDep = Annotated[AdministrativeDocumentService, Depends(get_administrative_document_service)]


def _paginate(total: int, page: int, limit: int) -> Pagination:
    return Pagination(
        page=page,
        limit=limit,
        total=total,
        total_pages=math.ceil(total / limit) if limit else 0,
    )


def _require_community() -> int:
    internal_id = current_internal_community_id.get()
    if internal_id is None:
        raise ErrorException(errors.auth.FORBIDDEN, status_code=403)
    return internal_id


# ---------------------------------------------------------------------------
# Sharing operations
# ---------------------------------------------------------------------------


@administrative_document_routes.get(
    "/sharing-operations",
    response_model=ApiResponse[list[SharingOperationOut]],
    dependencies=[manager_only],
)
@with_default_error(errors.admin.GET_DOSSIERS)
async def list_sharing_operations(service: ServiceDep):
    """The community's sharing operations — every dossier is filed for one of these."""
    rows = await service.list_sharing_operations(_require_community())
    return ApiResponse(data=[SharingOperationOut(id=row.id, name=row.name) for row in rows])


# ---------------------------------------------------------------------------
# Dossiers
# ---------------------------------------------------------------------------


@administrative_document_routes.post(
    "/dossiers", response_model=ApiResponse[DossierOut], dependencies=[manager_only]
)
@with_default_error(errors.admin.CREATE_DOSSIER)
async def create_dossier(body: DossierCreate, service: ServiceDep):
    dossier = await service.create_dossier(
        id_community=_require_community(),
        dossier_type=body.dossier_type,
        title=body.title,
        external_ref=body.external_ref,
        id_sharing_operation=body.id_sharing_operation,
        metadata=body.metadata,
    )
    return ApiResponse(data=to_dossier_out(dossier))


@administrative_document_routes.get(
    "/dossiers",
    response_model=ApiResponsePaginated[list[DossierOut]],
    dependencies=[manager_only],
)
@with_default_error(errors.admin.GET_DOSSIERS)
async def list_dossiers(
    service: ServiceDep,
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=20, ge=1, le=200),
    status: int | None = Query(default=None),
    dossier_type: int | None = Query(default=None),
    id_sharing_operation: int | None = Query(
        default=None, description="Restrict to the dossiers filed for one sharing operation."
    ),
    sort: str | None = Query(default=None),
    order: str | None = Query(default=None),
):
    rows, total = await service.list_dossiers(
        page=page,
        limit=limit,
        status=status,
        dossier_type=dossier_type,
        id_sharing_operation=id_sharing_operation,
        sort=sort,
        order=order,
    )
    return ApiResponsePaginated(
        data=[to_dossier_out(row) for row in rows],
        pagination=_paginate(total, page, limit),
    )


@administrative_document_routes.get(
    "/dossiers/{dossier_id}",
    response_model=ApiResponse[DossierDetailOut],
    dependencies=[manager_only],
)
@with_default_error(errors.admin.GET_DOSSIER)
async def get_dossier(dossier_id: int, service: ServiceDep):
    dossier, documents, deadlines = await service.dossier_detail(dossier_id)
    return ApiResponse(data=to_dossier_detail_out(dossier, documents, deadlines))


@administrative_document_routes.patch(
    "/dossiers/{dossier_id}", response_model=ApiResponse[DossierOut], dependencies=[manager_only]
)
@with_default_error(errors.admin.UPDATE_DOSSIER)
async def update_dossier(dossier_id: int, body: DossierUpdate, service: ServiceDep):
    dossier = await service.update_dossier(
        dossier_id, title=body.title, external_ref=body.external_ref, metadata=body.metadata
    )
    return ApiResponse(data=to_dossier_out(dossier))


@administrative_document_routes.post(
    "/dossiers/{dossier_id}/transition",
    response_model=ApiResponse[DossierOut],
    dependencies=[manager_only],
)
@with_default_error(errors.admin.TRANSITION_FAILED)
async def transition_dossier(dossier_id: int, body: TransitionRequest, service: ServiceDep):
    dossier = await service.transition_dossier(
        dossier_id, to_status=body.to_status, context=body.context
    )
    return ApiResponse(data=to_dossier_out(dossier))


@administrative_document_routes.get(
    "/dossiers/{dossier_id}/deadlines",
    response_model=ApiResponse[list[DeadlineOut]],
    dependencies=[manager_only],
)
@with_default_error(errors.admin.GET_DEADLINES)
async def list_dossier_deadlines(dossier_id: int, service: ServiceDep):
    rows = await service.dossier_deadlines(dossier_id)
    return ApiResponse(data=[to_deadline_out(row) for row in rows])


@administrative_document_routes.get(
    "/dossiers/{dossier_id}/timeline",
    response_model=ApiResponse[list[StatusEventOut]],
    dependencies=[manager_only],
)
@with_default_error(errors.admin.GET_DOSSIER)
async def get_dossier_timeline(dossier_id: int, service: ServiceDep):
    """The immutable journal for the dossier and every document inside it."""
    events = await service.dossier_timeline(dossier_id)
    return ApiResponse(data=[to_status_event_out(event) for event in events])


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------


@administrative_document_routes.post(
    "/dossiers/{dossier_id}/documents",
    response_model=ApiResponse[DocumentOut],
    dependencies=[manager_only],
)
@with_default_error(errors.admin.CREATE_DOCUMENT)
async def create_document(dossier_id: int, body: DocumentCreate, service: ServiceDep):
    document = await service.create_document(
        dossier_id=dossier_id, doc_type=body.doc_type, title=body.title
    )
    return ApiResponse(data=to_document_out(document))


@administrative_document_routes.get(
    "/dossiers/{dossier_id}/documents",
    response_model=ApiResponse[list[DocumentOut]],
    dependencies=[manager_only],
)
@with_default_error(errors.admin.GET_DOCUMENTS)
async def list_documents(dossier_id: int, service: ServiceDep):
    rows = await service.list_documents(dossier_id)
    return ApiResponse(data=[to_document_out(doc, count) for doc, count in rows])


@administrative_document_routes.get(
    "/documents/{document_id}",
    response_model=ApiResponse[DocumentDetailOut],
    dependencies=[manager_only],
)
@with_default_error(errors.admin.GET_DOCUMENT)
async def get_document(document_id: int, service: ServiceDep):
    document, versions = await service.document_detail(document_id)
    return ApiResponse(data=to_document_detail_out(document, versions))


@administrative_document_routes.post(
    "/documents/{document_id}/versions",
    response_model=ApiResponse[DocumentVersionOut],
    dependencies=[manager_only],
)
@with_default_error(errors.admin.UPLOAD_VERSION_FAILED)
async def upload_version(document_id: int, service: ServiceDep, file: UploadFile = File(...)):
    version = await service.add_version(document_id=document_id, file=file)
    return ApiResponse(data=to_version_out(version))


@administrative_document_routes.get(
    "/documents/{document_id}/versions/{version_id}/file",
    responses={200: {"content": {"application/octet-stream": {}}}},
    dependencies=[manager_only],
)
@with_default_error(errors.admin.STORAGE_DOWNLOAD_FAILED)
async def download_version(document_id: int, version_id: int, service: ServiceDep):
    """Stream a stored version back exactly as it was filed."""
    version, content = await service.read_version_bytes(
        document_id=document_id, version_id=version_id
    )
    filename = _download_filename(
        version.original_filename,
        content_type=version.content_type,
        document_id=document_id,
        version_no=version.version_no,
    )
    return Response(
        content=content,
        media_type=version.content_type or "application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _download_filename(
    original: str | None, *, content_type: str | None, document_id: int, version_no: int
) -> str:
    """Name the download, always with a usable extension.

    An uploaded version brings its own filename and a generated one is named by
    its bundle, so the synthesised fallback is rare — but when it is used it must
    still end in ``.pdf``/``.xlsx``/``.docx``. A file the operating system cannot
    open is indistinguishable from a broken download, and versions stored before
    the filename was recorded still take this path.
    """
    name = _safe_filename(original or f"document-{document_id}-v{version_no}")
    extension = extension_for_content_type(content_type)
    if extension and not name.lower().endswith(extension):
        name += extension
    return name


def _safe_filename(name: str) -> str:
    """Strip anything that could break out of the Content-Disposition header."""
    cleaned = "".join(ch for ch in name if ch.isprintable() and ch not in '"\\\r\n')
    return cleaned[:200] or "download"


# ---- generation (Phase 2) --------------------------------------------------
#
# Three steps: read the CRM into a form (prefill), submit the corrected form
# (generate), watch it land (render-status). The document stays DRAFT throughout
# — a render is a technical operation, not a regulatory transition.


@administrative_document_routes.get(
    "/documents/{document_id}/prefill",
    response_model=ApiResponse[PrefillOut],
    dependencies=[manager_only],
)
@with_default_error(errors.admin.PREFILL_FAILED)
async def prefill_document(document_id: int, service: ServiceDep):
    """Build the render payload from the CRM WITHOUT persisting it.

    Read-only on purpose: a reviewer may open the form as often as they like,
    and the snapshot is frozen only when they submit.
    """
    prefilled = await service.build_prefill(document_id=document_id)
    return ApiResponse(
        data=PrefillOut(
            data=prefilled["data"],
            warnings=[to_prefill_warning_out(w) for w in prefilled["warnings"]],
        )
    )


@administrative_document_routes.post(
    "/documents/{document_id}/generate",
    response_model=ApiResponse[GenerateAccepted],
    dependencies=[manager_only],
)
@with_default_error(errors.admin.GENERATION_FAILED)
async def generate_document(document_id: int, payload: GenerateRequest, service: ServiceDep):
    """Accept the reviewed payload and hand the render off to the worker.

    Returns immediately: the artifact arrives asynchronously and the client
    polls ``/render-status``.
    """
    document, request_id = await service.request_generation(
        document_id=document_id, data=payload.data
    )
    return ApiResponse(
        data=GenerateAccepted(
            document_id=document.id,
            docgen_request_id=request_id,
            render_state=RenderState.PENDING,
        )
    )


@administrative_document_routes.get(
    "/documents/{document_id}/render-status",
    response_model=ApiResponse[RenderStatusOut],
    dependencies=[manager_only],
)
@with_default_error(errors.admin.GET_DOCUMENT)
async def render_status(document_id: int, service: ServiceDep):
    """Poll target. A null ``render_state`` means idle — or that it just landed,
    in which case ``current_version_id`` has moved."""
    status = await service.get_render_status(document_id=document_id)
    return ApiResponse(data=RenderStatusOut(**status))


# ---- document transitions --------------------------------------------------


@administrative_document_routes.post(
    "/documents/{document_id}/transition",
    response_model=ApiResponse[DocumentOut],
    dependencies=[manager_only],
)
@with_default_error(errors.admin.TRANSITION_FAILED)
async def transition_document(document_id: int, body: TransitionRequest, service: ServiceDep):
    document = await service.transition_document(
        document_id, to_status=body.to_status, context=body.context
    )
    return ApiResponse(data=to_document_out(document))


@administrative_document_routes.post(
    "/documents/{document_id}/mark-ready",
    response_model=ApiResponse[DocumentOut],
    dependencies=[manager_only],
)
@with_default_error(errors.admin.TRANSITION_FAILED)
async def mark_document_ready(document_id: int, service: ServiceDep):
    document = await service.transition_document(
        document_id, to_status=int(DocumentStatus.READY), context={}
    )
    return ApiResponse(data=to_document_out(document))


@administrative_document_routes.post(
    "/documents/{document_id}/mark-sent",
    response_model=ApiResponse[DocumentOut],
    dependencies=[manager_only],
)
@with_default_error(errors.admin.TRANSITION_FAILED)
async def mark_document_sent(document_id: int, body: MarkSentRequest, service: ServiceDep):
    context: dict[str, Any] = {"submission_date": body.submission_date}
    if body.note:
        context["note"] = body.note
    document = await service.transition_document(
        document_id, to_status=int(DocumentStatus.SENT), context=context
    )
    return ApiResponse(data=to_document_out(document))


@administrative_document_routes.post(
    "/documents/{document_id}/acknowledge",
    response_model=ApiResponse[DocumentOut],
    dependencies=[manager_only],
)
@with_default_error(errors.admin.TRANSITION_FAILED)
async def acknowledge_document(document_id: int, body: AcknowledgeRequest, service: ServiceDep):
    context: dict[str, Any] = {
        "acknowledged_date": body.acknowledged_date,
        "authority_file_ref": body.authority_file_ref,
        "result": body.result.value,
    }
    if body.note:
        context["note"] = body.note
    document = await service.transition_document(
        document_id, to_status=int(DocumentStatus.ACKNOWLEDGED), context=context
    )
    return ApiResponse(data=to_document_out(document))


def _rollback_context(body: RollbackRequest) -> dict[str, Any]:
    """A corrective transition's context: whatever the target status requires,
    plus the mandatory reason.

    Some corrective edges land on a status that has its own requirement — a
    document rolled back *to* SENT still needs the submission date it is being
    restored to. The typed ``reason`` wins over a duplicate in ``context``; it is
    the one the schema length-validated.
    """
    return {**body.context, "reason": body.reason}


@administrative_document_routes.post(
    "/documents/{document_id}/rollback",
    response_model=ApiResponse[DocumentOut],
    dependencies=[manager_only],
)
@with_default_error(errors.admin.TRANSITION_FAILED)
async def rollback_document(document_id: int, body: RollbackRequest, service: ServiceDep):
    """Correct a mistake by appending a corrective transition. Never rewrites history."""
    document = await service.transition_document(
        document_id, to_status=body.to_status, context=_rollback_context(body)
    )
    return ApiResponse(data=to_document_out(document))


@administrative_document_routes.post(
    "/dossiers/{dossier_id}/rollback",
    response_model=ApiResponse[DossierOut],
    dependencies=[manager_only],
)
@with_default_error(errors.admin.TRANSITION_FAILED)
async def rollback_dossier(dossier_id: int, body: RollbackRequest, service: ServiceDep):
    dossier = await service.transition_dossier(
        dossier_id, to_status=body.to_status, context=_rollback_context(body)
    )
    return ApiResponse(data=to_dossier_out(dossier))


# ---------------------------------------------------------------------------
# "What has been filed about me" — the one member-reachable read
# ---------------------------------------------------------------------------


# NOTE: `/filings/mine` carries NO `manager_only`, unlike every other read in
# this router. Its safety is not the role gate but the row filter in
# `service.my_filings`, which reduces each snapshot to the caller's own rows
# before the payload is built — so the rest of the community never crosses the
# wire (decision B4). Keep the two facts together: loosening the filter without
# adding a gate here silently turns this into the exposure it replaced.
@administrative_document_routes.get("/filings/mine", response_model=ApiResponse[list[MyFilingOut]])
@with_default_error(errors.admin.GET_DOCUMENTS)
async def list_my_filings(
    service: ServiceDep,
    limit: int = Query(default=50, ge=1, le=200),
):
    """Every filing of record that names the caller, with only their own rows."""
    filings = await service.my_filings(id_community=_require_community(), limit=limit)
    return ApiResponse(data=[to_my_filing_out(filing) for filing in filings])


# ---------------------------------------------------------------------------
# Deadlines
# ---------------------------------------------------------------------------


@administrative_document_routes.get(
    "/deadlines",
    response_model=ApiResponsePaginated[list[DeadlineOut]],
    dependencies=[manager_only],
)
@with_default_error(errors.admin.GET_DEADLINES)
async def list_deadlines(
    service: ServiceDep,
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=50, ge=1, le=200),
    status: int | None = Query(default=None),
    deadline_type: str | None = Query(default=None),
    dossier_id: int | None = Query(default=None),
    id_sharing_operation: int | None = Query(
        default=None, description="Restrict to the deadlines of one sharing operation."
    ),
    sort: str | None = Query(default=None),
    order: str | None = Query(default=None),
):
    """The cross-dossier dashboard: what is due, soonest first."""
    rows, total = await service.list_deadlines(
        page=page,
        limit=limit,
        status=status,
        deadline_type=deadline_type,
        dossier_id=dossier_id,
        id_sharing_operation=id_sharing_operation,
        sort=sort,
        order=order,
    )
    return ApiResponsePaginated(
        data=[to_deadline_out(row) for row in rows],
        pagination=_paginate(total, page, limit),
    )


@administrative_document_routes.post(
    "/deadlines/{deadline_id}", response_model=ApiResponse[DeadlineOut], dependencies=[manager_only]
)
@with_default_error(errors.admin.UPDATE_DEADLINE)
async def resolve_deadline(deadline_id: int, body: DeadlineResolveRequest, service: ServiceDep):
    deadline = await service.resolve_deadline(deadline_id, status=body.status)
    return ApiResponse(data=to_deadline_out(deadline))


@administrative_document_routes.post(
    "/maintenance/deadline-sweep",
    response_model=ApiResponse[DeadlineSweepOut],
    dependencies=[manager_only],
)
@with_default_error(errors.admin.DEADLINE_SWEEP_FAILED)
async def deadline_sweep(service: ServiceDep):
    """Flip past-due deadlines to missed and roll recurring ones. Scheduler-driven."""
    missed, rolled = await service.sweep_deadlines()
    return ApiResponse(data=DeadlineSweepOut(missed=missed, rolled=rolled))


# ---------------------------------------------------------------------------
# Registries (reference data, edited as data rather than deployed)
# ---------------------------------------------------------------------------


@administrative_document_routes.get(
    "/templates",
    response_model=ApiResponse[list[TemplateOut]],
    dependencies=[manager_only],
)
@with_default_error(errors.admin.GET_TEMPLATES)
async def list_templates(
    service: ServiceDep,
    region: int | None = Query(default=None),
    doc_type: str | None = Query(default=None),
):
    rows = await service.list_templates(region=region, doc_type=doc_type)
    return ApiResponse(data=[to_template_out(row) for row in rows])


@administrative_document_routes.post(
    "/templates", response_model=ApiResponse[TemplateOut], dependencies=[admin_only]
)
@with_default_error(errors.admin.SAVE_TEMPLATE)
async def create_template(body: TemplateCreate, service: ServiceDep):
    template = await service.create_template(
        region=int(body.region),
        doc_type=body.doc_type,
        version=body.version,
        valid_from=body.valid_from,
        valid_to=body.valid_to,
        file_ref=body.file_ref,
        mapping_json=body.mapping,
        output_format=body.output_format,
        label=body.label,
    )
    return ApiResponse(data=to_template_out(template))


@administrative_document_routes.patch(
    "/templates/{template_id}", response_model=ApiResponse[TemplateOut], dependencies=[admin_only]
)
@with_default_error(errors.admin.SAVE_TEMPLATE)
async def update_template(template_id: int, body: TemplateUpdate, service: ServiceDep):
    template = await service.update_template(
        template_id,
        {
            "valid_to": body.valid_to,
            "file_ref": body.file_ref,
            "mapping_json": body.mapping,
            "label": body.label,
        },
    )
    return ApiResponse(data=to_template_out(template))


@administrative_document_routes.get(
    "/deadline-rules",
    response_model=ApiResponse[list[DeadlineRuleOut]],
    dependencies=[manager_only],
)
@with_default_error(errors.admin.GET_DEADLINE_RULES)
async def list_deadline_rules(service: ServiceDep, region: int | None = Query(default=None)):
    rows = await service.list_deadline_rules(region=region)
    return ApiResponse(data=[to_deadline_rule_out(row) for row in rows])


@administrative_document_routes.post(
    "/deadline-rules", response_model=ApiResponse[DeadlineRuleOut], dependencies=[admin_only]
)
@with_default_error(errors.admin.SAVE_DEADLINE_RULE)
async def create_deadline_rule(body: DeadlineRuleCreate, service: ServiceDep):
    rule = await service.create_deadline_rule(
        region=int(body.region),
        dossier_type=int(body.dossier_type),
        trigger_event=body.trigger_event,
        deadline_type=body.deadline_type,
        offset_value=body.offset_value,
        offset_unit=int(body.offset_unit),
        recurring=body.recurring,
        recur_months=body.recur_months,
        description=body.description,
    )
    return ApiResponse(data=to_deadline_rule_out(rule))


@administrative_document_routes.patch(
    "/deadline-rules/{rule_id}",
    response_model=ApiResponse[DeadlineRuleOut],
    dependencies=[admin_only],
)
@with_default_error(errors.admin.SAVE_DEADLINE_RULE)
async def update_deadline_rule(rule_id: int, body: DeadlineRuleUpdate, service: ServiceDep):
    rule = await service.update_deadline_rule(
        rule_id,
        {
            "offset_value": body.offset_value,
            "offset_unit": int(body.offset_unit) if body.offset_unit is not None else None,
            "recurring": body.recurring,
            "recur_months": body.recur_months,
            "description": body.description,
        },
    )
    return ApiResponse(data=to_deadline_rule_out(rule))
