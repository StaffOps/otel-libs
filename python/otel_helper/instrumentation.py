"""Auto-instrumentation setup for common Python frameworks.

Health-check exclusion (R7, design D5): HTTP server spans are dropped by the
helper's sampler (``tracing.HealthCheckSampler``), never through FastAPI's
``excluded_urls``, which would drop the request from the HTTP server metrics
too. ``OTEL_PYTHON_FASTAPI_EXCLUDED_URLS`` / ``OTEL_PYTHON_EXCLUDED_URLS`` set by
the user keep their upstream meaning (span and metric dropped). gRPC servers use
the instrumentation's native filter: it emits no RPC metrics, so nothing is lost.
Client calls to health paths / health RPCs are not traced either (A5/A7 rulings:
exact path match; upstream ``excluded_urls`` drops the client metric too).
"""

import importlib
import os
import re
from contextlib import contextmanager

from otel_helper.config import TelemetryOptions
from otel_helper.tracing import DEFAULT_HEALTH_PATHS, normalize_health_path

_GRPC = frozenset(["GrpcInstrumentorServer", "GrpcAioInstrumentorServer",
                   "GrpcInstrumentorClient", "GrpcAioInstrumentorClient"])
_ENV_GRPC_EXCLUDED_SERVICES = "OTEL_PYTHON_GRPC_EXCLUDED_SERVICES"
_ENV_EXCLUDED_URLS = "OTEL_PYTHON_EXCLUDED_URLS"
_ENV_HTTPX_EXCLUDED_URLS = "OTEL_PYTHON_HTTPX_EXCLUDED_URLS"
_ENV_REQUESTS_EXCLUDED_URLS = "OTEL_PYTHON_REQUESTS_EXCLUDED_URLS"

# (module, class, helper EXTRA_INSTRUMENTATION flag or None = always on)
_INSTRUMENTORS = (
    ("opentelemetry.instrumentation.fastapi", "FastAPIInstrumentor", None),
    ("opentelemetry.instrumentation.httpx", "HTTPXClientInstrumentor", None),
    ("opentelemetry.instrumentation.requests", "RequestsInstrumentor", None),
    ("opentelemetry.instrumentation.sqlalchemy", "SQLAlchemyInstrumentor", "SQL"),
    ("opentelemetry.instrumentation.redis", "RedisInstrumentor", "REDIS"),
    ("opentelemetry.instrumentation.botocore", "BotocoreInstrumentor", "AWS"),
    # gRPC client + server: grpc.aio and the sync grpc.server()/channels (P13)
    ("opentelemetry.instrumentation.grpc", "GrpcAioInstrumentorClient", None),
    ("opentelemetry.instrumentation.grpc", "GrpcAioInstrumentorServer", None),
    ("opentelemetry.instrumentation.grpc", "GrpcInstrumentorClient", None),
    ("opentelemetry.instrumentation.grpc", "GrpcInstrumentorServer", None),
    # System/runtime metrics (CPU, memory, GC, network)
    ("opentelemetry.instrumentation.system_metrics", "SystemMetricsInstrumentor", None),
)


@contextmanager
def _env_hidden_or_set(values: dict):
    """Temporarily set (str) or hide (None) env vars around an upstream constructor/instrument().

    Restored on exit: the helper never leaves the environment changed.
    """
    saved = {k: os.environ.get(k) for k in values}
    try:
        for k, v in values.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _user_grpc_excluded_services() -> list[str]:
    """``OTEL_PYTHON_GRPC_EXCLUDED_SERVICES`` parsed as upstream does (comma list, trimmed)."""
    return [s.strip() for s in os.environ.get(_ENV_GRPC_EXCLUDED_SERVICES, "").split(",") if s.strip()]


def _grpc_filter():
    """Trace unless the call is a health RPC or a service the user excluded.

    Upstream ORs any ``filter_`` with its own ``OTEL_PYTHON_GRPC_EXCLUDED_SERVICES``
    filter (trace when either says trace), which would re-enable the user's
    excluded services; so the helper folds the variable into one filter and
    hides it from the instrumentors while they are built.
    """
    from opentelemetry.instrumentation.grpc import filters

    excluded = [filters.health_check()] + [filters.service_name(s) for s in _user_grpc_excluded_services()]
    return filters.negate(filters.any_of(*excluded))


