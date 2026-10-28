"""Opt-in SQLAlchemy instrumentation extension."""

from otel_helper._semconv import ensure_semconv_default


def instrument_sql(engine=None) -> None:
    """Enable SQLAlchemy tracing instrumentation.

    Args:
        engine: Optional SQLAlchemy engine instance. If provided, instruments
                only that engine; otherwise instruments all engines globally.
    """
    ensure_semconv_default()  # before the instrumentor reads it (design D12)
    try:
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
    except ImportError as e:
        raise ImportError(
            "SQL instrumentation not installed. Run: pip install otel-helper[sql]"
        ) from e

    if engine:
        SQLAlchemyInstrumentor().instrument(engine=engine)
    else:
        SQLAlchemyInstrumentor().instrument()
