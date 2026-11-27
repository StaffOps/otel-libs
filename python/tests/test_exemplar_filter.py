"""OTEL_METRICS_EXEMPLAR_FILTER (task A33, design D1 row "Metrics exporters / interval / exemplars")."""

import logging

import pytest
from opentelemetry.sdk.metrics import AlwaysOffExemplarFilter, AlwaysOnExemplarFilter, TraceBasedExemplarFilter
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider

from staffops_otel_helper import metrics as m
from staffops_otel_helper import setup_telemetry
from staffops_otel_helper.config import ENV_EXEMPLAR_FILTER, TelemetryOptions

_LOGGER = "staffops_otel_helper"


def _resolve(**kw) -> TelemetryOptions:
    opts = TelemetryOptions(service_name="s", **kw)
    opts.resolve_from_env()
    return opts


def _filter_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if ENV_EXEMPLAR_FILTER in r.getMessage()]


@pytest.mark.parametrize("value, name", [
    (None, "trace_based"),
    ("", "trace_based"),
    ("   ", "trace_based"),
    ("trace_based", "trace_based"),
    ("always_on", "always_on"),
    ("always_off", "always_off"),
    (" ALWAYS_OFF\t", "always_off"),  # contract trim, ASCII case-insensitive
    ("Always_On", "always_on"),
])
def test_known_values(monkeypatch, caplog, value, name):
    if value is None:
        monkeypatch.delenv(ENV_EXEMPLAR_FILTER, raising=False)
    else:
        monkeypatch.setenv(ENV_EXEMPLAR_FILTER, value)
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        assert _resolve().resolved.exemplar_filter == name
    assert not _filter_lines(caplog)


@pytest.mark.parametrize("value", [
    "sometimes",
    "always-on",
    " always_off",  # NBSP is not in the contract trim set: str.strip() would accept it
    "x" * 200,
])
def test_unknown_value_warns_once_and_uses_trace_based(monkeypatch, caplog, value):
    monkeypatch.setenv(ENV_EXEMPLAR_FILTER, value)
    opts = TelemetryOptions(service_name="s")
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        opts.resolve_from_env()
        opts.resolve_from_env()
        assert opts.resolved.exemplar_filter == "trace_based"
    lines = _filter_lines(caplog)
    assert len(lines) == 1, lines
    assert "using trace_based" in lines[0]
    if len(value) > 64:  # echoed value cut to 64 code points plus an ellipsis
        assert "x" * 64 + "…" in lines[0]
        assert "x" * 65 not in lines[0]


def test_unknown_value_warns_with_sdk_disabled(monkeypatch, caplog):
    # Parsed at every setup, also when no MeterProvider is built (as .NET and A29).
    monkeypatch.setenv(ENV_EXEMPLAR_FILTER, "sometimes")
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        setup_telemetry(TelemetryOptions(service_name="s"))
    assert len(_filter_lines(caplog)) == 1


@pytest.mark.parametrize("value, sampled_span, want", [
    (None, False, False),
    (None, True, True),
    ("always_on", False, True),
    ("always_off", True, False),
    ("sometimes", True, True),  # warned, trace_based
])
def test_filter_changes_recorded_exemplars(monkeypatch, value, sampled_span, want):
    """The resolved filter reaches the helper's MeterProvider and decides what is recorded."""
    if value is None:
        monkeypatch.delenv(ENV_EXEMPLAR_FILTER, raising=False)
    else:
        monkeypatch.setenv(ENV_EXEMPLAR_FILTER, value)
    monkeypatch.setenv("OTEL_HELPER_METRICS_PORT", "0")  # no standalone listener
    reader = InMemoryMetricReader()
    real = m.MeterProvider
    seen = {}

    def spy(**kw):
        seen.update(kw)
        kw["metric_readers"] = list(kw.get("metric_readers", [])) + [reader]
        return real(**kw)
    monkeypatch.setattr(m, "MeterProvider", spy)
    provider, listener, _ = m.build_metrics(Resource.create({"service.name": "s"}), _resolve())
    try:
        expected_cls = {None: TraceBasedExemplarFilter, "always_on": AlwaysOnExemplarFilter,
                        "always_off": AlwaysOffExemplarFilter, "sometimes": TraceBasedExemplarFilter}[value]
        assert type(seen["exemplar_filter"]) is expected_cls
        counter = provider.get_meter("t").create_counter("a33.count")
        if sampled_span:
            with TracerProvider().get_tracer("t").start_as_current_span("s"):
                counter.add(1)
        else:
            counter.add(1)
        data = reader.get_metrics_data()
        points = [p for rm in data.resource_metrics for sm in rm.scope_metrics for mt in sm.metrics
                  if mt.name == "a33.count" for p in mt.data.data_points]
        assert len(points) == 1
        assert bool(points[0].exemplars) is want
    finally:
        m.stop_listener(listener)
        provider.shutdown()
