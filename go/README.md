# OTel Helper — Go

OpenTelemetry helper for Go services. One `Setup` call configures traces,
metrics and logs: OTLP export to a Collector, W3C propagation, a ParentBased
sampler with health-check exclusion, Go runtime metrics, and a `log/slog`
handler correlated with traces.

- **[HOW-TO.md](HOW-TO.md)** — developer guide: HTTP, gRPC, workers, custom
  signals, configuration rules, lifecycle, troubleshooting
- **[TESTS.md](TESTS.md)** — running the unit, parity, ext and e2e tests
- **[example/](example/README.md)** — sample apps, including a custom-signals
  example verified against a real Collector
- **[Migration 0.2 → 0.3](#migration-from-020)** and the full
  [CHANGELOG](../CHANGELOG.md)

This page describes the **0.3.0** contract (unreleased, branch
`feat/v0.3-config-contract`), specified in
[`specs/config-contract-v0.3/`](https://github.com/StaffOps/otel-libs/blob/main/specs/config-contract-v0.3/design.md).

## Quick start

```go
func main() {
    // SIGTERM is what `docker stop` and Kubernetes send.
    ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
    defer stop()

    shutdown, err := otelhelper.Setup(ctx)
    if err != nil {
        log.Fatalf("otel setup: %v", err)
    }
    defer func() {
        // ctx is cancelled by the time this runs: flush under a fresh deadline.
        flushCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
        defer cancel()
        _ = shutdown(flushCtx)
    }()

    // ... run the service until ctx is done ...
}
```

Configure it with standard OTel variables (usually injected by the platform):

```bash
OTEL_SERVICE_NAME=orders-api
OTEL_RESOURCE_ATTRIBUTES=deployment.environment.name=PRD
OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317
```

## Installation

```bash
go get github.com/staffops/otel-libs/go
```

Requires Go **1.26** or newer (`go.mod`: `go 1.26`); CI runs 1.26.x and 1.27.x.
Older toolchains fail with `go: go.mod requires go >= 1.26.0`.

---

## Environment variables

Precedence, for every setting: **code (`With*` option) > standard `OTEL_*`
variable > deprecated / `OTEL_HELPER_*` variable > default**. A value that
cannot be parsed from the environment logs a warning (stderr) and falls back to
the next level; the same invalid value passed in code makes `Setup` return an
error naming the option. The resolution rules (endpoint, TLS, timeout,
compression, headers) are in [HOW-TO §9](HOW-TO.md#9-configuration-reference).

### Identity

| Variable | Default | Description |
|----------|---------|-------------|
| `OTEL_SERVICE_NAME` | _(unset)_ | `service.name`. Order: code > `OTEL_SERVICE_NAME` > `service.name` in `OTEL_RESOURCE_ATTRIBUTES` > `SERVICE_NAME` > SDK `unknown_service:<exe>` + warning. Whitespace-only = unset |
| `OTEL_RESOURCE_ATTRIBUTES` | _(unset)_ | Resource attributes, incl. `deployment.environment.name` and `service.instance.id`. A key set in code (`WithResourceAttributes`) wins; a helper default never overwrites a key set here. Parsed by the helper: values percent-decoded then trimmed; an item without `=`, with an empty key or a bad `%` escape is warned by position (never by value); a blank identity value (`service.name=`) counts as unset; `telemetry.sdk.*` are ignored (the SDK sets them). Limitation: the Go SDK providers re-read the variable, so a blank `deployment.environment.name=` comes back as `""` and a key with surrounding Unicode whitespace appears twice (the helper keeps the SDK from echoing malformed items) |
| `SERVICE_NAME` | _(unset)_ | **Deprecated** alias of `OTEL_SERVICE_NAME` (lowest priority); warns once per process when set |
| `ENVIRONMENT` | _(unset)_ | **Deprecated** alias of `deployment.environment.name` (`OTEL_RESOURCE_ATTRIBUTES` wins); warns once when set. Unset everywhere → attribute absent + one warning. Values other than `LOCAL`/`DEV`/`HML`/`PRD` are emitted verbatim |

The SDK resource also carries `service.instance.id`: a per-process UUID unless
code or `OTEL_RESOURCE_ATTRIBUTES` sets one
([design D4](https://github.com/StaffOps/otel-libs/blob/main/specs/config-contract-v0.3/design.md#d4--serviceinstanceid)).
Everything else (`k8s.*`, `service.version`, cloud attributes) is added by the
Collector (`k8sattributes`, `resourcedetection`).

### OTLP export

`<SIGNAL>` is `TRACES`, `METRICS` or `LOGS`. Every per-signal variable falls
back to the generic one, then to the default.

| Variable | Default | Description |
|----------|---------|-------------|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | _(unset)_ | Generic Collector endpoint, `http(s)://host:port[/prefix]` (or deprecated-for-HTTP schemeless `host:port`). `/v1/<signal>` is appended for http/protobuf. **Unset = that signal is not exported** (see [No OTLP endpoint](#when-no-otlp-endpoint-is-configured)). gRPC: a path (other than `/`) is ignored and warned once, naming the variable only — `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=http://c:4317/v1/traces` sends to `c:4317` (A29). |
| `OTEL_EXPORTER_OTLP_<SIGNAL>_ENDPOINT` | _(unset)_ | Per-signal endpoint, used verbatim; configures OTLP for that signal alone |
| `OTEL_EXPORTER_OTLP[_<SIGNAL>]_PROTOCOL` | `grpc` | `grpc` or `http/protobuf`. Unset: port `4318` on that signal's endpoint infers `http/protobuf`. `http/json` is warned and ignored |
| `OTEL_EXPORTER_OTLP[_<SIGNAL>]_INSECURE` | _(unset)_ | `true` = plaintext for **schemeless** endpoints only; with `http://`/`https://` the scheme decides and a contradicting value is warned and ignored |
| `OTEL_EXPORTER_OTLP[_<SIGNAL>]_CERTIFICATE` | _(unset)_ | CA bundle for TLS (read by the SDK) |
| `OTEL_EXPORTER_OTLP[_<SIGNAL>]_CLIENT_CERTIFICATE` / `_CLIENT_KEY` | _(unset)_ | mTLS client certificate and key (read by the SDK); ignored with a warning on a plaintext endpoint |
| `OTEL_EXPORTER_OTLP[_<SIGNAL>]_HEADERS` | _(unset)_ | `key=value,key2=value2`; values percent-decoded. Use it for auth (`Authorization=Basic%20<base64>`). Values of 4 characters or more, and each space-separated part of 4 or more (the token of `Bearer <token>`), are masked as `<redacted>` in SDK error text; the helper's OTel error handler also drops OTLP/HTTP response bodies (A29, [HOW-TO](HOW-TO.md#sdk-errors-response-bodies-and-header-values)). The Go SDK's own internal logger can still print a malformed item when the exporter is built (README [known gaps](https://github.com/StaffOps/otel-libs/blob/main/README.md#known-emitted-telemetry-gaps)). |
| `OTEL_EXPORTER_OTLP[_<SIGNAL>]_TIMEOUT` | SDK `10000` | Export timeout in **milliseconds**, clamped to [1 ms, 24 h] |
| `OTEL_EXPORTER_OTLP[_<SIGNAL>]_COMPRESSION` | `gzip` | `gzip` or `none` (case-insensitive) |

> **Redirects are refused** (0.3, task A31). The OTLP/HTTP exporters never follow a `3xx`: that export fails (it is not retried) and the helper writes one ERROR per signal and status code on its stderr logger (`msg="otelhelper: the OTLP endpoint for traces answered HTTP 307 (a redirect); redirects are not followed, …" signal=traces status=307`), never the `Location` value; the `3xx` response reaches the exporter with an empty body (redirectors often repeat the `Location` there), so its export error carries no body either. 0.2.x followed redirects: a 307/308 replayed the POST, its body and the custom OTLP headers to the `Location` host (net/http drops its credential headers — `Authorization`, `Www-Authenticate`, `Cookie`, `Cookie2` — only on a redirect to a different domain that is not a subdomain; other headers, API keys included, were always forwarded); a 301/302/303 turned it into a GET without a body. Configure the final endpoint URL.

### Signals, exporters and propagation

| Variable | Default | Description |
|----------|---------|-------------|
| `OTEL_TRACES_EXPORTER` / `OTEL_LOGS_EXPORTER` | `otlp` | Comma list of `otlp`, `console` (compact JSON on stdout), `none`. `logging` = deprecated `console` (warned; in code it is an unknown exporter); `none` in a list wins (warned). Set to a valid entry, it wins over `OTEL_HELPER_DISABLED_SIGNALS` for its signal |
| `OTEL_METRICS_EXPORTER` | _(inferred)_ | `otlp`, `prometheus`, `console`, `none` or a list (`otlp,prometheus`). Unset: `otlp` when a metrics endpoint is configured, else `prometheus`. `otlp` without an endpoint is warned and dropped |
| `OTEL_METRIC_EXPORT_INTERVAL` | `30000` | Periodic export interval in ms (helper default; the SDK's is 60000): ASCII digits with an optional `+` (any number of digits; leading zeros ignored), > 0, else warned and 30 000; above 2 147 483 647 clamped with a warning |
| `OTEL_METRICS_EXEMPLAR_FILTER` | SDK `trace_based` | Read by the SDK; the helper never sets it |
| `OTEL_TRACES_SAMPLER` / `OTEL_TRACES_SAMPLER_ARG` | `parentbased_always_on` | The six spec samplers (case-insensitive); unknown or unsupported names warn and use the default. `_ARG` is a plain decimal in [0, 1] |
| `OTEL_PROPAGATORS` | `tracecontext,baggage` | `tracecontext`, `baggage`, `none`. `b3`, `b3multi`, `jaeger`, `xray`, `ottrace` are warned and skipped |
| `OTEL_SDK_DISABLED` | `false` | `true` installs no-op providers (no exporters, no `/metrics` listener) and logs one INFO line; propagation still works |
| `OTEL_CONFIG_FILE` / `OTEL_EXPERIMENTAL_CONFIG_FILE` | _(unset)_ | Declarative configuration is **not supported**: ignored with one warning per process naming the variable (never the path) |
| `OTEL_SEMCONV_STABILITY_OPT_IN` | _(unset)_ | Read by contrib instrumentations: `rpc/dup` or `rpc/old` (otelgrpc), `database` / `database/dup` (`ext/otelsql`, `ext/otelredis`, `ext/otelaws`), and by `ext/otelaws` `rpc` / `rpc/dup` (AWS RPC keys: legacy by default; A30). `rpc/dup` also switches otelgrpc to dup: old and new RPC metrics, double the series. `rpc` is harmless for otelgrpc (its default is already the new mode) |

### Helper variables

| Variable | Default | Description |
|----------|---------|-------------|
| `OTEL_HELPER_DEBUG_LEVEL` | `false` | `true`: DEBUG log level **and** `debug=true` on entry spans ([Debug mode](HOW-TO.md#6-debug-mode)) |
| `OTEL_EXPORTER_PROMETHEUS_PORT` | _(unset)_ | Standalone `/metrics` listener port; beats `OTEL_HELPER_METRICS_PORT`, code (Go: `WithPrometheusMetricsPort` / `WithoutMetricsListener`) still wins. Same grammar; `0` = no listener; invalid → warned, next level (A29) |
| `OTEL_EXPORTER_PROMETHEUS_HOST` | every interface | Listener bind address: an IPv4/IPv6 literal (brackets allowed, no zone id) or `localhost` (= `127.0.0.1`); `::` = every interface. Unset binds every interface — **not** the spec default `localhost`, so other pods can scrape. Other values (host names, `*`) are warned and every interface is used; no code option (A29) |
| `OTEL_HELPER_METRICS_PORT` | `9464` | Port of the standalone Prometheus `/metrics` listener (dual-stack, `GET`/`HEAD /metrics` only: other paths 404, other methods 405); ASCII digits with an optional `+`, in [0, 65535], anything else warns and uses 9464; `0` = no listener (mount `MetricsHandler()` yourself). Used when `OTEL_EXPORTER_PROMETHEUS_PORT` is unset or invalid. |
| `OTEL_HELPER_DISABLED_METRICS` | _(unset)_ | Comma list of patterns (`http.server.*`), each a case-sensitive glob on the instrument name (`*` any run of characters, `.` and `/` included; `?` one character; everything else literal, no `[...]` classes); items trimmed, blank items ignored; matching instruments are dropped from every exporter |
| `OTEL_HELPER_HTTP_OPT_IN_METRICS` | `false` | `true` keeps otelhttp's body-size histograms (`http.server.request.body.size`, `http.server.response.body.size`, `http.client.request.body.size`), dropped by default (semconv opt-in metrics; A30). `WithHTTPOptInMetrics(bool)` wins. otelhttp has no `http.server.active_requests`. The drop applies to the contrib instrumentation scopes: an application's own instrument of those names is kept |
| `OTEL_HELPER_SAMPLE_RATIO` | _(unset)_ | **Deprecated** (warns once): ratio in [0, 1] → `ParentBased(TraceIDRatioBased(x))`, only while `OTEL_TRACES_SAMPLER` is unset. Use `OTEL_TRACES_SAMPLER=parentbased_traceidratio` + `_ARG` |
| `OTEL_HELPER_DISABLED_SIGNALS` | _(unset)_ | **Deprecated** (warns once): `traces`, `metrics`, `logs`. Use `OTEL_<SIGNAL>_EXPORTER=none`. Ignored for a signal whose `OTEL_<SIGNAL>_EXPORTER` holds a valid entry, and for metrics when `WithMetricExporters` is used; unknown entries warn once |
| `OTEL_HELPER_EXTRA_INSTRUMENTATION` | _(unset)_ | **Removed** in Go 0.3.0: no effect, warns once when set. Import an [`ext/*` module](#opt-in-extensions-ext) instead |

Deprecated variables keep working until 1.0 and warn even when the standard
variable wins, so they can be removed from manifests early.

> **`OTEL_HELPER_DEBUG_LEVEL=true` in production raises volume and cost.**
> Use it for targeted troubleshooting only.

---

## Options

Options passed to `Setup` win over every environment variable.

| Option | Effect |
|--------|--------|
| `WithServiceName(name)` | `service.name`; empty fails `Setup` |
| `WithEnvironment(env)` | `deployment.environment.name` (`LOCAL`, `DEV`, `HML`, `PRD` or any value, emitted verbatim); empty fails `Setup` |
| `WithResourceAttributes(map[string]string)` | Extra resource attributes; each key wins over `OTEL_RESOURCE_ATTRIBUTES` |
| `WithEndpoint(endpoint)` | Generic endpoint: URL (`http://c:4318/otlp`) or `host:port`; `/v1/<signal>` appended for http/protobuf |
| `WithTracesEndpoint` / `WithMetricsEndpoint` / `WithLogsEndpoint` | Per-signal endpoint URL, path used verbatim |
| `WithOtlpProtocol(p)` | `ProtocolGRPC` (`grpc`) or `ProtocolHTTP` (`http/protobuf`) for every signal; anything else fails `Setup` |
| `WithInsecure(bool)` | Plaintext/TLS for **schemeless** endpoints; contradicting a code URL's scheme fails `Setup` |
| `WithCompression(c)` | `CompressionGzip` (default) or `CompressionNone`, every signal |
| `WithExportTimeout(ms)` | Export timeout for every signal (> 0) |
| `WithMetricExporters(names...)` | `otlp`, `prometheus`, `console`, `none` (trimmed, case-insensitive, duplicates dropped); `otlp` without an endpoint, an unknown value (`logging` included) or `none` with others fails `Setup` |
| `WithExportInterval(ms)` | Metric export interval in [1, 2 147 483 647] ms |
| `WithPrometheusMetricsPort(port)` | Standalone `/metrics` port in [0, 65535] |
| `WithoutMetricsListener()` | No standalone listener; mount `MetricsHandler()` on your mux |
| `WithDisabledMetrics(patterns)` | Drop instruments matching the patterns (same glob as `OTEL_HELPER_DISABLED_METRICS`; items trimmed, blanks ignored); `nil` = use env, `[]string{}` = none (beats env) |
| `WithHTTPOptInMetrics(on)` | Keep (`true`) or drop (`false`, the default) the semconv opt-in HTTP metrics (body sizes); wins over `OTEL_HELPER_HTTP_OPT_IN_METRICS` (A30) |
| `WithDisabledSignals(signals)` | `traces`, `metrics`, `logs` (other names fail `Setup`); `nil` = use env, `[]string{}` = none |
| `WithSampleRatio(ratio)` | Root-span ratio in [0, 1] → `ParentBased(TraceIDRatioBased)`, any value (1.0 included) beats `OTEL_TRACES_SAMPLER`; NaN / out of range fails `Setup` |
| `WithHealthPaths(paths...)` | Replace the [health path list](#health-checks-spans-dropped-metrics-kept); `WithHealthPaths()` = none. Paths must be ASCII, start with `/`, no `?` `#` `,` |
| `WithDebug()` | Debug mode (see `OTEL_HELPER_DEBUG_LEVEL`) |

**Removed in 0.3.0:** `WithExtraInstrumentation`, `Options.HasInstrumentation`
and the `Options.ExtraInstrumentation` field (they never changed what Go
instruments), and the `UnaryServerInterceptor` / `StreamServerInterceptor` /
`UnaryClientInterceptor` / `StreamClientInterceptor` functions (otelgrpc
dropped interceptors; use the stats handlers).

---

## API reference

### `Setup(ctx, ...Option) (Shutdown, error)`

Configures traces, metrics and logs. Every signal is built before anything is
installed: on error, what was built is released, the global providers are left
untouched and `Setup` can be retried (the returned `Shutdown` is a no-op).
While a pipeline is installed, later calls return its `Shutdown`. A `Setup`
called while a `Shutdown` is in flight waits for it, then builds a fresh
pipeline (or returns `ctx.Err()` if its own context ends first).

`Shutdown(ctx)` flushes logs, metrics and traces **concurrently** under `ctx`
(errors joined), is idempotent (later or concurrent calls return the first
result) and resets the helper, so a following `Setup` builds a new pipeline.
Tracers, meters and instrumented handlers obtained *before* that new `Setup`
stay bound to the shut-down providers — obtain them after it. The helper
installs **no signal handlers**: see [Lifecycle](HOW-TO.md#8-lifecycle-and-graceful-shutdown).

### `GetTracer(name ...string) trace.Tracer` / `GetMeter(name ...string) metric.Meter`

Tracer / Meter from the global provider; the name (instrumentation scope)
defaults to `otel-helper`.

### `StartRootSpan(ctx, tracer, name, ...SpanStartOption) (context.Context, trace.Span)`

Starts a span with no parent (a new trace) even if `ctx` carries one — one
trace per worker iteration or consumed message.

### `WithSpan(ctx, tracer, name, fn, ...SpanStartOption) error` / `RecordFailure(ctx, err)`

Application spans with one failure rule in every language (R11,
[ADR-016](../specs/decisions/ADR-016-application-span-failure-status.md)):

```go
err := otelhelper.WithSpan(ctx, tracer, "order.process", func(ctx context.Context) error {
    return process(ctx, order) // a returned error marks the span
})

// A failure the code handles instead of returning:
otelhelper.RecordFailure(ctx, &OrderRejectedError{ID: id})
```

| `fn` outcome | Span | Log record |
|---|---|---|
| returns `nil` | status **Unset** (the helper never sets Ok) | none |
| returns an error | status **ERROR**, empty description, `error.type`; no `exception` event | one ERROR record in the span's context |
| returns a cancellation: every leaf of the error tree is `context.Canceled` or a gRPC status with `codes.Canceled` (`fmt.Errorf("q: %w", ctx.Err())` included) | **Unset** | none |
| returns `context.DeadlineExceeded` (a timeout) | ERROR | one |
| panics | ERROR, then re-panics with the same value (an `error` value follows the error rows, a cancellation included) | one |
| panics with or returns `http.ErrAbortHandler` | Unset (a panic is re-panicked) | none |
| `runtime.Goexit` | Unset | none |

The error is returned unchanged. `trace.WithNewRoot()` in the options makes a
root span (`StartRootSpan` stays a plain span starter); a nil `tracer` is
`GetTracer()` (scope `otel-helper`, global provider). `RecordFailure` marks
`trace.SpanFromContext(ctx)` the same way, always records, and does not apply
the cancellation rule; a nil error does nothing.

- **`error.type`** (also the record's `exception.type`): the first
  non-empty `ErrorType() string` method along the `Unwrap` chain wins (the
  upstream convention; an empty one does not hide a deeper one), else `semconv.ErrorType` (semconv v1.43.0): a single-`%w`
  wrapper is stripped, a pointer type prints the package name
  (`*fs.PathError`), a value type the full import path. `errors.New`
  (`*errors.errorString`), several `%w` (`*fmt.wrapErrors`) and `errors.Join`
  (`*errors.joinError`) are shared buckets: give application errors an
  `ErrorType()` method. A panicking `ErrorType()` (typed-nil receiver) falls
  back to `%T`; a panic value that is not an `error` gets `%T`.
- **The record** goes through the helper's slog handler (OTel bridge with a
  logs exporter, stdout JSON otherwise, with `"logger":"otel-helper"` on
  stdout — the bridge scope name): severity ERROR, body = the error text
  (`<nil>` for a typed nil), `exception.type`, `exception.message`,
  `exception.stacktrace` (`debug.Stack()` where the helper marks the span,
  without the helper's own leading frames: the first frame is the caller of
  `WithSpan` or `RecordFailure`; for a panic, the stack is taken at the
  recover and starts at the runtime `panic(...)` frame, then the panic site,
  in every layer of nested `WithSpan`; `debug.Stack` keeps at most 100 frames,
  and above 12 KiB the helper cuts the middle, with
  `...N frames elided by otel-helper...`). It is emitted in
  the span's context also when the span is not sampled. With logs disabled
  the span is still marked. A stdout write error is reported once on stderr.
- **One record per span.** A failure that crosses several `WithSpan` calls
  marks every span and each one emits its own record in its own span context:
  3 nested `WithSpan` give 3 records. Use `WithSpan` only at the layers that
  should report. Details: [HOW-TO §3b](HOW-TO.md#3b-application-span-failures-withspan-and-recordfailure).

### `NewHTTPHandler(handler http.Handler, operation string) http.Handler`

otelhttp server instrumentation: a server span per request and
`http.server.request.duration` (seconds, stable semconv). Span names are
`<METHOD> <route>` from the `http.ServeMux` pattern (`GET /orders/{id}`, with
`http.route`), or `<METHOD>` without a pattern. The `operation` argument is
ignored since 0.3.0 (kept for source compatibility). Health checks are
measured; their spans are dropped by the sampler.

A response with a 5xx status, or a code 600–999, gets `error.type` = the
status code (`"500"`) on the server span and on
`http.server.request.duration` (design D16, A40: otelhttp v0.72 sets it on
client spans only):

- **Span:** only when the span is recording, is an SDK span and has no
  `error.type` yet (a `RecordFailure` value is kept). 4xx and 1xx–3xx get none.
- **Metric, always, sampled or not** (an inbound `traceparent …-00`, a health
  path whose span the sampler drops): the `error.type` `RecordFailure` (or a
  `WithSpan` returning an error) gave **the server span itself** — remembered
  per request, so an unsampled span gives the same value as a sampled one —
  or one set on the recording server span directly; else an `error.type` the
  application added to the otelhttp `Labeler` (`otelhttp.LabelerFromContext`;
  the helper adds nothing then, so the point has exactly the application's
  value); else the status code. A failing child span (`WithSpan` inside the
  handler) does not count: the server span keeps the status code, sampled or
  not. A Labeler-only value makes the span say `500` and the metric say the
  application's value; use `RecordFailure` on the request context to keep
  both the same. `RecordFailure` is the supported way to set the value (the
  last call wins); a direct `SetAttributes` of `error.type` on the server
  span reaches the metric only when no `RecordFailure` value exists. The
  server span is recognized by its own (local) span context, so with no
  TracerProvider installed or with traces disabled the metric gets the status
  code, whatever the handler records (an inbound `traceparent` is the
  caller's context, not the server span).
- With `OTEL_HELPER_HTTP_OPT_IN_METRICS=true` the body-size histograms
  (`http.server.request.body.size`, `http.server.response.body.size`) carry
  the same `error.type`: otelhttp records them with the same attributes.
- **Status as otelhttp reads it:** the first `WriteHeader` (1xx other than
  101 ignored), an implicit 200 on the first `Write` or `Flush`, 200 when the
  handler writes nothing; a body sent with `ReadFrom` (`io.Copy` onto the
  writer) before any `WriteHeader` is not seen (200). So `error.type` and
  `http.response.status_code` never disagree.
- The handler's writer keeps the server writer's optional interfaces
  (`http.Flusher`, `http.Hijacker`, `io.ReaderFrom`, `http.Pusher`;
  `httpsnoop`, a direct dependency since 0.3.0) and gains none.
- A panicking handler gets no `error.type` (otelhttp re-panics before the
  helper runs).

### `NewHTTPTransport(base http.RoundTripper) http.RoundTripper`

otelhttp client instrumentation (`nil` wraps `http.DefaultTransport`): client
spans named `HTTP <METHOD>`, `http.client.request.duration`, and `traceparent`
injection. Requests to a health path get no client span and no client metric.

### gRPC (stats handlers)

| Function | Description |
|----------|-------------|
| `GRPCServerOption()` | `grpc.ServerOption` installing the server stats handler (unary + streaming) |
| `GRPCDialOption()` | `grpc.DialOption` installing the client stats handler |
| `GRPCServerHandler()` / `GRPCClientHandler()` | The `stats.Handler`s themselves |

```go
srv := grpc.NewServer(otelhelper.GRPCServerOption())
conn, err := grpc.NewClient(addr, grpc.WithTransportCredentials(insecure.NewCredentials()),
    otelhelper.GRPCDialOption())
```

Metrics: `rpc.server.call.duration` / `rpc.client.call.duration` (seconds,
`rpc.system.name`, `rpc.method="pkg.Svc/Method"`). Server-side
`grpc.health.v1.Health/*` calls are counted, their spans dropped; client-side
health calls get neither span nor metric.

Failed RPCs get `error.type` = the canonical status code name on the span
(design D16, A46; otelgrpc v0.72 sets it on the metrics only), when the span
is recording, is an SDK span and has none:

| Span | Gets `error.type` for | Value |
|------|-----------------------|-------|
| CLIENT | every non-OK status | `CANCELLED` … `UNAUTHENTICATED` (`FAILED_PRECONDITION`, not Go's `FailedPrecondition`); `_OTHER` for a code above 16 |
| SERVER | the six codes otelgrpc marks ERROR: `UNKNOWN`, `DEADLINE_EXCEEDED`, `UNIMPLEMENTED`, `INTERNAL`, `UNAVAILABLE`, `DATA_LOSS` | the same names; any other code leaves the span Unset with no `error.type` |

grpc-go reports an error without a code (a handler returning
`errors.New(...)`) as `UNKNOWN`, so Go gives `_OTHER` only for a code above
16. For a code above 16 the span gets `_OTHER` while otelgrpc's
`rpc.client.call.duration` gets `CODE(<n>)` (its own name table). Unary,
server, client and bidi streaming calls take the same path. A filtered client
health call has no span, and no other span is written to on its behalf — not
the caller's, and not the enclosing server span when the call is made inside
a gRPC handler with the handler's context.

Failed RPC spans carry **no status description** (task A37, ADR-014
decision 4): otelgrpc v0.72 wrote the gRPC status message there — the peer's
text on CLIENT spans, the handler's `status.Error` text (or a plain error's
`Error()`) on SERVER spans for the six codes above — and that text can quote
request values. The handlers hand otelgrpc a copy of the end-of-RPC stats
whose error keeps its code with an empty message, so the status code,
`error.type`, `rpc.response.status_code` and the metrics stay; the caller
still receives the full status; your own `SetStatus` description on the RPC
span stays when the RPC ends OK or with a code otelgrpc leaves Unset (on a
failure otelgrpc's own status replaces it). `NewHTTPTransport` client spans
drop the description too: otelhttp wrote a failed round trip's
`*url.Error` text there, which quotes the whole URL, presigned query
included — defense in depth, as a custom `RoundTripper`'s error or a body
read/write error can quote it too. No log record is written (Go errors carry no exception
object): log the detail yourself, or use `RecordFailure` in the handler.

### Logging: `NewSlogHandler()`, `NewLogger(env, debug)`, `DefaultLogLevel(env, debug)`

`NewSlogHandler()` returns one `slog.Handler` that decides **per record**:

| State | Destination |
|-------|-------------|
| `Setup` installed a logs exporter (OTLP and/or `OTEL_LOGS_EXPORTER=console`) | OTel bridge (`otelslog`) → that pipeline's `LoggerProvider` |
| Before `Setup`, no logs endpoint, `OTEL_LOGS_EXPORTER=none`, after `Shutdown` | stdout, one JSON line per record |

A handler or logger created before `Setup` switches when `Setup` runs, and
back to stdout after `Shutdown`. Stdout shape (same as Python):

```json
{"time":"2026-10-28T18:41:25.734Z","level":"ERROR","msg":"order failed","order.id":"ord-3","exception":"card declined by issuer"}
```

`time` is RFC 3339 UTC with milliseconds; `level` is `DEBUG`/`INFO`/`WARN`/`ERROR`;
the first error-valued attribute of the record becomes `exception` (its
`Error()` text). A record emitted in a valid span context (a `*Context` call)
carries `trace_id` and `span_id` right after `msg` (after `logger` when the
record has one; `With` attributes stay before them), and a top-level
attribute of either name is then dropped:

```json
{"time":"2026-11-17T10:02:11.201Z","level":"WARN","msg":"retrying","trace_id":"4bf92f3577b34da6a3ce929d0e0e4736","span_id":"00f067aa0ba902b7","attempt":2}
```

Under a `WithGroup` the ids stay at the top level, before the group object.

**Trace correlation** needs the `*Context` calls: `logger.InfoContext(ctx, …)`
attaches the active span's `trace_id`/`span_id` to the exported record;
`logger.Info(…)` does not.

`NewSlogHandler` applies the level the last `Setup` resolved (INFO before it)
on both paths, stdout and the OTel bridge, as Python does;
`NewLogger(env, debug)` wraps the same handler with the level of `env`:

| `DefaultLogLevel(env, debug)` | Level |
|-------------|-------|
| `LOCAL` | Debug |
| `DEV`, `HML` | Info |
| `PRD` | Warn |
| unset / any other value | Info |
| `debug=true` | Debug |

### `MetricsHandler() http.Handler`

Prometheus exposition of the helper's dedicated registry (not the
`client_golang` global one). Status:

| State | Status |
|-------|--------|
| Before `Setup`, after a failed `Setup`, after `Shutdown` | `503` |
| Prometheus deliberately off (`OTEL_SDK_DISABLED=true`, or `OTEL_METRICS_EXPORTER` without `prometheus`) | `200`, empty body |
| Prometheus reader active | `200`, metrics |

### Other exports

`DefaultHealthPaths()`, the `DeploymentEnvironment` constants (`LOCAL`, `DEV`,
`HML`, `PRD`), `ProtocolGRPC` / `ProtocolHTTP`, `CompressionGzip` /
`CompressionNone`, and the `Env*` constants naming every variable above.

---

## What is configured automatically

| Area | Behavior |
|------|----------|
| Resource | `service.name`, `deployment.environment.name` (when resolved), `service.instance.id`, `telemetry.sdk.*`; schema URL `https://opentelemetry.io/schemas/1.43.0` |
| Propagation | W3C TraceContext + Baggage (`OTEL_PROPAGATORS`) |
| Sampler | `parentbased_always_on` by default ([ADR-002](https://github.com/StaffOps/otel-libs/blob/main/specs/decisions/ADR-002-parentbased-sampler.md)) wrapped by the health-check drop; tail sampling happens at the Collector |
| Traces | Batch span processor → OTLP |
| Metrics | Go runtime metrics (`go.memory.used`, `go.memory.limit` (only when `GOMEMLIMIT` is set), `go.memory.allocated`, `go.memory.allocations`, `go.memory.gc.goal`, `go.goroutine.count`, `go.processor.limit`, `go.config.gogc`); exemplars (SDK `trace_based`); export every 30 s |
| Logs | Batch log processor → OTLP, via the slog handler above |
| Diagnostics | Helper warnings go to a stderr `slog` text logger (never `slog.Default()`), and never print header values, client-key paths or URL userinfo/query |

`OTEL_GO_X_DEPRECATED_RUNTIME_METRICS=true` also emits the 0.2.0
`process.runtime.go.*` / `runtime.uptime` set.

### When no OTLP endpoint is configured

"OTLP configured" is decided **per signal**: a code endpoint, the generic env
endpoint, or that signal's env endpoint.

| Signal without an endpoint | Behavior |
|--------|----------|
| Traces | Not exported; spans are still created and context propagates |
| Metrics | Prometheus `/metrics` on `:9464` (`OTEL_HELPER_METRICS_PORT`) |
| Logs | stdout JSON (the shape above) |

So `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` alone exports traces, serves metrics on
`/metrics` and writes logs to stdout. The `/metrics` fallback is the one
exception to "everything via the Collector"
([ADR-007](https://github.com/StaffOps/otel-libs/blob/main/specs/decisions/ADR-007-keep-prometheus-in-core.md)).

### Health checks: spans dropped, metrics kept

The sampler drops the **server** span of a health check that has no local
parent (none, or a remote `traceparent`); spans the handler starts follow it
under the default ParentBased sampler. Health requests stay in
`http.server.request.duration` / `rpc.server.call.duration`, so failing probes
remain visible — exclude them in SLI queries
([design D5](https://github.com/StaffOps/otel-libs/blob/main/specs/config-contract-v0.3/design.md#d5--health-check-exclusion)).

- HTTP: `/health /healthz /ready /readyz /live /livez /ping /metrics` — exact,
  case-insensitive, trailing slash and query ignored (`/users/ping`,
  `/healthcare`, `/health/live` stay traced). Matched on `url.path`
  (`http.target` fallback). `WithHealthPaths(...)` replaces the list for
  server and client.
- gRPC: the whole `grpc.health.v1.Health/` service (Check, Watch, List), always.
- Clients (`NewHTTPTransport`, `GRPCDialOption`): health calls get no span
  **and no metric** (upstream filter behavior).

---

## Opt-in extensions (`ext/`)

Separate Go modules, so core stays free of AWS/Redis/SQL dependencies:

```bash
go get github.com/staffops/otel-libs/go/ext/otelaws
go get github.com/staffops/otel-libs/go/ext/otelredis
go get github.com/staffops/otel-libs/go/ext/otelsql
```

```go
// AWS SDK v2: middleware on the aws.Config (semconv v1.43, rpc.system.name)
otelaws.Instrument(&cfg)

// go-redis v9: tracing + pool metrics; call once per client
if err := otelredis.Instrument(rdb); err != nil { ... } // tracing is already installed on a metrics error

// database/sql: tracing + db.sql.* pool metrics
db, closeDB, err := otelsql.Open("pgx", dsn)
if err != nil { ... }
defer closeDB() // instead of db.Close(): unregisters the pool-metrics callback, then closes
```

| Module | What it instruments |
|--------|---------------------|
| `ext/otelaws` | AWS SDK v2 calls (S3, SQS, DynamoDB, …): spans `<Service>.<Operation>`, CLIENT except SQS/SNS send and publish (PRODUCER) and SQS receive (CONSUMER), with the legacy `rpc.system=aws-api`, `rpc.service`, `rpc.method=<Operation>` (`OTEL_SEMCONV_STABILITY_OPT_IN=rpc`: `rpc.system.name`, `rpc.method=<Service>/<Operation>`; `rpc/dup`: both), `server.address`/`server.port` of the request, `messaging.system` `aws.sns` / `aws_sqs`, `messaging.operation.name` = the AWS operation (`Publish`, `SendMessage`), DynamoDB `db.system.name=aws.dynamodb` plus the legacy `db.system=dynamodb` (same opt-in rule as `ext/otelsql`) |
| `ext/otelredis` | go-redis commands: spans named by the uppercased command (`SET`, `CLUSTER INFO`; a pipeline `SET GET`; the dial span `connect`) with `db.system.name=redis`, `db.operation.name` as go-redis provides it (`set`), no command values (`db.statement`) and no connection string; `db.client.connections.*` pool metrics (Prometheus `db_client_connections_usage`, `_max`, `_idle_max`, `_idle_min`, `_waits_total`, `_timeouts_total`, `_hits_total`, `_misses_total`, …) |
| `ext/otelsql` | `database/sql` queries and `db.sql.*` pool metrics. `db.system.name` from the driver: `pgx`, `pgx/v5`, `postgres` → `postgresql`; `sqlite`, `sqlite3` → `sqlite`; `mysql`; `sqlserver`, `mssql` → `microsoft.sql_server`; else `other_sql`. The legacy `db.system` key is emitted too until 1.0, unless `OTEL_SEMCONV_STABILITY_OPT_IN` contains `database` without `database/dup`. `db.namespace` (and legacy `db.name` with the legacy keys) = the database name, see below. Failed operations carry `db.response.status_code` / `error.type`, see below. A pool-metrics registration failure is returned by `Open` (the DB is closed) |

#### `ext/otelsql`: `db.namespace` and error attributes (design D16, A38, A44)

`Open(driverName, dsn string, opts ...otelsql.Option)` (variadic since 0.3.0,
source compatible). `db.namespace` is resolved once per `Open`, in this order:

1. `otelsql.WithDBNamespace(name)` (an empty name is ignored);
2. the database name parsed from the DSN (standard library only):
   - PostgreSQL URL `postgres://` / `postgresql://`, parsed with `net/url`
     as pgx parses it (a parse error, such as an unencoded `/` in the
     password, is unknown): the path, with `?dbname=` (or pgx's `?database=`)
     winning — also when present but empty, which gives no name, as in pgx;
     libpq's percent-encoded socket host (`postgres://%2Fvar%2Frun%2Fpostgresql/db`)
     is read too; userinfo, query and fragment never reach the namespace;
   - PostgreSQL keyword form `dbname=…` or pgx's `database=…` (`dbname` wins;
     quoted `dbname='orders'`, backslash escapes, the last occurrence wins;
     an unquoted value may hold `=`, such as a base64 password). A malformed
     string — an unterminated quote, a key without `=` — is unknown. An empty
     `dbname=` reads the next keyword as its value, which the name check
     below rejects (no `PGDATABASE` fallback, one line);
   - go-sql-driver/mysql `[user[:pass]@][net[(addr)]]/dbname[?params]`, cut at
     the last `/` (a password may contain `/`), path-unescaped; a candidate
     holding `@`, `(` or `)` (a DSN with no `/dbname` whose password has a
     `/`) is unknown, and so is a DSN with neither `@` nor `(` (`u:a/b`)
     unless it starts with `/` (`/orders`: no user, default address);
   - SQL Server: the URL's `?database=` (parsed with `net/url`, as
     go-mssqldb does), or `database=` /
     `initial catalog=` in the ADO and `odbc:` forms, tokenized on `;` with
     `{braces}` (`}}` = `}`) and `"`/`'` quotes (doubled = literal); an
     unbalanced or trailing-text value makes the whole string unknown. The
     database only, also with a named instance (a documented deviation from
     semconv `sql-server.md`);
3. for the drivers mapped to `postgresql` (`pgx`, `pgx/v5`, `postgres`,
   `postgresql`), the `PGDATABASE` environment variable — not when the DSN
   names a `service=` (the service file decides the database);
4. else none, and one line on stderr per driver name per process, naming the
   driver only (never the DSN): `otelhelper: db.namespace unknown for driver
   <name>; pass otelsql.WithDBNamespace`.

A name from steps 2–3 that holds `@ : , = ( ) { }` or whitespace is treated
as unknown (step 4; a rejected DSN name does not fall back to `PGDATABASE`): a parse gone wrong never puts a credential or a host in
an attribute. The `WithDBNamespace` value is used as given.

Valid pgx URLs that get no `db.namespace` (no attribute and one diagnostics
line, never a leak; pass `WithDBNamespace` instead): a multi-host list of
IPv6 addresses with ports (`postgres://[::1]:5432,[::2]:5433/db`, which
`net/url` rejects), a password with an unencoded `?` or `#`, and a database
name with whitespace (`my%20db`), which the name check rejects on purpose.

SQLite (`sqlite`, `sqlite3`) gets no `db.namespace` from the DSN (a file path
is not a namespace and may leak) and no line; `WithDBNamespace` still sets
one. A DSN registered with pgx `stdlib.RegisterConnConfig` is a key, not a
DSN: pass `WithDBNamespace(cfg.Database)`. With no database in the DSN and no
`PGDATABASE`, pgx and libpq connect to the database named after the user;
the helper does not guess it (no attribute, one line). MySQL: the namespace
is the database given at connect time; a later `USE other` is not reflected
(semconv `mysql.md` asks for this to be documented). A schema given at connect time
(`search_path`, `options`) is not part of the namespace (semconv
`postgresql.md` allows the database alone). The name goes on every span, on
`db.client.operation.duration` and on the `db.sql.*` pool metrics.

A failed operation (status ERROR, no description, as before):

| Driver error | Span | `db.client.operation.duration` |
|--------------|------|------------|
| PostgreSQL SQLSTATE (`SQLState()` method: pgx `*pgconn.PgError`, lib/pq `*pq.Error`), accepted as `^[0-9A-Z]{5}$` with at least one digit (`EPIPE` is rejected, and so is an application-defined code of letters only, `RAISE ... USING ERRCODE = 'ABCDE'`: `_OTHER`) | `db.response.status_code` = `error.type` = `23505` | `db.response.status_code=23505`; `error.type` = the Go type name (XSAM's, see the root README gaps) |
| MySQL server error (`Number` field of a struct named `MySQLError`, go-sql-driver `*mysql.MySQLError`, read by reflection), 1–65535 outside the client range 2000–2999 | `1062` on both | `db.response.status_code=1062`; `error.type` = the Go type name |
| none of these (connection refused, a client error such as MySQL 2003, SQLite, other databases) | `error.type=_OTHER`, no `db.response.status_code` | `error.type` = the Go type name |
| `driver.ErrSkip` (go-sql-driver returns it for every statement with arguments when `interpolateParams=false`) | Unset, no attribute, no exception event | no `error.type` (counted as a success) |

The code is searched along the error's `Unwrap` chain (`%w`, `errors.Join`);
message text never reaches an attribute. Only errors are recorded (PostgreSQL
warnings and notices are not). The values are written only where none is set:
XSAM writes neither attribute itself and the helper installs no XSAM
attributes getter. Exception: XSAM can call the getter twice for one
`sql.rows` span (from `Next` and from `Close`): the second error's attributes
overwrite the same keys, so a code-less second error leaves the first
`db.response.status_code` next to `error.type=_OTHER`.

**Exception detail goes to a correlated log** ([ADR-014](../specs/decisions/ADR-014-exception-detail-in-correlated-logs.md), task A36).
A failed operation's `exception` event keeps only `exception.type` (the
helper's `error.type` rule of `WithSpan`: the first `ErrorType()` method in
the chain, else `semconv.ErrorType`, e.g. `*pgconn.PgError`); no
`exception.message`, no `exception.stacktrace`. The error's text and a stack
go to one ERROR log record per recorded error, emitted in the span's context
(same `trace_id` / `span_id`, also when the span is not sampled), through the
helper's log channel: OTLP while a logs exporter is installed, else the
stdout JSON fallback (`logger` `otel-helper`, then `trace_id` and `span_id`);
the level rules of `NewSlogHandler` apply (ERROR passes every environment
level). Body and `exception.message` = `err.Error()`; `exception.type` = the
event's type; `exception.stacktrace` = `debug.Stack()` taken where XSAM
records the error, without the helper's frames, so it starts at
`github.com/XSAM/otelsql.recordSpanError` and runs down through
`database/sql` to your call (Go errors carry no stack of their own; cut in the
middle above 12 KiB). The record also carries `db.system.name` and, when
the error has one, `db.response.status_code` and `error.type` (the same
values as the span, computed for an unsampled span too), so database records
can be selected apart from application failure records. A `sql.rows` span
whose `Next` and `Close` both fail with different errors gives two records;
the same error recorded twice on one span (pgx returns it from `Rows.Err` and
`Close`) gives one. A cancellation (`context.Canceled`, a gRPC `Canceled`
status) keeps its type-only event and writes no record, as `WithSpan`. A
failed query inside your own `WithSpan` gives two records: the database
span's and the `WithSpan` span's, each in its own span context. The record
can quote statement values or row data (`Duplicate entry 'x'`,
`invalid input syntax for type integer: "x"`): treat the log pipeline as
sensitive (ADR-014 Negative). The severity is ERROR and the naming
`exception.*`, a deliberate deviation from the database exception
conventions (root README gaps).

`ext/otelsql` requires the core module for this channel (`replace => ../..`
on the branch; a release tags the core first): importing it brings the
core's dependency graph (OTLP exporters, gRPC, the Prometheus exporter) into
your module graph; the binary links only what is used. Without
`otelhelper.Setup` (or after `Shutdown`) the records still go to the stdout
JSON fallback at INFO, so a program that uses `ext/otelsql` without `Setup`
sees one stdout line per failed operation.

`driver.ErrSkip`, the full story: database/sql retries a skipped
`ExecContext`/`QueryContext` as prepare + `stmt.exec`/`stmt.query`. The
skipped attempt is still a near-zero `sql.conn.exec` / `sql.conn.query` span
and one `db.client.operation.duration` measurement, now Unset and counted as
a success (before: span ERROR with an exception event; measurement
`error.type=database/sql/driver.ErrSkip`). With go-sql-driver/mysql and
`interpolateParams=false`, every statement with arguments adds one such span
and point: error ratios drop and latency percentiles shift down. Set
`interpolateParams=true` (one round trip, no prepare; go-sql-driver refuses
it with some multibyte connection charsets such as BIG5, CP932, GB2312, GBK
and SJIS), or filter `db_operation_name` / the span name in queries and
dashboards.

([design D14](https://github.com/StaffOps/otel-libs/blob/main/specs/config-contract-v0.3/design.md#d14--go-ext-modules-api-a10-validation),
[D16](https://github.com/StaffOps/otel-libs/blob/main/specs/config-contract-v0.3/design.md#d16--protocol-span-error-attributes-r12-tasks-a38-a40-a44-a46-a43-upstream))

---

## Migration from 0.2.0

The full list, with every telemetry-output change for dashboards and alerts, is
in the [CHANGELOG](../CHANGELOG.md) (Unreleased → Go and the "telemetry output
changes" table). The changes most likely to need action:

| 0.2.0 | 0.3.0 — what to do |
|-------|--------------------|
| `SERVICE_NAME`, `ENVIRONMENT` | Still work, warn. Set `OTEL_SERVICE_NAME` and `OTEL_RESOURCE_ATTRIBUTES=deployment.environment.name=<env>` |
| Default `service.name` `my-service`, environment `LOCAL` | Unset name → `unknown_service:<exe>` + warning; unset environment → attribute **absent** (log level INFO, not DEBUG). The Collector needs a tail-sampling branch for spans without it |
| `OTEL_EXPORTER_OTLP_INSECURE=true` with `https://` gave plaintext | The scheme wins: use `http://` for plaintext |
| `https://host` (no port) under http/protobuf went to 4317 | Goes to 443; set the port explicitly |
| `/v1/<signal>` added by hand to work around a dropped path prefix | Remove it: prefixes are kept (`…/otlp` → `/otlp/v1/<signal>`) |
| Credentials in the endpoint URL (`https://user:pw@host`) | Stripped with a warning: use `OTEL_EXPORTER_OTLP_HEADERS=Authorization=Basic%20<base64>` |
| Header keys like `x+y` | Skipped with a warning (keys must match `[A-Za-z0-9._-]+`); keys compared case-insensitively |
| `OTEL_EXPORTER_OTLP_TIMEOUT` ignored (10 s fixed) | Honored, in milliseconds |
| `WithExtraInstrumentation`, `HasInstrumentation`, `OTEL_HELPER_EXTRA_INSTRUMENTATION` | Removed / no effect: import `ext/otelsql`, `ext/otelredis`, `ext/otelaws` |
| `otelsql.Open` returned `(db, err)` | Returns `(db, closeDB, err)`: `defer closeDB()` |
| `*Interceptor()` gRPC functions | `grpc.NewServer(otelhelper.GRPCServerOption())`, `grpc.NewClient(target, otelhelper.GRPCDialOption())` |
| `http.server.duration` (ms), `rpc.server.duration` (ms), `process.runtime.go.*` | `http.server.request.duration` (s), `rpc.server.call.duration` (s), `go.*` runtime metrics — update queries |
| Server span names from the `operation` argument | `<METHOD> <route>`: filter on `http.route` |
| Health requests had no metric | Counted in `http.server.request.duration`: exclude probe routes in SLIs |
| `MetricsHandler()` 503 when Prometheus was off | `200` with an empty body |
| `NewSlogHandler()` without an endpoint dropped records | Writes stdout JSON |
| Go 1.22 | Go 1.26 |

---

## Architecture

```
[ Go app: otelhelper.Setup ]
      │ OTLP (gRPC :4317 or http/protobuf :4318), gzip, TLS by scheme
      ▼
[ OTel Collector: k8sattributes, tail sampling ]
      ▼
 traces → Tempo · metrics → VictoriaMetrics/Prometheus · logs → Loki
```

- The SDK samples with `parentbased_always_on` (health spans dropped); what to
  keep is decided by tail sampling at the Collector gateway.
- The SDK sets only `service.name`, `deployment.environment.name` and
  `service.instance.id`; the Collector enriches the rest.
- Everything goes through the Collector, never directly to a backend — except
  the Prometheus `/metrics` fallback.

## Tests

See [TESTS.md](TESTS.md). Quick run (unit + parity, from the repo root):

```bash
docker compose -f testing/docker-compose.yml run --rm go-tests
```

## Project structure

```
go/
├── otelhelper.go      # Setup / Shutdown lifecycle, GetTracer, GetMeter, resource
├── options.go         # Options + With* functional options
├── config.go          # Env resolution, validation, deprecations, propagators
├── endpoint.go        # OTLP endpoint / TLS / timeout / compression / headers resolution
├── tracing.go         # TracerProvider, sampler, StartRootSpan
├── failure.go         # WithSpan, RecordFailure (application span failure rule, R11)
├── health.go          # Health-check sampler and path matching
├── processors.go      # debug=true marker on entry spans
├── metrics.go         # MeterProvider, runtime metrics, Prometheus reader, MetricsHandler
├── logging.go         # LoggerProvider, per-record slog handler, NewLogger, DefaultLogLevel
├── console.go         # console (stdout) exporters
├── middleware.go      # NewHTTPHandler, NewHTTPTransport, gRPC stats handlers
├── diag.go            # stderr diagnostics logger, redaction, warn-once
├── instrumentation.go # pointer to the ext/ modules
├── doc.go             # package documentation
├── *_test.go          # unit + parity tests (TESTS.md)
├── ext/               # otelaws, otelredis, otelsql (separate modules)
└── example/           # go-api, go-backend, go-process, go-custom-signals
```
