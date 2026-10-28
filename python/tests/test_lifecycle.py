"""Setup/shutdown lifecycle (P12, task A6; mirrors Go G1/G2).

- setup builds every signal before installing anything global; a failure at any
  stage shuts down what was built and leaves no global state behind;
- a second setup returns the first resolved options;
- shutdown_telemetry() flushes and shuts down every provider, once, under one
  deadline, joining errors; a later setup works.
"""

import logging
import os
import socket
import threading
import time

import pytest
from starlette.testclient import TestClient

from opentelemetry import metrics, trace
from opentelemetry.propagate import get_global_textmap
from opentelemetry.instrumentation.logging.handler import LoggingHandler
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import otel_helper.setup as setup_mod
from otel_helper import metrics_app, setup_telemetry, shutdown_telemetry
from otel_helper.config import TelemetryOptions


def _opts(**kwargs) -> TelemetryOptions:
    kwargs.setdefault("service_name", "lifecycle-svc")
    kwargs.setdefault("metric_exporters", ["prometheus"])
    kwargs.setdefault("prometheus_metrics_port", 0)
    # A logs exporter, so a LoggerProvider and the OTel handler are built (P10:
    # with none, the helper attaches no OTel handler). Console: no network.
    os.environ.setdefault("OTEL_LOGS_EXPORTER", "console")
    return TelemetryOptions(**kwargs)


def _helper_handlers() -> list:
    return [h for h in logging.getLogger().handlers if isinstance(h, LoggingHandler)]


def _nothing_global_installed() -> None:
    assert isinstance(trace.get_tracer_provider(), trace.ProxyTracerProvider)
    assert not isinstance(metrics.get_meter_provider(), MeterProvider)
    assert _helper_handlers() == []
    assert setup_mod._initialized is False
    assert setup_mod._pipeline is None


def _scrape_status() -> int:
    return TestClient(metrics_app()).get("/").status_code


def _spy_shutdowns(pipeline, behaviors=None) -> dict:
    """Wrap each provider's shutdown with a call counter (optionally raising/sleeping)."""
    calls = {}
    behaviors = behaviors or {}
    for name in ("tracer_provider", "meter_provider", "logger_provider"):
        provider = getattr(pipeline, name)
        original = provider.shutdown
        calls[name] = 0

        def spy(*args, _name=name, _original=original, **kwargs):
            calls[_name] += 1
            action = behaviors.get(_name)
            if isinstance(action, BaseException):
                _original(*args, **kwargs)
                raise action
            if isinstance(action, (int, float)):
                time.sleep(action)
            return _original(*args, **kwargs)

        provider.shutdown = spy
    return calls


class TestSecondSetup:
    def test_returns_first_resolved_options(self):
        first = setup_telemetry(_opts(service_name="first"))
        second = setup_telemetry(_opts(service_name="second"))
        assert second is first
        assert second.service_name == "first"

    def test_second_call_without_options_returns_first(self):
        first = setup_telemetry(_opts(service_name="first"))
        assert setup_telemetry() is first


