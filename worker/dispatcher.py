"""NATS subscriptions + the per-message ack/nak/DLQ matrix.

Failure classes, and why each is treated the way it is:

* **Deterministic** (no render row, no bundle, malformed body) → ack and drop.
  Redelivering a poison message forever helps nobody and hides the real problem.
* **Transient** (a database or broker hiccup) → nak for redelivery, bounded by
  ``_MAX_DELIVER``, then park on the DLQ and ack.

``subscribe_all`` is the single wiring point ``worker.main`` calls; it returns the
subscriptions to drain on shutdown.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any

from nats.aio.msg import Msg
from nats.js import JetStreamContext
from nats.js.api import ConsumerConfig

from core.queue.helper import Event
from ports.document_generation_nats import NatsDocumentGeneration
from shared.const import (
    SUBJECT_DLQ_DOCGEN_RESULT,
    SUBJECT_DLQ_GENERATE,
    SUBJECT_GENERATE_REQUESTED,
)
from worker import docgen_results, generate

logger = logging.getLogger(__name__)

_GENERATE_DURABLE = "worker-administrative-document-generate"
_ACK_WAIT_SECONDS = 120
_NAK_RETRY_DELAY_SECONDS = 30
_MAX_DELIVER = 5

# The docgen results stream is owned by document-generation. On a cold stack it
# may be created *after* we boot, so the first subscribe can lose the race.
# Rather than leave rendering half-wired for the whole process lifetime, retry in
# the background with capped backoff (~20 minutes of attempts).
_DOCGEN_RETRY_BASE_DELAY_SECONDS = 1
_DOCGEN_RETRY_MAX_DELAY_SECONDS = 30
_DOCGEN_RETRY_MAX_ATTEMPTS = 40


async def subscribe_all(js: JetStreamContext, *, inflight: set[asyncio.Task] | None = None) -> list:
    """Register the durable consumers. Returns their subscriptions."""
    subs = [
        await js.subscribe(
            subject=SUBJECT_GENERATE_REQUESTED,
            durable=_GENERATE_DURABLE,
            queue=_GENERATE_DURABLE,
            manual_ack=True,
            cb=_make_handler(js, _process_generate_message, inflight=inflight),
            config=ConsumerConfig(ack_wait=_ACK_WAIT_SECONDS, max_deliver=_MAX_DELIVER),
        )
    ]
    logger.info("Subscribed the generation consumer")

    async def _to_result_dlq(msg: Msg) -> None:
        await _to_dlq(js, SUBJECT_DLQ_DOCGEN_RESULT, msg)

    try:
        subs.append(await docgen_results.subscribe(js, on_dlq=_to_result_dlq))
        logger.info("Subscribed the docgen results consumer")
    except Exception as exc:
        logger.warning("docgen results not ready (%s); retrying attach in the background", exc)
        _spawn_docgen_result_retry(js, _to_result_dlq, inflight=inflight)
    return subs


def _spawn_docgen_result_retry(
    js: JetStreamContext,
    on_dlq: Callable[[Msg], Awaitable[None]],
    *,
    inflight: set[asyncio.Task] | None,
) -> None:
    """Background-retry the results subscription until it succeeds.

    The late subscription is NOT appended to the caller's drain list — mutating
    that list from here would race the shutdown drain. It is torn down by the
    connection-level drain in ``close_nats`` instead. The task is tracked in
    ``inflight`` so shutdown cancels it cleanly; in the inline unit path
    (``inflight is None``) there is no loop machinery, so the retry is skipped.
    """
    if inflight is None:
        return

    async def _retry() -> None:
        for attempt in range(1, _DOCGEN_RETRY_MAX_ATTEMPTS + 1):
            delay = min(
                _DOCGEN_RETRY_BASE_DELAY_SECONDS * (2 ** (attempt - 1)),
                _DOCGEN_RETRY_MAX_DELAY_SECONDS,
            )
            await asyncio.sleep(delay)
            try:
                await docgen_results.subscribe(js, on_dlq=on_dlq)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("docgen results attach retry %d failed: %s", attempt, exc)
                continue
            logger.info("docgen results attached after %d background retr(y/ies)", attempt)
            return
        logger.warning(
            "docgen results still unavailable after %d retries; rendering will not complete",
            _DOCGEN_RETRY_MAX_ATTEMPTS,
        )

    task: asyncio.Task[None] = asyncio.create_task(_retry(), name="docgen-result-retry")
    inflight.add(task)
    task.add_done_callback(inflight.discard)


def _make_handler(
    js: JetStreamContext,
    process: Callable[[JetStreamContext, Msg], Coroutine[Any, Any, None]],
    *,
    inflight: set[asyncio.Task] | None,
) -> Callable[[Msg], Awaitable[None]]:
    """Build the per-message callback.

    With ``inflight`` set each message is handled as a tracked background task so
    the listener never blocks; without it (unit tests) it runs inline.
    """

    def _on_done(task: asyncio.Task) -> None:
        if inflight is not None:
            inflight.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.error("handler task crashed: %r", task.exception())

    async def handle(msg: Msg) -> None:
        if inflight is None:
            await process(js, msg)
            return
        task: asyncio.Task[None] = asyncio.create_task(process(js, msg))
        inflight.add(task)
        task.add_done_callback(_on_done)

    return handle


async def _process_generate_message(js: JetStreamContext, msg: Msg) -> None:
    try:
        event = Event.decode(msg.data)
    except Exception:
        logger.exception(
            "undecodable message on %s; acking and dropping", SUBJECT_GENERATE_REQUESTED
        )
        await msg.ack()
        return

    document_id = event.data.get("document_id") if isinstance(event.data, dict) else None
    if not isinstance(document_id, int):
        logger.error(
            "missing/invalid document_id on %s: %r", SUBJECT_GENERATE_REQUESTED, event.data
        )
        await msg.ack()
        return

    try:
        await generate.process_generate(document_id, doc_port=NatsDocumentGeneration(js))
    except generate.GenerateRequestError as exc:
        # Deterministic: the render row or its template is unusable. No retry can
        # change that, and the API's staleness window will let the user try again.
        logger.warning(
            "generation request for document %s failed permanently: %s", document_id, exc
        )
        await msg.ack()
        return
    except Exception as exc:
        if msg.metadata.num_delivered >= _MAX_DELIVER:
            logger.error("document %s generation exhausted retries; routing to DLQ", document_id)
            await _to_dlq(js, SUBJECT_DLQ_GENERATE, msg)
            await msg.ack()
        else:
            logger.warning("document %s generation transient failure: %s", document_id, exc)
            await msg.nak(delay=_NAK_RETRY_DELAY_SECONDS)
        return

    await msg.ack()


async def _to_dlq(js: JetStreamContext, subject: str, msg: Msg) -> None:
    """Best-effort park of a poison message for inspection."""
    try:
        await js.publish(subject, msg.data, headers={"dlq-origin": msg.subject})
    except Exception:
        logger.exception("failed to route message to DLQ %s", subject)
