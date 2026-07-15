"""Logging setup — equivalent to LoggingSetup.cs."""

import logging

from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.resources import Resource

from otel_helper.config import PROTOCOL_HTTP, TelemetryOptions, get_default_log_level


def configure_logging(resource: Resource, options: TelemetryOptions) -> LoggerProvider:
    """Configure OTel logging with OTLP export and level filtering.

    When otel_endpoint is empty, configures the LoggerProvider without an exporter.
    Standard Python logging still works (stdout); OTel log correlation is available in-process.
    """
    provider = LoggerProvider(resource=resource)

    if options.otel_endpoint:
        if options.resolved_otlp_protocol() == PROTOCOL_HTTP:
            from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter as HttpLogExporter

            endpoint = options.otel_endpoint.rstrip("/")
            if not endpoint.endswith("/v1/logs"):
                endpoint = f"{endpoint}/v1/logs"
            exporter = HttpLogExporter(
                endpoint=endpoint,
                timeout=options.export_timeout_ms / 1000,
            )
        else:
            from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter

            exporter = OTLPLogExporter(
                endpoint=options.otel_endpoint,
                insecure=options.resolve_insecure(),
                timeout=options.export_timeout_ms / 1000,
            )
        provider.add_log_record_processor(BatchLogRecordProcessor(exporter))

    level = options.minimum_log_level or get_default_log_level(options.environment, options.debug_level)

    handler = LoggingHandler(level=level, logger_provider=provider)
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(level)

    # Reduce framework noise in non-debug
    if level > logging.DEBUG:
        logging.getLogger("urllib3").setLevel(logging.ERROR)
        logging.getLogger("httpcore").setLevel(logging.ERROR)
        logging.getLogger("grpc").setLevel(logging.ERROR)
        logging.getLogger("opentelemetry").setLevel(logging.WARNING)

    return provider
