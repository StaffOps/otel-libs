"""Health-check exclusion, instrument_fastapi, sync gRPC (task A7-py: R7, P11, P13, design D5)."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from concurrent import futures

import pytest

from opentelemetry import trace
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.sampling import ALWAYS_ON, Decision, ParentBased
from opentelemetry.trace import NonRecordingSpan, SpanContext, SpanKind, TraceFlags, TraceState

from staffops_otel_helper import setup_telemetry
from staffops_otel_helper.config import TelemetryOptions
from staffops_otel_helper.tracing import DEFAULT_HEALTH_PATHS, HealthCheckSampler

_TRACE_ID = 0x0AF7651916CD43DD8448EB211C80319C


def _parent(remote: bool, sampled: bool = True):
    ctx = SpanContext(_TRACE_ID, 0xB7AD6B7169203331, is_remote=remote,
                      trace_flags=TraceFlags(TraceFlags.SAMPLED if sampled else 0),
                      trace_state=TraceState([("vendor", "v1")]))
    return trace.set_span_in_context(NonRecordingSpan(ctx))


def _decide(sampler, kind=SpanKind.SERVER, attrs=None, name="GET", parent=None):
    return sampler.should_sample(parent, _TRACE_ID, name, kind=kind, attributes=attrs)


class TestHealthCheckSampler:
    sampler = HealthCheckSampler(ParentBased(ALWAYS_ON))

    @pytest.mark.parametrize("attrs", [
        {"url.path": "/health"}, {"url.path": "/Health/"}, {"url.path": "/READYZ"},
        {"http.target": "/healthz?probe=1"}, {"http.target": "/live#x"}, {"url.path": "/metrics"},
        {"url.path": "/ping//"},
    ])
    def test_health_server_root_dropped(self, attrs):
        assert _decide(self.sampler, attrs=attrs).decision == Decision.DROP

    @pytest.mark.parametrize("attrs", [
        {"url.path": "/users/ping"}, {"url.path": "/healthcare"}, {"url.path": "/health/live"},
        {"url.path": "/"}, {}, None, {"url.path": 7},
    ])
    def test_other_paths_sampled(self, attrs):
        assert _decide(self.sampler, attrs=attrs).decision == Decision.RECORD_AND_SAMPLE

    def test_url_path_wins_over_http_target(self):
        attrs = {"url.path": "/orders", "http.target": "/health"}
        assert _decide(self.sampler, attrs=attrs).decision == Decision.RECORD_AND_SAMPLE

    def test_non_server_kind_defers(self):
        assert _decide(self.sampler, kind=SpanKind.CLIENT, attrs={"url.path": "/health"}).decision \
            == Decision.RECORD_AND_SAMPLE

    def test_remote_parent_dropped_and_tracestate_kept(self):
        result = _decide(self.sampler, attrs={"url.path": "/health"}, parent=_parent(remote=True))
        assert result.decision == Decision.DROP
        assert result.trace_state.get("vendor") == "v1"

    def test_local_parent_defers_to_inner(self):
        result = _decide(self.sampler, attrs={"url.path": "/health"}, parent=_parent(remote=False))
        assert result.decision == Decision.RECORD_AND_SAMPLE

    @pytest.mark.parametrize("name", ["/grpc.health.v1.Health/Check", "grpc.health.v1.Health/Watch"])
    def test_grpc_health_names_dropped(self, name):
        assert _decide(self.sampler, name=name).decision == Decision.DROP

    def test_grpc_other_method_sampled(self):
        assert _decide(self.sampler, name="/parity.v1.Echo/Ping").decision == Decision.RECORD_AND_SAMPLE

    def test_custom_and_empty_lists(self):
        custom = HealthCheckSampler(ALWAYS_ON, ["/status"])
        assert _decide(custom, attrs={"url.path": "/STATUS/"}).decision == Decision.DROP
        assert _decide(custom, attrs={"url.path": "/health"}).decision == Decision.RECORD_AND_SAMPLE
        none = HealthCheckSampler(ALWAYS_ON, [])
        assert _decide(none, attrs={"url.path": "/health"}).decision == Decision.RECORD_AND_SAMPLE
        assert _decide(none, name="/grpc.health.v1.Health/Check").decision == Decision.DROP  # always

    def test_description_and_inner(self):
        assert self.sampler.get_description().startswith("HealthCheckExcluding{ParentBased")
        assert isinstance(self.sampler.inner, ParentBased)

    def test_default_list_matches_go(self):
        assert DEFAULT_HEALTH_PATHS == ("/health", "/healthz", "/ready", "/readyz", "/live", "/livez",
                                        "/ping", "/metrics")


class TestHealthPathsOption:
    @pytest.mark.parametrize("bad", ["health", "/h?x=1", "/h#f", 3, "/a,/b", "/sa\u00fade"])
    def test_invalid_paths_fail_fast(self, bad):
        with pytest.raises(ValueError, match="health_paths"):
            TelemetryOptions(service_name="s", health_paths=[bad]).validate()

    def test_not_a_list_fails(self):
        with pytest.raises(ValueError, match="health_paths"):
            TelemetryOptions(service_name="s", health_paths="/health").validate()

    def test_option_reaches_the_installed_sampler(self):
        setup_telemetry(TelemetryOptions(service_name="s", health_paths=["/status"], metric_exporters=["none"]))
        sampler = trace.get_tracer_provider().sampler
        assert _decide(sampler, attrs={"url.path": "/status"}).decision == Decision.DROP
        assert _decide(sampler, attrs={"url.path": "/health"}).decision == Decision.RECORD_AND_SAMPLE


_PRELUDE = """
import os
if os.environ.get("PARITY_COVERAGE_DATA"):
    import coverage
    _cov = coverage.Coverage(data_file=os.environ["PARITY_COVERAGE_DATA"], data_suffix=True,
                             source=["staffops_otel_helper"], config_file=False)
    _cov.start()
    import atexit
    atexit.register(lambda: (_cov.stop(), _cov.save()))
