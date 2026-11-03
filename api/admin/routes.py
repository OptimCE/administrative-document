"""The procedures engine's routes, under ``/admin``
(``/administrative-document/admin/...`` behind the gateway).

Managers and admins of the community only, module active: the same gate as
every state-changing July route.

NOTE: no ``from __future__ import annotations`` here, for the reason given at
the top of ``api/administrative_document/routes.py``.
"""

import datetime
import math
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, Form, Query, Response, UploadFile
from fastapi.responses import JSONResponse

from api.admin.deps import (
    get_admin_service,
    get_digest_settings_service,
    get_inbound_service,
    get_procedure_service,
)
from api.admin.digest import DigestSettingsService
from api.admin.inbound import InboundService
from api.admin.procedures import ProcedureService
from api.admin.schemas import (
    BaselineConfirmIn,
    BaselineOut,
    ChangeExcludeIn,
    ChangeSummaryOut,
    CorrespondenceProposalOut,
    DeadlineItemOut,
    DigestSettingIn,
    DigestSettingOut,
    EffectiveDateIn,
    EntrySheetOut,
    FinalizeOut,
    InboundAddressOut,
    InboundAttachIn,
    InboundMailOut,
    InboxOut,
    PieceFromInboundIn,
    ProcedureCreateIn,
    ProcedureDefinitionOut,
    ProcedureDetailOut,
    ProcedureItemOut,
    RefileIn,
    RefileOut,
    RegenerateIn,
    RegenerateOut,
    SignatureRequestedOut,
    StatusOut,
    SummaryOut,
    TransitionIn,
    VerifyAccepted,
)
from api.admin.service import AdminService, ModuleInactiveError
from core.api_response import ApiResponse, ApiResponsePaginated, Pagination
from core.context_vars import current_internal_community_id, current_locale, current_user_id
from core.errors.errors import ErrorException
from core.errors.with_default_error import with_default_error
from core.i18n import translate
from core.security.community_scope import resolve_internal_community
from core.security.dependencies import require_feature, require_min_role
from core.security.user_context import Role
from engine.baseline import BaselineError, Choice, ChoiceIn
from ports.core_effects import EffectConflictError
from ports.events import EventPublisher
from ports.providers import get_event_publisher
from shared.const import FeatureName
from shared.custom_errors import errors

admin_routes = APIRouter(
    prefix="/admin",
    dependencies=[
        Depends(resolve_internal_community),
        Depends(require_feature(FeatureName.ADMINISTRATIVE_DOCUMENT)),
        Depends(require_min_role(Role.MANAGER)),
    ],
)

ServiceDep = Annotated[AdminService, Depends(get_admin_service)]


def _community() -> int:
    internal_id = current_internal_community_id.get()
    if internal_id is None:
        raise ErrorException(errors.auth.FORBIDDEN, status_code=403)
    return internal_id


@admin_routes.get("/baseline", response_model=ApiResponse[BaselineOut])
@with_default_error(errors.admin.GET_BASELINE)
async def get_baseline(service: ServiceDep):
    """The starting state to confirm, photographed on first sight; or, once
    confirmed, what was chosen. ``not_supported`` when the community's
    regulator has no plugin: no baseline, no tracking."""
    try:
        view = await service.baseline(_community())
    except ModuleInactiveError:
        raise ErrorException(errors.subscription.NOT_SUBSCRIBED, status_code=403) from None
    return ApiResponse(data=BaselineOut.model_validate(view))


