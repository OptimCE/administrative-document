"""The annexe's one write into the core: the effect columns of an episode.

Decision Q3 (no effects API): the annexe's DB role has INSERT on
``regulatory_link_status`` and UPDATE on four columns only
(``regulatory_status``, ``valid_from``, ``valid_to``, ``regulatory_ref``; see
the monorepo's postgres/provision/30-crm-grants.sql). This port is the only
code that uses them, and it never commits: the caller commits the core first,
then its own transition, so a replay after a crash between the two finds the
core already right and writes the same values again.
"""

from __future__ import annotations

import datetime
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from shared.const import RegulatoryStatus


@dataclass(frozen=True)
class EpisodeWrite:
    """Absolute values for one episode, keyed by (origin, subject)."""

    id_sharing_operation: int
    subject_type: int  # 1 = meter, 2 = key (the only subjects the core tracks)
    subject_ref: str
    id_dossier_origin: int
    regulatory_status: RegulatoryStatus
    valid_from: datetime.date | None = None
    valid_to: datetime.date | None = None
    regulatory_ref: str | None = None
    # Set to move an episode another procedure opened (a meter leaving after a
    # baseline): updated by id instead of upserted by origin.
    episode_id: int | None = None


class EffectConflictError(Exception):
    """The core refuses: these EANs would be pending, authorised or active in
    two operations at once (its EXCLUDE constraint)."""

    def __init__(self, subjects: Sequence[str]) -> None:
        self.subjects = sorted(set(subjects))
        super().__init__(f"already tracked in another operation: {', '.join(self.subjects)}")


class CoreEffectsPort(Protocol):
    async def apply(
        self, id_community: int, writes: Sequence[EpisodeWrite], *, audit_payload: dict
    ) -> int:
        """Write every episode and one audit row, in the caller's CRM
        transaction. Raises ``EffectConflictError`` before writing anything when
        a write would break the one-operation constraint."""
        ...

    async def set_refs(self, id_community: int, refs: Sequence[tuple[int, int, str, str]]) -> int:
        """Point episodes, by (origin, subject type, subject ref), at the
        procedure that now drives them. Idempotent."""
        ...
