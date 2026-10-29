"""Tests for staffops_otel_helper.config — the 0.3 resolution contract (design D1, R1/R2/R4/R5/R8).

Pure resolution: no exporter is built here. Wire behavior is covered by the
parity fixture (``tests/test_parity.py``); exporter arguments by
``tests/test_shims.py``.
"""

import logging
import os

import pytest

from staffops_otel_helper.config import (
    DeploymentEnvironment,
    TelemetryOptions,
    _parse_environment,
    get_default_log_level,
    redact_url,
)


def _resolved(**kwargs) -> TelemetryOptions:
    opts = TelemetryOptions(**kwargs)
    opts.resolve_from_env()
    return opts


class TestDefaults:
    def test_nothing_set_in_code(self):
        opts = TelemetryOptions()
        assert opts.service_name is None
        assert opts.environment is None
        assert opts.export_timeout_ms is None
        assert opts.compression is None
        assert opts.insecure is None
        # None = "not set in code" (D1 A13 ruling); resolution never writes these fields.
        assert opts.debug_level is None
        assert opts.extra_instrumentation is None
        assert opts.sample_ratio is None
        assert opts.prometheus_metrics_port is None
        assert opts.disabled_signals is None
        assert opts.disabled_metrics is None

    def test_defaults_after_resolution(self):
        opts = _resolved(service_name="s")
        assert opts.sample_ratio is None and opts.debug_level is None  # code values untouched
        assert opts.resolved.debug_level is False
        assert opts.resolved.extra_instrumentation == "SQL"
        assert opts.resolved.sample_ratio == 1.0
        assert opts.resolved.prometheus_metrics_port == 9464
        assert opts.resolved.disabled_signals == []
        assert opts.resolved.disabled_metrics == []


class TestServiceName:
    """R5 / D1: code > OTEL_SERVICE_NAME > OTEL_RESOURCE_ATTRIBUTES > SERVICE_NAME > SDK default."""

    def test_otel_service_name_beats_legacy(self):
        os.environ["SERVICE_NAME"] = "legacy"
        os.environ["OTEL_SERVICE_NAME"] = "std"
        opts = _resolved()
        assert opts.service_name == "std"
        assert "service.name" not in opts._resource_overrides  # the SDK sets it

    def test_resource_attributes_beat_legacy(self):
        os.environ["SERVICE_NAME"] = "legacy"
        os.environ["OTEL_RESOURCE_ATTRIBUTES"] = "service.name=ra"
        opts = _resolved()
        assert opts.service_name == "ra"
        assert "service.name" not in opts._resource_overrides

    def test_legacy_alias_alone_is_used(self):
        os.environ["SERVICE_NAME"] = "legacy"
        opts = _resolved()
        assert opts._resource_overrides["service.name"] == "legacy"

    def test_code_wins(self):
        os.environ["OTEL_SERVICE_NAME"] = "std"
        opts = _resolved(service_name="explicit")
        assert opts._resource_overrides["service.name"] == "explicit"

    def test_code_resource_attribute_counts_as_configured(self, caplog):
        with caplog.at_level(logging.WARNING, logger="staffops_otel_helper"):
            opts = _resolved(resource_attributes={"service.name": "attr"})
        assert opts.service_name == "attr"
        assert "No service name" not in caplog.text

    def test_unset_sets_nothing_and_warns(self, caplog):
        with caplog.at_level(logging.WARNING, logger="staffops_otel_helper"):
            opts = _resolved()
        assert opts.service_name is None
        assert "service.name" not in opts._resource_overrides
        assert "OTEL_SERVICE_NAME" in caplog.text

    @pytest.mark.parametrize("bad", ["", "   "])
    def test_empty_code_value_raises(self, bad):
        with pytest.raises(ValueError, match="service_name"):
            _resolved(service_name=bad)


