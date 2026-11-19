"""Tracing setup — equivalent to TracerSetup.cs."""

import contextvars
import functools
import inspect
import logging
import math
import threading
from contextlib import contextmanager

from opentelemetry import trace
from opentelemetry.sdk.trace import SpanLimits, TracerProvider
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
    ascii_lower,
)
from staffops_otel_helper._exception_detail import HelperTracerProvider
from staffops_otel_helper._url_redaction import UrlQueryRedactionProcessor
from staffops_otel_helper.processors import DebugProcessor, GrpcSpanNameProcessor

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
    name = ascii_lower(raw)
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
    """Strip query/fragment, lowercase A-Z, drop trailing slashes ("/" stays "/")."""
    for sep in "?#":
        path = path.split(sep, 1)[0]
    path = ascii_lower(path)  # ASCII only (design D1 row "ASCII case folding", A20)
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
    limits = options.value_length_limits()
    kwargs = {}
    if "span" in limits or "generic" in limits:
        # A value-length limit of 0 is invalid (D1 ruling, A18): pass the next level explicitly.
        kwargs["span_limits"] = SpanLimits(
            max_attribute_length=limits.get("generic"),
            max_span_attribute_length=limits.get("span"),  # None: the SDK reads a non-zero env value
        )
    # HelperTracerProvider: HTTP and gRPC spans keep exception.type only, detail in a correlated record (A37).
    provider = HelperTracerProvider(resource=resource, sampler=sampler, shutdown_on_exit=False, **kwargs)
    # Before any exporting processor: URL query values redacted at span start (A28),
    # contrib gRPC spans renamed to the semconv name (A30).
    provider.add_span_processor(UrlQueryRedactionProcessor())
    provider.add_span_processor(GrpcSpanNameProcessor())
    try:
        exporters = options.resolved_exporters("traces")
        plan = options.otlp_export("traces")
        if "otlp" in exporters and plan is not None:
            # The resolved OTLP timeout is the export deadline (A18); OTEL_BSP_EXPORT_TIMEOUT is not honored.
            provider.add_span_processor(BatchSpanProcessor(build_otlp_exporter(plan),
                                                           export_timeout_millis=plan.timeout_ms,
                                                           **options.batch_size_override("OTEL_BSP")))
        if "console" in exporters:
            from opentelemetry.sdk.trace.export import ConsoleSpanExporter

            provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter(),
                                                           **options.batch_size_override("OTEL_BSP")))

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


# Keyword arguments of Tracer.start_as_current_span the failure rule owns (R8: rejected).
_FORBIDDEN_SPAN_KWARGS = ("record_exception", "set_status_on_exception")


def _check_span_kwargs(function: str, kwargs: dict) -> None:
    for key in _FORBIDDEN_SPAN_KWARGS:
        if key in kwargs:
            raise TypeError(f"{function}() does not accept {key!r}: the helper applies the failure rule "
                            "(status ERROR without description, error.type, one correlated log record)")


def _default_tracer():
    from staffops_otel_helper import get_tracer  # the package imports this module

    return get_tracer()


@contextmanager
def _span_scope(name: str, tracer, root: bool, span_kwargs: dict):
    """One span applying the failure rule (design D15); fresh state per call."""
    from staffops_otel_helper._failure import is_cancellation, mark_failure

    if tracer is None:
        tracer = _default_tracer()
    if root:
        # A new trace: an invalid parent in the current context (baggage kept); an explicit context wins.
        span_kwargs = {"context": trace.set_span_in_context(trace.INVALID_SPAN), **span_kwargs}
    with tracer.start_as_current_span(name, record_exception=False, set_status_on_exception=False,
                                      **span_kwargs) as span:
        try:
            yield span
        except BaseException as exc:
            if not is_cancellation(exc):
                mark_failure(span, exc)
            raise


# Scopes entered through a `with traced(...)` object, per context (asyncio task, thread):
# an immutable tuple of (owner, scope), replaced on every enter and exit (copy-on-write),
# so one object shared by concurrent tasks or threads exits its own scope.
_ACTIVE_SCOPES: contextvars.ContextVar[tuple] = contextvars.ContextVar("staffops_otel_helper_traced_scopes",
                                                                        default=())


