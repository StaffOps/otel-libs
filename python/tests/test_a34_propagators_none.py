"""A34: ``OTEL_PROPAGATORS=none`` must never fail a request (design D1 propagators row).

``none`` disables propagation in both directions: a server starts a new trace,
inbound baggage is dropped and nothing is injected. Before the fix the helper's
empty composite returned ``None`` from ``extract``; the ASGI instrumentation
attached it and every request ended in a 500.
"""

from __future__ import annotations

import os

import pytest
from opentelemetry import baggage, metrics, trace
from opentelemetry.context import Context
from opentelemetry.propagate import extract, get_global_textmap, inject

from staffops_otel_helper import TelemetryOptions, setup_telemetry

INBOUND_TRACE_ID = "0af7651916cd43dd8448eb211c80319c"
TRACEPARENT = f"00-{INBOUND_TRACE_ID}-b7ad6b7169203331-01"


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


def _setup(propagators: str, **env: str) -> None:
    os.environ["OTEL_PROPAGATORS"] = propagators
    os.environ.update({"OTEL_METRICS_EXPORTER": "none", "OTEL_LOGS_EXPORTER": "none", **env})
    setup_telemetry(TelemetryOptions(service_name="a34-test", prometheus_metrics_port=0))


class TestExtractNeverReturnsNone:
    """Every helper propagator path returns a Context from extract (A34)."""

    @pytest.mark.parametrize("value", [
        "none", "tracecontext,none", "none,b3", "tracecontext", "baggage", "b3,baggage", "foo", "",
    ])
    def test_extract_without_context(self, value):
        from staffops_otel_helper._propagators import build_propagator

        os.environ["OTEL_PROPAGATORS"] = value
        propagator = build_propagator()
        for carrier in ({}, {"traceparent": TRACEPARENT}, {"baggage": "user.id=42"}):
            assert isinstance(propagator.extract(carrier), Context)

    def test_none_keeps_the_passed_context_and_reads_nothing(self):
        from staffops_otel_helper._propagators import build_propagator

        os.environ["OTEL_PROPAGATORS"] = "none"
        propagator = build_propagator()
        passed = baggage.set_baggage("k", "v", Context())
        carrier = {"traceparent": TRACEPARENT, "baggage": "user.id=42"}
        assert propagator.extract(carrier, passed) is passed
        fresh = propagator.extract(carrier)
        assert isinstance(fresh, Context)
        assert not trace.get_current_span(fresh).get_span_context().is_valid
        assert baggage.get_all(fresh) == {}

    def test_none_injects_nothing(self):
        from staffops_otel_helper._propagators import build_propagator

        os.environ["OTEL_PROPAGATORS"] = "none"
        _setup("none")
        tracer = trace.get_tracer("a34")
        with tracer.start_as_current_span("root"):
            headers: dict[str, str] = {}
            inject(headers, context=baggage.set_baggage("tenant", "acme"))
        assert headers == {}
        assert build_propagator().fields == set()
        # The global API path, the one every instrumentation uses.
        assert isinstance(extract({"traceparent": TRACEPARENT}), Context)
        assert get_global_textmap().fields == set()


def test_extract_as_span_parent_starts_a_new_root_under_none():
    """Consumers and gRPC servers pass extract(carrier) as the parent: a new root, not the current span."""
    _setup("none", OTEL_TRACES_EXPORTER="none")
    tracer = trace.get_tracer("a34")
    with tracer.start_as_current_span("current") as current:
        child = tracer.start_span("consumer", context=extract({"traceparent": TRACEPARENT}))
        child.end()
    assert child.parent is None
    assert child.get_span_context().trace_id != current.get_span_context().trace_id
    assert format(child.get_span_context().trace_id, "032x") != INBOUND_TRACE_ID


def _app():
    # Imported after setup on purpose: FastAPIInstrumentor patches the class.
    from fastapi import FastAPI

    app = FastAPI()

    @app.get("/in")
    def _handler() -> dict:
        span_context = trace.get_current_span().get_span_context()
        outbound: dict[str, str] = {}
        inject(outbound)  # what an instrumented outbound client does
        return {
            "trace_id": format(span_context.trace_id, "032x"),
            "valid": span_context.is_valid,
            "baggage": dict(baggage.get_all()),
            "outbound": outbound,
        }

    return app


@pytest.mark.parametrize("env", [
    {"OTEL_TRACES_EXPORTER": "console"},
    {"OTEL_TRACES_EXPORTER": "none"},
], ids=["traces-console", "traces-exporter-none"])
@pytest.mark.parametrize("headers", [
    {},
    {"traceparent": TRACEPARENT},
    {"baggage": "user.id=42"},
    {"traceparent": TRACEPARENT, "baggage": "user.id=42"},
    {"traceparent": "garbage"},
], ids=["no-headers", "traceparent", "baggage", "traceparent-and-baggage", "invalid-traceparent"])
def test_fastapi_request_under_none(headers, env, capsys):
    from fastapi.testclient import TestClient

    _setup("none", **env)
    client = TestClient(_app(), raise_server_exceptions=False)
    response = client.get("/in", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["valid"] is True  # the server span exists: a new trace
    assert body["trace_id"] != INBOUND_TRACE_ID  # the inbound trace is not continued
    assert body["baggage"] == {}  # inbound baggage is not extracted
    assert body["outbound"] == {}  # nothing is injected
    capsys.readouterr()  # console span output is not under test


def test_fastapi_request_under_none_with_traces_disabled():
    from fastapi.testclient import TestClient

    _setup("none", OTEL_HELPER_DISABLED_SIGNALS="traces")
    client = TestClient(_app(), raise_server_exceptions=False)
    response = client.get("/in", headers={"traceparent": TRACEPARENT, "baggage": "user.id=42"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["valid"] is False and body["baggage"] == {} and body["outbound"] == {}
