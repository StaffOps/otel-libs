"""Opt-in Redis instrumentation extension."""

from otel_helper._semconv import ensure_semconv_default


def instrument_redis() -> None:
    """Enable Redis tracing instrumentation."""
    ensure_semconv_default()  # before the instrumentor reads it (design D12)
    try:
        from opentelemetry.instrumentation.redis import RedisInstrumentor
    except ImportError as e:
        raise ImportError(
            "Redis instrumentation not installed. Run: pip install otel-helper[redis]"
        ) from e

    RedisInstrumentor().instrument()
