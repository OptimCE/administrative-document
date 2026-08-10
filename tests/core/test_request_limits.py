"""Per-route body-size cap tests for the request-limits middleware.

The only upload is ``POST /documents/{id}/versions``; because that path is
parameterised the middleware matches on the ``/versions`` suffix. It must
receive the larger ``UPLOAD_MAX_BODY_BYTES`` cap; every other route keeps the
conservative ``MAX_BODY_BYTES`` default.
"""

from __future__ import annotations

from types import SimpleNamespace

from core.middleware import request_limits


def _request(method: str, path: str) -> SimpleNamespace:
    return SimpleNamespace(method=method, url=SimpleNamespace(path=path))


def test_upload_route_gets_large_cap():
    req = _request("POST", "/documents/42/versions")
    assert request_limits._max_body_for(req) == request_limits.UPLOAD_MAX_BODY_BYTES


def test_upload_route_with_trailing_slash_gets_large_cap():
    req = _request("POST", "/documents/42/versions/")
    assert request_limits._max_body_for(req) == request_limits.UPLOAD_MAX_BODY_BYTES


def test_get_versions_keeps_default_cap():
    # The upload cap is POST-only; a GET on the same path keeps the default.
    req = _request("GET", "/documents/42/versions")
    assert request_limits._max_body_for(req) == request_limits.MAX_BODY_BYTES


def test_version_download_keeps_default_cap():
    req = _request("GET", "/documents/42/versions/7/file")
    assert request_limits._max_body_for(req) == request_limits.MAX_BODY_BYTES


def test_other_post_route_keeps_default_cap():
    req = _request("POST", "/dossiers")
    assert request_limits._max_body_for(req) == request_limits.MAX_BODY_BYTES


def test_health_route_keeps_default_cap():
    req = _request("POST", "/health/readiness")
    assert request_limits._max_body_for(req) == request_limits.MAX_BODY_BYTES
