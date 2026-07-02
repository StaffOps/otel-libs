"""Integration tests for tracing signal — verifies spans are created, exported, and configured correctly.

Uses InMemorySpanExporter to capture spans without network I/O.
"""

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.sampling import TraceIdRatioBased, ALWAYS_ON
from opentelemetry.sdk.resources import Resource, SERVICE_NAME

from otel_helper.config import TelemetryOptions
from otel_helper.tracing import configure_tracing, start_root_span
from otel_helper.processors import DebugProcessor


class TestGetTracerCreatesExportedSpans:
    """Verify that after setup, spans created via get_tracer are actually exported."""

    def test_span_is_exported(self):
        exporter = InMemorySpanExporter()
        resource = Resource.create({SERVICE_NAME: "integration-test"})
        opts = TelemetryOptions(
            service_name="integration-test",
            otel_endpoint="http://localhost:4317",
        )
        provider = configure_tracing(resource, opts)
        provider.add_span_processor(SimpleSpanProcessor(exporter))

        tracer = provider.get_tracer("test-tracer")
        with tracer.start_as_current_span("test-operation"):
            pass

        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        assert spans[0].name == "test-operation"

    def test_multiple_spans_exported(self):
        exporter = InMemorySpanExporter()
        resource = Resource.create({SERVICE_NAME: "multi-span-test"})
        opts = TelemetryOptions(
            service_name="multi-span-test",
            otel_endpoint="http://localhost:4317",
        )
        provider = configure_tracing(resource, opts)
        provider.add_span_processor(SimpleSpanProcessor(exporter))

        tracer = provider.get_tracer("test-tracer")
        with tracer.start_as_current_span("parent"):
            with tracer.start_as_current_span("child"):
                pass

        spans = exporter.get_finished_spans()
        assert len(spans) == 2
        names = {s.name for s in spans}
        assert names == {"parent", "child"}


class TestGetTracerUsesConfiguredResource:
    """Verify service.name is set correctly in the resource attached to spans."""

    def test_resource_has_service_name(self):
        exporter = InMemorySpanExporter()
        resource = Resource.create({SERVICE_NAME: "my-service-name"})
        opts = TelemetryOptions(
            service_name="my-service-name",
            otel_endpoint="http://localhost:4317",
        )
        provider = configure_tracing(resource, opts)
        provider.add_span_processor(SimpleSpanProcessor(exporter))

        tracer = provider.get_tracer("test")
        with tracer.start_as_current_span("op"):
            pass

        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        res_attrs = spans[0].resource.attributes
        assert res_attrs.get(SERVICE_NAME) == "my-service-name"

    def test_custom_resource_attributes_propagated(self):
        exporter = InMemorySpanExporter()
        resource = Resource.create({
            SERVICE_NAME: "attr-test",
            "deployment.environment": "DEV",
        })
        opts = TelemetryOptions(
            service_name="attr-test",
            otel_endpoint="http://localhost:4317",
        )
        provider = configure_tracing(resource, opts)
        provider.add_span_processor(SimpleSpanProcessor(exporter))

        tracer = provider.get_tracer("test")
        with tracer.start_as_current_span("op"):
            pass

        spans = exporter.get_finished_spans()
        res_attrs = spans[0].resource.attributes
        assert res_attrs.get("deployment.environment") == "DEV"


class TestDebugProcessorSetsAttributeOnRootSpan:
    """Verify DebugProcessor marks root spans with debug=true."""

    def test_root_span_has_debug_attribute(self):
        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(DebugProcessor())
        provider.add_span_processor(SimpleSpanProcessor(exporter))

        tracer = provider.get_tracer("test")
        with tracer.start_as_current_span("root-op"):
            pass

        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        assert spans[0].attributes.get("debug") == "true"

    def test_debug_processor_added_when_debug_level_enabled(self):
        exporter = InMemorySpanExporter()
        resource = Resource.create({SERVICE_NAME: "debug-test"})
        opts = TelemetryOptions(
            service_name="debug-test",
            otel_endpoint="http://localhost:4317",
            debug_level=True,
        )
        provider = configure_tracing(resource, opts)
        provider.add_span_processor(SimpleSpanProcessor(exporter))

        tracer = provider.get_tracer("test")
        with tracer.start_as_current_span("debug-root"):
            pass

        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        assert spans[0].attributes.get("debug") == "true"


