"""What the procedures engine reads from the CRM, and nothing else.

Raw SQL on the CRM session, like ``crm_core_sqlalchemy.py``:

- the community's regulator and whether this module is active for it. Read
  on EVERY event, never cached: a deactivation must stop the very next event
  (spec, "Activation du module"). ``require_feature`` cannot serve here: it is
  a FastAPI dependency reading request ContextVars, and a consumer has neither.
- which subjects the core already tracks in ``regulatory_link_status`` (the
  authorised state), so an event that changes nothing is recognised, and the
  episodes themselves, which ``engine.effects`` compares its plan with;
- the photo the baseline is built from: each operation, its current meters,
  its approved key.
"""

from __future__ import annotations

import datetime
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

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


@dataclass(frozen=True)
class PhotoMeter:
    id_sharing_operation: int
    ean: str
    id_member: int | None
    grd: str | None
    injection_status: int | None
    start_date: datetime.date | None


@dataclass(frozen=True)
class PhotoKey:
    id_sharing_operation: int
    id_key: int
    name: str
    start_date: datetime.date


@dataclass(frozen=True)
class CommunityPhoto:
    operations: dict[int, str] = field(default_factory=dict)  # id -> name
    meters: tuple[PhotoMeter, ...] = ()
    keys: tuple[PhotoKey, ...] = ()


@dataclass(frozen=True)
class Episode:
    """A ``regulatory_link_status`` row: what the core holds as authorised."""

    id: int
    id_sharing_operation: int
    subject_type: int
    subject_ref: str
    id_dossier_origin: int
    regulatory_status: int
    valid_from: datetime.date | None
    valid_to: datetime.date | None
    regulatory_ref: str | None = None

    @property
    def key(self) -> tuple[int, int, str]:
        """(origin, subject type, subject ref): the row's identity, UNIQUE."""
        return (self.id_dossier_origin, self.subject_type, self.subject_ref)


class EngineCrmRead(Protocol):
    async def module_state(self, id_community: int) -> CommunityModuleState: ...

    async def community_photo(self, id_community: int, *, on: datetime.date) -> CommunityPhoto:
        """The operations, their meters and approved keys on day ``on``."""
        ...

    async def episodes(self, id_community: int, *, statuses: Sequence[int]) -> list[Episode]:
        """The community's episodes in one of ``statuses``."""
        ...

    async def communities_with_module(self) -> list[int]:
        """Every community whose subscription to this module is active: what
        the daily jobs run for."""
        ...

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

    async def episode_rows(
        self, id_community: int, keys: Sequence[tuple[int, int, str]]
    ) -> dict[tuple[int, int, str], Episode]:
        """The episodes of these (origin, subject type, subject ref) identities
        that exist, by identity."""
        ...

    async def live_episode(
        self, id_community: int, *, id_sharing_operation: int, subject_type: int, subject_ref: str
    ) -> Episode | None:
        """The newest episode of a subject in an operation still to notify,
        pending, authorised, active or leaving: what a removal moves."""
        ...

    async def active_keys(self, id_community: int, id_sharing_operation: int) -> list[Episode]:
        """The operation's key episodes that are active: what a new key ends."""
        ...

    async def meters_in_operation(
        self,
        id_community: int,
        id_sharing_operation: int,
        eans: Sequence[str],
        *,
        on: datetime.date,
    ) -> set[str]:
        """The ``eans`` the CRM attributes to the operation on day ``on``, at
        any status but inactive (one waiting for its GRD is there)."""
        ...

    async def current_key(
        self, id_community: int, id_sharing_operation: int, *, on: datetime.date
    ) -> int | None:
        """The operation's approved key on day ``on``."""
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


_OPERATIONS_SQL = text(
    "SELECT id, name FROM sharing_operation WHERE id_community = :id_community ORDER BY id"
)

# One row per meter: its attribution on day `on`, to an operation of the same
# community. meter_data is a dated history; the latest slice covering `on` wins.
_PHOTO_METERS_SQL = text(
    "SELECT DISTINCT ON (md.ean) md.id_sharing_operation, md.ean, md.id_member, md.grd, "
    "       md.injection_status, md.start_date "
    "FROM meter_data md "
    "JOIN sharing_operation so "
    "  ON so.id = md.id_sharing_operation AND so.id_community = md.id_community "
    "WHERE md.id_community = :id_community AND md.status = :active "
    "  AND (md.start_date IS NULL OR md.start_date <= :on) "
    "  AND (md.end_date IS NULL OR md.end_date >= :on) "
    "ORDER BY md.ean, md.start_date DESC NULLS LAST, md.id DESC"
)