class TestEnvironment:
    """R4 / ADR-003: code > OTEL_RESOURCE_ATTRIBUTES > ENVIRONMENT > unset."""

    def test_unset_is_absent_and_warns_once(self, caplog):
        with caplog.at_level(logging.WARNING, logger="staffops_otel_helper"):
            opts = _resolved(service_name="s")
        assert opts.environment is None
        assert "deployment.environment.name" not in opts._resource_overrides
        assert caplog.text.count("deployment.environment.name") == 1

    def test_known_alias_value_normalized(self):
        os.environ["ENVIRONMENT"] = "prd"
        opts = _resolved(service_name="s")
        assert opts.environment == DeploymentEnvironment.PRD
        assert opts._resource_overrides["deployment.environment.name"] == "PRD"

    def test_unknown_alias_value_verbatim(self):
        os.environ["ENVIRONMENT"] = "staging"
        opts = _resolved(service_name="s")
        assert opts.environment == "staging"
        assert opts._resource_overrides["deployment.environment.name"] == "staging"

    def test_resource_attributes_beat_alias_and_are_not_overwritten(self):
        os.environ["ENVIRONMENT"] = "DEV"
        os.environ["OTEL_RESOURCE_ATTRIBUTES"] = "deployment.environment.name=PRD"
        opts = _resolved(service_name="s")
        assert opts.environment == DeploymentEnvironment.PRD
        assert "deployment.environment.name" not in opts._resource_overrides

    def test_code_enum_wins(self):
        os.environ["OTEL_RESOURCE_ATTRIBUTES"] = "deployment.environment.name=PRD"
        opts = _resolved(service_name="s", environment=DeploymentEnvironment.HML)
        assert opts._resource_overrides["deployment.environment.name"] == "HML"

    def test_code_string_accepted(self):
        opts = _resolved(service_name="s", environment="qa")
        assert opts.environment == "qa"
        assert opts._resource_overrides["deployment.environment.name"] == "qa"

    def test_code_resource_attribute_drives_behavior(self):
        opts = _resolved(service_name="s", resource_attributes={"deployment.environment.name": "LOCAL"})
        assert opts.environment == DeploymentEnvironment.LOCAL

    @pytest.mark.parametrize("bad", ["", 42])
    def test_invalid_code_value_raises(self, bad):
        with pytest.raises(ValueError, match="environment"):
            _resolved(service_name="s", environment=bad)


class TestHasInstrumentation:
    def test_from_env(self):
        os.environ["OTEL_HELPER_EXTRA_INSTRUMENTATION"] = "SQL,AWS"
        assert _resolved(service_name="s").resolved.extra_instrumentation == "SQL,AWS"

    def test_debug_level_from_env(self):
        os.environ["OTEL_HELPER_DEBUG_LEVEL"] = "true"
        assert _resolved(service_name="s").resolved.debug_level is True

    def test_sql_enabled_by_default(self):
        opts = TelemetryOptions()
        assert opts.has_instrumentation("SQL") is True

    def test_aws_not_enabled_by_default(self):
        opts = TelemetryOptions()
        assert opts.has_instrumentation("AWS") is False

    def test_debug_enables_all(self):
        opts = TelemetryOptions(debug_level=True)
        assert opts.has_instrumentation("SQL") is True
        assert opts.has_instrumentation("AWS") is True
        assert opts.has_instrumentation("ANYTHING") is True

    def test_case_insensitive(self):
        opts = TelemetryOptions(extra_instrumentation="sql,aws")
        assert opts.has_instrumentation("SQL") is True
        assert opts.has_instrumentation("Aws") is True


class TestLogLevel:
    @pytest.mark.parametrize("env,level", [
        (DeploymentEnvironment.LOCAL, logging.DEBUG),
        (DeploymentEnvironment.DEV, logging.INFO),
        (DeploymentEnvironment.HML, logging.INFO),
        (DeploymentEnvironment.PRD, logging.WARNING),
        (None, logging.INFO),
        ("staging", logging.INFO),
    ])
    def test_levels(self, env, level):
        assert get_default_log_level(env) == level

    def test_debug_overrides(self):
        assert get_default_log_level(DeploymentEnvironment.PRD, debug_level=True) == logging.DEBUG


class TestParseEnvironment:
    def test_case_insensitive_known(self):
        assert _parse_environment("Dev") == DeploymentEnvironment.DEV
        assert _parse_environment(" prd ") == DeploymentEnvironment.PRD

    def test_unknown_kept(self):
        assert _parse_environment("STAGING") == "STAGING"


