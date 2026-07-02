"""Opt-in AWS SDK (botocore/boto3) instrumentation extension."""


def instrument_aws() -> None:
    """Enable AWS SDK (botocore/boto3) tracing instrumentation."""
    from opentelemetry.instrumentation.botocore import BotocoreInstrumentor

    BotocoreInstrumentor().instrument()
