"""Reconnect coverage for the NATS plumbing.

The actual reconnection behaviour lives inside the nats-py client and
can only be verified end-to-end with a real broker. What we *can* lock
in unit tests is:

* the surface we expose to the broker (the connect kwargs),
* the publish-timeout so the API path cannot hang on a stalled broker.

* the stream-reconcile branches, which are the difference between an
  actionable boot error and an opaque crash.

Everything else (actual reconnection, durable consumer recovery) is covered
manually by the smoke test in the production roadmap's verification section.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from nats.js.errors import BadRequestError

# ---------------------------------------------------------------------------
# init_nats — reconnect kwargs
# ---------------------------------------------------------------------------


async def test_init_nats_passes_reconnect_options(monkeypatch):
    from core.queue import init as nats_init

    mock_jetstream = MagicMock()
    mock_jetstream.add_stream = AsyncMock()
    mock_client = MagicMock()
    mock_client.jetstream = MagicMock(return_value=mock_jetstream)
    mock_client.drain = AsyncMock()
    mock_connect = AsyncMock(return_value=mock_client)

    monkeypatch.setattr(nats_init.nats, "connect", mock_connect)
    # Anchor the module globals to monkeypatch so init_nats's writes get
    # reverted at end of test — otherwise a half-mocked client would leak
    # to whichever test runs next.
    monkeypatch.setattr(nats_init, "_nats_client", nats_init._nats_client)
    monkeypatch.setattr(nats_init, "_jetstream", nats_init._jetstream)

    await nats_init.init_nats()

    mock_connect.assert_awaited_once()
    kwargs = mock_connect.await_args.kwargs
    assert kwargs["max_reconnect_attempts"] == -1
    assert kwargs["reconnect_time_wait"] == 2
    assert kwargs["connect_timeout"] == 5
    for cb in ("error_cb", "disconnected_cb", "reconnected_cb", "closed_cb"):
        assert callable(kwargs[cb]), f"{cb} must be wired to a coroutine"


# ---------------------------------------------------------------------------
# send_event — publish timeout
# ---------------------------------------------------------------------------


async def test_send_event_passes_publish_timeout():
    """Publish must use an explicit timeout so the API can't hang on a
    stalled broker. The default is 5 s; callers may override."""
    from core.queue.helper import Event, send_event

    mock_js = MagicMock()
    ack = MagicMock(stream="S", seq=1)
    mock_js.publish = AsyncMock(return_value=ack)

    await send_event(mock_js, "subject.test", Event(type="t", data={}))

    mock_js.publish.assert_awaited_once()
    kwargs = mock_js.publish.await_args.kwargs
    assert kwargs["timeout"] == 5.0


async def test_send_event_respects_caller_timeout():
    from core.queue.helper import Event, send_event

    mock_js = MagicMock()
    ack = MagicMock(stream="S", seq=1)
    mock_js.publish = AsyncMock(return_value=ack)

    await send_event(mock_js, "subject.test", Event(type="t", data={}), timeout=1.0)

    assert mock_js.publish.await_args.kwargs["timeout"] == 1.0


# ---------------------------------------------------------------------------
# _ensure_stream — the three add_stream outcomes
# ---------------------------------------------------------------------------


def _bad_request(err_code: int) -> BadRequestError:
    return BadRequestError(err_code=err_code)


_STREAM = {"name": "ADMIN_DOCUMENT", "subjects": ["optimce.administrative_document.x"]}


async def test_ensure_stream_reconciles_name_in_use():
    from core.queue.init import _ensure_stream

    js = MagicMock()
    js.add_stream = AsyncMock(side_effect=_bad_request(10058))
    js.update_stream = AsyncMock()

    await _ensure_stream(js, dict(_STREAM))

    js.update_stream.assert_awaited_once()


async def test_ensure_stream_reports_subject_overlap_actionably():
    """A `>` wildcard on a sibling stream must not surface as a bare BadRequestError.

    This is the exact failure Phase 2 hits if ADMIN_DOCUMENT_EVENTS still claims
    `optimce.administrative_document.>` — the message has to say so.
    """
    from core.queue.init import _ensure_stream

    js = MagicMock()
    js.add_stream = AsyncMock(side_effect=_bad_request(10065))
    js.update_stream = AsyncMock()

    with pytest.raises(RuntimeError, match="overlap"):
        await _ensure_stream(js, dict(_STREAM))

    js.update_stream.assert_not_awaited()  # overlap is NOT reconcilable


async def test_ensure_stream_reraises_unknown_bad_request():
    from core.queue.init import _ensure_stream

    js = MagicMock()
    js.add_stream = AsyncMock(side_effect=_bad_request(10999))
    js.update_stream = AsyncMock()

    with pytest.raises(BadRequestError):
        await _ensure_stream(js, dict(_STREAM))
