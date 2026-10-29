"""Main entry point — equivalent to AddOtelHelper()."""

from __future__ import annotations

import atexit
import logging
import os
import sys
import threading
import time
import uuid
import weakref
from dataclasses import dataclass, field

from opentelemetry import _logs, metrics, trace
from opentelemetry.propagate import set_global_textmap
from opentelemetry.sdk.resources import (
    TELEMETRY_SDK_LANGUAGE,
    TELEMETRY_SDK_NAME,
    TELEMETRY_SDK_VERSION,
    Resource,
)
from opentelemetry.sdk.version import __version__ as _sdk_version

from staffops_otel_helper._propagators import build_propagator
from staffops_otel_helper._resource_echo import suppress_sdk_resource_echo
from staffops_otel_helper._semconv import ensure_semconv_default
from staffops_otel_helper._diag import warn_once_per_process
from staffops_otel_helper.config import (
    ATTR_ENVIRONMENT,
    ATTR_INSTANCE_ID,
    ATTR_SERVICE_NAME,
    ENV_RESOURCE_DETECTORS,
    TRIM_CHARS,
    TelemetryOptions,
)
from staffops_otel_helper.tracing import build_tracer_provider
from staffops_otel_helper import metrics as _metrics_state
from staffops_otel_helper.metrics import build_metrics, stop_listener
from staffops_otel_helper.logging_setup import (
    attach_logging,
    begin_setup_logging,
    build_logging,
    build_logging_level,
    detach_logging,
    end_setup_logging,
    remove_foreign_otel_handlers,
)
from staffops_otel_helper.instrumentation import configure_instrumentations, uninstrument

_log = logging.getLogger("staffops_otel_helper.setup")
_initialized = False
# Serializes setup/shutdown; reentrant so a failing setup can roll back under it.
_lock = threading.RLock()
# The options returned by the first successful setup_telemetry() (P12).
_options: TelemetryOptions | None = None
# True once this process installed the global Tracer/MeterProvider. The OTel
# Python API sets each global provider once per process (set_tracer_provider /
# set_meter_provider ignore later calls and log "Overriding ... is not allowed"),
# so after shutdown_telemetry() a new setup cannot install fresh ones.
_globals_installed = False
# True once this process installed the global LoggerProvider (same set-once rule).
_logs_global_installed = False
# The first shutdown's RuntimeError, re-raised by every later shutdown call until
# the next successful setup (design "Shutdown result semantics").
_shutdown_error: RuntimeError | None = None
_DEFAULT_SHUTDOWN_TIMEOUT_MS = 30_000
_ROLLBACK_TIMEOUT_MS = 5_000


@dataclass
class _Pipeline:
    """What one setup_telemetry() built; close() undoes it."""

    tracer_provider: object = None
    meter_provider: object = None
    listener: object = None
    prometheus_reader: object = None
    logger_provider: object = None
    log_handler: logging.Handler | None = None
    log_level: int = logging.NOTSET
    log_attachment: object = None
    instrumented: list = field(default_factory=list)

    def close(self, timeout_ms: int) -> list[str]:
        """Undo the pipeline; returns the errors (never raises).

        Sequentially: uninstrument, detach the logging (OTel handler or console
        fallback) and close the handler, stop the listener. Then the three providers shut down concurrently, each flushing
        its exporters, under the single ``timeout_ms`` deadline.
        """
        errors = uninstrument(self.instrumented)
        if self.log_attachment is not None:
            detach_logging(self.log_attachment)
            self.log_attachment = None
        if self.log_handler is not None:
            self.log_handler.close()
            self.log_handler = None
        try:
            stop_listener(self.listener)
        except Exception as e:  # noqa: BLE001
            errors.append(f"metrics listener: {e}")
        self.listener = None
        jobs = []
        if self.logger_provider is not None:
            jobs.append(("logs", self.logger_provider.shutdown))
        if self.meter_provider is not None:
            mp = self.meter_provider
            jobs.append(("metrics", lambda: mp.shutdown(timeout_millis=timeout_ms)))
        if self.tracer_provider is not None:
            jobs.append(("traces", self.tracer_provider.shutdown))
        self.logger_provider = self.meter_provider = self.tracer_provider = None
        errors.extend(_run_with_deadline(jobs, timeout_ms))
        return errors


