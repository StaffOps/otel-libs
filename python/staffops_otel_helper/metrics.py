"""Metrics setup — equivalent to MetricsSetup.cs."""

import dataclasses
import logging
import re
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from opentelemetry import metrics
from opentelemetry.exporter.prometheus import PrometheusMetricReader
from opentelemetry.sdk.metrics import MeterProvider, TraceBasedExemplarFilter
from opentelemetry.sdk.metrics.export import Gauge, MetricsData, PeriodicExportingMetricReader
from opentelemetry.sdk.metrics.view import View, DropAggregation
from opentelemetry.sdk.resources import Resource

from staffops_otel_helper._diag import warn_once_per_process
from staffops_otel_helper._exporters import build_otlp_exporter
from prometheus_client import CollectorRegistry

from staffops_otel_helper.config import ENV_PROMETHEUS_HOST, ENV_PROMETHEUS_PORT, TelemetryOptions, _truncate

_logger = logging.getLogger(__name__)

# Dedicated registry for the helper's /metrics (P14): only OTel-produced series.
# prometheus_client's global REGISTRY also carries process_*/python_gc_*/
# python_info collectors and whatever the application registered itself; none
# of that belongs on the helper's endpoint. Module-level so metrics_app() can be
# mounted before setup_telemetry() runs; each PrometheusMetricReader registers
# its collector here and unregisters it on shutdown.
_REGISTRY = CollectorRegistry()

# Helper readers whose MeterProvider is installed (added at install, removed at
# shutdown) and what metrics_app() answers without one (design "Mounted
# /metrics handler status"): 503 before setup, after a failed setup and after
# shutdown; 200 with an empty body when setup deliberately left Prometheus off
# (OTEL_SDK_DISABLED, metrics disabled, OTEL_METRICS_EXPORTER without prometheus).
_active_readers: set = set()
_active_lock = threading.Lock()
_deliberately_inactive = False


class _HelperPrometheusReader(PrometheusMetricReader):
    """PrometheusMetricReader on the dedicated registry, tracked in _active_readers."""

    def __init__(self) -> None:
        super().__init__(registry=_REGISTRY)

    def _receive_metrics(self, metrics_data: MetricsData, timeout_millis: float = 10_000, **kwargs) -> None:
        # The MetricReader hook every reader implements; the data is adapted, then handed over.
        super()._receive_metrics(_ratio_units(metrics_data), timeout_millis=timeout_millis, **kwargs)

    def shutdown(self, timeout_millis: float = 30_000, **kwargs) -> None:
        with _active_lock:
            if getattr(self, "_helper_shut_down", False):
                return  # already shut down: the collector is unregistered
            self._helper_shut_down = True
            _active_readers.discard(self)
        super().shutdown(timeout_millis=timeout_millis, **kwargs)


def activate(reader: "_HelperPrometheusReader | None", *, deliberately_inactive: bool = False) -> None:
    """Record the outcome of an installed setup for metrics_app()."""
    global _deliberately_inactive
    with _active_lock:
        _deliberately_inactive = reader is None and deliberately_inactive
        if reader is not None:
            _active_readers.add(reader)


def deactivate() -> None:
    """After shutdown or a failed setup: metrics_app() answers 503 until the next setup.

    Called before the providers are flushed so a scrape during shutdown already
    sees 503; the readers then shut down (and unregister) as usual.
    """
    global _deliberately_inactive
    with _active_lock:
        _deliberately_inactive = False
        _active_readers.clear()


_UNIT_ANNOTATION = re.compile(r"\{[^}]*\}")


_LABEL_RE = re.compile(r"[^a-zA-Z0-9]+")


def _label_name(key: str) -> str:
    """The upstream exporter's label sanitization (``sanitize_attribute``)."""
    if key and key[0].isdigit():
        key = "_" + key[1:]
    return _LABEL_RE.sub("_", key)


def _label_value(value) -> str:
    """A merged value as Go and Node render it: strings as is, ``true``/``false``, compact JSON otherwise."""
    import json

    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, tuple):
        value = list(value)
    return json.dumps(value, default=str, separators=(",", ":"))