# The approved key of each operation on day `on` (sharing_operation_key.status 1).
_PHOTO_KEYS_SQL = text(
    "SELECT DISTINCT ON (sok.id_sharing_operation) sok.id_sharing_operation, sok.id_key, "
    "       ak.name, sok.start_date "
    "FROM sharing_operation_key sok "
    "JOIN allocation_key ak ON ak.id = sok.id_key "
    "WHERE sok.id_community = :id_community AND sok.status = 1 "
    "  AND sok.start_date <= :on AND (sok.end_date IS NULL OR sok.end_date >= :on) "
    "ORDER BY sok.id_sharing_operation, sok.start_date DESC, sok.id DESC"
)

_ACTIVE_COMMUNITIES_SQL = text(
    "SELECT s.id_community FROM community_subscription s "
    "JOIN community c ON c.id = s.id_community "
    "WHERE s.feature = :feature AND s.is_active ORDER BY s.id_community"
)

_EPISODE_COLUMNS = (
    "id, id_sharing_operation, subject_type, subject_ref, id_dossier_origin, "
    "regulatory_status, valid_from, valid_to, regulatory_ref "
)

_EPISODES_SQL = text(
    f"SELECT {_EPISODE_COLUMNS}FROM regulatory_link_status "
    "WHERE id_community = :id_community AND regulatory_status IN :statuses "
    "ORDER BY id"
).bindparams(bindparam("statuses", expanding=True))

# Narrowed by origin and ref in SQL, matched on the full identity in Python.
_EPISODE_ROWS_SQL = text(
    f"SELECT {_EPISODE_COLUMNS}FROM regulatory_link_status "
    "WHERE id_community = :id_community AND id_dossier_origin IN :origins "
    "  AND subject_ref IN :refs"
).bindparams(bindparam("origins", expanding=True), bindparam("refs", expanding=True))

_LIVE_EPISODE_SQL = text(
    f"SELECT {_EPISODE_COLUMNS}FROM regulatory_link_status "
    "WHERE id_community = :id_community AND id_sharing_operation = :id_sharing_operation "
    "  AND subject_type = :subject_type AND subject_ref = :subject_ref "
    "  AND regulatory_status IN :statuses "
    "ORDER BY id DESC LIMIT 1"
).bindparams(bindparam("statuses", expanding=True))

_ACTIVE_KEYS_SQL = text(
    f"SELECT {_EPISODE_COLUMNS}FROM regulatory_link_status "
    "WHERE id_community = :id_community AND id_sharing_operation = :id_sharing_operation "
    "  AND subject_type = 2 AND regulatory_status = :active "
    "ORDER BY id"
)

# meter_data.status of an active attribution (MeterDataStatus.ACTIVE), and of
# one the meter left (MeterDataStatus.INACTIVE).
_ACTIVE_METER_DATA = 1
_INACTIVE_METER_DATA = 2

_IN_OPERATION_SQL = text(
    "SELECT DISTINCT md.ean FROM meter_data md "
    "WHERE md.id_community = :id_community AND md.id_sharing_operation = :id_sharing_operation "
    "  AND md.ean IN :eans AND md.status <> :inactive "
    "  AND (md.start_date IS NULL OR md.start_date <= :on) "
    "  AND (md.end_date IS NULL OR md.end_date >= :on)"
).bindparams(bindparam("eans", expanding=True))

