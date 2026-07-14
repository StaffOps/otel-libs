"""Tests for otel_helper.config — mirrors OptionsTests.cs + ValidationTests.cs."""

import os
import logging
import pytest

from otel_helper.config import (
    TelemetryOptions,
    DeploymentEnvironment,
    get_default_log_level,
    _parse_environment,
)


class TestDefaults:
    def test_default_service_name(self):
        opts = TelemetryOptions()
        assert opts.service_name == "my-service"

    def test_default_environment(self):
        opts = TelemetryOptions()
        assert opts.environment == DeploymentEnvironment.LOCAL

    def test_default_debug_level(self):
        opts = TelemetryOptions()
        assert opts.debug_level is False

    def test_default_extra_instrumentation(self):
        opts = TelemetryOptions()
        assert opts.extra_instrumentation == "SQL"

    def test_default_export_timeout(self):
        opts = TelemetryOptions()
        assert opts.export_timeout_ms == 10_000


class TestEnvResolution:
    def setup_method(self):
        for var in ["SERVICE_NAME", "OTEL_SERVICE_NAME", "ENVIRONMENT",
                    "OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_HELPER_DEBUG_LEVEL",
                    "OTEL_HELPER_EXTRA_INSTRUMENTATION", "OTEL_EXPORTER_OTLP_INSECURE"]:
            os.environ.pop(var, None)

    def test_service_name_from_env(self):
        os.environ["SERVICE_NAME"] = "checkout-api"
        opts = TelemetryOptions()
        opts.resolve_from_env()
        assert opts.service_name == "checkout-api"

    def test_otel_service_name_fallback(self):
        os.environ["OTEL_SERVICE_NAME"] = "otel-name"
        opts = TelemetryOptions()
        opts.resolve_from_env()
        assert opts.service_name == "otel-name"

    def test_service_name_priority(self):
        os.environ["SERVICE_NAME"] = "primary"
        os.environ["OTEL_SERVICE_NAME"] = "secondary"
        opts = TelemetryOptions()
        opts.resolve_from_env()
        assert opts.service_name == "primary"

    def test_environment_from_env(self):
        os.environ["ENVIRONMENT"] = "PRD"
        opts = TelemetryOptions()
        opts.resolve_from_env()
        assert opts.environment == DeploymentEnvironment.PRD

    def test_environment_invalid_falls_back_to_local(self):
        os.environ["ENVIRONMENT"] = "INVALID"
        opts = TelemetryOptions()
        opts.resolve_from_env()
        assert opts.environment == DeploymentEnvironment.LOCAL

    def test_collector_endpoint_from_env(self):
        os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = "http://custom-collector.svc:4317"
        opts = TelemetryOptions()
        opts.resolve_from_env()
        assert "custom-collector" in opts.otel_endpoint

    def test_collector_endpoint_default_empty(self):
        """When OTEL_EXPORTER_OTLP_ENDPOINT is not set, endpoint stays empty (Prometheus fallback)."""
        opts = TelemetryOptions()
        opts.resolve_from_env()
        assert opts.otel_endpoint == ""

    def test_collector_endpoint_preserves_custom_port(self):
        os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = "https://gateway:14317"
        opts = TelemetryOptions()
        opts.resolve_from_env()
        assert opts.otel_endpoint == "https://gateway:14317"

    def test_collector_endpoint_defaults_port_when_absent(self):
        os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = "https://gateway"
        opts = TelemetryOptions()
        opts.resolve_from_env()
        assert opts.otel_endpoint == "https://gateway:4317"

    def test_collector_endpoint_schemeless_preserves_port(self):
        os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = "gateway:14317"
        opts = TelemetryOptions()
        opts.resolve_from_env()
        assert opts.otel_endpoint == "https://gateway:14317"

    def test_collector_endpoint_strips_path(self):
        """Scheme+host+port are kept; any path suffix is dropped."""
        os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = "http://collector.svc:4318/v1/traces"
        opts = TelemetryOptions()
        opts.resolve_from_env()
        assert opts.otel_endpoint == "http://collector.svc:4318"

    def test_debug_level_from_env(self):
        os.environ["OTEL_HELPER_DEBUG_LEVEL"] = "true"
        opts = TelemetryOptions()
        opts.resolve_from_env()
        assert opts.debug_level is True

    def test_extra_instrumentation_from_env(self):
        os.environ["OTEL_HELPER_EXTRA_INSTRUMENTATION"] = "SQL,AWS"
        opts = TelemetryOptions()
        opts.resolve_from_env()
        assert opts.extra_instrumentation == "SQL,AWS"

    def test_explicit_value_overrides_env(self):
        os.environ["SERVICE_NAME"] = "from-env"
        opts = TelemetryOptions(service_name="explicit")
        opts.resolve_from_env()
        assert opts.service_name == "explicit"


