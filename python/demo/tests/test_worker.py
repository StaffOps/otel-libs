"""worker: demo.drive outcomes and the CONSUMER span's parent + link (DE-D6)."""

from __future__ import annotations

import asyncio

import httpx
import pytest
from opentelemetry import propagate
from opentelemetry.trace import SpanKind, StatusCode

import democommon as dc
import worker


def run_drive(otel, handler, seq):
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            return await worker.drive_once(http, "http://api", otel.tracer, seq)

    return asyncio.run(go())


def test_drive_ok_is_a_root_span(otel):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(201, json={"status": "processed"})

    with otel.tracer.start_as_current_span("outer"):
        assert run_drive(otel, handler, 1) == 201
    assert seen[0].url.path == "/orders"
    (drive,) = otel.finished("demo.drive")
    assert drive.parent is None  # new trace even inside another span
    assert drive.attributes["demo.scenario"] == "ok" and drive.attributes["demo.outcome"] == "ok"
    assert drive.status.status_code is StatusCode.UNSET and not otel.records()


def test_drive_non_2xx_sets_error_type_without_event(otel):
    assert run_drive(otel, lambda r: httpx.Response(422), 3) == 422
    (drive,) = otel.finished("demo.drive")
    assert drive.attributes["demo.scenario"] == "business_error"
    assert drive.status.status_code is StatusCode.ERROR and drive.status.description is None
    assert drive.attributes["error.type"] == "422" and drive.attributes["demo.outcome"] == "error"
    assert not drive.events
    (rec,) = otel.records()
    assert rec.body == "order request failed" and rec.severity_number.value == 13
    assert rec.attributes["http.response.status_code"] == 422
    assert rec.span_id == drive.context.span_id


def test_drive_transport_failure(otel):
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    assert run_drive(otel, handler, 1) is None
    (drive,) = otel.finished("demo.drive")
    assert drive.attributes["error.type"] == "ConnectError" and not drive.events


class FakeRedis:
    def __init__(self, batches=None):
        self.acked = []
        self.batches = batches or []
        self.reads = []

    async def xack(self, stream, group, entry_id):
        from opentelemetry.instrumentation.utils import is_instrumentation_enabled

        assert is_instrumentation_enabled()  # XACK spans stay under the CONSUMER span
        self.acked.append((stream, group, entry_id))

    async def xreadgroup(self, group, consumer, streams, count=None):
        from opentelemetry.instrumentation.utils import is_instrumentation_enabled

        assert not is_instrumentation_enabled()  # the poll itself is untraced (r3.6)
        self.reads.append((group, consumer, streams, count))
        return self.batches


def producer_fields(otel):
    with otel.tracer.start_as_current_span("send demo:python:orders") as producer:
        carrier = {}
        propagate.inject(carrier)
    fields = {b"order_id": b"ok-1", b"scenario": b"ok"}
    fields.update({k.encode(): v.encode() for k, v in carrier.items()})
    return producer, fields


def test_consumer_span_parent_and_link_are_the_producer(otel):
    producer, fields = producer_fields(otel)
    redis = FakeRedis()
    asyncio.run(worker.consume_message(redis, otel.tracer, "1-1", fields, "demo-redis", 6379))
    (consumer,) = otel.finished(f"process {dc.STREAM}")
    assert consumer.kind is SpanKind.CONSUMER
    assert consumer.context.trace_id == producer.context.trace_id
    assert consumer.parent.span_id == producer.context.span_id
    assert [l.context.span_id for l in consumer.links] == [producer.context.span_id]
    assert dict(consumer.attributes) == {
        "messaging.system": "redis", "messaging.operation.name": "process",
        "messaging.operation.type": "process", "messaging.destination.name": "demo:python:orders",
        "messaging.message.id": "1-1", "messaging.consumer.group.name": "demo-workers",
        "server.address": "demo-redis", "server.port": 6379,
    }
    assert redis.acked == [(dc.STREAM, dc.CONSUMER_GROUP, "1-1")]
    (rec,) = otel.records()
    assert rec.body == "order event processed" and rec.span_id == consumer.context.span_id
    assert rec.attributes["order.id"] == "ok-1"


def test_consumer_without_context_is_a_root_without_link(otel):
    asyncio.run(worker.consume_message(FakeRedis(), otel.tracer, "1-2", {"order_id": "x"}, "r", 1))
    (consumer,) = otel.finished(f"process {dc.STREAM}")
    assert consumer.parent is None and not consumer.links


def test_poll_once_reads_without_block_and_handles_each_entry(otel):
    _, fields = producer_fields(otel)
    redis = FakeRedis(batches=[(b"demo:python:orders", [(b"1-1", fields), (b"1-2", fields)])])
    assert asyncio.run(worker.poll_once(redis, otel.tracer, "pod-1", "r", 1)) == 2
    assert redis.reads == [(dc.CONSUMER_GROUP, "pod-1", {dc.STREAM: ">"}, 10)]
    assert [a[2] for a in redis.acked] == ["1-1", "1-2"]
    redis.batches = []
    assert asyncio.run(worker.poll_once(redis, otel.tracer, "pod-1", "r", 1)) == 0


def test_every_runs_until_stopped_and_survives_a_failing_step():
    calls = []

    async def go():
        stop = asyncio.Event()

        async def step():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("transient")
            stop.set()

        await worker._every(0.01, stop, step)

    asyncio.run(go())
    assert len(calls) == 2