def _run_with_deadline(jobs, timeout_ms: int) -> list[str]:
    """Run the shutdown jobs concurrently under ONE deadline (not one per job).

    TracerProvider/LoggerProvider.shutdown() take no timeout and wait for their
    exporters; a job still running at the deadline is reported and left to finish
    on its daemon thread.
    """
    errors: list[str] = []
    results: dict[str, BaseException | None] = {}

    def run(name, fn):
        try:
            fn()
            results[name] = None
        except BaseException as e:  # noqa: BLE001 - reported, never raised from a thread
            results[name] = e

    threads = [(name, threading.Thread(target=run, args=(name, fn), daemon=True,
                                       name=f"otel-helper-shutdown-{name}")) for name, fn in jobs]
    for _, t in threads:
        t.start()
    end = time.monotonic() + max(timeout_ms, 0) / 1000
    for name, t in threads:
        t.join(max(0.0, end - time.monotonic()))
        if t.is_alive():
            errors.append(f"{name}: shutdown did not finish within {timeout_ms} ms")
        elif results.get(name) is not None:
            errors.append(f"{name}: {results[name]}")
    return errors


_pipeline: _Pipeline | None = None
# Set in a child process forked after the helper was imported (e.g. gunicorn
# --preload): the only fork the helper can detect (design D4).
_forked_after_import = False


# True when this process's service.instance.id is the helper's UUID (sdk 1.42–1.44):
# a fork inherits it unless setup runs again in the child.
_helper_instance_id = False


# Set by the at-fork hook when setup already ran in the parent with the helper's id;
# reported on the child's next helper call (logging inside the hook can deadlock on
# locks the parent's logging/OTel threads held at fork time).
_fork_warning_pending = False


def _mark_forked() -> None:
    """``os.register_at_fork(after_in_child=...)``: only sets flags, never logs or locks."""
    global _forked_after_import, _fork_warning_pending, _lock
    _forked_after_import = True
    if _initialized and _helper_instance_id:
        _fork_warning_pending = True
    # The parent may have held the lock while forking (another thread in setup or
    # shutdown); a plain assignment gives the child a usable one.
    _lock = threading.RLock()
    _metrics_state._active_lock = threading.Lock()


def emit_fork_warning() -> None:
    """Report a pre-fork setup once, from a normal (non-hook) call in the child."""
    global _fork_warning_pending
    if _fork_warning_pending:
        _fork_warning_pending = False
        _log.warning(
            "[OtelHelper] This worker was forked after setup_telemetry() ran in the parent (e.g. "
            "gunicorn --preload): every worker shares the parent's %s. Call setup_telemetry() only in "
            "each worker (post_fork), not in the preloaded module — a second call in a worker is a no-op.",
            ATTR_INSTANCE_ID,
        )


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_mark_forked)


# The semantic-conventions schema the helper's resource declares: the semconv version the
# helper targets, the same literal in every language (design D1 row "telemetry.sdk.* and the
# resource schema URL", 2026-10-29). It is a contract literal, not read from the installed
# semconv package, so on SDK 1.42-1.44 it may be newer than that package's newest schema.
# Bumping it is a contract change.
SCHEMA_URL = "https://opentelemetry.io/schemas/1.43.0"

# telemetry.sdk.* always come from the SDK; env and code values for these keys are ignored.
_TELEMETRY_SDK = {
    TELEMETRY_SDK_NAME: "opentelemetry",
    TELEMETRY_SDK_LANGUAGE: "python",
    TELEMETRY_SDK_VERSION: _sdk_version,
}

