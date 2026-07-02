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


def configure_metrics(resource: Resource, options: TelemetryOptions) -> MeterProvider:
    """Configure and set the global MeterProvider with trace-based exemplars.

    When otel_endpoint is set, uses OTLP push exporter.
    When otel_endpoint is empty, falls back to Prometheus HTTP scrape endpoint.
    """
    if options.otel_endpoint:
        # OTLP push mode (existing behavior)
        from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter

        exporter = OTLPMetricExporter(
            endpoint=options.otel_endpoint,
            insecure=True,
            timeout=options.export_timeout_ms / 1000,
        )
        reader = PeriodicExportingMetricReader(exporter, export_interval_millis=30_000)
    else:
        # Prometheus scrape fallback — expose /metrics HTTP endpoint
        from opentelemetry.exporter.prometheus import PrometheusMetricReader
        from prometheus_client import start_http_server

        start_http_server(port=options.prometheus_metrics_port)
        reader = PrometheusMetricReader()
        _logger.info(
            "OTel Prometheus fallback: metrics available at http://0.0.0.0:%d/metrics",
            options.prometheus_metrics_port,
        )

    views = _build_drop_views(options.disabled_metrics) if options.disabled_metrics else []

    kwargs = dict(
        resource=resource,
        metric_readers=[reader],
        exemplar_filter=TraceBasedExemplarFilter(),
    )
    if views:
        kwargs["views"] = views

    provider = MeterProvider(**kwargs)
    metrics.set_meter_provider(provider)
    return provider
