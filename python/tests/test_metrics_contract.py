"""Contract tests for OTEL_METRICS_EXPORTER / OTEL_METRIC_EXPORT_INTERVAL (US-1, US-2, US-6).

Cross-language parity: go/metrics_contract_test.go and
dotnet/OtelHelper.Tests/MetricsContractTests.cs assert the same table.
"""

import asyncio
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

    def test_unknown_value_fails_validation(self):
        os.environ["OTEL_METRICS_EXPORTER"] = "statsd"
        opts = _resolved(service_name="svc")
        with pytest.raises(ValueError, match="statsd.*Valid values"):
            opts.validate()

    def test_otlp_without_endpoint_fails_validation(self):
        os.environ["OTEL_METRICS_EXPORTER"] = "otlp"
        opts = _resolved(service_name="svc")
        with pytest.raises(ValueError, match="requires an endpoint"):
            opts.validate()

    def test_none_combined_with_other_fails_validation(self):
        os.environ["OTEL_METRICS_EXPORTER"] = "none,prometheus"
        opts = _resolved(service_name="svc")
        with pytest.raises(ValueError, match="cannot be combined"):
            opts.validate()


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

    def test_invalid_env_falls_back_to_default(self):
        os.environ["OTEL_METRIC_EXPORT_INTERVAL"] = "not-a-number"
        assert _resolved().export_interval_ms == 30_000

    def test_non_positive_fails_validation(self):
        opts = _resolved(service_name="svc", export_interval_ms=0)
        with pytest.raises(ValueError, match="ExportIntervalMs"):
            opts.validate()


class TestDualModePipeline:
    """US-1/US-6: both readers on ONE provider; same counter in both outputs."""

    def test_dual_mode_has_both_readers(self):
        opts = _resolved(
            service_name="dual-svc",
            otel_endpoint="http://localhost:4317",
            metric_exporters=["otlp", "prometheus"],
            prometheus_metrics_port=0,
        )
        provider = configure_metrics(Resource.create({}), opts)
        try:
            readers = provider._metric_readers
            assert len(readers) == 2
            assert any(isinstance(r, PeriodicExportingMetricReader) for r in readers)
            assert any(isinstance(r, PrometheusMetricReader) for r in readers)

            counter = provider.get_meter("contract-test").create_counter("dual_mode_hits")
            counter.add(7)

            from prometheus_client import REGISTRY, generate_latest
            scrape = generate_latest(REGISTRY).decode()
            assert "dual_mode_hits" in scrape
            assert "7.0" in scrape
        finally:
            provider.shutdown()

    def test_prometheus_only_with_endpoint_has_no_otlp_reader(self):
        opts = _resolved(
            service_name="prom-svc",
            otel_endpoint="http://localhost:4317",
            metric_exporters=["prometheus"],
            prometheus_metrics_port=0,
        )
        provider = configure_metrics(Resource.create({}), opts)
        try:
            readers = provider._metric_readers
            assert len(readers) == 1
            assert isinstance(readers[0], PrometheusMetricReader)
        finally:
            provider.shutdown()

    def test_otlp_reader_uses_resolved_interval(self):
        opts = _resolved(
            service_name="interval-svc",
            otel_endpoint="http://localhost:4317",
            metric_exporters=["otlp"],
            export_interval_ms=5000,
        )
        provider = configure_metrics(Resource.create({}), opts)
        try:
            reader = provider._metric_readers[0]
            assert reader._export_interval_millis == 5000
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