# Detector names that only name what the helper already does (env parse, instance id).
_BUILTIN_DETECTORS = frozenset({"otel", "service_instance"})


def _default_service_name() -> str:
    """The SDK default, ``unknown_service:<interpreter>`` (sdk >= 1.45 spelling, on every version)."""
    return f"unknown_service:{os.path.basename(sys.executable)}" if sys.executable else "unknown_service"


def _sdk_instance_id() -> str | None:
    """The SDK's per-process id (``ServiceInstanceIdResourceDetector``, sdk >= 1.45), else None."""
    try:
        from opentelemetry.sdk.resources import ServiceInstanceIdResourceDetector
    except ImportError:  # sdk 1.42-1.44
        return None
    value = ServiceInstanceIdResourceDetector().detect().attributes.get(ATTR_INSTANCE_ID)
    return value if isinstance(value, str) and value else None


def _warn_unused_detectors() -> None:
    """Design D1 detector-selection rule: the variable is not honored, warned once per process."""
    raw = os.environ.get(ENV_RESOURCE_DETECTORS) or ""
    names = {n.strip(TRIM_CHARS) for n in raw.split(",")} - {""}
    if names - _BUILTIN_DETECTORS:
        warn_once_per_process(
            _log, f"unused:{ENV_RESOURCE_DETECTORS}",
            "[OtelHelper] %s is ignored: the helper builds the resource itself (service identity, "
            "OTEL_RESOURCE_ATTRIBUTES, code attributes), also after a fork. Add host/process/cloud "
            "attributes in the Collector (resourcedetection, k8sattributes processors).",
            ENV_RESOURCE_DETECTORS)


def _warn_code_telemetry_sdk(options: TelemetryOptions) -> None:
    """A telemetry.sdk.* key set in code is ignored: warned once per process, naming the key only."""
    for key in sorted(k for k in options.resource_attributes if k in _TELEMETRY_SDK):
        warn_once_per_process(
            _log, f"code-telemetry-sdk:{key}",
            "[OtelHelper] resource_attributes[%r] is ignored: the OpenTelemetry SDK sets %s.", key, key)


def _build_resource(options: TelemetryOptions) -> Resource:
    """OTEL_RESOURCE_ATTRIBUTES (the helper's parse) < code attributes < resolved identity < telemetry.sdk.*.

    The SDK's ``Resource.create()`` is not used: its env detector would parse
    OTEL_RESOURCE_ATTRIBUTES again and log invalid items verbatim (R8). Identity
    (design D1, D4): ``service.name`` always set (SDK default when none was
    configured), ``deployment.environment.name`` absent when unset, and
    ``service.instance.id`` from code or env, else the SDK detector (sdk >= 1.45,
    refreshed after fork), else a per-process UUID.
    """
    _warn_unused_detectors()
    _warn_code_telemetry_sdk(options)
    identity = options._resource_overrides
    attributes: dict[str, object] = dict(options._env_resource)
    attributes.update(options.resource_attributes)
    attributes[ATTR_SERVICE_NAME] = identity.get(ATTR_SERVICE_NAME) or _default_service_name()
    environment = identity.get(ATTR_ENVIRONMENT)
    if environment is None:
        attributes.pop(ATTR_ENVIRONMENT, None)
    else:
        attributes[ATTR_ENVIRONMENT] = environment

    global _helper_instance_id
    provided = identity.get(ATTR_INSTANCE_ID)
    sdk_id = None if provided else _sdk_instance_id()
    _helper_instance_id = not provided and sdk_id is None
    attributes[ATTR_INSTANCE_ID] = provided or sdk_id or str(uuid.uuid4())
    if provided and _forked_after_import:
        _log.warning(
            "[OtelHelper] %s is set explicitly (code or OTEL_RESOURCE_ATTRIBUTES) in a forked worker "
            "process: every worker reports the same id. Leave it unset so each process gets its own.",
            ATTR_INSTANCE_ID,
        )
    attributes.update(_TELEMETRY_SDK)
    return Resource(attributes, SCHEMA_URL)


