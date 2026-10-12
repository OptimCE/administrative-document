"""In-memory stand-in for the JetStream calls a durable subscription makes.

``subscribe`` treats an existing durable the way nats-py 2.14 does: it binds
with the config the broker stored and drops the one passed in. Only a missing
durable is created from ``config``. A fake that applied ``config`` every time
would hide exactly the bug ``core.queue.helper.subscribe_durable`` exists for.

``add_consumer`` behaves like the broker's create-or-update: it rejects a change
to a field a push consumer cannot change, so an update that does not start from
the stored config fails here as it would in production.
"""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace
from typing import Any

from nats.js.api import ConsumerConfig
from nats.js.errors import BadRequestError, Error, NotFoundError

# What nats-server 2.10 stores for a field the create request left unset.
_SERVER_DEFAULTS = {"ack_wait": 30.0, "max_deliver": -1, "max_ack_pending": 1000}

# Fields the broker refuses to change on an existing push consumer.
_FIXED_FIELDS = (
    "deliver_subject",
    "deliver_group",
    "filter_subject",
    "deliver_policy",
    "ack_policy",
)


def _subject_matches(pattern: str, subject: str) -> bool:
    pattern_tokens, subject_tokens = pattern.split("."), subject.split(".")
    for index, token in enumerate(pattern_tokens):
        if token == ">":
            return len(subject_tokens) > index
        if index >= len(subject_tokens) or token not in ("*", subject_tokens[index]):
            return False
    return len(pattern_tokens) == len(subject_tokens)


def _as_stored(config: ConsumerConfig) -> ConsumerConfig:
    unset: dict[str, Any] = {
        name: value for name, value in _SERVER_DEFAULTS.items() if getattr(config, name) is None
    }
    return dataclasses.replace(config, **unset)


class FakeJetStream:
    def __init__(self, streams: dict[str, list[str]]) -> None:
        """``streams`` maps each stream name to its subject patterns."""
        self._streams = streams
        self.consumers: dict[tuple[str, str | None], ConsumerConfig] = {}
        self.add_consumer_calls: list[tuple[str, ConsumerConfig]] = []
        self.subscriptions: list[dict] = []
        self.reject_add_consumer: Exception | None = None

    def seed(self, subject: str, durable: str, **settings) -> None:
        """Store a durable as an earlier build's ``js.subscribe`` created it."""
        config = ConsumerConfig(
            durable_name=durable,
            deliver_group=durable,
            deliver_subject=f"_INBOX.{durable}",
            filter_subject=subject,
            **settings,
        )
        self.consumers[(self._stream_of(subject), durable)] = _as_stored(config)

    def stored(self, subject: str, durable: str) -> ConsumerConfig:
        return self.consumers[(self._stream_of(subject), durable)]

    def _stream_of(self, subject: str) -> str:
        for stream, patterns in self._streams.items():
            if any(_subject_matches(pattern, subject) for pattern in patterns):
                return stream
        raise NotFoundError

    async def find_stream_name_by_subject(self, subject: str) -> str:
        return self._stream_of(subject)

    async def consumer_info(self, stream: str, consumer: str):
        if (stream, consumer) not in self.consumers:
            raise NotFoundError(code=404, err_code=10014, description="consumer not found")
        config = dataclasses.replace(self.consumers[(stream, consumer)])
        return SimpleNamespace(name=consumer, stream_name=stream, config=config)

    async def add_consumer(self, stream: str, config: ConsumerConfig):
        self.add_consumer_calls.append((stream, config))
        if self.reject_add_consumer is not None:
            raise self.reject_add_consumer
        key = (stream, config.durable_name)
        existing = self.consumers.get(key)
        if existing is not None:
            for name in _FIXED_FIELDS:
                if getattr(existing, name) != getattr(config, name):
                    raise BadRequestError(
                        code=400, err_code=10012, description=f"{name} can not be updated"
                    )
        self.consumers[key] = _as_stored(config)
        return SimpleNamespace(name=config.durable_name, stream_name=stream, config=config)

    async def subscribe(
        self,
        subject: str,
        queue: str | None = None,
        cb=None,
        durable: str | None = None,
        stream: str | None = None,
        config: ConsumerConfig | None = None,
        manual_ack: bool = False,
        **_ignored,
    ):
        if stream is None:
            stream = self._stream_of(subject)
        key = (stream, durable)
        if key in self.consumers:
            # nats-py: ``config = consumer_info.config``; the argument is dropped.
            if self.consumers[key].deliver_group != queue:
                raise Error(f"cannot create a queue subscription {queue} for this consumer")
        else:
            created = dataclasses.replace(config or ConsumerConfig())
            created.durable_name = durable
            created.deliver_group = queue
            created.deliver_subject = f"_INBOX.{durable}"
            created.filter_subject = subject
            self.consumers[key] = _as_stored(created)
        subscription = {
            "subject": subject,
            "stream": stream,
            "durable": durable,
            "queue": queue,
            "manual_ack": manual_ack,
            "cb": cb,
        }
        self.subscriptions.append(subscription)
        return SimpleNamespace(**subscription)
