# nats/helper.py
import json
import logging
from dataclasses import dataclass

from nats.js import JetStreamContext

logger = logging.getLogger(__name__)


@dataclass
class Event:
    type: str
    data: dict
    version: int = 1

    def encode(self) -> bytes:
        return json.dumps(
            {
                "type": self.type,
                "version": self.version,
                "data": self.data,
            }
        ).encode()

    @classmethod
    def decode(cls, raw: bytes) -> "Event":
        parsed = json.loads(raw)
        return cls(
            type=parsed["type"],
            version=parsed["version"],
            data=parsed["data"],
        )


# Hard cap on a publish round-trip. Without this, a stalled broker would
# block the API request thread until the client's default request timeout
# (≥30 s with reconnects). Five seconds is plenty for a healthy broker
# and short enough to fail fast back to the user with a 500 + FAILED row.
_PUBLISH_TIMEOUT_SECONDS = 5.0


async def send_event(
    js: JetStreamContext,
    subject: str,
    event: Event,
    *,
    timeout: float = _PUBLISH_TIMEOUT_SECONDS,  # noqa: ASYNC109  # forwarded to js.publish(timeout=...)
) -> None:
    ack = await js.publish(
        subject,
        event.encode(),
        headers={"Event-Type": event.type},
        timeout=timeout,
    )
    logger.debug(
        "Published %s to %s (stream=%s, seq=%d)",
        event.type,
        subject,
        ack.stream,
        ack.seq,
    )
