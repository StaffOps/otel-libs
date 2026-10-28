"""Logging setup (design D6, P10, P16).

With a logs exporter active, an OpenTelemetry handler from
``opentelemetry-instrumentation-logging`` (the SDK's ``LoggingHandler`` is
deprecated) goes on the root logger and on uvicorn's non-propagating loggers.
Without one, no OTel handler is attached at all: application records reach
stdout through a console fallback installed as ``logging.lastResort`` (see
``otel_helper._console``), so ``logging.basicConfig()`` called later still works.
"""

from __future__ import annotations

import logging
import logging.config
import threading
import weakref
from dataclasses import dataclass, field

from opentelemetry.instrumentation.logging.handler import LoggingHandler
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs import LoggingHandler as _SDKLoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.resources import Resource

from otel_helper import _console
from otel_helper._exporters import build_otlp_exporter
from otel_helper.config import TelemetryOptions, _env, get_default_log_level

# Upstream variable (opentelemetry-instrumentation-logging): code.file.path,
# code.function.name and code.line.number on every record. Default false, as upstream.
ENV_LOG_CODE_ATTRIBUTES = "OTEL_PYTHON_LOG_CODE_ATTRIBUTES"

_log = logging.getLogger("otel_helper.logging")

# Framework loggers quieted outside DEBUG — only when the application left them NOTSET.
# httpx/hpack log every request at INFO/DEBUG.
_NOISE_LEVELS = {"urllib3": logging.ERROR, "httpcore": logging.ERROR, "grpc": logging.ERROR,
                 "httpx": logging.WARNING, "hpack": logging.WARNING, "opentelemetry": logging.WARNING}

# uvicorn's default LOGGING_CONFIG sets propagate=False on "uvicorn" and
# "uvicorn.access", so their records never reach the root handler (P16).
_EXTRA_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")

# Name of the SDK BatchLogRecordProcessor worker thread (sdk 1.42-1.45): records it
# emits come from exporting logs and would feed the next batch.
_LOG_EXPORT_THREAD = "OtelBatchLogRecordProcessor"
_HELPER_LOGGER = "otel_helper"

# Serializes the guarded stdlib config functions (they mutate root's handler list).
_config_lock = threading.RLock()


class HelperLogHandler(LoggingHandler):
    """The contrib OTel handler: each record at most once, SDK diagnostics never.

    The same instance sits on root and on uvicorn's loggers; a record that
    propagates through several of them is exported once. ``opentelemetry[.*]``
    records (SDK/exporter diagnostics) are not exported: an export failure logged
    through the pipeline that failed would feed itself (design D6 A5 rulings);
    they reach stderr instead.
    """

    def __init__(self, level: int, logger_provider: LoggerProvider, log_code_attributes: bool) -> None:
        super().__init__(level=level, logger_provider=logger_provider, log_code_attributes=log_code_attributes)
        self._last = threading.local()

    def handle(self, record: logging.LogRecord) -> bool:
        if _console.is_sdk_diagnostic(record.name) or record.threadName == _LOG_EXPORT_THREAD:
            return False  # SDK diagnostics, and whatever the log exporter's own I/O logs (urllib3...)
        last = getattr(self._last, "ref", None)
        if last is not None and last() is record:
            return False
        self._last.ref = weakref.ref(record)
        return bool(super().handle(record))


def build_logging_level(options: TelemetryOptions) -> int:
    """Level of the helper's handlers: code value (``0``/NOTSET included), else by environment (R4)."""
    if options.minimum_log_level is not None:
        return options.minimum_log_level
    return get_default_log_level(options.environment, options.resolved.debug_level)


def build_logging(resource: Resource, options: TelemetryOptions):
    """Build the LoggerProvider and its handler without attaching anything.

    Returns ``(provider, handler, level)``. With no active logs exporter (no
    OTLP endpoint for logs, ``OTEL_LOGS_EXPORTER=none``) provider and handler
    are None: an OTel handler without a processor would swallow records.
    """
    level = build_logging_level(options)
    exporters = options.resolved_exporters("logs")
    plan = options.otlp_export("logs")
    want_otlp = "otlp" in exporters and plan is not None
    if not want_otlp and "console" not in exporters:
        return None, None, level
    provider = LoggerProvider(resource=resource, shutdown_on_exit=False)
    try:
        if want_otlp:
            provider.add_log_record_processor(BatchLogRecordProcessor(build_otlp_exporter(plan)))
        if "console" in exporters:
            from opentelemetry.sdk._logs.export import ConsoleLogRecordExporter

            provider.add_log_record_processor(BatchLogRecordProcessor(ConsoleLogRecordExporter()))
        code_attributes = (_env(ENV_LOG_CODE_ATTRIBUTES) or "").lower() == "true"
        handler = HelperLogHandler(level, provider, code_attributes)
    except BaseException:
        provider.shutdown()
        raise
    return provider, handler, level


