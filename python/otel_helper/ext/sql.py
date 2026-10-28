"""Opt-in SQLAlchemy instrumentation extension."""

from otel_helper._semconv import ensure_semconv_default
from otel_helper._support import missing_dependency_hint as _missing_hint
from otel_helper._support import warn_if_unsupported


def instrument_sql(engine=None) -> bool:
    """Enable SQLAlchemy tracing instrumentation.

    Args:
        engine: Optional SQLAlchemy engine instance. If provided, instruments
                only that engine; otherwise instruments all engines globally.

    Returns True when the instrumentor is active. False (with a warning on the
    ``otel_helper`` logger) when it refuses the installed SQLAlchemy version —
    contrib 0.63b0-0.66b1 support ``< 2.1.0`` only (P18).
    """
    ensure_semconv_default()  # before the instrumentor reads it (design D12)
    try:
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
    except ImportError as e:
        hint = _missing_hint(e, "sql", "SQL", library="sqlalchemy",
                            module="opentelemetry.instrumentation.sqlalchemy")
        raise ImportError(hint) from e

    instrumentor = SQLAlchemyInstrumentor()
    if engine:
        instrumentor.instrument(engine=engine)
    else:
        instrumentor.instrument()
    warn_if_unsupported(instrumentor)
    return bool(instrumentor.is_instrumented_by_opentelemetry)
