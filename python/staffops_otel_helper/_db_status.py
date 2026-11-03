"""DB client span status rule shared by ``ext.sql`` and ``ext.redis`` (A25 R1, A26).

A failed database client span keeps the ERROR status code and drops its
description, in every language (contract design D1 "DB error status"). The
helper passes contrib instrumentors a TracerProvider wrapper scoped to their
own tracer; no SDK hook (a span processor cannot change the status).

Redis spans (``redis=True``, A26 fix rounds of 2026-11-03) also get:

- ``db.response.status_code`` and ``error.type``: a **known** reply code
  (:data:`REPLY_CODES`, the same allowlist in Go and Node), else
  ``error.type=_OTHER``. Resolved, in order, from the exception's
  ``status_code`` (redis-py 8.x sets it for the codes it maps, ``ERR``
  included, since it strips them from the message), the message's leading
  token (codes redis-py does not map stay in the text: ``WRONGTYPE``; an
  ERR-stripped text such as ``EXEC without MULTI`` or ``DUMP payload ...``
  starts with a word that is not a known code, so it is never taken for one),
  the exception class mapped back from ``BaseParser.EXCEPTION_CLASSES``
  (unambiguous ``ResponseError`` subclasses: ``NoPermissionError`` →
  ``NOPERM``), and — redis-py 5.x/7.x only, which has no ``status_code`` — a
  plain ``ResponseError`` without a known leading code reads as ``ERR``
  (redis-py removed it). A Lua
  ``error_reply`` whose first word is data is ``_OTHER`` on redis-py 8.x and
  ``ERR`` on older ones (indistinguishable there; never the word itself).
- a sanitized ``exception`` event for a pipeline error: redis-py's
  ``annotate_exception`` (``asyncio``: the same, with the reply as a tuple
  repr) rewrites the message to ``Command # N (<command and its arguments>) of
  pipeline caused error: ...`` on the client, which would put keys and values
  in ``exception.message`` and ``exception.stacktrace``. The helper keeps the
  command name and one ``?`` per argument (an approximate count: a value with
  spaces adds ``?``; the value itself is never kept), and builds the
  stacktrace with ``traceback`` from the same chain with every message
  sanitized. The server text after ``caused error:`` stays (the A25 residual).
  Any internal failure records a minimal sanitized event; the wrapper never
  raises into the instrumented call.
"""

from __future__ import annotations

import ast
import logging
import re
import traceback
from contextlib import contextmanager
from typing import Any, Iterator

from opentelemetry import trace
from opentelemetry.trace import Span, Status, StatusCode

from staffops_otel_helper._diag import warn_once_per_process

# Set on a contrib instrumentor singleton when the helper instrumented it with the wrapper.
_WRAPPED_MARK = "_staffops_error_status_dropped"

#: Known Redis reply codes (contract D1 "DB error status", A26 final round): the
#: error codes Redis 7.4 sends (``src/*.c``, e.g. server.c:1875 NOSCRIPT,
#: cluster.c:1186 CROSSSLOT, cluster.c:1205 MOVED/ASK) plus redis-py's
#: ``BaseParser.EXCEPTION_CLASSES`` keys. The same set in Go and Node.
REPLY_CODES = frozenset({
    "ASK", "BUSY", "BUSYGROUP", "BUSYKEY", "CLUSTERDOWN", "CROSSSLOT", "DENIED", "ERR", "EXECABORT", "INPROG",
    "INVALIDOBJ", "IOERR", "LOADING", "MASTERDOWN", "MISCONF", "MOVED", "NOAUTH", "NOGOODSLAVE", "NOGROUP",
    "NOMASTERLINK", "NOPERM", "NOPROTO", "NOQUORUM", "NOREPLICAS", "NOSCRIPT", "NOTBUSY", "OOM", "READONLY",
    "TRYAGAIN", "UNBLOCKED", "UNKILLABLE", "WRONGPASS", "WRONGTYPE",
})

_LEADING_TOKEN = re.compile(r"^([A-Z][A-Z0-9_]{0,31}) ")
# redis-py Pipeline.annotate_exception (sync and asyncio).
_PIPELINE_ERROR = re.compile(r"^Command # (\d+) \((.*)\) of pipeline caused error: (.*)$", re.S)
# Fallback when the format changes: anything between "(" and ") of pipeline" goes.
_PIPELINE_ANY = re.compile(r"\(.*\) of pipeline", re.S)
_LITERAL_MAX = 4096

_FOREIGN = {
    "sql": ("SQLAlchemy", "failed statements may carry the database error text in their span status",
            "instrument_sql()"),
    "redis": ("Redis", "failed commands may carry the server error text in their span status",
              "instrument_redis()"),
}

_log = logging.getLogger("staffops_otel_helper._db_status")


def warn_foreign(log: logging.Logger, kind: str) -> None:
    """Warn once that ``kind`` (``sql`` / ``redis``) was instrumented by other code without the wrapper."""
    label, consequence, call = _FOREIGN[kind]
    warn_once_per_process(
        log, f"{kind}:instrumented-without-wrapper",
        f"[OtelHelper] {label} is already instrumented without the helper's tracer: {consequence}. "
        f"Call {call} (or setup_telemetry()) before instrumenting {label} elsewhere.",
    )


