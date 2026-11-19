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
- ``db.operation.name`` on command spans (A30, contract D1 "Redis
  ``db.operation.name``"): the command as contrib names the span (redis-py's
  first argument, ``SET``; ``FT.SEARCH`` / ``FT.CREATE``), written when the
  span ends; none on a pipeline span or a span without a command, as Go.

SQL spans (``redis=False``, contract D16, R12, task A44) also get, when they
end with status ERROR and carry no ``error.type`` yet, ``db.response.status_code``
and ``error.type`` set to the driver's error code (:func:`sql_error_code`), or
``error.type=_OTHER`` without an accepted code. The exception is captured in
the wrapped ``set_status(ERROR)`` from ``sys.exc_info()`` when its ``str()``
equals the status description (contrib sqlalchemy calls it from SQLAlchemy's
``handle_error``, inside the ``except``), and in a wrapped ``record_exception``
(the ``connect`` span). Values already on the span are never overwritten.
SQLite spans keep contrib's ``db.namespace`` / ``db.name`` (the file path)
and span name (documented residual).

SQL spans also follow ADR-014 (task A36): a recorded exception (the
``connect`` span; contrib records none on statement spans) becomes an
``exception`` event with ``exception.type`` only, and a span that ended with
status ERROR writes one ERROR record in its context on the D15 channel
(``staffops_otel_helper.failure``) with the message, ``exception.type`` and
the stack trace (:func:`_emit_sql_record`). Unsampled spans too: contrib
reports nothing on them, so the helper's own SQLAlchemy ``handle_error``
listener (:func:`_sql_handle_error`) and ``start_as_current_span`` (the
``connect`` span) arm the record. A cancellation writes none.
"""

from __future__ import annotations

import ast
import logging
import re
import sys
import threading
import traceback
from contextlib import contextmanager
from typing import Any, Iterator

from opentelemetry import trace
from opentelemetry.trace import Span, Status, StatusCode

from staffops_otel_helper._diag import warn_once_per_process
from staffops_otel_helper._failure import (
    _qualified,
    db_exception_attributes,
    emit_exception_record,
    error_type,
    is_cancellation,
)

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
    "aws": ("botocore", "failed AWS calls may carry the error text in their span status",
            "instrument_aws()"),
}

_log = logging.getLogger("staffops_otel_helper._db_status")


def warn_foreign(log: logging.Logger, kind: str) -> None:
    """Warn once that ``kind`` (``sql`` / ``redis`` / ``aws``) was instrumented by other code without the wrapper."""
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


_SQLSTATE = re.compile(r"[0-9A-Z]{5}")
_POSTGRES_SYSTEMS = frozenset({"postgresql"})
_MYSQL_SYSTEMS = frozenset({"mysql", "mariadb"})


def _sqlstate(value: Any) -> str | None:
    """``value`` when it is a SQLSTATE: five of ``0-9A-Z`` with at least one digit (rejects ``EPIPE``)."""
    if isinstance(value, str) and _SQLSTATE.fullmatch(value) and any(c.isdigit() for c in value):
        return value
    return None


def _mysql_number(value: Any) -> str | None:
    """``value`` as a decimal string when it is a MySQL server error number.

    Accepted: an ``int`` 1–65535 outside the 2000–2999 client range.
    """
    if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 65535 and not 2000 <= value <= 2999:
        return str(value)
    return None


def _safe_getattr(obj: Any, name: str) -> Any:
    try:
        return getattr(obj, name, None)
    except Exception:  # noqa: BLE001 - a property that raises: no value
        return None


def _postgres_code(exc: BaseException) -> str | None:
    # psycopg2 ``pgcode``, psycopg 3 and asyncpg ``sqlstate``, psycopg 2/3 ``diag.sqlstate``.
    for value in (_safe_getattr(exc, "pgcode"), _safe_getattr(exc, "sqlstate"),
                  _safe_getattr(_safe_getattr(exc, "diag"), "sqlstate")):
        code = _sqlstate(value)
        if code is not None:
            return code
    return None


def _mysql_code(exc: BaseException) -> str | None:
    # pymysql and mysqlclient: ``args[0]`` is the error number.
    args = _safe_getattr(exc, "args")
    return _mysql_number(args[0]) if isinstance(args, tuple) and args else None


def sql_error_code(exc: BaseException | None, system: Any) -> str | None:
    """The accepted error code of a failed SQL call, or None (``error.type=_OTHER``) (D16, A44).

    ``system`` is the span's ``db.system.name`` / ``db.system``. PostgreSQL:
    ``pgcode``, then ``sqlstate``, then ``diag.sqlstate``, accepted only as a
    SQLSTATE (``^[0-9A-Z]{5}$`` with at least one digit). MySQL / MariaDB:
    ``args[0]`` when it is an int 1–65535 outside the client range 2000–2999.
    The exception is read first, then its ``__cause__`` (asyncpg through
    SQLAlchemy's adapted error); an ``OSError`` candidate is skipped (its
    ``args[0]`` is an errno). Any other system: None. Never raises.
    """
    if exc is None:
        return None
    if system in _POSTGRES_SYSTEMS:
        read = _postgres_code
    elif system in _MYSQL_SYSTEMS:
        read = _mysql_code
    else:
        return None
    for candidate in (exc, _safe_getattr(exc, "__cause__")):
        # An OSError (ConnectionResetError 104 under aiomysql's 2013) carries an errno, never a code.
        if isinstance(candidate, BaseException) and not isinstance(candidate, OSError):
            try:
                code = read(candidate)
            except Exception:  # noqa: BLE001 - a shape this helper does not know: no code
                code = None
            if code is not None:
                return code
    return None


def _set_sql_error_type(span: Span, exception: BaseException | None) -> None:
    """At the end of a SQL span: ``db.response.status_code`` and ``error.type`` when ERROR and absent (A44)."""
    if not span.is_recording():
        return
    status = getattr(span, "status", None)
    if status is None or status.status_code is not StatusCode.ERROR:
        return
    attributes = getattr(span, "attributes", None) or {}
    if "error.type" in attributes:
        return
    preset = attributes.get("db.response.status_code")
    if preset is not None:  # set upstream: error.type SHOULD match it (semconv)
        span.set_attribute("error.type", str(preset))
        return
    code = sql_error_code(exception, attributes.get("db.system.name", attributes.get("db.system")))
    if code is not None:
        span.set_attribute("db.response.status_code", code)
    span.set_attribute("error.type", code or "_OTHER")


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


#: Set on a wrapped SQL span: the hook the helper's ``handle_error`` listener and
#: ``start_as_current_span`` call for a failure contrib does not report (unsampled spans).
_ARM = "_staffops_arm_failure"

#: Span attributes copied into the record (A36 review: DB records can be told apart).
_RECORD_KEYS = ("db.system.name", "db.response.status_code", "error.type")


#: SQLAlchemy dialect names → semconv ``db.system.name`` values (semconv v1.43.0, the version Go pins;
#: A36 review N4). Others are kept as they are. The record uses these; the span keeps contrib's value.
_SEMCONV_SYSTEMS = {
    "mssql": "microsoft.sql_server",
    "oracle": "oracle.db",
    "postgresql": "postgresql",
    "mysql": "mysql",
    "mariadb": "mariadb",
    "sqlite": "sqlite",
}


def semconv_system(name: str) -> str:
    """The semconv ``db.system.name`` value of a SQLAlchemy dialect name (unknown names unchanged)."""
    return _SEMCONV_SYSTEMS.get(name, name)


def _record_db_attributes(span: Span, exception: BaseException | None, system: Any) -> dict:
    """``db.system.name``, ``db.response.status_code`` and ``error.type`` for the record.

    A recording span gives its own values (``db.system.name``, else the legacy
    ``db.system``; the codes set by :func:`_set_sql_error_type`). An unsampled
    span has none: the dialect name the listener saw and the code read from
    the exception (:func:`sql_error_code`, else ``_OTHER``).
    """
    attributes = getattr(span, "attributes", None) if span.is_recording() else None
    if attributes:
        out = {k: str(attributes[k]) for k in _RECORD_KEYS if attributes.get(k) is not None}
        if "db.system.name" not in out and attributes.get("db.system") is not None:
            out["db.system.name"] = str(attributes["db.system"])
        if "db.system.name" in out:
            out["db.system.name"] = semconv_system(out["db.system.name"])
        elif isinstance(system, str) and system:
            out["db.system.name"] = semconv_system(system)
        return out
    out = {}
    if isinstance(system, str) and system:
        out["db.system.name"] = semconv_system(system)
    code = sql_error_code(exception, system)
    if code is not None:
        out["db.response.status_code"] = code
    out["error.type"] = code or "_OTHER"
    return out


def _emit_sql_record(span: Span, exception: BaseException | None, description: str | None,
                     system: Any = None) -> None:
    """The correlated ERROR record of a failed SQL span (ADR-014, task A36), in the span's context.

    With the captured exception: its ``str()`` as body, ``exception.type``
    (the D15 ``error_type``, also the event's), ``exception.message`` and
    ``exception.stacktrace`` (:func:`_failure.db_exception_attributes`: the
    caller's stack above the traceback, helper and contrib frames dropped, cut
    in the middle above 12 KiB). Without one: the dropped status description
    as body and ``exception.message`` only. Neither: no record. Always the
    ``_RECORD_KEYS`` (:func:`_record_db_attributes`).
    """
    if exception is not None:
        message, attributes = db_exception_attributes(exception, error_type(exception))
    elif description:
        message, attributes = description, {"exception.message": description}
    else:
        return
    attributes.update(_record_db_attributes(span, exception, system))
    emit_exception_record(span, message, attributes)


def _in_context_chain(exception: BaseException, head: BaseException | None) -> bool:
    """``exception`` is ``head`` or in its ``__context__`` chain (bounded, cycle-safe)."""
    seen = 0
    while head is not None and seen < 64:
        if head is exception:
            return True
        head = head.__context__
        seen += 1
    return False


def _drop_error_description(span: Span, redis: bool = False) -> Span:
    """Make ``span.set_status`` keep the ERROR code and drop its description (A25 R1, A26).

    contrib sqlalchemy sets ``Status(ERROR, str(exc))`` (``engine.py``
    ``_handle_error``), and ``trace.use_span`` sets ``"<type>: <exc>"`` on the
    ``connect`` span and on every contrib redis span (``start_as_current_span``):
    database error text can quote a statement value, Redis errors a command
    argument (``unknown command 'X', with args beginning with: '<arg>'``). Go
    and Node leave the description empty too. For Redis an exception event,
    when one is recorded, is kept (semconv ``exception.message``; the A25
    residual) and sanitized as described in the module docstring. SQL spans
    (``redis=False``) also capture the exception, write ``error.type`` at
    ``end()`` (A44), keep a type-only exception event and write the
    correlated record (A36, module docstring).
    """
    original_status = span.set_status
    original_record = getattr(span, "record_exception", None)
    original_end = getattr(span, "end", None)
    original_add_event = getattr(span, "add_event", None)
    typed = [False]  # error.type already set by record_exception (Redis)
    captured: list = [None]  # the failed call's exception (SQL, A44), released at end()
    # SQL (A36): failed (set_status(ERROR) or an armed failure), the description, the dialect name,
    # end() done, record written (one per span).
    failed: list = [False, None]
    system: list = [None]
    ended = [False]
    written = [False]
    # The exception active when the span ended (A36 review N1): a late arm must belong to it.
    end_exc: list = [None]

    def write_record() -> None:
        if written[0] or not failed[0] or (captured[0] is not None and is_cancellation(captured[0])):
            return
        written[0] = True
        try:
            _emit_sql_record(span, captured[0], failed[1], system[0])
        except Exception:  # noqa: BLE001 - never raise into the instrumented call
            _log.debug("sql exception record not written", exc_info=False)
        finally:
            captured[0] = None

    def arm_failure(exception: BaseException, dialect: Any = None, system_only: bool = False) -> None:
        """A failure contrib may not report (an unsampled span): the record is written anyway (A36).

        After ``end()`` only the failure the span ended with is armed (the
        exception active at end time, or one in its ``__context__`` chain):
        a fetch that fails after a statement span ended successfully
        (``stream_results``, ``yield_per``) reaches the same execution
        context, and must not mark that span. ``system_only``: just the
        dialect name (a failed connect, seen by the listener).
        """
        if system[0] is None and dialect is not None:
            system[0] = dialect
        if system_only or not isinstance(exception, BaseException) or is_cancellation(exception):
            return
        if ended[0] and not _in_context_chain(exception, end_exc[0]):
            return
        if captured[0] is None:
            captured[0] = exception
        failed[0] = True
        if ended[0]:
            write_record()  # the listener ran after contrib ended the span

    def capture_active(status: Status | StatusCode, description: str | None) -> None:
        # contrib sqlalchemy: Status(ERROR, str(exc)) from handle_error, inside SQLAlchemy's except.
        text = status.description if isinstance(status, Status) else description
        active = sys.exc_info()[1]
        if active is not None and text is not None and str(active) == text:
            captured[0] = active

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
        elif not redis and ended[0]:
            pass  # a status set after end() (contrib on a later fetch failure): not this span's failure
        elif not redis:
            failed[0] = True
            text = status.description if isinstance(status, Status) else description
            if failed[1] is None and isinstance(text, str) and text:
                failed[1] = text
            if captured[0] is None:
                try:
                    capture_active(status, description)
                except Exception:  # noqa: BLE001 - str() of a foreign exception may raise
                    _log.debug("sql exception not captured", exc_info=False)
        original_status(Status(StatusCode.ERROR))

    def record_sql_exception(exception: BaseException, attributes: Any = None, timestamp: int | None = None,
                             escaped: bool = False) -> None:
        # ADR-014 (A36): the event keeps exception.type only; the detail goes to the record at end().
        if captured[0] is None and isinstance(exception, BaseException):
            captured[0] = exception
        try:
            original_add_event("exception", {"exception.type": error_type(exception)}, timestamp=timestamp)
        except Exception:  # noqa: BLE001 - never the raw text, never a raise
            _log.debug("sql exception event not recorded", exc_info=False)

    def end_sql(*args: Any, **kwargs: Any) -> None:
        try:
            _set_sql_error_type(span, captured[0])
        except Exception:  # noqa: BLE001 - never raise into the instrumented call
            _log.debug("sql error.type not set", exc_info=False)
        if ended[0]:  # a second end() (contrib on a later fetch failure): the SDK ignores it
            original_end(*args, **kwargs)
            return
        ended[0] = True
        end_exc[0] = sys.exc_info()[1]
        write_record()
        original_end(*args, **kwargs)

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
        if not redis:
            if original_record is not None and original_add_event is not None:
                span.record_exception = record_sql_exception  # type: ignore[method-assign]
            if original_end is not None:
                span.end = end_sql  # type: ignore[method-assign]
                setattr(span, _ARM, arm_failure)
    except AttributeError:  # a span type with __slots__: left as it is
        pass
    return span


#: contrib redis span names that are not the command (``util.py`` ``_build_span_name``).
_REDIS_RENAMED = {"redis.search": "FT.SEARCH", "redis.create_index": "FT.CREATE"}


def redis_operation_name(span_name: str) -> str | None:
    """``db.operation.name`` of a contrib redis command span named ``span_name`` (A30), or ``None``.

    contrib names a command span by the first command argument as redis-py
    sends it (``SET``), except ``FT.SEARCH`` / ``FT.CREATE`` (``redis.search``,
    ``redis.create_index``), and a span without a command by the database
    number; pipeline spans are recognized by ``db.redis.pipeline_length`` at
    end (no operation name, as Go).
    """
    if not isinstance(span_name, str) or not span_name or span_name.isdigit() or span_name == "redis":
        return None
    return _REDIS_RENAMED.get(span_name, span_name)


def _set_operation_name_at_end(span: Span, span_name: str) -> Span:
    """Write ``db.operation.name`` on a command span when it ends (contract D1 "Redis ``db.operation.name``", A30)."""
    operation = redis_operation_name(span_name)
    original_end = getattr(span, "end", None)
    if operation is None or original_end is None:
        return span

    def end(end_time: int | None = None) -> None:
        try:
            attributes = getattr(span, "attributes", None) or {}
            if "db.redis.pipeline_length" not in attributes and "db.operation.name" not in attributes:
                span.set_attribute("db.operation.name", operation)
        except Exception:  # noqa: BLE001 - never raise into the instrumented call
            _log.debug("redis db.operation.name not set", exc_info=False)
        original_end(end_time=end_time)

    try:
        span.end = end  # type: ignore[method-assign]
    except AttributeError:  # a span type with __slots__: left as it is
        pass
    return span


class _ErrorDescriptionDroppingTracer(trace.Tracer):
    """Tracer for a DB instrumentation (SQLAlchemy, Redis): its spans drop the ERROR description."""

    def __init__(self, tracer: trace.Tracer, redis: bool = False) -> None:
        self._tracer = tracer
        self._redis = redis

    def start_span(self, *args: Any, **kwargs: Any) -> Span:
        span = _drop_error_description(self._tracer.start_span(*args, **kwargs), redis=self._redis)
        if self._redis:
            span = _set_operation_name_at_end(span, kwargs.get("name", args[0] if args else None))
        return span

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
            try:
                yield current
            except BaseException as exc:
                # use_span records nothing on an unsampled span (the connect span): arm the
                # A36 record; the cancellation rule is applied by the arm hook.
                if not self._redis and not current.is_recording():
                    arm = getattr(current, _ARM, None)
                    if callable(arm):
                        arm(exc)
                raise


class _ErrorDescriptionDroppingTracerProvider(trace.TracerProvider):
    """Wraps the global TracerProvider (resolved lazily by the API proxy when it is not set yet)."""

    def __init__(self, provider: trace.TracerProvider, redis: bool = False) -> None:
        self._provider = provider
        self._redis = redis

    def get_tracer(self, *args: Any, **kwargs: Any) -> trace.Tracer:
        return _ErrorDescriptionDroppingTracer(self._provider.get_tracer(*args, **kwargs), redis=self._redis)


def _sql_handle_error(context: Any) -> None:
    """The helper's SQLAlchemy ``handle_error`` listener (A36): arms the record of a wrapped span.

    contrib sqlalchemy (0.63b0 to 0.66b2, ``engine.py`` ``_handle_error``)
    reports a failed statement only when its span records; this listener reads
    the same span (``context.execution_context._otel_span``, the attribute both
    versions set in ``_before_cur_exec``) and arms it with the original
    exception and the dialect name, sampled or not. A sampled span then still
    writes one record (no duplicate). For a failed connect (no execution
    context) it gives contrib's current ``connect`` span the dialect name only;
    the ``connect`` wrapper arms the failure. Never raises.
    """
    try:
        dialect = getattr(getattr(context, "dialect", None), "name", None)
        execution_context = getattr(context, "execution_context", None)
        if execution_context is None:
            # A failed connect: no execution context; contrib's connect span is current.
            arm = getattr(trace.get_current_span(), _ARM, None)
            if callable(arm):
                arm(context.original_exception, dialect, system_only=True)
            return
        arm = getattr(getattr(execution_context, "_otel_span", None), _ARM, None)
        if callable(arm):
            arm(context.original_exception, dialect)
    except Exception:  # noqa: BLE001 - a listener must not change the failing call
        _log.debug("sql handle_error listener failed", exc_info=False)


_LISTENER_INSTALLED = [False]
_LISTENER_LOCK = threading.Lock()


def install_sql_error_listener() -> None:
    """Listen to ``handle_error`` on every SQLAlchemy Engine, once per process (A36). No SQLAlchemy: nothing.

    Thread-safe (a lock). The listener stays registered after
    ``SQLAlchemyInstrumentor().uninstrument()``: it then finds no wrapped span
    (contrib sets no ``_otel_span``) and does nothing.
    """
    with _LISTENER_LOCK:
        if _LISTENER_INSTALLED[0]:
            return
        try:
            from sqlalchemy import event
            from sqlalchemy.engine import Engine
        except Exception:  # noqa: BLE001 - the [sql] extra is not installed
            return
        event.listen(Engine, "handle_error", _sql_handle_error)
        _LISTENER_INSTALLED[0] = True


def wrapped_instrument_kwargs(redis: bool = False) -> dict:
    """``instrument()`` kwargs every helper path uses for contrib sqlalchemy and redis (A25 R1, A26).

    Shared by ``instrument_sql()``, ``instrument_redis()`` and the deprecated
    ``SQL`` / ``REDIS`` flags of ``setup_telemetry()``
    (``instrumentation.configure_instrumentations``), so no path instruments a
    database client without the status wrapper. ``redis``: the Redis rules too.
    SQL also installs the ``handle_error`` listener (A36, unsampled spans).
    """
    if not redis:
        install_sql_error_listener()
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
