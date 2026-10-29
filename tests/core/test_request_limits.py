"""Per-route body-size cap tests for the request-limits middleware.

The routes that take a file are the /admin ones ending in ``/versions``,
``/send`` and ``/correspondence``; their paths are parameterised, so the
middleware matches on the suffix. They must receive the larger
``UPLOAD_MAX_BODY_BYTES`` cap; every other route keeps the conservative
``MAX_BODY_BYTES`` default.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.middleware import request_limits


def _request(method: str, path: str) -> SimpleNamespace:
    return SimpleNamespace(method=method, url=SimpleNamespace(path=path))


@pytest.mark.parametrize(
    "path",
    [
        "/admin/dossiers/3/documents/42/versions",
        "/admin/dossiers/3/documents/42/versions/",
        # A sending's .eml or a decision's PDF easily passes the 2 MB default.
        "/admin/dossiers/3/send",
        "/admin/dossiers/3/correspondence",
    ],
)
def test_a_route_that_takes_a_file_gets_the_large_cap(path):
    req = _request("POST", path)
    assert request_limits._max_body_for(req) == request_limits.UPLOAD_MAX_BODY_BYTES


def test_get_versions_keeps_default_cap():
    # The upload cap is POST-only; a GET on the same path keeps the default.
    req = _request("GET", "/admin/dossiers/3/documents/42/versions")
    assert request_limits._max_body_for(req) == request_limits.MAX_BODY_BYTES


def test_version_download_keeps_default_cap():
    req = _request("GET", "/documents/42/versions/7/file")
    assert request_limits._max_body_for(req) == request_limits.MAX_BODY_BYTES


def test_a_messages_file_keeps_default_cap():
    req = _request("GET", "/admin/dossiers/3/correspondence/9/file")
    assert request_limits._max_body_for(req) == request_limits.MAX_BODY_BYTES


def test_other_post_route_keeps_default_cap():
    req = _request("POST", "/admin/dossiers")
    assert request_limits._max_body_for(req) == request_limits.MAX_BODY_BYTES


def test_health_route_keeps_default_cap():
    req = _request("POST", "/health/readiness")
    assert request_limits._max_body_for(req) == request_limits.MAX_BODY_BYTES
