"""The transition rules (spec R1).

These are the guarantees the product is sold on: a document cannot be recorded as
acknowledged without ever having been sent, a submission cannot be recorded
without its date, and a correction cannot be made anonymously.
"""

import pytest

from core.errors.errors import ErrorException
from domain import statemachine
from shared.const import DocumentStatus, DossierStatus, SubjectType


class TestDocumentEdges:
    @pytest.mark.parametrize(
        ("from_status", "to_status"),
        [
            (DocumentStatus.DRAFT, DocumentStatus.READY),
            (DocumentStatus.READY, DocumentStatus.SENT),
            (DocumentStatus.SENT, DocumentStatus.ACKNOWLEDGED),
            (DocumentStatus.DRAFT, DocumentStatus.OBSOLETE),
            (DocumentStatus.ACKNOWLEDGED, DocumentStatus.OBSOLETE),
        ],
    )
    def test_forward_edges_are_allowed(self, from_status, to_status):
        statemachine.assert_transition_allowed(SubjectType.DOCUMENT, from_status, to_status)

    def test_draft_cannot_jump_to_acknowledged(self):
        """The headline invariant: no skipping the paper trail."""
        with pytest.raises(ErrorException) as exc:
            statemachine.assert_transition_allowed(
                SubjectType.DOCUMENT, DocumentStatus.DRAFT, DocumentStatus.ACKNOWLEDGED
            )
        assert exc.value.status_code == 409

    def test_draft_cannot_jump_to_sent(self):
        with pytest.raises(ErrorException) as exc:
            statemachine.assert_transition_allowed(
                SubjectType.DOCUMENT, DocumentStatus.DRAFT, DocumentStatus.SENT
            )
        assert exc.value.status_code == 409

    def test_obsolete_is_terminal(self):
        with pytest.raises(ErrorException):
            statemachine.assert_transition_allowed(
                SubjectType.DOCUMENT, DocumentStatus.OBSOLETE, DocumentStatus.DRAFT
            )

    def test_transition_to_same_status_is_rejected(self):
        with pytest.raises(ErrorException) as exc:
            statemachine.assert_transition_allowed(
                SubjectType.DOCUMENT, DocumentStatus.READY, DocumentStatus.READY
            )
        assert exc.value.status_code == 409


class TestDossierEdges:
    @pytest.mark.parametrize(
        ("from_status", "to_status"),
        [
            (DossierStatus.IN_PREPARATION, DossierStatus.SUBMITTED),
            (DossierStatus.SUBMITTED, DossierStatus.COMPLETE),
            (DossierStatus.COMPLETE, DossierStatus.CLOSED),
            (DossierStatus.SUBMITTED, DossierStatus.LAPSED),
        ],
    )
    def test_forward_edges_are_allowed(self, from_status, to_status):
        statemachine.assert_transition_allowed(SubjectType.DOSSIER, from_status, to_status)

    def test_in_preparation_cannot_jump_to_complete(self):
        with pytest.raises(ErrorException) as exc:
            statemachine.assert_transition_allowed(
                SubjectType.DOSSIER, DossierStatus.IN_PREPARATION, DossierStatus.COMPLETE
            )
        assert exc.value.status_code == 409

    def test_closed_is_terminal(self):
        with pytest.raises(ErrorException):
            statemachine.assert_transition_allowed(
                SubjectType.DOSSIER, DossierStatus.CLOSED, DossierStatus.SUBMITTED
            )


