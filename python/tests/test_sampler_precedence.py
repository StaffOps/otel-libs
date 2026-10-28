"""Sampler contract (R6, design D3, ADR-002).

Precedence: code ratio (any value) > OTEL_TRACES_SAMPLER/_ARG > OTEL_HELPER_SAMPLE_RATIO
(deprecated alias, parentbased_traceidratio) > parentbased_always_on. Behavior is
asserted on sampling decisions, not only on sampler types.
"""

import logging
import os

import pytest

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace.sampling import ParentBased, StaticSampler, TraceIdRatioBased
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags

from otel_helper.config import TelemetryOptions
from otel_helper.tracing import build_sampler, configure_tracing

_TRACE_ID = 0x0AF7651916CD43DD8448EB211C80319C
_SPAN_ID = 0xB7AD6B7169203331


def _provider(**kwargs):
    opts = TelemetryOptions(**kwargs)
    opts.resolve_from_env()
    return configure_tracing(Resource.create({}), opts), opts


def _sampler(**kwargs):
    opts = TelemetryOptions(**kwargs)
    opts.resolve_from_env()
    return build_sampler(opts)


def _remote(sampled: bool) -> Context:
    ctx = SpanContext(_TRACE_ID, _SPAN_ID, is_remote=True,
                      trace_flags=TraceFlags(TraceFlags.SAMPLED if sampled else TraceFlags.DEFAULT))
    return trace.set_span_in_context(NonRecordingSpan(ctx))


def _sampled(provider, parent: Context | None) -> bool:
    span = provider.get_tracer("t").start_span("s", context=parent if parent is not None else Context())
    try:
        return span.get_span_context().trace_flags.sampled
    finally:
        span.end()


def _helper_warnings(caplog) -> list[str]:
    """Sampler diagnostics only (otel_helper.tracing); config warns about unrelated unset vars."""
    return [r.getMessage() for r in caplog.records
            if r.name == "otel_helper.tracing" and r.levelno >= logging.WARNING]


class TestPrecedence:
    def test_helper_var_only_applies_parentbased_ratio(self):
        os.environ["OTEL_HELPER_SAMPLE_RATIO"] = "0.25"
        provider, opts = _provider(service_name="svc")
        assert opts.resolved.sample_ratio == 0.25
        assert isinstance(provider.sampler.inner, ParentBased)
        assert "root:TraceIdRatioBased{0.25}" in provider.sampler.get_description()

    def test_standard_var_only_wins(self):
        os.environ["OTEL_TRACES_SAMPLER"] = "always_off"
        provider, _ = _provider(service_name="svc")
        assert isinstance(provider.sampler.inner, StaticSampler)

    def test_standard_var_beats_helper_var(self):
        os.environ["OTEL_TRACES_SAMPLER"] = "parentbased_always_on"
        os.environ["OTEL_HELPER_SAMPLE_RATIO"] = "0.25"
        provider, opts = _provider(service_name="svc")
        assert opts.resolved.sample_ratio == 1.0
        assert isinstance(provider.sampler.inner, ParentBased)
        assert "root:AlwaysOnSampler" in provider.sampler.get_description()

    def test_whitespace_standard_var_counts_as_unset(self):
        os.environ["OTEL_TRACES_SAMPLER"] = "  "
        os.environ["OTEL_HELPER_SAMPLE_RATIO"] = "0.25"
        _, opts = _provider(service_name="svc")
        assert opts.resolved.sample_ratio == 0.25

    def test_explicit_ratio_beats_standard_var(self):
        os.environ["OTEL_TRACES_SAMPLER"] = "always_off"
        provider, opts = _provider(service_name="svc", sample_ratio=0.5)
        assert opts.resolved.sample_ratio == 0.5
        assert isinstance(provider.sampler.inner, ParentBased)
        assert "root:TraceIdRatioBased{0.5}" in provider.sampler.get_description()