_CURRENT_KEY_SQL = text(
    "SELECT sok.id_key FROM sharing_operation_key sok "
    "WHERE sok.id_community = :id_community AND sok.id_sharing_operation = :id_sharing_operation "
    "  AND sok.status = 1 AND sok.start_date <= :on "
    "  AND (sok.end_date IS NULL OR sok.end_date >= :on) "
    "ORDER BY sok.start_date DESC, sok.id DESC LIMIT 1"
)


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

    async def community_photo(self, id_community: int, *, on: datetime.date) -> CommunityPhoto:
        operations = {
            int(row.id): str(row.name)
            for row in await self._session.execute(_OPERATIONS_SQL, {"id_community": id_community})
        }
        meters = tuple(
            PhotoMeter(
                id_sharing_operation=int(row.id_sharing_operation),
                ean=str(row.ean),
                id_member=row.id_member,
                grd=row.grd,
                injection_status=row.injection_status,
                start_date=row.start_date,
            )
            for row in await self._session.execute(
                _PHOTO_METERS_SQL,
                {"id_community": id_community, "on": on, "active": _ACTIVE_METER_DATA},
            )
        )
        keys = tuple(
            PhotoKey(
                id_sharing_operation=int(row.id_sharing_operation),
                id_key=int(row.id_key),
                name=str(row.name),
                start_date=row.start_date,
            )
            for row in await self._session.execute(
                _PHOTO_KEYS_SQL, {"id_community": id_community, "on": on}
            )
        )
        return CommunityPhoto(operations=operations, meters=meters, keys=keys)

    async def episodes(self, id_community: int, *, statuses: Sequence[int]) -> list[Episode]:
        if not statuses:
            return []
        result = await self._session.execute(
            _EPISODES_SQL,
            {"id_community": id_community, "statuses": [int(s) for s in statuses]},
        )
        return [_episode(row) for row in result]

    async def episode_rows(
        self, id_community: int, keys: Sequence[tuple[int, int, str]]
    ) -> dict[tuple[int, int, str], Episode]:
        if not keys:
            return {}
        wanted = set(keys)
        result = await self._session.execute(
            _EPISODE_ROWS_SQL,
            {
                "id_community": id_community,
                "origins": sorted({k[0] for k in wanted}),
                "refs": sorted({k[2] for k in wanted}),
            },
        )
        rows = (_episode(row) for row in result)
        return {e.key: e for e in rows if e.key in wanted}

    async def live_episode(
        self, id_community: int, *, id_sharing_operation: int, subject_type: int, subject_ref: str
    ) -> Episode | None:
        row = (
            await self._session.execute(
                _LIVE_EPISODE_SQL,
                {
                    "id_community": id_community,
                    "id_sharing_operation": id_sharing_operation,
                    "subject_type": subject_type,
                    "subject_ref": subject_ref,
                    "statuses": list(range(1, 7)),
                },
            )
        ).first()
        return _episode(row) if row is not None else None

    async def meters_in_operation(
        self,
        id_community: int,
        id_sharing_operation: int,
        eans: Sequence[str],
        *,
        on: datetime.date,
    ) -> set[str]:
        if not eans:
            return set()
        result = await self._session.execute(
            _IN_OPERATION_SQL,
            {
                "id_community": id_community,
                "id_sharing_operation": id_sharing_operation,
                "eans": list(eans),
                "inactive": _INACTIVE_METER_DATA,
                "on": on,
            },
        )
        return {str(ean) for ean in result.scalars()}

    async def current_key(
        self, id_community: int, id_sharing_operation: int, *, on: datetime.date
    ) -> int | None:
        key = await self._session.scalar(
            _CURRENT_KEY_SQL,
            {"id_community": id_community, "id_sharing_operation": id_sharing_operation, "on": on},
        )
        return int(key) if key is not None else None

    async def active_keys(self, id_community: int, id_sharing_operation: int) -> list[Episode]:
        result = await self._session.execute(
            _ACTIVE_KEYS_SQL,
            {
                "id_community": id_community,
                "id_sharing_operation": id_sharing_operation,
                "active": int(RegulatoryStatus.ACTIVE),
            },
        )
        return [_episode(row) for row in result]

    async def communities_with_module(self) -> list[int]:
        result = await self._session.execute(
            _ACTIVE_COMMUNITIES_SQL, {"feature": str(FeatureName.ADMINISTRATIVE_DOCUMENT)}
        )
        return [int(row) for row in result.scalars()]


def _episode(row: Any) -> Episode:
    return Episode(
        id=int(row.id),
        id_sharing_operation=int(row.id_sharing_operation),
        subject_type=int(row.subject_type),
        subject_ref=str(row.subject_ref),
        id_dossier_origin=int(row.id_dossier_origin),
        regulatory_status=int(row.regulatory_status),
        valid_from=row.valid_from,
        valid_to=row.valid_to,
        regulatory_ref=row.regulatory_ref,
    )
