"""Demo api: HTTP ``POST /orders`` → Redis quote cache → gRPC ``PlaceOrder`` → Redis stream.

specs/demo-environment DE-D2…DE-D8. Each request runs in the demo span
``demo.order.accept``: ``unhandled`` raises ``DemoUnhandledError`` there (recorded
and logged inside the span, then rethrown: the framework answers 500);
otherwise the quote cache is read (miss → SET, TTL 30 s), the backend is
called over ``grpc.aio`` and, for an ``ok`` order, one event is published to
``demo:<lang>:orders`` inside a PRODUCER span with the W3C context injected
into the message fields (DE-D6).

Environment: ``DEMO_HTTP_PORT`` (8080), ``DEMO_BACKEND_ADDR``
(``demo-python-backend:50051``), ``DEMO_REDIS_ADDR`` (``demo-redis:6379``),
``DEMO_REDIS_PASSWORD_FILE``, plus the OTel variables the helper reads.
"""

# staffops_otel_helper first: it must run before any opentelemetry import so unsupported
# OTEL_PROPAGATORS values are skipped instead of raising (isort would move it).
import staffops_otel_helper  # isort: skip  # noqa: F401

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Literal

import grpc
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from opentelemetry import propagate, trace
from opentelemetry.instrumentation.utils import suppress_instrumentation
from opentelemetry.trace import SpanKind, Status, StatusCode, Tracer
from pydantic import BaseModel

import democommon as dc

logger = logging.getLogger("demo.api")

# gRPC status → HTTP answer of the api (design "Scenario table").
HTTP_FOR_GRPC = {
    grpc.StatusCode.FAILED_PRECONDITION: 422,
    grpc.StatusCode.ALREADY_EXISTS: 409,
}


class OrderBody(BaseModel):
    order_id: str
    customer: str
    amount: float
    # An unknown scenario is a 400 (DE-D7 r3.5), see the validation handler in create_app.
    scenario: Literal["ok", "business_error", "db_error", "unhandled"]


@dataclass
class Deps:
    """What the routes need; built in ``lifespan`` from the environment, or by tests."""

    redis: Any  # redis.asyncio.Redis
    stub: Any  # demo_pb2_grpc.OrderServiceStub (grpc.aio)
    request_cls: Any  # demo_pb2.PlaceOrderRequest
    tracer: Tracer
    run: str
    redis_host: str
    redis_port: int
    channel: Any = None
    health_stub: Any = None  # grpc_health.v1 HealthStub on the backend channel
    health_request: Any = None  # grpc_health.v1 HealthCheckRequest
    ready_checked_at: float = float("-inf")
    ready: bool = False


# Readiness (DE-D11 r3.6): each dependency check times out quickly; the answer is reused for ~1 s.
READY_TIMEOUT_SECONDS = 1.0
READY_CACHE_SECONDS = 1.0


async def check_ready(deps: Deps) -> bool:
    """True when Redis answers PING and the backend's grpc.health.v1 says SERVING.

    Runs with instrumentation suppressed: probes every few seconds would
    otherwise make root Redis and gRPC client spans (noise in every check).
    """
    now = time.monotonic()
    if now - deps.ready_checked_at < READY_CACHE_SECONDS:
        return deps.ready
    ok = False
    with suppress_instrumentation():
        try:
            await asyncio.wait_for(deps.redis.ping(), READY_TIMEOUT_SECONDS)
            if deps.health_stub is not None:
                reply = await asyncio.wait_for(
                    deps.health_stub.Check(deps.health_request(service="")), READY_TIMEOUT_SECONDS)
                ok = reply.status == 1  # HealthCheckResponse.SERVING
        except Exception:  # noqa: BLE001 - not ready, whatever the reason
            ok = False
    deps.ready_checked_at, deps.ready = time.monotonic(), ok
    return ok


def quote_for(customer: str) -> str:
    """A deterministic price list entry; what the cache stores."""
    return f"{sum(customer.encode()) % 7 + 1}.00"


async def cached_quote(deps: Deps, customer: str) -> str:
    key = dc.QUOTE_KEY_PREFIX + customer
    value = await deps.redis.get(key)
    if value is not None:
        return value.decode() if isinstance(value, bytes) else str(value)
    value = quote_for(customer)
    await deps.redis.set(key, value, ex=dc.QUOTE_TTL_SECONDS)
    return value


async def publish_order(deps: Deps, order_id: str, scenario: str) -> str:
    """XADD one order event inside a PRODUCER span with the context injected (DE-D6)."""
    with deps.tracer.start_as_current_span(
        f"send {dc.STREAM}", kind=SpanKind.PRODUCER,
        attributes={
            "messaging.system": "redis",
            "messaging.operation.name": "send",
            "messaging.operation.type": "send",
            "messaging.destination.name": dc.STREAM,
            "server.address": deps.redis_host,
            "server.port": deps.redis_port,
        },
        # No exception event of its own (r3.5): a failure is ERROR + error.type here,
        # one WARN in accept_order, and the Redis client span below has the detail.
        record_exception=False, set_status_on_exception=False,
    ) as span:
        # Application fields order_id and scenario, plus the propagator's fields.
        fields = {"order_id": order_id, "scenario": scenario}
        carrier: dict[str, str] = {}
        propagate.inject(carrier)  # global propagator: traceparent, tracestate, baggage
        fields.update(carrier)
        try:
            entry_id = await deps.redis.xadd(dc.STREAM, fields, maxlen=dc.STREAM_MAXLEN, approximate=True)
        except Exception as e:
            span.set_status(Status(StatusCode.ERROR))
            span.set_attribute("error.type", type(e).__name__)
            raise
        entry_id = entry_id.decode() if isinstance(entry_id, bytes) else str(entry_id)
        span.set_attribute("messaging.message.id", entry_id)
        return entry_id


