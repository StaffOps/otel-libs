"""Tests for new features: sample_ratio, debug attribute, gRPC auto-instrumentation."""

import os
import pytest

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.sampling import ALWAYS_ON, TraceIdRatioBased
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from otel_helper.config import TelemetryOptions, DeploymentEnvironment
from otel_helper.tracing import configure_tracing
from otel_helper.processors import DebugProcessor
from otel_helper.setup import reset_telemetry


@pytest.fixture(autouse=True)
def clean():
    for var in ["OTEL_HELPER_SAMPLE_RATIO", "OTEL_HELPER_DEBUG_LEVEL", "SERVICE_NAME",
                "OTEL_EXPORTER_OTLP_ENDPOINT", "ENVIRONMENT"]:
        os.environ.pop(var, None)
    reset_telemetry()
    yield
    reset_telemetry()


class TestSampleRatio:
    def test_default_is_always_on(self):
        opts = TelemetryOptions(service_name="test", otel_endpoint="http://localhost:4317")
        opts.resolve_from_env()
        assert opts.sample_ratio == 1.0

    def test_env_var_sets_ratio(self):
        os.environ["OTEL_HELPER_SAMPLE_RATIO"] = "0.5"
        opts = TelemetryOptions(service_name="test", otel_endpoint="http://localhost:4317")
        opts.resolve_from_env()
        assert opts.sample_ratio == 0.5

    def test_env_var_clamped_to_0_1(self):
        os.environ["OTEL_HELPER_SAMPLE_RATIO"] = "2.5"
        opts = TelemetryOptions(service_name="test", otel_endpoint="http://localhost:4317")
        opts.resolve_from_env()
        assert opts.sample_ratio == 1.0

    def test_env_var_negative_clamped(self):
        os.environ["OTEL_HELPER_SAMPLE_RATIO"] = "-0.5"
        opts = TelemetryOptions(service_name="test", otel_endpoint="http://localhost:4317")
        opts.resolve_from_env()
        assert opts.sample_ratio == 0.0

    def test_invalid_env_var_ignored(self):
        os.environ["OTEL_HELPER_SAMPLE_RATIO"] = "not_a_number"
        opts = TelemetryOptions(service_name="test", otel_endpoint="http://localhost:4317")
        opts.resolve_from_env()
        assert opts.sample_ratio == 1.0

    def test_explicit_value_overrides_env(self):
        os.environ["OTEL_HELPER_SAMPLE_RATIO"] = "0.1"
        opts = TelemetryOptions(service_name="test", otel_endpoint="http://localhost:4317", sample_ratio=0.75)
        opts.resolve_from_env()
        assert opts.sample_ratio == 0.75

    def test_ratio_below_1_uses_trace_id_sampler(self):
        from opentelemetry.sdk.resources import Resource
        opts = TelemetryOptions(service_name="test", otel_endpoint="http://localhost:4317", sample_ratio=0.5)
        resource = Resource.create({"service.name": "test"})
        provider = configure_tracing(resource, opts)
        sampler = provider.sampler
        assert isinstance(sampler, TraceIdRatioBased)

    def test_ratio_1_uses_always_on(self):
        from opentelemetry.sdk.resources import Resource
        opts = TelemetryOptions(service_name="test", otel_endpoint="http://localhost:4317", sample_ratio=1.0)
        resource = Resource.create({"service.name": "test"})
        provider = configure_tracing(resource, opts)
        sampler = provider.sampler
        assert not isinstance(sampler, TraceIdRatioBased)


class TestDebugProcessor:
    def test_sets_debug_attribute_on_root_span(self):
        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(DebugProcessor())
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        provider.add_span_processor(SimpleSpanProcessor(exporter))

        tracer = provider.get_tracer("test")
        with tracer.start_as_current_span("root-span"):
            pass

        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        assert spans[0].attributes.get("debug") == "true"

    def test_does_not_set_on_child_span(self):
        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(DebugProcessor())
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        provider.add_span_processor(SimpleSpanProcessor(exporter))

        tracer = provider.get_tracer("test")
        with tracer.start_as_current_span("root"):
            with tracer.start_as_current_span("child"):
                pass

        spans = exporter.get_finished_spans()
        child = next(s for s in spans if s.name == "child")
        root = next(s for s in spans if s.name == "root")
        assert root.attributes.get("debug") == "true"
        assert child.attributes.get("debug") is None


class TestGrpcAutoInstrumentation:
    def test_grpc_aio_client_instrumentor_patches(self):
        """Verify GrpcAioInstrumentorClient monkey-patches grpc.aio.insecure_channel."""
        import grpc
        from opentelemetry.instrumentation.grpc import GrpcAioInstrumentorClient

        instrumentor = GrpcAioInstrumentorClient()
        try:
            instrumentor.uninstrument()
        except Exception:
            pass

        original = grpc.aio.insecure_channel
        instrumentor.instrument()
        assert grpc.aio.insecure_channel is not original
        instrumentor.uninstrument()

    def test_grpc_aio_server_instrumentor_patches(self):
        """Verify GrpcAioInstrumentorServer monkey-patches grpc.aio.server."""
        import grpc
        from opentelemetry.instrumentation.grpc import GrpcAioInstrumentorServer

        instrumentor = GrpcAioInstrumentorServer()
        try:
            instrumentor.uninstrument()
        except Exception:
            pass

        original = grpc.aio.server
        instrumentor.instrument()
        assert grpc.aio.server is not original
        instrumentor.uninstrument()


class TestHelpers:
    def test_get_tracer(self):
        from otel_helper import get_tracer, setup_telemetry
        setup_telemetry(TelemetryOptions(service_name="test", otel_endpoint="http://localhost:4317"))
        tracer = get_tracer("my-svc")
        assert tracer is not None

    def test_get_meter(self):
        from otel_helper import get_meter, setup_telemetry
        setup_telemetry(TelemetryOptions(service_name="test", otel_endpoint="http://localhost:4317"))
        meter = get_meter("my-svc")
        assert meter is not None

    def test_get_tracer_default_name(self):
        from otel_helper import get_tracer
        tracer = get_tracer()
        assert tracer is not None

    def test_get_meter_default_name(self):
        from otel_helper import get_meter
        meter = get_meter()
        assert meter is not None


class TestStartRootSpan:
    def test_creates_independent_trace(self):
        from otel_helper.tracing import start_root_span
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        tracer = provider.get_tracer("test")

        with tracer.start_as_current_span("parent"):
            with start_root_span(tracer, "root-child") as span:
                pass

        spans = exporter.get_finished_spans()
        root_child = next(s for s in spans if s.name == "root-child")
        assert root_child.parent is None

    def test_yields_span(self):
        from otel_helper.tracing import start_root_span
        from opentelemetry.sdk.trace import TracerProvider

        provider = TracerProvider()
        tracer = provider.get_tracer("test")

        with start_root_span(tracer, "my-span") as span:
            assert span is not None
            assert span.is_recording()


class TestDebugProcessorLifecycle:
    def test_shutdown(self):
        from otel_helper.processors import DebugProcessor
        p = DebugProcessor()
        p.shutdown()

    def test_force_flush(self):
        from otel_helper.processors import DebugProcessor
        p = DebugProcessor()
        assert p.force_flush() is True
