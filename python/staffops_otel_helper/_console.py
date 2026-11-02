"""Console output of the logging setup (design D6, P10, A5 validation rulings).

The only module of the package that touches the standard streams or
``logging.lastResort`` (the diagnostics-channel test exempts it by name).

- ``ConsoleFallback`` (no logs exporter): installed as ``logging.lastResort``,
  which the stdlib consults only when a record found no handler anywhere in its
  logger chain — so it is active exactly while the application has not
  configured logging, and a later ``logging.basicConfig()`` takes over without
  the helper intercepting it. Application records -> stdout as single-line JSON
  (same shape as Go's slog JSON fallback); diagnostics -> stderr.
- ``DiagnosticsToStderr`` (logs exporter active): a root handler forwarding only
  helper/SDK diagnostics to the original stderr lastResort, silent once the
  application has configured a root handler of its own.
"""

from __future__ import annotations

import datetime
import json
import logging
import math
import sys
import traceback

# Self-diagnostics of the helper and of the OTel SDK reach stderr at INFO and above.
_DIAGNOSTIC_LOGGERS = ("staffops_otel_helper", "opentelemetry")
DIAGNOSTIC_LEVEL = logging.INFO

# LogRecord attributes that are not "extra" fields.
_RESERVED = frozenset(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {"message", "asctime"}


def is_diagnostic(name: str) -> bool:
    """True for records of the ``staffops_otel_helper`` and ``opentelemetry`` logger hierarchies."""
    return any(name == p or name.startswith(p + ".") for p in _DIAGNOSTIC_LOGGERS)


def is_sdk_diagnostic(name: str) -> bool:
    """True for ``opentelemetry[.*]`` records: never exported through OTLP (feedback loop)."""
    return name == "opentelemetry" or name.startswith("opentelemetry.")


def _slog_level(levelno: int) -> str:
    """Go slog level names: DEBUG, INFO, WARN, ERROR (CRITICAL -> ERROR)."""
    if levelno < logging.INFO:
        return "DEBUG"
    if levelno < logging.WARNING:
        return "INFO"
    if levelno < logging.ERROR:
        return "WARN"
    return "ERROR"


def _rfc3339_ms(created: float) -> str:
    """RFC 3339 in UTC, millisecond precision, ``Z`` suffix, whatever the process zone (``TZ``).

    Same as the Go and Node.js fallbacks (contract design D1 row "Stdout fallback time zone", A23).
    """
    stamp = datetime.datetime.fromtimestamp(created, tz=datetime.timezone.utc).isoformat(timespec="milliseconds")
    return stamp[:-6] + "Z"  # isoformat() of an aware UTC datetime ends with "+00:00"


_JSON_KEY_TYPES = (str, int, float, bool, type(None))


def _json_key(key):
    """A dict key json encodes: str/int/bool/None and finite floats as they are, anything else ``str(key)``."""
    if isinstance(key, float):
        return key if math.isfinite(key) else str(key)
    return key if isinstance(key, _JSON_KEY_TYPES) else _text(key)


def _json_safe(value, ancestors: set):
    """A JSON-encodable copy of ``value`` (contract design D1 row "Stdout fallback fields", A23).

    Used only when the fast ``json.dumps`` failed: non-finite floats as ``"nan"`` / ``"inf"`` /
    ``"-inf"``, a cycle as ``"[Circular]"``, dict keys json cannot encode (tuples, objects) as
    ``str(key)``; ``ancestors`` holds the ids of the containers on the current path (O(1) per level).
    """
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if not isinstance(value, (dict, list, tuple)):
        return value  # json.dumps(default=_text) renders the rest
    if id(value) in ancestors:
        return "[Circular]"
    ancestors.add(id(value))
    try:
        if isinstance(value, dict):
            return {_json_key(k): _json_safe(v, ancestors) for k, v in value.items()}
        return [_json_safe(v, ancestors) for v in value]
    finally:
        ancestors.discard(id(value))


def _dump(obj) -> str:
    return json.dumps(obj, default=_text, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _dumps(entry: dict) -> str:
    """One JSON line, compact separators as Go and Node; never bare NaN/Infinity (A23).

    Fast path: the whole entry at once. When that raises, each field is tried on its own and
    only the failing ones are sanitized (``_json_safe``), then rendered as text; no extra can
    cost the record.
    """
    try:
        return _dump(entry)
    except Exception:  # noqa: BLE001 - logging must not raise; the fields are retried below
        pass
    out = {}
    for key, value in entry.items():
        try:
            _dump(value)
            out[key] = value
            continue
        except Exception:  # noqa: BLE001
            pass
        try:
            safe = _json_safe(value, set())
            _dump(safe)
            out[key] = safe
        except Exception:  # noqa: BLE001 - a hostile container (items() raising, too deep)
            out[key] = _text(value)
    return _dump(out)


def _text(value) -> str:
    """``str(value)`` that never raises (a deep or hostile ``__str__``/``__repr__``)."""
    try:
        return str(value)
    except Exception:  # noqa: BLE001 - logging must not raise
        return f"[unprintable {type(value).__name__}]"


class JSONFormatter(logging.Formatter):
    """One JSON object per line: time, level, msg, logger, extra attributes, exception."""

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "time": _rfc3339_ms(record.created),
            "level": _slog_level(record.levelno),
            "msg": record.getMessage(),
            "logger": record.name,
        }
        for key, value in vars(record).items():
            if key not in _RESERVED and key not in entry:
                entry[key] = value
        if record.exc_info:
            entry["exception"] = "".join(traceback.format_exception(*record.exc_info)).rstrip()
        elif record.exc_text:
            entry["exception"] = record.exc_text
        if record.stack_info:
            entry["stack"] = record.stack_info
        return _dumps(entry)


class _StdoutHandler(logging.StreamHandler):
    """StreamHandler bound to the *current* ``sys.stdout`` (like the stdlib's stderr lastResort)."""

    def __init__(self, level: int) -> None:
        logging.Handler.__init__(self, level)  # StreamHandler.__init__ would capture the stream

    @property
    def stream(self):  # type: ignore[override]
        return sys.stdout


def _to_stderr(previous: logging.Handler | None, record: logging.LogRecord) -> bool:
    if previous is None or record.levelno < DIAGNOSTIC_LEVEL:
        return False
    previous.handle(record)  # Handler.handle does not re-check the handler's own level
    return True


class ConsoleFallback(logging.Handler):
    """Application records -> stdout JSON at ``level``; diagnostics -> the previous lastResort."""

    def __init__(self, level: int, previous: logging.Handler | None) -> None:
        super().__init__(logging.NOTSET)
        self._stdout = _StdoutHandler(level)
        self._stdout.setFormatter(JSONFormatter())
        self.previous = previous

    def handle(self, record: logging.LogRecord) -> bool:
        if is_diagnostic(record.name):
            return _to_stderr(self.previous, record)
        return bool(self._stdout.handle(record))

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover - handle() routes
        self.handle(record)

    def flush(self) -> None:
        self._stdout.flush()


class DiagnosticsToStderr(logging.Handler):
    """Root handler: diagnostics -> the stdlib lastResort, while root has no application handler.

    ``helper_handlers`` are the helper's own root handlers (ignored when deciding
    whether the application configured root).
    """

    def __init__(self, previous: logging.Handler | None, helper_handlers: tuple) -> None:
        super().__init__(logging.NOTSET)
        self.previous = previous
        self._helper = helper_handlers

    def handle(self, record: logging.LogRecord) -> bool:
        if not is_diagnostic(record.name):
            return False
        if any(h is not self and h not in self._helper for h in logging.getLogger().handlers):
            return False  # the application configured root: its handlers decide
        return _to_stderr(self.previous, record)

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover - handle() routes
        self.handle(record)


class SetupDiagnostics(logging.Handler):
    """lastResort while setup_telemetry() runs: stdlib behavior, plus INFO+ helper diagnostics."""

    def __init__(self, previous: logging.Handler | None) -> None:
        super().__init__(logging.NOTSET)
        self.previous = previous

    def handle(self, record: logging.LogRecord) -> bool:
        if self.previous is None:
            return False
        if is_diagnostic(record.name) and not is_sdk_diagnostic(record.name):
            return _to_stderr(self.previous, record)
        if record.levelno >= self.previous.level:  # what the stdlib would have done
            self.previous.handle(record)
            return True
        return False

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover - handle() routes
        self.handle(record)


def install_setup_diagnostics() -> SetupDiagnostics:
    handler = SetupDiagnostics(logging.lastResort)
    logging.lastResort = handler
    return handler


def uninstall_setup_diagnostics(handler: SetupDiagnostics | None) -> None:
    if handler is not None and logging.lastResort is handler:
        logging.lastResort = handler.previous


def install(level: int) -> ConsoleFallback:
    """Make a ConsoleFallback the stdlib lastResort; returns it for uninstall()."""
    fallback = ConsoleFallback(level, logging.lastResort)
    logging.lastResort = fallback
    return fallback


def uninstall(fallback: ConsoleFallback) -> None:
    """Restore the previous lastResort, unless someone replaced ours meanwhile."""
    if logging.lastResort is fallback:
        logging.lastResort = fallback.previous
    fallback.flush()


def diagnostics_handler(helper_handlers: tuple) -> DiagnosticsToStderr:
    """A DiagnosticsToStderr bound to the current lastResort."""
    return DiagnosticsToStderr(logging.lastResort, helper_handlers)
