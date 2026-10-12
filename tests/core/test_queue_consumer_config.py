"""``subscribe_durable``: the code's consumer settings reach an existing durable.

nats-py 2.14's ``js.subscribe`` applies its ``config`` only when it creates the
durable. These tests run against ``FakeJetStream``, which drops ``config`` for an
existing durable the same way, so each one fails if ``subscribe_durable`` goes
back to relying on ``js.subscribe`` alone.
"""

from __future__ import annotations

import logging

import pytest
from nats.js.api import ConsumerConfig, DeliverPolicy
from nats.js.errors import BadRequestError

from core.queue.helper import subscribe_durable
from tests.fake_jetstream import FakeJetStream

_SUBJECT = "optimce.test.run"
_DURABLE = "worker-test"


def _js() -> FakeJetStream:
    return FakeJetStream({"TEST": ["optimce.test.>"]})


async def _noop(msg) -> None:
    return None


async def _subscribe(js, config):
    return await subscribe_durable(js, subject=_SUBJECT, durable=_DURABLE, cb=_noop, config=config)


async def test_js_subscribe_alone_keeps_an_existing_durables_settings():
    """The bug, as the fake reproduces it: the passed config is dropped."""
    js = _js()
    js.seed(_SUBJECT, _DURABLE, ack_wait=120.0)

    await js.subscribe(
        subject=_SUBJECT,
        durable=_DURABLE,
        queue=_DURABLE,
        config=ConsumerConfig(ack_wait=600, max_ack_pending=1),
    )

    stored = js.stored(_SUBJECT, _DURABLE)
    assert (stored.ack_wait, stored.max_ack_pending) == (120.0, 1000)


async def test_existing_durable_gets_the_declared_settings():
    js = _js()
    js.seed(_SUBJECT, _DURABLE, ack_wait=120.0)
    before = js.stored(_SUBJECT, _DURABLE)

    await _subscribe(js, ConsumerConfig(ack_wait=600, max_deliver=5, max_ack_pending=1))

    stored = js.stored(_SUBJECT, _DURABLE)
    assert (stored.ack_wait, stored.max_deliver, stored.max_ack_pending) == (600, 5, 1)
    # Updated in place: the push consumer keeps its inbox, queue group and filter.
    assert stored.deliver_subject == before.deliver_subject
    assert stored.deliver_group == before.deliver_group == _DURABLE
    assert stored.filter_subject == before.filter_subject == _SUBJECT


async def test_subscription_binds_the_queue_group_with_manual_ack():
    js = _js()

    subscription = await _subscribe(js, ConsumerConfig(ack_wait=600))

    assert subscription.stream == "TEST"
    assert subscription.durable == subscription.queue == _DURABLE
    assert subscription.manual_ack is True
    assert subscription.cb is _noop


async def test_durable_already_in_step_is_left_alone():
    js = _js()
    # The broker reports durations as float seconds.
    js.seed(_SUBJECT, _DURABLE, ack_wait=600.0, max_ack_pending=1)

    await _subscribe(js, ConsumerConfig(ack_wait=600, max_ack_pending=1))

    assert js.add_consumer_calls == []


async def test_missing_durable_is_created_by_subscribe_from_the_config():
    js = _js()

    await _subscribe(js, ConsumerConfig(ack_wait=600, max_deliver=5))

    assert js.add_consumer_calls == []
    stored = js.stored(_SUBJECT, _DURABLE)
    assert (stored.ack_wait, stored.max_deliver) == (600, 5)


async def test_setting_left_unset_keeps_the_brokers_value():
    js = _js()
    js.seed(_SUBJECT, _DURABLE, ack_wait=120.0, max_ack_pending=7)

    await _subscribe(js, ConsumerConfig(ack_wait=600))

    stored = js.stored(_SUBJECT, _DURABLE)
    assert (stored.ack_wait, stored.max_ack_pending) == (600, 7)


async def test_setting_only_applied_at_creation_is_refused():
    js = _js()
    js.seed(_SUBJECT, _DURABLE)

    with pytest.raises(ValueError, match="deliver_policy"):
        await _subscribe(js, ConsumerConfig(ack_wait=600, deliver_policy=DeliverPolicy.NEW))

    assert js.add_consumer_calls == []
    assert js.subscriptions == []


async def test_rejected_update_stops_the_subscription_and_names_the_way_out():
    js = _js()
    js.seed(_SUBJECT, _DURABLE, ack_wait=120.0)
    js.reject_add_consumer = BadRequestError(code=400, err_code=10012, description="nope")

    with pytest.raises(RuntimeError, match=f"nats consumer rm TEST {_DURABLE}"):
        await _subscribe(js, ConsumerConfig(ack_wait=600))

    assert js.subscriptions == []


async def test_update_is_logged_with_old_and_new_values(caplog):
    js = _js()
    js.seed(_SUBJECT, _DURABLE, ack_wait=120.0)

    with caplog.at_level(logging.WARNING, logger="core.queue.helper"):
        await _subscribe(js, ConsumerConfig(ack_wait=600, max_ack_pending=1))

    assert "TEST/worker-test" in caplog.text
    assert "ack_wait 120.0 -> 600" in caplog.text
    assert "max_ack_pending 1000 -> 1" in caplog.text
