"""Custom span processors — equivalent to DebugTraceStateProcessor.cs."""

from opentelemetry.sdk.trace import SpanProcessor, ReadableSpan
from opentelemetry.trace import Span


class DebugProcessor(SpanProcessor):
    """Sets span attribute 'debug=true' on root spans when debug mode is enabled.

    The Collector tail_sampling policy 'debug-forced-attribute' uses:
        type: string_attribute
        string_attribute:
            key: debug
            values: ["true"]

    This ensures 100% sampling for debug traces regardless of language.
    """

    def on_start(self, span: Span, parent_context=None) -> None:
        if span.parent is None:
            span.set_attribute("debug", "true")

    def on_end(self, span: ReadableSpan) -> None:
        pass

    def shutdown(self) -> None:
        pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True
