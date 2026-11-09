"""The CWaPE plugin's guess at what a received mail is (phase 4)."""

from __future__ import annotations

import pytest

from regulators.cwape.plugin import CwapePlugin

PLUGIN = CwapePlugin()


@pytest.mark.parametrize(
    ("subject", "text", "expected"),
    [
        ("RE: [OPT-12] Modification du partage", "Votre dossier est complet.", "completeness_ack"),
        ("Accusé de réception", "", "completeness_ack"),
        ("Dossier incomplet", "Merci de compléter", "incomplete_notice"),
        ("RE: [OPT-12]", "Il manque des pièces manquantes au dossier", "incomplete_notice"),
        ("Invitation à se conformer", "", "non_conformity"),
        ("Décision", "La CWaPE refuse le partage", "decision_refused"),
        ("Décision de la CWaPE", "La CWaPE autorise le partage", "decision_granted"),
        ("Proposition d'avenant", "", "amendment_proposal"),
        ("Avis technique du GRD", "", "technical_opinion"),
        ("Bonjour", "Rien de particulier", None),
    ],
)
def test_suggests_the_most_specific_response(subject, text, expected):
    assert PLUGIN.suggest_response(subject, text) == expected