@admin_routes.post("/baseline/confirm", response_model=ApiResponse[BaselineOut])
@with_default_error(errors.admin.CONFIRM_BASELINE)
async def confirm_baseline(body: BaselineConfirmIn, service: ServiceDep):
    """Confirm the starting state and write its effects to the core. An item
    left out takes its default. Confirming twice changes nothing.

    409 when the core refuses an EAN already tracked in another operation on
    those dates: ``conflicts`` lists them, and nothing is confirmed."""
    choices = [
        ChoiceIn(
            key=c.key,
            choice=Choice(c.choice),
            effective_date=c.effective_date,
            sent_date=c.sent_date,
        )
        for c in body.choices
    ]
    try:
        view = await service.confirm_baseline(
            _community(), choices, actor=current_user_id.get() or "unknown"
        )
    except ModuleInactiveError:
        raise ErrorException(errors.subscription.NOT_SUBSCRIBED, status_code=403) from None
    except BaselineError:
        raise ErrorException(errors.admin.BASELINE_INVALID, status_code=422) from None
    except EffectConflictError as exc:
        error = errors.admin.BASELINE_CONFLICT
        return JSONResponse(
            status_code=409,
            content={
                "data": translate(error.key, locale=current_locale.get()),
                "error_code": error.code,
                "conflicts": exc.subjects,
            },
        )
    return ApiResponse(data=BaselineOut.model_validate(view))


# ---------------------------------------------------------------------------
# Procedures
# ---------------------------------------------------------------------------

ProceduresDep = Annotated[ProcedureService, Depends(get_procedure_service)]


def _actor() -> str:
    return current_user_id.get() or "unknown"


@admin_routes.get("/status", response_model=ApiResponse[StatusOut])
@with_default_error(errors.admin.GET_PROCEDURES)
async def get_status(service: ProceduresDep):
    """The module for this community: active, regulator followed or not, the
    starting state to confirm. Opens the tracking period on first sight."""
    return ApiResponse(data=StatusOut.model_validate(await service.status(_community())))


@admin_routes.get(
    "/procedure-definitions", response_model=ApiResponse[list[ProcedureDefinitionOut]]
)
@with_default_error(errors.admin.GET_PROCEDURES)
async def list_procedure_definitions(service: ProceduresDep):
    """The regulator's catalogue, every version: steps, responses, deadlines,
    pieces and their labels in four languages."""
    rows = await service.definitions(_community())
    return ApiResponse(data=[ProcedureDefinitionOut.model_validate(r) for r in rows])


@admin_routes.get("/summary", response_model=ApiResponse[SummaryOut])
@with_default_error(errors.admin.GET_PROCEDURES)
async def get_summary(service: ProceduresDep):
    """The navigation's badge: how many procedures wait for the community,
    how many deadlines are past due. Read on every page, so counts only."""
    return ApiResponse(data=SummaryOut.model_validate(await service.summary(_community())))


@admin_routes.get("/changes/{change_id}", response_model=ApiResponse[ChangeSummaryOut])
@with_default_error(errors.admin.GET_PROCEDURES)
async def get_change(change_id: int, service: ProceduresDep):
    """One change and where it was filed, for the CRM's toast (« À notifier à
    ORES : ajouté au brouillon … »). 404 for another community's change."""
    view = await service.change_summary(_community(), change_id)
    return ApiResponse(data=ChangeSummaryOut.model_validate(view))


@admin_routes.get("/inbox", response_model=ApiResponse[InboxOut])
@with_default_error(errors.admin.GET_PROCEDURES)
async def get_inbox(
    service: ProceduresDep,
    operation_id: int | None = None,
    grd: str | None = None,
    bucket: Literal["todo", "waiting", "planned", "done"] | None = None,
):
    """The dashboard: procedures grouped by bucket with their next action and
    its due date (in working days, counted here), and the three soonest
    deadlines."""
    view = await service.inbox(_community(), operation=operation_id, grd=grd, bucket=bucket)
    return ApiResponse(data=InboxOut.model_validate(view))


@admin_routes.get("/deadlines", response_model=ApiResponse[list[DeadlineItemOut]])
@with_default_error(errors.admin.GET_PROCEDURES)
async def list_procedure_deadlines(
    service: ProceduresDep,
    start: Annotated[datetime.date | None, Query(alias="from")] = None,
    end: Annotated[datetime.date | None, Query(alias="to")] = None,
):
    """The agenda. Without a range: every open deadline."""
    rows = await service.deadlines(_community(), start=start, end=end)
    return ApiResponse(data=[DeadlineItemOut.model_validate(r) for r in rows])