class TestFailedSetupLeavesNoState:
    @pytest.mark.parametrize("stage", [
        "build_propagator", "build_tracer_provider", "build_metrics", "build_logging",
        "configure_instrumentations",
    ])
    def test_failure_at_each_stage(self, stage, monkeypatch):
        shut = {}  # provider name -> shutdown calls, for every provider built before the failure
        for name in ("build_tracer_provider", "build_metrics", "build_logging"):
            real = getattr(setup_mod, name)

            def recording(*args, _real=real, _name=name, **kwargs):
                result = _real(*args, **kwargs)
                provider = result[0] if isinstance(result, tuple) else result
                original = provider.shutdown
                shut[_name] = 0

                def spy(*a, **kw):
                    shut[_name] += 1
                    return original(*a, **kw)

                provider.shutdown = spy
                return result

            monkeypatch.setattr(setup_mod, name, recording)

        def boom(*args, **kwargs):
            raise RuntimeError(f"injected {stage} failure")

        monkeypatch.setattr(setup_mod, stage, boom)
        textmap = get_global_textmap()
        root_level = logging.getLogger().level

        with pytest.raises(RuntimeError, match=f"injected {stage}"):
            setup_telemetry(_opts())

        _nothing_global_installed()
        assert get_global_textmap() is textmap
        assert logging.getLogger().level == root_level
        assert _scrape_status() == 503
        # Everything built before the failure was shut down, exactly once.
        order = ["build_propagator", "build_tracer_provider", "build_metrics", "build_logging",
                 "configure_instrumentations"]
        expected = {n for n in order[1:order.index(stage)]}
        if stage == "configure_instrumentations":
            expected = {"build_tracer_provider", "build_metrics", "build_logging"}
        assert set(shut) == expected
        assert set(shut.values()) <= {1}
        monkeypatch.undo()
        # A retry after the cause is fixed installs the pipeline.
        setup_telemetry(_opts())
        assert not isinstance(trace.get_tracer_provider(), trace.ProxyTracerProvider)
        assert _scrape_status() == 200

    def test_busy_metrics_port_installs_nothing_and_retry_works(self):
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        blocker.bind(("0.0.0.0", 0))
        try:
            with pytest.raises(RuntimeError, match="metrics_app"):
                setup_telemetry(_opts(prometheus_metrics_port=blocker.getsockname()[1]))
        finally:
            blocker.close()
        _nothing_global_installed()
        assert _scrape_status() == 503
        opts = setup_telemetry(_opts())
        assert setup_mod._options is opts
        assert _scrape_status() == 200

    def test_invalid_code_option_installs_nothing(self):
        with pytest.raises(ValueError):
            setup_telemetry(_opts(sample_ratio=2.0))
        _nothing_global_installed()


class TestShutdown:
    def test_flushes_and_shuts_down_every_provider(self):
        setup_telemetry(_opts())
        exporter = InMemorySpanExporter()
        trace.get_tracer_provider().add_span_processor(BatchSpanProcessor(exporter, schedule_delay_millis=60_000))
        trace.get_tracer("t").start_span("pending").end()
        calls = _spy_shutdowns(setup_mod._pipeline)
        assert _scrape_status() == 200

        shutdown_telemetry()

        assert [s.name for s in exporter.get_finished_spans()] == ["pending"]
        assert calls == {"tracer_provider": 1, "meter_provider": 1, "logger_provider": 1}
        assert _helper_handlers() == []
        assert _scrape_status() == 503
        assert setup_mod._initialized is False

    def test_stops_standalone_listener(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        setup_telemetry(_opts(prometheus_metrics_port=port))
        shutdown_telemetry()
        with socket.socket() as s:
            s.bind(("0.0.0.0", port))  # the port is free again

    def test_restores_log_levels(self):
        logging.getLogger().setLevel(logging.ERROR)
        logging.getLogger("grpc").setLevel(logging.NOTSET)
        setup_telemetry(_opts())
        assert logging.getLogger("grpc").level == logging.ERROR
        shutdown_telemetry()
        assert logging.getLogger().level == logging.ERROR
        assert logging.getLogger("grpc").level == logging.NOTSET

    def test_idempotent(self):
        setup_telemetry(_opts())
        calls = _spy_shutdowns(setup_mod._pipeline)
        shutdown_telemetry()
        shutdown_telemetry()
        assert set(calls.values()) == {1}

    def test_without_setup_is_noop(self):
        shutdown_telemetry()

    def test_concurrent_calls_shut_down_once(self):
        setup_telemetry(_opts())
        calls = _spy_shutdowns(setup_mod._pipeline, {"tracer_provider": 0.2})
        errors = []

        def call():
            try:
                shutdown_telemetry()
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=call) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
        assert set(calls.values()) == {1}

    def test_joins_errors_and_still_resets(self):
        setup_telemetry(_opts())
        _spy_shutdowns(setup_mod._pipeline, {"tracer_provider": ValueError("trace boom"),
                                             "logger_provider": ValueError("logs boom")})
        with pytest.raises(RuntimeError) as info:
            shutdown_telemetry()
        assert "traces: trace boom" in str(info.value)
        assert "logs: logs boom" in str(info.value)
        assert setup_mod._initialized is False and setup_mod._pipeline is None
        with pytest.raises(RuntimeError) as again:
            shutdown_telemetry()  # same result until the next setup
        assert again.value is info.value

    def test_one_deadline_for_all_providers(self):
        setup_telemetry(_opts())
        _spy_shutdowns(setup_mod._pipeline, {"tracer_provider": 1.0, "logger_provider": 1.0})
        start = time.monotonic()
        with pytest.raises(RuntimeError, match="did not finish within 100 ms"):
            shutdown_telemetry(timeout_ms=100)
        assert time.monotonic() - start < 0.8

    def test_at_exit_hook_logs_instead_of_raising(self, caplog):
        setup_telemetry(_opts())
        _spy_shutdowns(setup_mod._pipeline, {"meter_provider": ValueError("exit boom")})
        with caplog.at_level(logging.WARNING, logger="otel_helper"):
            setup_mod._shutdown_at_exit()
        assert "exit boom" in caplog.text


