"""A9 (Python): deprecation warnings (R8/D8) and the debug entry-span marker (R9, ADR-006).

Deprecations: one WARNING per deprecated variable per process, on the
``otel_helper`` logger, naming the variable and its replacement and never its
value. Debug: ``debug=true`` on spans with no parent or a remote parent, only
from local config; inbound tracestate/baggage never set it and tracestate is
never written.
"""

import logging
import os

import pytest
from opentelemetry import baggage, trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags, TraceState

from otel_helper.config import TelemetryOptions
from otel_helper.processors import DebugProcessor
from otel_helper.tracing import build_tracer_provider
from opentelemetry.sdk.resources import Resource

DEPRECATED = {
    "SERVICE_NAME": "OTEL_SERVICE_NAME",
    "ENVIRONMENT": "OTEL_RESOURCE_ATTRIBUTES=deployment.environment.name",
    "OTEL_HELPER_SAMPLE_RATIO": "OTEL_TRACES_SAMPLER=parentbased_traceidratio",
    "OTEL_HELPER_DISABLED_SIGNALS": "OTEL_LOGS_EXPORTER=none",
    "OTEL_HELPER_EXTRA_INSTRUMENTATION": "otel_helper.ext.sql.instrument_sql()",
}
SAMPLE_VALUES = {
    "SERVICE_NAME": "secret-looking-svc",
    "ENVIRONMENT": "DEV",
    "OTEL_HELPER_SAMPLE_RATIO": "0.25",
    "OTEL_HELPER_DISABLED_SIGNALS": "logs",
    "OTEL_HELPER_EXTRA_INSTRUMENTATION": "SQL,REDIS",
}


def _deprecations(caplog, var: str) -> list[logging.LogRecord]:
    return [r for r in caplog.records
            if r.name.startswith("otel_helper") and r.levelno == logging.WARNING
            and "deprecated" in r.getMessage() and r.getMessage().split(" ")[1] == var]


def _resolve(**kwargs) -> TelemetryOptions:
    opts = TelemetryOptions(**kwargs)
    opts.resolve_from_env()
    return opts


class TestDeprecatedVariables:
    @pytest.mark.parametrize("var", sorted(DEPRECATED))
    def test_warns_naming_variable_and_replacement(self, caplog, var):
        os.environ["OTEL_SERVICE_NAME"] = "svc"
        os.environ[var] = SAMPLE_VALUES[var]
        with caplog.at_level(logging.WARNING, logger="otel_helper"):
            _resolve()
        records = _deprecations(caplog, var)
        assert len(records) == 1
        msg = records[0].getMessage()
        assert DEPRECATED[var] in msg and "1.0" in msg
        assert SAMPLE_VALUES[var] not in msg  # the value is never printed

    @pytest.mark.parametrize("var", sorted(DEPRECATED))
    def test_once_per_process_across_resolutions(self, caplog, var):
        os.environ[var] = SAMPLE_VALUES[var]
        with caplog.at_level(logging.WARNING, logger="otel_helper"):
            _resolve()
            _resolve()
            TelemetryOptions(service_name="code").resolve_from_env()
        assert len(_deprecations(caplog, var)) == 1

    def test_warns_even_when_superseded(self, caplog):
        """Set but ignored (standard var / code value wins) is still a deprecated usage."""
        os.environ.update({"SERVICE_NAME": "legacy", "OTEL_SERVICE_NAME": "std",
                           "OTEL_HELPER_SAMPLE_RATIO": "0.5", "OTEL_TRACES_SAMPLER": "always_on"})
        with caplog.at_level(logging.WARNING, logger="otel_helper"):
            opts = _resolve()
        assert opts.service_name == "std"  # behavior unchanged: the standard var still wins
        assert len(_deprecations(caplog, "SERVICE_NAME")) == 1
        assert len(_deprecations(caplog, "OTEL_HELPER_SAMPLE_RATIO")) == 1

    def test_deprecated_values_still_take_effect(self):
        os.environ.update(SAMPLE_VALUES)
        opts = _resolve()
        assert opts.service_name == "secret-looking-svc"
        assert opts.sample_ratio == 0.25
        assert opts.disabled_signals == ["logs"]
        assert opts.has_instrumentation("REDIS")

    @pytest.mark.parametrize("var", ["SERVICE_NAME", "ENVIRONMENT", "OTEL_HELPER_SAMPLE_RATIO",
                                     "OTEL_HELPER_DISABLED_SIGNALS"])
    def test_whitespace_only_is_unset(self, caplog, var):
        os.environ[var] = "  "
        with caplog.at_level(logging.WARNING, logger="otel_helper"):
            _resolve(service_name="svc")
        assert _deprecations(caplog, var) == []

    def test_empty_extra_instrumentation_warns(self, caplog):
        """An empty value still has an effect (turns the SQL default off), so it is a usage."""
        os.environ["OTEL_HELPER_EXTRA_INSTRUMENTATION"] = ""
        with caplog.at_level(logging.WARNING, logger="otel_helper"):
            opts = _resolve(service_name="svc")
        assert opts.has_instrumentation("SQL") is False
        assert len(_deprecations(caplog, "OTEL_HELPER_EXTRA_INSTRUMENTATION")) == 1

    def test_nothing_deprecated_set_no_warning(self, caplog):
        os.environ["OTEL_SERVICE_NAME"] = "svc"
        with caplog.at_level(logging.WARNING, logger="otel_helper"):
            _resolve()
        assert not [r for r in caplog.records if "deprecated" in r.getMessage()]