@dataclass
class LogAttachment:
    """What attach_logging() changed; detach_logging() undoes it."""

    handler: logging.Handler | None
    fallback: _console.ConsoleFallback | None = None
    diagnostics: logging.Handler | None = None
    root_level: int | None = None  # the level the helper set on root, if it did
    saved_levels: dict[str, tuple[int, int]] = field(default_factory=dict)  # name -> (before, set)
    removed_foreign: list = field(default_factory=list)
    config_guard: dict | None = None


def _is_foreign_otel_handler(handler: logging.Handler) -> bool:
    return isinstance(handler, (LoggingHandler, _SDKLoggingHandler)) and not isinstance(handler, HelperLogHandler)


def remove_foreign_otel_handlers() -> list:
    """Take foreign OTel handlers off root (warned); returns them for restoring.

    A second OTel handler would export every record twice — and, installed
    before the helper's providers, export the SDK's own install-time records.
    """
    foreign = [h for h in logging.getLogger().handlers if _is_foreign_otel_handler(h)]
    for h in foreign:
        logging.getLogger().removeHandler(h)
    if foreign:
        _log.warning(
            "[OtelHelper] Removed %d OpenTelemetry LoggingHandler(s) another component put on the root "
            "logger (opentelemetry-instrumentation-logging auto-instrumentation, e.g. "
            "`opentelemetry-instrument`): the helper owns log export, so records are exported once. "
            "Set OTEL_PYTHON_LOG_AUTO_INSTRUMENTATION=false (and leave "
            "OTEL_PYTHON_LOGGING_AUTO_INSTRUMENTATION_ENABLED unset) to silence this.", len(foreign))
    return foreign


@dataclass
class SetupPrep:
    """Logging state held while setup_telemetry() runs (begin/end_setup_logging)."""

    setup_last_resort: _console.SetupDiagnostics | None = None
    helper_level_set: bool = False
    removed_foreign: list = field(default_factory=list)
    done: bool = False


def begin_setup_logging() -> SetupPrep:
    """Before options are resolved: helper INFO diagnostics must not be lost.

    The ``otel_helper`` logger goes to INFO when the application left it NOTSET
    (otherwise PRD's WARNING root level would drop INFO diagnostics), and, while
    setup runs, the stdlib lastResort also prints INFO+ helper diagnostics.
    """
    prep = SetupPrep(setup_last_resort=_console.install_setup_diagnostics())
    if logging.getLogger(_HELPER_LOGGER).level == logging.NOTSET:
        logging.getLogger(_HELPER_LOGGER).setLevel(logging.INFO)
        prep.helper_level_set = True
    return prep


def end_setup_logging(prep: SetupPrep, keep: bool) -> None:
    """After setup: drop the setup-time lastResort; on failure also undo the rest.

    ``keep=True`` (successful setup): the removed foreign handlers and the helper
    logger level are owned by the attachment and restored by detach_logging().
    """
    if prep.done:
        return
    prep.done = True
    _console.uninstall_setup_diagnostics(prep.setup_last_resort)
    if keep:
        return
    for h in prep.removed_foreign:
        logging.getLogger().addHandler(h)
    if prep.helper_level_set and logging.getLogger(_HELPER_LOGGER).level == logging.INFO:
        logging.getLogger(_HELPER_LOGGER).setLevel(logging.NOTSET)


def attach_logging(handler: logging.Handler | None, level: int, prep: SetupPrep | None = None) -> LogAttachment:
    """Attach the helper's logging to the stdlib (design D6 level rule and A5 rulings).

    - A foreign OTel LoggingHandler on root is removed first (warned).
    - root level: set to ``level`` only when root has no handlers and still has
      the stdlib default WARNING; otherwise left alone.
    - ``handler`` given (a logs exporter is active): on root and uvicorn's loggers,
      kept there across ``basicConfig``/``dictConfig``/``fileConfig``; while the
      application configures no root handler, helper/SDK diagnostics also reach
      stderr through a root filter handler.
    - no handler: console fallback (stdout JSON) for application records, active
      while the application configures no handler of its own.
    """
    if prep is not None:  # the fallback/diagnostics handlers must wrap the stdlib lastResort
        _console.uninstall_setup_diagnostics(prep.setup_last_resort)
        prep.setup_last_resort = None
    state = LogAttachment(handler=handler)
    state.removed_foreign = prep.removed_foreign if prep is not None else remove_foreign_otel_handlers()
    if prep is not None and prep.helper_level_set:
        helper_level = min(level, logging.INFO)  # DEBUG environments keep helper DEBUG records
        logging.getLogger(_HELPER_LOGGER).setLevel(helper_level)
        state.saved_levels[_HELPER_LOGGER] = (logging.NOTSET, helper_level)
    unconfigured = not logging.getLogger().handlers
    if unconfigured and logging.getLogger().level == logging.WARNING:
        logging.getLogger().setLevel(level)
        state.root_level = level
    if level > logging.DEBUG:
        for name, quiet in _NOISE_LEVELS.items():
            before = logging.getLogger(name).level
            if before == logging.NOTSET:
                logging.getLogger(name).setLevel(quiet)
                state.saved_levels[name] = (before, quiet)
    if handler is None:
        if unconfigured:
            state.fallback = _console.install(level)
        return state
    if unconfigured:
        state.diagnostics = _console.diagnostics_handler((handler,))
        logging.getLogger().addHandler(state.diagnostics)
    logging.getLogger().addHandler(handler)
    _attach_extra_loggers(handler)
    state.config_guard = _guard_config_functions(state)
    return state


