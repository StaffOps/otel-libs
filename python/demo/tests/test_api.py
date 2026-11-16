"""api: HTTP answers per scenario, quote cache, PRODUCER span and injected context."""

from __future__ import annotations

import grpc
import pytest
from fastapi.testclient import TestClient
from opentelemetry.trace import SpanKind, StatusCode

import api
import democommon as dc


class FakeRedis:
    def __init__(self):
        self.kv: dict[str, tuple[str, int]] = {}
        self.stream: list[dict] = []
        self.closed = False

    async def get(self, key):
        item = self.kv.get(key)
        return item[0].encode() if item else None

    async def set(self, key, value, ex=None):
        self.kv[key] = (value, ex)

    async def xadd(self, name, fields, maxlen=None, approximate=None):
        assert (name, maxlen, approximate) == (dc.STREAM, dc.STREAM_MAXLEN, True)
        self.stream.append(dict(fields))
        return f"1700000000000-{len(self.stream)}".encode()

    async def aclose(self):
        self.closed = True


class FakeStub:
    def __init__(self, error=None):
        self.error = error
        self.requests = []

    async def PlaceOrder(self, request):
        self.requests.append(request)
        if self.error is not None:
            raise grpc.aio.AioRpcError(self.error, grpc.aio.Metadata(), grpc.aio.Metadata(),
                                       details="order rejected")
        import demo_pb2

        return demo_pb2.PlaceOrderResponse(order_id=request.order_id, status="processed")


def client_for(otel, stub=None, redis=None):
    import demo_pb2

    redis = redis or FakeRedis()
    stub = stub or FakeStub()

    async def factory():
        return api.Deps(redis=redis, stub=stub, request_cls=demo_pb2.PlaceOrderRequest,
                        tracer=otel.tracer, run="r1", redis_host="demo-redis", redis_port=6379)

    return TestClient(api.create_app(factory), raise_server_exceptions=False), redis, stub


def body(scenario, order_id="ok-1"):
    return {"order_id": order_id, "customer": "cust-a", "amount": 12.0, "scenario": scenario}


def test_health(otel):
    client, _, _ = client_for(otel)
    with client:
        assert client.get("/health").json() == {"status": "ok"}


def test_ok_caches_calls_backend_and_publishes_with_context(otel):
    client, redis, stub = client_for(otel)
    with client:
        first = client.post("/orders", json=body("ok"))
        second = client.post("/orders", json=body("ok", "ok-2"))
    assert redis.closed  # lifespan shutdown closes the client
    assert first.status_code == 201 and first.json()["order_id"] == "ok-1"
    assert second.json()["quote"] == first.json()["quote"]  # second call is a cache hit
    assert redis.kv["demo:python:quote:cust-a"][1] == dc.QUOTE_TTL_SECONDS
    assert [r.scenario for r in stub.requests] == ["ok", "ok"]

    producers = otel.finished(f"send {dc.STREAM}")
    accepts = otel.finished("demo.order.accept")
    assert len(producers) == 2 and len(accepts) == 2
    producer, accept = producers[0], accepts[0]
    assert producer.kind is SpanKind.PRODUCER
    assert producer.parent.span_id == accept.context.span_id
    assert dict(producer.attributes) == {
        "messaging.system": "redis", "messaging.operation.name": "send",
        "messaging.operation.type": "send", "messaging.destination.name": "demo:python:orders",
        "server.address": "demo-redis", "server.port": 6379,
        "messaging.message.id": "1700000000000-1",
    }
    fields = redis.stream[0]
    assert fields["order_id"] == "ok-1" and fields["scenario"] == "ok"
    trace_hex, span_hex = f"{producer.context.trace_id:032x}", f"{producer.context.span_id:016x}"
    version, tp_trace, tp_span, flags = fields["traceparent"].split("-")
    assert (version, tp_trace, tp_span) == ("00", trace_hex, span_hex)
    assert int(flags, 16) & 1  # sampled (the SDK may also set the random-trace-id bit)


@pytest.mark.parametrize("code, status", [
    (grpc.StatusCode.FAILED_PRECONDITION, 422),
    (grpc.StatusCode.ALREADY_EXISTS, 409),
])
def test_backend_errors_map_to_4xx_without_event_or_log(otel, code, status):
    client, redis, _ = client_for(otel, stub=FakeStub(code))
    with client:
        r = client.post("/orders", json=body("business_error", "ord-2"))
    assert r.status_code == status
    (accept,) = otel.finished("demo.order.accept")
    assert accept.status.status_code is StatusCode.ERROR and accept.status.description is None
    assert not accept.events and not otel.records()
    assert not redis.stream and not otel.finished(f"send {dc.STREAM}")


