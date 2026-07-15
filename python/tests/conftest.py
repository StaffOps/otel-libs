"""Shared fixtures for OTel test isolation.

Ensures each test starts with a clean OTel global state — no bleeding between tests.
"""

import os
import logging

import pytest
import opentelemetry.trace as _trace_mod
import opentelemetry.metrics._internal as _metrics_mod
from opentelemetry.util._once import Once
from opentelemetry.sdk._logs import LoggingHandler

from otel_helper.setup import reset_telemetry

ALL_ENV_VARS = [
    "SERVICE_NAME",
    "OTEL_SERVICE_NAME",
    "ENVIRONMENT",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_INSECURE",
    "OTEL_EXPORTER_OTLP_PROTOCOL",
    "OTEL_HELPER_DEBUG_LEVEL",
    "OTEL_HELPER_EXTRA_INSTRUMENTATION",
    "OTEL_HELPER_SAMPLE_RATIO",
    "OTEL_HELPER_DISABLED_SIGNALS",
    "OTEL_HELPER_DISABLED_METRICS",
    "OTEL_HELPER_METRICS_PORT",
    "OTEL_METRICS_EXPORTER",
    "OTEL_METRIC_EXPORT_INTERVAL",
    "OTEL_TRACES_SAMPLER",
    "OTEL_TRACES_SAMPLER_ARG",
    "OTEL_PYTHON_HTTPX_EXCLUDED_URLS",
    "OTEL_PYTHON_REQUESTS_EXCLUDED_URLS",
]


def _reset_otel_globals():
    """Reset OTel SDK global singletons so set_tracer_provider/set_meter_provider can be called again."""
    _trace_mod._TRACER_PROVIDER_SET_ONCE = Once()
    _trace_mod._TRACER_PROVIDER = None
    _trace_mod._PROXY_TRACER_PROVIDER._tracer_provider = None

    _metrics_mod._METER_PROVIDER_SET_ONCE = Once()
    _metrics_mod._METER_PROVIDER = None
    _metrics_mod._PROXY_METER_PROVIDER._meter_provider = None


def _clean_logging_handlers():
    """Remove OTel LoggingHandlers from root logger to avoid duplicate exports."""
    root = logging.getLogger()
    root.handlers = [h for h in root.handlers if not isinstance(h, LoggingHandler)]
    root.setLevel(logging.WARNING)


@pytest.fixture(autouse=True)
def isolate_otel_state():
    """Autouse fixture that isolates OTel global state per test.

    Before each test: clears env vars, resets the otel_helper init guard,
    resets SDK global providers, and removes OTel log handlers.
    After each test: same cleanup to prevent leakage into the next test.
    """
    # --- Setup ---
    for var in ALL_ENV_VARS:
        os.environ.pop(var, None)
    reset_telemetry()
    _reset_otel_globals()
    _clean_logging_handlers()

    yield

    # --- Teardown ---
    for var in ALL_ENV_VARS:
        os.environ.pop(var, None)
    reset_telemetry()
    _reset_otel_globals()
    _clean_logging_handlers()