def health_url_pattern(paths) -> str:
    """One anchored regex matching full URLs whose path is a health path (D5 normalization).

    Exact path, case-insensitive, trailing slashes, query and fragment ignored;
    no commas (upstream splits ``excluded_urls`` on them). Empty -> "".
    """
    normalized = sorted({normalize_health_path(p) for p in paths})
    if not normalized:
        return ""
    alternatives = "|".join(re.escape(p.rstrip("/")) for p in normalized)
    return rf"^[A-Za-z][A-Za-z0-9+.-]*://[^/?#]*(?i:{alternatives})/*(?:[?#].*)?$"


def _client_excluded_urls(library_var: str, health: str) -> str:
    """The user's upstream value (library var, else the generic one) plus the health pattern."""
    user = os.environ.get(library_var, os.environ.get(_ENV_EXCLUDED_URLS, ""))
    return ",".join(x for x in (user, health) if x)


def configure_instrumentations(options: TelemetryOptions, instrumented: list | None = None) -> list:
    """Auto-instrument available frameworks based on options.

    Appends every instrumentor this call turned on to ``instrumented`` as it goes
    (so a failure midway still reports what to undo) and returns the list;
    instrumentors already active before the call are left alone.
    """
    instrumented = [] if instrumented is None else instrumented
    paths = DEFAULT_HEALTH_PATHS if options.health_paths is None else options.health_paths
    health = health_url_pattern(paths)

    for module_name, class_name, flag in _INSTRUMENTORS:
        if flag is not None and not options.has_instrumentation(flag):
            continue
        try:
            cls = getattr(importlib.import_module(module_name), class_name)
            env: dict = {}
            kwargs: dict = {}
            if class_name in _GRPC:
                env = {_ENV_GRPC_EXCLUDED_SERVICES: None}
            elif class_name == "HTTPXClientInstrumentor":  # reads env only, no excluded_urls kwarg
                env = {_ENV_HTTPX_EXCLUDED_URLS: _client_excluded_urls(_ENV_HTTPX_EXCLUDED_URLS, health)}
            elif class_name == "RequestsInstrumentor":
                kwargs = {"excluded_urls": _client_excluded_urls(_ENV_REQUESTS_EXCLUDED_URLS, health)}
            grpc_filter = _grpc_filter() if class_name in _GRPC else None
            with _env_hidden_or_set(env):
                instrumentor = cls(filter_=grpc_filter) if class_name in _GRPC else cls()
                if instrumentor.is_instrumented_by_opentelemetry:
                    continue
                instrumentor.instrument(**kwargs)
        except ImportError:
            continue
        # instrument() is a no-op (with its own warning) when the library is missing.
        if instrumentor.is_instrumented_by_opentelemetry:
            instrumented.append(instrumentor)
    return instrumented


def instrument_fastapi(app):
    """Instrument a FastAPI app created from a class imported before ``setup_telemetry()`` (P11).

    ``setup_telemetry()`` patches the ``fastapi.FastAPI`` class, which only
    affects apps built from the patched class. With ``from fastapi import FastAPI``
    executed before setup (a common import order), call this on the app instead.
    Idempotent: an app already instrumented (either way) is left alone. Health
    paths are excluded from traces only, as for auto-instrumented apps.

    Returns the app.
    """
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    if getattr(app, "_is_instrumented_by_opentelemetry", False):
        return app
    FastAPIInstrumentor.instrument_app(app)
    return app


def uninstrument(instrumented: list) -> list[str]:
    """Undo configure_instrumentations() in reverse order; returns the errors."""
    errors = []
    for instrumentor in reversed(instrumented):
        try:
            instrumentor.uninstrument()
        except Exception as e:  # noqa: BLE001 - reported by the caller
            errors.append(f"{type(instrumentor).__name__}: {e}")
    instrumented.clear()
    return errors
