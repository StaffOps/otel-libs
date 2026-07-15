"""OTLP wire protocol selection (grpc vs http/protobuf) — tracing, metrics, logging.

Ported from bdcotelhelper's port-based auto-detection (4318 -> http/protobuf),
extended to honor the standard OTEL_EXPORTER_OTLP_PROTOCOL env var first (see
config.py's resolved_otlp_protocol() for the full precedence order).

Verifies exporter selection via mocking the exporter constructors — the
underlying SDK behavior itself (manual /v1/{signal} path append needed for an
explicit endpoint, no such need for gRPC) was confirmed empirically against
the real installed SDK before writing this file, not assumed.
"""

from unittest.mock import MagicMock, patch

from opentelemetry.sdk.resources import Resource

from otel_helper.config import TelemetryOptions
from otel_helper.logging_setup import configure_logging
from otel_helper.metrics import configure_metrics
from otel_helper.tracing import configure_tracing


def _resource():
    return Resource.create({"service.name": "protocol-test"})


class TestTracingProtocolSelection:
    def test_grpc_endpoint_uses_grpc_exporter(self):
        with patch("opentelemetry.exporter.otlp.proto.grpc.trace_exporter.OTLPSpanExporter") as grpc_exp, \
             patch("opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter") as http_exp:
            grpc_exp.return_value = MagicMock()
            opts = TelemetryOptions(service_name="svc", otel_endpoint="http://collector:4317")
            configure_tracing(_resource(), opts)

            grpc_exp.assert_called_once()
            http_exp.assert_not_called()
            assert grpc_exp.call_args.kwargs["endpoint"] == "http://collector:4317"

    def test_http_endpoint_uses_http_exporter_with_path_appended(self):
        with patch("opentelemetry.exporter.otlp.proto.grpc.trace_exporter.OTLPSpanExporter") as grpc_exp, \
             patch("opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter") as http_exp:
            http_exp.return_value = MagicMock()
            opts = TelemetryOptions(service_name="svc", otel_endpoint="http://collector:4318")
            configure_tracing(_resource(), opts)

            http_exp.assert_called_once()
            grpc_exp.assert_not_called()
            assert http_exp.call_args.kwargs["endpoint"] == "http://collector:4318/v1/traces"
            # HTTP exporter has no "insecure" kwarg — transport security is the URL scheme.
            assert "insecure" not in http_exp.call_args.kwargs

    def test_http_endpoint_with_trailing_slash_no_double_path(self):
        with patch("opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter") as http_exp:
            http_exp.return_value = MagicMock()
            opts = TelemetryOptions(service_name="svc", otel_endpoint="http://collector:4318/")
            configure_tracing(_resource(), opts)
            assert http_exp.call_args.kwargs["endpoint"] == "http://collector:4318/v1/traces"

    def test_explicit_protocol_overrides_port_inference(self):
        with patch("opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter") as http_exp:
            http_exp.return_value = MagicMock()
            # Port 4317 would normally infer grpc; explicit protocol overrides it.
            opts = TelemetryOptions(
                service_name="svc", otel_endpoint="http://collector:4317", otlp_protocol="http/protobuf",
            )
            configure_tracing(_resource(), opts)
            http_exp.assert_called_once()
            assert http_exp.call_args.kwargs["endpoint"] == "http://collector:4317/v1/traces"


class TestMetricsProtocolSelection:
    def test_grpc_endpoint_uses_grpc_exporter(self):
        with patch("opentelemetry.exporter.otlp.proto.grpc.metric_exporter.OTLPMetricExporter") as grpc_exp, \
             patch("opentelemetry.exporter.otlp.proto.http.metric_exporter.OTLPMetricExporter") as http_exp:
            grpc_exp.return_value = MagicMock()
            opts = TelemetryOptions(
                service_name="svc", otel_endpoint="http://collector:4317", metric_exporters=["otlp"],
            )
            provider = configure_metrics(_resource(), opts)
            try:
                grpc_exp.assert_called_once()
                http_exp.assert_not_called()
                assert grpc_exp.call_args.kwargs["endpoint"] == "http://collector:4317"
            finally:
                provider.shutdown()

    def test_http_endpoint_uses_http_exporter_with_path_appended(self):
        with patch("opentelemetry.exporter.otlp.proto.http.metric_exporter.OTLPMetricExporter") as http_exp:
            http_exp.return_value = MagicMock()
            opts = TelemetryOptions(
                service_name="svc", otel_endpoint="http://collector:4318", metric_exporters=["otlp"],
            )
            provider = configure_metrics(_resource(), opts)
            try:
                http_exp.assert_called_once()
                assert http_exp.call_args.kwargs["endpoint"] == "http://collector:4318/v1/metrics"
            finally:
                provider.shutdown()


class TestLoggingProtocolSelection:
    def test_grpc_endpoint_uses_grpc_exporter(self):
        with patch("opentelemetry.exporter.otlp.proto.grpc._log_exporter.OTLPLogExporter") as grpc_exp, \
             patch("opentelemetry.exporter.otlp.proto.http._log_exporter.OTLPLogExporter") as http_exp:
            grpc_exp.return_value = MagicMock()
            opts = TelemetryOptions(service_name="svc", otel_endpoint="http://collector:4317")
            configure_logging(_resource(), opts)

            grpc_exp.assert_called_once()
            http_exp.assert_not_called()
            assert grpc_exp.call_args.kwargs["endpoint"] == "http://collector:4317"

    def test_http_endpoint_uses_http_exporter_with_path_appended(self):
        with patch("opentelemetry.exporter.otlp.proto.http._log_exporter.OTLPLogExporter") as http_exp:
            http_exp.return_value = MagicMock()
            opts = TelemetryOptions(service_name="svc", otel_endpoint="http://collector:4318")
            configure_logging(_resource(), opts)

            http_exp.assert_called_once()
            assert http_exp.call_args.kwargs["endpoint"] == "http://collector:4318/v1/logs"