def detach_logging(state: LogAttachment) -> None:
    """Undo attach_logging(); levels the application changed since are kept."""
    if state.config_guard is not None:
        _unguard_config_functions(state.config_guard)
    if state.handler is not None:
        logging.getLogger().removeHandler(state.handler)
        for name in _EXTRA_LOGGERS:
            logging.getLogger(name).removeHandler(state.handler)
    if state.diagnostics is not None:
        logging.getLogger().removeHandler(state.diagnostics)
    if state.fallback is not None:
        _console.uninstall(state.fallback)
    for h in state.removed_foreign:
        logging.getLogger().addHandler(h)
    for name, (before, quiet) in state.saved_levels.items():
        if logging.getLogger(name).level == quiet:
            logging.getLogger(name).setLevel(before)
    if state.root_level is not None and logging.getLogger().level == state.root_level:
        logging.getLogger().setLevel(logging.WARNING)


def _attach_extra_loggers(handler: logging.Handler) -> None:
    for name in _EXTRA_LOGGERS:
        logging.getLogger(name).addHandler(handler)  # no-op when already there


def _guard_config_functions(state: LogAttachment) -> dict:
    """Keep the OTel handler attached across the stdlib config functions.

    ``basicConfig()`` would otherwise do nothing (root already has a handler) and
    ``dictConfig``/``fileConfig`` would drop it — uvicorn calls ``dictConfig``
    when it starts. The helper's root handlers are taken off for the call, so the
    application's configuration applies as if the helper weren't there; the OTel
    handler is put back afterwards (the approach of
    opentelemetry-instrumentation-logging), the stderr diagnostics handler only
    while root still has no application handler. Calls are serialized by a
    module lock. A reference captured before setup (``from logging.config
    import dictConfig``) bypasses the guard.
    """
    handler = state.handler
    guard = {"active": True, "originals": {
        "basicConfig": logging.basicConfig,
        "dictConfig": logging.config.dictConfig,
        "fileConfig": logging.config.fileConfig,
    }, "wrappers": {}}

    def wrap(original):
        def guarded(*args, **kwargs):
            with _config_lock:
                if not guard["active"]:
                    return original(*args, **kwargs)
                was_attached = handler in logging.getLogger().handlers
                diagnostics = state.diagnostics if state.diagnostics in logging.getLogger().handlers else None
                if was_attached:
                    logging.getLogger().removeHandler(handler)
                if diagnostics is not None:
                    logging.getLogger().removeHandler(diagnostics)
                try:
                    return original(*args, **kwargs)
                finally:
                    _reenable_diagnostic_loggers()
                    if guard["active"]:
                        if diagnostics is not None and not logging.getLogger().handlers:
                            logging.getLogger().addHandler(diagnostics)
                        if was_attached:
                            logging.getLogger().addHandler(handler)
                            _attach_extra_loggers(handler)

        guarded.__wrapped__ = original
        guarded.__doc__ = original.__doc__
        return guarded

    wrappers = {name: wrap(fn) for name, fn in guard["originals"].items()}
    guard["wrappers"] = wrappers
    logging.basicConfig = wrappers["basicConfig"]
    logging.config.dictConfig = wrappers["dictConfig"]
    logging.config.fileConfig = wrappers["fileConfig"]
    return guard


def _reenable_diagnostic_loggers() -> None:
    """dictConfig/fileConfig(disable_existing_loggers=True) would silence helper/SDK diagnostics."""
    for name, lg in list(logging.Logger.manager.loggerDict.items()):
        if isinstance(lg, logging.Logger) and _console.is_diagnostic(name):
            lg.disabled = False


def _unguard_config_functions(guard: dict) -> None:
    """Restore the stdlib functions; a wrapper someone else wrapped again turns inert."""
    with _config_lock:
        guard["active"] = False
        originals, wrappers = guard["originals"], guard["wrappers"]
        if logging.basicConfig is wrappers["basicConfig"]:
            logging.basicConfig = originals["basicConfig"]
        if logging.config.dictConfig is wrappers["dictConfig"]:
            logging.config.dictConfig = originals["dictConfig"]
        if logging.config.fileConfig is wrappers["fileConfig"]:
            logging.config.fileConfig = originals["fileConfig"]


def configure_logging(resource: Resource, options: TelemetryOptions) -> LoggerProvider | None:
    """Build the logging pipeline and attach it (tests and direct use)."""
    provider, handler, level = build_logging(resource, options)
    attach_logging(handler, level)
    return provider
