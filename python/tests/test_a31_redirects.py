"""Contract task A31 (design D1 row "OTLP export redirects"): an OTLP/HTTP export answered with
a 3xx is a failed export, never followed, and the helper writes one ERROR per signal and status
naming both, never the Location. The parity fixture covers the wire side
(export.http_redirect_refused_warned); these tests use an in-process server whose redirect
answers also carry the Location, token included, in the body ("Redirecting to <url>")."""

from __future__ import annotations

import http.server
import logging
import re
import threading
from collections.abc import Iterator

import pytest
import requests

from staffops_otel_helper import _exporters
from staffops_otel_helper._exporters import build_otlp_exporter
from staffops_otel_helper.config import TelemetryOptions

TOKEN = "a31-redirect-token"

HEADER_VALUE = "a31-header-value"


class _Server:
    """One in-process HTTP server: /redirect-<code>/... answers <code> with a Location on
    /redirected/... carrying a token; every other path answers 200. Records every path."""

    def __init__(self) -> None:
        paths: list[str] = []
        self.paths = paths

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - http.server API
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                paths.append(self.path)
                parts = self.path.split("/")
                body = b""
                if len(parts) > 1 and parts[1].startswith("redirect-"):
                    self.send_response(int(parts[1].removeprefix("redirect-")))
                    rest = "/".join(parts[2:])
                    location = f"http://{self.headers['Host']}/redirected/{rest}?token={TOKEN}"
                    self.send_header("Location", location)
                    body = f"Redirecting to {location}".encode()
                else:
                    self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args: object) -> None:
                pass

        self._httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self._httpd.server_address[1]}"
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()

    def followed(self) -> list[str]:
        return [p for p in self.paths if p.startswith("/redirected")]


@pytest.fixture
def server() -> Iterator[_Server]:
    srv = _Server()
    yield srv
    srv.close()


def _plan(monkeypatch, endpoint: str, signal: str):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", endpoint)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", f"x-a31={HEADER_VALUE}")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_COMPRESSION", "none")
    opts = TelemetryOptions(service_name="a31")
    opts.resolve_from_env()
    plan = opts.otlp_export(signal)
    assert plan is not None
    return plan


def _export(signal: str, exporter) -> object:  # noqa: ANN001 - one of three SDK exporter types
    if signal == "traces":
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

        mem = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(mem))
        provider.get_tracer("a31").start_span("a31").end()
        return exporter.export(mem.get_finished_spans())
    if signal == "metrics":
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import InMemoryMetricReader

        reader = InMemoryMetricReader()
        MeterProvider(metric_readers=[reader]).get_meter("a31").create_counter("a31").add(1)
        return exporter.export(reader.get_metrics_data())
    from opentelemetry.sdk._logs import LoggerProvider
    from opentelemetry.sdk._logs import export as log_export

    mem = getattr(log_export, "InMemoryLogRecordExporter", None) or log_export.InMemoryLogExporter
    mem = mem()
    provider = LoggerProvider()
    provider.add_log_record_processor(log_export.SimpleLogRecordProcessor(mem))
    logger = provider.get_logger("a31")
    try:
        from opentelemetry._logs import LogRecord
        logger.emit(LogRecord(body="a31"))
    except TypeError:  # pragma: no cover - SDK LogRecord signature of older releases
        from opentelemetry.sdk._logs import LogRecord as SdkLogRecord
        logger.emit(SdkLogRecord(body="a31"))
    records = mem.get_finished_logs()
    assert records
    return exporter.export(records)


def _helper_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name.startswith("staffops_otel_helper")]


def _sdk_export_errors(caplog) -> list[logging.LogRecord]:
    """The SDK exporter's own ERROR for a failed export (1.42 and 1.45 both write one)."""
    return [r for r in caplog.records
            if r.name.startswith("opentelemetry.exporter") and r.levelno == logging.ERROR
            and "Failed to export" in r.getMessage()]


def _forget_sdk_duplicates() -> None:
    """The logs exporter's logger carries the SDK DuplicateFilter (both bounds): the same message
    template is written once per 20 s window. Forget it so each export's error is observable."""
    for name in ("opentelemetry.exporter.otlp.proto.http._log_exporter",):
        for f in logging.getLogger(name).filters:
            if hasattr(f, "last_log"):
                del f.last_log