def sanitize_pipeline_message(message: str) -> str:
    """``Command # 2 (LPUSH k v) of pipeline ...`` → ``Command # 2 (LPUSH ? ?) of pipeline ...``; others unchanged.

    The ``?`` count is approximate (the arguments are split on spaces, so a
    value with spaces adds some); the values themselves are never kept. Any
    other text with ``of pipeline`` loses everything between ``(`` and
    ``) of pipeline``.
    """
    m = _PIPELINE_ERROR.match(message)
    if m is None:
        return _PIPELINE_ANY.sub("() of pipeline", message) if "of pipeline" in message else message
    tokens = m.group(2).split(" ")
    name = re.sub(r"[a-z]+", lambda x: x.group(0).upper(), tokens[0])
    return f"Command # {m.group(1)} ({name}{' ?' * (len(tokens) - 1)}) of pipeline caused error: {m.group(3)}"


def _server_text(message: str) -> str:
    """The server's reply text; redis.asyncio annotates with ``exception.args`` (a tuple repr)."""
    m = _PIPELINE_ERROR.match(message)
    if m is None:
        return message
    tail = m.group(3)
    if tail.startswith("(") and len(tail) < _LITERAL_MAX:
        try:
            args = ast.literal_eval(tail)
        except Exception:  # noqa: BLE001 - not a literal: keep the text
            return tail
        if isinstance(args, tuple) and args and isinstance(args[0], str):
            return args[0]
    return tail


def _message(exception: BaseException) -> str:
    """``args[0]`` when it is a str (redis-py's message), else ``str(exception)``."""
    args = getattr(exception, "args", ())
    return args[0] if args and isinstance(args[0], str) else str(exception)


def _leading_code(text: str) -> str | None:
    m = _LEADING_TOKEN.match(text)
    return m.group(1) if m else None


_CLASS_CODES: dict[type, str] | None = None


def _class_codes() -> dict[type, str]:
    """redis-py exception class → its reply code, from ``BaseParser.EXCEPTION_CLASSES``, when unambiguous.

    Only ``ResponseError`` subclasses (``AuthenticationError`` is a
    ``ConnectionError``, raised for socket failures too, and maps to several
    codes). Empty when redis-py is missing or the table changed shape.
    """
    global _CLASS_CODES
    if _CLASS_CODES is not None:
        return _CLASS_CODES
    found: dict[type, set[str]] = {}
    try:
        from redis._parsers.base import BaseParser
        from redis.exceptions import ResponseError

        for code, entry in BaseParser.EXCEPTION_CLASSES.items():
            classes = entry.values() if isinstance(entry, dict) else [entry]
            for cls in classes:
                if isinstance(cls, type) and issubclass(cls, ResponseError) and cls is not ResponseError:
                    found.setdefault(cls, set()).add(code)
    except Exception:  # noqa: BLE001 - another redis-py layout: no class mapping
        found = {}
    _CLASS_CODES = {cls: next(iter(codes)) for cls, codes in found.items() if len(codes) == 1}
    return _CLASS_CODES


def redis_reply_code(exception: BaseException | None, text: str) -> str | None:
    """The known reply code of a failed Redis call, or None (``_OTHER``); see the module docstring."""
    if exception is not None:
        status_code = getattr(exception, "status_code", None)
        if isinstance(status_code, str):
            return status_code if status_code in REPLY_CODES else None
    token = _leading_code(text)
    if token is not None and token in REPLY_CODES:
        return token
    if exception is None:
        return None
    code = _class_codes().get(type(exception))
    if code is not None:
        return code
    if hasattr(exception, "status_code"):  # redis-py 8.x: an unmapped code stays in the text
        return None
    try:
        from redis.exceptions import ResponseError
    except Exception:  # noqa: BLE001
        return None
    # redis-py < 8 strips ERR and leaves no trace of it: a plain ResponseError
    # without a known leading code was an ERR reply (or a Lua word, then ERR too).
    return "ERR" if type(exception) is ResponseError else None


def _qualified(exc_type: type) -> str:
    module = exc_type.__module__
    return exc_type.__qualname__ if module in ("builtins", "__main__") else f"{module}.{exc_type.__qualname__}"


def _sanitized_stacktrace(exception: BaseException) -> str:
    """``traceback`` format of the chain with every exception's message sanitized."""
    te = traceback.TracebackException.from_exception(exception)
    seen: set[int] = set()
    stack = [te]
    while stack:
        current = stack.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        current._str = sanitize_pipeline_message(current._str)  # noqa: SLF001 - the formatted message
        stack.extend([current.__cause__, current.__context__])
    return "".join(te.format())


def _minimal_event(exception: BaseException) -> dict:
    return {"exception.type": _qualified(type(exception)),
            "exception.message": "(the message could not be sanitized)",
            "exception.stacktrace": ""}


