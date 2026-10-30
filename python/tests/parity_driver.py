"""Parity fixture driver: runs ONE parity case in a fresh interpreter.

Invoked by tests/test_parity.py as ``python parity_driver.py <case.json> <out.json>``.
OTel globals are set-once per process, so every case gets its own process
(ADR-008). The case's env is already applied by the parent and ``{receiver}``
placeholders are already expanded.

The driver reports a snapshot (JSON) built only from helper-owned state: the
options ``setup_telemetry()`` returns, the providers it installs (public SDK
API), the helper's ``metrics_app()`` and what the fake receiver recorded on the
wire. Matching against ``expect`` happens in the parent.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import threading
import time
import urllib.request
import warnings

# Subprocess coverage: the parent passes the data file of the pytest-cov run;
# parallel-suffixed data files are combined by pytest-cov at session end.
_COV = None
if os.environ.get("PARITY_COVERAGE_DATA"):
    import coverage

    _COV = coverage.Coverage(data_file=os.environ["PARITY_COVERAGE_DATA"], data_suffix=True,
                             source=["staffops_otel_helper"], config_file=False)
    _COV.start()

# ── diagnostics capture ───────────────────────────────────────────────────
# Record every WARNING+ log record and every warnings.warn() without adding a
# handler to the root logger: the helper's handler decisions depend on what
# root has at setup time (P10), so the capture must not change them.
# Helper diagnostics (helper_log) are records from the helper's own loggers
# (`staffops_otel_helper` and children) at any level; SDK/library output is excluded.
_captured: list[str] = []
_helper: list[str] = []
_orig_call_handlers = logging.Logger.callHandlers


def _call_handlers(self: logging.Logger, record: logging.LogRecord) -> None:
    try:
        msg = record.getMessage()
    except Exception:  # noqa: BLE001 - malformed record args
        msg = str(record.msg)
    line = f"{record.name} {record.levelname} {msg}"
    if record.name == "staffops_otel_helper" or record.name.startswith("staffops_otel_helper."):
        _helper.append(line)
    if record.levelno >= logging.WARNING:
        _captured.append(line)
    _orig_call_handlers(self, record)


logging.Logger.callHandlers = _call_handlers  # type: ignore[method-assign]


def _show_warning(message, category, filename, lineno, file=None, line=None):  # noqa: ANN001
    _captured.append(f"{category.__name__}: {message}")


warnings.showwarning = _show_warning
warnings.simplefilter("always")

_TRACE_ID = 0x0AF7651916CD43DD8448EB211C80319C
_SPAN_ID = 0xB7AD6B7169203331
_SHUTDOWN_DEADLINE_S = 15.0  # generous: CI legs run concurrently and exporters retry under load
# Export cases may set timeouts above the SDK's 30 s batch default (e.g. 60 000 ms) and
# must see that export finish; joins return as soon as the work is done.
_EXPORT_SHUTDOWN_DEADLINE_S = 75.0


class UnsupportedCodeKey(Exception):
    """A fixture `code` key this adapter cannot map to the helper's API."""


def _options(code: dict):
    from staffops_otel_helper import DeploymentEnvironment, TelemetryOptions

    direct = {"service_name", "otel_endpoint", "otlp_protocol", "metric_exporters",
              "sample_ratio", "export_interval_ms", "resource_attributes",
              "traces_endpoint", "metrics_endpoint", "logs_endpoint",
              "insecure", "compression", "export_timeout_ms", "health_paths",
              "disabled_signals"}
    kwargs = {}
    unsupported = []
    for key, value in sorted(code.items()):
        if key in direct:
            kwargs[key] = value
        elif key == "environment":
            known = {e.value for e in DeploymentEnvironment}
            kwargs["environment"] = DeploymentEnvironment(value) if value in known else value
        elif key == "debug":
            kwargs["debug_level"] = bool(value)
        else:
            unsupported.append(key)
    if unsupported:
        raise UnsupportedCodeKey(", ".join(unsupported))
    return TelemetryOptions(**kwargs)


def _setup(case: dict) -> tuple[object | None, dict]:
    from staffops_otel_helper import setup_telemetry

    snap: dict = {}
    options = _options(case.get("code") or {})
    try:
        return setup_telemetry(options), snap
    except Exception as exc:  # noqa: BLE001 - the error IS the observation
        snap["error"] = f"{type(exc).__name__}: {exc}"
        return None, snap


