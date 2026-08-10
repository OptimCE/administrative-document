"""EventPublisher — a thin seam over NATS JetStream publish.

Injected into the API so tests can substitute a fake: the ASGI test client never
initialises NATS, and a transition must remain testable without a broker.

Publishing is deliberately the *last* thing a transition does, after the database
commit. An event that fails to publish must never roll back a recorded
transition — the journal is the source of truth, the event is a notification.
"""

from __future__ import annotations

import logging
from typing import Protocol

from core.queue.helper import Event, send_event
from core.queue.init import get_jetstream

logger = logging.getLogger(__name__)


class EventPublisher(Protocol):
    async def publish(self, subject: str, event: Event) -> None: ...


class NatsEventPublisher:
    """Publishes to the live JetStream context (set up by the app lifespan)."""

    async def publish(self, subject: str, event: Event) -> None:
        await send_event(get_jetstream(), subject, event)


class NoopEventPublisher:
    """Drops events. Used when NATS is not configured (local/test)."""

    async def publish(self, subject: str, event: Event) -> None:
        logger.debug("NATS not configured; dropping event %s on %s", event.type, subject)
