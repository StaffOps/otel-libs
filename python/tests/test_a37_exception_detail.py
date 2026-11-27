"""HTTP, gRPC and framework exception detail (task A37, ADR-014 decision 4, contract design D15 amendment 2026-11-19).

Server spans (FastAPI / ASGI, gRPC server): type-only ``exception`` event, no
status description, one correlated ERROR record per span on the
``staffops_otel_helper.failure`` logger, sampled or not, none for a
cancellation. Client spans (requests, httpx, gRPC client): type-only event,
no description, no record.
"""

from __future__ import annotations

import asyncio
import logging
from concurrent import futures

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.sampling import ALWAYS_OFF
from opentelemetry.trace import SpanKind, Status, StatusCode

from staffops_otel_helper import TelemetryOptions, _exception_detail, setup_telemetry
from staffops_otel_helper._exception_detail import HelperTracerProvider, scope_rule

FAILURE_LOGGER = "staffops_otel_helper.failure"
SECRET = "bound-secret-4711"


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.NOTSET)
        self.records: list[tuple[logging.LogRecord, trace.SpanContext]] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append((record, trace.get_current_span().get_span_context()))


@pytest.fixture
def records():
    capture = _Capture()
    lg = logging.getLogger(FAILURE_LOGGER)
    lg.addHandler(capture)
    yield capture
    lg.removeHandler(capture)


