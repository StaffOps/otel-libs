"""Opt-in AWS SDK (botocore/boto3) instrumentation extension."""

import logging

from staffops_otel_helper._aws_status import aws_instrument_kwargs
from staffops_otel_helper._db_status import clear_wrapped, is_wrapped, mark_wrapped, warn_foreign
from staffops_otel_helper._semconv import ensure_semconv_default
from staffops_otel_helper._support import missing_dependency_hint as _missing_hint
from staffops_otel_helper._support import warn_if_unsupported

_log = logging.getLogger("staffops_otel_helper.ext.aws")


def instrument_aws() -> bool:
    """Enable AWS SDK (botocore/boto3) tracing instrumentation.

    Returns True when the instrumentor is active. False (with a warning on the
    ``staffops_otel_helper`` logger) when it refuses the installed library version (P18).

    A failed call's span keeps the ERROR status code without its description
    (the AWS error text can quote request values; A27, contract design D1 "AWS
    error status"); ``error.type`` is the AWS error code (``NoSuchKey``) or
    ``_OTHER``. ``aws.dynamodb.item_collection_metrics`` (a partition-key
    value) is never recorded. Already instrumented by ``setup_telemetry()``
    (the deprecated ``AWS`` flag, same wrapper): True. Already instrumented by
    someone else without the wrapper: kept as it is and warned once.
    """
    ensure_semconv_default()  # before the instrumentor reads it (design D12)
    try:
        from opentelemetry.instrumentation.botocore import BotocoreInstrumentor
    except ImportError as e:
        hint = _missing_hint(e, "aws", "AWS", library="botocore",
                            module="opentelemetry.instrumentation.botocore")
        raise ImportError(hint) from e

    instrumentor = BotocoreInstrumentor()
    if instrumentor.is_instrumented_by_opentelemetry:
        if not is_wrapped(instrumentor):
            warn_foreign(_log, "aws")
        return True
    clear_wrapped(instrumentor)  # a mark left by an earlier, since uninstrumented, helper call
    instrumentor.instrument(**aws_instrument_kwargs())
    if instrumentor.is_instrumented_by_opentelemetry:
        mark_wrapped(instrumentor)
    warn_if_unsupported(instrumentor)  # P18: refused library version → helper warning
    return bool(instrumentor.is_instrumented_by_opentelemetry)
