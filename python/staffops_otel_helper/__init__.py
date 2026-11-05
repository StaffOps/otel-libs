"""OpenTelemetry Helper for Python.

Usage:
    from staffops_otel_helper import setup_telemetry
    setup_telemetry()
"""

# Must run before anything imports opentelemetry.propagate (see _propagators).
from staffops_otel_helper._propagators import import_propagate_safely as _import_propagate_safely

_import_propagate_safely()

# Before anything imports the SDK log module: it parses OTEL_RESOURCE_ATTRIBUTES on import
# and would echo malformed items (see _resource_echo).
from staffops_otel_helper._resource_echo import import_sdk_logs_quietly as _import_sdk_logs_quietly  # noqa: E402

_import_sdk_logs_quietly()

from opentelemetry import trace as _trace  # noqa: E402
from opentelemetry import metrics as _metrics  # noqa: E402

from staffops_otel_helper.config import TelemetryOptions, DeploymentEnvironment, get_default_log_level
from staffops_otel_helper.instrumentation import instrument_fastapi
from staffops_otel_helper.metrics import metrics_app
from staffops_otel_helper.setup import emit_fork_warning as _emit_fork_warning
from staffops_otel_helper.setup import setup_telemetry, shutdown_telemetry, reset_telemetry
from staffops_otel_helper.tracing import DEFAULT_HEALTH_PATHS, start_root_span


def get_tracer(name: str | None = None) -> _trace.Tracer:
    """Get a Tracer instance. Uses the global TracerProvider.

    Args:
        name: Instrumentation scope name (your module or library). Defaults
              to ``"otel-helper"``; the service name comes from the resource.
    """
    _emit_fork_warning()
    return _trace.get_tracer(name or "otel-helper")


def get_meter(name: str | None = None) -> _metrics.Meter:
    """Get a Meter instance. Uses the global MeterProvider.

    Args:
        name: Instrumentation scope name (your module or library). Defaults
              to ``"otel-helper"``; the service name comes from the resource.
    """
    _emit_fork_warning()
    return _metrics.get_meter(name or "otel-helper")


__all__ = [
    "setup_telemetry",
    "shutdown_telemetry",
    "reset_telemetry",
    "TelemetryOptions",
    "DeploymentEnvironment",
    "get_default_log_level",
    "get_tracer",
    "get_meter",
    "metrics_app",
    "instrument_fastapi",
    "start_root_span",
    "DEFAULT_HEALTH_PATHS",
    "ext",
]
