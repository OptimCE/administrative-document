"""The consumer of the core's events (``core.evt.>`` on ``CORE_EVENTS``).

One durable with ``max_ack_pending=1``: events are filed one at a time and in
stream order across every replica, so a removal never overtakes the addition
it compensates. The stream belongs to the core (crm-outbox-relay creates it);
this only binds a durable to it, and ``dispatcher`` retries the binding in the
background while the stream is missing.

``process_core_event`` is extracted from the subscription so tests drive it
with their own sessions. Ack matrix, decided by ``subscribe``:

- filed, skipped (module off, no plugin, no procedure…) or a duplicate: ack;
- an envelope nobody can read: park it on the DLQ and ack;
- anything else (a database hiccup, a catalogue not synced yet): nak with a
  delay; after ``_MAX_DELIVER`` deliveries, park it and ack. The broker's own
  ``max_deliver`` is one higher, so the handler's DLQ always runs first.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable

from nats.aio.msg import Msg
from nats.js import JetStreamContext
from nats.js.api import ConsumerConfig
from sqlalchemy.ext.asyncio import AsyncSession

from core import metrics
from core.database.database import AsyncSessionCRMFactory, AsyncSessionLocalFactory
from core.queue.helper import subscribe_durable
from core.realtime import CommunityAudience, Tier, emit
from engine.contracts import PluginRegistry
from engine.events import CoreEvent, EventError
from engine.routing import DuplicateEventError, RoutingResult, route_event
from ports.crm_engine import SqlEngineCrmRead
from shared.const import CORE_EVENTS_SUBJECT, EventOutcome
from worker.context import with_tenant

logger = logging.getLogger(__name__)

_DURABLE = "worker-administrative-document-core-events"
_ACK_WAIT_SECONDS = 60
_NAK_RETRY_DELAY_SECONDS = 30
_MAX_DELIVER = 5

# Outcomes, mirroring the ack/nak decision the subscription makes.
FILED = "filed"
SKIPPED = "skipped"
DUPLICATE = "duplicate"
POISON = "poison"
TRANSIENT = "transient"


async def process_core_event(
    data: bytes,
    *,
    registry: PluginRegistry,
    local_session: AsyncSession | None = None,
    crm_session: AsyncSession | None = None,
) -> str:
    """File one core event. Returns an outcome constant; raises on a transient
    failure (the caller naks)."""
    try:
        event = CoreEvent.parse(json.loads(data))
    except (EventError, ValueError, TypeError, AttributeError) as exc:
        logger.error("unreadable core event: %s", exc)
        return POISON

    own_local = local_session is None
    own_crm = crm_session is None
    local = local_session or AsyncSessionLocalFactory()
    crm = crm_session or AsyncSessionCRMFactory()
    try:
        with with_tenant(event.id_community):
            try:
                result = await route_event(
                    event, local=local, crm=SqlEngineCrmRead(crm), registry=registry
                )
            except DuplicateEventError:
                if own_local:
                    await local.rollback()
                return DUPLICATE
            except EventError as exc:
                if own_local:
                    await local.rollback()
                logger.error("core event %s cannot be filed: %s", event.event_id, exc)
                return POISON
            if own_local:
                await local.commit()
    finally:
        if own_local:
            await local.close()
        if own_crm:
            await crm.close()

    if result.outcome != EventOutcome.ROUTED:
        metrics.admin_events_skipped.add(1, {"reason": result.outcome.name.lower()})
        return SKIPPED
    metrics.admin_changes_filed.add(len(result.changes))
    await _hint(event.id_community, result)
    return FILED


async def _hint(id_community: int, result: RoutingResult) -> None:
    """Tell the community's managers to refetch. After the commit, never
    before (see ``core.realtime.emit``); never raises."""
    audience = CommunityAudience(id_community, Tier.MANAGER)
    for change_id in result.changes:
        await emit(
            topic="admin.change.detected",
            audience=audience,
            resource=("change", change_id),
            scope_community_id=id_community,
        )
    for dossier_id in result.dossiers:
        await emit(
            topic="admin.dossier.updated",
            audience=audience,
            resource=("dossier", dossier_id),
            scope_community_id=id_community,
        )


async def subscribe(
    js: JetStreamContext,
    *,
    registry: PluginRegistry,
    on_dlq: Callable[[Msg], Awaitable[None]],
) -> JetStreamContext.PushSubscription:
    async def _handle(msg: Msg) -> None:
        try:
            outcome = await process_core_event(msg.data, registry=registry)
        except Exception:
            logger.exception("core event handler failed")
            outcome = TRANSIENT
        if outcome == POISON:
            await on_dlq(msg)
            await msg.ack()
            return
        if outcome != TRANSIENT:
            await msg.ack()
            return
        if msg.metadata.num_delivered >= _MAX_DELIVER:
            logger.error("core event exhausted %d deliveries; routing to DLQ", _MAX_DELIVER)
            await on_dlq(msg)
            await msg.ack()
        else:
            await msg.nak(delay=_NAK_RETRY_DELAY_SECONDS)

    return await subscribe_durable(
        js,
        subject=CORE_EVENTS_SUBJECT,
        durable=_DURABLE,
        cb=_handle,
        config=ConsumerConfig(
            ack_wait=_ACK_WAIT_SECONDS,
            max_deliver=_MAX_DELIVER + 1,
            max_ack_pending=1,
        ),
        # No stream= on purpose: the lookup by subject fails while the core's
        # stream does not exist yet, which is what sends dispatcher into its
        # background retry.
    )