class TestDebugProcessorDoesNotSetOnChild:
    """Verify DebugProcessor only marks root spans, not child spans."""

    def test_child_span_no_debug_attribute(self):
        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(DebugProcessor())
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

    def test_deeply_nested_child_no_debug(self):
        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(DebugProcessor())
        provider.add_span_processor(SimpleSpanProcessor(exporter))

        tracer = provider.get_tracer("test")
        with tracer.start_as_current_span("root"):
            with tracer.start_as_current_span("child-1"):
                with tracer.start_as_current_span("child-2"):
                    pass

        spans = exporter.get_finished_spans()
        for span in spans:
            if span.name == "root":
                assert span.attributes.get("debug") == "true"
            else:
                assert span.attributes.get("debug") is None


class TestSampleRatioBelowOneUsesTraceIdSampler:
    """Verify that sample_ratio < 1.0 configures TraceIdRatioBased sampler."""

    def test_ratio_0_5_uses_trace_id_ratio(self):
        resource = Resource.create({SERVICE_NAME: "sampler-test"})
        opts = TelemetryOptions(
            service_name="sampler-test",
            otel_endpoint="http://localhost:4317",
            sample_ratio=0.5,
        )
        provider = configure_tracing(resource, opts)
        assert isinstance(provider.sampler, TraceIdRatioBased)

    def test_ratio_0_01_uses_trace_id_ratio(self):
        resource = Resource.create({SERVICE_NAME: "sampler-test"})
        opts = TelemetryOptions(
            service_name="sampler-test",
            otel_endpoint="http://localhost:4317",
            sample_ratio=0.01,
        )
        provider = configure_tracing(resource, opts)
        assert isinstance(provider.sampler, TraceIdRatioBased)

    def test_ratio_1_uses_always_on(self):
        resource = Resource.create({SERVICE_NAME: "sampler-test"})
        opts = TelemetryOptions(
            service_name="sampler-test",
            otel_endpoint="http://localhost:4317",
            sample_ratio=1.0,
        )
        provider = configure_tracing(resource, opts)
        assert provider.sampler is ALWAYS_ON


class TestStartRootSpanCreatesIndependentTrace:
    """Verify start_root_span detaches from parent context, creating a new trace."""

    def test_root_span_has_no_parent(self):
        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        tracer = provider.get_tracer("test")

        with tracer.start_as_current_span("outer-parent"):
            with start_root_span(tracer, "independent-root") as span:
                span.set_attribute("key", "value")

        spans = exporter.get_finished_spans()
        independent = next(s for s in spans if s.name == "independent-root")
        assert independent.parent is None

    def test_root_span_has_different_trace_id(self):
        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        tracer = provider.get_tracer("test")

        with tracer.start_as_current_span("outer") as outer_span:
            outer_trace_id = outer_span.get_span_context().trace_id
            with start_root_span(tracer, "new-root") as root_span:
                root_trace_id = root_span.get_span_context().trace_id

        assert outer_trace_id != root_trace_id

    def test_root_span_attributes_exported(self):
        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        tracer = provider.get_tracer("test")

        with start_root_span(tracer, "worker-cycle") as span:
            span.set_attribute("batch.size", 42)

        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        assert spans[0].name == "worker-cycle"
        assert spans[0].attributes.get("batch.size") == 42

    def test_context_restored_after_root_span(self):
        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        tracer = provider.get_tracer("test")

        with tracer.start_as_current_span("parent") as parent_span:
            parent_ctx = parent_span.get_span_context()
            with start_root_span(tracer, "detached"):
                pass
            # After start_root_span exits, parent context should be restored
            current = trace.get_current_span()
            assert current.get_span_context().trace_id == parent_ctx.trace_id
