"""Metrics setup — equivalent to MetricsSetup.cs."""

from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.metrics._internal.exemplar import TraceBasedExemplarFilter
from opentelemetry.sdk.metrics.view import View, DropAggregation
from opentelemetry.sdk.resources import Resource
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter

from otel_helper.config import TelemetryOptions


def _build_drop_views(patterns: list[str]) -> list[View]:
    """Build View objects that drop metrics matching wildcard patterns."""
    views = []
    for pattern in patterns:
        views.append(View(instrument_name=pattern, aggregation=DropAggregation()))
    return views


def configure_metrics(resource: Resource, options: TelemetryOptions) -> MeterProvider:
    """Configure and set the global MeterProvider with trace-based exemplars."""
    exporter = OTLPMetricExporter(
        endpoint=options.otel_endpoint,
        insecure=True,
        timeout=options.export_timeout_ms / 1000,
    )
    reader = PeriodicExportingMetricReader(exporter)

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
