"""Configuration for OtelHelper — equivalent to TelemetryOptions.cs + PostConfigure + Validator."""

import os
import logging
from dataclasses import dataclass, field
from enum import Enum
from urllib.parse import urlparse


class DeploymentEnvironment(Enum):
    LOCAL = "LOCAL"
    DEV = "DEV"
    HML = "HML"
    PRD = "PRD"


# Environment variable names
ENV_COLLECTOR_ENDPOINT = "OTEL_EXPORTER_OTLP_ENDPOINT"
ENV_SERVICE_NAME = "SERVICE_NAME"
ENV_OTEL_SERVICE_NAME = "OTEL_SERVICE_NAME"
ENV_ENVIRONMENT = "ENVIRONMENT"
ENV_DEBUG_LEVEL = "OTEL_HELPER_DEBUG_LEVEL"
ENV_EXTRA_INSTRUMENTATION = "OTEL_HELPER_EXTRA_INSTRUMENTATION"
ENV_SAMPLE_RATIO = "OTEL_HELPER_SAMPLE_RATIO"
ENV_DISABLED_SIGNALS = "OTEL_HELPER_DISABLED_SIGNALS"
ENV_DISABLED_METRICS = "OTEL_HELPER_DISABLED_METRICS"
ENV_METRICS_PORT = "OTEL_HELPER_METRICS_PORT"

_DEFAULT_COLLECTOR_HOST = "http://localhost"
_DEFAULT_OTLP_PORT = 4317


def _parse_environment(value: str) -> DeploymentEnvironment:
    normalized = value.upper().replace("-", "_")
    try:
        return DeploymentEnvironment(normalized)
    except ValueError:
        return DeploymentEnvironment.LOCAL


def _resolve_collector_host() -> str:
    env = os.getenv(ENV_COLLECTOR_ENDPOINT, "").strip().rstrip("/")
    if not env:
        return ""

    # urlparse treats "host:port" (no scheme) as scheme="host", path="port",
    # yielding hostname=None. Default to http:// when no scheme is present so
    # endpoints like "collector.svc:4317" resolve correctly instead of producing
    # an invalid "collector.svc://None" that silently drops all telemetry.
    if "://" not in env:
        env = f"http://{env}"

    parsed = urlparse(env)
    if not parsed.hostname:
        # Could not extract a host — fall back to the raw value rather than
        # emitting a broken "scheme://None" endpoint.
        return env
    return f"{parsed.scheme}://{parsed.hostname}"


def _env_bool(var_name: str) -> bool:
    return os.getenv(var_name, "false").lower() == "true"


def get_default_log_level(environment: DeploymentEnvironment, debug_level: bool = False) -> int:
    """Returns the default log level for a given environment."""
    if debug_level:
        return logging.DEBUG
    return {
        DeploymentEnvironment.LOCAL: logging.DEBUG,
        DeploymentEnvironment.DEV: logging.INFO,
        DeploymentEnvironment.HML: logging.INFO,
        DeploymentEnvironment.PRD: logging.WARNING,
    }.get(environment, logging.INFO)


@dataclass
class TelemetryOptions:
    """Configuration options for OtelHelper. Env vars fill defaults; explicit values take priority."""

    service_name: str = "my-service"
    environment: DeploymentEnvironment = DeploymentEnvironment.LOCAL
    otel_endpoint: str = ""
    debug_level: bool = False
    extra_instrumentation: str = "SQL"
    export_timeout_ms: int = 10_000
    sample_ratio: float = 1.0
    disabled_signals: list[str] = field(default_factory=list)
    disabled_metrics: list[str] = field(default_factory=list)
    prometheus_metrics_port: int = 9464
    minimum_log_level: int | None = None
    resource_attributes: dict[str, object] = field(default_factory=dict)

    def has_instrumentation(self, name: str) -> bool:
        if self.debug_level:
            return True
        return name.upper() in [x.strip().upper() for x in self.extra_instrumentation.split(",") if x.strip()]

    def is_signal_enabled(self, signal: str) -> bool:
        """Check if a signal (traces, metrics, logs) is enabled."""
        return signal.lower() not in [s.lower() for s in self.disabled_signals]

    def resolve_from_env(self) -> None:
        """Apply environment variable defaults (PostConfigure equivalent)."""
        if self.service_name == "my-service":
            self.service_name = os.getenv(ENV_SERVICE_NAME) or os.getenv(ENV_OTEL_SERVICE_NAME) or "my-service"

        if self.environment == DeploymentEnvironment.LOCAL:
            env_val = os.getenv(ENV_ENVIRONMENT)
            if env_val:
                self.environment = _parse_environment(env_val)

        collector_host = _resolve_collector_host()
        if not self.otel_endpoint:
            if collector_host:
                self.otel_endpoint = f"{collector_host}:{_DEFAULT_OTLP_PORT}"
            # else: leave empty — triggers Prometheus fallback

        if not self.debug_level:
            self.debug_level = _env_bool(ENV_DEBUG_LEVEL)

        if self.extra_instrumentation == "SQL":
            env_extra = os.getenv(ENV_EXTRA_INSTRUMENTATION)
            if env_extra is not None:
                self.extra_instrumentation = env_extra

        if self.sample_ratio == 1.0:
            env_ratio = os.getenv(ENV_SAMPLE_RATIO)
            if env_ratio is not None:
                try:
                    self.sample_ratio = max(0.0, min(1.0, float(env_ratio)))
                except ValueError:
                    pass

        if not self.disabled_signals:
            env_disabled = os.getenv(ENV_DISABLED_SIGNALS)
            if env_disabled:
                self.disabled_signals = [s.strip().lower() for s in env_disabled.split(",") if s.strip()]

        if not self.disabled_metrics:
            env_metrics = os.getenv(ENV_DISABLED_METRICS)
            if env_metrics:
                self.disabled_metrics = [s.strip() for s in env_metrics.split(",") if s.strip()]

        if self.prometheus_metrics_port == 9464:
            env_port = os.getenv(ENV_METRICS_PORT)
            if env_port is not None:
                try:
                    self.prometheus_metrics_port = int(env_port)
                except ValueError:
                    pass

    def validate(self) -> None:
        """Validate options at startup. Raises ValueError on invalid config.

        This is equivalent to .NET's IValidateOptions — the application MUST NOT start with invalid config.
        Call setup_telemetry() in main() to ensure failures are visible.
        """
        if not self.service_name or not self.service_name.strip():
            raise ValueError(f"[OtelHelper] ServiceName is required. Set the {ENV_SERVICE_NAME} environment variable.")

        if self.otel_endpoint and self.otel_endpoint.strip():
            parsed = urlparse(self.otel_endpoint)
            if not parsed.scheme or not parsed.hostname:
                raise ValueError(f"[OtelHelper] OtelCollectorEndpoint '{self.otel_endpoint}' is not a valid URI.")

        if self.export_timeout_ms <= 0:
            raise ValueError("[OtelHelper] ExportTimeoutMs must be greater than 0.")
