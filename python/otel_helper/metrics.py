"""Metrics setup — equivalent to MetricsSetup.cs."""

import logging

from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.metrics._internal.exemplar import TraceBasedExemplarFilter
from opentelemetry.sdk.metrics.view import View, DropAggregation
from opentelemetry.sdk.resources import Resource

from otel_helper.config import TelemetryOptions

_logger = logging.getLogger(__name__)


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
    """
    from prometheus_client import make_asgi_app

    return make_asgi_app()


def configure_metrics(resource: Resource, options: TelemetryOptions) -> MeterProvider:
    """Configure and set the global MeterProvider with trace-based exemplars.

    The active exporters come from options.resolved_metric_exporters():
    "otlp" (push) and/or "prometheus" (/metrics scrape) on the same provider.
    """
    exporters = options.resolved_metric_exporters()
    readers = []

    if "otlp" in exporters:
        from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter

        exporter = OTLPMetricExporter(
            endpoint=options.otel_endpoint,
            insecure=options.resolve_insecure(),
            timeout=options.export_timeout_ms / 1000,
        )
        readers.append(PeriodicExportingMetricReader(
            exporter, export_interval_millis=options.export_interval_ms,
        ))

    if "prometheus" in exporters:
        from opentelemetry.exporter.prometheus import PrometheusMetricReader

        readers.append(PrometheusMetricReader())
        if options.prometheus_metrics_port > 0:
            from prometheus_client import start_http_server

            try:
                start_http_server(port=options.prometheus_metrics_port)
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

    views = _build_drop_views(options.disabled_metrics) if options.disabled_metrics else []

    kwargs = dict(
        resource=resource,
        metric_readers=readers,
        exemplar_filter=TraceBasedExemplarFilter(),
    )
    if views:
        kwargs["views"] = views

    provider = MeterProvider(**kwargs)
    metrics.set_meter_provider(provider)
    return provider