class TestEnvParsingNeverRaises:
    """R8: env values warn and fall back; never raise."""

    @pytest.mark.parametrize("var,value", [
        ("OTEL_HELPER_DEBUG_LEVEL", "maybe"),
        ("OTEL_HELPER_SAMPLE_RATIO", "half"),
        ("OTEL_HELPER_METRICS_PORT", "http"),
        ("OTEL_SDK_DISABLED", "maybe"),
        ("OTEL_EXPORTER_OTLP_PROTOCOL", "http/json"),
        ("OTEL_EXPORTER_OTLP_INSECURE", "yes"),
        ("OTEL_EXPORTER_OTLP_TIMEOUT", "soon"),
        ("OTEL_EXPORTER_OTLP_COMPRESSION", "zstd"),
        ("OTEL_EXPORTER_OTLP_COMPRESSION", "deflate"),
        ("OTEL_EXPORTER_OTLP_HEADERS", "a=1,broken"),
        ("OTEL_TRACES_EXPORTER", "zipkin"),
        ("OTEL_LOGS_EXPORTER", "bogus"),
    ])
    def test_warns_naming_the_variable(self, var, value, caplog):
        os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = "http://collector:4317"
        os.environ[var] = value
        with caplog.at_level(logging.WARNING, logger="staffops_otel_helper"):
            opts = _resolved(service_name="s")
            opts.validate()
        assert var in caplog.text

    def test_defaults_after_bad_values(self):
        os.environ["OTEL_HELPER_SAMPLE_RATIO"] = "half"
        os.environ["OTEL_HELPER_METRICS_PORT"] = "http"
        os.environ["OTEL_HELPER_DEBUG_LEVEL"] = "maybe"
        opts = _resolved(service_name="s")
        assert opts.resolved.sample_ratio == 1.0
        assert opts.resolved.prometheus_metrics_port == 9464
        assert opts.resolved.debug_level is False

    def test_sdk_disabled_valid_values_do_not_warn(self, caplog):
        os.environ["OTEL_SDK_DISABLED"] = "TRUE"
        with caplog.at_level(logging.WARNING, logger="staffops_otel_helper"):
            _resolved(service_name="s")
        assert "OTEL_SDK_DISABLED" not in caplog.text


class TestSignalSwitches:
    """R8: OTEL_{TRACES,LOGS}_EXPORTER."""

    def test_default_is_otlp(self):
        assert _resolved(service_name="s").resolved_exporters("traces") == ["otlp"]

    def test_none(self):
        os.environ["OTEL_TRACES_EXPORTER"] = "none"
        assert _resolved(service_name="s").resolved_exporters("traces") == []

    def test_console_and_otlp(self):
        os.environ["OTEL_LOGS_EXPORTER"] = "otlp, CONSOLE"
        assert _resolved(service_name="s").resolved_exporters("logs") == ["otlp", "console"]

    def test_logging_alias(self, caplog):
        os.environ["OTEL_LOGS_EXPORTER"] = "logging"
        with caplog.at_level(logging.WARNING, logger="staffops_otel_helper"):
            opts = _resolved(service_name="s")
        assert opts.resolved_exporters("logs") == ["console"]
        assert "OTEL_LOGS_EXPORTER" in caplog.text

    def test_none_in_list_wins(self, caplog):
        os.environ["OTEL_TRACES_EXPORTER"] = "none,otlp"
        with caplog.at_level(logging.WARNING, logger="staffops_otel_helper"):
            opts = _resolved(service_name="s")
        assert opts.resolved_exporters("traces") == []
        assert "OTEL_TRACES_EXPORTER" in caplog.text

    def test_only_unknown_falls_back_to_default(self):
        os.environ["OTEL_TRACES_EXPORTER"] = "zipkin"
        assert _resolved(service_name="s").resolved_exporters("traces") == ["otlp"]

    def test_standard_var_beats_disabled_signals(self):
        os.environ["OTEL_HELPER_DISABLED_SIGNALS"] = "traces,logs"
        os.environ["OTEL_TRACES_EXPORTER"] = "otlp"
        opts = _resolved(service_name="s")
        assert opts.is_signal_enabled("traces")
        assert not opts.is_signal_enabled("logs")

    def test_code_disabled_signals_win(self):
        os.environ["OTEL_TRACES_EXPORTER"] = "otlp"
        opts = _resolved(service_name="s", disabled_signals=["traces"])
        assert not opts.is_signal_enabled("traces")