import re
from opentelemetry import trace
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind

def record():
    exp = InMemorySpanExporter()
    trace.get_tracer_provider().add_span_processor(SimpleSpanProcessor(exp))
    return exp

def server_paths(exp):
    # Contrib spans only: recent FastAPI releases also emit their own (scope "fastapi").
    return sorted(s.attributes.get("url.path") for s in exp.get_finished_spans()
                  if s.kind == SpanKind.SERVER and s.instrumentation_scope.name.startswith("opentelemetry."))

def counted():
    from starlette.testclient import TestClient
    from staffops_otel_helper import metrics_app
    total = 0.0
    for line in TestClient(metrics_app()).get("/").text.splitlines():
        if re.match(r"^http_server_request_duration[a-z_]*_count[{ ]", line):
            total += float(line.split()[-1])
    return total
"""


def _run(body: str, **env: str) -> str:
    full_env = {k: v for k, v in os.environ.items()
                if not (k.startswith("OTEL_") or k in ("SERVICE_NAME", "ENVIRONMENT"))}
    full_env.update({"OTEL_SERVICE_NAME": "a7", "OTEL_HELPER_METRICS_PORT": "0"})
    full_env.update(env)
    proc = subprocess.run([sys.executable, "-c", _PRELUDE + textwrap.dedent(body)], env=full_env,
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


class TestFastAPI:
    def test_user_excluded_urls_keep_upstream_meaning(self):
        # OTEL_PYTHON_FASTAPI_EXCLUDED_URLS drops span AND metric (upstream);
        # the helper's health exclusion drops only the span.
        out = _run("""
            from staffops_otel_helper import setup_telemetry
            setup_telemetry()
            exp = record()
            from fastapi import FastAPI
            from fastapi.testclient import TestClient
            app = FastAPI()
            @app.get("/{p:path}")
            def any_(p: str):
                return "ok"
            c = TestClient(app)
            for path in ("/orders", "/health", "/items"):
                c.get(path)
            print("RESULT", server_paths(exp), counted(), flush=True)
        """, OTEL_PYTHON_FASTAPI_EXCLUDED_URLS="/orders")
        assert "RESULT ['/items'] 2.0" in out

    def test_instrument_fastapi_for_app_imported_before_setup(self):
        out = _run("""
            import logging
            from fastapi import FastAPI  # imported BEFORE setup (P11)
            from fastapi.testclient import TestClient
            from staffops_otel_helper import instrument_fastapi, setup_telemetry
            app = FastAPI()
            @app.get("/{p:path}")
            def any_(p: str):
                return "ok"
            setup_telemetry()
            exp = record()
            warnings = []
            logging.getLogger("opentelemetry").addHandler(type("H", (logging.Handler,), {
                "emit": lambda self, r: warnings.append(r.getMessage())})())
            assert instrument_fastapi(app) is app
            instrument_fastapi(app)  # idempotent, no "already instrumented" warning
            c = TestClient(app)
            c.get("/orders"); c.get("/health")
            print("RESULT", server_paths(exp), counted(), warnings, flush=True)
        """)
        assert "RESULT ['/orders'] 2.0 []" in out


def _sync_server():
    import grpc

    def unary(request, context):
        return b"\x08\x01"

    server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
    server.add_generic_rpc_handlers((
        grpc.method_handlers_generic_handler("grpc.health.v1.Health", {
            "Check": grpc.unary_unary_rpc_method_handler(unary)}),
        grpc.method_handlers_generic_handler("parity.v1.Echo", {
            "Ping": grpc.unary_unary_rpc_method_handler(unary)}),
    ))
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    return server, port


class TestSyncGrpc:
    """P13: sync grpc.server()/channels are instrumented like grpc.aio."""

    def _exercise(self, *methods):
        import grpc

        setup_telemetry(TelemetryOptions(service_name="s", metric_exporters=["none"]))
        exporter = InMemorySpanExporter()
        trace.get_tracer_provider().add_span_processor(SimpleSpanProcessor(exporter))
        server, port = _sync_server()
        try:
            with grpc.insecure_channel(f"127.0.0.1:{port}") as channel:
                for method in methods:
                    channel.unary_unary(method)(b"", timeout=5)
        finally:
            server.stop(0.5)
        return exporter.get_finished_spans()

    def test_spans_and_propagation(self):
        spans = self._exercise("/parity.v1.Echo/Ping")
        client = [s for s in spans if s.kind == SpanKind.CLIENT]
        server = [s for s in spans if s.kind == SpanKind.SERVER]
        assert len(client) == 1 and len(server) == 1
        assert server[0].context.trace_id == client[0].context.trace_id
        assert server[0].parent.span_id == client[0].context.span_id

    def test_health_check_not_traced_on_server(self):
        spans = self._exercise("/grpc.health.v1.Health/Check")
        assert [s for s in spans if s.kind == SpanKind.SERVER] == []

    def test_excluded_services_env_keeps_meaning_and_health_still_dropped(self):
        os.environ["OTEL_PYTHON_GRPC_EXCLUDED_SERVICES"] = "parity.v1.Echo"
        spans = self._exercise("/parity.v1.Echo/Ping", "/grpc.health.v1.Health/Check")
        assert [s for s in spans if s.kind == SpanKind.SERVER] == []


class TestClientExclusion:
    """A5/A7 rulings: client health calls not traced (exact path), env never mutated."""

    @pytest.mark.parametrize("url,excluded", [
        ("http://svc:8080/health", True), ("https://svc/HEALTH/", True), ("http://svc/readyz?x=1", True),
        ("http://svc/ping#f", True), ("http://svc/users/ping", False), ("http://svc/healthcare", False),
        ("http://svc/health/live", False), ("http://svc/api?next=/health", False),
    ])
    def test_pattern_is_exact(self, url, excluded):
        import re

        from staffops_otel_helper.instrumentation import health_url_pattern

        pattern = health_url_pattern(DEFAULT_HEALTH_PATHS)
        assert "," not in pattern
        assert bool(re.search(pattern, url)) is excluded
        assert health_url_pattern([]) == ""

    def test_env_not_mutated_and_user_value_merged(self):
        os.environ["OTEL_PYTHON_EXCLUDED_URLS"] = "/mine"
        before = dict(os.environ)
        setup_telemetry(TelemetryOptions(service_name="s", metric_exporters=["none"]))
        after = dict(os.environ)
        after.pop("OTEL_SEMCONV_STABILITY_OPT_IN", None)  # design D12 default, set on purpose
        assert after == before
        from staffops_otel_helper.instrumentation import _client_excluded_urls

        assert _client_excluded_urls("OTEL_PYTHON_HTTPX_EXCLUDED_URLS", "H") == "/mine,H"
        os.environ["OTEL_PYTHON_HTTPX_EXCLUDED_URLS"] = ""  # library var set (even empty) wins, as upstream
        assert _client_excluded_urls("OTEL_PYTHON_HTTPX_EXCLUDED_URLS", "H") == "H"

    def test_httpx_health_calls_not_traced(self):
        out = _run("""
            from staffops_otel_helper import setup_telemetry
            setup_telemetry()
            exp = record()
            import http.server, threading, httpx

            class H(http.server.BaseHTTPRequestHandler):
                def do_GET(self):
                    self.send_response(200); self.send_header("Content-Length", "0"); self.end_headers()
                def log_message(self, *a):
                    pass

            srv = http.server.HTTPServer(("127.0.0.1", 0), H)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            base = f"http://127.0.0.1:{srv.server_port}"
            with httpx.Client() as c:
                for u in ("/Health/", "/readyz?x=1", "/users/ping"):
                    c.get(base + u)
            paths = sorted(sp.attributes.get("url.full").split(base)[1]
                           for sp in exp.get_finished_spans() if sp.kind == SpanKind.CLIENT)
            print("RESULT", paths, flush=True)
            srv.shutdown()
        """)
        assert "RESULT ['/users/ping']" in out

    def test_requests_gets_the_pattern_as_kwarg(self, monkeypatch):
        pytest.importorskip("requests")
        from opentelemetry.instrumentation.requests import RequestsInstrumentor

        seen = {}
        monkeypatch.setattr(RequestsInstrumentor, "_instrument", lambda self, **kw: seen.update(kw))
        os.environ["OTEL_PYTHON_REQUESTS_EXCLUDED_URLS"] = "/mine"
        setup_telemetry(TelemetryOptions(service_name="s", metric_exporters=["none"], health_paths=["/status"]))
        assert seen["excluded_urls"].startswith("/mine,^") and "/status" in seen["excluded_urls"]


class TestGrpcClientAndExcludedServices:
    def _aio(self, *methods):
        import asyncio

        import grpc

        async def run():
            serving = b"\x08\x01"

            async def unary(request, context):
                return serving

            async def watch(request, context):
                # context.write() style (grpcio-health-checking's aio servicer): the upstream
                # aio interceptor only supports async-generator handlers, so this works only
                # because health RPCs are never intercepted.
                await context.write(serving)

            server = grpc.aio.server()
            server.add_generic_rpc_handlers((
                grpc.method_handlers_generic_handler("grpc.health.v1.Health", {
                    "Check": grpc.unary_unary_rpc_method_handler(unary),
                    "Watch": grpc.unary_stream_rpc_method_handler(watch)}),
                grpc.method_handlers_generic_handler("parity.v1.Echo", {
                    "Ping": grpc.unary_unary_rpc_method_handler(unary)}),
            ))
            port = server.add_insecure_port("127.0.0.1:0")
            await server.start()
            try:
                async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
                    for method in methods:
                        if method.endswith("/Watch"):
                            call = channel.unary_stream(method)(b"")
                            async for _ in call:
                                break
                            call.cancel()
                        else:
                            await channel.unary_unary(method)(b"", timeout=5)
            finally:
                await server.stop(0.5)

        setup_telemetry(TelemetryOptions(service_name="s", metric_exporters=["none"]))
        exporter = InMemorySpanExporter()
        trace.get_tracer_provider().add_span_processor(SimpleSpanProcessor(exporter))
        asyncio.run(run())
        return exporter.get_finished_spans()

    def test_aio_client_health_not_traced(self):
        spans = self._aio("/grpc.health.v1.Health/Check", "/parity.v1.Echo/Ping")
        assert sorted((s.kind.name, s.name.lstrip("/")) for s in spans) == [
            ("CLIENT", "parity.v1.Echo/Ping"), ("SERVER", "parity.v1.Echo/Ping")]

    def test_excluded_services_var_with_aio_watch(self):
        os.environ["OTEL_PYTHON_GRPC_EXCLUDED_SERVICES"] = "parity.v1.Echo"
        spans = self._aio("/grpc.health.v1.Health/Watch", "/parity.v1.Echo/Ping")
        assert spans == () or list(spans) == []
        assert os.environ["OTEL_PYTHON_GRPC_EXCLUDED_SERVICES"] == "parity.v1.Echo"  # restored

    def test_sync_client_health_not_traced(self):
        spans = TestSyncGrpc()._exercise("/grpc.health.v1.Health/Check")
        assert list(spans) == []
