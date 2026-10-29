"""``CoreEffectsPort`` over the CRM session, in raw SQL.

- **Idempotent.** A new episode is upserted on its UNIQUE
  ``(id_dossier_origin, subject_type, subject_ref)`` with absolute values; an
  existing one is updated by id. Writing the same thing twice changes nothing.
- **The constraint is the core's.** "One EAN pending, authorised or active in a
  single operation per date" is the EXCLUDE constraint on the table. Every
  write is checked against it first, so the refusal names all the EANs at once
  (the database would name the first); a race that still trips it is caught
  and reported the same way.
- **Audited, not swallowed.** ``AuditLogService.log`` swallows its failures by
  design, which is wrong for an effect: the audit row is written here, in the
  same transaction, and a failure fails the effect.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core.audit_log import AuditActions
from core.audit_log.service import AUDIT_LOG_DEFAULT_SOURCE
from core.context_vars import current_user_id
from core.database.models import AuditLogModel
from ports.core_effects import EffectConflictError, EpisodeWrite
from ports.crm_engine import TRACKED_STATUSES
from shared.models.crm_models import AppUserModel

_EXCLUDE_CONSTRAINT = "ex_regulatory_link_one_operation"
_METER = 1
_TRACKED = tuple(int(s) for s in TRACKED_STATUSES)

_CONFLICT_SQL = text(
    "SELECT 1 FROM regulatory_link_status r "
    "WHERE r.subject_type = 1 AND r.subject_ref = :ref "
    "  AND r.regulatory_status IN (2, 3, 4, 5) "
    "  AND daterange(r.valid_from, r.valid_to, '[]') "
    "      && daterange(CAST(:valid_from AS DATE), CAST(:valid_to AS DATE), '[]') "
    "  AND NOT (r.id_dossier_origin = :origin AND r.subject_type = :subject_type "
    "           AND r.subject_ref = :ref) "
    "  AND (CAST(:episode_id AS BIGINT) IS NULL OR r.id <> :episode_id) "
    "LIMIT 1"
)

_UPSERT_SQL = text(
    "INSERT INTO regulatory_link_status (id_community, id_sharing_operation, subject_type, "
    " subject_ref, id_dossier_origin, regulatory_status, valid_from, valid_to, regulatory_ref) "
    "VALUES (:id_community, :id_sharing_operation, :subject_type, :ref, :origin, :status, "
    " :valid_from, :valid_to, :regulatory_ref) "
    "ON CONFLICT (id_dossier_origin, subject_type, subject_ref) DO UPDATE SET "
    " regulatory_status = EXCLUDED.regulatory_status, valid_from = EXCLUDED.valid_from, "
    " valid_to = EXCLUDED.valid_to, regulatory_ref = EXCLUDED.regulatory_ref"
)

_UPDATE_SQL = text(
    "UPDATE regulatory_link_status SET regulatory_status = :status, valid_from = :valid_from, "
    " valid_to = :valid_to, regulatory_ref = :regulatory_ref "
    "WHERE id = :episode_id AND id_community = :id_community"
)

_SET_REF_SQL = text(
    "UPDATE regulatory_link_status SET regulatory_ref = :ref "
    "WHERE id_community = :id_community AND id_dossier_origin = :origin "
    "  AND subject_type = :subject_type AND subject_ref = :subject_ref "
    "  AND regulatory_ref IS DISTINCT FROM :ref"
)

# "Key (subject_ref, daterange(...))=(541448800000000001, [2026-01-01,)) conflicts …"
_CONFLICT_DETAIL = re.compile(r"=\(([^,]+),")


def _values(id_community: int, write: EpisodeWrite) -> dict[str, Any]:
    return {
        "id_community": id_community,
        "id_sharing_operation": write.id_sharing_operation,
        "subject_type": write.subject_type,
        "ref": write.subject_ref,
        "origin": write.id_dossier_origin,
        "status": int(write.regulatory_status),
        "valid_from": write.valid_from,
        "valid_to": write.valid_to,
        "regulatory_ref": write.regulatory_ref,
        "episode_id": write.episode_id,
    }


class SqlCoreEffects:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _conflicts(self, id_community: int, writes: Sequence[EpisodeWrite]) -> list[str]:
        found = []
        for write in writes:
            if write.subject_type != _METER or int(write.regulatory_status) not in _TRACKED:
                continue
            hit = await self._session.scalar(_CONFLICT_SQL, _values(id_community, write))
            if hit is not None:
                found.append(write.subject_ref)
        return found

    async def apply(
        self, id_community: int, writes: Sequence[EpisodeWrite], *, audit_payload: dict
    ) -> int:
        conflicts = await self._conflicts(id_community, writes)
        if conflicts:
            raise EffectConflictError(conflicts)
        try:
            for write in writes:
                values = _values(id_community, write)
                if write.episode_id is not None:
                    await self._session.execute(_UPDATE_SQL, values)
                else:
                    await self._session.execute(_UPSERT_SQL, values)
        except IntegrityError as exc:
            if _EXCLUDE_CONSTRAINT not in str(exc.orig):
                raise
            match = _CONFLICT_DETAIL.search(str(exc.orig))
            raise EffectConflictError([match.group(1)] if match else ["?"]) from exc
        await self._audit(id_community, writes, audit_payload)
        return len(writes)

    async def _audit(
        self, id_community: int, writes: Sequence[EpisodeWrite], payload: dict
    ) -> None:
        user_id = user_email = None
        auth_user_id = current_user_id.get()
        if auth_user_id:
            row = (
                await self._session.execute(
                    select(AppUserModel.id, AppUserModel.email).where(
                        AppUserModel.auth_user_id == auth_user_id
                    )
                )
            ).first()
            if row is not None:
                user_id, user_email = row
        origins = sorted({w.id_dossier_origin for w in writes})
        self._session.add(
            AuditLogModel(
                id_community=id_community,
                action=AuditActions.REGULATORY_EFFECT_APPLIED,
                source=AUDIT_LOG_DEFAULT_SOURCE,
                entity_type="dossier",
                entity_id=str(origins[0]) if len(origins) == 1 else None,
                user_id=user_id,
                user_email=user_email,
                payload={
                    **payload,
                    "episodes": [
                        {
                            "subject_type": w.subject_type,
                            "subject_ref": w.subject_ref,
                            "id_sharing_operation": w.id_sharing_operation,
                            "regulatory_status": int(w.regulatory_status),
                            "valid_from": w.valid_from.isoformat() if w.valid_from else None,
                            "valid_to": w.valid_to.isoformat() if w.valid_to else None,
                        }
                        for w in writes
                    ],
                },
            )
        )
        await self._session.flush()

    async def set_refs(self, id_community: int, refs: Sequence[tuple[int, int, str, str]]) -> int:
        changed = 0
        for origin, subject_type, subject_ref, ref in refs:
            result = await self._session.execute(
                _SET_REF_SQL,
                {
                    "id_community": id_community,
                    "origin": origin,
                    "subject_type": subject_type,
                    "subject_ref": subject_ref,
                    "ref": ref,
                },
            )
            changed += int(result.rowcount or 0)  # type: ignore[attr-defined]
        return changed