class TestOtlpConfiguredPerSignal:
    """R2 / D1: "OTLP configured" per signal."""

    def test_nothing_configured(self):
        opts = _resolved(service_name="s")
        assert [opts.is_otlp_configured(s) for s in ("traces", "metrics", "logs")] == [False] * 3
        assert opts.resolved_metric_exporters() == ["prometheus"]

    def test_per_signal_env_only(self):
        os.environ["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"] = "http://collector:4318/v1/traces"
        opts = _resolved(service_name="s")
        assert opts.is_otlp_configured("traces")
        assert not opts.is_otlp_configured("metrics")
        assert not opts.is_otlp_configured("logs")
        assert opts.resolved_metric_exporters() == ["prometheus"]

    def test_generic_env_configures_all(self):
        os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = "http://collector:4317"
        opts = _resolved(service_name="s")
        assert all(opts.is_otlp_configured(s) for s in ("traces", "metrics", "logs"))
        assert opts.resolved_metric_exporters() == ["otlp"]

    def test_code_per_signal(self):
        opts = _resolved(service_name="s", logs_endpoint="http://collector:4318/v1/logs")
        assert opts.is_otlp_configured("logs") and not opts.is_otlp_configured("traces")


class TestProtocol:
    """D1 protocol: code > per-signal env > generic env > port 4318 > grpc."""

    def test_default_grpc(self):
        assert _resolved(service_name="s", otel_endpoint="http://c:4317").resolved_otlp_protocol() == "grpc"

    def test_no_endpoint_defaults_to_grpc(self):
        assert TelemetryOptions().resolved_otlp_protocol() == "grpc"

    def test_port_4318_inferred(self):
        assert _resolved(service_name="s", otel_endpoint="http://c:4318").resolved_otlp_protocol() == "http/protobuf"

    def test_inference_uses_the_signal_endpoint(self):
        os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = "http://c:4317"
        os.environ["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"] = "http://c:4318/v1/traces"
        opts = _resolved(service_name="s")
        assert opts.resolved_otlp_protocol("traces") == "http/protobuf"
        assert opts.resolved_otlp_protocol("metrics") == "grpc"

    def test_per_signal_env_beats_generic(self):
        os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = "http://c:4317"
        os.environ["OTEL_EXPORTER_OTLP_PROTOCOL"] = "grpc"
        os.environ["OTEL_EXPORTER_OTLP_LOGS_PROTOCOL"] = "http/protobuf"
        opts = _resolved(service_name="s")
        assert opts.resolved_otlp_protocol("logs") == "http/protobuf"
        assert opts.resolved_otlp_protocol("traces") == "grpc"

    def test_env_case_and_whitespace_tolerant(self):
        os.environ["OTEL_EXPORTER_OTLP_PROTOCOL"] = "  HTTP/PROTOBUF  "
        assert _resolved(service_name="s", otel_endpoint="http://c:4317").resolved_otlp_protocol() == "http/protobuf"

    def test_code_beats_env(self):
        os.environ["OTEL_EXPORTER_OTLP_PROTOCOL"] = "http/protobuf"
        opts = _resolved(service_name="s", otel_endpoint="http://c:4317", otlp_protocol="grpc")
        assert opts.resolved_otlp_protocol() == "grpc"

    def test_unknown_env_warns_and_defaults(self, caplog):
        os.environ["OTEL_EXPORTER_OTLP_PROTOCOL"] = "http/json"
        with caplog.at_level(logging.WARNING, logger="staffops_otel_helper"):
            opts = _resolved(service_name="s", otel_endpoint="http://c:4317")
            opts.validate()
        assert opts.resolved_otlp_protocol() == "grpc"
        assert "OTEL_EXPORTER_OTLP_PROTOCOL" in caplog.text

    def test_unknown_code_raises(self):
        with pytest.raises(ValueError, match="Unknown OTLP protocol"):
            _resolved(service_name="s", otlp_protocol="http/json")

    def test_valid_code_values(self):
        for proto in ("grpc", "http/protobuf"):
            _resolved(service_name="s", otlp_protocol=proto).validate()


