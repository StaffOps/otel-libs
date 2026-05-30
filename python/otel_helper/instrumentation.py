"""Auto-instrumentation setup for common Python frameworks."""

import os
from otel_helper.config import TelemetryOptions


HEALTH_PATHS = frozenset(["/ping", "/health", "/healthz", "/ready"])


def _is_health_path(path: str) -> bool:
    return path in HEALTH_PATHS


def configure_instrumentations(options: TelemetryOptions) -> None:
    """Auto-instrument available frameworks based on options."""

    # Set excluded URLs env var for HTTP client instrumentors (health path filter)
    _excluded = ",".join(HEALTH_PATHS)
    os.environ.setdefault("OTEL_PYTHON_HTTPX_EXCLUDED_URLS", _excluded)
    os.environ.setdefault("OTEL_PYTHON_REQUESTS_EXCLUDED_URLS", _excluded)

    # FastAPI / Starlette
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        FastAPIInstrumentor().instrument(
            excluded_urls=",".join(HEALTH_PATHS),
        )
    except ImportError:
        pass

    # HTTPX (async HTTP client)
    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        HTTPXClientInstrumentor().instrument()
    except ImportError:
        pass

    # Requests (sync HTTP client)
    try:
        from opentelemetry.instrumentation.requests import RequestsInstrumentor
        RequestsInstrumentor().instrument()
    except ImportError:
        pass

    # SQLAlchemy (if SQL instrumentation enabled)
    if options.has_instrumentation("SQL"):
        try:
            from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
            SQLAlchemyInstrumentor().instrument()
        except ImportError:
            pass

    # Redis
    if options.has_instrumentation("REDIS"):
        try:
            from opentelemetry.instrumentation.redis import RedisInstrumentor
            RedisInstrumentor().instrument()
        except ImportError:
            pass

    # AWS (botocore/boto3/aiobotocore)
    if options.has_instrumentation("AWS"):
        try:
            from opentelemetry.instrumentation.botocore import BotocoreInstrumentor
            BotocoreInstrumentor().instrument()
        except ImportError:
            pass

    # gRPC client + server (async — patches grpc.aio automatically)
    try:
        from opentelemetry.instrumentation.grpc import GrpcAioInstrumentorClient
        GrpcAioInstrumentorClient().instrument()
    except ImportError:
        pass

    try:
        from opentelemetry.instrumentation.grpc import GrpcAioInstrumentorServer
        GrpcAioInstrumentorServer().instrument()
    except ImportError:
        pass

    # System/runtime metrics (CPU, memory, GC, network)
    try:
        from opentelemetry.instrumentation.system_metrics import SystemMetricsInstrumentor
        SystemMetricsInstrumentor().instrument()
    except ImportError:
        pass