def _merged(attributes):
    """Keys colliding once sanitized become one label, the sorted values joined with ``;``.

    Design D1 row "Colliding label names" (as Go, ``slices.Sort(vals)``): the exporter would
    otherwise keep the last value. Attributes without a collision are returned unchanged.
    """
    if not attributes:
        return attributes
    groups: dict[str, list[str]] = {}
    for key in sorted(attributes):
        groups.setdefault(_label_name(key), []).append(key)
    if all(len(keys) == 1 for keys in groups.values()):
        return attributes
    return {name: attributes[keys[0]] if len(keys) == 1 else ";".join(sorted(_label_value(attributes[k]) for k in keys))
            for name, keys in groups.items()}


def _ratio_units(metrics_data: MetricsData | None) -> MetricsData | None:
    """Gauges in unit ``1`` get the ``_ratio`` suffix (design D1 row "Unit 1", A21).

    The OpenTelemetry Prometheus compatibility spec suffixes a Gauge in unit ``1`` with
    ``_ratio`` and no other instrument; the upstream exporter maps ``1`` to no suffix for
    every type (``_mapping.py`` ``"1": ""``). Rewriting only those Gauges' unit to ``ratio``
    makes the exporter write the spec name; nothing else changes.
    """
    if metrics_data is None:
        return None

    # Objects without a change are returned as they are: nothing is copied on a plain collect.
    def adapt(metric):
        data = metric.data
        merged = [_merged(p.attributes) for p in data.data_points]
        if any(m is not p.attributes for m, p in zip(merged, data.data_points)):
            points = [p if m is p.attributes else dataclasses.replace(p, attributes=m)
                      for m, p in zip(merged, data.data_points)]
            metric = dataclasses.replace(metric, data=dataclasses.replace(data, data_points=points))
        if isinstance(data, Gauge) and _UNIT_ANNOTATION.sub("", metric.unit or "") == "1":
            return dataclasses.replace(metric, unit="ratio")
        return metric

    def scope(sm):
        metrics_ = [adapt(m) for m in sm.metrics]
        return sm if all(a is b for a, b in zip(metrics_, sm.metrics)) else dataclasses.replace(sm, metrics=metrics_)

    def resource(res):
        attrs = dict(res.attributes)
        merged = _merged(attrs)
        return res if merged is attrs else Resource(merged, res.schema_url)

    def resource_metrics(rm):
        res = resource(rm.resource)
        scopes = [scope(sm) for sm in rm.scope_metrics]
        if res is rm.resource and all(a is b for a, b in zip(scopes, rm.scope_metrics)):
            return rm
        return dataclasses.replace(rm, resource=res, scope_metrics=scopes)

    adapted = [resource_metrics(rm) for rm in metrics_data.resource_metrics]
    if all(a is b for a, b in zip(adapted, metrics_data.resource_metrics)):
        return metrics_data
    return dataclasses.replace(metrics_data, resource_metrics=adapted)


def _view_pattern(pattern: str) -> str | None:
    """The SDK View pattern equal to a contract glob (design D1 row "Disabled-metric patterns", A21).

    Contract: case-sensitive, ``*`` any run of characters, ``?`` one character, every other
    character literal. The View matches with ``fnmatchcase``, where ``[`` opens a class
    (escaped as ``[[]``), against the name the SDK stores lower-cased (``instrument.py``
    ``self.name = name.lower()``): a pattern with an upper-case letter can match no name
    case-sensitively, so it gets no View (the SDK lower-cases the pattern before matching).
    """
    if pattern != pattern.lower():
        # Patterns are not secrets: named, truncated, as Go's warnEnv (design D1 row "Disabled-metric patterns").
        warn_once_per_process(
            _logger, f"disabled-metrics-upper-case:{pattern}",
            "[OtelHelper] OTEL_HELPER_DISABLED_METRICS / disabled_metrics pattern %r has an upper-case "
            "letter; the Python SDK lower-cases instrument names, so it matches no instrument (use lower case).",
            _truncate(pattern))
        return None
    return pattern.replace("[", "[[]")