class TestCodeValidation:
    """R8: invalid code values raise naming the field."""

    @pytest.mark.parametrize("kwargs,match", [
        ({"export_timeout_ms": 0}, "export_timeout_ms"),
        ({"export_timeout_ms": 1.5}, "export_timeout_ms"),
        ({"export_interval_ms": -1}, "export_interval_ms"),
        ({"compression": "zstd"}, "compression"),
        ({"compression": "deflate"}, "compression"),
        ({"compression": 123}, "compression"),
        ({"otlp_protocol": 123}, "otlp_protocol"),
        ({"insecure": "yes"}, "insecure"),
        ({"sample_ratio": 1.5}, "sample_ratio"),
        ({"sample_ratio": -0.1}, "sample_ratio"),
        ({"sample_ratio": float("nan")}, "sample_ratio"),
        ({"prometheus_metrics_port": 70000}, "prometheus_metrics_port"),
        ({"prometheus_metrics_port": -1}, "prometheus_metrics_port"),
        ({"disabled_signals": ["traces", "profiles"]}, "disabled_signals"),
        ({"metric_exporters": "otlp"}, "metric_exporters"),
        ({"otel_endpoint": None}, "otel_endpoint"),
    ])
    def test_raises(self, kwargs, match):
        with pytest.raises(ValueError, match=match):
            _resolved(service_name="s", **kwargs).validate()

    def test_non_string_service_name_raises(self):
        with pytest.raises(ValueError, match="service_name"):
            _resolved(service_name=5)

    @pytest.mark.parametrize("endpoint", ["http://", "ftp://collector:4317", "http://[::1"])
    def test_invalid_code_endpoint_raises(self, endpoint):
        with pytest.raises(ValueError, match="otel_endpoint"):
            _resolved(service_name="s", otel_endpoint=endpoint)

    def test_invalid_code_per_signal_endpoint_raises(self):
        with pytest.raises(ValueError, match="traces_endpoint"):
            _resolved(service_name="s", traces_endpoint="http://:4318")

    @pytest.mark.parametrize("endpoint", ["http://", "ftp://collector:4317", "http://collector:port"])
    def test_invalid_env_endpoint_warns_and_skips(self, endpoint, caplog):
        os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = endpoint
        with caplog.at_level(logging.WARNING, logger="staffops_otel_helper"):
            opts = _resolved(service_name="s")
        assert not opts.is_otlp_configured("traces")
        assert "OTEL_EXPORTER_OTLP_ENDPOINT" in caplog.text


class TestRedaction:
    def test_userinfo_query_fragment_removed(self):
        assert redact_url("https://u:pw@host:4318/otlp?token=x#f") == "https://host:4318/otlp"

    def test_schemeless(self):
        assert redact_url("u:pw@host:4317") == "host:4317"

    def test_unparseable(self):
        assert redact_url("http://[::1") == "<unparseable URL>"

    @pytest.mark.parametrize("raw", ["http://user:s3cr/et@host:4317", "http://user:s3#cr@host:4317",
                                     "http://user:s3?cr@host:4317"])
    def test_password_with_url_delimiters(self, raw):
        assert "s3" not in redact_url(raw)

    @pytest.mark.parametrize("raw", ["http://user:s3cr/et@host:4317", "http://user:s3#cr@host:4317"])
    def test_invalid_endpoint_warning_never_shows_password(self, raw, caplog):
        os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = raw
        with caplog.at_level(logging.DEBUG):
            _resolved(service_name="s")
        assert "s3cr" not in caplog.text and "s3#" not in caplog.text and "et@" not in caplog.text


