"""Opt-in Redis instrumentation extension."""


def instrument_redis() -> None:
    """Enable Redis tracing instrumentation."""
    from opentelemetry.instrumentation.redis import RedisInstrumentor

    RedisInstrumentor().instrument()
