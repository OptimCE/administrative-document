"""ORM row -> response schema conversion.

Explicit rather than ``from_attributes``: keeping the translation in one place
means a column rename cannot silently change the public contract.
"""

from api.administrative_document.schemas import (
    MyFilingDocumentOut,
    MyFilingDossierOut,
    MyFilingOut,
    MyFilingRowsOut,
    MyFilingVersionOut,
    PrefillWarningOut,
    TemplateOut,
)
from api.administrative_document.service import MyFiling
from ports.crm_core import PrefillWarning
from shared.models.local_models import DocumentTemplateModel


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
            status=filing.dossier.status,
            family=filing.dossier.family,
            step=filing.dossier.step,
            title=filing.dossier.title,
            external_ref=filing.dossier.external_ref,
            # The first sending; a converted July dossier kept its own date.
            submitted_at=filing.dossier.frozen_at or filing.dossier.submitted_at,
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
