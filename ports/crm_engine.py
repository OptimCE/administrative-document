"""What the procedures engine reads from the CRM, and nothing else.

Two reads, both raw SQL on the CRM session like ``crm_core_sqlalchemy.py``:

- the community's regulator and whether this module is active for it. Read
  on EVERY event, never cached: a deactivation must stop the very next event
  (spec, "Activation du module"). ``require_feature`` cannot serve here: it is
  a FastAPI dependency reading request ContextVars, and a consumer has neither.
- which subjects the core already tracks in ``regulatory_link_status`` (the
  authorised state), so an event that changes nothing is recognised.
"""

from __future__ import annotations

import datetime
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

from shared.const import FeatureName, RegulatoryStatus

# The statuses in which a subject counts as part of the operation, the same set
# the core's EXCLUDE constraint guards.
TRACKED_STATUSES = (
    RegulatoryStatus.PENDING_GRD,
    RegulatoryStatus.PENDING_REGULATOR,
    RegulatoryStatus.AUTHORIZED,
    RegulatoryStatus.ACTIVE,
)


@dataclass(frozen=True)
class CommunityModuleState:
    exists: bool
    regulator: str | None = None
    active: bool = False
    # community_subscription.updated_at: when the subscription last changed,
    # the best the CRM can say about when it was switched on or off.
    changed_at: datetime.datetime | None = None


class EngineCrmRead(Protocol):
    async def module_state(self, id_community: int) -> CommunityModuleState: ...

    async def tracked_subjects(
        self,
        *,
        id_community: int,
        id_sharing_operation: int,
        subject_type: int,
        refs: Sequence[str],
        on: datetime.date,
    ) -> set[str]:
        """The ``refs`` the core tracks in this operation on day ``on``."""
        ...


_MODULE_STATE_SQL = text(
    "SELECT c.regulator, s.is_active, s.updated_at "
    "FROM community c "
    "LEFT JOIN community_subscription s "
    "  ON s.id_community = c.id AND s.feature = :feature "
    "WHERE c.id = :id_community"
)

_TRACKED_SQL = text(
    "SELECT DISTINCT subject_ref FROM regulatory_link_status "
    "WHERE id_community = :id_community AND id_sharing_operation = :id_sharing_operation "
    "  AND subject_type = :subject_type AND subject_ref IN :refs "
    "  AND regulatory_status IN :statuses "
    "  AND (valid_from IS NULL OR valid_from <= :on) "
    "  AND (valid_to IS NULL OR valid_to >= :on)"
).bindparams(bindparam("refs", expanding=True), bindparam("statuses", expanding=True))


class SqlEngineCrmRead:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def module_state(self, id_community: int) -> CommunityModuleState:
        row = (
            (
                await self._session.execute(
                    _MODULE_STATE_SQL,
                    {
                        "id_community": id_community,
                        "feature": str(FeatureName.ADMINISTRATIVE_DOCUMENT),
                    },
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return CommunityModuleState(exists=False)
        return CommunityModuleState(
            exists=True,
            regulator=row["regulator"],
            active=bool(row["is_active"]),
            changed_at=row["updated_at"],
        )

    async def tracked_subjects(
        self,
        *,
        id_community: int,
        id_sharing_operation: int,
        subject_type: int,
        refs: Sequence[str],
        on: datetime.date,
    ) -> set[str]:
        if not refs:
            return set()
        result = await self._session.execute(
            _TRACKED_SQL,
            {
                "id_community": id_community,
                "id_sharing_operation": id_sharing_operation,
                "subject_type": subject_type,
                "refs": list(refs),
                "statuses": [int(s) for s in TRACKED_STATUSES],
                "on": on,
            },
        )
        return {str(ref) for ref in result.scalars()}
