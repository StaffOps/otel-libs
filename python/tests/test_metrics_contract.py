"""Contract tests for OTEL_METRICS_EXPORTER / OTEL_METRIC_EXPORT_INTERVAL (US-1, US-2, US-6).

Cross-language parity: go/metrics_contract_test.go and
dotnet/OtelHelper.Tests/MetricsContractTests.cs assert the same table.
"""

import asyncio
import logging
import os
import socket

import pytest

from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.exporter.prometheus import PrometheusMetricReader
from opentelemetry.sdk.resources import Resource

from otel_helper import metrics_app, setup_telemetry
from otel_helper.config import TelemetryOptions
from otel_helper.metrics import configure_metrics


def _resolved(**kwargs) -> TelemetryOptions:
    opts = TelemetryOptions(**kwargs)
    opts.resolve_from_env()
    return opts


class TestExporterResolution:
    """US-1: the OTEL_METRICS_EXPORTER value table."""

    def test_unset_with_endpoint_is_otlp(self):
        opts = _resolved(otel_endpoint="http://collector:4317")
        assert opts.resolved_metric_exporters() == ["otlp"]

    def test_unset_without_endpoint_is_prometheus(self):
        opts = _resolved()
        assert opts.resolved_metric_exporters() == ["prometheus"]

    def test_otlp_only(self):
        os.environ["OTEL_METRICS_EXPORTER"] = "otlp"
        opts = _resolved(otel_endpoint="http://collector:4317")
        assert opts.resolved_metric_exporters() == ["otlp"]

    def test_prometheus_only_even_with_endpoint(self):
        os.environ["OTEL_METRICS_EXPORTER"] = "prometheus"
        opts = _resolved(otel_endpoint="http://collector:4317")
        assert opts.resolved_metric_exporters() == ["prometheus"]

    def test_dual_mode(self):
        os.environ["OTEL_METRICS_EXPORTER"] = "otlp,prometheus"
        opts = _resolved(otel_endpoint="http://collector:4317")
        assert opts.resolved_metric_exporters() == ["otlp", "prometheus"]

    def test_none_disables_metrics(self):
        os.environ["OTEL_METRICS_EXPORTER"] = "none"
        opts = _resolved(otel_endpoint="http://collector:4317")
        opts.validate()
        assert opts.resolved_metric_exporters() == []

    def test_case_and_whitespace_tolerant(self):
        os.environ["OTEL_METRICS_EXPORTER"] = " OTLP , Prometheus "
        opts = _resolved(otel_endpoint="http://collector:4317")
        assert opts.resolved_metric_exporters() == ["otlp", "prometheus"]

    def test_explicit_option_beats_env(self):
        os.environ["OTEL_METRICS_EXPORTER"] = "otlp"
        opts = _resolved(metric_exporters=["prometheus"], otel_endpoint="http://collector:4317")
        assert opts.resolved_metric_exporters() == ["prometheus"]

    def test_unknown_env_value_warns_and_uses_inference(self, caplog):
        os.environ["OTEL_METRICS_EXPORTER"] = "statsd"
        with caplog.at_level(logging.WARNING, logger="otel_helper"):
            opts = _resolved(service_name="svc")
            opts.validate()
        assert opts.resolved_metric_exporters() == ["prometheus"]
        assert "OTEL_METRICS_EXPORTER" in caplog.text and "statsd" in caplog.text

    def test_unknown_code_value_fails_validation(self):
        opts = _resolved(service_name="svc", metric_exporters=["statsd"])
        with pytest.raises(ValueError, match="statsd.*Valid values"):
            opts.validate()

    def test_otlp_env_without_endpoint_warns_and_falls_back(self, caplog):
        os.environ["OTEL_METRICS_EXPORTER"] = "otlp"
        with caplog.at_level(logging.WARNING, logger="otel_helper"):
            opts = _resolved(service_name="svc")
            opts.validate()
        assert opts.resolved_metric_exporters() == ["prometheus"]
        assert "no metrics endpoint" in caplog.text

    def test_otlp_env_without_endpoint_keeps_other_exporters(self):
        os.environ["OTEL_METRICS_EXPORTER"] = "otlp,console"
        assert _resolved(service_name="svc").resolved_metric_exporters() == ["console"]

    def test_otlp_code_without_endpoint_fails_validation(self):
        opts = _resolved(service_name="svc", metric_exporters=["otlp"])
        with pytest.raises(ValueError, match="requires an endpoint"):
            opts.validate()

    def test_otlp_per_signal_metrics_endpoint_counts_as_configured(self):
        os.environ["OTEL_EXPORTER_OTLP_METRICS_ENDPOINT"] = "http://collector:4318/v1/metrics"
        opts = _resolved(service_name="svc", metric_exporters=["otlp"])
        opts.validate()
        assert opts.resolved_metric_exporters() == ["otlp"]

    def test_none_combined_env_warns_and_is_none(self, caplog):
        os.environ["OTEL_METRICS_EXPORTER"] = "none,prometheus"
        with caplog.at_level(logging.WARNING, logger="otel_helper"):
            opts = _resolved(service_name="svc")
        assert opts.resolved_metric_exporters() == []
        assert "'none'" in caplog.text

    def test_none_combined_code_fails_validation(self):
        opts = _resolved(service_name="svc", metric_exporters=["none", "prometheus"])
        with pytest.raises(ValueError, match="cannot be combined"):
            opts.validate()

    def test_logging_alias_is_console(self, caplog):
        os.environ["OTEL_METRICS_EXPORTER"] = "logging"
        with caplog.at_level(logging.WARNING, logger="otel_helper"):
            opts = _resolved(service_name="svc")
        assert opts.resolved_metric_exporters() == ["console"]
        assert "deprecated" in caplog.text


