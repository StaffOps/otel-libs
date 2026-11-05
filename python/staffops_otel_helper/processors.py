"""Custom span processors: the debug marker (R9, ADR-006)."""

from opentelemetry.sdk.trace import SpanProcessor, ReadableSpan
from opentelemetry.trace import Span


class DebugProcessor(SpanProcessor):
    """Sets span attribute ``debug=true`` on the service's entry spans (R9, ADR-006).

    Entry spans are spans with no parent or a **remote** parent; spans with a
    local parent are left alone. Installed only when debug mode is on in local
    config (``OTEL_HELPER_DEBUG_LEVEL`` / ``debug_level``): the marker is never
    derived from inbound tracestate or baggage, and tracestate is never written.

    The Collector tail_sampling policy 'debug-forced-attribute' uses:
        type: string_attribute
        string_attribute:
            key: debug
            values: ["true"]

    This ensures 100% sampling for debug traces regardless of language.
    """

    def on_start(self, span: Span, parent_context=None) -> None:
        parent = span.parent
        if parent is None or parent.is_remote:
            span.set_attribute("debug", "true")

    def on_end(self, span: ReadableSpan) -> None:
        pass

    def shutdown(self) -> None:
        pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True