@admin_routes.get("/dossiers", response_model=ApiResponsePaginated[list[ProcedureItemOut]])
@with_default_error(errors.admin.GET_PROCEDURES)
async def list_procedures(
    service: ProceduresDep,
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=20, ge=1, le=200),
    status: int | None = Query(default=None, description="ProcedureStatus"),
    code: str | None = Query(default=None, max_length=64),
    operation_id: int | None = None,
):
    """Every procedure, newest first, finished ones included: the dashboard
    keeps those for a month only."""
    rows, total = await service.procedures(
        _community(), page=page, limit=limit, status=status, code=code, operation=operation_id
    )
    return ApiResponsePaginated(
        data=[ProcedureItemOut.model_validate(r) for r in rows],
        pagination=Pagination(
            page=page,
            limit=limit,
            total=total,
            total_pages=math.ceil(total / limit),
        ),
    )


@admin_routes.get("/dossiers/{dossier_id}", response_model=ApiResponse[ProcedureDetailOut])
@with_default_error(errors.admin.GET_PROCEDURES)
async def get_procedure(dossier_id: int, service: ProceduresDep):
    """One procedure: header, timeline, changes, pieces, deadlines, thread."""
    view = await service.procedure(_community(), dossier_id)
    return ApiResponse(data=ProcedureDetailOut.model_validate(view))


@admin_routes.post("/dossiers", response_model=ApiResponse[ProcedureDetailOut])
@with_default_error(errors.admin.PROCEDURE_ACTION_FAILED)
async def create_procedure(body: ProcedureCreateIn, service: ProceduresDep):
    """A procedure opened by hand from the catalogue (an end of sharing, a
    procedure the CRM does not trigger yet)."""
    view = await service.create(
        _community(),
        code=body.code,
        id_sharing_operation=body.id_sharing_operation,
        requested_effective_date=body.requested_effective_date,
        actor=_actor(),
    )
    return ApiResponse(data=ProcedureDetailOut.model_validate(view))


@admin_routes.post(
    "/dossiers/{dossier_id}/changes/{change_id}/defer",
    response_model=ApiResponse[ProcedureDetailOut],
)
@with_default_error(errors.admin.PROCEDURE_ACTION_FAILED)
async def defer_change(dossier_id: int, change_id: int, service: ProceduresDep):
    """Take a change out of this procedure; it joins the next one of its type
    once this one is sent or cancelled."""
    view = await service.defer_change(_community(), dossier_id, change_id, actor=_actor())
    return ApiResponse(data=ProcedureDetailOut.model_validate(view))


@admin_routes.post(
    "/dossiers/{dossier_id}/changes/{change_id}/exclude",
    response_model=ApiResponse[ProcedureDetailOut],
)
@with_default_error(errors.admin.PROCEDURE_ACTION_FAILED)
async def exclude_change(
    dossier_id: int, change_id: int, body: ChangeExcludeIn, service: ProceduresDep
):
    """Decide a change needs no procedure, with the reason (audited)."""
    view = await service.exclude_change(
        _community(), dossier_id, change_id, reason=body.reason, actor=_actor()
    )
    return ApiResponse(data=ProcedureDetailOut.model_validate(view))


@admin_routes.post("/dossiers/{dossier_id}/refile", response_model=ApiResponse[RefileOut])
@with_default_error(errors.admin.PROCEDURE_ACTION_FAILED)
async def refile_changes(dossier_id: int, body: RefileIn, service: ProceduresDep):
    """« Corriger et renvoyer »: file a refused (or lapsed) procedure's changes
    again, as new changes, into the family's open draft (created if none). The
    refused procedure keeps its own, refused, as they were. 409 (2392) for a
    change that cannot be refiled, (2393) when the CRM no longer holds it so."""
    refiled = await service.refile(
        _community(), dossier_id, change_ids=body.change_ids, actor=_actor()
    )
    return ApiResponse(data=RefileOut.model_validate(refiled))


