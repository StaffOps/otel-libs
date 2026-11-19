"""Application span failure rule (R11, ADR-016, contract design D15).

A failure marks the span with status ERROR without a description and
``error.type``; no ``exception`` event is added. The message and the stack go
to one ERROR record on the logger ``staffops_otel_helper.failure``, emitted in
the marked span's context (ADR-014): one record per span that marks the
failure (no deduplication, design D15 "Nesting"). A cancellation is not a
failure.

That logger carries **application** records, not helper diagnostics: the
console routing sends it the application way (stdout JSON without a logs
exporter, OTLP only with one, never stderr; ``_console.is_failure_record``).
"""

from __future__ import annotations

import builtins
import logging
import sys
import traceback

from opentelemetry import trace
from opentelemetry.trace import Span, Status, StatusCode


#: The logger of the failure records (design D15 "Channel").
FAILURE_LOGGER = "staffops_otel_helper.failure"
#: Namespaced override of the type value (class or instance attribute, non-empty ``str``).
_OVERRIDE = "__otel_error_type__"
#: Exception-group walk bound (the A29 budget): groups and leaves visited.
_MAX_GROUP_NODES = 256

_log = logging.getLogger("staffops_otel_helper.failure")  # literal: the A3 channel scan reads it


def _qualified(exc_type: type) -> str:
    """``module.qualname``, bare for ``builtins`` / ``__main__`` (also re-exported by ``_db_status``)."""
    module = exc_type.__module__
    return exc_type.__qualname__ if module in ("builtins", "__main__") else f"{module}.{exc_type.__qualname__}"
_diag = logging.getLogger("staffops_otel_helper.tracing")


def _is_cancellation_leaf(exc: BaseException) -> bool:
    if not isinstance(exc, Exception):
        return True
    grpc = sys.modules.get("grpc")
    if grpc is None:
        return False
    try:
        if not isinstance(exc, grpc.RpcError):
            return False
        return exc.code() == grpc.StatusCode.CANCELLED
    except Exception:  # noqa: BLE001 - any error means "not cancelled"
        return False


def _group_types() -> tuple:
    """``BaseExceptionGroup`` (3.11+) and the ``exceptiongroup`` backport when already imported."""
    types = []
    builtin = getattr(builtins, "BaseExceptionGroup", None)
    if builtin is not None:
        types.append(builtin)
    backport = sys.modules.get("exceptiongroup")
    backport_type = getattr(backport, "BaseExceptionGroup", None) if backport is not None else None
    if isinstance(backport_type, type) and backport_type not in types:
        types.append(backport_type)
    return tuple(types)


def is_cancellation(exc: BaseException) -> bool:
    """True for a cancellation (design D15): never a failure, the span stays Unset.

    - a ``BaseException`` that is not an ``Exception``;
    - a ``grpc.RpcError`` whose ``code()`` is ``CANCELLED`` (checked only when
      ``grpc`` is already imported, ``code()`` under ``try``);
    - a ``BaseExceptionGroup`` (``ExceptionGroup`` too) whose every leaf is a
      cancellation, walked through ``.exceptions`` up to 256 groups and leaves
      (beyond that it is a failure).
    """
    groups = _group_types()
    if not groups or not isinstance(exc, groups):
        return _is_cancellation_leaf(exc)
    pending: list[BaseException] = [exc]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue  # the same member twice, or a cycle
        if len(seen) >= _MAX_GROUP_NODES:
            return False
        seen.add(id(current))
        if isinstance(current, groups):
            try:
                members = list(current.exceptions)
            except Exception:  # noqa: BLE001 - a hostile group
                return False
            if not members:
                return False
            pending.extend(members)
        elif not _is_cancellation_leaf(current):
            return False
    return True


def error_type(exc: BaseException) -> str:
    """``__otel_error_type__`` (non-empty ``str``) when set, else ``module.qualname`` (bare for builtins)."""
    try:
        override = getattr(exc, _OVERRIDE, None)
    except Exception:  # noqa: BLE001 - a hostile __getattr__
        override = None
    if isinstance(override, str) and override:
        return override
    return _qualified(type(exc))


def _message(exc: BaseException) -> str:
    try:
        return str(exc)
    except Exception:  # noqa: BLE001 - a raising __str__ must not cost the record
        return f"[unprintable {type(exc).__name__}]"


