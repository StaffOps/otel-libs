"""Opt-in Redis instrumentation extension."""

import logging

from staffops_otel_helper._db_status import clear_wrapped, is_wrapped, mark_wrapped, warn_foreign, wrapped_instrument_kwargs
from staffops_otel_helper._semconv import ensure_semconv_default
from staffops_otel_helper._support import missing_dependency_hint as _missing_hint
from staffops_otel_helper._support import warn_if_unsupported

_log = logging.getLogger("staffops_otel_helper.ext.redis")


def instrument_redis() -> bool:
    """Enable Redis tracing instrumentation.

    Returns True when the instrumentor is active. False (with a warning on the
    ``staffops_otel_helper`` logger) when it refuses the installed library version (P18).

    A failed command's span keeps the ERROR status code without its
    description, which is the server's error text and can quote a command
    argument (A26; contract design D1 "DB error status"); the reply's code is
    ``db.response.status_code`` and ``error.type``, and a pipeline error's
    exception event keeps the command names only. Already instrumented
    by ``setup_telemetry()`` (the deprecated ``REDIS`` flag, same wrapper):
    True. Already instrumented by someone else without the wrapper: kept as it
    is and warned once.
    """
    ensure_semconv_default()  # before the instrumentor reads it (design D12)
    try:
        from opentelemetry.instrumentation.redis import RedisInstrumentor
    except ImportError as e:
        hint = _missing_hint(e, "redis", "Redis", library="redis",
                            module="opentelemetry.instrumentation.redis")
        raise ImportError(hint) from e

    instrumentor = RedisInstrumentor()
    if instrumentor.is_instrumented_by_opentelemetry:
        if not is_wrapped(instrumentor):
            warn_foreign(_log, "redis")
        return True
    clear_wrapped(instrumentor)  # a mark left by an earlier, since uninstrumented, helper call
    instrumentor.instrument(**wrapped_instrument_kwargs(redis=True))
    if instrumentor.is_instrumented_by_opentelemetry:
        mark_wrapped(instrumentor)
    warn_if_unsupported(instrumentor)  # P18: refused library version → helper warning
    return bool(instrumentor.is_instrumented_by_opentelemetry)
