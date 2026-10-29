"""Wiring for the ``/admin`` routes. Providers are plain functions so a test can
override one (``app.dependency_overrides``) without the rest."""

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from api.admin.procedures import ProcedureService
from api.admin.service import AdminService
from core.database.database import get_crm_session, get_local_session
from engine.contracts import PluginRegistry
from ports.core_effects_sqlalchemy import SqlCoreEffects
from ports.crm_engine import SqlEngineCrmRead
from regulators.registry import REGISTRY


def get_registry() -> PluginRegistry:
    return REGISTRY


def get_admin_service(
    local_session: AsyncSession = Depends(get_local_session),
    crm_session: AsyncSession = Depends(get_crm_session),
    registry: PluginRegistry = Depends(get_registry),
) -> AdminService:
    return AdminService(
        local_session=local_session,
        crm_session=crm_session,
        crm_read=SqlEngineCrmRead(crm_session),
        effects=SqlCoreEffects(crm_session),
        registry=registry,
    )


def get_procedure_service(
    local_session: AsyncSession = Depends(get_local_session),
    crm_session: AsyncSession = Depends(get_crm_session),
    registry: PluginRegistry = Depends(get_registry),
) -> ProcedureService:
    return ProcedureService(
        local_session=local_session,
        crm_session=crm_session,
        crm_read=SqlEngineCrmRead(crm_session),
        registry=registry,
    )
