"""HTTP routes of the administrative-document annexe outside ``/admin``.

What a procedure's pieces need from the CRM and the renderer (prefill,
generate, render status, a version's file), what a member may read about
themselves (``/filings/mine``), the template registry, and the community's
sharing operations. A procedure's lifecycle lives under ``/admin``
(``api/admin/routes.py``).

NOTE: this module must NOT use ``from __future__ import annotations``. The
``with_default_error`` wrapper resolves string annotations against its own module
globals, so stringified Pydantic body types become invisible and FastAPI demotes
them to query parameters (422 "Field required, loc: [query, body]"). Keep real
annotation objects here.

Access model: the router requires an authenticated caller with an active
subscription. Every route needs MANAGER except ``/filings/mine``, and the
template registry's writes need ADMIN.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response

from api.administrative_document.deps import get_administrative_document_service
from api.administrative_document.mappers import (
    to_my_filing_out,
    to_prefill_warning_out,
    to_template_out,
)
from api.administrative_document.schemas import (
    GenerateAccepted,
    GenerateRequest,
    MyFilingOut,
    PrefillOut,
    RenderStatusOut,
    SharingOperationOut,
    TemplateCreate,
    TemplateOut,
    TemplateUpdate,
)
from api.administrative_document.service import AdministrativeDocumentService
from core.api_response import ApiResponse
from core.context_vars import current_internal_community_id
from core.errors.errors import ErrorException
from core.errors.with_default_error import with_default_error
from core.security.community_scope import resolve_internal_community
from core.security.dependencies import require_feature, require_min_role
from core.security.user_context import Role
from shared.const import (
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
    """The community's sharing operations: the operation-scope procedures' subjects."""
    rows = await service.list_sharing_operations(_require_community())
    return ApiResponse(data=[SharingOperationOut(id=row.id, name=row.name) for row in rows])


# ---------------------------------------------------------------------------
# Pieces: a version's file, and generation
# ---------------------------------------------------------------------------


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


# ---- generation -------------------------------------------------------------
#
# Three steps: read the CRM into a form (prefill), submit the corrected form
# (generate), watch it land (render-status). A render is a technical operation,
# never a step of the procedure; a landed one makes the piece ready.


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
        document_id=document_id, data=payload.data, upgrade_template=payload.upgrade_template
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


# ---------------------------------------------------------------------------
# What has been filed about me
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
# Template registry (reference data, edited as data rather than deployed)
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