class TestCorrectiveEdges:
    @pytest.mark.parametrize(
        ("subject_type", "from_status", "to_status"),
        [
            (SubjectType.DOCUMENT, DocumentStatus.SENT, DocumentStatus.READY),
            (SubjectType.DOCUMENT, DocumentStatus.READY, DocumentStatus.DRAFT),
            (SubjectType.DOCUMENT, DocumentStatus.ACKNOWLEDGED, DocumentStatus.SENT),
            (SubjectType.DOSSIER, DossierStatus.SUBMITTED, DossierStatus.IN_PREPARATION),
            (SubjectType.DOSSIER, DossierStatus.COMPLETE, DossierStatus.SUBMITTED),
            (SubjectType.DOSSIER, DossierStatus.LAPSED, DossierStatus.IN_PREPARATION),
        ],
    )
    def test_backward_edges_exist_and_are_flagged_corrective(
        self, subject_type, from_status, to_status
    ):
        statemachine.assert_transition_allowed(subject_type, from_status, to_status)
        assert statemachine.is_corrective(subject_type, from_status, to_status)

    def test_forward_edges_are_not_corrective(self):
        assert not statemachine.is_corrective(
            SubjectType.DOCUMENT, DocumentStatus.READY, DocumentStatus.SENT
        )

    def test_corrective_transition_requires_a_reason(self):
        with pytest.raises(ErrorException) as exc:
            statemachine.assert_requirements(
                SubjectType.DOCUMENT, DocumentStatus.SENT, DocumentStatus.READY, {}
            )
        assert exc.value.status_code == 422

    def test_corrective_transition_accepts_a_reason(self):
        statemachine.assert_requirements(
            SubjectType.DOCUMENT,
            DocumentStatus.SENT,
            DocumentStatus.READY,
            {"reason": "sent the wrong annex"},
        )


class TestTransitionRequirements:
    def test_mark_sent_requires_a_submission_date(self):
        with pytest.raises(ErrorException) as exc:
            statemachine.assert_requirements(
                SubjectType.DOCUMENT, DocumentStatus.READY, DocumentStatus.SENT, {}
            )
        assert exc.value.status_code == 422

    def test_acknowledge_requires_date_and_authority_reference(self):
        with pytest.raises(ErrorException):
            statemachine.assert_requirements(
                SubjectType.DOCUMENT,
                DocumentStatus.SENT,
                DocumentStatus.ACKNOWLEDGED,
                {"acknowledged_date": "2026-09-01"},
            )

    def test_acknowledge_accepts_full_context(self):
        statemachine.assert_requirements(
            SubjectType.DOCUMENT,
            DocumentStatus.SENT,
            DocumentStatus.ACKNOWLEDGED,
            {"acknowledged_date": "2026-09-01", "authority_file_ref": "CWAPE-2026-1234"},
        )

    def test_blank_string_does_not_satisfy_a_requirement(self):
        """A whitespace-only reference is not a reference."""
        with pytest.raises(ErrorException):
            statemachine.assert_requirements(
                SubjectType.DOCUMENT,
                DocumentStatus.SENT,
                DocumentStatus.ACKNOWLEDGED,
                {"acknowledged_date": "2026-09-01", "authority_file_ref": "   "},
            )

    def test_dossier_submission_requires_a_date(self):
        with pytest.raises(ErrorException):
            statemachine.assert_requirements(
                SubjectType.DOSSIER,
                DossierStatus.IN_PREPARATION,
                DossierStatus.SUBMITTED,
                {},
            )

    def test_unconstrained_transition_needs_no_context(self):
        statemachine.assert_requirements(
            SubjectType.DOCUMENT, DocumentStatus.DRAFT, DocumentStatus.READY, {}
        )


class TestComposedRequirements:
    """Corrective edges that also land on a constrained status.

    These two compose both requirement sets, which is easy to miss when writing a
    client: a caller that sends only the reason is refused, and one that sends
    only the date is refused too.
    """

    @pytest.mark.parametrize(
        ("subject", "from_status", "to_status"),
        [
            (SubjectType.DOCUMENT, DocumentStatus.ACKNOWLEDGED, DocumentStatus.SENT),
            (SubjectType.DOSSIER, DossierStatus.COMPLETE, DossierStatus.SUBMITTED),
        ],
    )
    def test_requires_the_target_context_and_a_reason(self, subject, from_status, to_status):
        assert statemachine.required_context_fields(subject, from_status, to_status) == (
            "submission_date",
            "reason",
        )

        with pytest.raises(ErrorException) as exc:
            statemachine.assert_requirements(subject, from_status, to_status, {"reason": "typo"})
        assert exc.value.status_code == 422

        with pytest.raises(ErrorException):
            statemachine.assert_requirements(
                subject, from_status, to_status, {"submission_date": "2026-09-07"}
            )

        statemachine.assert_requirements(
            subject,
            from_status,
            to_status,
            {"submission_date": "2026-09-07", "reason": "typo"},
        )

    def test_a_corrective_edge_to_an_unconstrained_status_needs_only_a_reason(self):
        assert statemachine.required_context_fields(
            SubjectType.DOCUMENT, DocumentStatus.SENT, DocumentStatus.READY
        ) == ("reason",)
