"""Main entry point — equivalent to AddOtelHelper()."""

from opentelemetry import trace, metrics
from opentelemetry.propagate import set_global_textmap
from opentelemetry.propagators.composite import CompositePropagator
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from opentelemetry.baggage.propagation import W3CBaggagePropagator
from opentelemetry.sdk.resources import Resource, SERVICE_NAME

from otel_helper.config import TelemetryOptions
from otel_helper.tracing import configure_tracing
from otel_helper.metrics import configure_metrics
from otel_helper.logging_setup import configure_logging
from otel_helper.instrumentation import configure_instrumentations

_initialized = False


def setup_telemetry(options: TelemetryOptions | None = None) -> TelemetryOptions:
    """
    Configure all telemetry: tracing, metrics, logging, and auto-instrumentation.

    Call once at application startup. Equivalent to services.AddOtelHelper() in .NET.

    Args:
        options: Optional configuration. If None, uses env var defaults.

    Returns:
        The resolved options (useful for inspection/testing).
    """
    global _initialized
    if _initialized:
        return options or TelemetryOptions()

    if options is None:
        options = TelemetryOptions()

    options.resolve_from_env()
    options.validate()

    # Build resource
    attributes = {SERVICE_NAME: options.service_name}
    attributes.update(options.resource_attributes)
    resource = Resource.create(attributes)

    # Configure propagators (W3C Trace Context + Baggage)
    set_global_textmap(CompositePropagator([
        TraceContextTextMapPropagator(),
        W3CBaggagePropagator(),
    ]))

    # Configure signals
    if options.is_signal_enabled('traces'):
        configure_tracing(resource, options)
    if options.is_signal_enabled('metrics') and options.resolved_metric_exporters():
        configure_metrics(resource, options)
    if options.is_signal_enabled('logs'):
        configure_logging(resource, options)
    configure_instrumentations(options)

    _initialized = True
    return options


def reset_telemetry() -> None:
    """Reset global state. For testing only."""
    global _initialized
    _initialized = False
