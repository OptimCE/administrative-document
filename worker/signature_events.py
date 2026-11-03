"""The signature annexe's answers about our pieces (``signature.event.administrative_document.*``).

The stream (SIGNATURE_EVENTS) belongs to the signature service; we only bind a
durable to our own prefix. Each event moves a ``piece_signature`` row, and a
``completed`` one makes the sealed PDF the piece's FINAL version
(``engine.signatures.apply_event``). Correlation is by the idempotency key we
chose when asking, never by the event's other fields.

Ack / nak: an event for no row of ours, or for a request already settled, is
acked and dropped; a storage or database hiccup is retried, then parked on
``optimce.administrative_document.dlq.signature_event``.
"""

from __future__ import annotations

import datetime
import logging
from typing import Any

from nats.aio.msg import Msg
from nats.js import JetStreamContext
from nats.js.api import ConsumerConfig

from core.config import settings
from core.database.database import AsyncSessionLocalFactory
from core.queue.helper import Event, subscribe_durable
from core.realtime import CommunityAudience, Tier, emit
from core.storage import build_s3_uri, download, upload
from engine.baseline import brussels_today
from engine.contracts import PluginRegistry
from engine.signatures import Applied, apply_event
from regulators.registry import REGISTRY
from shared.const import SIGNATURE_SOURCE, SUBJECT_SIGNATURE_EVENTS

logger = logging.getLogger(__name__)

_DURABLE = "worker-administrative-document-signature"
_ACK_WAIT_SECONDS = 120
_NAK_RETRY_DELAY_SECONDS = 30
_MAX_DELIVER = 5
_PREFIX = f"signature.event.{SIGNATURE_SOURCE}."


async def _fetch(key: str) -> bytes:
    return await download(key, bucket=settings.OUTPUT_BUCKET)


async def _store(key: str, content: bytes, content_type: str) -> str:
    await upload(key, content, content_type=content_type, bucket=settings.OUTPUT_BUCKET)
    return build_s3_uri(settings.OUTPUT_BUCKET, key)


async def process_signature_event(
    subject: str,
    data: dict[str, Any],
    *,
    registry: PluginRegistry = REGISTRY,
    session_factory: Any = AsyncSessionLocalFactory,
    fetch: Any = _fetch,
    store: Any = _store,
) -> Applied:
    """Apply one event; then tell the procedure's managers to refetch."""
    event = subject.removeprefix(_PREFIX)
    now = datetime.datetime.now(datetime.UTC)
    async with session_factory() as session:
        applied = await apply_event(
            session,
            event=event,
            data=data,
            registry=registry,
            fetch=fetch,
            store=store,
            now=now,
            today=brussels_today(now),
        )
    if applied.id_dossier is not None and applied.id_community is not None:
        await emit(
            topic="admin.dossier.updated",
            audience=CommunityAudience(applied.id_community, Tier.MANAGER),
            resource=("dossier", applied.id_dossier),
            scope_community_id=applied.id_community,
        )
    return applied


async def subscribe(js: JetStreamContext, *, registry: PluginRegistry = REGISTRY, on_dlq=None):
    """Bind our durable to the signature service's events stream (not declared here)."""

    async def _handle(msg: Msg) -> None:
        try:
            data = Event.decode(msg.data).data
            if not isinstance(data, dict):
                raise ValueError("not an object")
        except Exception:
            logger.exception("undecodable signature event on %s; acking and dropping", msg.subject)
            await msg.ack()
            return
        try:
            applied = await process_signature_event(msg.subject, data, registry=registry)
        except Exception:
            logger.exception("signature event %s failed", msg.subject)
            if msg.metadata.num_delivered >= _MAX_DELIVER:
                if on_dlq is not None:
                    await on_dlq(msg)
                await msg.ack()
            else:
                await msg.nak(delay=_NAK_RETRY_DELAY_SECONDS)
            return
        logger.info("signature event %s: %s", msg.subject, applied.outcome)
        await msg.ack()

    return await subscribe_durable(
        js,
        subject=SUBJECT_SIGNATURE_EVENTS,
        durable=_DURABLE,
        cb=_handle,
        config=ConsumerConfig(ack_wait=_ACK_WAIT_SECONDS, max_deliver=_MAX_DELIVER + 1),
    )