# The helper's own package: traceback frames from its modules at the head of a
# failure's traceback are the helper's (the scope's `yield`, a decorator wrapper).
# Matched on the module name, not the file path, so a sourceless or relocated
# install trims the same frames.
_HELPER_PACKAGE = __name__.rpartition(".")[0]


def _is_helper_frame(tb) -> bool:
    module = tb.tb_frame.f_globals.get("__name__", "")
    return module == _HELPER_PACKAGE or module.startswith(_HELPER_PACKAGE + ".")


def _skip_helper_frames(tb):
    """The traceback without its leading helper frames (the exception is not touched).

    With ``traced`` the traceback starts at the scope's ``yield`` (the exception
    is thrown into it) and, for a decorated function, the helper's wrapper. A
    traceback made only of helper frames is kept as it is.
    """
    head = tb
    while head is not None and _is_helper_frame(head):
        head = head.tb_next
    return head if head is not None else tb


def _stacktrace(exc: BaseException) -> str:
    try:
        tb = _skip_helper_frames(exc.__traceback__)
        return "".join(traceback.format_exception(type(exc), exc, tb)).rstrip()
    except Exception:  # noqa: BLE001
        return ""


def emit_exception_record(span: Span, message: str, attributes: dict) -> None:
    """One ERROR record on the D15 channel, in ``span``'s context (recording or not).

    The body is ``message``; ``attributes`` (``exception.type``,
    ``exception.message``, ``exception.stacktrace``, any subset) go through
    ``extra=``. Shared by the failure rule and the DB client spans of
    ``ext.sql`` (ADR-014, task A36); ``ext.redis`` could use it too. Never
    raises into the caller: an internal error is one diagnostics line.
    """
    try:
        # The helper owns this logger: neither an app dictConfig(disable_existing_loggers=True) without a
        # logs exporter nor a level set on the staffops_otel_helper diagnostics logger may drop records (D15).
        if _log.disabled:
            _log.disabled = False
        if _log.level == logging.NOTSET:
            _log.setLevel(logging.DEBUG)
        with trace.use_span(span, end_on_exit=False, record_exception=False, set_status_on_exception=False):
            _log.error(message, extra=attributes)
    except Exception as internal:  # noqa: BLE001 - never raise into the failing code path
        _diag.warning("[OtelHelper] Could not write an exception log record (%s).", type(internal).__name__)


def exception_attributes(exc: BaseException, type_value: str) -> tuple[str, dict]:
    """The body and the three attributes of the record for ``exc`` (ADR-014)."""
    message = _message(exc)
    return message, {
        "exception.type": type_value,
        "exception.message": message,
        "exception.stacktrace": _stacktrace(exc),
    }


#: Module prefixes whose frames leave the DB record's stack: the helper and the OpenTelemetry instrumentations.
_SKIPPED_MODULES = (_HELPER_PACKAGE, "opentelemetry")
#: Cap of the DB record's stack, cut in the middle as Go does (Loki structured metadata, containerd lines).
_MAX_STACK_BYTES = 12 << 10


def _skipped_frame(frame) -> bool:
    module = frame.f_globals.get("__name__", "")
    return any(module == m or module.startswith(m + ".") for m in _SKIPPED_MODULES)


def _skipped_file(filename: str) -> bool:
    """A traceback entry (a file name only) of the helper or an OpenTelemetry package."""
    path = filename.replace("\\", "/")
    return f"/{_HELPER_PACKAGE}/" in path or "/opentelemetry/" in path


def _cut_middle(text: str, limit: int = _MAX_STACK_BYTES) -> str:
    """``text`` cut in the middle above ``limit`` bytes (UTF-8), whole lines, one marker line."""
    if len(text.encode("utf-8")) <= limit:
        return text
    lines = text.split("\n")
    budget = limit - 64
    head: list[str] = []
    tail: list[str] = []
    used = 0
    lo, hi = 0, len(lines) - 1
    while lo <= hi:
        size = len(lines[hi].encode("utf-8")) + 1
        if used + size > budget // 2:
            break
        tail.insert(0, lines[hi])
        used += size
        hi -= 1
    while lo <= hi:
        size = len(lines[lo].encode("utf-8")) + 1
        if used + size > budget:
            break
        head.append(lines[lo])
        used += size
        lo += 1
    elided = hi - lo + 1
    return "\n".join([*head, f"  ...{elided} lines elided by otel-helper...", *tail])