@admin_routes.post(
    "/dossiers/{dossier_id}/transition", response_model=ApiResponse[ProcedureDetailOut]
)
@with_default_error(errors.admin.PROCEDURE_ACTION_FAILED)
async def transition_procedure(dossier_id: int, body: TransitionIn, service: ProceduresDep):
    """A step change by hand, along one of the catalogue's edges, with its
    reason (journaled). Into force, the core is written first, on ``on`` (or
    the effect date): a 409 (2391) lists the EANs it refused, and the
    procedure stays where it was."""
    try:
        view = await service.transition(
            _community(),
            dossier_id,
            to_step=body.to_step,
            reason=body.reason,
            on=body.on,
            actor=_actor(),
        )
    except EffectConflictError as exc:
        return _effect_conflict(exc)
    return ApiResponse(data=ProcedureDetailOut.model_validate(view))


@admin_routes.post(
    "/dossiers/{dossier_id}/effective-date", response_model=ApiResponse[ProcedureDetailOut]
)
@with_default_error(errors.admin.PROCEDURE_ACTION_FAILED)
async def correct_effective_date(dossier_id: int, body: EffectiveDateIn, service: ProceduresDep):
    """Correct the effect date by hand, with its reason: the GRD took effect
    from another day than the one counted. Only while the effect is still to
    come (409, 2397, otherwise); the daily job applies it when it comes."""
    view = await service.correct_effective_date(
        _community(), dossier_id, on=body.on, reason=body.reason, actor=_actor()
    )
    return ApiResponse(data=ProcedureDetailOut.model_validate(view))


def _effect_conflict(exc: EffectConflictError) -> JSONResponse:
    error = errors.admin.EFFECT_CONFLICT
    return JSONResponse(
        status_code=409,
        content={
            "data": translate(error.key, locale=current_locale.get()),
            "error_code": error.code,
            "conflicts": exc.subjects,
        },
    )


@admin_routes.post(
    "/dossiers/{dossier_id}/documents/{document_id}/versions",
    response_model=ApiResponse[ProcedureDetailOut],
)
@with_default_error(errors.admin.UPLOAD_VERSION_FAILED)
async def upload_piece(
    dossier_id: int,
    document_id: int,
    service: ProceduresDep,
    file: Annotated[UploadFile, File()],
):
    """Upload a piece: a signed declaration, a document received. Refused once
    the procedure was sent, except for a piece that is received after it."""
    view = await service.upload_piece(_community(), dossier_id, document_id, file, actor=_actor())
    return ApiResponse(data=ProcedureDetailOut.model_validate(view))


@admin_routes.post(
    "/dossiers/{dossier_id}/documents/{document_id}/signature-request",
    status_code=202,
    response_model=ApiResponse[SignatureRequestedOut],
)
@with_default_error(errors.admin.PROCEDURE_ACTION_FAILED)
async def request_signature(
    dossier_id: int,
    document_id: int,
    service: ProceduresDep,
    publisher: Annotated[EventPublisher, Depends(get_event_publisher)],
):
    """Ask the participant to sign a per-member collected piece in the app (the
    signature annexe). Its current version must be the one to sign; the
    signed PDF comes back as the piece's final version. 409: module off (2600),
    not signable (2601), no account (2602), already asked (2603)."""
    result = await service.request_signature(
        _community(),
        dossier_id,
        document_id,
        actor=_actor(),
        locale=current_locale.get(),
        publisher=publisher,
    )
    return ApiResponse(data=SignatureRequestedOut.model_validate(result))


@admin_routes.get(
    "/dossiers/{dossier_id}/entry-sheet", response_model=ApiResponse[list[EntrySheetOut]]
)
@with_default_error(errors.admin.GET_PROCEDURES)
async def get_entry_sheets(dossier_id: int, service: ProceduresDep):
    """The fiches de saisie of the procedure's form pieces: each value in the
    order of the regulator's form, from the piece's current version. Empty
    when no piece has one."""
    sheets = await service.entry_sheets(_community(), dossier_id)
    return ApiResponse(data=[EntrySheetOut.model_validate(s) for s in sheets])


