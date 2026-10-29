"""Opt-in AWS SDK (botocore/boto3) instrumentation extension."""

from staffops_otel_helper._semconv import ensure_semconv_default
from staffops_otel_helper._support import missing_dependency_hint as _missing_hint
from staffops_otel_helper._support import warn_if_unsupported


def instrument_aws() -> bool:
    """Enable AWS SDK (botocore/boto3) tracing instrumentation.

    Returns True when the instrumentor is active. False (with a warning on the
    ``staffops_otel_helper`` logger) when it refuses the installed library version (P18).
    """
    ensure_semconv_default()  # before the instrumentor reads it (design D12)
    try:
        from opentelemetry.instrumentation.botocore import BotocoreInstrumentor
    except ImportError as e:
        hint = _missing_hint(e, "aws", "AWS", library="botocore",
                            module="opentelemetry.instrumentation.botocore")
        raise ImportError(hint) from e

    instrumentor = BotocoreInstrumentor()
    instrumentor.instrument()
    warn_if_unsupported(instrumentor)  # P18: refused library version → helper warning
    return bool(instrumentor.is_instrumented_by_opentelemetry)