async def accept_order(deps: Deps, body: OrderBody) -> tuple[int, dict]:
    """The work of ``POST /orders`` inside ``demo.order.accept``; returns (status, payload)."""
    with deps.tracer.start_as_current_span(
        "demo.order.accept",
        attributes={"demo.scenario": body.scenario, "order.id": body.order_id},
        record_exception=False, set_status_on_exception=False,
    ) as span:
        if body.scenario == dc.UNHANDLED:
            try:
                raise dc.DemoUnhandledError(f"boom unh-{deps.run}")
            except dc.DemoUnhandledError as e:
                # Logged here, inside the owning span, before the rethrow (DE-D8 r3).
                dc.record_failure(span, logger, e)
                raise
        # The cache is not essential (r3.5): a failure is one WARN and no quote.
        try:
            quote = await cached_quote(deps, body.customer)
        except Exception as e:  # noqa: BLE001 - the type is enough; the Redis span has the rest
            logger.warning("quote cache unavailable", extra={"error.type": type(e).__name__})
            quote = ""
        try:
            reply = await deps.stub.PlaceOrder(deps.request_cls(
                order_id=body.order_id, customer=body.customer,
                amount=body.amount, scenario=body.scenario))
        except grpc.aio.AioRpcError as e:
            # Propagates the backend's failure: ERROR only, no description, no
            # error.type, no event, no log (DE-D8, r3.4). 422/409 for the two business
            # codes, 502 for any other gRPC failure (r3.5).
            span.set_status(Status(StatusCode.ERROR))
            status = HTTP_FOR_GRPC.get(e.code(), 502)
            return status, {"error": e.details() if status != 502 else "backend failed"}
        if body.scenario == dc.OK:
            try:
                await publish_order(deps, reply.order_id, body.scenario)
            except Exception as e:  # noqa: BLE001 - the order is stored; the event is best effort
                logger.warning("order event not published", extra={"error.type": type(e).__name__})
        return 201, {"order_id": reply.order_id, "status": reply.status, "quote": quote}


def create_app(deps_factory=None) -> FastAPI:
    """Build the app; ``deps_factory`` is an async callable returning ``Deps`` (tests pass a fake)."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        deps = await (deps_factory or deps_from_env)()
        app.state.deps = deps
        try:
            yield
        finally:
            if deps.channel is not None:
                await deps.channel.close()
            close = getattr(deps.redis, "aclose", None)
            if close is not None:
                await close()

    app = FastAPI(title="otel-libs demo api", lifespan=lifespan)

    @app.exception_handler(RequestValidationError)
    async def bad_request(request: Request, exc: RequestValidationError):
        # FastAPI's default is 422, which the demo reserves for business_error (r3.5).
        return JSONResponse({"error": "invalid order"}, status_code=400)

    @app.get("/health")
    async def health():
        # 200 only when Redis and the backend are reachable: the worker waits for it (r3.6).
        if await check_ready(app.state.deps):
            return {"status": "ok"}
        return JSONResponse({"status": "unavailable"}, status_code=503)

    @app.post("/orders")
    async def orders(body: OrderBody):
        status, payload = await accept_order(app.state.deps, body)
        return JSONResponse(payload, status_code=status)

    return app



async def deps_from_env() -> Deps:
    import demo_pb2
    import demo_pb2_grpc
    import redis.asyncio as aioredis
    from grpc_health.v1 import health_pb2, health_pb2_grpc

    host, port = dc.split_host_port(os.environ.get("DEMO_REDIS_ADDR", "demo-redis:6379"), 6379)
    password = dc.read_secret_file(os.environ.get("DEMO_REDIS_PASSWORD_FILE"), "DEMO_REDIS_PASSWORD_FILE")
    client = aioredis.Redis(host=host, port=port, password=password)
    await dc.ensure_group(client)
    channel = grpc.aio.insecure_channel(os.environ.get("DEMO_BACKEND_ADDR", f"demo-{dc.LANG}-backend:50051"))
    return Deps(redis=client, stub=demo_pb2_grpc.OrderServiceStub(channel),
                request_cls=demo_pb2.PlaceOrderRequest,
                tracer=staffops_otel_helper.get_tracer(dc.SCOPE), run=dc.run_id(),
                redis_host=host, redis_port=port, channel=channel,
                health_stub=health_pb2_grpc.HealthStub(channel),
                health_request=health_pb2.HealthCheckRequest)


def main() -> None:  # pragma: no cover - process wiring, run by the image smoke
    import uvicorn
    from staffops_otel_helper.ext.redis import instrument_redis

    instrument_redis()  # before setup_telemetry(): the 0.3 extension mechanism
    staffops_otel_helper.setup_telemetry()
    app = create_app()
    staffops_otel_helper.instrument_fastapi(app)  # idempotent; the class was imported before setup
    try:
        # uvicorn turns SIGTERM into a lifespan shutdown, then returns here.
        uvicorn.run(app, host="0.0.0.0", port=dc.env_int("DEMO_HTTP_PORT", 8080),
                    log_config=None, access_log=False)
    finally:
        try:
            staffops_otel_helper.shutdown_telemetry(timeout_ms=5_000)
        except RuntimeError as e:
            logger.warning("telemetry shutdown incomplete: %s", e)


if __name__ == "__main__":  # pragma: no cover
    main()