class TestSetupAfterShutdown:
    def test_builds_a_fresh_pipeline(self, caplog):
        first = setup_telemetry(_opts(service_name="first"))
        old_handler = _helper_handlers()[0]
        shutdown_telemetry()
        with caplog.at_level(logging.WARNING, logger="otel_helper"):
            second = setup_telemetry(_opts(service_name="second"))
        assert second is not first and second.service_name == "second"
        handlers = _helper_handlers()
        assert len(handlers) == 1 and handlers[0] is not old_handler
        # The OTel API sets global providers once per process: documented + warned.
        assert "only once per process" in caplog.text
        assert setup_mod._pipeline.tracer_provider is None
        assert setup_mod._pipeline.meter_provider is None

    def test_after_failed_setup_globals_are_still_installable(self):
        with pytest.raises(ValueError):
            setup_telemetry(_opts(sample_ratio=-1.0))
        setup_telemetry(_opts())
        assert trace.get_tracer_provider() is setup_mod._pipeline.tracer_provider
        assert metrics.get_meter_provider() is setup_mod._pipeline.meter_provider

    def test_reset_telemetry_shuts_down(self):
        setup_telemetry(_opts())
        calls = _spy_shutdowns(setup_mod._pipeline)
        setup_mod.reset_telemetry()
        assert set(calls.values()) == {1}
        assert _helper_handlers() == []
        assert setup_mod._globals_installed is False


def test_metrics_app_503_before_setup():
    assert _scrape_status() == 503


