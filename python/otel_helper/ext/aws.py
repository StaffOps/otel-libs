"""Opt-in AWS SDK (botocore/boto3) instrumentation extension."""


def instrument_aws() -> None:
    """Enable AWS SDK (botocore/boto3) tracing instrumentation."""
    try:
        from opentelemetry.instrumentation.botocore import BotocoreInstrumentor
    except ImportError as e:
        raise ImportError(
            "AWS instrumentation not installed. Run: pip install otel-helper[aws]"
        ) from e

    BotocoreInstrumentor().instrument()