class TestValidationRound:
    """A3 validation rulings (design "D1 edge-case rulings")."""

    @pytest.mark.parametrize("raw,expected", [("TRUE", True), ("false", False), ("yes", False), ("1", False)])
    def test_boolean_env_anything_but_true_is_false(self, raw, expected, caplog):
        os.environ["OTEL_HELPER_DEBUG_LEVEL"] = raw
        with caplog.at_level(logging.WARNING, logger="staffops_otel_helper"):
            assert _resolved(service_name="s").resolved.debug_level is expected
        assert ("OTEL_HELPER_DEBUG_LEVEL" in caplog.text) is (raw.lower() not in ("true", "false"))

    def test_sdk_disabled_true(self):
        os.environ["OTEL_SDK_DISABLED"] = "True"
        assert _resolved(service_name="s").sdk_disabled is True

    def test_sdk_disabled_garbage_is_false(self, caplog):
        os.environ["OTEL_SDK_DISABLED"] = "maybe"
        with caplog.at_level(logging.WARNING, logger="staffops_otel_helper"):
            assert _resolved(service_name="s").sdk_disabled is False
        assert "OTEL_SDK_DISABLED" in caplog.text

    @pytest.mark.parametrize("var", ["OTEL_SERVICE_NAME", "SERVICE_NAME"])
    def test_whitespace_service_name_is_unset(self, var, caplog):
        os.environ[var] = "   "
        with caplog.at_level(logging.WARNING, logger="staffops_otel_helper"):
            opts = _resolved()
        assert opts.service_name is None
        assert "OTEL_SERVICE_NAME" in caplog.text

    def test_whitespace_otel_service_name_falls_back_to_legacy(self):
        os.environ["OTEL_SERVICE_NAME"] = "  "
        os.environ["SERVICE_NAME"] = "legacy"
        assert _resolved()._resource_overrides["service.name"] == "legacy"

    def test_env_ratio_clamped_with_warning(self, caplog):
        os.environ["OTEL_HELPER_SAMPLE_RATIO"] = "2"
        with caplog.at_level(logging.WARNING, logger="staffops_otel_helper"):
            assert _resolved(service_name="s").resolved.sample_ratio == 1.0
        assert "outside [0, 1]" in caplog.text

    def test_env_ratio_nan_ignored(self, caplog):
        os.environ["OTEL_HELPER_SAMPLE_RATIO"] = "nan"
        with caplog.at_level(logging.WARNING, logger="staffops_otel_helper"):
            assert _resolved(service_name="s").resolved.sample_ratio == 1.0
        assert "OTEL_HELPER_SAMPLE_RATIO" in caplog.text

    def test_env_port_out_of_range(self, caplog):
        os.environ["OTEL_HELPER_METRICS_PORT"] = "70000"
        with caplog.at_level(logging.WARNING, logger="staffops_otel_helper"):
            assert _resolved(service_name="s").resolved.prometheus_metrics_port == 9464

    @pytest.mark.parametrize("signal", ["traces", "logs"])
    def test_otlp_exporter_without_endpoint_warns(self, signal, caplog):
        os.environ[f"OTEL_{signal.upper()}_EXPORTER"] = "otlp"
        with caplog.at_level(logging.WARNING, logger="staffops_otel_helper"):
            _resolved(service_name="s")
        assert f"OTEL_{signal.upper()}_EXPORTER" in caplog.text

    def test_invalid_per_signal_endpoint_falls_back_to_generic(self, caplog):
        os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = "http://good:4317"
        os.environ["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"] = "http://[::1"
        with caplog.at_level(logging.WARNING, logger="staffops_otel_helper"):
            opts = _resolved(service_name="s")
        # The helper owns the URL so the SDK doesn't read the invalid per-signal value.
        assert opts.otlp_export("traces").kwargs["endpoint"] == "http://good:4317"
        assert "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT" in caplog.text

    def test_unsupported_per_signal_scheme_not_configured(self):
        os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = "http://good:4317"
        os.environ["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"] = "unix:///tmp/otlp.sock"
        opts = _resolved(service_name="s")
        assert not opts.is_otlp_configured("traces") and opts.is_otlp_configured("metrics")

    def test_unsupported_scheme_alone_not_configured(self):
        os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = "unix:///tmp/otlp.sock"
        assert not _resolved(service_name="s").is_otlp_configured("traces")

    def test_code_insecure_contradicting_code_scheme_raises(self):
        with pytest.raises(ValueError, match="insecure"):
            _resolved(service_name="s", otel_endpoint="https://c:4317", insecure=True)

    def test_code_insecure_with_env_url_conflict_only_warns(self, caplog):
        os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = "https://c:4317"
        with caplog.at_level(logging.WARNING, logger="staffops_otel_helper"):
            _resolved(service_name="s", insecure=True)
        assert "insecure=true" in caplog.text

    def test_get_default_log_level_normalizes_strings(self):
        assert get_default_log_level("prd") == logging.WARNING
        assert get_default_log_level("staging") == logging.INFO


