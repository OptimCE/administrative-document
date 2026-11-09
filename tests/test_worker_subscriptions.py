"""The worker's durables take the settings the code declares.

Including durables an earlier build created with other settings: ``js.subscribe``
alone keeps those, and ``FakeJetStream`` reproduces that.
"""

from __future__ import annotations

from core.config import settings
from shared.const import CORE_EVENTS_SUBJECT, SUBJECT_GENERATE_REQUESTED
from tests.fake_jetstream import FakeJetStream
from worker import core_events, dispatcher, docgen_results


def _js(*, core_stream: bool = True) -> FakeJetStream:
    streams = {
        "ADMIN_DOCUMENT": [SUBJECT_GENERATE_REQUESTED],
        "DOCGEN_RESULTS": ["docgen.result.>"],
    }
    if core_stream:
        streams["CORE_EVENTS"] = [CORE_EVENTS_SUBJECT]
    return FakeJetStream(streams)


async def test_subscribe_all_updates_durables_an_earlier_build_created():
    js = _js()
    js.seed(SUBJECT_GENERATE_REQUESTED, dispatcher._GENERATE_DURABLE)
    js.seed(settings.DOCGEN_RESULT_SUBJECT, docgen_results._DOCGEN_DURABLE)

    subscriptions = await dispatcher.subscribe_all(js)

    assert len(subscriptions) == 3
    generate = js.stored(SUBJECT_GENERATE_REQUESTED, dispatcher._GENERATE_DURABLE)
    assert (generate.ack_wait, generate.max_deliver) == (
        dispatcher._ACK_WAIT_SECONDS,
        dispatcher._MAX_DELIVER,
    )
    result = js.stored(settings.DOCGEN_RESULT_SUBJECT, docgen_results._DOCGEN_DURABLE)
    # One above the handler's own bound, so the handler's DLQ fires first.
    assert (result.ack_wait, result.max_deliver) == (
        docgen_results._ACK_WAIT_SECONDS,
        docgen_results._MAX_DELIVER + 1,
    )


async def test_subscribe_all_binds_each_durable_as_its_own_queue_group():
    js = _js()

    await dispatcher.subscribe_all(js)

    assert [s["durable"] for s in js.subscriptions] == [
        dispatcher._GENERATE_DURABLE,
        docgen_results._DOCGEN_DURABLE,
        core_events._DURABLE,
    ]
    assert all(s["queue"] == s["durable"] and s["manual_ack"] for s in js.subscriptions)


async def test_the_core_stream_missing_does_not_stop_the_other_consumers():
    """crm-outbox-relay owns CORE_EVENTS and may start later: the worker binds
    the rest now and retries that one in the background."""
    js = _js(core_stream=False)

    subscriptions = await dispatcher.subscribe_all(js)

    assert [s["durable"] for s in js.subscriptions] == [
        dispatcher._GENERATE_DURABLE,
        docgen_results._DOCGEN_DURABLE,
    ]
    assert len(subscriptions) == 2