class TestValidation:
    def test_valid_options(self):
        opts = TelemetryOptions(
            service_name="test",
            otel_endpoint="http://localhost:4317",
        )
        opts.validate()  # Should not raise

    def test_empty_service_name_fails(self):
        opts = TelemetryOptions(service_name="", otel_endpoint="http://localhost:4317")
        with pytest.raises(ValueError, match="ServiceName"):
            opts.validate()

    def test_empty_endpoint_is_valid(self):
        """Empty endpoint means Prometheus fallback mode — no longer an error."""
        opts = TelemetryOptions(service_name="test", otel_endpoint="")
        opts.validate()  # Should not raise

    def test_invalid_endpoint_fails(self):
        opts = TelemetryOptions(service_name="test", otel_endpoint="not-a-uri")
        with pytest.raises(ValueError, match="not a valid URI"):
            opts.validate()

    def test_zero_timeout_fails(self):
        opts = TelemetryOptions(
            service_name="test",
            otel_endpoint="http://localhost:4317",
            export_timeout_ms=0,
        )
        with pytest.raises(ValueError, match="ExportTimeoutMs"):
            opts.validate()


class TestHasInstrumentation:
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
    def test_local_debug(self):
        assert get_default_log_level(DeploymentEnvironment.LOCAL) == logging.DEBUG

    def test_dev_info(self):
        assert get_default_log_level(DeploymentEnvironment.DEV) == logging.INFO

    def test_hml_info(self):
        assert get_default_log_level(DeploymentEnvironment.HML) == logging.INFO

    def test_prd_warning(self):
        assert get_default_log_level(DeploymentEnvironment.PRD) == logging.WARNING

    def test_debug_override(self):
        assert get_default_log_level(DeploymentEnvironment.PRD, debug_level=True) == logging.DEBUG


class TestEnvironmentParsing:
    def test_valid_values(self):
        assert _parse_environment("LOCAL") == DeploymentEnvironment.LOCAL
        assert _parse_environment("DEV") == DeploymentEnvironment.DEV
        assert _parse_environment("HML") == DeploymentEnvironment.HML
        assert _parse_environment("PRD") == DeploymentEnvironment.PRD

    def test_case_insensitive(self):
        assert _parse_environment("prd") == DeploymentEnvironment.PRD
        assert _parse_environment("Dev") == DeploymentEnvironment.DEV

    def test_invalid_falls_back(self):
        assert _parse_environment("STAGING") == DeploymentEnvironment.LOCAL


class TestResolveInsecure:
    """Tests for TelemetryOptions.resolve_insecure() — TLS auto-detection."""

    def test_https_endpoint_is_secure(self):
        opts = TelemetryOptions(otel_endpoint="https://otel-gateway:4317")
        assert opts.resolve_insecure() is False

    def test_http_endpoint_is_insecure(self):
        opts = TelemetryOptions(otel_endpoint="http://collector.svc:4317")
        assert opts.resolve_insecure() is True

    def test_empty_endpoint_is_insecure(self):
        opts = TelemetryOptions(otel_endpoint="")
        assert opts.resolve_insecure() is True

    def test_env_override_false_with_http_endpoint(self):
        os.environ["OTEL_EXPORTER_OTLP_INSECURE"] = "false"
        opts = TelemetryOptions(otel_endpoint="http://collector:4317")
        assert opts.resolve_insecure() is False

    def test_env_override_true_with_https_endpoint(self):
        os.environ["OTEL_EXPORTER_OTLP_INSECURE"] = "true"
        opts = TelemetryOptions(otel_endpoint="https://gateway:4317")
        assert opts.resolve_insecure() is True

    def test_env_override_case_insensitive(self):
        os.environ["OTEL_EXPORTER_OTLP_INSECURE"] = "False"
        opts = TelemetryOptions(otel_endpoint="http://collector:4317")
        assert opts.resolve_insecure() is False

    def test_env_override_with_whitespace(self):
        os.environ["OTEL_EXPORTER_OTLP_INSECURE"] = "  true  "
        opts = TelemetryOptions(otel_endpoint="https://gateway:4317")
        assert opts.resolve_insecure() is True

    def test_explicit_insecure_false_overrides_scheme(self):
        opts = TelemetryOptions(otel_endpoint="http://collector:4317", insecure=False)
        assert opts.resolve_insecure() is False

    def test_explicit_insecure_true_overrides_scheme(self):
        opts = TelemetryOptions(otel_endpoint="https://gateway:4317", insecure=True)
        assert opts.resolve_insecure() is True

    def test_explicit_overrides_env(self):
        os.environ["OTEL_EXPORTER_OTLP_INSECURE"] = "true"
        opts = TelemetryOptions(otel_endpoint="http://collector:4317", insecure=False)
        assert opts.resolve_insecure() is False

    def test_default_insecure_is_none(self):
        opts = TelemetryOptions()
        assert opts.insecure is None
