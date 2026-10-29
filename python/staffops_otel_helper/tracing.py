"""Tracing setup — equivalent to TracerSetup.cs."""

import logging
import math
from contextlib import contextmanager

from opentelemetry import context, trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import (
    ALWAYS_OFF,
    ALWAYS_ON,
    Decision,
    ParentBased,
    Sampler,
    SamplingResult,
    TraceIdRatioBased,
)
from opentelemetry.trace import SpanKind
from opentelemetry.sdk.resources import Resource

from staffops_otel_helper._exporters import build_otlp_exporter
from staffops_otel_helper.config import (
    _DECIMAL_RE,
    ENV_TRACES_SAMPLER,
    ENV_TRACES_SAMPLER_ARG,
    TelemetryOptions,
    _env,
)
from staffops_otel_helper.processors import DebugProcessor

# HTTP paths whose server spans the helper drops unless TelemetryOptions.health_paths
# replaces the list (R7, design D5) — same list as Go's DefaultHealthPaths.
DEFAULT_HEALTH_PATHS = ("/health", "/healthz", "/ready", "/readyz", "/live", "/livez", "/ping", "/metrics")
# The whole gRPC health service (Check, Watch, List), as span names with the leading "/" stripped.
_GRPC_HEALTH_SERVICE = "grpc.health.v1.Health/"
# Stable HTTP semconv (the helper's default, design D12) and the old generation.
_ATTR_URL_PATH = "url.path"
_ATTR_HTTP_TARGET = "http.target"

_log = logging.getLogger(__name__)

# The six sampler names the OTel SDK environment spec defines for OTEL_TRACES_SAMPLER.
_RATIO_SAMPLERS = frozenset(["traceidratio", "parentbased_traceidratio"])
_KNOWN_SAMPLERS = frozenset([
    "always_on", "always_off", "parentbased_always_on", "parentbased_always_off", *_RATIO_SAMPLERS,
])
# Listed by the spec but needing remote config / vendor packages the helper does not ship.
_UNSUPPORTED_SAMPLERS = frozenset(["jaeger_remote", "parentbased_jaeger_remote", "xray"])


def _sampler_ratio_arg(name: str) -> float:
    """OTEL_TRACES_SAMPLER_ARG for a ratio sampler: unset -> 1.0; invalid -> warn, 1.0.

    Plain decimal only (design D3, same grammar as the timeouts): no sign,
    exponent, hex, digit separators or NaN.
    """
    raw = _env(ENV_TRACES_SAMPLER_ARG)
    if raw is None:
        return 1.0
    ratio = float(raw) if _DECIMAL_RE.match(raw) else math.nan
    if math.isnan(ratio) or not 0.0 <= ratio <= 1.0:
        _log.warning("[OtelHelper] %s=%r is not a number in [0, 1] for sampler %r; using 1.0.",
                     ENV_TRACES_SAMPLER_ARG, raw, name)
        return 1.0
    return ratio


def _sampler_from_env() -> Sampler:
    """OTEL_TRACES_SAMPLER / _ARG -> sampler; unset -> parentbased_always_on.

    Built here instead of letting TracerProvider read the env because the SDK's
    own parsing (1.42) raises on an out-of-range _ARG, warns on its own logger
    without naming the variable, and keeps the sampler out of reach of the
    wrapper the health exclusion (design D5) adds on top of this one.
    """
    raw = _env(ENV_TRACES_SAMPLER)
    if raw is None:
        return ParentBased(ALWAYS_ON)
    name = raw.lower()
    if name in _UNSUPPORTED_SAMPLERS:
        _log.warning("[OtelHelper] %s=%r is unsupported by this helper; using parentbased_always_on.",
                     ENV_TRACES_SAMPLER, raw)
        return ParentBased(ALWAYS_ON)
    if name not in _KNOWN_SAMPLERS:
        _log.warning("[OtelHelper] %s=%r is not a known sampler (valid: %s); using parentbased_always_on.",
                     ENV_TRACES_SAMPLER, raw, ", ".join(sorted(_KNOWN_SAMPLERS)))
        return ParentBased(ALWAYS_ON)
    if name in _RATIO_SAMPLERS:
        ratio_sampler = TraceIdRatioBased(_sampler_ratio_arg(name))
        return ParentBased(ratio_sampler) if name == "parentbased_traceidratio" else ratio_sampler
    return {
        "always_on": ALWAYS_ON,
        "always_off": ALWAYS_OFF,
        "parentbased_always_on": ParentBased(ALWAYS_ON),
        "parentbased_always_off": ParentBased(ALWAYS_OFF),
    }[name]