def _provider(processor=None):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    if processor is not None:
        provider.add_span_processor(processor)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter


def _remote_ctx(tracestate=None, sampled=True):
    sc = SpanContext(trace_id=0x1234, span_id=0x5678, is_remote=True,
                     trace_flags=TraceFlags(TraceFlags.SAMPLED if sampled else 0),
                     trace_state=tracestate or TraceState())
    return trace.set_span_in_context(NonRecordingSpan(sc))


class TestDebugEntrySpans:
    def test_remote_parent_entry_span_marked(self):
        """Failed before A9: only parentless spans were marked (``span.parent is None``)."""
        provider, exporter = _provider(DebugProcessor())
        provider.get_tracer("t").start_span("server", context=_remote_ctx()).end()
        assert exporter.get_finished_spans()[0].attributes.get("debug") == "true"

    def test_local_child_of_remote_entry_not_marked(self):
        provider, exporter = _provider(DebugProcessor())
        tracer = provider.get_tracer("t")
        with tracer.start_as_current_span("server", context=_remote_ctx()):
            with tracer.start_as_current_span("child"):
                pass
        spans = {s.name: s for s in exporter.get_finished_spans()}
        assert spans["server"].attributes.get("debug") == "true"
        assert "debug" not in spans["child"].attributes

    def test_tracestate_never_written(self):
        provider, exporter = _provider(DebugProcessor())
        tracer = provider.get_tracer("t")
        inbound = TraceState([("vendor", "x")])
        tracer.start_span("server", context=_remote_ctx(inbound)).end()
        tracer.start_span("root").end()
        server, root = exporter.get_finished_spans()
        assert list(server.context.trace_state.items()) == [("vendor", "x")]
        assert list(root.context.trace_state.items()) == []

    @pytest.mark.parametrize("ctx_factory", [
        lambda: _remote_ctx(TraceState([("debug", "true")])),
        lambda: baggage.set_baggage("debug", "true", _remote_ctx()),
    ], ids=["tracestate", "baggage"])
    def test_debug_off_ignores_inbound_markers(self, ctx_factory):
        """The helper's provider with debug off: inbound debug markers set nothing."""
        provider = build_tracer_provider(Resource.create({}), _resolve(service_name="svc"))
        exporter = InMemorySpanExporter()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        provider.get_tracer("t").start_span("server", context=ctx_factory()).end()
        provider.shutdown()
        assert "debug" not in exporter.get_finished_spans()[0].attributes

    def test_debug_on_installs_marker_from_local_config(self):
        os.environ["OTEL_HELPER_DEBUG_LEVEL"] = "true"
        provider = build_tracer_provider(Resource.create({}), _resolve(service_name="svc"))
        exporter = InMemorySpanExporter()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        provider.get_tracer("t").start_span("server", context=_remote_ctx()).end()
        provider.shutdown()
        assert exporter.get_finished_spans()[0].attributes.get("debug") == "true"


class TestSchemelessHTTPDeprecation:
    def test_once_per_process(self, caplog):
        """Mirrors Go TestSchemelessHTTPDeprecationOncePerProcess: three signals x two resolutions → 1."""
        os.environ.update({"OTEL_SERVICE_NAME": "svc", "OTEL_EXPORTER_OTLP_ENDPOINT": "collector:4318",
                           "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf"})
        with caplog.at_level(logging.WARNING, logger="otel_helper"):
            _resolve()
            _resolve()
        msgs = [r.getMessage() for r in caplog.records if "schemeless endpoint is deprecated" in r.getMessage()]
        assert len(msgs) == 1
        assert "OTEL_EXPORTER_OTLP_ENDPOINT" in msgs[0] and "removed in 1.0" in msgs[0]

    def test_grpc_schemeless_not_deprecated(self, caplog):
        os.environ.update({"OTEL_SERVICE_NAME": "svc", "OTEL_EXPORTER_OTLP_ENDPOINT": "collector:4317"})
        with caplog.at_level(logging.WARNING, logger="otel_helper"):
            _resolve()
        assert "schemeless" not in caplog.text