def _provider(sampler=None):
    exporter = InMemorySpanExporter()
    provider = HelperTracerProvider(**({"sampler": sampler} if sampler is not None else {}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter


def _no_detail(span):
    """No message or stack anywhere on the span: description, event attributes, attributes."""
    assert span.status.description is None, span.status
    for event in span.events:
        assert set(event.attributes) <= {"exception.type"}, event.attributes
    # The URL attributes keep the request URL by design (A28 redacts only the listed query names).
    kept = {k: v for k, v in span.attributes.items() if k not in ("url.full", "http.url", "http.target")}
    blob = repr([dict(e.attributes) for e in span.events]) + repr(kept)
    assert SECRET not in blob, blob


def _exception_types(span):
    return [e.attributes["exception.type"] for e in span.events if e.name == "exception"]


def _raise(exc):
    try:
        raise exc
    except BaseException as caught:  # noqa: BLE001
        return caught


# ---------------------------------------------------------------- scope rule and provider


@pytest.mark.parametrize("name, want", [
    ("opentelemetry.instrumentation.fastapi", "http"),
    ("opentelemetry.instrumentation.asgi", "http"),
    ("opentelemetry.instrumentation.grpc", "grpc"),
    ("opentelemetry.instrumentation.grpc._server", "grpc"),
    ("opentelemetry.instrumentation.requests", None),
    ("opentelemetry.instrumentation.httpx", None),
    ("opentelemetry.instrumentation.sqlalchemy", False),
    ("opentelemetry.instrumentation.botocore", False),
    ("opentelemetry.instrumentation.grpcx", False),
    ("app", False),
    (None, False),
])
def test_scope_rule(name, want):
    assert scope_rule(name) is want


def test_other_scopes_get_the_plain_sdk_tracer():
    provider, _ = _provider()
    from opentelemetry.sdk.trace import Tracer

    assert isinstance(provider.get_tracer("app"), Tracer)
    assert isinstance(provider.get_tracer(instrumenting_module_name="app"), Tracer)
    assert not isinstance(provider.get_tracer("opentelemetry.instrumentation.requests"), Tracer)


def test_use_span_code_is_found():
    """The escape hook needs use_span's generator code (API 1.42–1.45 keep __wrapped__); the self-test arms."""
    assert _exception_detail._USE_SPAN_CODE is not None
    assert _exception_detail.check_use_span_hook() is True


def test_use_span_hook_missing_warns_once(monkeypatch, caplog):
    from staffops_otel_helper import _diag

    monkeypatch.setattr(_diag, "_emitted", set())
    monkeypatch.setattr(_exception_detail, "_USE_SPAN_CODE", None)
    with caplog.at_level(logging.WARNING, logger="staffops_otel_helper.tracing"):
        assert _exception_detail.check_use_span_hook() is False
        HelperTracerProvider()
    lines = [r.getMessage() for r in caplog.records if "use_span hook" in r.getMessage()]
    assert len(lines) == 1 and "Unsampled HTTP and gRPC server spans write no exception record" in lines[0]


def test_use_span_hook_that_does_not_arm_warns(monkeypatch, caplog):
    from staffops_otel_helper import _diag

    monkeypatch.setattr(_diag, "_emitted", set())
    monkeypatch.setattr(_exception_detail, "_USE_SPAN_CODE", (lambda: None).__code__)  # another use_span shape
    with caplog.at_level(logging.WARNING, logger="staffops_otel_helper.tracing"):
        assert _exception_detail.check_use_span_hook() is False
    assert any("self-test did not arm" in r.getMessage() for r in caplog.records)


def test_setup_installs_the_helper_provider():
    setup_telemetry(TelemetryOptions(service_name="s", metric_exporters=["none"]))
    assert isinstance(trace.get_tracer_provider(), HelperTracerProvider)


# ---------------------------------------------------------------- client rule


def test_client_span_type_only_event_no_description_no_record(records):
    provider, exporter = _provider()
    tracer = provider.get_tracer("opentelemetry.instrumentation.requests")
    exc = _raise(ConnectionError(f"refused /p?q={SECRET}"))
    with pytest.raises(ConnectionError):
        with tracer.start_as_current_span("GET", kind=SpanKind.CLIENT) as span:
            span.record_exception(exc)  # contrib's own call, then use_span records the same object
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            raise exc
    (s,) = exporter.get_finished_spans()
    assert s.status.status_code is StatusCode.ERROR
    _no_detail(s)
    assert _exception_types(s) == ["ConnectionError"]
    assert records.records == []


def test_status_code_form_and_ok_pass_through():
    provider, exporter = _provider()
    tracer = provider.get_tracer("opentelemetry.instrumentation.httpx")
    a = tracer.start_span("a", kind=SpanKind.CLIENT)
    a.set_status(StatusCode.ERROR, f"boom {SECRET}")
    a.end()
    b = tracer.start_span("b", kind=SpanKind.CLIENT)
    b.set_status(Status(StatusCode.OK))
    b.end()
    spans = {s.name: s for s in exporter.get_finished_spans()}
    assert spans["a"].status.status_code is StatusCode.ERROR and spans["a"].status.description is None
    assert spans["b"].status.status_code is StatusCode.OK


def test_asgi_internal_span_follows_the_client_rule(records):
    provider, exporter = _provider()
    span = provider.get_tracer("opentelemetry.instrumentation.asgi").start_span("send", kind=SpanKind.INTERNAL)
    span.record_exception(_raise(ValueError(SECRET)))
    span.end()
    (s,) = exporter.get_finished_spans()
    _no_detail(s)
    assert _exception_types(s) == ["ValueError"]
    assert records.records == []


# ---------------------------------------------------------------- server rule


def test_server_span_one_record_with_the_span_ids(records):
    provider, exporter = _provider()
    tracer = provider.get_tracer("opentelemetry.instrumentation.fastapi")
    span = tracer.start_span("POST /orders", kind=SpanKind.SERVER,
                             attributes={"http.request.method": "POST", "url.query": "q=1"})
    first = _raise(ValueError(f"order {SECRET} rejected"))
    span.record_exception(first)
    span.record_exception(first)  # the same object again: no second event
    span.record_exception(_raise(KeyError("other")))  # a second failure: an event, not a second record
    span.set_status(Status(StatusCode.ERROR, f"ValueError: order {SECRET} rejected"))
    span.set_attribute("http.route", "/orders")
    span.set_attribute("http.response.status_code", 500)
    span.set_attribute("error.type", "500")
    span.end()
    span.end()  # a second end writes nothing
    (s,) = exporter.get_finished_spans()
    _no_detail(s)
    assert _exception_types(s) == ["ValueError", "KeyError"]
    assert len(records.records) == 1
    record, ctx = records.records[0]
    assert ctx.span_id == s.context.span_id and ctx.trace_id == s.context.trace_id
    assert record.levelno == logging.ERROR
    assert record.getMessage() == f"order {SECRET} rejected"
    assert getattr(record, "exception.type") == "ValueError"
    assert getattr(record, "exception.message") == f"order {SECRET} rejected"
    assert "test_a37_exception_detail.py" in getattr(record, "exception.stacktrace")
    assert getattr(record, "error.type") == "500"
    assert getattr(record, "http.request.method") == "POST"
    assert getattr(record, "http.route") == "/orders"
    assert getattr(record, "http.response.status_code") == 500  # the attribute's type is kept
    assert not hasattr(record, "url.query")


def test_server_cancellation_keeps_the_event_writes_no_record(records):
    provider, exporter = _provider()
    span = provider.get_tracer("opentelemetry.instrumentation.grpc").start_span("rpc", kind=SpanKind.SERVER)
    span.record_exception(_raise(asyncio.CancelledError()))
    span.end()
    (s,) = exporter.get_finished_spans()
    assert _exception_types(s) == ["asyncio.exceptions.CancelledError"]
    assert records.records == []


def test_server_description_only_failure_is_the_record_body(records):
    provider, exporter = _provider()
    span = provider.get_tracer("opentelemetry.instrumentation.grpc").start_span("rpc", kind=SpanKind.SERVER)
    span.set_status(Status(StatusCode.ERROR, f"StatusCode.INTERNAL:{SECRET}"))
    span.end()
    (s,) = exporter.get_finished_spans()
    _no_detail(s)
    (record, _), = records.records
    assert record.getMessage() == f"StatusCode.INTERNAL:{SECRET}"
    assert getattr(record, "exception.message") == f"StatusCode.INTERNAL:{SECRET}"
    assert getattr(record, "error.type") == "UNKNOWN"  # no explicit code: what the client sees
    assert not hasattr(record, "exception.stacktrace")


def test_bare_abort_exception_takes_the_dropped_description(records):
    """The sync context.abort raises a bare Exception(): the details are in the dropped description."""
    provider, _ = _provider()
    span = provider.get_tracer("opentelemetry.instrumentation.grpc").start_span("rpc", kind=SpanKind.SERVER)
    span.set_status(Status(StatusCode.ERROR, f"StatusCode.UNKNOWN:{SECRET}"))
    span.record_exception(_raise(Exception()))
    span.end()
    (record, _), = records.records
    assert record.getMessage() == f"StatusCode.UNKNOWN:{SECRET}"
    assert getattr(record, "exception.type") == "Exception"


def test_ok_server_span_writes_nothing(records):
    provider, exporter = _provider()
    span = provider.get_tracer("opentelemetry.instrumentation.fastapi").start_span("GET /", kind=SpanKind.SERVER)
    span.end()
    assert exporter.get_finished_spans()[0].events == ()
    assert records.records == []


def test_unsampled_server_span_writes_its_record_through_use_span(records):
    """contrib records nothing on an unsampled span; use_span's handler still sees the exception."""
    provider, exporter = _provider(ALWAYS_OFF)
    tracer = provider.get_tracer("opentelemetry.instrumentation.asgi")
    span = tracer.start_span("GET /x", kind=SpanKind.SERVER, attributes={"http.request.method": "GET"})
    assert not span.is_recording()
    with pytest.raises(ValueError):
        with trace.use_span(span, end_on_exit=False):
            raise ValueError(f"unsampled {SECRET}")
    with pytest.raises(asyncio.CancelledError):  # not caught by use_span's handler: nothing
        with trace.use_span(span, end_on_exit=False):
            raise asyncio.CancelledError()
    assert exporter.get_finished_spans() == ()
    (record, ctx), = records.records
    assert ctx == span.get_span_context() and ctx.is_valid and not ctx.trace_flags.sampled
    assert record.getMessage() == f"unsampled {SECRET}"
    # No response status seen (review N6): the exception's type; no response started, so 500 is the answer (N2).
    assert getattr(record, "error.type") == "ValueError"
    assert getattr(record, "http.response.status_code") == 500
    assert getattr(record, "http.request.method") == "GET"
    span.end()
    assert len(records.records) == 1


@pytest.mark.parametrize("sampler", [None, ALWAYS_OFF])
def test_grpc_server_record_follows_the_code(records, sampler):
    """R1: only the six server-error codes, or an escaping exception without a code, write a record."""
    provider, _ = _provider(sampler)
    tracer = provider.get_tracer("opentelemetry.instrumentation.grpc")

    def call(code=None, escape=False, status=True):
        span = tracer.start_span("rpc", kind=SpanKind.SERVER, attributes={"rpc.grpc.status_code": 0})
        exc = _raise(RuntimeError(f"rpc {SECRET}"))
        span.record_exception(exc)  # contrib's interceptor records without checking is_recording
        span.record_exception(exc)
        if code is not None:
            span.set_attribute("rpc.grpc.status_code", code)  # contrib's servicer context, sampled or not
            if status:
                span.set_status(Status(StatusCode.ERROR, f"code {code}:{SECRET}"))
        if escape:
            with pytest.raises(RuntimeError):
                with trace.use_span(span, end_on_exit=False):
                    raise exc
        span.end()

    call(code=5)                       # NOT_FOUND: no record
    call(code=3, status=False)         # INVALID_ARGUMENT: no record
    call()                             # an app-recorded exception only: no record
    assert records.records == []
    call(code=13)                      # INTERNAL: a record
    call(escape=True)                  # an uncaught exception without a code: a record (UNKNOWN)
    call(code=5, escape=True)          # an abort with NOT_FOUND escapes too: still none
    assert [getattr(r, "error.type") for r, _ in records.records] == ["INTERNAL", "UNKNOWN"]
    assert [getattr(r, "rpc.grpc.status_code", None) for r, _ in records.records] == [13, None]
    assert all(r.getMessage() == f"rpc {SECRET}" for r, _ in records.records)


@pytest.mark.parametrize("sampler", [None, ALWAYS_OFF])
def test_http_server_handled_exception_writes_no_record(records, sampler):
    """An exception the app recorded and answered with 200 or 404: not a failed span (R1)."""
    provider, _ = _provider(sampler)
    tracer = provider.get_tracer("opentelemetry.instrumentation.fastapi")
    for status in (200, 404):
        span = tracer.start_span("GET /", kind=SpanKind.SERVER)
        span.record_exception(_raise(ValueError(SECRET)))
        span.set_attribute("http.response.status_code", status)
        span.end()
    assert records.records == []


def test_cancelled_failure_writes_nothing_from_the_description(records):
    provider, _ = _provider()
    span = provider.get_tracer("opentelemetry.instrumentation.grpc").start_span("rpc", kind=SpanKind.SERVER)
    span.set_status(Status(StatusCode.ERROR, "StatusCode.UNKNOWN:cancelled"))
    span.record_exception(_raise(asyncio.CancelledError()))
    span.end()
    assert records.records == []


def test_internal_failures_warn_once_by_type(monkeypatch, caplog):
    from staffops_otel_helper import _diag

    monkeypatch.setattr(_diag, "_emitted", set())
    provider, _ = _provider()
    span = provider.get_tracer("opentelemetry.instrumentation.fastapi").start_span("s", kind=SpanKind.SERVER)

    def boom(*a, **k):
        raise KeyError(SECRET)

    monkeypatch.setattr(_exception_detail, "emit_exception_record", boom)
    span.record_exception(_raise(ValueError(SECRET)))
    span.set_status(Status(StatusCode.ERROR))
    span.set_attribute("http.response.status_code", 500)  # a failed HTTP span is decided by its status (L3)
    with caplog.at_level(logging.WARNING, logger="staffops_otel_helper.tracing"):
        span.end()
    lines = [r.getMessage() for r in caplog.records if "OtelHelper" in r.getMessage()]
    assert lines == ["[OtelHelper] Could not write an exception record on an HTTP or gRPC span (KeyError)."]
    assert SECRET not in repr(lines)


def test_tracer_forwards_unknown_attributes():
    provider, _ = _provider()
    tracer = provider.get_tracer("opentelemetry.instrumentation.httpx")
    assert tracer.instrumentation_info.name == "opentelemetry.instrumentation.httpx"


def test_is_recording_outside_use_span_arms_nothing(records):
    provider, _ = _provider(ALWAYS_OFF)
    span = provider.get_tracer("opentelemetry.instrumentation.fastapi").start_span("s", kind=SpanKind.SERVER)
    try:
        raise ValueError(SECRET)
    except ValueError:
        assert span.is_recording() is False  # an app's except block: not use_span's
    assert records.records == []


def test_wrappers_never_raise(monkeypatch, records):
    provider, exporter = _provider()
    span = provider.get_tracer("opentelemetry.instrumentation.fastapi").start_span("s", kind=SpanKind.SERVER)

    def boom(*a, **k):
        raise RuntimeError("internal")

    monkeypatch.setattr(_exception_detail, "error_type", boom)
    monkeypatch.setattr(_exception_detail, "emit_exception_record", boom)
    span.record_exception(_raise(ValueError(SECRET)))
    span.end()
    (s,) = exporter.get_finished_spans()
    assert s.events == ()  # the event is dropped, never the raw text


def test_slotted_span_is_left_alone():
    class Frozen:
        __slots__ = ()

        def set_status(self, *a, **k):
            return None

        def is_recording(self):
            return True

    frozen = Frozen()
    assert _exception_detail.apply(frozen, True) is frozen


# ---------------------------------------------------------------- FastAPI (real instrumentation)


def _fastapi_app():
    import fastapi

    app = fastapi.FastAPI()

    @app.post("/orders/{order_id}")
    async def orders(order_id: str):
        raise ValueError(f"order {order_id} {SECRET}")

    @app.get("/handled")
    async def handled():
        try:
            raise ValueError(f"handled {SECRET}")
        except ValueError as exc:
            trace.get_current_span().record_exception(exc)
        return {"ok": True}

    @app.get("/answered-500")
    async def answered_500():
        try:
            raise ValueError(f"answered {SECRET}")
        except ValueError as exc:
            trace.get_current_span().record_exception(exc)
        return fastapi.responses.JSONResponse({"error": "internal"}, status_code=500)

    @app.get("/missing")
    async def missing():
        raise fastapi.HTTPException(status_code=404, detail="no such order")

    return app


def _fastapi_run(monkeypatch, sampler):
    pytest.importorskip("fastapi")
    from starlette.testclient import TestClient

    if sampler:
        monkeypatch.setenv("OTEL_TRACES_SAMPLER", sampler)
    setup_telemetry(TelemetryOptions(service_name="s", metric_exporters=["none"]))
    exporter = InMemorySpanExporter()
    trace.get_tracer_provider().add_span_processor(SimpleSpanProcessor(exporter))
    with TestClient(_fastapi_app(), raise_server_exceptions=False) as client:
        assert client.post("/orders/42?token=abc").status_code == 500
    return exporter.get_finished_spans()


def test_fastapi_500_server_span_and_record(monkeypatch, records):
    spans = _fastapi_run(monkeypatch, None)
    (server,) = [s for s in spans if s.kind is SpanKind.SERVER]
    assert server.status.status_code is StatusCode.ERROR
    for s in spans:
        _no_detail(s)
    assert _exception_types(server) == ["ValueError"]
    (record, ctx), = records.records
    assert ctx.span_id == server.context.span_id
    assert record.getMessage() == f"order 42 {SECRET}"
    # R2: the response status, sampled or not (contrib's semconv mode decides which status key is present).
    assert getattr(record, "error.type") == "500"
    status = getattr(record, "http.response.status_code", None) or getattr(record, "http.status_code", None)
    assert status == 500
    assert "in orders" in getattr(record, "exception.stacktrace")


def test_fastapi_500_unsampled_still_writes_one_record(monkeypatch, records):
    spans = _fastapi_run(monkeypatch, "always_off")
    assert spans == ()
    (record, ctx), = records.records
    assert ctx.is_valid and not ctx.trace_flags.sampled
    assert record.getMessage() == f"order 42 {SECRET}"
    assert getattr(record, "error.type") == "500"  # R2: the same value as sampled
    assert getattr(record, "http.response.status_code") == 500


@pytest.mark.parametrize("sampler", [None, "always_off"])
def test_fastapi_handled_exception_and_404_write_no_record(monkeypatch, records, sampler):
    pytest.importorskip("fastapi")
    from starlette.testclient import TestClient

    if sampler:
        monkeypatch.setenv("OTEL_TRACES_SAMPLER", sampler)
    setup_telemetry(TelemetryOptions(service_name="s", metric_exporters=["none"]))
    with TestClient(_fastapi_app(), raise_server_exceptions=False) as client:
        assert client.get("/handled").status_code == 200
        assert client.get("/missing").status_code == 404
    assert records.records == []


@pytest.mark.parametrize("sampler", [None, "always_off"])
def test_fastapi_recorded_exception_answered_500_writes_one_record(monkeypatch, records, sampler):
    """Review N1: no escape, the app answers 500 itself: one record, sampled or not, the same error.type."""
    pytest.importorskip("fastapi")
    from starlette.testclient import TestClient

    if sampler:
        monkeypatch.setenv("OTEL_TRACES_SAMPLER", sampler)
    setup_telemetry(TelemetryOptions(service_name="s", metric_exporters=["none"]))
    with TestClient(_fastapi_app(), raise_server_exceptions=False) as client:
        assert client.get("/answered-500").status_code == 500
    (record, ctx), = records.records
    assert ctx.is_valid and bool(ctx.trace_flags.sampled) is (sampler is None)
    assert record.getMessage() == f"answered {SECRET}"
    assert getattr(record, "error.type") == "500"
    assert getattr(record, "http.response.status_code") == 500


@pytest.mark.parametrize("sampler", [None, ALWAYS_OFF])
def test_escape_after_the_response_started_keeps_its_status(records, sampler):
    """Review N2: an exception escaping after a 200 started: no 500 injected, error.type = the exception's type."""
    provider, _ = _provider(sampler)
    span = provider.get_tracer("opentelemetry.instrumentation.asgi").start_span("GET /stream", kind=SpanKind.SERVER)
    import opentelemetry.instrumentation.asgi as asgi

    asgi.set_status_code(span, 200)  # what otel_send calls on http.response.start
    with pytest.raises(KeyError):
        with trace.use_span(span, end_on_exit=False):
            raise KeyError(SECRET)
    span.end()
    (record, _), = records.records
    assert getattr(record, "http.response.status_code") == 200
    assert getattr(record, "error.type") == "KeyError"


def test_asgi_status_patch_is_installed_once():
    import opentelemetry.instrumentation.asgi as asgi

    assert _exception_detail.patch_asgi_status() is True
    first = asgi.set_status_code
    assert getattr(first, "_staffops_a37", False)
    assert _exception_detail.patch_asgi_status() is True
    assert asgi.set_status_code is first


def test_tracer_getattr_does_not_recurse():
    bare = _exception_detail._ExceptionDetailTracer.__new__(_exception_detail._ExceptionDetailTracer)
    with pytest.raises(AttributeError):
        bare.anything


def test_upstream_fastapi_500_path_overwrites_the_description():
    """Evidence for the README correction (contrib alone, no helper provider): on the 500 path the
    middleware's description is replaced by a bare ERROR when ServerErrorMiddleware sends the response,
    and the event keeps the message (the pre-A37 behavior)."""
    pytest.importorskip("fastapi")
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from starlette.testclient import TestClient

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    app = _fastapi_app()
    FastAPIInstrumentor.instrument_app(app, tracer_provider=provider)
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            assert client.post("/orders/7").status_code == 500
    finally:
        FastAPIInstrumentor.uninstrument_app(app)
    (server,) = [s for s in exporter.get_finished_spans() if s.kind is SpanKind.SERVER]
    assert server.status.status_code is StatusCode.ERROR and server.status.description is None
    assert any(SECRET in str(e.attributes.get("exception.message")) for e in server.events)


# ---------------------------------------------------------------- requests and httpx (real clients)


def _client_run(call):
    setup_telemetry(TelemetryOptions(service_name="s", metric_exporters=["none"]))
    exporter = InMemorySpanExporter()
    trace.get_tracer_provider().add_span_processor(SimpleSpanProcessor(exporter))
    with pytest.raises(Exception):  # noqa: B017 - the library's own connection error
        call(f"http://127.0.0.1:1/p?q={SECRET}")
    return [s for s in exporter.get_finished_spans() if s.kind is SpanKind.CLIENT]


def test_requests_failed_call(records):
    requests = pytest.importorskip("requests")
    (s,) = _client_run(lambda url: requests.get(url, timeout=5))
    assert s.status.status_code is StatusCode.ERROR
    _no_detail(s)
    assert len(_exception_types(s)) == 1
    assert records.records == []


def test_httpx_failed_call(records):
    httpx = pytest.importorskip("httpx")
    (s,) = _client_run(lambda url: httpx.get(url, timeout=5))
    assert s.status.status_code is StatusCode.ERROR
    _no_detail(s)
    assert len(_exception_types(s)) == 1
    assert records.records == []


# ---------------------------------------------------------------- gRPC (real sync and aio)

_SERVICE = "parity.v1.A37"


def _wait(exporter, count):
    import time

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and len(exporter.get_finished_spans()) < count:
        time.sleep(0.02)
    return exporter.get_finished_spans()


def _grpc_exporter(monkeypatch, sampler):
    if sampler:
        monkeypatch.setenv("OTEL_TRACES_SAMPLER", sampler)
    setup_telemetry(TelemetryOptions(service_name="s", metric_exporters=["none"]))
    exporter = InMemorySpanExporter()
    trace.get_tracer_provider().add_span_processor(SimpleSpanProcessor(exporter))
    return exporter


@pytest.mark.parametrize("sampler", [None, "always_off"])
def test_sync_grpc_raise_and_abort(monkeypatch, records, sampler):
    grpc = pytest.importorskip("grpc")
    exporter = _grpc_exporter(monkeypatch, sampler)

    def unary(request, context):
        if request == b"raise":
            raise ValueError(f"handler {SECRET}")
        code = grpc.StatusCode.NOT_FOUND if request == b"notfound" else grpc.StatusCode.INTERNAL
        context.abort(code, f"details {SECRET}")

    server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
    server.add_generic_rpc_handlers((grpc.method_handlers_generic_handler(_SERVICE, {
        "Unary": grpc.unary_unary_rpc_method_handler(unary)}),))
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    bodies = []
    try:
        with grpc.insecure_channel(f"127.0.0.1:{port}") as channel:
            for request in (b"raise", b"abort", b"notfound"):
                exporter.clear()
                before = len(records.records)
                with pytest.raises(grpc.RpcError):
                    channel.unary_unary(f"/{_SERVICE}/Unary")(request, timeout=5)
                spans = _wait(exporter, 0 if sampler else 2)
                for s in spans:
                    _no_detail(s)
                if not sampler:
                    (srv,) = [s for s in spans if s.kind is SpanKind.SERVER]
                    (cli,) = [s for s in spans if s.kind is SpanKind.CLIENT]
                    assert len(_exception_types(srv)) == 1, srv.events
                    assert len(_exception_types(cli)) <= 1, cli.events
                    assert cli.status.status_code is StatusCode.ERROR
                new = records.records[before:]
                if request == b"notfound":
                    assert new == [], [r.getMessage() for r, _ in new]  # R1: NOT_FOUND is not a server error
                    continue
                assert len(new) == 1, [r.getMessage() for r, _ in new]
                record, ctx = new[0]
                if not sampler:
                    assert ctx.span_id == srv.context.span_id
                else:
                    assert ctx.is_valid and not ctx.trace_flags.sampled
                bodies.append((record.getMessage(), getattr(record, "error.type")))
    finally:
        server.stop(0.5)
    assert bodies == [(f"handler {SECRET}", "UNKNOWN"), (f"StatusCode.INTERNAL:details {SECRET}", "INTERNAL")]


@pytest.mark.parametrize("sampler", [None, "always_off"])
def test_aio_grpc_abort_one_event_records_by_code(monkeypatch, records, sampler):
    grpc = pytest.importorskip("grpc")
    exporter = _grpc_exporter(monkeypatch, sampler)
    seen = {}

    async def run():
        server = grpc.aio.server()

        async def unary(request, context):
            if request == b"raise":
                raise ValueError(f"aio handler {SECRET}")
            code = grpc.StatusCode.INTERNAL if request == b"internal" else grpc.StatusCode.NOT_FOUND
            await context.abort(code, f"aio details {SECRET}")

        server.add_generic_rpc_handlers((grpc.method_handlers_generic_handler(_SERVICE, {
            "Unary": grpc.unary_unary_rpc_method_handler(unary)}),))
        port = server.add_insecure_port("127.0.0.1:0")
        await server.start()
        try:
            async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
                for request in (b"internal", b"notfound", b"raise"):
                    exporter.clear()
                    before = len(records.records)
                    with pytest.raises(grpc.aio.AioRpcError):
                        await channel.unary_unary(f"/{_SERVICE}/Unary")(request, timeout=5)
                    for _ in range(250):
                        if sampler or len(exporter.get_finished_spans()) >= 2:
                            break
                        await asyncio.sleep(0.02)
                    await asyncio.sleep(0.05)
                    seen[request] = (list(exporter.get_finished_spans()), records.records[before:])
        finally:
            await server.stop(0.5)

    asyncio.run(run())
    for request, (spans, new) in seen.items():
        for s in spans:
            _no_detail(s)
        if not sampler:
            (srv,) = [s for s in spans if s.kind is SpanKind.SERVER]
            assert len(_exception_types(srv)) == 1, (request, srv.events)  # AbortError once, not twice
        if request == b"notfound":
            assert new == [], [r.getMessage() for r, _ in new]  # R1: NOT_FOUND is not a server error
            continue
        if not sampler:
            assert new[0][1].span_id == srv.context.span_id
        assert len(new) == 1, (request, [r.getMessage() for r, _ in new])
    internal, raised = seen[b"internal"][1][0][0], seen[b"raise"][1][0][0]
    assert (internal.getMessage(), getattr(internal, "error.type")) == (f"StatusCode.INTERNAL:aio details {SECRET}",
                                                                         "INTERNAL")
    assert (raised.getMessage(), getattr(raised, "error.type")) == (f"aio handler {SECRET}", "UNKNOWN")
