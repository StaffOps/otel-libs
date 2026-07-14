"""OpenTelemetry Helper for Python.

Usage:
    from otel_helper import setup_telemetry
    setup_telemetry()
"""

from opentelemetry import trace as _trace
from opentelemetry import metrics as _metrics

from otel_helper.config import TelemetryOptions, DeploymentEnvironment, get_default_log_level
from otel_helper.metrics import metrics_app
from otel_helper.setup import setup_telemetry, reset_telemetry
from otel_helper.tracing import start_root_span


def get_tracer(name: str | None = None) -> _trace.Tracer:
    """Get a Tracer instance. Uses the global TracerProvider.

    Args:
        name: Tracer name. If None, uses the service name from env.
    """
    return _trace.get_tracer(name or "otel-helper")


def get_meter(name: str | None = None) -> _metrics.Meter:
    """Get a Meter instance. Uses the global MeterProvider.

    Args:
        name: Meter name. If None, uses the service name from env.
    """
    return _metrics.get_meter(name or "otel-helper")


__all__ = [
    "setup_telemetry",
    "reset_telemetry",
    "TelemetryOptions",
    "DeploymentEnvironment",
    "get_default_log_level",
    "get_tracer",
    "get_meter",
    "metrics_app",
    "start_root_span",
    "ext",
]
