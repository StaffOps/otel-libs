"""Custom span processors: the debug marker (R9, ADR-006) and gRPC span names (A30)."""

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


class GrpcSpanNameProcessor(SpanProcessor):
    """Names contrib gRPC spans ``<package.Service>/<Method>`` (contract D1 "gRPC span names", A30).

    semconv v1.43 names an RPC span ``{rpc.method}``, the fully-qualified
    method without a leading ``/`` (``docs/rpc/rpc-spans.md``), as Go's
    otelgrpc and the Node.js helper do. contrib grpc 0.63b0–0.66b2 names its
    CLIENT and SERVER spans by the full method (``/pkg.Svc/Method``) and passes
    ``rpc.system=grpc``, ``rpc.service`` and ``rpc.method`` (the bare method)
    as span-start attributes; a span whose name is exactly
    ``/<rpc.service>/<rpc.method>`` is renamed at start, before any exporter
    sees it. Any other span (an app's own, or a contrib release that already
    uses the semconv name) is left alone. The health-check sampler decides
    before this rename and matches both forms.
    """

    def on_start(self, span: Span, parent_context=None) -> None:
        try:
            attributes = span.attributes or {}
            if attributes.get("rpc.system") != "grpc":
                return
            service, method = attributes.get("rpc.service"), attributes.get("rpc.method")
            if isinstance(service, str) and isinstance(method, str) and span.name == f"/{service}/{method}":
                span.update_name(f"{service}/{method}")
        except Exception:  # noqa: BLE001 - a span processor never raises into the instrumented call
            pass

    def on_end(self, span: ReadableSpan) -> None:
        pass

    def shutdown(self) -> None:
        pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True