class TestShutdownResult:
    """Design "Shutdown result semantics": later/concurrent calls get the SAME result."""

    def _failing_setup(self):
        setup_telemetry(_opts())
        _spy_shutdowns(setup_mod._pipeline, {"tracer_provider": ValueError("trace boom")})

    def test_later_calls_reraise_the_same_error(self):
        self._failing_setup()
        with pytest.raises(RuntimeError) as first:
            shutdown_telemetry()
        with pytest.raises(RuntimeError) as second:
            shutdown_telemetry()
        assert second.value is first.value

    def test_concurrent_callers_get_the_same_error(self):
        setup_telemetry(_opts())
        _spy_shutdowns(setup_mod._pipeline, {"tracer_provider": 0.2})
        pipeline = setup_mod._pipeline
        original = pipeline.logger_provider.shutdown

        def failing(*a, **kw):
            original(*a, **kw)
            raise ValueError("logs boom")

        pipeline.logger_provider.shutdown = failing
        raised = []

        def call():
            try:
                shutdown_telemetry()
            except RuntimeError as e:
                raised.append(e)

        threads = [threading.Thread(target=call) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(raised) == 6 and all(e is raised[0] for e in raised)

    def test_successful_setup_clears_the_stored_error(self):
        self._failing_setup()
        with pytest.raises(RuntimeError):
            shutdown_telemetry()
        setup_telemetry(_opts())
        shutdown_telemetry()  # new pipeline shuts down cleanly
        shutdown_telemetry()  # and the success is what later calls repeat


class TestMountedMetricsStatus:
    """Design "Mounted /metrics handler status"."""

    def test_sdk_disabled_is_200_empty(self, monkeypatch):
        monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
        setup_telemetry(_opts())
        response = TestClient(metrics_app()).get("/")
        assert (response.status_code, response.text) == (200, "")

    def test_exporter_without_prometheus_is_200_empty(self):
        setup_telemetry(_opts(metric_exporters=["console"], export_interval_ms=3_600_000))
        response = TestClient(metrics_app()).get("/")
        assert (response.status_code, response.text) == (200, "")

    def test_metrics_signal_disabled_is_200_empty(self):
        setup_telemetry(_opts(disabled_signals=["metrics"]))
        assert _scrape_status() == 200

    def test_after_shutdown_and_downgraded_setup_is_503(self):
        setup_telemetry(_opts())
        shutdown_telemetry()
        assert _scrape_status() == 503
        setup_telemetry(_opts())  # D13 downgrade: metrics stay off, not by choice
        assert _scrape_status() == 503

    def test_503_while_setup_is_in_progress(self, monkeypatch):
        seen = []
        real = setup_mod.configure_instrumentations

        def observing(*args, **kwargs):
            seen.append(_scrape_status())  # providers built, nothing installed yet
            return real(*args, **kwargs)

        monkeypatch.setattr(setup_mod, "configure_instrumentations", observing)
        setup_telemetry(_opts())
        assert seen == [503]
        assert _scrape_status() == 200


class TestDowngradeReportsSignalsOff:
    def test_is_signal_enabled_false_after_downgrade(self):
        setup_telemetry(_opts())
        shutdown_telemetry()
        opts = setup_telemetry(_opts())
        assert opts.is_signal_enabled("traces") is False
        assert opts.is_signal_enabled("metrics") is False
        assert opts.is_signal_enabled("logs") is True


class TestRollbackDetails:
    def test_partial_build_inside_build_metrics(self, monkeypatch):
        import otel_helper.metrics as metrics_mod

        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        collectors_before = len(list(metrics_mod._REGISTRY.collect()))

        def broken_provider(**kwargs):
            raise RuntimeError("injected MeterProvider failure")

        monkeypatch.setattr(metrics_mod, "MeterProvider", broken_provider)
        with pytest.raises(RuntimeError, match="injected MeterProvider"):
            setup_telemetry(_opts(metric_exporters=["console", "prometheus"], prometheus_metrics_port=port,
                                  export_interval_ms=3_600_000))
        _nothing_global_installed()
        with socket.socket() as s:
            s.bind(("0.0.0.0", port))  # listener closed
        assert len(list(metrics_mod._REGISTRY.collect())) == collectors_before
        assert not any(t.name.startswith("OtelPeriodicExportingMetricReader") and t.is_alive()
                       for t in threading.enumerate())

    def test_busy_port_does_not_register_the_reader(self, monkeypatch, caplog):
        import otel_helper.metrics as metrics_mod

        created = []
        monkeypatch.setattr(metrics_mod, "_HelperPrometheusReader",
                            lambda: created.append(1) or pytest.fail("reader built before the bind"))
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        blocker.bind(("0.0.0.0", 0))
        try:
            with caplog.at_level(logging.WARNING):
                with pytest.raises(RuntimeError, match="metrics_app"):
                    setup_telemetry(_opts(prometheus_metrics_port=blocker.getsockname()[1]))
        finally:
            blocker.close()
        assert created == []
        assert "Cannot call collect" not in caplog.text

    def test_instrumentation_undone_on_rollback_and_shutdown(self, monkeypatch):
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        if FastAPIInstrumentor().is_instrumented_by_opentelemetry:
            FastAPIInstrumentor().uninstrument()
        real = setup_mod.configure_instrumentations

        def instrument_then_fail(options, instrumented):
            real(options, instrumented)
            assert FastAPIInstrumentor().is_instrumented_by_opentelemetry
            raise RuntimeError("injected late instrumentation failure")

        monkeypatch.setattr(setup_mod, "configure_instrumentations", instrument_then_fail)
        with pytest.raises(RuntimeError, match="late instrumentation"):
            setup_telemetry(_opts())
        assert not FastAPIInstrumentor().is_instrumented_by_opentelemetry
        monkeypatch.undo()

        setup_telemetry(_opts())
        assert FastAPIInstrumentor().is_instrumented_by_opentelemetry
        shutdown_telemetry()
        assert not FastAPIInstrumentor().is_instrumented_by_opentelemetry

    def test_log_handler_closed_on_shutdown(self):
        setup_telemetry(_opts())
        handler = setup_mod._pipeline.log_handler
        closed = []
        original = handler.close
        handler.close = lambda: (closed.append(True), original())
        shutdown_telemetry()
        assert closed == [True]

    def test_fork_child_gets_a_fresh_lock(self, monkeypatch):
        monkeypatch.setattr(setup_mod, "_forked_after_import", False)
        old = setup_mod._lock
        monkeypatch.setattr(setup_mod, "_lock", old)  # restored after the test
        setup_mod._mark_forked()
        assert setup_mod._lock is not old


class TestFinalLowItems:
    def test_metrics_503_during_slow_flush(self):
        setup_telemetry(_opts())
        pipeline = setup_mod._pipeline
        seen = []
        original = pipeline.meter_provider.shutdown

        def slow(*args, **kwargs):
            seen.append(_scrape_status())  # shutdown already started
            time.sleep(0.2)
            return original(*args, **kwargs)

        pipeline.meter_provider.shutdown = slow
        shutdown_telemetry()
        assert seen == [503]

    def test_meter_provider_conflict_is_503_not_200(self, caplog):
        from opentelemetry.sdk.metrics import MeterProvider as SdkMeterProvider

        foreign = SdkMeterProvider()
        metrics.set_meter_provider(foreign)
        try:
            with caplog.at_level(logging.WARNING, logger="otel_helper"):
                opts = setup_telemetry(_opts())
            assert "already installed by other code" in caplog.text
            assert opts.is_signal_enabled("metrics") is False
            assert _scrape_status() == 503
        finally:
            foreign.shutdown()

    def test_rollback_errors_are_logged(self, monkeypatch, caplog):
        def boom(*args, **kwargs):
            raise RuntimeError("injected late failure")

        monkeypatch.setattr(setup_mod, "configure_instrumentations", boom)
        monkeypatch.setattr(setup_mod._Pipeline, "close", lambda self, timeout_ms: ["traces: close boom"])
        with caplog.at_level(logging.WARNING, logger="otel_helper"):
            with pytest.raises(RuntimeError, match="injected late failure"):
                setup_telemetry(_opts())
        assert "setup rollback: traces: close boom" in caplog.text

    def test_reraise_does_not_grow_traceback_and_atexit_is_quiet(self, caplog):
        import traceback

        setup_telemetry(_opts())
        _spy_shutdowns(setup_mod._pipeline, {"tracer_provider": ValueError("trace boom")})
        depths = []
        for _ in range(3):
            with pytest.raises(RuntimeError) as info:
                shutdown_telemetry()
            depths.append(len(traceback.extract_tb(info.value.__traceback__)))
        assert depths[1] == depths[2]
        with caplog.at_level(logging.WARNING, logger="otel_helper"):
            setup_mod._shutdown_at_exit()
        assert "trace boom" not in caplog.text

    def test_fork_child_gets_a_fresh_metrics_lock(self, monkeypatch):
        import otel_helper.metrics as metrics_mod

        monkeypatch.setattr(setup_mod, "_forked_after_import", False)
        monkeypatch.setattr(setup_mod, "_lock", setup_mod._lock)
        old = metrics_mod._active_lock
        monkeypatch.setattr(metrics_mod, "_active_lock", old)
        setup_mod._mark_forked()
        assert metrics_mod._active_lock is not old