class TestRound2:
    def test_code_disabled_signals_normalized(self):
        opts = _resolved(service_name="s", disabled_signals=[" Traces "])
        assert not opts.is_signal_enabled("traces")


class TestCodeValueEqualToDefaultBeatsEnv:
    """D1 A13 ruling: "set in code" is tracked with None sentinels, never by comparing to the default."""

    def test_metrics_port_9464_beats_env(self):
        os.environ["OTEL_HELPER_METRICS_PORT"] = "9999"
        assert _resolved(service_name="s", prometheus_metrics_port=9464).resolved.prometheus_metrics_port == 9464
        assert _resolved(service_name="s").resolved.prometheus_metrics_port == 9999

    def test_empty_disabled_metrics_beats_env(self):
        os.environ["OTEL_HELPER_DISABLED_METRICS"] = "http.*"
        assert _resolved(service_name="s", disabled_metrics=[]).resolved.disabled_metrics == []
        assert _resolved(service_name="s").resolved.disabled_metrics == ["http.*"]

    def test_empty_disabled_signals_beats_env(self):
        os.environ["OTEL_HELPER_DISABLED_SIGNALS"] = "traces"
        opts = _resolved(service_name="s", disabled_signals=[])
        assert opts.resolved.disabled_signals == [] and opts.is_signal_enabled("traces")
        assert not _resolved(service_name="s").is_signal_enabled("traces")

    def test_debug_level_false_beats_env(self):
        os.environ["OTEL_HELPER_DEBUG_LEVEL"] = "true"
        assert _resolved(service_name="s", debug_level=False).resolved.debug_level is False
        assert _resolved(service_name="s").resolved.debug_level is True

    def test_extra_instrumentation_sql_beats_env(self):
        os.environ["OTEL_HELPER_EXTRA_INSTRUMENTATION"] = "REDIS"
        opts = _resolved(service_name="s", extra_instrumentation="SQL")
        assert opts.resolved.extra_instrumentation == "SQL" and opts.has_instrumentation("SQL")
        assert not opts.has_instrumentation("REDIS")
        assert _resolved(service_name="s").resolved.extra_instrumentation == "REDIS"

    def test_has_instrumentation_before_resolution_uses_default(self):
        assert TelemetryOptions().has_instrumentation("sql")

    def test_minimum_log_level_notset_is_kept(self):
        from staffops_otel_helper.logging_setup import build_logging_level

        opts = _resolved(service_name="s", environment=DeploymentEnvironment.PRD, minimum_log_level=logging.NOTSET)
        assert build_logging_level(opts) == logging.NOTSET
        assert build_logging_level(_resolved(service_name="s", environment=DeploymentEnvironment.PRD)) \
            == logging.WARNING

    @pytest.mark.parametrize("kwargs,option", [
        ({"debug_level": "yes"}, "debug_level"),
        ({"extra_instrumentation": 1}, "extra_instrumentation"),
        ({"disabled_metrics": "http.*"}, "disabled_metrics"),
        ({"disabled_metrics": [1]}, "disabled_metrics"),
    ])
    def test_wrong_types_fail_fast(self, kwargs, option):
        with pytest.raises(ValueError, match=option):
            _resolved(service_name="s", **kwargs)