@pytest.mark.parametrize("code", [grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.INTERNAL,
                                  grpc.StatusCode.INVALID_ARGUMENT])
def test_other_grpc_failures_are_a_502_with_accept_error_only(otel, code):
    client, redis, _ = client_for(otel, stub=FakeStub(code))
    with client:
        r = client.post("/orders", json=body("ok"))
    assert r.status_code == 502 and r.json() == {"error": "backend failed"}
    (accept,) = otel.finished("demo.order.accept")
    assert accept.status.status_code is StatusCode.ERROR and accept.status.description is None
    assert "error.type" not in accept.attributes and not accept.events and not otel.records()
    assert not redis.stream


@pytest.mark.parametrize("payload", [
    body("bogus"),
    {"order_id": "ok-1", "customer": "cust-a", "scenario": "ok"},  # amount missing
])
def test_invalid_bodies_are_a_400(otel, payload):
    client, _, stub = client_for(otel)
    with client:
        r = client.post("/orders", json=payload)
    assert r.status_code == 400 and not stub.requests and not otel.finished("demo.order.accept")


def test_cache_failure_is_one_warn_and_the_order_goes_on(otel):
    class NoCache(FakeRedis):
        async def get(self, key):
            raise ConnectionError("redis down")

    client, redis, stub = client_for(otel, redis=NoCache())
    with client:
        r = client.post("/orders", json=body("ok"))
    assert r.status_code == 201 and r.json()["quote"] == "" and len(stub.requests) == 1
    (rec,) = otel.records()
    assert rec.body == "quote cache unavailable" and rec.severity_number.value == 13
    assert rec.attributes["error.type"] == "ConnectionError"
    (accept,) = otel.finished("demo.order.accept")
    assert accept.status.status_code is StatusCode.UNSET and rec.span_id == accept.context.span_id


def test_publish_failure_is_one_warn_and_still_201(otel):
    class NoStream(FakeRedis):
        async def xadd(self, *a, **k):
            raise ConnectionError("redis down")

    client, _, _ = client_for(otel, redis=NoStream())
    with client:
        r = client.post("/orders", json=body("ok"))
    assert r.status_code == 201
    (producer,) = otel.finished(f"send {dc.STREAM}")
    assert producer.status.status_code is StatusCode.ERROR and producer.status.description is None
    assert producer.attributes["error.type"] == "ConnectionError" and not producer.events
    (rec,) = otel.records()
    assert rec.body == "order event not published" and rec.severity_number.value == 13
    (accept,) = otel.finished("demo.order.accept")
    assert accept.status.status_code is StatusCode.UNSET


def test_unhandled_is_recorded_in_accept_then_500(otel):
    client, redis, stub = client_for(otel)
    with client:
        r = client.post("/orders", json=body("unhandled", "ord-8"))
    assert r.status_code == 500
    assert not stub.requests and not redis.kv  # fails before the cache and the backend
    (accept,) = otel.finished("demo.order.accept")
    assert accept.status.status_code is StatusCode.ERROR and accept.status.description is None
    assert accept.attributes["error.type"] == "DemoUnhandledError"
    assert [dict(e.attributes) for e in accept.events] == [{"exception.type": "DemoUnhandledError"}]
    (rec,) = [r for r in otel.records() if r.attributes.get("exception.type") == "DemoUnhandledError"]
    assert rec.body == "boom unh-r1"
    assert rec.trace_id == accept.context.trace_id and rec.span_id == accept.context.span_id
    assert "DemoUnhandledError" in rec.attributes["exception.stacktrace"]


def test_quote_for_is_deterministic():
    assert api.quote_for("cust-a") == api.quote_for("cust-a")
    assert api.quote_for("cust-a").endswith(".00")


def test_deps_from_env_reads_the_password_file_and_creates_the_group(monkeypatch, tmp_path):
    import asyncio

    pw = tmp_path / "redis-pw"
    pw.write_text("not-printed\n")
    monkeypatch.setenv("DEMO_REDIS_ADDR", "redis.test:6390")
    monkeypatch.setenv("DEMO_REDIS_PASSWORD_FILE", str(pw))
    monkeypatch.setenv("DEMO_BACKEND_ADDR", "backend.test:50051")
    monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", "otel_libs.run=r9")
    groups = []

    async def fake_group(client):
        groups.append(client)

    monkeypatch.setattr(dc, "ensure_group", fake_group)

    async def go():
        deps = await api.deps_from_env()
        try:
            return deps, deps.redis.connection_pool.connection_kwargs
        finally:
            await deps.channel.close()
            await deps.redis.aclose()

    deps, kwargs = asyncio.run(go())
    assert (deps.redis_host, deps.redis_port, deps.run) == ("redis.test", 6390, "r9")
    assert kwargs["password"] == "not-printed" and groups == [deps.redis]
