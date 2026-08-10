"""FastAPI dependency wiring for the administrative-document API.

Plain ``Depends`` assembly, no container — the same shape as the other OptimCE
annexes. Ports are injected through trivial provider functions so a test can
override them without a broker or a second database.
"""

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from api.administrative_document.repository import AdministrativeDocumentRepository
from api.administrative_document.service import AdministrativeDocumentService
from core.database.database import get_crm_session, get_local_session
from ports.crm_core_sqlalchemy import SqlAlchemyCrmCoreRead
from ports.events import EventPublisher

# Which adapter backs the port is decided in `ports/providers.py`, not here, so
# the worker can make the same conditional choice without importing fastapi.
# Re-exported because `dependency_overrides[deps.get_event_publisher]` keys on
# this object.
from ports.providers import get_event_publisher

__all__ = ["get_administrative_document_service", "get_event_publisher"]


def get_administrative_document_service(
    local_session: AsyncSession = Depends(get_local_session),
    crm_session: AsyncSession = Depends(get_crm_session),
    publisher: EventPublisher = Depends(get_event_publisher),
) -> AdministrativeDocumentService:
    return AdministrativeDocumentService(
        local_session=local_session,
        crm_session=crm_session,
        repository=AdministrativeDocumentRepository(local_session),
        crm_read=SqlAlchemyCrmCoreRead(crm_session),
        publisher=publisher,
    )