def _restore_resource(providers: list, resource: Resource, explicit_id: bool) -> None:
    """Put the helper's resource back on providers after the SDK's fork refresh (sdk >= 1.45).

    The SDK merges its process-dependent detectors (``ServiceInstanceIdResourceDetector``
    and any from ``OTEL_EXPERIMENTAL_RESOURCE_DETECTORS``) on top of the resource after a
    fork, which replaced an explicit ``service.instance.id`` and re-added detector
    attributes. The child keeps the helper's resource, with the SDK's fresh per-process
    id unless code or the env set one (design D4). Private SDK attributes, guarded by
    ``hasattr``; runs inside the fork hook, so it never logs and never raises.
    """
    desired = resource
    if not explicit_id:
        fresh = _sdk_instance_id()
        if fresh is not None:
            desired = Resource({**resource.attributes, ATTR_INSTANCE_ID: fresh}, resource.schema_url)
    for provider in providers:
        if provider is None:
            continue
        try:
            if hasattr(provider, "_sdk_config"):  # MeterProvider
                provider._sdk_config.resource = desired
            elif hasattr(provider, "_resource"):  # TracerProvider, LoggerProvider
                provider._resource = desired
            else:
                continue
            provider._update_resource(Resource.get_empty())  # pushes the resource to tracers/loggers
        except Exception:  # noqa: BLE001 - a fork hook must not raise; the SDK's own refresh stays
            pass


def _register_fork_restore(pipeline: _Pipeline, resource: Resource, explicit_id: bool) -> None:
    """Register :func:`_restore_resource` after the providers' own fork hooks (sdk >= 1.45).

    ``after_in_child`` hooks run in registration order, so this one runs after the SDK
    refresh. Providers are held weakly: a hook of a shut-down pipeline does nothing.
    """
    providers = [p for p in (pipeline.tracer_provider, pipeline.meter_provider, pipeline.logger_provider)
                 if p is not None and hasattr(type(p), "_handle_fork") and hasattr(p, "_update_resource")]
    if not providers or not hasattr(os, "register_at_fork"):
        return
    refs = [weakref.ref(p) for p in providers]

    def _after_in_child() -> None:
        _restore_resource([r() for r in refs], resource, explicit_id)

    os.register_at_fork(after_in_child=_after_in_child)


def setup_telemetry(options: TelemetryOptions | None = None) -> TelemetryOptions:
    """
    Configure all telemetry: tracing, metrics, logging, and auto-instrumentation.

    Call once at application startup. Equivalent to services.AddOtelHelper() in .NET.
    Further calls are no-ops that return the options of the first successful call
    (the ``options`` argument is ignored) until shutdown_telemetry() runs.

    Every signal is built before anything global is installed: if building fails
    (exporter error, /metrics port in use, ...) what was built is shut down, the
    previous propagator stays, the error propagates and a later call can retry.

    Args:
        options: Optional configuration. If None, uses env var defaults.

    Returns:
        The resolved options (useful for inspection/testing).

    Raises:
        ValueError: an option set in code is invalid. Invalid environment values
            only log a warning (``staffops_otel_helper`` logger) and fall back to the default.
        RuntimeError: a pipeline could not be built (e.g. the /metrics port is busy).
    """
    emit_fork_warning()
    # Before anything can initialize a contrib instrumentation (design D12).
    ensure_semconv_default()
    with _lock:
        if _initialized and _options is not None:
            return _options

        if options is None:
            options = TelemetryOptions()

        # Helper INFO diagnostics reach stderr during setup too (D6 A5 rulings).
        prep = begin_setup_logging()
        try:
            _setup_locked(options, prep)
        except BaseException:
            end_setup_logging(prep, keep=False)
            raise
        return options