def _flush_and_shutdown(deadline_s: float = _SHUTDOWN_DEADLINE_S) -> None:
    """Flush logs, shut down providers; never block longer than the deadline.

    Exporters pointed at a wrong port/TLS mode retry until their timeout; the
    snapshot only needs what reached the receiver, so stragglers are abandoned
    (the process then exits with os._exit, skipping SDK atexit hooks).
    """
    from opentelemetry import metrics, trace

    def run(fn):
        try:
            fn()
        except Exception:  # noqa: BLE001
            pass

    jobs = [h.flush for h in logging.getLogger().handlers]
    for provider in (trace.get_tracer_provider(), metrics.get_meter_provider()):
        if hasattr(provider, "shutdown"):
            jobs.append(provider.shutdown)
    threads = [threading.Thread(target=run, args=(j,), daemon=True) for j in jobs]
    for t in threads:
        t.start()
    end = time.monotonic() + deadline_s  # one deadline for all joins, not one per thread
    for t in threads:
        t.join(max(0.0, end - time.monotonic()))


def _resource_snapshot() -> dict:
    from opentelemetry import trace

    resource = getattr(trace.get_tracer_provider(), "resource", None)
    if resource is None:
        return {}
    return {k: str(v) for k, v in resource.attributes.items()}


def kind_resolve(case: dict) -> dict:
    from staffops_otel_helper import get_default_log_level

    opts, snap = _setup(case)
    if opts is not None:
        snap["resource"] = _resource_snapshot()
        # The effective list (parity/README.md): [] when metrics or the SDK are disabled.
        enabled = not opts.sdk_disabled and opts.is_signal_enabled("metrics")
        snap["metric_exporters"] = list(opts.resolved_metric_exporters()) if enabled else []
        snap["metric_export_interval_ms"] = opts.resolved.export_interval_ms
        level = logging.getLevelName(get_default_log_level(opts.environment, opts.resolved.debug_level))
        snap["log_level"] = "WARN" if level == "WARNING" else level
    _flush_and_shutdown()
    return snap


def _admin(method: str) -> bytes:
    admin = os.environ["FAKEOTLP_ADMIN"].rstrip("/")
    req = urllib.request.Request(f"{admin}/requests", method=method)
    with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310 - test receiver
        return resp.read()


def _signal_of(proto: str, path: str) -> str:
    p = path.lower()
    if proto == "grpc":
        for token, sig in ((".trace.", "traces"), (".metrics.", "metrics"), (".logs.", "logs")):
            if token in p:
                return sig
        return "other"
    for token, sig in (("trace", "traces"), ("metric", "metrics"), ("log", "logs")):
        if token in p:
            return sig
    return "other"


def _grpc_timeout_ms(value: str) -> float | None:
    """grpc-timeout header ("<digits><H|M|S|m|u|n>") in milliseconds."""
    units = {"H": 3.6e6, "M": 6e4, "S": 1e3, "m": 1.0, "u": 1e-3, "n": 1e-6}
    if len(value) < 2 or value[-1] not in units or not value[:-1].isdigit():
        return None
    return int(value[:-1]) * units[value[-1]]


def kind_export(case: dict) -> dict:
    from opentelemetry import metrics, trace

    _admin("DELETE")
    opts, snap = _setup(case)
    if opts is not None:
        trace.get_tracer("parity").start_span("parity-span").end()
        metrics.get_meter("parity").create_counter("parity.requests").add(1)
        logging.getLogger("parity").warning("parity-log")
    _flush_and_shutdown(_EXPORT_SHUTDOWN_DEADLINE_S)
    # Only records sent to this case's receiver address count: unrelated tests
    # in the same network namespace may export to localhost:4317 meanwhile.
    dest = set(filter(None, os.environ.get("FAKEOTLP_DEST_IPS", "").split(",")))
    received: dict = {"traces": [], "metrics": [], "logs": [], "other": []}
    for rec in json.loads(_admin("GET")):
        if rec.get("addr") not in dest:
            continue
        received[_signal_of(rec["proto"], rec["path"])].append({
            "proto": rec["proto"], "tls": rec["tls"], "port": int(rec["port"]), "path": rec["path"],
            "raw_path": rec.get("raw_path", ""), "encoding": rec.get("encoding", ""), "headers": rec.get("headers") or {},
            **({"timeout_ms": ms} if (ms := _grpc_timeout_ms(rec.get("timeout", ""))) is not None else {}),
        })
    snap["received"] = received
    return snap


def _remote_parent(sampled: bool, debug_tracestate: bool = False):
    from opentelemetry import trace
    from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags, TraceState

    state = TraceState([("debug", "true")]) if debug_tracestate else TraceState()
    ctx = SpanContext(_TRACE_ID, _SPAN_ID, is_remote=True,
                      trace_flags=TraceFlags(TraceFlags.SAMPLED if sampled else TraceFlags.DEFAULT),
                      trace_state=state)
    return trace.set_span_in_context(NonRecordingSpan(ctx))