@admin_routes.get(
    "/dossiers/{dossier_id}/finals.zip",
    responses={200: {"content": {"application/zip": {}}}},
)
@with_default_error(errors.admin.STORAGE_DOWNLOAD_FAILED)
async def download_finals(dossier_id: int, service: ProceduresDep):
    """Every supplied piece's final in one archive, with ``SHA256SUMS.txt``.
    409 while a piece is still a draft or rendering."""
    name, content = await service.finals_zip(_community(), dossier_id)
    return Response(
        content=content,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@admin_routes.post(
    "/dossiers/{dossier_id}/finalize", status_code=202, response_model=ApiResponse[FinalizeOut]
)
@with_default_error(errors.admin.GENERATION_FAILED)
async def finalize_procedure(dossier_id: int, service: ProceduresDep):
    """Render the finals before sending: each generated piece whose current
    version is a reviewed draft, again from that draft's snapshot and template,
    without the watermark. 409 unless the pieces are complete and it is not
    sent yet. Sending is refused until every supplied piece is final."""
    outcome = await service.finalize(_community(), dossier_id)
    return ApiResponse(data=FinalizeOut.model_validate(outcome))


@admin_routes.post(
    "/dossiers/{dossier_id}/regenerate",
    status_code=202,
    response_model=ApiResponse[RegenerateOut],
)
@with_default_error(errors.admin.GENERATION_FAILED)
async def regenerate_pieces(dossier_id: int, body: RegenerateIn, service: ProceduresDep):
    """One click: regenerate the stale pieces (or the ones named) from a fresh
    pre-fill, the reviewer's own edits of each current version carried over."""
    outcome = await service.regenerate(_community(), dossier_id, body.document_ids)
    return ApiResponse(data=RegenerateOut.model_validate(outcome))


@admin_routes.post(
    "/dossiers/{dossier_id}/documents/{document_id}/versions/{version_id}/verify",
    status_code=202,
    response_model=ApiResponse[VerifyAccepted],
)
@with_default_error(errors.admin.GENERATION_FAILED)
async def verify_version(
    dossier_id: int, document_id: int, version_id: int, service: ProceduresDep
):
    """Regenerate a rendered version from what it froze (snapshot, template,
    watermark) and compare the bytes with the stored ones. Nothing new is
    stored but the outcome; allowed on a sent procedure."""
    request_id = await service.verify(_community(), dossier_id, document_id, version_id)
    return ApiResponse(data=VerifyAccepted(docgen_request_id=request_id))


@admin_routes.post("/dossiers/{dossier_id}/send", response_model=ApiResponse[ProcedureDetailOut])
@with_default_error(errors.admin.PROCEDURE_ACTION_FAILED)
async def send_procedure(
    dossier_id: int,
    service: ProceduresDep,
    sent_on: Annotated[datetime.date | None, Form()] = None,
    channel: Annotated[Literal["email", "online_form", "post"] | None, Form()] = None,
    subject: Annotated[str | None, Form(max_length=255)] = None,
    body_text: Annotated[str | None, Form()] = None,
    file: Annotated[UploadFile | None, File()] = None,
):
    """Mark as sent: the sending as it was done (date, channel, optionally the
    .eml sent or the online form's receipt). Freezes the procedure."""
    view = await service.send(
        _community(),
        dossier_id,
        on=sent_on,
        channel=channel,
        subject=subject,
        body_text=body_text,
        file=file,
        actor=_actor(),
    )
    return ApiResponse(data=ProcedureDetailOut.model_validate(view))


@admin_routes.post(
    "/dossiers/{dossier_id}/correspondence",
    response_model=ApiResponse[ProcedureDetailOut | CorrespondenceProposalOut],
)
@with_default_error(errors.admin.PROCEDURE_ACTION_FAILED)
async def record_correspondence(
    dossier_id: int,
    service: ProceduresDep,
    type: Annotated[str, Form(max_length=64)],
    occurred_on: Annotated[datetime.date | None, Form()] = None,
    subject: Annotated[str | None, Form(max_length=255)] = None,
    body_text: Annotated[str | None, Form()] = None,
    external_ref: Annotated[str | None, Form(max_length=128)] = None,
    file: Annotated[UploadFile | None, File()] = None,
    inbound_mail_id: Annotated[int | None, Form()] = None,
    dry_run: bool = False,
):
    """Add a response. With dry_run=true nothing is written: the answer says
    which step change and which deadlines recording it would make.

    ``inbound_mail_id``: record it from a mail the reception address received
    (its .eml as the file, its subject, text and date as defaults)."""
    view = await service.respond(
        _community(),
        dossier_id,
        type_=type,
        on=occurred_on,
        subject=subject,
        body_text=body_text,
        external_ref=external_ref,
        file=file,
        dry_run=dry_run,
        actor=_actor(),
        inbound_mail_id=inbound_mail_id,
    )
    if dry_run:
        return ApiResponse(data=CorrespondenceProposalOut.model_validate(view))
    return ApiResponse(data=ProcedureDetailOut.model_validate(view))


@admin_routes.get(
    "/dossiers/{dossier_id}/correspondence/{correspondence_id}/file",
    responses={200: {"content": {"application/octet-stream": {}}}},
)
@with_default_error(errors.admin.STORAGE_DOWNLOAD_FAILED)
async def download_correspondence(dossier_id: int, correspondence_id: int, service: ProceduresDep):
    """The file of a message, exactly as it was recorded."""
    message, content = await service.correspondence_file(
        _community(), dossier_id, correspondence_id
    )
    name = message.original_filename or f"message-{message.id}"
    name = "".join(ch for ch in name if ch.isprintable() and ch not in '"\\\r\n')[:200]
    return Response(
        content=content,
        media_type=message.content_type or "application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{name or "download"}"'},
    )


# ---------------------------------------------------------------------------
# The reception address (phase 4)
# ---------------------------------------------------------------------------

InboundDep = Annotated[InboundService, Depends(get_inbound_service)]


def _download(content: bytes, media_type: str | None, name: str) -> Response:
    safe = "".join(ch for ch in name if ch.isprintable() and ch not in '"\\\r\n')[:200]
    return Response(
        content=content,
        media_type=media_type or "application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{safe or "download"}"'},
    )


@admin_routes.get("/inbound-address", response_model=ApiResponse[InboundAddressOut])
@with_default_error(errors.admin.GET_PROCEDURES)
async def get_inbound_address(service: InboundDep):
    """The community's reception address, to forward the GRD's and the CWaPE's
    answers to (its code is made on first sight). ``enabled`` false: none on
    this instance."""
    view = await service.address(_community(), actor=_actor())
    return ApiResponse(data=InboundAddressOut.model_validate(view))


@admin_routes.post(
    "/inbound-address/rotate",
    response_model=ApiResponse[InboundAddressOut],
    dependencies=[Depends(require_min_role(Role.ADMIN))],
)
@with_default_error(errors.admin.PROCEDURE_ACTION_FAILED)
async def rotate_inbound_address(service: InboundDep):
    """A new code for the address (admins): the old address reaches nobody
    from now on."""
    view = await service.rotate(_community(), actor=_actor())
    return ApiResponse(data=InboundAddressOut.model_validate(view))


DigestDep = Annotated[DigestSettingsService, Depends(get_digest_settings_service)]


@admin_routes.get("/digest-settings", response_model=ApiResponse[DigestSettingOut])
@with_default_error(errors.admin.GET_PROCEDURES)
async def get_digest_settings(service: DigestDep):
    """The community's summary email: off by default; when on, its rhythm,
    its recipients (and who of them stopped it in their profile) and the next
    send."""
    view = await service.get(_community())
    return ApiResponse(data=DigestSettingOut.model_validate(view))


@admin_routes.put(
    "/digest-settings",
    response_model=ApiResponse[DigestSettingOut],
    dependencies=[Depends(require_min_role(Role.ADMIN))],
)
@with_default_error(errors.admin.PROCEDURE_ACTION_FAILED)
async def put_digest_settings(body: DigestSettingIn, service: DigestDep):
    """Turn the summary email on or off, set its rhythm and its recipients
    (admins). 422 (2398) when the setting does not hold together."""
    view = await service.save(
        _community(),
        enabled=body.enabled,
        cadence=body.cadence,
        weekday=body.weekday,
        first_on=body.first_on,
        recipient_ids=body.recipient_ids,
        actor=_actor(),
    )
    return ApiResponse(data=DigestSettingOut.model_validate(view))


@admin_routes.get("/inbound-mail", response_model=ApiResponse[list[InboundMailOut]])
@with_default_error(errors.admin.GET_PROCEDURES)
async def list_inbound_mail(
    service: InboundDep,
    status: Literal["open", "unmatched", "attached", "recorded", "discarded", "all"] = "open",
    id_dossier: int | None = None,
):
    """What the reception address received, newest first: ``open`` (the
    default) is what waits for a manager, unmatched or to qualify."""
    rows = await service.mails(_community(), status=status, dossier_id=id_dossier)
    return ApiResponse(data=[InboundMailOut.model_validate(r) for r in rows])


@admin_routes.post("/inbound-mail/{mail_id}/attach", response_model=ApiResponse[InboundMailOut])
@with_default_error(errors.admin.PROCEDURE_ACTION_FAILED)
async def attach_inbound_mail(mail_id: int, body: InboundAttachIn, service: InboundDep):
    """Attach a mail no reference placed to one of the community's procedures."""
    view = await service.attach(_community(), mail_id, body.id_dossier, actor=_actor())
    return ApiResponse(data=InboundMailOut.model_validate(view))


@admin_routes.post("/inbound-mail/{mail_id}/discard", response_model=ApiResponse[InboundMailOut])
@with_default_error(errors.admin.PROCEDURE_ACTION_FAILED)
async def discard_inbound_mail(mail_id: int, service: InboundDep):
    """Set a mail aside: not a procedure's (its file stays)."""
    view = await service.discard(_community(), mail_id, actor=_actor())
    return ApiResponse(data=InboundMailOut.model_validate(view))


@admin_routes.get(
    "/inbound-mail/{mail_id}/file", responses={200: {"content": {"message/rfc822": {}}}}
)
@with_default_error(errors.admin.STORAGE_DOWNLOAD_FAILED)
async def download_inbound_mail(mail_id: int, service: InboundDep):
    """The mail exactly as it was received (.eml)."""
    mail, content = await service.mail_file(_community(), mail_id)
    return _download(content, "message/rfc822", f"message-{mail.id}.eml")


@admin_routes.get(
    "/inbound-mail/{mail_id}/attachments/{attachment_id}/file",
    responses={200: {"content": {"application/octet-stream": {}}}},
)
@with_default_error(errors.admin.STORAGE_DOWNLOAD_FAILED)
async def download_inbound_attachment(mail_id: int, attachment_id: int, service: InboundDep):
    """One attachment of a received mail, exactly as it was received."""
    attachment, content = await service.attachment_file(_community(), mail_id, attachment_id)
    return _download(content, attachment.content_type, attachment.filename)


@admin_routes.post(
    "/dossiers/{dossier_id}/documents/{document_id}/versions/from-inbound",
    response_model=ApiResponse[ProcedureDetailOut],
)
@with_default_error(errors.admin.UPLOAD_VERSION_FAILED)
async def piece_from_inbound(
    dossier_id: int, document_id: int, body: PieceFromInboundIn, service: ProceduresDep
):
    """An attachment of a mail received for this procedure, as a new version
    of one of its pieces (« Utiliser comme pièce »)."""
    view = await service.piece_from_inbound(
        _community(),
        dossier_id,
        document_id,
        mail_id=body.id_inbound_mail,
        attachment_id=body.id_attachment,
        actor=_actor(),
    )
    return ApiResponse(data=ProcedureDetailOut.model_validate(view))
