"""Opt-in SQLAlchemy instrumentation extension."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any, Iterator

from opentelemetry import trace
from opentelemetry.trace import Span, Status, StatusCode

from staffops_otel_helper._semconv import ensure_semconv_default
from staffops_otel_helper._diag import warn_once_per_process
from staffops_otel_helper._support import missing_dependency_hint as _missing_hint
from staffops_otel_helper._support import warn_if_unsupported


def _drop_error_description(span: Span) -> Span:
    """Make ``span.set_status`` keep the ERROR code and drop its description (A25 R1).

    contrib sqlalchemy sets ``Status(ERROR, str(exc))`` (``engine.py``
    ``_handle_error``), and ``trace.use_span`` sets ``"<type>: <exc>"`` on the
    ``connect`` span: database error text can quote a statement value. Go
    (XSAM otelsql) and Node (``@staffops/otel-helper-sql``) leave the
    description empty too. An exception event, when one is recorded, is kept
    (semconv ``exception.message``).
    """
    original = span.set_status

    def set_status(status: Status | StatusCode, description: str | None = None) -> None:
        code = status.status_code if isinstance(status, Status) else status
        if code is StatusCode.ERROR:
            original(Status(StatusCode.ERROR))
        else:
            original(status, description)

    try:
        span.set_status = set_status  # type: ignore[method-assign]
    except AttributeError:  # a span type with __slots__: left as it is
        pass
    return span


class _ErrorDescriptionDroppingTracer(trace.Tracer):
    """Tracer for the SQLAlchemy instrumentation: its spans drop the ERROR description."""

    def __init__(self, tracer: trace.Tracer) -> None:
        self._tracer = tracer

    def start_span(self, *args: Any, **kwargs: Any) -> Span:
        return _drop_error_description(self._tracer.start_span(*args, **kwargs))

    @contextmanager
    def start_as_current_span(  # type: ignore[override]
        self,
        name: str,
        context: Any = None,
        kind: trace.SpanKind = trace.SpanKind.INTERNAL,
        attributes: Any = None,
        links: Any = None,
        start_time: int | None = None,
        record_exception: bool = True,
        set_status_on_exception: bool = True,
        end_on_exit: bool = True,
    ) -> Iterator[Span]:
        span = self.start_span(name=name, context=context, kind=kind, attributes=attributes, links=links,
                               start_time=start_time, record_exception=record_exception,
                               set_status_on_exception=set_status_on_exception)
        with trace.use_span(span, end_on_exit=end_on_exit, record_exception=record_exception,
                            set_status_on_exception=set_status_on_exception) as current:
            yield current


class _ErrorDescriptionDroppingTracerProvider(trace.TracerProvider):
    """Wraps the global TracerProvider (resolved lazily by the API proxy when it is not set yet)."""

    def __init__(self, provider: trace.TracerProvider) -> None:
        self._provider = provider

    def get_tracer(self, *args: Any, **kwargs: Any) -> trace.Tracer:
        return _ErrorDescriptionDroppingTracer(self._provider.get_tracer(*args, **kwargs))


_log = logging.getLogger("staffops_otel_helper.ext.sql")
# Set on the SQLAlchemyInstrumentor singleton when the helper instrumented it with the wrapper.
_WRAPPED_MARK = "_staffops_error_status_dropped"


def wrapped_instrument_kwargs() -> dict:
    """``instrument()`` kwargs every helper path uses for contrib sqlalchemy (A25 R1).

    Shared by ``instrument_sql()`` and the deprecated ``SQL`` flag of
    ``setup_telemetry()`` (``instrumentation.configure_instrumentations``), so
    neither path instruments SQLAlchemy without the status wrapper.
    """
    return {"tracer_provider": _ErrorDescriptionDroppingTracerProvider(trace.get_tracer_provider())}


def mark_wrapped(instrumentor) -> None:
    """Record that ``instrumentor`` was instrumented with :func:`wrapped_instrument_kwargs`."""
    setattr(instrumentor, _WRAPPED_MARK, True)


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
            warn_once_per_process(
                _log, "sql:instrumented-without-wrapper",
                "[OtelHelper] SQLAlchemy is already instrumented without the helper's tracer: failed "
                "statements may carry the database error text in their span status. Call "
                "instrument_sql() (or setup_telemetry()) before instrumenting SQLAlchemy elsewhere.",
            )
            return True
        return _attach_engine(engine) if engine else True
    kwargs = wrapped_instrument_kwargs()
    if engine:
        instrumentor.instrument(engine=engine, **kwargs)
    else:
        instrumentor.instrument(**kwargs)
    if instrumentor.is_instrumented_by_opentelemetry:
        mark_wrapped(instrumentor)
    warn_if_unsupported(instrumentor)
    return bool(instrumentor.is_instrumented_by_opentelemetry)