#: HTTP metrics semconv v1.43 makes opt-in (``docs/http/http-metrics.md``): dropped
#: unless ``http_opt_in_metrics`` / ``OTEL_HELPER_HTTP_OPT_IN_METRICS`` is on (contract
#: D1 "HTTP opt-in metrics", ruled 2026-11-06, A30), both HTTP semconv generations
#: (contrib ASGI/requests/httpx write the ``*.size`` names in the old one). Only the
#: two durations stay by default, as in Go and Node.js. Scoped to the contrib
#: instrumentations the helper installs (2026-11-08, a View matches one scope name
#: exactly): an application's own instrument of the same name is kept.
HTTP_OPT_IN_METRICS_SCOPES = (
    "opentelemetry.instrumentation.asgi", "opentelemetry.instrumentation.fastapi",
    "opentelemetry.instrumentation.requests", "opentelemetry.instrumentation.httpx",
)
HTTP_OPT_IN_METRICS = (
    "http.server.active_requests", "http.client.active_requests",
    "http.server.request.body.size", "http.server.response.body.size",
    "http.client.request.body.size", "http.client.response.body.size",
    "http.server.request.size", "http.server.response.size",
    "http.client.request.size", "http.client.response.size",
    "http.client.open_connections", "http.client.connection.duration",
)


def _build_drop_views(patterns: list[str]) -> list[View]:
    """Views that drop the instruments matching the contract globs, from every exporter."""
    views = []
    for pattern in patterns:
        view_pattern = _view_pattern(pattern)
        if view_pattern is not None:
            views.append(View(instrument_name=view_pattern, aggregation=DropAggregation()))
    return views