def kind_sampler(case: dict) -> dict:
    from opentelemetry import trace
    from opentelemetry.context import Context

    _, snap = _setup(case)
    tracer = trace.get_tracer("parity")
    scenario = (case.get("input") or {}).get("scenario")
    if scenario == "root":
        span = tracer.start_span("target", context=Context())
    elif scenario == "remote_sampled":
        span = tracer.start_span("target", context=_remote_parent(True))
    elif scenario == "remote_unsampled":
        span = tracer.start_span("target", context=_remote_parent(False))
    elif scenario == "remote_sampled_debug_tracestate":
        span = tracer.start_span("target", context=_remote_parent(True, True))
    elif scenario == "remote_sampled_debug_baggage":
        from opentelemetry import baggage

        span = tracer.start_span("target", context=baggage.set_baggage("debug", "true", _remote_parent(True)))
    elif scenario in ("local_child_of_remote_unsampled", "local_child_of_root"):
        parent_ctx = _remote_parent(False) if scenario == "local_child_of_remote_unsampled" else Context()
        parent = tracer.start_span("parent", context=parent_ctx)
        span = tracer.start_span("target", context=trace.set_span_in_context(parent))
    else:
        return {"adapter_error": f"unknown scenario {scenario!r}"}
    snap["sampled"] = bool(span.get_span_context().trace_flags.sampled)
    snap["attributes"] = {k: str(v) for k, v in (getattr(span, "attributes", None) or {}).items()}
    span.end()
    _flush_and_shutdown()
    return snap


def kind_propagation(case: dict) -> dict:
    # The helper is imported (by _setup) before opentelemetry.propagate, as an app
    # must for an unsupported OTEL_PROPAGATORS entry to be skipped instead of raising.
    _, snap = _setup(case)
    from opentelemetry import baggage, trace
    from opentelemetry import context as otel_context
    from opentelemetry.propagate import inject

    token = otel_context.attach(baggage.set_baggage("parity", "1"))
    try:
        with trace.get_tracer("parity").start_as_current_span("parity-span"):
            carrier: dict = {}
            inject(carrier)
    finally:
        otel_context.detach(token)
    snap["headers"] = sorted(k.lower() for k in carrier)
    _flush_and_shutdown()
    return snap


def _record_spans():
    from opentelemetry import trace
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    trace.get_tracer_provider().add_span_processor(SimpleSpanProcessor(exporter))
    return exporter


def _server_spans(exporter) -> int:
    from opentelemetry.trace import SpanKind

    return sum(1 for s in exporter.get_finished_spans() if s.kind == SpanKind.SERVER)


def _scrape_response():
    from starlette.testclient import TestClient

    from staffops_otel_helper import metrics_app

    return TestClient(metrics_app()).get("/")


def _scrape() -> str:
    return _scrape_response().text


def _scrape_count(name_regex: str) -> float:
    import re

    pattern = re.compile(name_regex)
    total = 0.0
    for line in _scrape().splitlines():
        if line.startswith("#") or not pattern.match(line):
            continue
        try:
            total += float(line.split()[-1])
        except ValueError:
            pass
    return total


def kind_http_server(case: dict) -> dict:
    _, snap = _setup(case)
    if "error" in snap:
        return snap
    exporter = _record_spans()
    # Imported after setup on purpose: FastAPIInstrumentor patches the class.
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from opentelemetry import trace

    child_span = bool(case["input"].get("child_span"))
    headers = {"traceparent": case["input"]["traceparent"]} if case["input"].get("traceparent") else {}
    app = FastAPI()

    @app.get("/{path:path}")
    def _any(path: str) -> str:
        if child_span:
            trace.get_tracer("parity").start_span("handler-child").end()
        return "ok"

    client = TestClient(app)
    for path in case["input"]["requests"]:
        client.get(path, headers=headers)
    snap["server_spans"] = _server_spans(exporter)
    snap["handler_child_spans"] = sum(1 for s in exporter.get_finished_spans() if s.name == "handler-child")
    snap["requests_counted"] = _scrape_count(r"^http_server(_request)?_duration[a-z_]*_count[{ ]")
    _flush_and_shutdown()
    return snap