class TestExportInterval:
    """US-2: OTEL_METRIC_EXPORT_INTERVAL precedence."""

    def test_default_is_30s(self):
        assert _resolved().export_interval_ms == 30_000

    def test_env_var_honored(self):
        os.environ["OTEL_METRIC_EXPORT_INTERVAL"] = "5000"
        assert _resolved().export_interval_ms == 5000

    def test_explicit_beats_env(self):
        os.environ["OTEL_METRIC_EXPORT_INTERVAL"] = "5000"
        assert _resolved(export_interval_ms=1234).export_interval_ms == 1234

    def test_invalid_env_falls_back_to_default(self, caplog):
        os.environ["OTEL_METRIC_EXPORT_INTERVAL"] = "not-a-number"
        with caplog.at_level(logging.WARNING, logger="otel_helper"):
            assert _resolved().export_interval_ms == 30_000
        assert "OTEL_METRIC_EXPORT_INTERVAL" in caplog.text

    @pytest.mark.parametrize("raw", ["0", "-5"])
    def test_non_positive_env_warns_and_falls_back(self, raw, caplog):
        os.environ["OTEL_METRIC_EXPORT_INTERVAL"] = raw
        with caplog.at_level(logging.WARNING, logger="otel_helper"):
            opts = _resolved()
            opts.validate()
        assert opts.export_interval_ms == 30_000
        assert "OTEL_METRIC_EXPORT_INTERVAL" in caplog.text

    def test_non_positive_fails_validation(self):
        with pytest.raises(ValueError, match="export_interval_ms"):
            _resolved(service_name="svc", export_interval_ms=0)


@pytest.fixture
def helper_calls(monkeypatch):
    """Record the readers and interval the helper passes to the public SDK constructors.

    Asserting on what configure_metrics() hands to MeterProvider /
    PeriodicExportingMetricReader keeps these tests off private SDK attributes
    (MeterProvider._metric_readers, reader._export_interval_millis), which
    are not a stable API across the supported SDK range.
    """
    import otel_helper.metrics as helper_metrics

    calls = {"readers": None, "intervals": []}

    class RecordingMeterProvider(helper_metrics.MeterProvider):
        def __init__(self, *args, **kwargs):
            calls["readers"] = list(kwargs.get("metric_readers", ()))
            super().__init__(*args, **kwargs)

    class RecordingPeriodicReader(PeriodicExportingMetricReader):
        def __init__(self, *args, **kwargs):
            calls["intervals"].append(kwargs.get("export_interval_millis"))
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(helper_metrics, "MeterProvider", RecordingMeterProvider)
    monkeypatch.setattr(helper_metrics, "PeriodicExportingMetricReader", RecordingPeriodicReader)
    return calls


class TestDualModePipeline:
    """US-1/US-6: both readers on ONE provider; same counter in both outputs."""

    def test_dual_mode_has_both_readers(self, helper_calls):
        opts = _resolved(
            service_name="dual-svc",
            otel_endpoint="http://localhost:4317",
            metric_exporters=["otlp", "prometheus"],
            prometheus_metrics_port=0,
        )
        provider = configure_metrics(Resource.create({}), opts)
        try:
            readers = helper_calls["readers"]
            assert len(readers) == 2
            assert any(isinstance(r, PeriodicExportingMetricReader) for r in readers)
            assert any(isinstance(r, PrometheusMetricReader) for r in readers)

            counter = provider.get_meter("contract-test").create_counter("dual_mode_hits")
            counter.add(7)

            _, body = _asgi_get(metrics_app())
            scrape = body.decode()
            assert "dual_mode_hits" in scrape
            assert "7.0" in scrape
        finally:
            provider.shutdown()

    def test_prometheus_only_with_endpoint_has_no_otlp_reader(self, helper_calls):
        opts = _resolved(
            service_name="prom-svc",
            otel_endpoint="http://localhost:4317",
            metric_exporters=["prometheus"],
            prometheus_metrics_port=0,
        )
        provider = configure_metrics(Resource.create({}), opts)
        try:
            readers = helper_calls["readers"]
            assert len(readers) == 1
            assert isinstance(readers[0], PrometheusMetricReader)
        finally:
            provider.shutdown()

    def test_otlp_reader_uses_resolved_interval(self, helper_calls):
        opts = _resolved(
            service_name="interval-svc",
            otel_endpoint="http://localhost:4317",
            metric_exporters=["otlp"],
            export_interval_ms=5000,
        )
        provider = configure_metrics(Resource.create({}), opts)
        try:
            assert helper_calls["intervals"] == [5000]
            assert len(helper_calls["readers"]) == 1
        finally:
            provider.shutdown()

    def test_none_skips_metrics_pipeline(self):
        os.environ["OTEL_METRICS_EXPORTER"] = "none"
        opts = setup_telemetry(TelemetryOptions(service_name="none-svc"))
        assert opts.resolved_metric_exporters() == []


