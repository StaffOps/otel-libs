"""Metrics setup — equivalent to MetricsSetup.cs."""

import logging
import threading

from opentelemetry import metrics
from opentelemetry.exporter.prometheus import PrometheusMetricReader
from opentelemetry.sdk.metrics import MeterProvider, TraceBasedExemplarFilter
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.metrics.view import View, DropAggregation
from opentelemetry.sdk.resources import Resource

from otel_helper._exporters import build_otlp_exporter
from prometheus_client import CollectorRegistry

from otel_helper.config import TelemetryOptions

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


def _build_drop_views(patterns: list[str]) -> list[View]:
    """Build View objects that drop metrics matching wildcard patterns."""
    views = []
    for pattern in patterns:
        views.append(View(instrument_name=pattern, aggregation=DropAggregation()))
    return views


def metrics_app():
    """ASGI app serving the Prometheus scrape endpoint.

    Mount it on the application's own server — the correct mode under
    multi-worker servers (gunicorn/uvicorn workers), where the standalone
    listener cannot bind the same port in every process:

        app.mount("/metrics", metrics_app())

    Combine with OTEL_HELPER_METRICS_PORT=0 to suppress the standalone listener.
    Without an active Prometheus reader it answers 503 (before setup, after a
    failed setup, after shutdown_telemetry()) or, when setup deliberately left
    Prometheus off, 200 with an empty body.
    """
    from prometheus_client import make_asgi_app

    inner = make_asgi_app(registry=_REGISTRY)

    async def app(scope, receive, send):
        if scope.get("type") == "http" and not _active_readers:
            if _deliberately_inactive:
                status, body = 200, b""
            else:
                status, body = 503, b"otel_helper: prometheus metrics reader not configured\n"
            await send({"type": "http.response.start", "status": status,
                        "headers": [(b"content-type", b"text/plain; charset=utf-8"),
                                    (b"content-length", str(len(body)).encode())]})
            await send({"type": "http.response.body", "body": body})
            return
        await inner(scope, receive, send)

    return app


def stop_listener(listener) -> None:
    """Stop a standalone /metrics listener started by build_metrics()."""
    if isinstance(listener, tuple) and listener:
        server = listener[0]
        server.shutdown()
        server.server_close()


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
            readers.append(PeriodicExportingMetricReader(
                build_otlp_exporter(plan), export_interval_millis=options.export_interval_ms,
            ))

        if "console" in exporters:
            from opentelemetry.sdk.metrics.export import ConsoleMetricExporter

            readers.append(PeriodicExportingMetricReader(
                ConsoleMetricExporter(), export_interval_millis=options.export_interval_ms,
            ))

        if "prometheus" in exporters:
            if options.prometheus_metrics_port > 0:
                from prometheus_client import start_http_server

                try:
                    listener = start_http_server(port=options.prometheus_metrics_port, registry=_REGISTRY)
                except OSError as e:
                    raise RuntimeError(
                        f"[OtelHelper] Could not bind the /metrics listener on port "
                        f"{options.prometheus_metrics_port}: {e}. If another process (or worker) owns "
                        "the port, mount otel_helper.metrics_app() on your app and set "
                        "OTEL_HELPER_METRICS_PORT=0 to disable the standalone listener."
                    ) from e
                _logger.info(
                    "OTel Prometheus exporter: metrics available at http://0.0.0.0:%d/metrics",
                    options.prometheus_metrics_port,
                )
            else:
                _logger.info(
                    "OTel Prometheus exporter: standalone listener disabled (port=0); "
                    "mount otel_helper.metrics_app() to expose /metrics",
                )
            # Registered on the registry only once the listener is bound, so a busy
            # port leaves nothing to roll back on it.
            prometheus_reader = _HelperPrometheusReader()
            readers.append(prometheus_reader)

        views = _build_drop_views(options.disabled_metrics) if options.disabled_metrics else []

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