class _Traced:
    """What :func:`traced` returns: a context manager and a decorator."""

    def __init__(self, name: str, tracer, root: bool, span_kwargs: dict) -> None:
        self._name = name
        self._tracer = tracer
        self._root = root
        self._span_kwargs = span_kwargs

    def _scope(self):
        return _span_scope(self._name, self._tracer, self._root, self._span_kwargs)

    def __enter__(self):
        scope = self._scope()
        span = scope.__enter__()
        _ACTIVE_SCOPES.set(_ACTIVE_SCOPES.get() + ((self, scope),))
        return span

    def __exit__(self, exc_type, exc, tb):
        # The innermost scope this object opened in the current context (task or thread).
        entries = _ACTIVE_SCOPES.get()
        index = next((i for i in range(len(entries) - 1, -1, -1) if entries[i][0] is self), None)
        if index is None:
            raise RuntimeError("traced(): __exit__ without a matching __enter__ in this context")
        _ACTIVE_SCOPES.set(entries[:index] + entries[index + 1:])
        return entries[index][1].__exit__(exc_type, exc, tb)

    def __call__(self, fn):
        if inspect.isgeneratorfunction(fn) or inspect.isasyncgenfunction(fn):
            raise TypeError(f"traced() cannot decorate the generator function {fn.__qualname__!r}: "
                            "the span would end before the lazy work runs")
        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def async_wrapper(*args, **kwargs):
                with self._scope():
                    return await fn(*args, **kwargs)

            return async_wrapper

        warned = threading.Event()

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            with self._scope():
                result = fn(*args, **kwargs)
            if not warned.is_set() and (inspect.isawaitable(result) or inspect.isgenerator(result)
                                        or inspect.isasyncgen(result)):
                warned.set()
                _log.warning("[OtelHelper] traced(%r): %s returned %s; the span ended when the call returned, "
                             "before that work ran. Decorate an `async def` function or wrap the work itself.",
                             self._name, getattr(fn, "__qualname__", type(fn).__name__), type(result).__name__)
            return result

        return wrapper


def traced(name: str, *, tracer=None, root: bool = False, **span_kwargs) -> _Traced:
    """Run work in a span that applies the failure rule (R11, ADR-016, design D15).

    Use as a context manager (``with traced("orders.process") as span:``) or as
    a decorator of a plain or ``async def`` function. When the work raises, the
    span gets status ERROR **without description** and ``error.type``
    (``__otel_error_type__`` when set, else the qualified class name), no
    ``exception`` event, and one ERROR record with the message and the stack
    goes to the ``staffops_otel_helper.failure`` logger in the span's context (one
    record per helper span the failure crosses);
    the exception propagates unchanged. On success the status stays Unset
    (never Ok). A cancellation (a ``BaseException`` that is not an
    ``Exception``, or a gRPC ``CANCELLED`` error) leaves the span Unset with no
    record.

    Args:
        name: span name.
        tracer: the tracer to use; default the helper's ``get_tracer()``, per call.
        root: start a new trace (no parent), as :func:`start_root_span`.
        **span_kwargs: passed to ``Tracer.start_as_current_span`` (``kind``,
            ``attributes``, ``links``, ...); ``record_exception`` and
            ``set_status_on_exception`` are rejected.

    Raises:
        TypeError: a non-`str` name; a rejected keyword argument; decorating a generator function.
    """
    if not isinstance(name, str):
        raise TypeError(f"traced() name must be a str, got {type(name).__name__}")
    _check_span_kwargs("traced", span_kwargs)
    return _Traced(name, tracer, root, span_kwargs)


@contextmanager
def start_root_span(tracer, name, **kwargs):
    """Start a new root span (new trace) that applies the failure rule (design D15).

    Use in workers/background services where each iteration should be an
    independent trace. Same rule as :func:`traced` (``traced(name,
    tracer=tracer, root=True)``): an exception that leaves the span sets
    status ERROR without description and ``error.type``; the message and the
    stack go to one correlated log record, not to the span (changed in 0.3).

    Usage:
        with start_root_span(tracer, "process.cycle") as span:
            span.set_attribute("batch.size", 10)
            ...

    Raises:
        TypeError: ``record_exception`` or ``set_status_on_exception`` passed.
    """
    _check_span_kwargs("start_root_span", kwargs)
    with _span_scope(name, tracer, True, kwargs) as span:
        yield span
