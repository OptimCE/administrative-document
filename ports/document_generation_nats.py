"""NATS adapter for DocumentGenerationPort.

document-generation's request and result models ARE the message body — there is
no ``{type, version, data}`` envelope, unlike every other subject this service
publishes on. Building the body here keeps that asymmetry in one place.
"""

from __future__ import annotations

import json

from nats.js import JetStreamContext

from core.config import settings
from ports.document_generation import DocgenRequest


class NatsDocumentGeneration:
    def __init__(self, js: JetStreamContext) -> None:
        self._js = js

    async def request_render(self, request: DocgenRequest) -> None:
        body = {
            "request_id": request.request_id,
            "tenant_id": request.tenant_id,
            "requested_by": "administrative-document",
            "template": {"uri": request.template_uri},
            "outputs": [{"format": request.output_format}],
            "data": request.data,
            "key_prefix": request.key_prefix,
            "reply_to": request.reply_to,
            "options": {"locale": request.locale, "presign_ttl": request.presign_ttl},
            "metadata": request.metadata,
        }
        await self._js.publish(settings.DOCGEN_REQUEST_SUBJECT, json.dumps(body).encode())
