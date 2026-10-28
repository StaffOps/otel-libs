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
import sys
import traceback

# Self-diagnostics of the helper and of the OTel SDK reach stderr at INFO and above.
_DIAGNOSTIC_LOGGERS = ("otel_helper", "opentelemetry")
DIAGNOSTIC_LEVEL = logging.INFO

# LogRecord attributes that are not "extra" fields.
_RESERVED = frozenset(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {"message", "asctime"}


def is_diagnostic(name: str) -> bool:
    """True for records of the ``otel_helper`` and ``opentelemetry`` logger hierarchies."""
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
    """RFC 3339, millisecond precision, ``Z`` for UTC (same as Go's fallback)."""
    stamp = datetime.datetime.fromtimestamp(created).astimezone().isoformat(timespec="milliseconds")
    return stamp[:-6] + "Z" if stamp.endswith("+00:00") else stamp


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
        return json.dumps(entry, default=str, ensure_ascii=False)


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
