"""gRPC span ``error.type`` (contract D16, R12, task A46).

contrib grpc writes the integer ``rpc.grpc.status_code`` and the ERROR status
on failed calls but no ``error.type``. The helper passes the gRPC
instrumentors a TracerProvider wrapper (the A25 pattern, ``_db_status``)
whose spans, at ``end()``, write ``error.type`` when the span is ERROR and has
none yet:

- the canonical name of the code (``FAILED_PRECONDITION``), from a table built
  from ``grpc.StatusCode``, for codes 1–16;
- ``_OTHER`` for code 0 or no code on an ERROR span (a failure that is not an
  ``RpcError``: contrib starts every span with code 0) and for a code above 16.

Client and server spans use the same rule: contrib sets ERROR on a server span
only for the semconv server codes (``UNKNOWN``, ``DEADLINE_EXCEEDED``,
``UNIMPLEMENTED``, ``INTERNAL``, ``UNAVAILABLE``, ``DATA_LOSS``), so a server
``NOT_FOUND`` stays Unset and gets nothing. A value already on the span is
never overwritten. Known gap: a server handler that raises leaves the span
Unset (code 0) while the peer receives ``UNKNOWN``.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any, Iterator

from opentelemetry import trace
from opentelemetry.trace import Span, StatusCode

from staffops_otel_helper._diag import warn_once_per_process

_log = logging.getLogger("staffops_otel_helper._grpc_status")

_NAMES: dict[int, str] | None = None


def _names() -> dict[int, str]:
    """``{1: "CANCELLED", ..., 16: "UNAUTHENTICATED"}`` from ``grpc.StatusCode`` (empty without grpc)."""
    global _NAMES
    if _NAMES is None:
        try:
            import grpc

            _NAMES = {sc.value[0]: sc.name for sc in grpc.StatusCode if 1 <= sc.value[0] <= 16}
        except Exception:  # noqa: BLE001 - grpc missing or another layout: every code is _OTHER
            _NAMES = {}
    return _NAMES


def grpc_error_type(code: Any) -> str:
    """``error.type`` of a failed gRPC span whose ``rpc.grpc.status_code`` is ``code``."""
    if isinstance(code, int) and not isinstance(code, bool):
        return _names().get(code, "_OTHER")
    return "_OTHER"


def _set_error_type(span: Span) -> None:
    if not span.is_recording():
        return
    status = getattr(span, "status", None)
    if status is None or status.status_code is not StatusCode.ERROR:
        return
    attributes = getattr(span, "attributes", None) or {}
    if "error.type" in attributes:
        return
    span.set_attribute("error.type", grpc_error_type(attributes.get("rpc.grpc.status_code")))


def _set_error_type_at_end(span: Span) -> Span:
    original_end = getattr(span, "end", None)
    if original_end is None:
        return span

    def end(*args: Any, **kwargs: Any) -> None:
        try:
            _set_error_type(span)
        except Exception:  # noqa: BLE001 - never raise into the instrumented call
            _log.debug("grpc error.type not set", exc_info=False)
        original_end(*args, **kwargs)

    try:
        span.end = end  # type: ignore[method-assign]
    except AttributeError:  # a span type with __slots__: left as it is
        pass
    return span


class _GrpcErrorTypeTracer(trace.Tracer):
    """Tracer for contrib grpc: its spans write ``error.type`` at ``end()``."""

    def __init__(self, tracer: trace.Tracer) -> None:
        self._tracer = tracer

    def start_span(self, *args: Any, **kwargs: Any) -> Span:
        return _set_error_type_at_end(self._tracer.start_span(*args, **kwargs))

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


class _GrpcErrorTypeTracerProvider(trace.TracerProvider):
    """Wraps the global TracerProvider (resolved lazily by the API proxy when it is not set yet)."""

    def __init__(self, provider: trace.TracerProvider) -> None:
        self._provider = provider

    def get_tracer(self, *args: Any, **kwargs: Any) -> trace.Tracer:
        return _GrpcErrorTypeTracer(self._provider.get_tracer(*args, **kwargs))


def grpc_instrument_kwargs() -> dict:
    """``instrument()`` kwargs for the four contrib gRPC instrumentors (A46)."""
    return {"tracer_provider": _GrpcErrorTypeTracerProvider(trace.get_tracer_provider())}


_BOUND_MARK = "_staffops_grpc_wrapper_fn"


def bind_sync_client(instrumentor: Any, tracer_provider: trace.TracerProvider) -> bool:
    """Make contrib's sync ``GrpcInstrumentorClient`` use ``tracer_provider``; call before ``instrument()``.

    contrib 0.63b0–0.66b2 ignores the ``tracer_provider`` given to
    ``instrument()`` for sync channels: its ``wrapper_fn`` reads it from the
    ``grpc.insecure_channel()`` / ``secure_channel()`` call's kwargs instead,
    so sync client spans come from the global provider. The helper sets an
    instance ``wrapper_fn`` that builds the same interceptor through contrib's
    public ``client_interceptor`` / ``intercept_channel`` with the wrapped
    provider, reading the instrumentor's filter and hooks as contrib does.
    When contrib's layout differs, nothing is bound: warned once, sync client
    spans keep contrib's attributes (no ``error.type``). Returns whether bound.
    """
    try:
        from opentelemetry.instrumentation.grpc import client_interceptor
        from opentelemetry.instrumentation.grpc.grpcext import intercept_channel

        contrib_wrapper_fn = getattr(type(instrumentor), "wrapper_fn", None)
        if not callable(contrib_wrapper_fn) or not hasattr(instrumentor, "_filter"):
            raise AttributeError("GrpcInstrumentorClient.wrapper_fn / _filter")
    except Exception as e:  # noqa: BLE001 - another contrib layout
        warn_once_per_process(
            _log, "grpc:sync-client-not-bound",
            "[OtelHelper] gRPC sync client spans carry no error.type: the installed "
            "opentelemetry-instrumentation-grpc has another layout (%s).", type(e).__name__,
        )
        return False

    def wrapper_fn(original_func: Any, instance: Any, args: Any, kwargs: Any) -> Any:
        channel = original_func(*args, **kwargs)
        return intercept_channel(channel, client_interceptor(
            tracer_provider=tracer_provider,
            filter_=instrumentor._filter,  # noqa: SLF001 - contrib's own fields, read as wrapper_fn does
            request_hook=getattr(instrumentor, "_request_hook", None),
            response_hook=getattr(instrumentor, "_response_hook", None),
        ))

    instrumentor.wrapper_fn = wrapper_fn
    setattr(instrumentor, _BOUND_MARK, True)
    return True


def unbind_sync_client(instrumentor: Any) -> None:
    """Undo :func:`bind_sync_client` (after ``uninstrument()``): contrib's own ``wrapper_fn`` again."""
    if getattr(instrumentor, _BOUND_MARK, False):
        try:
            del instrumentor.wrapper_fn
            delattr(instrumentor, _BOUND_MARK)
        except AttributeError:
            pass
