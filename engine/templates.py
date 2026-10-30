"""Which registered template a piece renders with.

A piece is pinned to the template in force when it is born
(``lifecycle.sync_pieces``), or at its first render when none was registered
then: a procedure keeps the form version of its creation, unless a render
explicitly upgrades it. The registry itself is generic (region, piece code,
version); nothing here knows a regulator.
"""

from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.local_models import DocumentTemplateModel


async def template_in_force(
    session: AsyncSession, *, id_community: int, region: int, doc_type: str
) -> DocumentTemplateModel | None:
    """The template currently in force for (region, doc_type), as the community
    sees it: its own override first, then the platform default, newest version
    first."""
    return (
        await session.execute(
            select(DocumentTemplateModel)
            .where(
                DocumentTemplateModel.region == region,
                DocumentTemplateModel.doc_type == doc_type,
                DocumentTemplateModel.valid_to.is_(None),
                or_(
                    DocumentTemplateModel.id_community.is_(None),
                    DocumentTemplateModel.id_community == id_community,
                ),
            )
            .order_by(
                DocumentTemplateModel.id_community.desc().nulls_last(),
                DocumentTemplateModel.version.desc(),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
