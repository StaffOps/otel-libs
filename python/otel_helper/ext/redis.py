"""Opt-in Redis instrumentation extension."""


def instrument_redis() -> None:
    """Enable Redis tracing instrumentation."""
    try:
        from opentelemetry.instrumentation.redis import RedisInstrumentor
    except ImportError as e:
        raise ImportError(
            "Redis instrumentation not installed. Run: pip install otel-helper[redis]"
        ) from e

    RedisInstrumentor().instrument()