def build_sampler(options: TelemetryOptions) -> Sampler:
    """The helper's sampler (R6, design D3, ADR-002) — the single place it is built.

    Precedence: a ratio set in code (any value, 1.0 included — D1 A13 ruling),
    or the deprecated OTEL_HELPER_SAMPLE_RATIO alias (resolved only while
    OTEL_TRACES_SAMPLER is unset) -> ``ParentBased(TraceIdRatioBased(ratio))``;
    else OTEL_TRACES_SAMPLER/_ARG; else the spec default ``parentbased_always_on``.
    """
    resolved = options.resolved
    if resolved.sample_ratio_set:
        return ParentBased(TraceIdRatioBased(resolved.sample_ratio))
    return _sampler_from_env()


def normalize_health_path(path: str) -> str:
    """Strip query/fragment, lowercase, drop trailing slashes ("/" stays "/")."""
    for sep in "?#":
        path = path.split(sep, 1)[0]
    path = path.lower()
    while len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    return path


class HealthCheckSampler(Sampler):
    """Drops health-check SERVER spans; defers everything else to ``inner`` (design D5).

    Dropping in the sampler instead of with the instrumentations' ``excluded_urls``
    keeps health requests in the HTTP server metrics: the middleware still runs,
    only the span is not recorded. It decides for the entry span of a request
    only (no parent, or a remote one — a probe carrying a sampled traceparent is
    still a probe); children follow the DROP through ParentBased (the default).
    """

    def __init__(self, inner: Sampler, paths=DEFAULT_HEALTH_PATHS) -> None:
        self._inner = inner
        self._paths = frozenset(normalize_health_path(p) for p in paths)

    def should_sample(self, parent_context, trace_id, name, kind=None, attributes=None, links=None,
                      trace_state=None) -> SamplingResult:
        if kind == SpanKind.SERVER:
            parent = trace.get_current_span(parent_context).get_span_context()
            if (not parent.is_valid or parent.is_remote) and self._is_health(name, attributes):
                return SamplingResult(Decision.DROP, None, parent.trace_state if parent.is_valid else None)
        return self._inner.should_sample(parent_context, trace_id, name, kind=kind, attributes=attributes,
                                         links=links, trace_state=trace_state)

    @property
    def inner(self) -> Sampler:
        """The wrapped sampler (``build_sampler()``'s result)."""
        return self._inner

    def _is_health(self, name: str, attributes) -> bool:
        if name and name.lstrip("/").startswith(_GRPC_HEALTH_SERVICE):
            return True
        if not attributes:
            return False
        path = attributes.get(_ATTR_URL_PATH)
        if path is None:
            path = attributes.get(_ATTR_HTTP_TARGET)
        return isinstance(path, str) and normalize_health_path(path) in self._paths

    def get_description(self) -> str:
        return f"HealthCheckExcluding{{{self._inner.get_description()}}}"


def build_tracer_provider(resource: Resource, options: TelemetryOptions) -> TracerProvider:
    """Build the TracerProvider without installing it globally.

    Without an OTLP traces endpoint (or with OTEL_TRACES_EXPORTER=none) the
    TracerProvider has no exporter.
    In-process context propagation and spans still work (useful for metric exemplars).
    """
    paths = DEFAULT_HEALTH_PATHS if options.health_paths is None else options.health_paths
    sampler = HealthCheckSampler(build_sampler(options), paths)
    provider = TracerProvider(resource=resource, sampler=sampler, shutdown_on_exit=False)
    try:
        exporters = options.resolved_exporters("traces")
        plan = options.otlp_export("traces")
        if "otlp" in exporters and plan is not None:
            provider.add_span_processor(BatchSpanProcessor(build_otlp_exporter(plan)))
        if "console" in exporters:
            from opentelemetry.sdk.trace.export import ConsoleSpanExporter

            provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))

        if options.resolved.debug_level:
            provider.add_span_processor(DebugProcessor())
    except BaseException:
        provider.shutdown()  # stops the BatchSpanProcessor threads already added
        raise
    return provider


def configure_tracing(resource: Resource, options: TelemetryOptions) -> TracerProvider:
    """Build the TracerProvider and set it as the global one."""
    provider = build_tracer_provider(resource, options)
    trace.set_tracer_provider(provider)
    return provider


@contextmanager
def start_root_span(tracer, name, **kwargs):
    """Start a new root span (new trace), detaching from any current parent context.

    Use in workers/background services where each iteration should be an independent trace.
    Equivalent to .NET's ActivitySource.StartRootActivity().

    Usage:
        with start_root_span(tracer, "process.cycle") as span:
            span.set_attribute("batch.size", 10)
            ...
    """
    from opentelemetry.trace import NonRecordingSpan, INVALID_SPAN_CONTEXT
    clean_ctx = trace.set_span_in_context(NonRecordingSpan(INVALID_SPAN_CONTEXT))
    token = context.attach(clean_ctx)
    try:
        with tracer.start_as_current_span(name, **kwargs) as span:
            yield span
    finally:
        context.detach(token)
