"""Opt-in AWS SDK (botocore/boto3) instrumentation extension."""

from otel_helper._semconv import ensure_semconv_default


def instrument_aws() -> None:
    """Enable AWS SDK (botocore/boto3) tracing instrumentation."""
    ensure_semconv_default()  # before the instrumentor reads it (design D12)
    try:
        from opentelemetry.instrumentation.botocore import BotocoreInstrumentor
    except ImportError as e:
        raise ImportError(
            "AWS instrumentation not installed. Run: pip install otel-helper[aws]"
        ) from e

    BotocoreInstrumentor().instrument()
