"""Fixtures shared by the administrative-document API tests.

``fake_storage`` lives in the root conftest instead: the worker tests need it
too, and pytest resolves fixtures upward, so nothing here had to change.
"""

import pytest


@pytest.fixture
def dossier_payload() -> dict:
    return {
        "dossier_type": 1,  # CREATION_NOTIFICATION
        "title": "Notification de creation",
        "metadata": {"note": "pilot community"},
    }
