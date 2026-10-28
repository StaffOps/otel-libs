"""Shared fixtures for OTel test isolation.

Ensures each test starts with a clean OTel global state — no bleeding between tests.
"""

import os
import logging
import logging.config

import pytest
import opentelemetry.trace as _trace_mod
import opentelemetry.metrics._internal as _metrics_mod
from opentelemetry.util._once import Once
import opentelemetry._logs._internal as _logs_mod
from opentelemetry.instrumentation.logging.handler import LoggingHandler as _ContribLoggingHandler
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


def _clear_env():
    """Remove every helper-relevant variable, including all OTEL_* (per-signal OTLP vars too)."""
    for var in list(os.environ):
        if var in ALL_ENV_VARS or var.startswith("OTEL_"):
            os.environ.pop(var, None)


def _reset_otel_globals():
    """Reset OTel SDK global singletons so set_tracer_provider/set_meter_provider can be called again."""
    _trace_mod._TRACER_PROVIDER_SET_ONCE = Once()
    _trace_mod._TRACER_PROVIDER = None
    _trace_mod._PROXY_TRACER_PROVIDER._tracer_provider = None

    _metrics_mod._METER_PROVIDER_SET_ONCE = Once()
    _metrics_mod._METER_PROVIDER = None
    _metrics_mod._PROXY_METER_PROVIDER._meter_provider = None

    _logs_mod._LOGGER_PROVIDER_SET_ONCE = Once()
    _logs_mod._LOGGER_PROVIDER = None


def _shutdown_stray_prometheus_readers():
    """Readers from tests that built a MeterProvider directly and never shut it down."""
    from otel_helper import metrics as metrics_mod

    for reader in list(metrics_mod._active_readers):
        reader.shutdown()


_STDLIB_LAST_RESORT = logging.lastResort
_STDLIB_CONFIG_FNS = (logging.basicConfig, logging.config.dictConfig, logging.config.fileConfig)
_OTEL_HANDLERS = (LoggingHandler, _ContribLoggingHandler)


def _clean_logging_handlers():
    """Undo what a test left behind: OTel handlers, console fallback, config wrappers, levels."""
    root = logging.getLogger()
    root.handlers = [h for h in root.handlers if not isinstance(h, _OTEL_HANDLERS)]
    root.setLevel(logging.WARNING)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers = [h for h in lg.handlers if not isinstance(h, _OTEL_HANDLERS)]
    logging.lastResort = _STDLIB_LAST_RESORT
    logging.basicConfig, logging.config.dictConfig, logging.config.fileConfig = _STDLIB_CONFIG_FNS
    for name in ("urllib3", "httpcore", "grpc", "httpx", "hpack", "opentelemetry", "otel_helper"):
        logging.getLogger(name).setLevel(logging.NOTSET)


@pytest.fixture(autouse=True)
def isolate_otel_state():
    """Autouse fixture that isolates OTel global state per test.

    Before each test: clears env vars, resets the otel_helper init guard,
    resets SDK global providers, and removes OTel log handlers.
    After each test: same cleanup to prevent leakage into the next test.
    """
    # --- Setup ---
    _clear_env()
    reset_telemetry()
    _shutdown_stray_prometheus_readers()
    _reset_otel_globals()
    _clean_logging_handlers()

    yield

    # --- Teardown ---
    _clear_env()
    reset_telemetry()
    _shutdown_stray_prometheus_readers()
    _reset_otel_globals()
    _clean_logging_handlers()
