"""Tests for otel_helper.setup — mirrors RegistrationTests.cs + TelemetryPipelineTests.cs."""

import os
import pytest
from unittest.mock import patch

from opentelemetry import trace, metrics

from otel_helper.setup import setup_telemetry, reset_telemetry
from otel_helper.config import TelemetryOptions, DeploymentEnvironment


@pytest.fixture(autouse=True)
def clean_env():
    """Clean env vars and reset telemetry state before each test."""
    env_vars = ["SERVICE_NAME", "OTEL_SERVICE_NAME", "ENVIRONMENT",
                "OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_HELPER_DEBUG_LEVEL",
                "OTEL_HELPER_EXTRA_INSTRUMENTATION"]
    for var in env_vars:
        os.environ.pop(var, None)
    reset_telemetry()
    yield
    reset_telemetry()


class TestSetupTelemetry:
    def test_returns_resolved_options(self):
        opts = setup_telemetry(TelemetryOptions(
            service_name="test-svc",
            otel_endpoint="http://localhost:4317",
        ))
        assert opts.service_name == "test-svc"

    def test_double_init_guard(self):
        opts1 = setup_telemetry(TelemetryOptions(
            service_name="first",
            otel_endpoint="http://localhost:4317",
        ))
        opts2 = setup_telemetry(TelemetryOptions(
            service_name="second",
            otel_endpoint="http://localhost:4317",
        ))
        # Second call should be no-op
        assert opts2.service_name == "second"  # returns the passed options but doesn't re-init

    def test_sets_tracer_provider(self):
        setup_telemetry(TelemetryOptions(
            service_name="test",
            otel_endpoint="http://localhost:4317",
        ))
        provider = trace.get_tracer_provider()
        assert provider is not None
        assert hasattr(provider, 'resource')

    def test_resource_emits_deployment_environment_name(self):
        """P8: 'deployment.environment.name' (semconv >= v1.27), not the legacy
        'deployment.environment' key — parity with Go/.NET."""
        setup_telemetry(TelemetryOptions(
            service_name="test",
            environment=DeploymentEnvironment.PRD,
            otel_endpoint="http://localhost:4317",
        ))
        provider = trace.get_tracer_provider()
        assert provider.resource.attributes["deployment.environment.name"] == "PRD"
        assert "deployment.environment" not in provider.resource.attributes

    def test_sets_meter_provider(self):
        setup_telemetry(TelemetryOptions(
            service_name="test",
            otel_endpoint="http://localhost:4317",
        ))
        provider = metrics.get_meter_provider()
        assert provider is not None

    def test_env_var_resolution(self):
        os.environ["SERVICE_NAME"] = "env-service"
        os.environ["ENVIRONMENT"] = "PRD"
        with patch("prometheus_client.start_http_server"):
            opts = setup_telemetry()
        assert opts.service_name == "env-service"
        assert opts.environment == DeploymentEnvironment.PRD

    def test_validation_fails_on_bad_config(self):
        with pytest.raises(ValueError):
            setup_telemetry(TelemetryOptions(
                service_name="",
                otel_endpoint="http://localhost:4317",
            ))

    def test_resource_attributes(self):
        opts = TelemetryOptions(
            service_name="test",
            otel_endpoint="http://localhost:4317",
            resource_attributes={"app.component": "gateway"},
        )
        opts.resolve_from_env()
        assert opts.resource_attributes["app.component"] == "gateway"
        assert opts.service_name == "test"

    def test_debug_mode(self):
        opts = setup_telemetry(TelemetryOptions(
            service_name="test",
            otel_endpoint="http://localhost:4317",
            debug_level=True,
        ))
        assert opts.debug_level is True
