"""The procedures engine's routes, under ``/admin``
(``/administrative-document/admin/...`` behind the gateway).

Managers and admins of the community only, module active: the same gate as
every state-changing July route.

NOTE: no ``from __future__ import annotations`` here, for the reason given at
the top of ``api/administrative_document/routes.py``.
"""

from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from api.admin.deps import get_admin_service
from api.admin.schemas import BaselineConfirmIn, BaselineOut
from api.admin.service import AdminService, ModuleInactiveError
from core.api_response import ApiResponse
from core.context_vars import current_internal_community_id, current_locale, current_user_id
from core.errors.errors import ErrorException
from core.errors.with_default_error import with_default_error
from core.i18n import translate
from core.security.community_scope import resolve_internal_community
from core.security.dependencies import require_feature, require_min_role
from core.security.user_context import Role
from engine.baseline import BaselineError, Choice, ChoiceIn
from ports.core_effects import EffectConflictError
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
