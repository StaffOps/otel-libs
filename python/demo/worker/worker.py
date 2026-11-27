"""Demo worker: load driver (``POST /orders``) and queue consumer (Redis Streams).

specs/demo-environment DE-D5…DE-D8. Two loops on one event loop:

- driver: every ``DEMO_INTERVAL_SECONDS`` (5) one order of the fixed cycle
  (DE-D7) in a new root span ``demo.drive``; a non-2xx answer sets ERROR with
  ``error.type`` = the status code, ``demo.outcome=error`` and one WARN record
  ``order request failed``, without an exception event (no exception occurs here);
- consumer: every ``DEMO_POLL_SECONDS`` (2) ``XREADGROUP COUNT 10`` without
  ``BLOCK``; per message one CONSUMER span ``process demo:<lang>:orders`` whose
  parent is the extracted producer context and which links to it (DE-D6), with
  the INFO record ``order event processed`` and the ``XACK`` inside it.

Environment: ``DEMO_API_URL`` (``http://demo-python-api:8080``),
``DEMO_REDIS_ADDR`` (``demo-redis:6379``), ``DEMO_REDIS_PASSWORD_FILE``,
``DEMO_INTERVAL_SECONDS``, ``DEMO_POLL_SECONDS``, plus the OTel variables.
"""

# staffops_otel_helper first: it must run before any opentelemetry import so unsupported
# OTEL_PROPAGATORS values are skipped instead of raising (isort would move it).
import staffops_otel_helper  # isort: skip

import asyncio
import logging
import os
import signal
import socket
from typing import Any

from opentelemetry import propagate, trace
from opentelemetry.instrumentation.utils import suppress_instrumentation
from opentelemetry.trace import Link, SpanKind, Status, StatusCode, Tracer

import democommon as dc

logger = logging.getLogger("demo.worker")


async def drive_once(http, api_url: str, tracer: Tracer, seq: int) -> int | None:
    """Send the ``seq``-th order in a new root span ``demo.drive``; return the HTTP status."""
    order = dc.order_for(seq)
    # Only propagates the outcome (DE-D8): every failure is caught and marked by hand below,
    # so the helper's rule (an exception leaving the span, D15) never records here.
    with staffops_otel_helper.start_root_span(
        tracer, "demo.drive", attributes={"demo.scenario": order.scenario},
    ) as span:
        try:
            response = await http.post(f"{api_url}/orders", json=order.to_json())
        except Exception as e:  # noqa: BLE001 - transport failure: recorded as error.type, no event
            span.set_status(Status(StatusCode.ERROR))
            span.set_attribute("error.type", type(e).__name__)
            span.set_attribute("demo.outcome", "error")
            logger.warning("order request failed", extra={"error.type": type(e).__name__})
            return None
        if 200 <= response.status_code < 300:
            span.set_attribute("demo.outcome", "ok")
        else:
            span.set_status(Status(StatusCode.ERROR))
            span.set_attribute("error.type", str(response.status_code))
            span.set_attribute("demo.outcome", "error")
            logger.warning("order request failed",
                           extra={"http.response.status_code": response.status_code})
        return response.status_code


def _text(value: Any) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


async def consume_message(redis_client, tracer: Tracer, entry_id: str, fields: dict,
                          redis_host: str, redis_port: int) -> None:
    """Process one stream entry in a CONSUMER span: parent and link = the producer (DE-D6)."""
    carrier = {_text(k): _text(v) for k, v in fields.items()}
    producer_ctx = propagate.extract(carrier)
    producer = trace.get_current_span(producer_ctx).get_span_context()
    links = [Link(producer)] if producer.is_valid else []
    # record_exception/set_status_on_exception off: a failing XACK is a Redis span
    # error and one WARN of the loop, not an exception event on this span (r3.5).
    with tracer.start_as_current_span(
        f"process {dc.STREAM}", context=producer_ctx, kind=SpanKind.CONSUMER, links=links,
        record_exception=False, set_status_on_exception=False,
        attributes={
            "messaging.system": "redis",
            "messaging.operation.name": "process",
            "messaging.operation.type": "process",
            "messaging.destination.name": dc.STREAM,
            "messaging.message.id": entry_id,
            "messaging.consumer.group.name": dc.CONSUMER_GROUP,
            "server.address": redis_host,
            "server.port": redis_port,
        },
    ):
        logger.info("order event processed", extra={"order.id": carrier.get("order_id", ""),
                                                     "demo.scenario": carrier.get("scenario", "")})
        await redis_client.xack(dc.STREAM, dc.CONSUMER_GROUP, entry_id)


