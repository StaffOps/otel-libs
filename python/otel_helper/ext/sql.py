"""Opt-in SQLAlchemy instrumentation extension."""


def instrument_sql(engine=None) -> None:
    """Enable SQLAlchemy tracing instrumentation.

    Args:
        engine: Optional SQLAlchemy engine instance. If provided, instruments
                only that engine; otherwise instruments all engines globally.
    """
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
