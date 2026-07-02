"""Opt-in SQLAlchemy instrumentation extension."""


def instrument_sql(engine=None) -> None:
    """Enable SQLAlchemy tracing instrumentation.

    Args:
        engine: Optional SQLAlchemy engine instance. If provided, instruments
                only that engine; otherwise instruments all engines globally.
    """
    from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

    if engine:
        SQLAlchemyInstrumentor().instrument(engine=engine)
    else:
        SQLAlchemyInstrumentor().instrument()