def _assert_nothing_leaked(caplog, *extra: str) -> None:
    everything = "\n".join([caplog.text, *(r.getMessage() for r in caplog.records), *extra])
    for secret in (TOKEN, "/redirected", "Redirecting to", HEADER_VALUE):
        assert secret not in everything, secret


@pytest.mark.parametrize("status", [307, 308])
@pytest.mark.parametrize("signal", ["traces", "metrics", "logs"])
def test_redirect_refused_once(monkeypatch, caplog, server, signal, status):
    caplog.set_level(logging.DEBUG)
    plan = _plan(monkeypatch, f"{server.base}/redirect-{status}", signal)
    for _ in range(2):  # two failed exports, one helper ERROR
        _forget_sdk_duplicates()
        exporter = build_otlp_exporter(plan)
        result = _export(signal, exporter)
        exporter.shutdown()
        # A refused redirect is a failed export on both exporter bounds (1.42 and 1.45).
        assert getattr(result, "name", str(result)) == "FAILURE", repr(result)
    assert server.followed() == [], "the redirect was followed"
    # One request per export: no retry, no follow-up request.
    assert server.paths == [f"/redirect-{status}/v1/{signal}"] * 2
    # Every refused batch also writes the SDK's own export error (later losses are not silent).
    sdk_errors = _sdk_export_errors(caplog)
    assert len(sdk_errors) == 2, [r.getMessage() for r in sdk_errors]
    assert all(f"status={status}" in r.getMessage() for r in sdk_errors)
    lines = _helper_lines(caplog)
    pattern = re.compile(rf"redirects are not followed.*signal={signal}.*status={status}")
    matching = [r for r in caplog.records
                if r.name.startswith("staffops_otel_helper") and pattern.search(r.getMessage())]
    assert len(matching) == 1, lines
    assert matching[0].levelno == logging.ERROR
    assert sum("redirects are not followed" in line for line in lines) == 1
    _assert_nothing_leaked(caplog)


def test_setup_path_fails_every_signal_and_leaks_nothing(monkeypatch, caplog, capfd, server):
    """Through setup_telemetry(): the body and the Location carry a token; no output names it."""
    from opentelemetry import _logs, metrics, trace

    from staffops_otel_helper import setup_telemetry, shutdown_telemetry

    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", f"{server.base}/redirect-307")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", f"x-a31={HEADER_VALUE}")
    setup_telemetry(TelemetryOptions(service_name="a31"))
    trace.get_tracer("a31").start_span("a31").end()
    metrics.get_meter("a31").create_counter("a31").add(1)
    try:
        from opentelemetry._logs import LogRecord
        _logs.get_logger("a31").emit(LogRecord(body="a31"))
    except TypeError:  # pragma: no cover - SDK LogRecord signature of older releases
        from opentelemetry.sdk._logs import LogRecord as SdkLogRecord
        _logs.get_logger("a31").emit(SdkLogRecord(body="a31"))
    shutdown_telemetry()
    assert server.followed() == []
    assert sorted(set(server.paths)) == [f"/redirect-307/v1/{s}" for s in ("logs", "metrics", "traces")]
    for signal in ("traces", "metrics", "logs"):
        assert any(f"signal={signal} status=307" in line for line in _helper_lines(caplog)), signal
    out, err = capfd.readouterr()
    _assert_nothing_leaked(caplog, out, err)


def test_once_per_signal_and_status(monkeypatch, caplog, server):
    caplog.set_level(logging.DEBUG)
    for status in (307, 307, 302):
        plan = _plan(monkeypatch, f"{server.base}/redirect-{status}", "traces")
        exporter = build_otlp_exporter(plan)
        _export("traces", exporter)
        exporter.shutdown()
    lines = [line for line in _helper_lines(caplog) if "redirects are not followed" in line]
    assert len(lines) == 2, lines
    assert any(line.endswith("signal=traces status=307") for line in lines)
    assert any(line.endswith("signal=traces status=302") for line in lines)
    assert server.followed() == []