class TestProvenanceSurvivesCopiesAndResetup:
    """A13 review H1/M1/M3: public fields only ever hold code values; ``resolved`` holds the effective ones."""

    def test_ratio_assigned_after_construction_is_code(self):
        from staffops_otel_helper.tracing import build_sampler

        os.environ["OTEL_TRACES_SAMPLER"] = "always_on"
        opts = TelemetryOptions(service_name="s")
        opts.sample_ratio = 0.1
        opts.resolve_from_env()
        assert opts.resolved.sample_ratio == 0.1 and opts.resolved.sample_ratio_set
        assert "root:TraceIdRatioBased{0.1}" in build_sampler(opts).get_description()

    def test_replace_of_resolved_options_rereads_env(self):
        import dataclasses

        from staffops_otel_helper.tracing import build_sampler

        resolved = _resolved(service_name="s")
        assert resolved.sample_ratio is None and resolved.prometheus_metrics_port is None
        os.environ["OTEL_TRACES_SAMPLER"] = "always_off"
        os.environ["OTEL_HELPER_METRICS_PORT"] = "9999"
        copy = dataclasses.replace(resolved, health_paths=["/ping"])
        copy.resolve_from_env()
        assert build_sampler(copy).get_description() == "AlwaysOffSampler"
        assert copy.resolved.prometheus_metrics_port == 9999
        explicit = dataclasses.replace(resolved, sample_ratio=0.2)
        explicit.resolve_from_env()
        assert "root:TraceIdRatioBased{0.2}" in build_sampler(explicit).get_description()

    def test_second_resolution_rereads_env(self):
        os.environ["OTEL_HELPER_SAMPLE_RATIO"] = "0.25"
        os.environ["OTEL_HELPER_METRICS_PORT"] = "9999"
        os.environ["OTEL_HELPER_DISABLED_METRICS"] = "http.*"
        opts = _resolved(service_name="s", export_timeout_ms=500)
        assert opts.resolved.sample_ratio == 0.25 and opts.resolved.sample_ratio_set
        for var in ("OTEL_HELPER_SAMPLE_RATIO", "OTEL_HELPER_METRICS_PORT", "OTEL_HELPER_DISABLED_METRICS"):
            del os.environ[var]
        os.environ["OTEL_METRIC_EXPORT_INTERVAL"] = "1000"
        opts.resolve_from_env()
        assert opts.resolved.sample_ratio == 1.0 and not opts.resolved.sample_ratio_set  # alias not sticky
        assert opts.resolved.prometheus_metrics_port == 9464
        assert opts.resolved.disabled_metrics == []
        assert opts.resolved.export_interval_ms == 1000
        assert opts.export_timeout_ms == 500  # code value kept

    def test_resolved_before_resolution_uses_code_then_default(self):
        opts = TelemetryOptions(prometheus_metrics_port=0, disabled_metrics=("a.*",))
        assert opts.resolved.prometheus_metrics_port == 0
        assert opts.resolved.disabled_metrics == ["a.*"]
        assert opts.resolved.sample_ratio == 1.0 and not opts.resolved.sample_ratio_set
        assert opts.resolved.metric_exporters is None

    def test_tuples_and_int_debug_accepted(self):
        opts = _resolved(service_name="s", disabled_metrics=("http.*",), disabled_signals=("Logs",), debug_level=1)
        assert opts.disabled_metrics == ["http.*"] and opts.resolved.disabled_metrics == ["http.*"]
        assert opts.disabled_signals == ["logs"] and not opts.is_signal_enabled("logs")
        assert opts.debug_level is True and opts.resolved.debug_level is True
        assert _resolved(service_name="s", debug_level=0).resolved.debug_level is False

    def test_metric_exporters_tuple_accepted(self):
        opts = _resolved(service_name="s", metric_exporters=("prometheus",))
        assert opts.metric_exporters == ["prometheus"]
        assert opts.resolved_metric_exporters() == ["prometheus"]

    def test_metric_exporters_wrong_type_fails(self):
        with pytest.raises(ValueError, match="metric_exporters"):
            _resolved(service_name="s", metric_exporters="prometheus")