def _asgi_get(app, path="/"):
    """Minimal ASGI test harness: GET request, returns (status, body)."""
    messages = []

    async def run():
        scope = {
            "type": "http", "method": "GET", "path": path, "raw_path": path.encode(),
            "query_string": b"", "headers": [], "asgi": {"version": "3.0"},
        }
        received = False

        async def receive():
            nonlocal received
            if received:
                await asyncio.sleep(3600)
            received = True
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            messages.append(message)

        await app(scope, receive, send)

    asyncio.run(run())
    status = next(m["status"] for m in messages if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    return status, body


class TestMountableHandler:
    """US-3: metrics_app() serves the Prometheus text format."""

    def test_metrics_app_serves_prometheus_text(self):
        opts = _resolved(
            service_name="mount-svc",
            metric_exporters=["prometheus"],
            prometheus_metrics_port=0,
        )
        provider = configure_metrics(Resource.create({}), opts)
        try:
            counter = provider.get_meter("mount-test").create_counter("mounted_requests")
            counter.add(3)

            status, body = _asgi_get(metrics_app())
            assert status == 200
            assert b"mounted_requests" in body
        finally:
            provider.shutdown()


class TestListenerRobustness:
    """US-4: port 0 suppresses the listener; busy port fails loudly."""

    def test_port_zero_suppresses_listener(self):
        opts = _resolved(
            service_name="noport-svc",
            metric_exporters=["prometheus"],
            prometheus_metrics_port=0,
        )
        provider = configure_metrics(Resource.create({}), opts)
        provider.shutdown()  # no OSError, nothing bound — reaching here is the assertion

    def test_busy_port_raises_actionable_error(self):
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        blocker.bind(("0.0.0.0", 0))
        busy_port = blocker.getsockname()[1]
        try:
            opts = _resolved(
                service_name="busy-svc",
                metric_exporters=["prometheus"],
                prometheus_metrics_port=busy_port,
            )
            with pytest.raises(RuntimeError, match="metrics_app"):
                configure_metrics(Resource.create({}), opts)
        finally:
            blocker.close()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestDedicatedRegistry:
    """P14: /metrics serves only OTel-produced series from a dedicated registry."""

    def _prometheus_provider(self, port=0):
        opts = _resolved(service_name="registry-svc", metric_exporters=["prometheus"],
                         prometheus_metrics_port=port)
        return configure_metrics(Resource.create({}), opts)

    def test_mounted_app_has_no_default_collectors_or_app_metrics(self):
        from prometheus_client import REGISTRY, Counter

        app_counter = Counter("app_own_counter", "registered on the global REGISTRY by the app")
        provider = self._prometheus_provider()
        try:
            provider.get_meter("registry-test").create_counter("registry_hits").add(2)
            app_counter.inc()
            _, body = _asgi_get(metrics_app())
            scrape = body.decode()
            assert "registry_hits_total" in scrape
            for leaked in ("process_cpu_seconds_total", "python_gc_objects_collected_total",
                           "python_info", "app_own_counter"):
                assert leaked not in scrape
        finally:
            provider.shutdown()
            REGISTRY.unregister(app_counter)

    def test_otel_series_do_not_leak_into_global_registry(self):
        from prometheus_client import REGISTRY, generate_latest

        provider = self._prometheus_provider()
        try:
            provider.get_meter("registry-test").create_counter("not_global_hits").add(1)
            assert "not_global_hits" not in generate_latest(REGISTRY).decode()
        finally:
            provider.shutdown()

    def test_standalone_listener_serves_dedicated_registry(self):
        import urllib.request

        port = _free_port()
        provider = self._prometheus_provider(port)
        try:
            provider.get_meter("registry-test").create_counter("listener_hits").add(4)
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=5) as resp:
                scrape = resp.read().decode()
            assert "listener_hits_total" in scrape
            assert "process_cpu_seconds_total" not in scrape
            assert "python_gc_objects_collected_total" not in scrape
        finally:
            provider.shutdown()

    def test_shutdown_unregisters_reader(self):
        provider = self._prometheus_provider()
        provider.get_meter("registry-test").create_counter("gone_after_shutdown").add(1)
        provider.shutdown()
        _, body = _asgi_get(metrics_app())
        assert b"gone_after_shutdown" not in body

    def test_busy_port_unregisters_reader(self):
        from otel_helper.metrics import _REGISTRY

        before = len(list(_REGISTRY.collect()))
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        blocker.bind(("0.0.0.0", 0))
        try:
            with pytest.raises(RuntimeError):
                self._prometheus_provider(blocker.getsockname()[1])
        finally:
            blocker.close()
        assert len(list(_REGISTRY.collect())) == before
