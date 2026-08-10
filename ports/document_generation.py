"""Port for the document-generation service.

Rendering is asynchronous and lives in another service: we publish a request and
a result arrives later on our own reply subject. Going through a Protocol keeps
the NATS wire format in one adapter and lets a test drive the whole generate flow
with a recording fake.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class DocgenRequest:
    """One render, as document-generation's contract expects it.

    ``request_id`` is the correlation and idempotency key: it is committed to
    ``document_render`` before this is published, so a redelivery reuses it — and
    with it the deterministic ``key_prefix`` — instead of writing a second object.

    ``metadata`` is the ONLY channel that round-trips: ``GenerationResult`` is
    ``extra="forbid"`` and declares no ``tenant_id`` of its own, so anything the
    result handler needs to see must travel here.
    """

    request_id: str
    tenant_id: str
    template_uri: str
    output_format: str
    data: dict[str, Any]
    key_prefix: str
    reply_to: str
    locale: str | None = None
    presign_ttl: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class DocumentGenerationPort(Protocol):
    async def request_render(self, request: DocgenRequest) -> None:
        """Ask for a render. Returns as soon as the broker has the message."""
        ...
