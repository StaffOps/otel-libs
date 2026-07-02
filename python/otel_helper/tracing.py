"""Tracing setup — equivalent to TracerSetup.cs."""

from contextlib import contextmanager

from opentelemetry import context, trace
from opentelemetry.sdk.trace import TracerProvider, SpanProcessor
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ALWAYS_ON, TraceIdRatioBased
from opentelemetry.sdk.resources import Resource

from otel_helper.config import TelemetryOptions
from otel_helper.processors import DebugProcessor

HEALTH_PATHS = frozenset(["/ping", "/health", "/healthz", "/ready"])


def configure_tracing(resource: Resource, options: TelemetryOptions) -> TracerProvider:
    """Configure and set the global TracerProvider.

    When otel_endpoint is empty, TracerProvider is configured without an OTLP exporter.
    In-process context propagation and spans still work (useful for metric exemplars).
    """
    sampler = ALWAYS_ON if options.sample_ratio >= 1.0 else TraceIdRatioBased(options.sample_ratio)

    provider = TracerProvider(
        resource=resource,
        sampler=sampler,
    )

    if options.otel_endpoint:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

        exporter = OTLPSpanExporter(
            endpoint=options.otel_endpoint,
            insecure=True,
            timeout=options.export_timeout_ms / 1000,
        )
        provider.add_span_processor(BatchSpanProcessor(exporter))

    if options.debug_level:
        provider.add_span_processor(DebugProcessor())

    trace.set_tracer_provider(provider)
    return provider


@contextmanager
def start_root_span(tracer, name, **kwargs):
    """Start a new root span (new trace), detaching from any current parent context.

    Use in workers/background services where each iteration should be an independent trace.
    Equivalent to .NET's ActivitySource.StartRootActivity().

    Usage:
        with start_root_span(tracer, "process.cycle") as span:
            span.set_attribute("batch.size", 10)
            ...
    """
    from opentelemetry.trace import NonRecordingSpan, INVALID_SPAN_CONTEXT
    clean_ctx = trace.set_span_in_context(NonRecordingSpan(INVALID_SPAN_CONTEXT))
    token = context.attach(clean_ctx)
    try:
        with tracer.start_as_current_span(name, **kwargs) as span:
            yield span
    finally:
        context.detach(token)
