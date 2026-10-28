"""Logging (task A5: P10, P16, design D6).

Handler decisions depend on what the root logger has at setup time, and pytest's
logging plugin puts its own handlers on root, so the behavioral cases run in a
fresh interpreter (``_run``); unit-level checks run in-process.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import textwrap

import pytest

from opentelemetry import _logs

import otel_helper.setup as setup_mod
from otel_helper import _console, setup_telemetry, shutdown_telemetry
from otel_helper.config import TelemetryOptions
from otel_helper.logging_setup import HelperLogHandler, build_logging
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk.resources import Resource

_PRELUDE = """
import os
if os.environ.get("PARITY_COVERAGE_DATA"):
    import coverage
    _cov = coverage.Coverage(data_file=os.environ["PARITY_COVERAGE_DATA"], data_suffix=True,
                             source=["otel_helper"], config_file=False)
    _cov.start()
    import atexit
    atexit.register(lambda: (_cov.stop(), _cov.save()))
import json, logging, logging.config, sys
"""


def _run(body: str, **env: str) -> subprocess.CompletedProcess:
    full_env = {k: v for k, v in os.environ.items()
                if not (k.startswith("OTEL_") or k in ("SERVICE_NAME", "ENVIRONMENT"))}
    full_env.update({"OTEL_SERVICE_NAME": "a5", "OTEL_HELPER_METRICS_PORT": "0",
                     "OTEL_TRACES_EXPORTER": "none", "OTEL_METRICS_EXPORTER": "none"})
    full_env.update(env)
    if os.environ.get("PARITY_COVERAGE_DATA"):
        full_env["PARITY_COVERAGE_DATA"] = os.environ["PARITY_COVERAGE_DATA"]
    proc = subprocess.run([sys.executable, "-c", _PRELUDE + textwrap.dedent(body)], env=full_env,
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return proc


class TestNoLogsExporter:
    """P10 / D6: no OTel handler; application records reach stdout."""

    def test_no_otel_handler_and_records_on_stdout(self):
        proc = _run("""
            from otel_helper import setup_telemetry
            setup_telemetry()
            print("HANDLERS", logging.getLogger().handlers, flush=True)
            logging.getLogger("app").info("HELLO-INFO")
            logging.getLogger("app").debug("HELLO-DEBUG")
        """, ENVIRONMENT="DEV")
        assert "HANDLERS []" in proc.stdout
        assert "HELLO-INFO" in proc.stdout and "HELLO-INFO" not in proc.stderr
        assert "HELLO-DEBUG" not in proc.stdout  # DEV -> INFO (R4)

    def test_logs_exporter_none_with_endpoint(self):
        proc = _run("""
            from otel_helper import setup_telemetry
            setup_telemetry()
            print("HANDLERS", logging.getLogger().handlers, flush=True)
            logging.getLogger("app").warning("HELLO-WARN")
        """, OTEL_EXPORTER_OTLP_ENDPOINT="http://127.0.0.1:9", OTEL_LOGS_EXPORTER="none", ENVIRONMENT="PRD")
        assert "HANDLERS []" in proc.stdout
        assert "HELLO-WARN" in proc.stdout

    def test_helper_diagnostics_stay_on_stderr(self):
        proc = _run("""
            from otel_helper import setup_telemetry
            setup_telemetry()  # no environment -> the helper warns
            logging.getLogger("opentelemetry.sdk").warning("SDK-DIAG")
        """)
        assert "deployment.environment.name is not set" in proc.stderr
        assert "deployment.environment.name" not in proc.stdout
        assert "SDK-DIAG" in proc.stderr and "SDK-DIAG" not in proc.stdout

    def test_basic_config_after_setup_works(self):
        proc = _run("""
            from otel_helper import setup_telemetry
            setup_telemetry()
            logging.basicConfig(format="BC %(message)s", stream=sys.stdout, level=logging.INFO)
            print("COUNT", len(logging.getLogger().handlers), flush=True)
            logging.getLogger("app").info("AFTER")
        """, ENVIRONMENT="DEV")
        assert "COUNT 1" in proc.stdout
        assert proc.stdout.count("AFTER") == 1 and "BC AFTER" in proc.stdout  # fallback stepped aside

    def test_app_configured_root_is_left_alone(self):
        proc = _run("""
            h = logging.StreamHandler(sys.stdout)
            h.setFormatter(logging.Formatter("MINE %(message)s"))
            logging.getLogger().addHandler(h)
            from otel_helper import setup_telemetry
            setup_telemetry()
            print("LEVEL", logging.getLogger().level, "N", len(logging.getLogger().handlers), flush=True)
            logging.getLogger("app").warning("W")
        """, ENVIRONMENT="LOCAL")
        assert "LEVEL 30 N 1" in proc.stdout  # root had a handler: level untouched
        assert proc.stdout.count("W\n") == 1 and "MINE W" in proc.stdout

    @pytest.mark.parametrize("env,expected", [
        ({"ENVIRONMENT": "LOCAL"}, logging.DEBUG),
        ({"ENVIRONMENT": "DEV"}, logging.INFO),
        ({"ENVIRONMENT": "PRD"}, logging.WARNING),
        ({}, logging.INFO),                       # unset -> INFO (R4)
        ({"ENVIRONMENT": "qa-7"}, logging.INFO),  # unknown -> INFO (R4)
    ])
    def test_root_level_by_environment(self, env, expected):
        proc = _run("""
            from otel_helper import setup_telemetry
            setup_telemetry()
            print("LEVEL", logging.getLogger().level, flush=True)
        """, **env)
        assert f"LEVEL {expected}" in proc.stdout

    def test_custom_root_level_is_left_alone(self):
        proc = _run("""
            logging.getLogger().setLevel(logging.ERROR)
            from otel_helper import setup_telemetry
            setup_telemetry()
            print("LEVEL", logging.getLogger().level, flush=True)
        """, ENVIRONMENT="LOCAL")
        assert "LEVEL 40" in proc.stdout

    def test_shutdown_restores_stdlib_state(self):
        proc = _run("""
            stdlib = (logging.lastResort, logging.basicConfig, logging.config.dictConfig)
            from otel_helper import setup_telemetry, shutdown_telemetry
            setup_telemetry()
            changed = logging.lastResort is not stdlib[0]
            shutdown_telemetry()
            print("CHANGED", changed, "RESTORED",
                  (logging.lastResort, logging.basicConfig, logging.config.dictConfig) == stdlib,
                  "LEVEL", logging.getLogger().level, flush=True)
        """, ENVIRONMENT="DEV")
        assert "CHANGED True RESTORED True LEVEL 30" in proc.stdout


    def test_stdout_fallback_is_single_line_json(self):
        proc = _run("""
            from otel_helper import setup_telemetry
            setup_telemetry()
            log = logging.getLogger("app.orders")
            log.warning("W %s", 1, extra={"order_id": "o-1"})
            log.critical("C")
            try:
                1 / 0
            except ZeroDivisionError:
                log.exception("boom")
        """, ENVIRONMENT="DEV")
        lines = [json.loads(line) for line in proc.stdout.splitlines()]
        assert [entry["level"] for entry in lines] == ["WARN", "ERROR", "ERROR"]
        first = lines[0]
        assert first["msg"] == "W 1" and first["logger"] == "app.orders" and first["order_id"] == "o-1"
        assert set(first) == {"time", "level", "msg", "logger", "order_id"}
        import re

        assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}(Z|[+-]\d\d:\d\d)", first["time"])
        assert first["time"].endswith("Z")  # the test container runs in UTC
        assert "ZeroDivisionError" in lines[2]["exception"] and "Traceback" in lines[2]["exception"]

    def test_info_helper_diagnostic_reaches_stderr_in_prd(self):
        proc = _run("""
            from otel_helper import setup_telemetry
            setup_telemetry()
        """, ENVIRONMENT="PRD", OTEL_EXPORTER_OTLP_ENDPOINT="http://127.0.0.1:4318", OTEL_LOGS_EXPORTER="none")
        assert "inferred as http/protobuf" in proc.stderr

    def test_foreign_handler_removed_before_providers_are_installed(self):
        # opentelemetry-instrument model: its own providers and LoggingHandler exist
        # before setup; the helper's install-time SDK records must not be exported.
        proc = _run("""
            from opentelemetry import _logs, trace
            from opentelemetry.sdk._logs import LoggerProvider
            from opentelemetry.sdk._logs.export import ConsoleLogRecordExporter, SimpleLogRecordProcessor
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.instrumentation.logging import LoggingInstrumentor
            lp = LoggerProvider()
            lp.add_log_record_processor(SimpleLogRecordProcessor(ConsoleLogRecordExporter()))
            _logs.set_logger_provider(lp)
            trace.set_tracer_provider(TracerProvider())
            LoggingInstrumentor().instrument()
            from otel_helper import setup_telemetry
            setup_telemetry()
        """, OTEL_LOGS_EXPORTER="console", OTEL_TRACES_EXPORTER="console", ENVIRONMENT="DEV")
        assert "Overriding of current TracerProvider" in proc.stderr
        assert "Overriding" not in proc.stdout  # not exported by the foreign handler

    def test_foreign_otel_handler_removed_and_fallback_still_works(self):
        proc = _run("""
            from opentelemetry.instrumentation.logging import LoggingInstrumentor
            LoggingInstrumentor().instrument()  # what opentelemetry-instrument does by default
            from otel_helper import setup_telemetry, shutdown_telemetry
            setup_telemetry()
            print("HANDLERS", logging.getLogger().handlers, flush=True)
            logging.getLogger("app").warning("STILL-HERE")
            shutdown_telemetry()
            print("RESTORED", len(logging.getLogger().handlers), flush=True)
        """, ENVIRONMENT="DEV")
        assert "HANDLERS []" in proc.stdout and "STILL-HERE" in proc.stdout
        assert "OTEL_PYTHON_LOG_AUTO_INSTRUMENTATION=false" in proc.stderr
        assert "RESTORED 1" in proc.stdout


class TestLogsExporterActive:
    def test_handler_on_root_and_kept_across_config_functions(self):
        proc = _run("""
            from otel_helper import setup_telemetry
            from otel_helper.logging_setup import HelperLogHandler
            setup_telemetry()
            otel = lambda: [h for h in logging.getLogger().handlers if isinstance(h, HelperLogHandler)]
            before = len(otel())
            logging.basicConfig(stream=sys.stdout, format="BC %(message)s")  # must still configure
            n_basic = len(logging.getLogger().handlers)
            logging.config.dictConfig({"version": 1, "root": {"handlers": []}})  # would drop it
            print("RESULT", before, n_basic, len(otel()), flush=True)
        """, OTEL_LOGS_EXPORTER="console", ENVIRONMENT="DEV")
        assert "RESULT 1 2 1" in proc.stdout  # app handler + OTel handler (diagnostics handler stepped aside)

    def test_uvicorn_loggers_reach_the_otel_handler_once(self):
        """P16: uvicorn loggers (propagate=False) reach the OTel handler."""
        # uvicorn's default LOGGING_CONFIG (propagate=False), applied after setup as
        # uvicorn.run() does; the console logs exporter prints one JSON per record.
        proc = _run("""
            from otel_helper import setup_telemetry, shutdown_telemetry
            setup_telemetry()
            logging.config.dictConfig({
                "version": 1, "disable_existing_loggers": False,
                "handlers": {"default": {"class": "logging.StreamHandler", "stream": "ext://sys.stderr"}},
                "loggers": {
                    "uvicorn": {"handlers": ["default"], "level": "INFO", "propagate": False},
                    "uvicorn.error": {"level": "INFO"},
                    "uvicorn.access": {"handlers": ["default"], "level": "INFO", "propagate": False},
                },
            })
            logging.getLogger("uvicorn.access").info("ACCESS-LINE")
            logging.getLogger("uvicorn.error").info("ERROR-LINE")
            logging.getLogger("app").info("APP-LINE")
            shutdown_telemetry()
        """, OTEL_LOGS_EXPORTER="console", ENVIRONMENT="DEV")
        for line in ("ACCESS-LINE", "ERROR-LINE", "APP-LINE"):
            assert proc.stdout.count(f'"body": "{line}"') == 1, (line, proc.stdout)

    def test_diagnostics_reach_stderr_and_are_never_exported(self):
        # Dead OTLP endpoint + console exporter: what the OTel handler exports shows
        # up on stdout as JSON with a "body"; diagnostics must be on stderr only
        # (opentelemetry.*) or on stderr and exported (otel_helper.*), no loop.
        proc = _run("""
            from otel_helper import setup_telemetry, shutdown_telemetry
            setup_telemetry()
            logging.getLogger("app").warning("APP-RECORD")
            logging.getLogger("otel_helper.test").warning("HELPER-DIAG")
            logging.getLogger("opentelemetry.sdk.test").warning("SDK-DIAG")
            try:
                shutdown_telemetry(timeout_ms=5000)
            except RuntimeError:
                pass
        """, OTEL_EXPORTER_OTLP_LOGS_ENDPOINT="http://127.0.0.1:9", OTEL_LOGS_EXPORTER="otlp,console",
            OTEL_EXPORTER_OTLP_TIMEOUT="1000", ENVIRONMENT="DEV")
        assert "HELPER-DIAG" in proc.stderr and "SDK-DIAG" in proc.stderr
        assert "APP-RECORD" not in proc.stderr
        assert proc.stdout.count('"body": "APP-RECORD"') == 1
        assert proc.stdout.count('"body": "HELPER-DIAG"') == 1
        assert "SDK-DIAG" not in proc.stdout
        assert "127.0.0.1:9" in proc.stderr  # the SDK's export-failure warning
        assert proc.stdout.count('"body"') == 2  # bounded: no export-failure records exported

    def test_diagnostics_handler_steps_aside_when_app_configures_root(self):
        proc = _run("""
            from otel_helper import setup_telemetry
            setup_telemetry()
            logging.basicConfig(stream=sys.stdout, format="BC %(name)s %(message)s")
            logging.getLogger("otel_helper.test").warning("ONCE")
            print("N", len(logging.getLogger().handlers), flush=True)
        """, OTEL_LOGS_EXPORTER="console", ENVIRONMENT="DEV")
        assert "BC otel_helper.test ONCE" in proc.stdout and "ONCE" not in proc.stderr
        assert "N 2" in proc.stdout  # app StreamHandler + OTel handler, no diagnostics handler

    def test_diagnostic_loggers_reenabled_after_dict_config(self):
        proc = _run("""
            pre = [logging.getLogger(n) for n in ("otel_helper.pre", "opentelemetry.pre", "app.pre")]
            from otel_helper import setup_telemetry
            setup_telemetry()
            logging.config.dictConfig({"version": 1})  # disable_existing_loggers defaults to True
            print("DISABLED", [lg.disabled for lg in pre], flush=True)
        """, OTEL_LOGS_EXPORTER="console", ENVIRONMENT="DEV")
        assert "DISABLED [False, False, True]" in proc.stdout

    def test_otlp_and_console_both(self, monkeypatch):
        import otel_helper.logging_setup as ls
        from opentelemetry.sdk._logs.export import SimpleLogRecordProcessor

        exporters = []
        monkeypatch.setattr(ls, "BatchLogRecordProcessor",
                            lambda exp: (exporters.append(type(exp).__name__), SimpleLogRecordProcessor(exp))[1])
        opts = TelemetryOptions(service_name="s", logs_endpoint="http://127.0.0.1:9")
        os.environ["OTEL_LOGS_EXPORTER"] = "otlp,console"
        opts.resolve_from_env()
        provider, handler, _ = build_logging(Resource.create({}), opts)
        try:
            assert sorted(exporters) == ["ConsoleLogRecordExporter", "OTLPLogExporter"]
            assert isinstance(handler, HelperLogHandler)
        finally:
            provider.shutdown()

    def test_set_logger_provider_for_logs_api_users(self):
        os.environ["OTEL_LOGS_EXPORTER"] = "console"
        setup_telemetry(TelemetryOptions(service_name="s", metric_exporters=["none"]))
        assert _logs.get_logger_provider() is setup_mod._pipeline.logger_provider
        shutdown_telemetry()

    def test_no_provider_without_exporter(self):
        setup_telemetry(TelemetryOptions(service_name="s", metric_exporters=["none"]))
        assert setup_mod._pipeline.logger_provider is None and setup_mod._pipeline.log_handler is None
        assert not isinstance(_logs.get_logger_provider(), LoggerProvider)

    def test_code_attributes_follow_upstream_env(self):
        opts = TelemetryOptions(service_name="s")
        os.environ["OTEL_LOGS_EXPORTER"] = "console"
        opts.resolve_from_env()
        for raw, expected in ((None, False), ("true", True)):
            if raw is not None:
                os.environ["OTEL_PYTHON_LOG_CODE_ATTRIBUTES"] = raw
            provider, handler, _ = build_logging(Resource.create({}), opts)
            assert handler._log_code_attributes is expected
            provider.shutdown()


class TestUnits:
    def test_handler_emits_a_record_once(self):
        handler = HelperLogHandler(logging.INFO, LoggerProvider(), False)
        emitted = []
        handler.emit = emitted.append
        record = logging.LogRecord("x", logging.INFO, __file__, 1, "m", None, None)
        assert handler.handle(record) is True
        assert handler.handle(record) is False
        handler.handle(logging.LogRecord("x", logging.INFO, __file__, 1, "m", None, None))
        assert len(emitted) == 2

    def test_console_fallback_routes_diagnostics_to_previous_last_resort(self, capsys):
        seen = []

        class Prev(logging.Handler):
            def emit(self, record):
                seen.append(record.name)

        fallback = _console.ConsoleFallback(logging.INFO, Prev(logging.WARNING))
        mk = lambda name, lvl: logging.LogRecord(name, lvl, __file__, 1, "msg-" + name, None, None)  # noqa: E731
        fallback.handle(mk("otel_helper.config", logging.WARNING))
        fallback.handle(mk("opentelemetry.sdk", logging.INFO))  # INFO+ (A5 rulings)
        fallback.handle(mk("opentelemetry.sdk", logging.DEBUG))
        fallback.handle(mk("app", logging.INFO))
        assert seen == ["otel_helper.config", "opentelemetry.sdk"]
        assert "msg-app" in capsys.readouterr().out
        assert _console.ConsoleFallback(logging.INFO, None).handle(mk("otel_helper", logging.ERROR)) is False

    def test_handler_never_exports_sdk_diagnostics(self):
        handler = HelperLogHandler(logging.INFO, LoggerProvider(), False)
        emitted = []
        handler.emit = emitted.append
        for name in ("opentelemetry", "opentelemetry.exporter.otlp", "otel_helper.x", "opentelemetryx"):
            handler.handle(logging.LogRecord(name, logging.WARNING, __file__, 1, "m", None, None))
        assert [r.name for r in emitted] == ["otel_helper.x", "opentelemetryx"]

    def test_handler_skips_records_from_the_log_export_thread(self):
        handler = HelperLogHandler(logging.DEBUG, LoggerProvider(), False)
        emitted = []
        handler.emit = emitted.append
        record = logging.LogRecord("urllib3", logging.DEBUG, __file__, 1, "m", None, None)
        record.threadName = "OtelBatchLogRecordProcessor"
        assert handler.handle(record) is False and emitted == []

    def test_setup_last_resort_routes_like_stdlib_plus_helper_info(self):
        seen = []

        class Prev(logging.Handler):
            def emit(self, record):
                seen.append((record.name, record.levelno))

        h = _console.SetupDiagnostics(Prev(logging.WARNING))
        mk = lambda name, lvl: logging.LogRecord(name, lvl, __file__, 1, "m", None, None)  # noqa: E731
        h.handle(mk("otel_helper.x", logging.INFO))
        h.handle(mk("opentelemetry.x", logging.INFO))
        h.handle(mk("app", logging.INFO))
        h.handle(mk("app", logging.WARNING))
        assert seen == [("otel_helper.x", logging.INFO), ("app", logging.WARNING)]
        assert _console.SetupDiagnostics(None).handle(mk("app", logging.ERROR)) is False

    def test_diagnostics_handler_ignores_app_records(self):
        handler = _console.DiagnosticsToStderr(None, ())
        assert handler.handle(logging.LogRecord("app", logging.ERROR, __file__, 1, "m", None, None)) is False

    def test_uninstall_keeps_a_replacement(self):
        original = logging.lastResort
        fallback = _console.install(logging.INFO)
        other = logging.StreamHandler()
        logging.lastResort = other
        _console.uninstall(fallback)
        assert logging.lastResort is other
        logging.lastResort = original

    def test_config_guard_inert_after_detach_when_rewrapped(self):
        from otel_helper.logging_setup import attach_logging, detach_logging

        handler = HelperLogHandler(logging.INFO, LoggerProvider(), False)
        state = attach_logging(handler, logging.INFO)
        ours = logging.basicConfig
        logging.basicConfig = lambda *a, **k: ours(*a, **k)  # someone wraps after the helper
        detach_logging(state)
        assert handler not in logging.getLogger().handlers
        logging.basicConfig(force=False)  # our wrapper is inert, does not re-add the handler
        assert handler not in logging.getLogger().handlers
