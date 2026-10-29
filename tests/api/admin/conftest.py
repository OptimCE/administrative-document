"""Fixtures for the /admin route tests."""

from __future__ import annotations

import pytest


@pytest.fixture
def admin_storage(
    fake_storage: dict[tuple[str | None, str], bytes],
) -> dict[tuple[str | None, str], bytes]:
    """The in-memory object store; the root ``fake_storage`` covers
    api/admin/procedures.py too."""
    return fake_storage