def _setup_locked(options: TelemetryOptions, prep) -> None:
    """setup_telemetry() body, under _lock; sets the module state on success."""
    global _initialized, _options, _pipeline, _shutdown_error
    options.resolve_from_env()
    options.validate()
    if not options.sdk_disabled:
        # Before any provider is installed: a foreign OTel handler would export the
        # SDK's install-time records ("Overriding of current TracerProvider ...").
        prep.removed_foreign = remove_foreign_otel_handlers()

    options._setup_disabled.clear()
    pipeline = _Pipeline()
    try:
        propagator = build_propagator()
        if not options.sdk_disabled:
            _build_signals(pipeline, options)
            # Instrumentors resolve tracers/meters through the API proxies, so
            # they can be enabled before the providers are installed.
            configure_instrumentations(options, pipeline.instrumented)
    except BaseException:
        # Nothing was installed and nothing recorded yet: the providers stop quickly.
        _metrics_state.deactivate()
        for error in pipeline.close(_ROLLBACK_TIMEOUT_MS):
            _log.warning("[OtelHelper] setup rollback: %s", error)
        raise

    # ── install: nothing below is expected to fail ──
    set_global_textmap(propagator)
    if options.sdk_disabled:
        # OTEL_SDK_DISABLED=true: the API's no-op providers stay installed; no exporters,
        # no /metrics listener, no log handlers, no instrumentation (D1 edge-case rulings).
        _log.info("[OtelHelper] OTEL_SDK_DISABLED=true: telemetry disabled, only propagators installed.")
        end_setup_logging(prep, keep=False)
    else:
        _install(pipeline, options, prep)
        end_setup_logging(prep, keep=True)
    # 200-empty on the mounted /metrics only when Prometheus is off by choice;
    # off because of the D13 downgrade stays 503.
    _metrics_state.activate(pipeline.prometheus_reader,
                            deliberately_inactive="metrics" not in options._setup_disabled)
    _pipeline, _options, _initialized, _shutdown_error = pipeline, options, True, None


def _build_signals(pipeline: _Pipeline, options: TelemetryOptions) -> None:
    with suppress_sdk_resource_echo():
        _build_signals_quietly(pipeline, options)


def _build_signals_quietly(pipeline: _Pipeline, options: TelemetryOptions) -> None:
    resource = _build_resource(options)
    want_traces = options.is_signal_enabled("traces")
    want_metrics = options.is_signal_enabled("metrics") and bool(options.resolved_metric_exporters())
    if _globals_installed and (want_traces or want_metrics):
        _log.warning(
            "[OtelHelper] setup_telemetry() after shutdown_telemetry(): OpenTelemetry Python sets the "
            "global TracerProvider/MeterProvider only once per process, so traces and metrics stay off "
            "(the previous providers are shut down). Logs and propagators are set up again.")
        options._setup_disabled.update(s for s, on in (("traces", want_traces), ("metrics", want_metrics)) if on)
        want_traces = want_metrics = False
    if want_traces:
        pipeline.tracer_provider = build_tracer_provider(resource, options)
    if want_metrics:
        pipeline.meter_provider, pipeline.listener, pipeline.prometheus_reader = build_metrics(resource, options)
    if options.is_signal_enabled("logs"):
        pipeline.logger_provider, pipeline.log_handler, pipeline.log_level = build_logging(resource, options)
    else:
        pipeline.log_level = build_logging_level(options)
    _register_fork_restore(pipeline, resource, explicit_id=bool(options._resource_overrides.get(ATTR_INSTANCE_ID)))


