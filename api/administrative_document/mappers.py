"""ORM row -> response schema conversion.

Explicit rather than ``from_attributes``: the JSONB columns are named
``*_json`` in the database but exposed as plain ``metadata`` / ``context`` on the
wire, and ``version_count`` is computed. Keeping the translation in one place
means a column rename cannot silently change the public contract.
"""

from api.administrative_document.schemas import (
    DeadlineOut,
    DeadlineRuleOut,
    DocumentDetailOut,
    DocumentOut,
    DocumentVersionOut,
    DossierDetailOut,
    DossierOut,
    MyFilingDocumentOut,
    MyFilingDossierOut,
    MyFilingOut,
    MyFilingRowsOut,
    MyFilingVersionOut,
    PrefillWarningOut,
    StatusEventOut,
    TemplateOut,
)
from api.administrative_document.service import MyFiling
from ports.crm_core import PrefillWarning
from shared.models.local_models import (
    DeadlineModel,
    DeadlineRuleModel,
    DocumentModel,
    DocumentTemplateModel,
    DocumentVersionModel,
    DossierModel,
    StatusEventModel,
)


def to_dossier_out(row: DossierModel) -> DossierOut:
    return DossierOut(
        id=row.id,
        dossier_type=row.dossier_type,
        status=row.status,
        region=row.region,
        title=row.title,
        external_ref=row.external_ref,
        id_sharing_operation=row.id_sharing_operation,
        submitted_at=row.submitted_at,
        metadata=row.metadata_json or {},
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def to_document_out(row: DocumentModel, version_count: int = 0) -> DocumentOut:
    return DocumentOut(
        id=row.id,
        id_dossier=row.id_dossier,
        doc_type=row.doc_type,
        origin=row.origin,
        status=row.status,
        title=row.title,
        current_version_id=row.current_version_id,
        version_count=version_count,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def to_version_out(row: DocumentVersionModel) -> DocumentVersionOut:
    return DocumentVersionOut(
        id=row.id,
        version_no=row.version_no,
        content_sha256=row.content_sha256,
        content_type=row.content_type,
        byte_size=row.byte_size,
        original_filename=row.original_filename,
        id_template=row.id_template,
        generated_by=row.generated_by,
        # The exact payload this version was rendered from. Exposed so the UI can
        # show what was actually filed — that is the point of freezing it.
        data_snapshot_json=row.data_snapshot_json,
        docgen_request_id=row.docgen_request_id,
        created_at=row.created_at,
    )


def to_document_detail_out(
    row: DocumentModel, versions: list[DocumentVersionModel]
) -> DocumentDetailOut:
    return DocumentDetailOut(
        **to_document_out(row, version_count=len(versions)).model_dump(),
        versions=[to_version_out(v) for v in versions],
    )


def to_status_event_out(row: StatusEventModel) -> StatusEventOut:
    return StatusEventOut(
        id=row.id,
        subject_type=row.subject_type,
        subject_id=row.subject_id,
        from_status=row.from_status,
        to_status=row.to_status,
        is_corrective=row.is_corrective,
        actor_id=row.actor_id,
        occurred_at=row.occurred_at,
        context=row.context_json or {},
    )


def to_deadline_out(row: DeadlineModel) -> DeadlineOut:
    return DeadlineOut(
        id=row.id,
        id_dossier=row.id_dossier,
        deadline_type=row.deadline_type,
        due_date=row.due_date,
        status=row.status,
        recurring=row.recurring,
        derived_from_event_id=row.derived_from_event_id,
        id_deadline_rule=row.id_deadline_rule,
        resolved_at=row.resolved_at,
        created_at=row.created_at,
    )


def to_dossier_detail_out(
    dossier: DossierModel,
    documents: list[tuple[DocumentModel, int]],
    deadlines: list[DeadlineModel],
) -> DossierDetailOut:
    return DossierDetailOut(
        **to_dossier_out(dossier).model_dump(),
        documents=[to_document_out(doc, count) for doc, count in documents],
        deadlines=[to_deadline_out(d) for d in deadlines],
    )


def to_template_out(row: DocumentTemplateModel) -> TemplateOut:
    return TemplateOut(
        id=row.id,
        id_community=row.id_community,
        region=row.region,
        doc_type=row.doc_type,
        version=row.version,
        valid_from=row.valid_from,
        valid_to=row.valid_to,
        file_ref=row.file_ref,
        output_format=row.output_format,
        label=row.label,
    )


def to_deadline_rule_out(row: DeadlineRuleModel) -> DeadlineRuleOut:
    return DeadlineRuleOut(
        id=row.id,
        id_community=row.id_community,
        region=row.region,
        dossier_type=row.dossier_type,
        trigger_event=row.trigger_event,
        deadline_type=row.deadline_type,
        offset_value=row.offset_value,
        offset_unit=row.offset_unit,
        recurring=row.recurring,
        recur_months=row.recur_months,
        description=row.description,
    )


def to_prefill_warning_out(warning: PrefillWarning) -> PrefillWarningOut:
    """Domain warning -> wire.

    The one mapper here whose source is a domain dataclass rather than an ORM
    row: warnings are produced by the CRM port and `domain.prefill`, never
    persisted, so there is no model to read them from.
    """
    return PrefillWarningOut(
        code=warning.code,
        subject_type=warning.subject_type,
        subject_id=warning.subject_id,
        params=dict(warning.params),
    )


def to_my_filing_out(filing: MyFiling, *, template_label: str | None = None) -> MyFilingOut:
    """Project a filing for the member it names.

    Every field here is chosen; the omissions are the point. `generated_by` and
    `status_event.actor_id` are Keycloak subject ids, `dossier.metadata_json`
    and the journal's `context.reason` are manager free text, and none of them
    answers "what was filed about me".
    """
    return MyFilingOut(
        dossier=MyFilingDossierOut(
            id=filing.dossier.id,
            dossier_type=filing.dossier.dossier_type,
            status=filing.dossier.status,
            title=filing.dossier.title,
            external_ref=filing.dossier.external_ref,
            submitted_at=filing.dossier.submitted_at,
        ),
        document=MyFilingDocumentOut(
            id=filing.document.id,
            doc_type=filing.document.doc_type,
            status=filing.document.status,
            title=filing.document.title,
        ),
        template_label=template_label,
        version=MyFilingVersionOut(
            id=filing.version.id,
            version_no=filing.version.version_no,
            created_at=filing.version.created_at,
        ),
        my_rows=MyFilingRowsOut(**filing.rows),
    )
