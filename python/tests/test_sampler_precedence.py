"""US-2: OTEL_TRACES_SAMPLER (standard SDK var) wins over OTEL_HELPER_SAMPLE_RATIO."""

import os

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace.sampling import ParentBased, StaticSampler, TraceIdRatioBased

from otel_helper.config import TelemetryOptions
from otel_helper.tracing import configure_tracing


def _provider(**kwargs):
    opts = TelemetryOptions(**kwargs)
    opts.resolve_from_env()
    return configure_tracing(Resource.create({}), opts), opts


class TestSamplerPrecedence:
    def test_helper_var_only_applies_ratio(self):
        os.environ["OTEL_HELPER_SAMPLE_RATIO"] = "0.25"
        provider, opts = _provider(service_name="svc")
        assert opts.sample_ratio == 0.25
        assert isinstance(provider.sampler, TraceIdRatioBased)

    def test_standard_var_only_wins(self):
        os.environ["OTEL_TRACES_SAMPLER"] = "always_off"
        provider, _ = _provider(service_name="svc")
        assert isinstance(provider.sampler, StaticSampler)

    def test_standard_var_beats_helper_var(self):
        os.environ["OTEL_TRACES_SAMPLER"] = "parentbased_always_on"
        os.environ["OTEL_HELPER_SAMPLE_RATIO"] = "0.25"
        provider, opts = _provider(service_name="svc")
        # Helper ratio is NOT applied; the SDK env config decides.
        assert opts.sample_ratio == 1.0
        assert isinstance(provider.sampler, ParentBased)

    def test_explicit_ratio_beats_standard_var(self):
        os.environ["OTEL_TRACES_SAMPLER"] = "always_off"
        provider, opts = _provider(service_name="svc", sample_ratio=0.5)
        # Explicit code config wins over the standard env var.
        assert opts.sample_ratio == 0.5
        assert isinstance(provider.sampler, TraceIdRatioBased)
