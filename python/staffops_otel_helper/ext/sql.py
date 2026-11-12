"""Opt-in SQLAlchemy instrumentation extension."""

from __future__ import annotations

import logging

from opentelemetry import trace

from staffops_otel_helper._db_status import (  # noqa: F401  (moved to _db_status in A26; names kept for tests and instrumentation)
    _WRAPPED_MARK,
    _drop_error_description,
    _ErrorDescriptionDroppingTracer,
    _ErrorDescriptionDroppingTracerProvider,
    clear_wrapped,
    mark_wrapped,
    warn_foreign,
    wrapped_instrument_kwargs,
)
from staffops_otel_helper._semconv import ensure_semconv_default
from staffops_otel_helper._diag import warn_once_per_process
from staffops_otel_helper._support import missing_dependency_hint as _missing_hint
from staffops_otel_helper._support import warn_if_unsupported

_log = logging.getLogger("staffops_otel_helper.ext.sql")


def _attach_engine(engine) -> bool:
    """Trace ``engine`` when SQLAlchemy is already instrumented globally (2026-11-03, M1).

    The global instrumentation wraps ``create_engine``, so an engine created
    before ``setup_telemetry()`` is never traced, and ``instrument(engine=…)`` is
    a no-op once the instrumentor is active. Builds the per-engine
    ``EngineTracer`` exactly as contrib's ``_instrument(engine=…)`` does — same
    constructor ``(tracer, engine, connections_usage)`` in 0.63b0
    (``engine.py:208-216``, ``__init__.py:270-278``) and 0.66b2
    (``engine.py:200-208``, ``__init__.py:267-275``) — with the wrapped tracer.
    An engine contrib already traces (listed in ``EngineTracer._remove_event_listener_params``)
    is left alone, so spans are never doubled. When that structure is not what
    these versions have, nothing is attached: warned, False.
    """
    try:
        from opentelemetry import metrics
        from opentelemetry.instrumentation.sqlalchemy.engine import EngineTracer
        from opentelemetry.instrumentation.sqlalchemy.version import __version__
        from opentelemetry.semconv.metrics import MetricInstruments

        params = EngineTracer._remove_event_listener_params
        if not isinstance(params, list):
            raise TypeError("EngineTracer._remove_event_listener_params is not a list")
        if any(ref() is engine for ref, *_ in params):
            return True
        try:  # the schema URL contrib uses; absent: none (span content unchanged)
            from opentelemetry.instrumentation._semconv import (
                _OpenTelemetryStabilitySignalType as _signal,
                _get_schema_url_for_signal_types,
            )
            schema_url = _get_schema_url_for_signal_types([_signal.DATABASE, _signal.HTTP])
        except Exception:  # noqa: BLE001 - private API, optional
            schema_url = None
        scope = "opentelemetry.instrumentation.sqlalchemy"
        tracer = trace.get_tracer(scope, __version__, wrapped_instrument_kwargs()["tracer_provider"], schema_url=schema_url)
        meter = metrics.get_meter(scope, __version__, schema_url=schema_url)
        usage = meter.create_up_down_counter(
            name=MetricInstruments.DB_CLIENT_CONNECTIONS_USAGE,
            unit="connections",
            description="The number of connections that are currently in state described by the state attribute.",
        )
        EngineTracer(tracer, engine, usage)
        return True
    except Exception as e:  # noqa: BLE001 - a contrib layout this helper does not know
        warn_once_per_process(
            _log, "sql:engine-not-attached",
            "[OtelHelper] instrument_sql(engine): SQLAlchemy is already instrumented globally and this "
            "engine (created before setup_telemetry()) could not be traced (%s); create engines after "
            "setup_telemetry() or call instrument_sql(engine) before it.", type(e).__name__,
        )
        return False


def instrument_sql(engine=None) -> bool:
    """Enable SQLAlchemy tracing instrumentation.

    Args:
        engine: Optional SQLAlchemy engine instance. If provided, instruments
                only that engine; otherwise instruments all engines globally.

    Returns True when the instrumentor is active. False (with a warning on the
    ``staffops_otel_helper`` logger) when it refuses the installed SQLAlchemy version —
    contrib 0.63b0-0.66b1 support ``< 2.1.0`` only (P18).

    A failed statement's span keeps the ERROR status code without its
    description, which is the database's error text (A25 R1).

    Already instrumented by ``setup_telemetry()`` (the deprecated ``SQL`` flag,
    same wrapper): True; with ``engine``, that engine is traced too when it was
    created before ``setup_telemetry()`` (``_attach_engine``; False and a warning
    if that cannot be done). Already instrumented by someone else
    without the wrapper: kept as it is and warned once — uninstrumenting would
    remove the event listeners of every engine created so far (contrib
    ``_uninstrument`` → ``EngineTracer.remove_all_event_listeners()``), so those
    engines would silently stop being traced.
    """
    ensure_semconv_default()  # before the instrumentor reads it (design D12)
    try:
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
    except ImportError as e:
        hint = _missing_hint(e, "sql", "SQL", library="sqlalchemy",
                            module="opentelemetry.instrumentation.sqlalchemy")
        raise ImportError(hint) from e

    instrumentor = SQLAlchemyInstrumentor()
    if instrumentor.is_instrumented_by_opentelemetry:
        if not getattr(instrumentor, _WRAPPED_MARK, False):
            warn_foreign(_log, "sql")
            return True
        return _attach_engine(engine) if engine else True
    clear_wrapped(instrumentor)  # a mark left by an earlier, since uninstrumented, helper call
    kwargs = wrapped_instrument_kwargs()
    if engine:
        instrumentor.instrument(engine=engine, **kwargs)
    else:
        instrumentor.instrument(**kwargs)
    if instrumentor.is_instrumented_by_opentelemetry:
        mark_wrapped(instrumentor)
    warn_if_unsupported(instrumentor)
    return bool(instrumentor.is_instrumented_by_opentelemetry)
