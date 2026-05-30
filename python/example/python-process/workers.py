"""Notification Process — Background workers for queue consumption, retries, and cleanup."""

import os
import asyncio
import time
import random
import logging

import httpx
from opentelemetry import trace, context
from opentelemetry.propagate import inject

from otel_helper import setup_telemetry, get_tracer, get_meter, TelemetryOptions
from otel_helper.tracing import start_root_span

logger = logging.getLogger(__name__)

API_URL = os.getenv("API_URL", "http://python-api-stable:8000")


def _propagation_headers() -> dict:
    """Inject W3C trace context into headers for distributed tracing."""
    headers = {}
    inject(headers, context=context.get_current())
    return headers


# --- Health server (for liveness/readiness probes) ---
from aiohttp import web

async def _health_handler(request):
    return web.Response(text="ok")

async def start_health_server():
    app = web.Application()
    app.router.add_get("/health", _health_handler)
    app.router.add_get("/healthz", _health_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8080)
    await site.start()


async def queue_consumer(tracer, queue_depth):
    """Consume pending notifications from queue (simulated)."""
    async with httpx.AsyncClient() as client:
        while True:
            with start_root_span(tracer, "process.queue-consume") as span:
                batch_size = random.randint(1, 5)
                span.set_attribute("queue.batch_size", batch_size)
                queue_depth.add(batch_size)

                for i in range(batch_size):
                    with tracer.start_as_current_span("process.dispatch-notification") as item_span:
                        recipient = f"user-{random.randint(1000, 9999)}@example.com"
                        channel = random.choice(["email", "sms", "push"])
                        item_span.set_attribute("notification.recipient", recipient)
                        item_span.set_attribute("notification.channel", channel)

                        try:
                            response = await client.post(f"{API_URL}/notify", headers=_propagation_headers(), json={
                                "recipient": recipient,
                                "channel": channel,
                                "template_id": random.choice(["default", "welcome", "alert"]),
                                "variables": {"name": f"User{i}", "message": "System check"},
                            })
                            if response.status_code != 200:
                                item_span.set_status(trace.StatusCode.ERROR, f"HTTP {response.status_code}")
                        except Exception as e:
                            item_span.set_status(trace.StatusCode.ERROR, str(e))
                            logger.error("Failed to dispatch", extra={"error": str(e)})

                queue_depth.add(-batch_size)

            await asyncio.sleep(30)


async def retry_worker(tracer, retries_total):
    """Retry failed notifications."""
    async with httpx.AsyncClient() as client:
        while True:
            with start_root_span(tracer, "process.retry-cycle") as span:
                failed_count = random.randint(0, 3)
                span.set_attribute("retry.failed_count", failed_count)

                for i in range(failed_count):
                    with tracer.start_as_current_span("process.retry-attempt") as retry_span:
                        retry_span.set_attribute("retry.attempt", i + 1)
                        retries_total.add(1)

                        try:
                            await client.post(f"{API_URL}/notify", headers=_propagation_headers(), json={
                                "recipient": f"retry-{random.randint(100, 999)}@example.com",
                                "channel": "email",
                                "template_id": "alert",
                                "variables": {"message": "Retry attempt"},
                            })
                        except Exception as e:
                            retry_span.set_status(trace.StatusCode.ERROR, str(e))

                logger.info("Retry cycle complete", extra={"retried": failed_count})

            await asyncio.sleep(60)


async def cleanup_worker(tracer, cleanup_total):
    """Clean up expired notifications (older than 24h simulated)."""
    while True:
        with start_root_span(tracer, "process.cleanup") as span:
            cleaned = random.randint(0, 10)
            span.set_attribute("cleanup.removed", cleaned)
            cleanup_total.add(cleaned)

            if cleaned > 0:
                logger.info("Cleaned expired notifications", extra={"count": cleaned})

        await asyncio.sleep(180)


async def health_checker(tracer):
    """Periodically check API health."""
    async with httpx.AsyncClient() as client:
        while True:
            with start_root_span(tracer, "process.health-check") as span:
                try:
                    response = await client.get(f"{API_URL}/health", headers=_propagation_headers())
                    span.set_attribute("health.status_code", response.status_code)
                    if response.status_code != 200:
                        span.set_status(trace.StatusCode.ERROR, "API unhealthy")
                except Exception as e:
                    span.set_status(trace.StatusCode.ERROR, str(e))
                    logger.warning("Health check failed", extra={"error": str(e)})

            await asyncio.sleep(60)


async def main():
    setup_telemetry(TelemetryOptions(
        resource_attributes={"app.component": "notification-workers"},
    ))
    tracer = get_tracer("python-process")
    meter = get_meter("python-process")
    queue_depth = meter.create_up_down_counter("notifications.queue_depth")
    retries_total = meter.create_counter("notifications.retries_total")
    cleanup_total = meter.create_counter("notifications.cleaned_total")

    logger.info("Starting notification workers")
    await start_health_server()
    await asyncio.gather(
        queue_consumer(tracer, queue_depth),
        retry_worker(tracer, retries_total),
        cleanup_worker(tracer, cleanup_total),
        health_checker(tracer),
    )


if __name__ == "__main__":
    asyncio.run(main())
