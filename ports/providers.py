"""Which adapter backs each port — chosen here, framework-free.

The choice used to live in ``api/administrative_document/deps.py``, which meant
importing it dragged in ``fastapi``. That is fine for the API and fatal for the
worker: ``Dockerfile.worker`` installs ``requirements/worker.txt`` (no fastapi,
no uvicorn) precisely to keep the HTTP stack out of that image, so the deadline
scheduler's ``from api.administrative_document.deps import ...`` crash-looped the
container on every start.

So the selection lives here, next to the adapters, and both callers import it:
``deps.py`` wraps it in ``Depends``, ``worker/sweeps.py`` calls it directly. One
definition, so the request path and the daily tick cannot drift onto different
adapters — which matters here, because the choice is conditional.
"""

from __future__ import annotations

from core.config import settings
from ports.events import EventPublisher, NatsEventPublisher, NoopEventPublisher


def get_event_publisher() -> EventPublisher:
    """Publish to NATS when it is configured, otherwise drop events.

    Local and test runs have no broker; a transition must still succeed there,
    since the journal — not the event — is the record of what happened.
    """
    return NatsEventPublisher() if settings.NATS_URL.strip() else NoopEventPublisher()