def db_stacktrace(exc: BaseException) -> str:
    """The DB record's ``exception.stacktrace`` (task A36 review): the call stack above the traceback.

    A DB error is raised and handled inside the driver and SQLAlchemy, so its
    own traceback never reaches the application. The callers of the frame
    handling the error (the traceback's first frame, when it is still running;
    else every running frame) are put above the traceback's frames, both without the helper's and the OpenTelemetry
    instrumentations' frames, so the application frame shows; the chained
    exceptions follow as ``traceback`` formats them. Cut in the middle above
    12 KiB. Never raises.
    """
    try:
        tb_frames = set()
        tb = exc.__traceback__
        while tb is not None:
            tb_frames.add(id(tb.tb_frame))
            tb = tb.tb_next
        # Callers of the frame handling the error (the traceback's first frame) when it is still
        # running: the frames below it on the live stack (SQLAlchemy's _handle_dbapi_exception, the
        # event dispatch, the helper) are not callers of the failed call (A36 review N2).
        frame = sys._getframe(1)
        head = exc.__traceback__.tb_frame if exc.__traceback__ is not None else None
        if head is not None:
            live = frame
            while live is not None and live is not head:
                live = live.f_back
            if live is head:
                frame = head.f_back
        outer = []
        while frame is not None:
            if id(frame) not in tb_frames and not _skipped_frame(frame):
                outer.append((frame, frame.f_lineno))
            frame = frame.f_back
        outer.reverse()
        te = traceback.TracebackException(type(exc), exc, exc.__traceback__)
        inner = [f for f in te.stack if not _skipped_file(f.filename)]
        te.stack = traceback.StackSummary.from_list(
            list(traceback.StackSummary.extract(outer, capture_locals=False)) + inner)
        return _cut_middle("".join(te.format()).rstrip())
    except Exception:  # noqa: BLE001 - the plain traceback, never a raise
        return _cut_middle(_stacktrace(exc))


def db_exception_attributes(exc: BaseException, type_value: str) -> tuple[str, dict]:
    """The body and the three attributes of a DB record (``exception.stacktrace`` = :func:`db_stacktrace`)."""
    message = _message(exc)
    return message, {
        "exception.type": type_value,
        "exception.message": message,
        "exception.stacktrace": db_stacktrace(exc),
    }


def _emit(exc: BaseException, span: Span, type_value: str) -> None:
    message, attributes = exception_attributes(exc, type_value)
    emit_exception_record(span, message, attributes)


def mark_failure(span: Span, exc: BaseException) -> None:
    """Apply the rule to ``span`` for ``exc``: ERROR, ``error.type`` and one record in its context."""
    try:
        type_value = error_type(exc)
        span.set_status(Status(StatusCode.ERROR))
        span.set_attribute("error.type", type_value)
        _emit(exc, span, type_value)
    except Exception as internal:  # noqa: BLE001 - the helper never raises into the failing code path
        # No exc_info: the internal error's __context__ is the application exception (its message, stack).
        _diag.warning("[OtelHelper] Could not record a failure of type %s on its span (%s).",
                      type(exc).__name__, type(internal).__name__)


def record_failure(exc: BaseException, *, span: Span | None = None) -> None:
    """Mark ``span`` (default: the current span) as failed by ``exc`` (R11, design D15).

    For a failure the application handles instead of letting it leave a
    :func:`~staffops_otel_helper.traced` span. The span gets status ERROR
    without a description and ``error.type`` (``__otel_error_type__`` when set,
    else the qualified class name); the message and the stack go to one ERROR
    record on the ``staffops_otel_helper.failure`` logger, emitted in the span's
    context even when that span is not current or not recording. Every call
    records. Never sets Ok; whatever it is given is marked (no cancellation
    check).

    Raises:
        TypeError: ``exc`` is not an exception, or ``span`` is not a Span.
    """
    if not isinstance(exc, BaseException):
        raise TypeError(f"record_failure() needs an exception, got {type(exc).__name__}")
    if span is None:
        span = trace.get_current_span()
    elif not isinstance(span, Span):
        raise TypeError(f"record_failure() span must be an opentelemetry Span, got {type(span).__name__}")
    mark_failure(span, exc)