class TestDefaultIsParentBased:
    """With no sampler configured: parentbased_always_on semantics."""

    def test_root_span_sampled(self):
        provider, _ = _provider(service_name="svc")
        assert _sampled(provider, None) is True

    def test_remote_sampled_parent_sampled(self):
        provider, _ = _provider(service_name="svc")
        assert _sampled(provider, _remote(True)) is True

    def test_remote_unsampled_parent_not_sampled(self):
        provider, _ = _provider(service_name="svc")
        assert _sampled(provider, _remote(False)) is False

    def test_local_child_inherits_unsampled(self):
        provider, _ = _provider(service_name="svc")
        tracer = provider.get_tracer("t")
        parent = tracer.start_span("parent", context=_remote(False))
        assert _sampled(provider, trace.set_span_in_context(parent)) is False
        parent.end()

    def test_local_child_inherits_sampled(self):
        provider, _ = _provider(service_name="svc")
        tracer = provider.get_tracer("t")
        parent = tracer.start_span("parent", context=Context())
        assert _sampled(provider, trace.set_span_in_context(parent)) is True
        parent.end()


class TestRatioAlias:
    def test_alias_zero_still_follows_sampled_remote_parent(self):
        os.environ["OTEL_HELPER_SAMPLE_RATIO"] = "0"
        provider, _ = _provider(service_name="svc")
        assert _sampled(provider, _remote(True)) is True
        assert _sampled(provider, None) is False

    def test_alias_one_follows_unsampled_remote_parent(self):
        os.environ["OTEL_HELPER_SAMPLE_RATIO"] = "1"
        provider, _ = _provider(service_name="svc")
        assert _sampled(provider, _remote(False)) is False

    def test_code_ratio_zero_respects_parent(self):
        provider, _ = _provider(service_name="svc", sample_ratio=0.0)
        assert _sampled(provider, _remote(True)) is True
        assert _sampled(provider, None) is False


class TestStandardEnvVar:
    @pytest.mark.parametrize("value, root_sampled, remote_unsampled_sampled", [
        ("always_on", True, True),
        ("always_off", False, False),
        ("parentbased_always_on", True, False),
        ("parentbased_always_off", False, False),
        ("ALWAYS_ON", True, True),
        (" parentbased_always_off ", False, False),
    ])
    def test_known_names(self, value, root_sampled, remote_unsampled_sampled, caplog):
        os.environ["OTEL_TRACES_SAMPLER"] = value
        provider, _ = _provider(service_name="svc")
        assert _sampled(provider, None) is root_sampled
        assert _sampled(provider, _remote(False)) is remote_unsampled_sampled
        assert _helper_warnings(caplog) == []

    def test_traceidratio_uses_arg(self):
        os.environ["OTEL_TRACES_SAMPLER"] = "traceidratio"
        os.environ["OTEL_TRACES_SAMPLER_ARG"] = "0.3"
        sampler = _sampler(service_name="svc")
        assert isinstance(sampler, TraceIdRatioBased)
        assert sampler.rate == 0.3

    def test_parentbased_traceidratio_uses_arg(self):
        os.environ["OTEL_TRACES_SAMPLER"] = "parentbased_traceidratio"
        os.environ["OTEL_TRACES_SAMPLER_ARG"] = "0"
        provider, _ = _provider(service_name="svc")
        assert "root:TraceIdRatioBased{0.0}" in provider.sampler.get_description()
        assert _sampled(provider, _remote(True)) is True
        assert _sampled(provider, None) is False

    def test_ratio_without_arg_defaults_to_one_silently(self, caplog):
        os.environ["OTEL_TRACES_SAMPLER"] = "traceidratio"
        sampler = _sampler(service_name="svc")
        assert sampler.rate == 1.0
        assert _helper_warnings(caplog) == []

    @pytest.mark.parametrize("arg", ["abc", "2", "-0.1", "nan", "2.5e-1", "0.2_5", "+0.5", "0x1", "\uff10.5", ".5"])
    def test_invalid_arg_warns_and_uses_one(self, arg, caplog):
        # SDK 1.42 raises ValueError on an out-of-range _ARG; the helper must not crash.
        os.environ["OTEL_TRACES_SAMPLER"] = "parentbased_traceidratio"
        os.environ["OTEL_TRACES_SAMPLER_ARG"] = arg
        provider, _ = _provider(service_name="svc")
        assert "root:TraceIdRatioBased{1.0}" in provider.sampler.get_description()
        warnings = _helper_warnings(caplog)
        assert len(warnings) == 1 and "OTEL_TRACES_SAMPLER_ARG" in warnings[0]

    def test_unknown_value_warns_and_uses_default(self, caplog):
        os.environ["OTEL_TRACES_SAMPLER"] = "bogus"
        provider, _ = _provider(service_name="svc")
        assert _sampled(provider, None) is True
        assert _sampled(provider, _remote(False)) is False
        warnings = _helper_warnings(caplog)
        assert len(warnings) == 1
        assert "OTEL_TRACES_SAMPLER" in warnings[0] and "'bogus'" in warnings[0]

    def test_unknown_value_ignores_helper_alias(self, caplog):
        os.environ["OTEL_TRACES_SAMPLER"] = "bogus"
        os.environ["OTEL_HELPER_SAMPLE_RATIO"] = "0"
        provider, opts = _provider(service_name="svc")
        assert opts.resolved.sample_ratio == 1.0
        assert _sampled(provider, None) is True


