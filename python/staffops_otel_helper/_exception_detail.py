"""Exception detail of HTTP, gRPC and framework spans (ADR-014 decision 4, contract task A37).

The helper's TracerProvider wraps the tracers of the HTTP and gRPC
instrumentations it turns on (:data:`_SCOPES`). Their spans keep status ERROR
without a description, and an ``exception`` event with ``exception.type``
only, at most one per exception object per span (gRPC aio records an
``AbortError`` twice: the interceptor and ``use_span``). Two roles (contract
design D15, amendment of 2026-11-19, coordinator rulings R1 and R2):

- **server** — FastAPI / ASGI SERVER spans and gRPC SERVER spans: one ERROR
  record on the D15 channel (``staffops_otel_helper.failure``), in the span's
  context, sampled or not, **only for a failed span** (R1):

  * HTTP: the response status is 5xx (seen through the patched
    ``set_status_code``, sampled or not), or an exception escaped the span
    (Starlette's ``ServerErrorMiddleware`` then answers 500, or it escaped
    after the response started); an exception the application recorded and
    answered with 2xx / 4xx writes none;
  * gRPC: the explicit status code the application or contrib set
    (``rpc.grpc.status_code`` written after start) is one of the server-error
    codes ``UNKNOWN``, ``DEADLINE_EXCEEDED``, ``UNIMPLEMENTED``, ``INTERNAL``,
    ``UNAVAILABLE``, ``DATA_LOSS``; with no explicit code, an exception that
    escaped the handler (the client sees ``UNKNOWN``);

  never for a cancellation. Its ``error.type`` does not depend on sampling
  (R2, review N6): HTTP the 5xx status when known, else the span's
  ``error.type``, else the exception's type (D15 ``error_type``); gRPC the D16
  code name (``UNKNOWN`` without a code). A recording span writes the
  record at ``end()``; an unsampled ASGI span (never ended) when the escaping
  exception is seen.
- **client** — requests, httpx, gRPC CLIENT spans and the other ASGI spans
  (INTERNAL, ``send`` / ``receive``): type-only event, no description, no
  record (the server explains the failure).

An exception that escapes a span is seen in ``trace.use_span``'s exception
handler, which asks the span ``is_recording()``: the server span's
``is_recording()`` (answer unchanged) notes the exception being handled when
its caller is ``use_span``'s generator (checked by code object; contrib records
nothing on an unsampled span). If that hook is unavailable, one warning per
process names the effect. A foreign global TracerProvider gets none of this
(as the A28 URL redaction).
"""

from __future__ import annotations

import functools
import logging
import sys
import threading
from contextlib import contextmanager
from typing import Any, Iterator

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import Span, SpanKind, Status, StatusCode

from staffops_otel_helper._diag import warn_once_per_process
from staffops_otel_helper._failure import emit_exception_record, error_type, exception_attributes, is_cancellation
from staffops_otel_helper._grpc_status import grpc_error_type

_log = logging.getLogger("staffops_otel_helper._exception_detail")
_diag = logging.getLogger("staffops_otel_helper.tracing")

#: Instrumentation scopes whose spans follow the rule -> their kind ("http", "grpc", or None for clients).
_SCOPES = {
    "opentelemetry.instrumentation.fastapi": "http",
    "opentelemetry.instrumentation.asgi": "http",
    "opentelemetry.instrumentation.grpc": "grpc",
    "opentelemetry.instrumentation.requests": None,
    "opentelemetry.instrumentation.httpx": None,
}
#: Span and start attributes copied to a server record (never URL or query keys).
RECORD_KEYS = ("error.type", "http.request.method", "http.route", "http.response.status_code", "http.method",
               "http.status_code", "rpc.system", "rpc.system.name", "rpc.service", "rpc.method",
               "rpc.grpc.status_code", "rpc.response.status_code")
_RECORDED = frozenset(RECORD_KEYS)
#: The gRPC codes the semantic conventions count as server errors (D16): a server record only for these.
GRPC_SERVER_ERROR_CODES = frozenset({2, 4, 12, 13, 14, 15})
_GRPC_CODE = "rpc.grpc.status_code"
_HTTP_STATUS_KEYS = ("http.response.status_code", "http.status_code")
#: Instance attribute of an HTTP server span: called with the response status by the patched
#: ``opentelemetry.instrumentation.asgi.set_status_code`` (also for an unsampled span).
_STATUS_HOOK = "_staffops_a37_response_status"
#: Distinct exception objects remembered per span (the dedupe of the type-only event).
_MAX_SEEN = 16