async def poll_once(redis_client, tracer: Tracer, consumer: str, redis_host: str, redis_port: int) -> int:
    """One non-blocking XREADGROUP (DE-D5); returns the number of messages processed."""
    # No active span here: every poll is a root Redis client span, and every
    # CONSUMER span below starts outside any other span (DE-D6).
    from redis.exceptions import ResponseError

    try:
        # Tracing suppressed around the poll (DE-D11 r3.6): an empty poll makes no
        # span; the CONSUMER span and its XACK below are traced as usual.
        with suppress_instrumentation():
            batches = await redis_client.xreadgroup(dc.CONSUMER_GROUP, consumer, {dc.STREAM: ">"}, count=10)
    except ResponseError as e:
        if "NOGROUP" not in str(e):
            raise
        # Redis lost the stream or the group (restart with empty data): recreate, no ERROR span of ours.
        await dc.ensure_group(redis_client)
        logger.warning("consumer group recreated", extra={"messaging.destination.name": dc.STREAM})
        return 0
    handled = 0
    for _stream, entries in batches or []:
        for entry_id, fields in entries:
            await consume_message(redis_client, tracer, _text(entry_id), fields, redis_host, redis_port)
            handled += 1
    return handled



async def wait_for_api(http, api_url: str, stop: asyncio.Event, every: float = 1.0) -> bool:
    """Wait until ``GET <api>/health`` answers 200 before the first order (DE-D11 r3.6).

    Untraced (the probes are not demo traffic). Returns False when stopped first.
    """
    waited = 0
    while not stop.is_set():
        try:
            with suppress_instrumentation():
                response = await http.get(f"{api_url}/health")
            if response.status_code == 200:
                logger.info("api ready", extra={"demo.wait_attempts": waited})
                return True
        except Exception:  # noqa: BLE001 - not up yet
            pass
        waited += 1
        try:
            await asyncio.wait_for(stop.wait(), timeout=every)
        except asyncio.TimeoutError:
            pass
    return False


async def _guarded(step) -> None:
    try:
        await step()
    except Exception as e:  # noqa: BLE001 - keep looping; the type is enough to diagnose
        logger.warning("loop step failed", extra={"error.type": type(e).__name__})


async def _every(seconds: float, stop: asyncio.Event, step, concurrent: bool = False) -> None:
    """Run ``step`` at a fixed rate: ticks at start + n × ``seconds`` (DE-D7 r3.5).

    ``concurrent``: each tick starts the step as a task, so a slow request
    never delays the next one (the driver); otherwise a step that overruns its
    tick makes the missed ticks lapse (the poller), never a burst of catch-up runs.
    """
    loop = asyncio.get_running_loop()
    pending: set[asyncio.Task] = set()
    deadline = loop.time()
    while not stop.is_set():
        if concurrent:
            task = asyncio.create_task(_guarded(step))
            pending.add(task)
            task.add_done_callback(pending.discard)
        else:
            await _guarded(step)
        deadline += seconds
        now = loop.time()
        if deadline < now:  # overran: skip the missed ticks
            deadline = now + seconds - (now - deadline) % seconds
        try:
            await asyncio.wait_for(stop.wait(), timeout=deadline - now)
        except asyncio.TimeoutError:
            pass
    if pending:
        await asyncio.gather(*pending)


async def run() -> None:  # pragma: no cover - process wiring, run by the image smoke
    import httpx
    import redis.asyncio as aioredis

    tracer = staffops_otel_helper.get_tracer(dc.SCOPE)
    api_url = os.environ.get("DEMO_API_URL", f"http://demo-{dc.LANG}-api:8080").rstrip("/")
    host, port = dc.split_host_port(os.environ.get("DEMO_REDIS_ADDR", "demo-redis:6379"), 6379)
    password = dc.read_secret_file(os.environ.get("DEMO_REDIS_PASSWORD_FILE"), "DEMO_REDIS_PASSWORD_FILE")
    interval = dc.env_float("DEMO_INTERVAL_SECONDS", 5.0)
    poll = dc.env_float("DEMO_POLL_SECONDS", 2.0)
    consumer = socket.gethostname()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    redis_client = aioredis.Redis(host=host, port=port, password=password)
    seq = 0

    async def drive() -> None:
        nonlocal seq
        seq += 1  # seq starts at 1 (DE-D7 r3.5)
        current = seq
        await drive_once(http, api_url, tracer, current)

    async def consume() -> None:
        await poll_once(redis_client, tracer, consumer, host, port)

    async with httpx.AsyncClient(timeout=10.0) as http:
        await dc.ensure_group(redis_client)
        logger.info("worker started", extra={"demo.interval_seconds": interval, "demo.poll_seconds": poll})

        async def drive_when_ready() -> None:
            if await wait_for_api(http, api_url, stop):
                await _every(interval, stop, drive, concurrent=True)

        await asyncio.gather(drive_when_ready(), _every(poll, stop, consume))
    await redis_client.aclose()


def main() -> None:  # pragma: no cover - process wiring, run by the image smoke
    from staffops_otel_helper.ext.redis import instrument_redis

    instrument_redis()  # before setup_telemetry(): the 0.3 extension mechanism
    staffops_otel_helper.setup_telemetry()
    try:
        asyncio.run(run())
    finally:
        try:
            staffops_otel_helper.shutdown_telemetry(timeout_ms=5_000)
        except RuntimeError as e:
            logger.warning("telemetry shutdown incomplete: %s", e)


if __name__ == "__main__":  # pragma: no cover
    main()