class TestNumericGrammarAndUnsupported:
    """Design D3: plain-decimal grammar for _ARG and the alias; unsupported spec names."""

    @pytest.mark.parametrize("raw", ["2.5e-1", "0.2_5", "+0.5", "nan", "\uff10.5", ".5"])
    def test_alias_rejects_non_plain_decimal(self, raw, caplog):
        os.environ["OTEL_HELPER_SAMPLE_RATIO"] = raw
        with caplog.at_level(logging.WARNING, logger="otel_helper"):
            _, opts = _provider(service_name="svc")
        assert opts.resolved.sample_ratio == 1.0
        assert "OTEL_HELPER_SAMPLE_RATIO" in caplog.text

    @pytest.mark.parametrize("raw", ["0", "0.25", "1", "1.0"])
    def test_alias_accepts_plain_decimal(self, raw):
        os.environ["OTEL_HELPER_SAMPLE_RATIO"] = raw
        _, opts = _provider(service_name="svc")
        assert opts.resolved.sample_ratio == float(raw)

    @pytest.mark.parametrize("name", ["jaeger_remote", "parentbased_jaeger_remote", "XRAY"])
    def test_unsupported_spec_names_warn(self, name, caplog):
        os.environ["OTEL_TRACES_SAMPLER"] = name
        provider, _ = _provider(service_name="svc")
        warnings = _helper_warnings(caplog)
        assert len(warnings) == 1 and "unsupported by this helper" in warnings[0]
        assert _sampled(provider, _remote(False)) is False  # default parentbased_always_on


class TestCodeRatioEqualToDefault:
    """D1 A13 ruling: a code ratio wins whatever its value, 1.0 (the default) included."""

    @pytest.mark.parametrize("var,value", [("OTEL_TRACES_SAMPLER", "always_off"),
                                           ("OTEL_HELPER_SAMPLE_RATIO", "0")])
    def test_code_ratio_one_beats_env(self, var, value):
        os.environ[var] = value
        provider, opts = _provider(service_name="s", sample_ratio=1.0)
        assert opts.resolved.sample_ratio == 1.0
        assert "root:TraceIdRatioBased{1.0}" in build_sampler(opts).get_description()
        assert _sampled(provider, None)

    def test_unset_code_ratio_still_reads_alias(self):
        os.environ["OTEL_HELPER_SAMPLE_RATIO"] = "0"
        provider, opts = _provider(service_name="s")
        assert opts.resolved.sample_ratio == 0.0
        assert not _sampled(provider, None)

    def test_default_without_code_or_env_is_parentbased_always_on(self):
        _, opts = _provider(service_name="s")
        assert opts.resolved.sample_ratio == 1.0
        assert "root:AlwaysOnSampler" in build_sampler(opts).get_description()
