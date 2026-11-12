"""W3C propagation through the helper-configured global propagator (P17).

Formerly a script with import-time setup that CI had to --ignore; these are
regular tests using the conftest isolation fixture.
"""

import pytest
from opentelemetry import baggage, metrics, trace
from opentelemetry.propagate import extract, inject

from staffops_otel_helper import TelemetryOptions, setup_telemetry


@pytest.fixture(autouse=True)
def _shutdown_providers():
    """Undo what setup_telemetry installed: instrumentors, then providers."""
    yield
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

    for instrumentor in (HTTPXClientInstrumentor(), FastAPIInstrumentor()):
        if instrumentor.is_instrumented_by_opentelemetry:
            instrumentor.uninstrument()
    for provider in (trace.get_tracer_provider(), metrics.get_meter_provider()):
        shutdown = getattr(provider, "shutdown", None)
        if shutdown:
            shutdown()


def _setup() -> trace.Tracer:
    setup_telemetry(TelemetryOptions(service_name="propagation-test", prometheus_metrics_port=0))
    return trace.get_tracer("propagation-test")


def test_inject_writes_traceparent_inside_span():
    """P17: real propagation tests, setup inside the test (not at import)."""
    tracer = _setup()
    with tracer.start_as_current_span("parent") as span:
        headers: dict[str, str] = {}
        inject(headers)
    trace_id = format(span.get_span_context().trace_id, "032x")
    assert headers["traceparent"].split("-")[1] == trace_id


def test_extract_continues_the_remote_trace():
    tracer = _setup()
    with tracer.start_as_current_span("parent"):
        headers: dict[str, str] = {}
        inject(headers)
    with tracer.start_as_current_span("child", context=extract(headers)) as child:
        child_ctx = child.get_span_context()
    assert format(child_ctx.trace_id, "032x") == headers["traceparent"].split("-")[1]
    assert child.parent is not None and child.parent.is_remote


def test_baggage_is_propagated():
    _setup()
    headers: dict[str, str] = {}
    inject(headers, context=baggage.set_baggage("tenant", "acme"))
    assert headers.get("baggage") == "tenant=acme"
    assert baggage.get_baggage("tenant", extract(headers)) == "acme"


def test_http_client_and_server_instrumentors_active():
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

    _setup()
    assert HTTPXClientInstrumentor().is_instrumented_by_opentelemetry
    assert FastAPIInstrumentor().is_instrumented_by_opentelemetry
