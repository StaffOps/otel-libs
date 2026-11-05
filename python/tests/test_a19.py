"""Task A19: contract rulings from the Node B4c work (design D1 rows dated
2026-10-29): OTEL_HELPER_DISABLED_SIGNALS vs the standard variables and the
code options, the ``logging`` alias in code, code exporter/signal lists, the
metric export interval grammar and bound, OTEL_CONFIG_FILE, echoed-value
truncation.
"""

import logging

import pytest

from staffops_otel_helper import _diag
from staffops_otel_helper.config import TelemetryOptions


def _opts(**kwargs):
    opts = TelemetryOptions(service_name="s", **kwargs)
    opts.resolve_from_env()
    return opts


def _helper_warnings(caplog):
    return [r.getMessage() for r in caplog.records
            if r.name.startswith("staffops_otel_helper") and r.levelno >= logging.WARNING]


class TestDisabledSignals:
    def test_standard_variable_wins_and_unknown_warns_once(self, monkeypatch, caplog):
        caplog.set_level(logging.WARNING)
        monkeypatch.setenv("OTEL_HELPER_DISABLED_SIGNALS", "traces, Metrics,profiles,profiles,PROFILES,logs")
        monkeypatch.setenv("OTEL_TRACES_EXPORTER", "otlp")
        opts = _opts()
        assert opts.is_signal_enabled("traces")
        assert not opts.is_signal_enabled("metrics")
        assert not opts.is_signal_enabled("logs")
        assert sum("profiles" in m for m in _helper_warnings(caplog)) == 1

    def test_code_wins(self, monkeypatch):
        monkeypatch.setenv("OTEL_HELPER_DISABLED_SIGNALS", "logs")
        assert _opts(disabled_signals=[]).is_signal_enabled("logs")

    def test_standard_variable_set_only_with_a_valid_entry(self, monkeypatch, caplog):
        caplog.set_level(logging.WARNING)
        monkeypatch.setenv("OTEL_HELPER_DISABLED_SIGNALS", "traces,metrics,logs")
        monkeypatch.setenv("OTEL_TRACES_EXPORTER", "bogus")
        monkeypatch.setenv("OTEL_LOGS_EXPORTER", " , ,")
        monkeypatch.setenv("OTEL_METRICS_EXPORTER", "none")
        opts = _opts()
        assert not opts.is_signal_enabled("traces")
        assert not opts.is_signal_enabled("logs")
        assert opts.is_signal_enabled("metrics")
        assert any("bogus" in m for m in _helper_warnings(caplog))

    def test_code_metric_exporters_win(self, monkeypatch):
        monkeypatch.setenv("OTEL_HELPER_DISABLED_SIGNALS", "metrics")
        assert _opts(metric_exporters=["console"]).is_signal_enabled("metrics")

    def test_code_disabled_signals_trimmed_lowered_deduplicated(self):
        opts = _opts(disabled_signals=[" Traces ", "traces", "LOGS"])
        opts.validate()
        assert opts.resolved.disabled_signals == ["traces", "logs"]
        assert not opts.is_signal_enabled("traces")

    def test_long_values_are_truncated_in_warnings(self, monkeypatch, caplog):
        caplog.set_level(logging.WARNING)
        monkeypatch.setenv("OTEL_HELPER_DISABLED_SIGNALS", "x" * 200)
        _opts()
        hit = [m for m in _helper_warnings(caplog) if "unknown signal" in m][0]
        assert "x" * 64 + "…" in hit and "x" * 65 not in hit


class TestExporterLists:
    def test_code_logging_fails(self):
        with pytest.raises(ValueError, match="logging"):
            _opts(metric_exporters=["logging"]).validate()

    def test_env_logging_is_console(self, monkeypatch, caplog):
        caplog.set_level(logging.WARNING)
        monkeypatch.setenv("OTEL_METRICS_EXPORTER", "logging")
        assert _opts().resolved_metric_exporters() == ["console"]
        assert any("logging" in m for m in _helper_warnings(caplog))

    def test_code_none_duplicated_disables(self):
        opts = _opts(metric_exporters=["none", " NONE "])
        opts.validate()
        assert opts.resolved_metric_exporters() == []

    def test_code_duplicates_dropped(self):
        assert _opts(metric_exporters=["console", " Console"]).resolved_metric_exporters() == ["console"]


class TestExportInterval:
    @pytest.mark.parametrize("raw,want,warning", [
        ("5000", 5000, None),
        ("+5000", 5000, None),
        ("007", 7, None),
        ("0" * 5000 + "5", 5, None),
        ("2147483647", 2147483647, None),
        ("2147483648", 2147483647, "clamped"),
        ("99999999999999999999999", 2147483647, "clamped"),
        ("9" * 5000, 2147483647, "clamped"),  # int() raises above 4300 digits
        ("0", 30000, "not a positive integer"),
        ("-5", 30000, "not a positive integer"),
        ("+", 30000, "not a positive integer"),
        ("5_000", 30000, "not a positive integer"),
        ("5000.0", 30000, "not a positive integer"),
        ("٥٠٠٠", 30000, "not a positive integer"),  # Arabic-Indic digits: int() accepts them
    ])
    def test_env_grammar(self, monkeypatch, caplog, raw, want, warning):
        caplog.set_level(logging.WARNING)
        monkeypatch.setenv("OTEL_METRIC_EXPORT_INTERVAL", raw)
        assert _opts().resolved.export_interval_ms == want
        hits = [m for m in _helper_warnings(caplog) if "OTEL_METRIC_EXPORT_INTERVAL" in m]
        if warning is None:
            assert hits == []
        else:
            assert len(hits) == 1 and warning in hits[0]

    @pytest.mark.parametrize("value", [0, -1, 2147483648])
    def test_code_bound(self, value):
        with pytest.raises(ValueError, match="export_interval_ms"):
            _opts(export_interval_ms=value).validate()

    def test_code_max_accepted(self):
        _opts(export_interval_ms=2147483647).validate()


class TestConfigFile:
    def test_warns_once_per_process_naming_the_variable(self, monkeypatch, caplog):
        caplog.set_level(logging.WARNING)
        _diag._reset()
        monkeypatch.setenv("OTEL_CONFIG_FILE", "/etc/secret-path/otel.yaml")
        monkeypatch.setenv("OTEL_EXPERIMENTAL_CONFIG_FILE", "/etc/secret-path/otel.yaml")
        _opts()
        _opts()
        for var in ("OTEL_CONFIG_FILE", "OTEL_EXPERIMENTAL_CONFIG_FILE"):
            hits = [m for m in _helper_warnings(caplog) if m.startswith(f"[OtelHelper] {var} ")]
            assert len(hits) == 1
            assert "secret-path" not in hits[0]
