"""Tests for the RequestIdFilter introduced in core/logging.py.

The filter is the single point that stamps request-scoped context onto
every log record. We assert it pulls each of the four ContextVars and
falls back to ``"-"`` when they are unset, so log records always have
the expected attributes (the JSON formatter would otherwise raise on a
missing key).
"""

from __future__ import annotations

import logging

from core import logging as core_logging
from core.context_vars import (
    current_community_id,
    current_request_id,
    current_user_id,
    current_user_role,
)


def _make_record() -> logging.LogRecord:
    return logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=0,
        msg="hello",
        args=None,
        exc_info=None,
    )


def test_request_id_filter_injects_all_context_vars_when_set():
    tokens = [
        current_request_id.set("req-abc"),
        current_user_id.set("user-42"),
        current_community_id.set("comm-7"),
        current_user_role.set("admin"),
    ]
    try:
        record = _make_record()
        assert core_logging.RequestIdFilter().filter(record) is True
        assert record.request_id == "req-abc"
        assert record.user_id == "user-42"
        assert record.community_id == "comm-7"
        assert record.user_role == "admin"
    finally:
        # Reset ContextVars in reverse order so this test never leaks
        # state into the next one running on the same task.
        for token, var in zip(
            reversed(tokens),
            (
                current_user_role,
                current_community_id,
                current_user_id,
                current_request_id,
            ),
            strict=False,
        ):
            var.reset(token)


def test_request_id_filter_uses_dash_placeholder_when_context_vars_unset():
    record = _make_record()
    assert core_logging.RequestIdFilter().filter(record) is True
    assert record.request_id == "-"
    assert record.user_id == "-"
    assert record.community_id == "-"
    assert record.user_role == "-"