async def _grpc_call(method: str) -> None:
    import grpc

    serving = b"\x08\x01"  # HealthCheckResponse{status: SERVING}

    async def unary(request, context):  # noqa: ANN001
        return serving

    async def watch(request, context):  # noqa: ANN001
        yield serving

    server = grpc.aio.server()
    server.add_generic_rpc_handlers((
        grpc.method_handlers_generic_handler("grpc.health.v1.Health", {
            "Check": grpc.unary_unary_rpc_method_handler(unary),
            "Watch": grpc.unary_stream_rpc_method_handler(watch),
        }),
        grpc.method_handlers_generic_handler("parity.v1.Echo", {
            "Ping": grpc.unary_unary_rpc_method_handler(unary),
        }),
    ))
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    try:
        async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
            if method.endswith("/Watch"):
                call = channel.unary_stream(method)(b"")
                async for _ in call:
                    break
                call.cancel()
            else:
                await channel.unary_unary(method)(b"")
    finally:
        await server.stop(0.5)


def kind_grpc_server(case: dict) -> dict:
    _, snap = _setup(case)
    if "error" in snap:
        return snap
    exporter = _record_spans()
    try:
        asyncio.run(_grpc_call(case["input"]["method"]))
        snap["call_ok"] = True
    except Exception as exc:  # noqa: BLE001 - a failed call is the observation
        snap["call_ok"] = False
        _captured.append(f"grpc call failed: {exc!r}")
    snap["server_spans"] = _server_spans(exporter)
    snap["rpcs_counted"] = _scrape_count(r"^rpc_server(_call)?_duration[a-z_]*_count[{ ]")
    _flush_and_shutdown()
    return snap


def kind_logging(case: dict) -> dict:
    # "console" (stdout + stderr) is filled in by the parent from the process output.
    _, snap = _setup(case)
    logging.getLogger("parity").warning("parity-console-record")
    _flush_and_shutdown()
    return snap


def kind_metrics_scrape(case: dict) -> dict:
    from opentelemetry import metrics

    _, snap = _setup(case)
    if "error" in snap:
        return snap
    metrics.get_meter("parity").create_counter("parity.requests").add(1)
    response = _scrape_response()
    snap["scrape"], snap["scrape_status"] = response.text, response.status_code
    _flush_and_shutdown()
    return snap


def kind_fork(case: dict) -> dict:
    """Pre-fork server model (gunicorn post_fork): fork twice, set up in each child."""
    import tempfile

    import staffops_otel_helper  # noqa: F401 - imported before fork, as in a preloaded app

    workers = []
    tmp = tempfile.mkdtemp()
    for i in range(2):
        out = os.path.join(tmp, f"worker-{i}.json")
        pid = os.fork()
        if pid == 0:  # child
            try:
                _, snap = _setup(case)
                snap["instance_id"] = _resource_snapshot().get("service.instance.id")
                snap["diag"] = list(_captured)
                snap["helper"] = list(_helper)
                with open(out, "w", encoding="utf-8") as fh:
                    json.dump(snap, fh, default=str)
            finally:
                os._exit(0)
        os.waitpid(pid, 0)
        workers.append(out)
    snap: dict = {}
    ids = []
    for out in workers:
        with open(out, encoding="utf-8") as fh:
            w = json.load(fh)
        ids.append(w.get("instance_id"))
        _captured.extend(w.get("diag", []))
        _helper.extend(w.get("helper", []))
        if "error" in w:
            snap["error"] = w["error"]
    snap["instance_ids"] = ids
    snap["instance_ids_distinct"] = None not in ids and len(set(ids)) == len(ids)
    return snap


KINDS = {
    "resolve": kind_resolve,
    "export": kind_export,
    "sampler": kind_sampler,
    "propagation": kind_propagation,
    "http_server": kind_http_server,
    "grpc_server": kind_grpc_server,
    "logging": kind_logging,
    "metrics_scrape": kind_metrics_scrape,
    "fork": kind_fork,
}


def main() -> None:
    with open(sys.argv[1], encoding="utf-8") as fh:
        case = json.load(fh)
    kind = KINDS.get(case["kind"])
    if kind is None:
        snap = {"adapter_error": f"unknown kind {case['kind']!r}"}
    else:
        try:
            snap = kind(case)
        except UnsupportedCodeKey as exc:
            snap = {"adapter_error": f"unsupported code key(s) {exc}"}
    if "adapter_error" not in snap:
        snap["diagnostics"] = "\n".join(_captured)
        snap["helper_log"] = "\n".join(_helper)
    with open(sys.argv[2], "w", encoding="utf-8") as fh:
        json.dump(snap, fh, default=str)
    if _COV is not None:
        _COV.stop()
        _COV.save()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)  # skip SDK atexit shutdown hooks (they would retry dead exporters)


if __name__ == "__main__":
    main()