def _install(pipeline: _Pipeline, options: TelemetryOptions, prep=None) -> None:
    global _globals_installed, _logs_global_installed
    if pipeline.tracer_provider is not None:
        trace.set_tracer_provider(pipeline.tracer_provider)
        _globals_installed = True
        if trace.get_tracer_provider() is not pipeline.tracer_provider:
            _log.warning("[OtelHelper] A global TracerProvider was already installed by other code; "
                         "the helper's traces pipeline is not active.")
            options._setup_disabled.add("traces")
    if pipeline.meter_provider is not None:
        metrics.set_meter_provider(pipeline.meter_provider)
        _globals_installed = True
        if metrics.get_meter_provider() is not pipeline.meter_provider:
            _log.warning("[OtelHelper] A global MeterProvider was already installed by other code; "
                         "the helper's metrics pipeline is not active.")
            pipeline.prometheus_reader = None
            options._setup_disabled.add("metrics")  # a conflict, not a choice: /metrics stays 503
    if pipeline.logger_provider is not None and not _logs_global_installed:
        # For code using the OTel Logs API directly (opentelemetry._logs), as Go does.
        # Set once per process like the other globals: after shutdown -> setup the
        # helper's handler uses its new provider, the API global stays the first one.
        _logs.set_logger_provider(pipeline.logger_provider)
        _logs_global_installed = True
        if _logs.get_logger_provider() is not pipeline.logger_provider:
            _log.warning("[OtelHelper] A global LoggerProvider was already installed by other code; the "
                         "helper's log handler still exports, OTel Logs API callers use the other provider.")
    # D6: OTel handler when a logs exporter is active, else the stdout console fallback.
    pipeline.log_attachment = attach_logging(pipeline.log_handler, pipeline.log_level, prep)


def shutdown_telemetry(timeout_ms: int = _DEFAULT_SHUTDOWN_TIMEOUT_MS) -> None:
    """Flush and shut down every provider the helper built (traces, metrics, logs).

    The first call does the work; later and concurrent calls (they wait for it)
    get the same result — None, or the same RuntimeError re-raised — until the
    next successful setup_telemetry(). It undoes the instrumentation, removes the
    helper's log handler, stops the standalone /metrics listener (``metrics_app()``
    then answers 503), resets the setup guard, and shuts the three providers down
    concurrently under one ``timeout_ms`` deadline.

    It also runs at interpreter exit (``atexit``) — which Python skips on an
    unhandled SIGTERM: handle SIGTERM in the application (see HOW-TO).

    A later setup_telemetry() works, but OpenTelemetry Python sets the global
    TracerProvider/MeterProvider only once per process, so it cannot install new
    ones: only logs and propagators come back (a warning says so; design D13).

    Raises:
        RuntimeError: one or more parts failed or timed out; the message joins
            every error. The helper state is reset regardless.
    """
    global _initialized, _options, _pipeline, _shutdown_error
    with _lock:
        pipeline, _pipeline = _pipeline, None
        _initialized, _options = False, None
        if pipeline is None:
            if _shutdown_error is not None:
                raise _shutdown_error.with_traceback(None)  # same object, no growing traceback
            return
        # Stop advertising /metrics before the (possibly slow) flush, as Go does.
        _metrics_state.deactivate()
        errors = pipeline.close(timeout_ms)
        if errors:
            _shutdown_error = RuntimeError("[OtelHelper] shutdown_telemetry: " + "; ".join(errors))
            raise _shutdown_error


def _shutdown_at_exit() -> None:
    if _pipeline is None:
        return  # never set up, or already shut down explicitly (its error was raised there)
    try:
        shutdown_telemetry()
    except Exception as e:  # noqa: BLE001 - interpreter exit: report, never raise
        _log.warning("%s", e)


atexit.register(_shutdown_at_exit)


def reset_telemetry() -> None:
    """Reset global state. For testing only.

    Shuts down the current pipeline (short deadline, errors ignored) and forgets
    that global providers were installed — the caller is expected to reset the
    OTel API globals itself, as the test suite's conftest does.
    """
    global _globals_installed, _logs_global_installed, _shutdown_error
    try:
        shutdown_telemetry(timeout_ms=200)
    except RuntimeError:
        pass
    _globals_installed = _logs_global_installed = False
    _shutdown_error = None