def test_every_concurrent_keeps_the_rate_while_a_step_is_slow():
    starts = []

    async def go():
        loop = asyncio.get_running_loop()
        stop = asyncio.Event()

        async def step():
            starts.append(loop.time())
            if len(starts) == 4:
                stop.set()
            await asyncio.sleep(0.2)  # each step is 4 ticks long

        t0 = loop.time()
        await worker._every(0.05, stop, step, concurrent=True)
        return t0, loop.time()

    t0, end = asyncio.run(go())
    assert len(starts) == 4
    assert starts[3] - t0 < 0.19  # four starts in ~0.15 s: no waiting for the slow steps
    assert end - starts[3] >= 0.19  # the pending steps are awaited before returning


def test_every_sequential_skips_missed_ticks():
    starts = []

    async def go():
        loop = asyncio.get_running_loop()
        stop = asyncio.Event()

        async def step():
            starts.append(loop.time())
            if len(starts) == 1:
                await asyncio.sleep(0.12)  # overruns two 0.05 s ticks
            elif len(starts) == 3:
                stop.set()

        await worker._every(0.05, stop, step)

    asyncio.run(go())
    assert 0.12 <= starts[1] - starts[0] < 0.2  # next tick on the grid, no burst
    assert starts[2] - starts[1] >= 0.04


class GroupRedis:
    """Fake for ensure_group: EXISTS, XINFO GROUPS, XGROUP CREATE."""

    def __init__(self, exists=False, groups=(), create_error=None):
        self.exists_ = exists
        self.groups = list(groups)
        self.create_error = create_error
        self.created = []

    async def exists(self, name):
        assert name == dc.STREAM
        return 1 if self.exists_ else 0

    async def xinfo_groups(self, name):
        return self.groups

    async def xgroup_create(self, name, group, id, mkstream):
        assert (name, group, id, mkstream) == (dc.STREAM, dc.CONSUMER_GROUP, "$", True)
        if self.create_error:
            raise self.create_error
        self.created.append(group)


def test_ensure_group_creates_when_absent():
    r = GroupRedis()
    assert asyncio.run(dc.ensure_group(r)) is True and r.created == [dc.CONSUMER_GROUP]


@pytest.mark.parametrize("name", ["demo-workers", b"demo-workers"])
def test_ensure_group_existing_group_sends_no_failing_command(name):
    r = GroupRedis(exists=True, groups=[{"name": "other"}, {"name": name}])
    assert asyncio.run(dc.ensure_group(r)) is False and r.created == []


def test_ensure_group_stream_without_group_creates_it():
    r = GroupRedis(exists=True, groups=[{b"name": b"other"}])
    assert asyncio.run(dc.ensure_group(r)) is True


def test_ensure_group_race_busygroup_is_accepted():
    from redis.exceptions import ResponseError

    r = GroupRedis(create_error=ResponseError("BUSYGROUP Consumer Group name already exists"))
    assert asyncio.run(dc.ensure_group(r)) is False


def test_ensure_group_other_errors_raise():
    from redis.exceptions import ResponseError

    with pytest.raises(ResponseError):
        asyncio.run(dc.ensure_group(GroupRedis(create_error=ResponseError("NOAUTH"))))


def test_poll_once_recovers_from_nogroup(otel, monkeypatch):
    from redis.exceptions import ResponseError

    class R(FakeRedis):
        async def xreadgroup(self, *a, **k):
            raise ResponseError("NOGROUP No such key 'demo:python:orders'")

    recreated = []

    async def fake_group(client):
        recreated.append(client)
        return True

    monkeypatch.setattr(dc, "ensure_group", fake_group)
    r = R()
    assert asyncio.run(worker.poll_once(r, otel.tracer, "pod-1", "r", 1)) == 0
    assert recreated == [r] and not otel.finished()  # no span of ours, ERROR or otherwise
    (rec,) = otel.records()
    assert rec.body == "consumer group recreated"


def test_poll_once_other_redis_errors_raise(otel):
    from redis.exceptions import ResponseError

    class R(FakeRedis):
        async def xreadgroup(self, *a, **k):
            raise ResponseError("NOAUTH")

    with pytest.raises(ResponseError):
        asyncio.run(worker.poll_once(R(), otel.tracer, "pod-1", "r", 1))


def test_consumer_span_records_no_exception_when_xack_fails(otel):
    _, fields = producer_fields(otel)

    class R(FakeRedis):
        async def xack(self, *a):
            raise ConnectionError("gone")

    with pytest.raises(ConnectionError):
        asyncio.run(worker.consume_message(R(), otel.tracer, "1-1", fields, "r", 1))
    (consumer,) = otel.finished(f"process {dc.STREAM}")
    assert not consumer.events and consumer.status.description is None


def test_wait_for_api_polls_health_untraced_until_200(otel):
    from opentelemetry.instrumentation.utils import is_instrumentation_enabled

    answers = [None, 503, 200]
    seen = []

    def handler(request):
        assert request.url.path == "/health" and not is_instrumentation_enabled()
        answer = answers[len(seen)]
        seen.append(answer)
        if answer is None:
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(answer)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            return await worker.wait_for_api(http, "http://api", asyncio.Event(), every=0.01)

    assert asyncio.run(go()) is True and seen == [None, 503, 200]
    (rec,) = otel.records()
    assert rec.body == "api ready" and rec.attributes["demo.wait_attempts"] == 2
    assert not otel.finished()


def test_wait_for_api_returns_false_when_stopped():
    async def go():
        stop = asyncio.Event()
        stop.set()
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(503))) as http:
            return await worker.wait_for_api(http, "http://api", stop)

    assert asyncio.run(go()) is False