def _use_span_code() -> Any:
    wrapped = getattr(trace.use_span, "__wrapped__", None)
    return getattr(wrapped, "__code__", None)


#: The code object of ``trace.use_span``'s generator (``_agnosticcontextmanager`` keeps ``__wrapped__``).
_USE_SPAN_CODE = _use_span_code()
_HOOK_WARNING = ("[OtelHelper] Unsampled HTTP and gRPC server spans write no exception record, and an exception "
                 "escaping a gRPC handler without a status code is not seen: the opentelemetry-api use_span hook "
                 "is unavailable (%s).")


def check_use_span_hook() -> bool:
    """Self-test of the ``use_span`` hook (once per call); warns once per process when it does not work."""
    if _USE_SPAN_CODE is None:
        warn_once_per_process(_diag, "a37:use-span-hook", _HOOK_WARNING, "use_span has no __wrapped__ code")
        return False
    seen: list = []
    probe = trace.NonRecordingSpan(trace.INVALID_SPAN_CONTEXT)

    def is_recording() -> bool:
        if sys._getframe(1).f_code is _USE_SPAN_CODE:
            seen.append(sys.exc_info()[1])
        return False

    probe.is_recording = is_recording  # type: ignore[method-assign]
    try:
        with trace.use_span(probe, end_on_exit=False):
            raise ValueError("probe")
    except ValueError:
        pass
    except Exception:  # noqa: BLE001 - another use_span shape
        seen.clear()
    if not seen:
        warn_once_per_process(_diag, "a37:use-span-hook", _HOOK_WARNING, "self-test did not arm")
        return False
    return True


def patch_asgi_status() -> bool:
    """Wrap ``opentelemetry.instrumentation.asgi.set_status_code`` once per process (A37 review N1).

    ``otel_send`` calls this module-level function with the response status
    for sampled and unsampled server spans alike (contrib 0.63b0–0.66b2), while
    ``_set_status`` writes nothing on an unsampled span: the wrapper hands the
    status to the helper's patched span first. Without contrib asgi: nothing.
    """
    try:
        import opentelemetry.instrumentation.asgi as asgi
    except Exception:  # noqa: BLE001 - asgi (FastAPI) not installed
        return False
    original = getattr(asgi, "set_status_code", None)
    if not callable(original):
        warn_once_per_process(_diag, "a37:asgi-status",
                              "[OtelHelper] Unsampled FastAPI / ASGI server spans that answer 5xx without an "
                              "escaping exception write no exception record: contrib asgi has no set_status_code.")
        return False
    if getattr(original, "_staffops_a37", False):
        return True

    @functools.wraps(original)
    def set_status_code(span: Any, status_code: Any, *args: Any, **kwargs: Any) -> Any:
        hook = getattr(span, _STATUS_HOOK, None)
        if callable(hook):
            try:
                hook(status_code)
            except Exception as internal:  # noqa: BLE001 - never raise into otel_send
                _internal_failure("see the response status", internal)
        return original(span, status_code, *args, **kwargs)

    set_status_code._staffops_a37 = True  # type: ignore[attr-defined]
    asgi.set_status_code = set_status_code
    return True


def scope_rule(name: Any) -> str | None | bool:
    """``"http"`` / ``"grpc"`` (server records), ``None`` (client rule) or ``False`` (not covered)."""
    if not isinstance(name, str):
        return False
    if name in _SCOPES:
        return _SCOPES[name]
    if name.startswith("opentelemetry.instrumentation.grpc."):
        return "grpc"
    return False


def _is_grpc_abort(exc: BaseException) -> bool:
    """``grpc.aio.AbortError``, or the bare ``Exception()`` of the sync ``context.abort``: no details of their own."""
    if type(exc) is Exception and not exc.args:  # noqa: E721 - exactly the bare class
        return True
    return type(exc).__name__ == "AbortError" and type(exc).__module__.startswith("grpc")


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _internal_failure(what: str, internal: BaseException) -> None:
    warn_once_per_process(_diag, f"a37:{what}", "[OtelHelper] Could not %s on an HTTP or gRPC span (%s).",
                          what, type(internal).__name__)