def test_success_writes_nothing(monkeypatch, caplog, server):
    caplog.set_level(logging.DEBUG)
    plan = _plan(monkeypatch, server.base, "logs")
    exporter = build_otlp_exporter(plan)
    assert getattr(_export("logs", exporter), "name", "") == "SUCCESS"
    exporter.shutdown()
    assert not [line for line in _helper_lines(caplog) if "redirect" in line]


def test_grpc_exporter_gets_no_session(monkeypatch):
    def no_session(_signal: str) -> None:
        raise AssertionError("a gRPC exporter asked for a requests.Session")

    monkeypatch.setattr(_exporters, "_http_session", no_session)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:9")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc")
    opts = TelemetryOptions(service_name="a31")
    opts.resolve_from_env()
    exporter = build_otlp_exporter(opts.otlp_export("traces"))
    exporter.shutdown()


def test_session_refuses_redirects_without_provider():
    session = _exporters._http_session("traces")
    assert isinstance(session, requests.Session)
    assert session.get_redirect_target(object()) is None
    assert session.hooks["response"]


class _ProviderSession(requests.Session):
    pass


def test_credential_provider_session_also_refuses(monkeypatch):
    """A configured credential provider keeps its session; the helper adds the refusal to it."""
    from opentelemetry.exporter.otlp.proto.http import _common

    loaded: list[str] = []

    def fake_loader(var):  # noqa: ANN001, ANN202 - SDK private signature
        loaded.append(str(var))
        return _ProviderSession()

    monkeypatch.setattr(_common, "_load_session_from_envvar", fake_loader)
    monkeypatch.setenv("OTEL_PYTHON_EXPORTER_OTLP_HTTP_METRICS_CREDENTIAL_PROVIDER", "example")
    session = _exporters._http_session("metrics")
    assert isinstance(session, _ProviderSession)
    assert session.get_redirect_target(object()) is None
    assert loaded and "METRICS_CREDENTIAL_PROVIDER" in loaded[0]


def test_credential_provider_none_gets_a_refusing_session(monkeypatch):
    """A loader that returns no session: the SDK would build a plain one; the helper's refuses."""
    from opentelemetry.exporter.otlp.proto.http import _common

    monkeypatch.setattr(_common, "_load_session_from_envvar", lambda _var: None)
    monkeypatch.setenv("OTEL_PYTHON_EXPORTER_OTLP_HTTP_CREDENTIAL_PROVIDER", "example")
    session = _exporters._http_session("logs")
    assert type(session) is requests.Session
    assert session.get_redirect_target(object()) is None
    assert session.hooks["response"]


def test_credential_provider_loader_missing_warns_once(monkeypatch, caplog):
    """The private loader moved: one WARNING naming the variable only; the provider stays the SDK's."""
    from opentelemetry.exporter.otlp.proto.http import _common

    caplog.set_level(logging.DEBUG)
    monkeypatch.delattr(_common, "_load_session_from_envvar")
    monkeypatch.setenv("OTEL_PYTHON_EXPORTER_OTLP_HTTP_TRACES_CREDENTIAL_PROVIDER", "example-secret-provider")
    assert _exporters._http_session("traces") is None
    assert _exporters._http_session("traces") is None
    hits = [r for r in caplog.records if r.name == "staffops_otel_helper.export"]
    assert len(hits) == 1, [r.getMessage() for r in hits]
    assert hits[0].levelno == logging.WARNING
    assert "OTEL_PYTHON_EXPORTER_OTLP_HTTP_TRACES_CREDENTIAL_PROVIDER" in hits[0].getMessage()
    assert "example-secret-provider" not in caplog.text


def test_refused_error_is_not_retryable():
    """Neither exporter bound retries it: not a connection error, not a timeout."""
    err = _exporters._redirect_refused_error()
    assert issubclass(err, requests.exceptions.RequestException)
    # The SDK self-metric error.type is type(error).__qualname__ (1.42 and 1.45).
    assert err.__qualname__ == "OTLPRedirectRefused"
    assert err.__name__ == "OTLPRedirectRefused"
    for retryable in (requests.exceptions.ConnectionError, requests.exceptions.Timeout,
                      requests.exceptions.SSLError, requests.exceptions.ProxyError):
        assert not issubclass(err, retryable)
