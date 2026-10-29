"""Fixtures for the /admin route tests."""

from __future__ import annotations

import pytest


@pytest.fixture
def admin_storage(monkeypatch) -> dict[tuple[str | None, str], bytes]:
    """The object store, in memory, for api/admin/procedures.py (which imports
    upload/download itself, so the root ``fake_storage`` does not reach it)."""
    from api.admin import procedures

    store: dict[tuple[str | None, str], bytes] = {}

    async def _upload(key, content, content_type=None, bucket=None):
        store[(bucket, key)] = content

    async def _download(key, bucket=None):
        try:
            return store[(bucket, key)]
        except KeyError:
            raise FileNotFoundError(key) from None

    monkeypatch.setattr(procedures, "upload", _upload)
    monkeypatch.setattr(procedures, "download", _download)
    return store
