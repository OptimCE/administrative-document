"""Owned-database access for dossiers, documents, versions, journal and deadlines.

Every SELECT on tenant data goes through ``with_community_scope`` so a missing
community in the request context yields no rows rather than another tenant's.
Every INSERT stamps ``id_community`` from the same ContextVar.

The two reference registries — ``document_template`` and ``deadline_rule`` — are
the exception: ``id_community IS NULL`` marks a platform default shared by every
tenant, so a plain equality predicate would hide them. They get a two-tier scope
instead, ``_visible_row`` for reads and ``_own_row`` for writes; see those
helpers for why the split matters.

Two things deliberately have no update path: ``status_event`` and
``document_version``. Database triggers reject UPDATE/DELETE on both, and there
is no method here that would attempt one.
"""

from __future__ import annotations

import datetime
from collections.abc import Sequence
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from core.context_vars import current_internal_community_id
from core.database.with_community import with_community_scope
from core.errors.errors import ErrorException
from domain.deadlines import DeadlineRuleSpec, DerivedDeadline
from shared.const import DeadlineStatus, RenderState, SubjectType
from shared.custom_errors import errors
from shared.models.local_models import (
    DeadlineModel,
    DeadlineRuleModel,
    DocumentModel,
    DocumentRenderModel,
    DocumentTemplateModel,
    DocumentVersionModel,
    DossierModel,
    StatusEventModel,
)

# Allow-list for caller-supplied ``sort``. The raw string is never interpolated
# into SQL; an unknown value silently falls back to the first column.
_DOSSIER_SORT_COLUMNS = {
    "id": DossierModel.id,
    "created_at": DossierModel.created_at,
    "updated_at": DossierModel.updated_at,
    "submitted_at": DossierModel.submitted_at,
    "dossier_type": DossierModel.dossier_type,
    "status": DossierModel.status,
}

_DEADLINE_SORT_COLUMNS = {
    "due_date": DeadlineModel.due_date,
    "created_at": DeadlineModel.created_at,
    "id": DeadlineModel.id,
}


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


def _order_by(columns: dict[str, Any], sort: str | None, order: str | None, default: str):
    column = columns.get(sort or default, columns[default])
    return column.asc() if (order or "").lower() == "asc" else column.desc()


class AdministrativeDocumentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ---- dossiers ----------------------------------------------------------

    def add_dossier(
        self,
        *,
        dossier_type: int,
        status: int,
        region: int,
        title: str | None,
        external_ref: str | None,
        id_sharing_operation: int,
        metadata: dict[str, Any],
    ) -> DossierModel:
        dossier = DossierModel(
            id_community=_tenant_id(),
            dossier_type=dossier_type,
            status=status,
            region=region,
            title=title,
            external_ref=external_ref,
            id_sharing_operation=id_sharing_operation,
            metadata_json=metadata,
        )
        self._session.add(dossier)
        return dossier

    async def get_dossier(self, dossier_id: int) -> DossierModel | None:
        stmt = with_community_scope(select(DossierModel), DossierModel).where(
            DossierModel.id == dossier_id
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_dossiers(
        self,
        *,
        page: int,
        limit: int,
        status: int | None = None,
        dossier_type: int | None = None,
        id_sharing_operation: int | None = None,
        sort: str | None = None,
        order: str | None = None,
    ) -> tuple[Sequence[DossierModel], int]:
        base = with_community_scope(select(DossierModel), DossierModel)
        count_base = with_community_scope(
            select(func.count()).select_from(DossierModel), DossierModel
        )
        filters = []
        if status is not None:
            filters.append(DossierModel.status == status)
        if dossier_type is not None:
            filters.append(DossierModel.dossier_type == dossier_type)
        if id_sharing_operation is not None:
            filters.append(DossierModel.id_sharing_operation == id_sharing_operation)
        for condition in filters:
            base = base.where(condition)
            count_base = count_base.where(condition)

        total = (await self._session.execute(count_base)).scalar_one()
        stmt = (
            base.order_by(_order_by(_DOSSIER_SORT_COLUMNS, sort, order, "created_at"))
            .offset((page - 1) * limit)
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return rows, total

    async def external_ref_taken(
        self, *, dossier_type: int, external_ref: str, exclude_id: int | None = None
    ) -> bool:
        stmt = with_community_scope(select(DossierModel.id), DossierModel).where(
            DossierModel.dossier_type == dossier_type,
            DossierModel.external_ref == external_ref,
        )
        if exclude_id is not None:
            stmt = stmt.where(DossierModel.id != exclude_id)
        return (await self._session.execute(stmt)).first() is not None

    # ---- documents ---------------------------------------------------------

    def add_document(
        self,
        *,
        id_dossier: int,
        doc_type: str,
        origin: int,
        status: int,
        title: str | None,
    ) -> DocumentModel:
        document = DocumentModel(
            id_community=_tenant_id(),
            id_dossier=id_dossier,
            doc_type=doc_type,
            origin=origin,
            status=status,
            title=title,
        )
        self._session.add(document)
        return document

    async def get_document(self, document_id: int) -> DocumentModel | None:
        stmt = with_community_scope(select(DocumentModel), DocumentModel).where(
            DocumentModel.id == document_id
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_documents(self, dossier_id: int) -> list[tuple[DocumentModel, int]]:
        """Documents of a dossier, each with its version count."""
        stmt = (
            with_community_scope(select(DocumentModel), DocumentModel)
            .where(DocumentModel.id_dossier == dossier_id)
            .order_by(DocumentModel.id.asc())
        )
        documents = list((await self._session.execute(stmt)).scalars().all())
        if not documents:
            return []
        counts = await self._version_counts([d.id for d in documents])
        return [(d, counts.get(d.id, 0)) for d in documents]

    async def _version_counts(self, document_ids: Sequence[int]) -> dict[int, int]:
        stmt = (
            with_community_scope(
                select(DocumentVersionModel.id_document, func.count()), DocumentVersionModel
            )
            .where(DocumentVersionModel.id_document.in_(document_ids))
            .group_by(DocumentVersionModel.id_document)
        )
        return {row[0]: row[1] for row in (await self._session.execute(stmt)).all()}

    async def count_versions(self, document_id: int) -> int:
        counts = await self._version_counts([document_id])
        return counts.get(document_id, 0)

    # ---- versions (append-only) -------------------------------------------

    async def next_version_no(self, document_id: int) -> int:
        stmt = with_community_scope(
            select(func.coalesce(func.max(DocumentVersionModel.version_no), 0)),
            DocumentVersionModel,
        ).where(DocumentVersionModel.id_document == document_id)
        return int((await self._session.execute(stmt)).scalar_one()) + 1

    def add_version(
        self,
        *,
        id_document: int,
        version_no: int,
        file_ref: str,
        content_sha256: str | None,
        content_type: str | None,
        byte_size: int | None,
        original_filename: str | None,
        generated_by: str | None,
        id_template: int | None = None,
        data_snapshot: dict[str, Any] | None = None,
    ) -> DocumentVersionModel:
        version = DocumentVersionModel(
            id_community=_tenant_id(),
            id_document=id_document,
            version_no=version_no,
            file_ref=file_ref,
            content_sha256=content_sha256,
            content_type=content_type,
            byte_size=byte_size,
            original_filename=original_filename,
            generated_by=generated_by,
            id_template=id_template,
            data_snapshot_json=data_snapshot,
        )
        self._session.add(version)
        return version

    async def list_versions(self, document_id: int) -> list[DocumentVersionModel]:
        stmt = (
            with_community_scope(select(DocumentVersionModel), DocumentVersionModel)
            .where(DocumentVersionModel.id_document == document_id)
            .order_by(DocumentVersionModel.version_no.asc())
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def get_version(self, document_id: int, version_id: int) -> DocumentVersionModel | None:
        stmt = with_community_scope(select(DocumentVersionModel), DocumentVersionModel).where(
            DocumentVersionModel.id == version_id,
            DocumentVersionModel.id_document == document_id,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    # ---- status journal (append-only) --------------------------------------

    def append_status_event(
        self,
        *,
        subject_type: SubjectType,
        subject_id: int,
        from_status: int | None,
        to_status: int,
        is_corrective: bool,
        actor_id: str | None,
        context: dict[str, Any],
    ) -> StatusEventModel:
        event = StatusEventModel(
            id_community=_tenant_id(),
            subject_type=int(subject_type),
            subject_id=subject_id,
            from_status=from_status,
            to_status=to_status,
            is_corrective=is_corrective,
            actor_id=actor_id,
            context_json=context,
        )
        self._session.add(event)
        return event

    async def dossier_timeline(
        self, dossier_id: int, document_ids: Sequence[int]
    ) -> list[StatusEventModel]:
        """The journal for a dossier and all of its documents, oldest first."""
        condition = (StatusEventModel.subject_type == int(SubjectType.DOSSIER)) & (
            StatusEventModel.subject_id == dossier_id
        )
        if document_ids:
            condition = condition | (
                (StatusEventModel.subject_type == int(SubjectType.DOCUMENT))
                & (StatusEventModel.subject_id.in_(document_ids))
            )
        stmt = (
            with_community_scope(select(StatusEventModel), StatusEventModel)
            .where(condition)
            .order_by(StatusEventModel.id.asc())
        )
        return list((await self._session.execute(stmt)).scalars().all())

    # ---- deadline rules ----------------------------------------------------

    async def matching_rules(
        self, *, region: int, dossier_type: int, trigger_event: str
    ) -> list[DeadlineRuleSpec]:
        """Candidate rules for a trigger: platform defaults plus this community's overrides."""
        stmt = select(DeadlineRuleModel).where(
            DeadlineRuleModel.region == region,
            DeadlineRuleModel.dossier_type == dossier_type,
            DeadlineRuleModel.trigger_event == trigger_event,
            _visible_row(DeadlineRuleModel),
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [
            DeadlineRuleSpec(
                id=row.id,
                deadline_type=row.deadline_type,
                offset_value=row.offset_value,
                offset_unit=row.offset_unit,
                recurring=row.recurring,
                recur_months=row.recur_months,
                id_community=row.id_community,
            )
            for row in rows
        ]

    async def list_deadline_rules(self, *, region: int | None = None) -> list[DeadlineRuleModel]:
        stmt = select(DeadlineRuleModel).where(_visible_row(DeadlineRuleModel))
        if region is not None:
            stmt = stmt.where(DeadlineRuleModel.region == region)
        stmt = stmt.order_by(DeadlineRuleModel.id.asc())
        return list((await self._session.execute(stmt)).scalars().all())

    async def get_deadline_rule(self, rule_id: int) -> DeadlineRuleModel | None:
        """Read a rule the community can see — its own override or a platform default.

        A deadline derived from a seeded rule carries that rule's id, so rolling a
        recurring obligation has to be able to re-read a default. Read-only: use
        ``get_own_deadline_rule`` before mutating anything.
        """
        stmt = select(DeadlineRuleModel).where(
            DeadlineRuleModel.id == rule_id, _visible_row(DeadlineRuleModel)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_own_deadline_rule(self, rule_id: int) -> DeadlineRuleModel | None:
        """The community's OWN rule override, for update. Platform defaults are not returned."""
        stmt = select(DeadlineRuleModel).where(
            DeadlineRuleModel.id == rule_id, _own_row(DeadlineRuleModel)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    def add_deadline_rule(self, **values: Any) -> DeadlineRuleModel:
        rule = DeadlineRuleModel(id_community=_tenant_id(), **values)
        self._session.add(rule)
        return rule

    # ---- deadlines ---------------------------------------------------------

    async def create_derived_deadlines(
        self,
        *,
        id_dossier: int,
        derived: Sequence[DerivedDeadline],
        event_id: int,
    ) -> int:
        """Insert the deadlines a transition implied. Idempotent.

        ``ON CONFLICT DO NOTHING`` plus the ``(id_dossier, deadline_type,
        derived_from_event_id)`` unique index means replaying the same event —
        a retry, a redelivered message — cannot create duplicates.
        """
        if not derived:
            return 0
        rows = [
            {
                "id_community": _tenant_id(),
                "id_dossier": id_dossier,
                "deadline_type": d.deadline_type,
                "due_date": d.due_date,
                "status": int(DeadlineStatus.OPEN),
                "recurring": d.recurring,
                "derived_from_event_id": event_id,
                "id_deadline_rule": d.id_deadline_rule,
            }
            for d in derived
        ]
        stmt = pg_insert(DeadlineModel).values(rows).on_conflict_do_nothing()
        result = await self._session.execute(stmt)
        return result.rowcount or 0

    async def get_deadline(self, deadline_id: int) -> DeadlineModel | None:
        stmt = with_community_scope(select(DeadlineModel), DeadlineModel).where(
            DeadlineModel.id == deadline_id
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_deadlines_for_dossier(self, dossier_id: int) -> list[DeadlineModel]:
        stmt = (
            with_community_scope(select(DeadlineModel), DeadlineModel)
            .where(DeadlineModel.id_dossier == dossier_id)
            .order_by(DeadlineModel.due_date.asc())
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def list_deadlines(
        self,
        *,
        page: int,
        limit: int,
        status: int | None = None,
        deadline_type: str | None = None,
        dossier_id: int | None = None,
        id_sharing_operation: int | None = None,
        due_before: datetime.date | None = None,
        sort: str | None = None,
        order: str | None = None,
    ) -> tuple[Sequence[DeadlineModel], int]:
        base = with_community_scope(select(DeadlineModel), DeadlineModel)
        count_base = with_community_scope(
            select(func.count()).select_from(DeadlineModel), DeadlineModel
        )
        filters = []
        if status is not None:
            filters.append(DeadlineModel.status == status)
        if deadline_type is not None:
            filters.append(DeadlineModel.deadline_type == deadline_type)
        if dossier_id is not None:
            filters.append(DeadlineModel.id_dossier == dossier_id)
        if due_before is not None:
            filters.append(DeadlineModel.due_date <= due_before)
        if id_sharing_operation is not None:
            # A deadline inherits its operation from the dossier it hangs off,
            # so filtering by operation is a join rather than a denormalised
            # column that could drift.
            operation_dossiers = with_community_scope(select(DossierModel.id), DossierModel).where(
                DossierModel.id_sharing_operation == id_sharing_operation
            )
            filters.append(DeadlineModel.id_dossier.in_(operation_dossiers))
        for condition in filters:
            base = base.where(condition)
            count_base = count_base.where(condition)

        total = (await self._session.execute(count_base)).scalar_one()
        stmt = (
            base.order_by(
                _order_by(_DEADLINE_SORT_COLUMNS, sort, order or "asc", "due_date"),
                DeadlineModel.id.asc(),
            )
            .offset((page - 1) * limit)
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return rows, total

    async def open_deadlines_past_due(self, today: datetime.date) -> list[DeadlineModel]:
        stmt = with_community_scope(select(DeadlineModel), DeadlineModel).where(
            DeadlineModel.status == int(DeadlineStatus.OPEN),
            DeadlineModel.due_date < today,
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def open_deadlines_due_within(
        self, as_of: datetime.date, days: int
    ) -> list[DeadlineModel]:
        """Open deadlines falling due in ``[as_of, as_of + days]``, not yet reminded.

        ``due_date >= as_of`` matters: without it this would also match everything
        `open_deadlines_past_due` returns, and a deadline that is both past due
        and inside the window would produce a "missed" AND a "due soon" message
        in the same sweep.

        ``reminded_at IS NULL`` is what makes a daily sweep idempotent. The email
        half is protected by ``outbound_message.dedupe_key``, but nothing
        protects the in-app notification, so without this filter a manager would
        collect one bell entry per day until the due date.
        """
        stmt = with_community_scope(select(DeadlineModel), DeadlineModel).where(
            DeadlineModel.status == int(DeadlineStatus.OPEN),
            DeadlineModel.reminded_at.is_(None),
            DeadlineModel.due_date >= as_of,
            DeadlineModel.due_date <= as_of + datetime.timedelta(days=days),
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def cancel_open_deadlines(self, *, id_dossier: int, deadline_type: str) -> int:
        """Cancel every still-open deadline of a type (e.g. lapse, once complete)."""
        stmt = with_community_scope(select(DeadlineModel), DeadlineModel).where(
            DeadlineModel.id_dossier == id_dossier,
            DeadlineModel.deadline_type == deadline_type,
            DeadlineModel.status == int(DeadlineStatus.OPEN),
        )
        rows = list((await self._session.execute(stmt)).scalars().all())
        for row in rows:
            row.status = int(DeadlineStatus.CANCELLED)
            row.resolved_at = datetime.datetime.now(datetime.UTC)
        return len(rows)

    def add_recurring_occurrence(
        self,
        *,
        id_dossier: int,
        deadline_type: str,
        due_date: datetime.date,
        id_deadline_rule: int | None,
    ) -> DeadlineModel:
        """The next occurrence of a recurring obligation.

        ``derived_from_event_id`` is NULL: a roll follows the resolution of the
        previous occurrence, not a status transition. The partial unique index on
        (dossier, type) WHERE open AND recurring is what prevents a double roll.
        """
        occurrence = DeadlineModel(
            id_community=_tenant_id(),
            id_dossier=id_dossier,
            deadline_type=deadline_type,
            due_date=due_date,
            status=int(DeadlineStatus.OPEN),
            recurring=True,
            derived_from_event_id=None,
            id_deadline_rule=id_deadline_rule,
        )
        self._session.add(occurrence)
        return occurrence

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
        """The form currently in force for this (region, doc_type).

        A community override wins over the platform default — hence the ORDER BY
        on ``id_community`` with NULLS LAST rather than a second query.
        """
        stmt = (
            select(DocumentTemplateModel)
            .where(
                _visible_row(DocumentTemplateModel)
                & (DocumentTemplateModel.region == region)
                & (DocumentTemplateModel.doc_type == doc_type)
                & (DocumentTemplateModel.valid_to.is_(None))
            )
            .order_by(
                DocumentTemplateModel.id_community.desc().nulls_last(),
                DocumentTemplateModel.version.desc(),
            )
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

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

    async def delete_render(self, document_id: int) -> None:
        """Clear the in-flight slot. Called when a render lands successfully.

        Scoped by hand: ``with_community_scope`` only types SELECT-shaped
        statements, and a DELETE without the tenant predicate is exactly the
        cross-tenant write that guard exists to prevent.
        """
        stmt = delete(DocumentRenderModel).where(
            (DocumentRenderModel.id_community == _tenant_id())
            & (DocumentRenderModel.id_document == document_id)
        )
        await self._session.execute(stmt)

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
