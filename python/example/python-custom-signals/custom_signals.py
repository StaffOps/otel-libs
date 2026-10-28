"""Custom signals with otel-helper: spans, metrics and logs correlated with the active trace.

A short-lived job that processes one batch of three simulated orders (ord-3
fails) and exits. It shows the three custom-signal APIs an application uses on
top of the helper's automatic instrumentation, with the same names and trace
shape as the Go twin (go/example/go-custom-signals):

- spans: a batch root span (``start_root_span``) with one child span per order,
  attributes, an event, status and exception recording;
- metrics: counter, histogram, up-down counter and observable gauge, each with
  attributes;
- logs: standard ``logging`` records, exported with the trace_id/span_id of the
  span that is current when the record is emitted.

Run it against a Collector (see ../README.md, "Custom signals"):

    OTEL_SERVICE_NAME=python-custom-signals \\
    OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317 \\
    python custom_signals.py
"""

# otel_helper must be imported before any opentelemetry module so that
# unsupported OTEL_PROPAGATORS values are skipped instead of raising. isort sorts
# "opentelemetry" before "otel_helper"; the skip marker keeps this line first.
import otel_helper  # isort: skip  # noqa: F401

import logging
import signal
import threading
import time
from dataclasses import dataclass

from opentelemetry.metrics import CallbackOptions, Observation
from opentelemetry.trace import Status, StatusCode

from otel_helper import (
    get_meter,
    get_tracer,
    setup_telemetry,
    shutdown_telemetry,
    start_root_span,
)

logger = logging.getLogger("custom_signals")

# Instrumentation scope name of the tracer and the meter.
SCOPE = "orders"


class PaymentDeclinedError(Exception):
    """Business error of a failed charge, recorded on the span and logged."""


@dataclass(frozen=True)
class Order:
    id: str
    kind: str
    amount: float
    fail: bool = False


ORDERS = [
    Order("ord-1", "standard", 42.5),
    Order("ord-2", "express", 99.9),
    Order("ord-3", "standard", 10, fail=True),
]

# Set by SIGTERM / SIGINT: the loop stops taking new orders, the flush still runs.
_stop = threading.Event()

# State read by the observable gauge callback (the SDK calls it at every collection).
_queue = {"orders": len(ORDERS)}


def _observe_queue_depth(options: CallbackOptions):
    yield Observation(_queue["orders"], {"queue.name": "orders"})


def charge(order: Order) -> None:
    if order.fail:
        raise PaymentDeclinedError("card declined by issuer")


def main() -> None:
    setup_telemetry()

    tracer = get_tracer(SCOPE)
    meter = get_meter(SCOPE)

    # Instrument names follow OTel conventions: dot-separated, unit in `unit`,
    # no `_total` suffix (the Prometheus exporter adds `_total` to counters).
    processed = meter.create_counter(
        "order.processed", unit="{order}", description="Orders processed")
    # Second-based histograms need their own boundaries: the SDK defaults
    # (0, 5, 10, ... 10000) suit milliseconds.
    duration = meter.create_histogram(
        "order.duration", unit="s", description="Time spent processing one order",
        explicit_bucket_boundaries_advisory=[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1])
    active = meter.create_up_down_counter(
        "order.active", unit="{order}", description="Orders being processed now")
    meter.create_observable_gauge(
        "order.queue.depth", callbacks=[_observe_queue_depth], unit="{order}",
        description="Orders waiting in the queue")

    # A root span starts a new trace, whatever span is current (workers,
    # consumers, cron jobs: one trace per unit of work).
    with start_root_span(tracer, "orders.process-batch",
                         attributes={"batch.size": len(ORDERS)}) as batch:
        print(f"trace_id={batch.get_span_context().trace_id:032x}", flush=True)

        failed = 0
        for order in ORDERS:
            if _stop.is_set():
                logger.warning("batch interrupted", extra={"orders.pending": _queue["orders"]})
                break
            # Child span of the batch span (the current span).
            with tracer.start_as_current_span(
                "order.process",
                # order.id is fine on spans; never as a metric attribute.
                attributes={"order.id": order.id, "order.type": order.kind},
            ) as span:
                started = time.perf_counter()
                attrs = {"order.type": order.kind}
                active.add(1, attrs)
                try:
                    # An event is a timestamped annotation inside the span.
                    span.add_event("order.validated", {"order.amount": order.amount})
                    time.sleep(0.02)  # simulated call to a payment provider
                    try:
                        charge(order)
                    except PaymentDeclinedError as e:
                        # Handled error: record it explicitly. An exception that
                        # leaves start_as_current_span() is recorded and sets ERROR
                        # automatically.
                        span.record_exception(e)
                        span.set_status(Status(StatusCode.ERROR, "payment declined"))
                        # error.type only on failure, low-cardinality: the class name.
                        attrs = {**attrs, "error.type": type(e).__name__}
                        failed += 1
                        # Correlated with order.process (the current span);
                        # exc_info adds exception.type / exception.message.
                        logger.error("order failed", exc_info=e, extra={"order.id": order.id})
                    else:
                        logger.info("order processed",
                                    extra={"order.id": order.id, "order.amount": order.amount})
                finally:
                    active.add(-1, {"order.type": order.kind})
                    duration.record(time.perf_counter() - started, attrs)
                    processed.add(1, attrs)
                    _queue["orders"] -= 1

        if failed:
            # Status only: the error itself was recorded on the child span.
            batch.set_status(Status(StatusCode.ERROR, f"{failed} of {len(ORDERS)} orders failed"))
        # Correlated with orders.process-batch (the current span again).
        logger.info("batch finished", extra={"batch.size": len(ORDERS), "orders.failed": failed})


if __name__ == "__main__":
    # Python's default SIGTERM handler kills the process without running finally
    # blocks: turn SIGTERM/SIGINT into a stop request, so the flush below runs.
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: _stop.set())
    try:
        main()
    finally:
        # Flushes the batch span/log processors and exports the last metric interval.
        try:
            shutdown_telemetry(timeout_ms=5_000)
        except RuntimeError as e:
            logger.warning("telemetry shutdown incomplete: %s", e)