def _drop_error_description(span: Span, redis: bool = False) -> Span:
    """Make ``span.set_status`` keep the ERROR code and drop its description (A25 R1, A26).

    contrib sqlalchemy sets ``Status(ERROR, str(exc))`` (``engine.py``
    ``_handle_error``), and ``trace.use_span`` sets ``"<type>: <exc>"`` on the
    ``connect`` span and on every contrib redis span (``start_as_current_span``):
    database error text can quote a statement value, Redis errors a command
    argument (``unknown command 'X', with args beginning with: '<arg>'``). Go
    and Node leave the description empty too. An exception event, when one is
    recorded, is kept (semconv ``exception.message``; the A25 residual), and
    for Redis sanitized as described in the module docstring.
    """
    original_status = span.set_status
    original_record = getattr(span, "record_exception", None)
    typed = [False]  # error.type already set by record_exception (Redis)

    def set_error_type(exception: BaseException | None, text: str) -> None:
        code = redis_reply_code(exception, text)
        if code is not None:
            span.set_attribute("db.response.status_code", code)
        span.set_attribute("error.type", code or "_OTHER")
        typed[0] = True

    def set_status(status: Status | StatusCode, description: str | None = None) -> None:
        code = status.status_code if isinstance(status, Status) else status
        if code is not StatusCode.ERROR:
            original_status(status, description)
            return
        if redis and not typed[0]:
            try:
                text = (status.description if isinstance(status, Status) else description) or ""
                # use_span writes "<ExceptionType>: <message>".
                set_error_type(None, _server_text(text.split(": ", 1)[1] if ": " in text else text))
            except Exception:  # noqa: BLE001 - never raise into the instrumented call
                _log.debug("redis error.type not set", exc_info=False)
        original_status(Status(StatusCode.ERROR))

    def record_exception(exception: BaseException, attributes: Any = None, timestamp: int | None = None,
                         escaped: bool = False) -> None:
        try:
            message = _message(exception)
            safe = sanitize_pipeline_message(message)
            extra = dict(attributes or {})
            if safe != message or exception.__cause__ is not None or exception.__context__ is not None:
                extra["exception.message"] = safe
                extra["exception.stacktrace"] = _sanitized_stacktrace(exception)
            if not typed[0]:
                set_error_type(exception, _server_text(message))
        except Exception:  # noqa: BLE001 - a minimal event, never the raw text, never a raise
            extra = _minimal_event(exception)
        try:
            original_record(exception, attributes=extra or None, timestamp=timestamp, escaped=escaped)
        except Exception:  # noqa: BLE001
            _log.debug("redis exception event not recorded", exc_info=False)

    try:
        span.set_status = set_status  # type: ignore[method-assign]
        if redis and original_record is not None:
            span.record_exception = record_exception  # type: ignore[method-assign]
    except AttributeError:  # a span type with __slots__: left as it is
        pass
    return span


class _ErrorDescriptionDroppingTracer(trace.Tracer):
    """Tracer for a DB instrumentation (SQLAlchemy, Redis): its spans drop the ERROR description."""

    def __init__(self, tracer: trace.Tracer, redis: bool = False) -> None:
        self._tracer = tracer
        self._redis = redis

    def start_span(self, *args: Any, **kwargs: Any) -> Span:
        return _drop_error_description(self._tracer.start_span(*args, **kwargs), redis=self._redis)

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

    def __init__(self, provider: trace.TracerProvider, redis: bool = False) -> None:
        self._provider = provider
        self._redis = redis

    def get_tracer(self, *args: Any, **kwargs: Any) -> trace.Tracer:
        return _ErrorDescriptionDroppingTracer(self._provider.get_tracer(*args, **kwargs), redis=self._redis)


def wrapped_instrument_kwargs(redis: bool = False) -> dict:
    """``instrument()`` kwargs every helper path uses for contrib sqlalchemy and redis (A25 R1, A26).

    Shared by ``instrument_sql()``, ``instrument_redis()`` and the deprecated
    ``SQL`` / ``REDIS`` flags of ``setup_telemetry()``
    (``instrumentation.configure_instrumentations``), so no path instruments a
    database client without the status wrapper. ``redis``: the Redis rules too.
    """
    return {"tracer_provider": _ErrorDescriptionDroppingTracerProvider(trace.get_tracer_provider(), redis=redis)}


def mark_wrapped(instrumentor) -> None:
    """Record that ``instrumentor`` was instrumented with :func:`wrapped_instrument_kwargs`."""
    setattr(instrumentor, _WRAPPED_MARK, True)


def clear_wrapped(instrumentor) -> None:
    """Forget the mark (after ``uninstrument()``, or before instrumenting again)."""
    if hasattr(instrumentor, _WRAPPED_MARK):
        delattr(instrumentor, _WRAPPED_MARK)


def is_wrapped(instrumentor) -> bool:
    """Whether ``instrumentor`` was instrumented with :func:`wrapped_instrument_kwargs`."""
    return bool(getattr(instrumentor, _WRAPPED_MARK, False))
