"""Owned-database access for the routes outside ``/admin``: pieces and their
versions, the render slot, the template registry, the member's filings.

Every SELECT on tenant data goes through ``with_community_scope`` so a missing
community in the request context yields no rows rather than another tenant's.
Every INSERT stamps ``id_community`` from the same ContextVar.

The template registry is the exception: ``id_community IS NULL`` marks a
platform default shared by every tenant, so a plain equality predicate would
hide it. It gets a two-tier scope instead, ``_visible_row`` for reads and
``_own_row`` for writes; see those helpers for why the split matters.

``document_version`` has no update path: a database trigger rejects UPDATE and
DELETE, and there is no method here that would attempt one.
"""

from __future__ import annotations

import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from core.context_vars import current_internal_community_id
from core.database.with_community import with_community_scope
from core.errors.errors import ErrorException
from engine.templates import template_in_force
from shared.const import RenderPurpose, RenderState, VersionStage
from shared.custom_errors import errors
from shared.models.local_models import (
    DocumentModel,
    DocumentRenderModel,
    DocumentTemplateModel,
    DocumentVersionModel,
    DossierModel,
)


def _tenant_id() -> int:
    """The internal community id for the current request.

    Raises rather than returning None: an INSERT without a tenant would create an
    unreachable row, which is worse than a clear 403.
    """
    internal_id = current_internal_community_id.get()
    if internal_id is None:
        raise ErrorException(errors.auth.FORBIDDEN, status_code=403)
    return internal_id


def _visible_row(model: Any):
    """Reference rows this community may READ: its own overrides plus the platform defaults."""
    tenant = _tenant_id()
    return (model.id_community == tenant) | (model.id_community.is_(None))


def _own_row(model: Any):
    """Reference rows this community may WRITE: its own overrides, and nothing else.

    Deliberately NOT ``_visible_row``. ``Role.ADMIN`` is community-scoped — the
    creator of any community holds it — so an admin reaching a row with
    ``id_community IS NULL`` through a PATCH would be editing the CWaPE registry
    for every tenant on the platform. A default is changed by shipping a new seed,
    never over HTTP; a community customises one by POSTing its own override.
    """
    return model.id_community == _tenant_id()


class AdministrativeDocumentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ---- procedures and pieces ----------------------------------------------

    async def get_dossier(self, dossier_id: int) -> DossierModel | None:
        stmt = with_community_scope(select(DossierModel), DossierModel).where(
            DossierModel.id == dossier_id
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_document(self, document_id: int) -> DocumentModel | None:
        stmt = with_community_scope(select(DocumentModel), DocumentModel).where(
            DocumentModel.id == document_id
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_version(self, document_id: int, version_id: int) -> DocumentVersionModel | None:
        stmt = with_community_scope(select(DocumentVersionModel), DocumentVersionModel).where(
            DocumentVersionModel.id == version_id,
            DocumentVersionModel.id_document == document_id,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    # ---- templates ---------------------------------------------------------

    async def list_templates(
        self, *, region: int | None = None, doc_type: str | None = None
    ) -> list[DocumentTemplateModel]:
        stmt = select(DocumentTemplateModel).where(_visible_row(DocumentTemplateModel))
        if region is not None:
            stmt = stmt.where(DocumentTemplateModel.region == region)
        if doc_type is not None:
            stmt = stmt.where(DocumentTemplateModel.doc_type == doc_type)
        stmt = stmt.order_by(
            DocumentTemplateModel.doc_type.asc(), DocumentTemplateModel.version.desc()
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def get_own_template(self, template_id: int) -> DocumentTemplateModel | None:
        """The community's OWN template override, for update. Platform defaults are not returned."""
        stmt = select(DocumentTemplateModel).where(
            DocumentTemplateModel.id == template_id, _own_row(DocumentTemplateModel)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    def add_template(self, **values: Any) -> DocumentTemplateModel:
        template = DocumentTemplateModel(id_community=_tenant_id(), **values)
        self._session.add(template)
        return template

    async def get_current_template(
        self, *, region: int, doc_type: str
    ) -> DocumentTemplateModel | None:
        """The form currently in force for this (region, doc_type): the community's
        own override first, then the platform default. The rule lives in the engine,
        which pins a piece to it when the piece is born.
        """
        return await template_in_force(
            self._session, id_community=_tenant_id(), region=region, doc_type=doc_type
        )

    # ---- renders (Phase 2) --------------------------------------------------

    async def get_render(self, document_id: int) -> DocumentRenderModel | None:
        stmt = with_community_scope(select(DocumentRenderModel), DocumentRenderModel).where(
            DocumentRenderModel.id_document == document_id
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def claim_render(
        self,
        *,
        document_id: int,
        docgen_request_id: str,
        id_template: int,
        data_snapshot: dict[str, Any],
        requested_by: str | None,
        stale_after_seconds: int,
        stage: VersionStage = VersionStage.FINAL,
        watermark: str | None = None,
        prefill: dict[str, Any] | None = None,
        purpose: RenderPurpose = RenderPurpose.RENDER,
        id_document_version: int | None = None,
    ) -> bool:
        """Take the single in-flight render slot for a document.

        Returns False when another render already holds it — that is the 409.

        One statement, so there is no SELECT-then-INSERT window two concurrent
        requests could both pass through. ``UNIQUE (id_document)`` makes the
        conflict target exact. A row older than ``stale_after_seconds`` is
        superseded rather than blocking forever: without this, a result lost to
        docgen's DLQ would wedge the document permanently.
        """
        now = datetime.datetime.now(datetime.UTC)
        cutoff = now - datetime.timedelta(seconds=stale_after_seconds)
        values = {
            "id_community": _tenant_id(),
            "id_document": document_id,
            "docgen_request_id": docgen_request_id,
            "render_state": int(RenderState.PENDING),
            "render_error_json": None,
            "id_template": id_template,
            "data_snapshot_json": data_snapshot,
            "requested_by": requested_by,
            "requested_at": now,
            "purpose": int(purpose),
            "stage": int(stage),
            "watermark": watermark,
            "prefill_json": prefill,
            "id_document_version": id_document_version,
        }
        stmt = (
            pg_insert(DocumentRenderModel)
            .values(**values)
            .on_conflict_do_update(
                index_elements=[DocumentRenderModel.id_document],
                set_=values,
                # Retry a failure freely; supersede a pending render only once it
                # is provably stuck.
                where=(
                    (DocumentRenderModel.render_state == int(RenderState.FAILED))
                    | (DocumentRenderModel.requested_at < cutoff)
                ),
            )
            .returning(DocumentRenderModel.id)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none() is not None

    async def list_current_versions_with_snapshot(
        self, *, limit: int
    ) -> list[tuple[DocumentVersionModel, DocumentModel, DossierModel]]:
        """Every document's CURRENT version that carries a frozen snapshot.

        The current version only — a member asking "what has been filed about
        me" wants the state of record, not every superseded draft that also
        named them. Uploaded versions have `data_snapshot_json IS NULL` and are
        excluded, because an arbitrary PDF says nothing about who is in it.

        Joined rather than three round trips because the caller needs all three
        rows for every hit and the result set is one row per document.
        """
        stmt = (
            with_community_scope(
                select(DocumentVersionModel, DocumentModel, DossierModel), DocumentVersionModel
            )
            .join(DocumentModel, DocumentModel.id == DocumentVersionModel.id_document)
            .join(DossierModel, DossierModel.id == DocumentModel.id_dossier)
            .where(
                DocumentModel.current_version_id == DocumentVersionModel.id,
                DocumentVersionModel.data_snapshot_json.is_not(None),
            )
            .order_by(DocumentVersionModel.created_at.desc(), DocumentVersionModel.id.desc())
            .limit(limit)
        )
        return [tuple(row) for row in (await self._session.execute(stmt)).all()]