def apply(span: Span, server: str | None, start_attributes: Any = None) -> Span:
    """Patch ``span`` (recording or not) with the A37 rule; ``server`` (``"http"``/``"grpc"``) selects the record.

    Never raises.
    """
    known: dict = {}
    try:
        for key, value in dict(start_attributes or {}).items():
            # rpc.grpc.status_code at start is contrib's placeholder 0, not the call's code.
            if key in _RECORDED and key != _GRPC_CODE and value is not None and not isinstance(value, (list, tuple)):
                known[key] = value
    except Exception:  # noqa: BLE001 - a foreign mapping
        known = {}
    recording = span.is_recording()
    original_status = span.set_status
    original_attribute = getattr(span, "set_attribute", None)
    original_attributes = getattr(span, "set_attributes", None)
    original_add_event = getattr(span, "add_event", None)
    original_end = getattr(span, "end", None)
    original_is_recording = span.is_recording
    # One request is handled on one thread, but end() and a late exception hook may race: guard the write.
    lock = threading.Lock()
    state: dict = {"seen": [], "failure": None, "dropped": None, "errored": False, "escaped": False,
                   "cancelled": False, "written": False}

    def keep(key: Any, value: Any) -> None:
        if key in _RECORDED and value is not None and not isinstance(value, (list, tuple)):
            known[key] = value

    def failed() -> bool:
        if server == "grpc":
            code = _int(known.get(_GRPC_CODE))
            if code is not None and code != 0:
                return code in GRPC_SERVER_ERROR_CODES
            return state["escaped"] or (state["errored"] and code is None)
        # HTTP (review L3): a known response status decides; without one only an escaping exception counts
        # (sampled and unsampled agree: the patched set_status_code reports the status for both).
        status = http_status()
        if status is not None:
            return status >= 500 or state["escaped"]
        return state["escaped"]

    def http_status() -> int | None:
        return next((_int(known.get(k)) for k in _HTTP_STATUS_KEYS if _int(known.get(k)) is not None), None)

    def record_error_type(exc: BaseException | None) -> str:
        if server == "grpc":
            code = _int(known.get(_GRPC_CODE))
            return grpc_error_type(code) if code else "UNKNOWN"
        # HTTP (review N6): the 5xx status when known, else the span's error.type, else the exception's type.
        status = http_status()
        if status is not None and status >= 500:
            return str(status)
        if isinstance(known.get("error.type"), str) and known["error.type"]:
            return known["error.type"]
        return error_type(exc) if exc is not None else "_OTHER"

    def write() -> None:
        with lock:
            if state["written"] or not failed():
                return
            exc = state["failure"]
            if exc is None and (state["cancelled"] or not state["dropped"]):
                return
            state["written"] = True
        try:
            if exc is not None:
                message, attributes = exception_attributes(exc, error_type(exc))
                if state["dropped"] and (not message or _is_grpc_abort(exc)):
                    message = attributes["exception.message"] = state["dropped"]
            else:
                message = state["dropped"]
                attributes = {"exception.message": message}
            extra = dict(known)
            # Review N2: only when no response started (no status seen) is the answer known to be 500.
            if server == "http" and not any(k in extra for k in _HTTP_STATUS_KEYS):
                extra["http.response.status_code"] = 500
            extra["error.type"] = record_error_type(exc)
            attributes.update(extra)
            emit_exception_record(span, message, attributes)
        except Exception as internal:  # noqa: BLE001 - never raise into the instrumented call
            _internal_failure("write an exception record", internal)
        finally:
            state["failure"] = None

    def note(exception: Any, escaped: bool) -> None:
        if not server or not isinstance(exception, BaseException):
            return
        if is_cancellation(exception):
            state["cancelled"] = True
            return
        if escaped:
            state["escaped"] = True
        if state["failure"] is None:
            state["failure"] = exception
        if escaped and not recording:  # an unsampled ASGI span is never ended: decide now
            write()

    def record_exception(exception: BaseException, attributes: Any = None, timestamp: int | None = None,
                         escaped: bool = False) -> None:
        try:
            if any(e is exception for e in state["seen"]):
                return  # the same object again on this span (gRPC aio AbortError, use_span after contrib)
            if len(state["seen"]) < _MAX_SEEN:
                state["seen"].append(exception)
            note(exception, False)
            if original_add_event is not None and recording:
                original_add_event("exception", {"exception.type": error_type(exception)}, timestamp=timestamp)
        except Exception as internal:  # noqa: BLE001 - the event is dropped, never the raw text
            _internal_failure("record an exception event", internal)

    def set_status(status: Status | StatusCode, description: str | None = None) -> None:
        code = status.status_code if isinstance(status, Status) else status
        if code is not StatusCode.ERROR:
            original_status(status, description)
            return
        # contrib gRPC calls this on unsampled spans too (its servicer context does not check is_recording).
        state["errored"] = True
        text = status.description if isinstance(status, Status) else description
        if server and state["dropped"] is None and isinstance(text, str) and text:
            state["dropped"] = text
        original_status(Status(StatusCode.ERROR))

    def set_attribute(key: str, value: Any) -> Any:
        keep(key, value)
        return original_attribute(key, value)

    def set_attributes(attributes: Any) -> Any:
        try:
            for key, value in dict(attributes).items():
                keep(key, value)
        except Exception:  # noqa: BLE001 - a foreign mapping: passed on as given
            pass
        return original_attributes(attributes)

    def response_status(status_code: Any) -> None:
        try:
            status = int(status_code)
        except (TypeError, ValueError):
            return
        known["http.response.status_code"] = status
        if not recording and status >= 500:  # an unsampled ASGI span is never ended: decide now
            write()

    def end(*args: Any, **kwargs: Any) -> None:
        if server:
            write()
        state["seen"].clear()
        state["failure"] = None
        original_end(*args, **kwargs)

    def is_recording() -> bool:
        # Asked from use_span's exception handler: the exception escaping the server span.
        try:
            if _USE_SPAN_CODE is not None and sys._getframe(1).f_code is _USE_SPAN_CODE:
                exc = sys.exc_info()[1]
                if isinstance(exc, Exception):
                    note(exc, True)
        except Exception as internal:  # noqa: BLE001 - never raise into use_span
            _internal_failure("see an escaping exception", internal)
        return original_is_recording()

    try:
        span.set_status = set_status  # type: ignore[method-assign]
        span.record_exception = record_exception  # type: ignore[method-assign]
        if server and original_attribute is not None:
            span.set_attribute = set_attribute  # type: ignore[method-assign]
            if original_attributes is not None:
                span.set_attributes = set_attributes  # type: ignore[method-assign]
        if server:
            span.is_recording = is_recording  # type: ignore[method-assign]
        if server == "http":
            setattr(span, _STATUS_HOOK, response_status)
        if original_end is not None:
            span.end = end  # type: ignore[method-assign]
    except AttributeError:  # a span type with __slots__: left as it is
        pass
    return span