class _ListenerHandler(BaseHTTPRequestHandler):
    """The standalone listener: ``GET``/``HEAD /metrics`` only (design D1 row "Metrics listener", A21).

    Every method goes through :meth:`_serve`: other paths answer 404, other methods on
    ``/metrics`` 405 (as Go). A connection idle for ``timeout`` seconds is closed. Requests
    are not logged; failures go to the helper logger.
    """

    timeout = 10

    def __getattr__(self, name: str):
        # BaseHTTPRequestHandler dispatches to do_<METHOD> and answers 501 without one.
        if name.startswith("do_"):
            return self._serve
        raise AttributeError(name)

    def _serve(self) -> None:
        if urlsplit(self.path).path != "/metrics":
            self._plain(404, b"404 page not found\n")
            return
        if self.command not in ("GET", "HEAD"):
            self._plain(405, b"Method Not Allowed\n", allow="GET, HEAD")
            return
        from prometheus_client import generate_latest

        try:
            body = generate_latest(_REGISTRY)  # text exposition format 0.0.4
        except Exception as e:  # noqa: BLE001 - a failed collection answers 500, never a traceback
            _logger.warning("[OtelHelper] /metrics collection failed: %s", _truncate(str(e)))
            self._plain(500, b"metric collection failed\n")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _plain(self, status: int, body: bytes, allow: str | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if allow:
            self.send_header("Allow", allow)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def log_message(self, format, *args) -> None:  # noqa: A002 - silence per-request logging
        pass


class _QuietErrors:
    """Logs a request that failed in the server machinery on the helper logger, not as a stderr traceback.

    Also binds without ``HTTPServer.server_bind``'s ``socket.getfqdn(host)``: a reverse DNS
    lookup at setup for an ``OTEL_EXPORTER_PROMETHEUS_HOST`` address (A29 fix round, obs L4).
    """

    def server_bind(self) -> None:
        import socketserver

        socketserver.TCPServer.server_bind(self)  # type: ignore[arg-type]
        host, port = self.server_address[:2]  # type: ignore[attr-defined]
        self.server_name, self.server_port = str(host), port

    def handle_error(self, request, client_address) -> None:
        import sys

        _logger.warning("[OtelHelper] /metrics listener request failed: %s", _truncate(str(sys.exc_info()[1])))


class _DualStackServer(_QuietErrors, ThreadingHTTPServer):
    """``[::]`` accepting IPv4 too (``IPV6_V6ONLY`` off), as Go's ``:port`` and Node's omitted host."""

    address_family = socket.AF_INET6
    daemon_threads = True

    def server_bind(self) -> None:
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()


class _IPv4Server(_QuietErrors, ThreadingHTTPServer):
    daemon_threads = True


class _IPv6Server(_QuietErrors, ThreadingHTTPServer):
    """One IPv6 address (``OTEL_EXPORTER_PROMETHEUS_HOST``)."""

    address_family = socket.AF_INET6
    daemon_threads = True


def _start_listener(port: int, host: str = ""):
    """Binds the listener: on ``host`` when set (OTEL_EXPORTER_PROMETHEUS_HOST, A29), else
    dual-stack, else IPv4 when the platform has no IPv6.

    A port in use is never retried on IPv4 (it would bind only half the address space).
    Returns ``(server, thread)`` (the shape :func:`stop_listener` takes).
    """
    import errno

    server = None
    if host:
        server = (_IPv6Server if ":" in host else _IPv4Server)((host, port), _ListenerHandler)
    elif socket.has_ipv6:
        try:
            server = _DualStackServer(("::", port), _ListenerHandler)
        except OSError as e:
            if e.errno == errno.EADDRINUSE:
                raise
    if server is None:
        server = _IPv4Server(("0.0.0.0", port), _ListenerHandler)
    thread = threading.Thread(target=server.serve_forever, name="otel-helper-metrics", daemon=True)
    thread.start()
    return server, thread


class _MetricsASGIApp:
    """The ASGI app returned by metrics_app().

    A class instance, not a function: Starlette's ``Route``/``add_route`` treat a
    plain function as a request endpoint (``TypeError`` on call) and an object
    with ``__call__`` as an ASGI app, so the exact-path form works.
    """

    def __init__(self) -> None:
        from prometheus_client import make_asgi_app

        self._inner = make_asgi_app(registry=_REGISTRY)

    async def __call__(self, scope, receive, send) -> None:
        kind = scope.get("type")
        if kind == "lifespan":  # served standalone (uvicorn staffops_otel_helper...): nothing to start or stop
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if kind != "http":
            return  # websocket and unknown scopes: not served
        if not _active_readers:
            if _deliberately_inactive:
                status, body = 200, b""
            else:
                status, body = 503, b"staffops_otel_helper: prometheus metrics reader not configured\n"
            await send({"type": "http.response.start", "status": status,
                        "headers": [(b"content-type", b"text/plain; charset=utf-8"),
                                    (b"content-length", str(len(body)).encode())]})
            await send({"type": "http.response.body", "body": body})
            return
        await self._inner(scope, receive, send)


def metrics_app() -> _MetricsASGIApp:
    """ASGI app serving the Prometheus scrape endpoint.

    Serve it on the application's own server — the correct mode under
    multi-worker servers (gunicorn/uvicorn workers), where the standalone
    listener cannot bind the same port in every process. Register it on the
    exact path:

        app.add_route("/metrics", metrics_app())          # FastAPI / Starlette
        # or: Route("/metrics", metrics_app()) in a Starlette routes list

    ``app.mount("/metrics", metrics_app())`` also works, but Starlette then
    answers ``GET /metrics`` with a 307 redirect to ``/metrics/``.

    Combine with OTEL_EXPORTER_PROMETHEUS_PORT=0 (or OTEL_HELPER_METRICS_PORT=0, or
    ``prometheus_metrics_port=0``) to suppress the standalone listener.
    Without an active Prometheus reader it answers 503 (before setup, after a
    failed setup, after shutdown_telemetry()) or, when setup deliberately left
    Prometheus off, 200 with an empty body.
    """
    return _MetricsASGIApp()


def _bind_hint(options) -> str:
    """Names what set the port and host (A29 fix round, M1): the standard variable now
    outranks OTEL_HELPER_METRICS_PORT, so the cure must name the one that applied."""
    source = options._effective.get("prometheus_metrics_port_source", "")
    off = ("prometheus_metrics_port=0" if source == "prometheus_metrics_port"
           else f"{source or ENV_PROMETHEUS_PORT}=0")
    hint = ""
    if options.resolved.prometheus_metrics_host:
        hint = f"{ENV_PROMETHEUS_HOST} sets the address: is it an address of this host? "
    return (f"{hint}If another process (or worker) owns the port, mount staffops_otel_helper.metrics_app() "
            f"on your app and set {off} to disable the standalone listener.")


def stop_listener(listener) -> None:
    """Stop a standalone /metrics listener started by build_metrics()."""
    if isinstance(listener, tuple) and listener:
        server = listener[0]
        server.shutdown()
        server.server_close()


def foreign_meter_provider() -> bool:
    """Whether code other than the helper already installed a global MeterProvider.

    Until one is set, the API returns its proxy (class ``_ProxyMeterProvider``; compared by
    name, nothing is imported from ``opentelemetry.*._internal``, P17).
    """
    return type(metrics.get_meter_provider()).__name__ != "_ProxyMeterProvider"


def build_metrics(resource: Resource, options: TelemetryOptions):
    """Build the MeterProvider (not installed globally) and the standalone listener.

    Returns ``(provider, listener, prometheus_reader)``; ``listener`` is None
    without a standalone /metrics port, ``prometheus_reader`` None without the
    prometheus exporter. Call activate(prometheus_reader) once the provider is
    installed. On failure nothing built here is left running.
    The active exporters come from options.resolved_metric_exporters():
    "otlp" (push), "console" and/or "prometheus" (/metrics scrape) on the same provider.
    """
    exporters = options.resolved_metric_exporters()
    readers = []
    listener = None
    prometheus_reader = None

    try:
        plan = options.otlp_export("metrics")
        if "otlp" in exporters and plan is not None:
            # The resolved OTLP timeout is the export deadline (A18); OTEL_METRIC_EXPORT_TIMEOUT is not honored.
            readers.append(PeriodicExportingMetricReader(
                build_otlp_exporter(plan), export_interval_millis=options.resolved.export_interval_ms,
                export_timeout_millis=plan.timeout_ms,
            ))

        if "console" in exporters:
            from opentelemetry.sdk.metrics.export import ConsoleMetricExporter

            readers.append(PeriodicExportingMetricReader(
                ConsoleMetricExporter(), export_interval_millis=options.resolved.export_interval_ms,
            ))

        if "prometheus" in exporters:
            # A foreign global MeterProvider will keep the helper's provider off (setup warns at install):
            # bind nothing, so a busy port cannot fail setup for a listener that would be stopped anyway.
            if options.resolved.prometheus_metrics_port > 0 and not foreign_meter_provider():
                host = options.resolved.prometheus_metrics_host
                where = f"{host or 'every interface'}, port {options.resolved.prometheus_metrics_port}"
                try:
                    listener = _start_listener(options.resolved.prometheus_metrics_port, host)
                except OSError as e:
                    raise RuntimeError(f"[OtelHelper] Could not bind the /metrics listener on {where}: {e}. "
                                       f"{_bind_hint(options)}") from e
                _logger.info(
                    "OTel Prometheus exporter: metrics available on %s, port %d at /metrics",
                    host or "every interface", options.resolved.prometheus_metrics_port,
                )
            else:
                _logger.info(
                    "OTel Prometheus exporter: standalone listener disabled (port=0); "
                    "mount staffops_otel_helper.metrics_app() to expose /metrics",
                )
            # Registered on the registry only once the listener is bound, so a busy
            # port leaves nothing to roll back on it.
            prometheus_reader = _HelperPrometheusReader()
            readers.append(prometheus_reader)

        disabled = options.resolved.disabled_metrics
        views = _build_drop_views(disabled) if disabled else []
        if not options.resolved.http_opt_in_metrics:
            views += [View(instrument_name=name, meter_name=scope, aggregation=DropAggregation())
                      for name in HTTP_OPT_IN_METRICS for scope in HTTP_OPT_IN_METRICS_SCOPES]

        kwargs = dict(
            resource=resource,
            metric_readers=readers,
            exemplar_filter=TraceBasedExemplarFilter(),
            shutdown_on_exit=False,  # the helper's atexit hook runs shutdown_telemetry()
        )
        if views:
            kwargs["views"] = views

        return MeterProvider(**kwargs), listener, prometheus_reader
    except BaseException:
        # Readers not yet owned by a MeterProvider: stop them here (periodic
        # readers own an export thread; the Prometheus one a registry entry).
        for reader in readers:
            try:
                reader.shutdown(timeout_millis=0)
            except Exception:  # noqa: BLE001 - best effort; the original error wins
                pass
        stop_listener(listener)
        raise


def configure_metrics(resource: Resource, options: TelemetryOptions) -> MeterProvider:
    """Build the MeterProvider and set it as the global one (with trace-based exemplars)."""
    provider, _, prometheus_reader = build_metrics(resource, options)
    metrics.set_meter_provider(provider)
    activate(prometheus_reader)
    return provider