class _ExceptionDetailTracer(trace.Tracer):
    """A covered instrumentation's tracer: its spans follow :func:`apply`."""

    def __init__(self, tracer: trace.Tracer, server: str | None) -> None:
        self._tracer = tracer
        self._server = server

    def __getattr__(self, name: str) -> Any:
        if name == "_tracer":  # not set yet (copy, pickle): no recursion
            raise AttributeError(name)
        return getattr(self._tracer, name)

    def start_span(  # type: ignore[override]
        self,
        name: str,
        context: Any = None,
        kind: SpanKind = SpanKind.INTERNAL,
        attributes: Any = None,
        links: Any = None,
        start_time: int | None = None,
        record_exception: bool = True,
        set_status_on_exception: bool = True,
    ) -> Span:
        span = self._tracer.start_span(name, context=context, kind=kind, attributes=attributes, links=links,
                                       start_time=start_time, record_exception=record_exception,
                                       set_status_on_exception=set_status_on_exception)
        return apply(span, self._server if kind is SpanKind.SERVER else None, attributes)

    @contextmanager
    def start_as_current_span(  # type: ignore[override]
        self,
        name: str,
        context: Any = None,
        kind: SpanKind = SpanKind.INTERNAL,
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


class HelperTracerProvider(TracerProvider):
    """The helper's SDK TracerProvider: the tracers of the covered HTTP and gRPC scopes apply A37."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        check_use_span_hook()
        patch_asgi_status()

    def get_tracer(self, *args: Any, **kwargs: Any) -> trace.Tracer:  # type: ignore[override]
        tracer = super().get_tracer(*args, **kwargs)
        name = args[0] if args else kwargs.get("instrumenting_module_name")
        rule = scope_rule(name)
        return tracer if rule is False else _ExceptionDetailTracer(tracer, rule)
