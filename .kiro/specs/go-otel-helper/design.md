# Design — Go OTel Helper Library

## Architecture Decision: Flat Package

**Decision:** Single flat package `otelhelper` (no sub-packages).

**Rationale:**
- Go idiom: small, focused packages. This library is small enough for one package.
- Simpler import: `import "github.com/staffops/otel-helper-go"` → `otelhelper.Setup(ctx)`
- Avoids circular dependency issues between config/tracing/metrics.
- Matches the Python pattern where `from otel_helper import setup_telemetry` is the primary interface.
- Internal organization via separate files (not packages).

---

## Pipeline Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        Go Application                            │
│                                                                  │
│  Setup() ──→ TracerProvider  ──→ BatchSpanProcessor ─────┐       │
│          ──→ MeterProvider   ──→ PeriodicReader ─────────┤       │
│          ──→ LoggerProvider  ──→ BatchLogProcessor ──────┤       │
│                                                          │       │
│  Propagators: W3C TraceContext + Baggage                 │       │
│                                                          ▼       │
│                                              OTLP gRPC :4317     │
└──────────────────────────────────────────────────────────────────┘
                                                   │
                                                   ▼
                                    ┌──────────────────────────┐
                                    │   OpenTelemetry Collector │
                                    │   - tail sampling         │
                                    │   - k8sattributes         │
                                    │   - resource enrichment   │
                                    └──────────────────────────┘
                                                   │
                              ┌─────────────┬──────┴──────┐
                              ▼             ▼              ▼
                          Tempo          VictoriaMetrics   Loki
                         (traces)        (metrics)        (logs)
```

---

## Package Layout

```
go/
├── go.mod
├── go.sum
├── doc.go                    # Package documentation
├── otelhelper.go             # Setup(), shutdown, GetTracer(), GetMeter(), globals
├── options.go                # Options struct, functional options (With*)
├── config.go                 # DeploymentEnvironment, env resolution, validation
├── tracing.go                # configureTracing(), StartRootSpan()
├── metrics.go                # configureMetrics()
├── logging.go                # configureLogging(), NewSlogHandler()
├── processors.go             # debugProcessor (SpanProcessor)
├── middleware.go             # NewHTTPHandler(), gRPC interceptor wrappers
├── instrumentation.go        # HasInstrumentation(), InstrumentSQL()
├── otelhelper_test.go        # Setup integration tests
├── options_test.go           # Config + env var tests
├── tracing_test.go           # StartRootSpan + DebugProcessor tests
├── middleware_test.go        # HTTP/gRPC middleware tests
├── testutil_test.go          # Shared test helpers (in-memory exporter setup)
├── Dockerfile.test           # Docker-based test runner
├── .dockerignore
├── example/
│   ├── README.md
│   ├── protos/
│   │   └── anomaly.proto
│   ├── go-api/
│   │   ├── main.go
│   │   ├── Dockerfile
│   │   └── Dockerfile.demo
│   ├── go-backend/
│   │   ├── main.go
│   │   ├── Dockerfile
│   │   └── Dockerfile.demo
│   └── go-process/
│       ├── main.go
│       ├── Dockerfile
│       └── Dockerfile.demo
```

---

## Config Resolution Pattern

### Struct + Functional Options

```go
type Options struct {
    ServiceName          string
    Environment          DeploymentEnvironment
    OtelEndpoint         string
    DebugLevel           bool
    ExtraInstrumentation string
    ExportTimeoutMs      int
    SampleRatio          float64
    ResourceAttributes   map[string]string
}

type Option func(*Options)

func WithServiceName(name string) Option       { return func(o *Options) { o.ServiceName = name } }
func WithEnvironment(env DeploymentEnvironment) Option { return func(o *Options) { o.Environment = env } }
func WithDebug() Option                        { return func(o *Options) { o.DebugLevel = true } }
func WithSampleRatio(ratio float64) Option     { return func(o *Options) { o.SampleRatio = ratio } }
func WithEndpoint(endpoint string) Option      { return func(o *Options) { o.OtelEndpoint = endpoint } }
func WithExtraInstrumentation(instr string) Option { return func(o *Options) { o.ExtraInstrumentation = instr } }
func WithResourceAttributes(attrs map[string]string) Option { return func(o *Options) { o.ResourceAttributes = attrs } }
func WithExportTimeout(ms int) Option          { return func(o *Options) { o.ExportTimeoutMs = ms } }
```

### Resolution Order

1. Start with defaults (same as Python/dotnet).
2. Apply functional options (programmatic overrides).
3. For fields still at default value, resolve from env vars.
4. Validate (fail-fast).

```go
func newOptions(opts ...Option) *Options {
    o := &Options{
        ServiceName:          "my-service",
        Environment:          LOCAL,
        ExtraInstrumentation: "SQL",
        ExportTimeoutMs:      10_000,
        SampleRatio:          1.0,
        ResourceAttributes:   make(map[string]string),
    }
    for _, opt := range opts {
        opt(o)
    }
    o.resolveFromEnv()
    return o
}
```

---

## Setup & Shutdown Pattern

```go
type Shutdown func(ctx context.Context) error

var (
    mu         sync.Mutex
    setupDone  bool
    shutdownFn Shutdown
)

// noopShutdown is returned when Setup fails so callers always get a safe function.
func noopShutdown(_ context.Context) error { return nil }

func Setup(ctx context.Context, opts ...Option) (Shutdown, error) {
    mu.Lock()
    defer mu.Unlock()

    if setupDone {
        return shutdownFn, nil
    }

    options := newOptions(opts...)
    if err := options.validate(); err != nil {
        // Validation failure does NOT set setupDone — caller can retry with valid config.
        return noopShutdown, fmt.Errorf("otelhelper: %w", err)
    }

    resource := buildResource(options)

    tp, err := configureTracing(ctx, resource, options)
    if err != nil { return noopShutdown, err }

    mp, err := configureMetrics(ctx, resource, options)
    if err != nil {
        tp.Shutdown(ctx) // cleanup already-created provider
        return noopShutdown, err
    }

    lp, err := configureLogging(ctx, resource, options)
    if err != nil {
        tp.Shutdown(ctx)
        mp.Shutdown(ctx)
        return noopShutdown, err
    }

    configurePropagators()

    shutdownFn = func(ctx context.Context) error {
        return errors.Join(
            tp.Shutdown(ctx),
            mp.Shutdown(ctx),
            lp.Shutdown(ctx),
        )
    }
    setupDone = true
    return shutdownFn, nil
}
```

---

## Tracing Configuration

```go
func configureTracing(ctx context.Context, res *resource.Resource, opts *Options) (*sdktrace.TracerProvider, error) {
    exporter, err := otlptracegrpc.New(ctx,
        otlptracegrpc.WithEndpoint(opts.OtelEndpoint),
        otlptracegrpc.WithInsecure(),
        otlptracegrpc.WithTimeout(time.Duration(opts.ExportTimeoutMs)*time.Millisecond),
        otlptracegrpc.WithCompressor("gzip"),
    )
    if err != nil {
        return nil, fmt.Errorf("trace exporter: %w", err)
    }

    // ParentBased wrapping: respects parent decisions, applies ratio to root spans
    var rootSampler sdktrace.Sampler
    if opts.SampleRatio >= 1.0 {
        rootSampler = sdktrace.AlwaysSample()
    } else {
        rootSampler = sdktrace.TraceIDRatioBased(opts.SampleRatio)
    }
    sampler := sdktrace.ParentBased(rootSampler)

    processors := []sdktrace.SpanProcessor{
        sdktrace.NewBatchSpanProcessor(exporter),
    }
    if opts.DebugLevel {
        processors = append(processors, &debugProcessor{})
    }

    tpOpts := []sdktrace.TracerProviderOption{
        sdktrace.WithResource(res),
        sdktrace.WithSampler(sampler),
    }
    for _, p := range processors {
        tpOpts = append(tpOpts, sdktrace.WithSpanProcessor(p))
    }

    tp := sdktrace.NewTracerProvider(tpOpts...)
    otel.SetTracerProvider(tp)
    return tp, nil
}
```

Key design decision: **ParentBased wrapping** ensures child spans inherit the parent's sampling decision. This is critical for distributed tracing — if a parent service sampled the trace, all downstream services must also sample it.

---

## Metrics Configuration

```go
func configureMetrics(ctx context.Context, res *resource.Resource, opts *Options) (*sdkmetric.MeterProvider, error) {
    // Exemplar filter via env var (programmatic API not exported in SDK v1.31.0)
    if os.Getenv("OTEL_METRICS_EXEMPLAR_FILTER") == "" {
        os.Setenv("OTEL_METRICS_EXEMPLAR_FILTER", "trace_based")
    }

    exporter, err := otlpmetricgrpc.New(ctx,
        otlpmetricgrpc.WithEndpoint(opts.OtelEndpoint),
        otlpmetricgrpc.WithInsecure(),
        otlpmetricgrpc.WithCompressor("gzip"),
    )
    if err != nil {
        return nil, fmt.Errorf("metric exporter: %w", err)
    }

    mp := sdkmetric.NewMeterProvider(
        sdkmetric.WithResource(res),
        sdkmetric.WithReader(sdkmetric.NewPeriodicReader(exporter)),
    )
    otel.SetMeterProvider(mp)

    // Start runtime metrics (goroutines, GC, memory). Non-fatal if it fails.
    if err := runtime.Start(runtime.WithMeterProvider(mp)); err != nil {
        otel.Handle(err)
    }

    return mp, nil
}
```

**Exemplar filter:** `OTEL_METRICS_EXEMPLAR_FILTER=trace_based` is set via env var (the programmatic `sdkmetric.WithExemplarFilter` API is not exported in the current SDK version). This attaches trace_id/span_id to metric data points when the span is sampled, enabling Grafana's "Exemplar" feature (click metric → jump to trace).

**Runtime metrics:** `go.opentelemetry.io/contrib/instrumentation/runtime` automatically collects goroutine count, GC stats, and memory usage.

---

## Logging Configuration

```go
func configureLogging(ctx context.Context, res *resource.Resource, opts *Options) (*sdklog.LoggerProvider, error) {
    exporter, err := otlploggrpc.New(ctx,
        otlploggrpc.WithEndpoint(opts.OtelEndpoint),
        otlploggrpc.WithInsecure(),
    )
    if err != nil {
        return nil, fmt.Errorf("log exporter: %w", err)
    }

    lp := sdklog.NewLoggerProvider(
        sdklog.WithResource(res),
        sdklog.WithProcessor(sdklog.NewBatchProcessor(exporter)),
    )
    global.SetLoggerProvider(lp)
    return lp, nil
}

// NewSlogHandler returns an slog.Handler bridging to OTel logs.
// Logs emitted within a span context automatically include trace_id and span_id.
func NewSlogHandler() slog.Handler {
    return otelslog.NewHandler("otel-helper")
}

// DefaultLogLevel returns the appropriate slog.Level for a given environment.
// LOCAL=Debug, DEV/HML=Info, PRD=Warning. Debug override forces Debug.
func DefaultLogLevel(env DeploymentEnvironment, debug bool) slog.Level {
    if debug { return slog.LevelDebug }
    switch env {
    case LOCAL: return slog.LevelDebug
    case DEV, HML: return slog.LevelInfo
    case PRD: return slog.LevelWarn
    default: return slog.LevelInfo
    }
}

// NewLogger returns a configured *slog.Logger with OTel bridge and environment-appropriate level.
func NewLogger(env DeploymentEnvironment, debug bool) *slog.Logger {
    level := DefaultLogLevel(env, debug)
    handler := NewSlogHandler()
    return slog.New(levelFilterHandler{level: level, inner: handler})
}
```

---

## StartRootSpan

```go
// StartRootSpan starts a new span detached from any parent context (new trace).
// Use in workers where each iteration should be an independent trace.
func StartRootSpan(ctx context.Context, tracer trace.Tracer, name string, opts ...trace.SpanStartOption) (context.Context, trace.Span) {
    opts = append(opts, trace.WithNewRoot())
    return tracer.Start(ctx, name, opts...)
}
```

The key insight: `trace.WithNewRoot()` tells the SDK to ignore any parent span in the context and create a new trace ID. Non-span values (deadlines, cancellation) from the original context are preserved.

---

## Debug Processor

```go
type debugProcessor struct{}

func (d *debugProcessor) OnStart(parent context.Context, s sdktrace.ReadWriteSpan) {
    if !s.Parent().IsValid() {
        s.SetAttributes(attribute.String("debug", "true"))
    }
}

func (d *debugProcessor) OnEnd(s sdktrace.ReadOnlySpan)       {}
func (d *debugProcessor) Shutdown(ctx context.Context) error   { return nil }
func (d *debugProcessor) ForceFlush(ctx context.Context) error { return nil }
```

This integrates with the Collector's tail-sampling policy:
```yaml
# Collector config (reference)
tail_sampling:
  policies:
    - name: debug-always
      type: string_attribute
      string_attribute:
        key: debug
        values: ["true"]
```

---

## Middleware Design

### HTTP

```go
var healthPaths = map[string]struct{}{
    "/ping": {}, "/health": {}, "/healthz": {}, "/ready": {},
}

func NewHTTPHandler(handler http.Handler, operation string) http.Handler {
    return otelhttp.NewHandler(handler, operation,
        otelhttp.WithFilter(func(r *http.Request) bool {
            _, skip := healthPaths[r.URL.Path]
            return !skip
        }),
    )
}

// NewHTTPTransport wraps an http.RoundTripper with OTel tracing for outgoing requests.
func NewHTTPTransport(base http.RoundTripper) http.RoundTripper {
    if base == nil {
        base = http.DefaultTransport
    }
    return otelhttp.NewTransport(base,
        otelhttp.WithFilter(func(r *http.Request) bool {
            _, skip := healthPaths[r.URL.Path]
            return !skip
        }),
    )
}
```

### gRPC

```go
func grpcHealthFilter(info *otelgrpc.InterceptorInfo) bool {
    return info.Method != "/grpc.health.v1.Health/Check"
}

func UnaryServerInterceptor() grpc.UnaryServerInterceptor {
    return otelgrpc.UnaryServerInterceptor(
        otelgrpc.WithInterceptorFilter(grpcHealthFilter),
    )
}

func StreamServerInterceptor() grpc.StreamServerInterceptor {
    return otelgrpc.StreamServerInterceptor(
        otelgrpc.WithInterceptorFilter(grpcHealthFilter),
    )
}

func UnaryClientInterceptor() grpc.UnaryClientInterceptor {
    return otelgrpc.UnaryClientInterceptor(
        otelgrpc.WithInterceptorFilter(grpcHealthFilter),
    )
}

func StreamClientInterceptor() grpc.StreamClientInterceptor {
    return otelgrpc.StreamClientInterceptor(
        otelgrpc.WithInterceptorFilter(grpcHealthFilter),
    )
}
```

---

## Propagators

```go
func configurePropagators() {
    otel.SetTextMapPropagator(propagation.NewCompositeTextMapPropagator(
        propagation.TraceContext{},
        propagation.Baggage{},
    ))
}
```

W3C TraceContext ensures cross-language compatibility. The `traceparent` header carries trace_id + span_id + sampling flag. Baggage propagates key-value pairs across service boundaries.

---

## Resource Construction

```go
func buildResource(opts *Options) *resource.Resource {
    attrs := []attribute.KeyValue{
        semconv.ServiceNameKey.String(opts.ServiceName),
        semconv.DeploymentEnvironmentKey.String(string(opts.Environment)),
    }
    for k, v := range opts.ResourceAttributes {
        attrs = append(attrs, attribute.String(k, v))
    }

    res, _ := resource.Merge(
        resource.Default(),
        resource.NewWithAttributes(semconv.SchemaURL, attrs...),
    )
    return res
}
```

**Principle:** SDK sets only `service.name` + `deployment.environment`. The Collector's `k8sattributes` processor enriches with `k8s.pod.name`, `k8s.namespace.name`, etc.

---

## Dependencies (go.mod)

```
module github.com/staffops/otel-helper-go

go 1.22

require (
    go.opentelemetry.io/otel                    v1.28.0
    go.opentelemetry.io/otel/sdk                v1.28.0
    go.opentelemetry.io/otel/sdk/metric         v1.28.0
    go.opentelemetry.io/otel/sdk/log            v0.4.0
    go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracegrpc  v1.28.0
    go.opentelemetry.io/otel/exporters/otlp/otlpmetric/otlpmetricgrpc v1.28.0
    go.opentelemetry.io/otel/exporters/otlp/otlplog/otlploggrpc      v0.4.0
    go.opentelemetry.io/otel/bridge/otelslog    v0.4.0
    go.opentelemetry.io/contrib/instrumentation/net/http/otelhttp     v0.53.0
    go.opentelemetry.io/contrib/instrumentation/google.golang.org/grpc/otelgrpc v0.53.0
    google.golang.org/grpc                      v1.65.0
)
```

---

## Error Handling Philosophy

- `Setup()` returns `error` — never panics.
- Invalid config → descriptive error with field name and env var hint.
- Exporter connection failures → logged, not fatal (OTel SDK handles graceful degradation).
- Shutdown errors → joined via `errors.Join()`, caller decides severity.

---

## Testing Strategy

### In-Memory Exporter Pattern

```go
func setupTestProvider(t *testing.T) (*tracetest.InMemoryExporter, trace.Tracer) {
    t.Helper()
    exporter := tracetest.NewInMemoryExporter()
    tp := sdktrace.NewTracerProvider(
        sdktrace.WithSpanProcessor(sdktrace.NewSimpleSpanProcessor(exporter)),
    )
    t.Cleanup(func() { tp.Shutdown(context.Background()) })
    return exporter, tp.Tracer("test")
}
```

### Test Categories

| File | Tests |
|------|-------|
| `options_test.go` | Defaults, env resolution, priority, validation |
| `tracing_test.go` | StartRootSpan independence, DebugProcessor root-only, ParentBased sampler |
| `otelhelper_test.go` | Setup returns shutdown, double-init guard, error on bad config |
| `middleware_test.go` | Health path filtering, span creation on non-health, gRPC filter |

### Environment Isolation

Use `t.Setenv()` (Go 1.17+) for env var tests — automatically cleaned up.

---

## Cross-Language Compatibility Matrix

| Feature | .NET | Python | Go |
|---------|------|--------|----|
| Setup call | `services.AddOtelHelper()` | `setup_telemetry()` | `otelhelper.Setup(ctx)` |
| Config | `TelemetryOptions` | `TelemetryOptions` | `Options` + functional opts |
| Env vars | Same 6 vars | Same 6 vars | Same 6 vars |
| Sampler | ParentBased(ratio) | ParentBased(ratio) | ParentBased(ratio) |
| Debug attr | `debug=true` on root | `debug=true` on root | `debug=true` on root |
| Health filter | `/health`, `/healthz`, `/ping`, `/ready` | `/health`, `/healthz`, `/ping`, `/ready` | `/health`, `/healthz`, `/ping`, `/ready` |
| Propagators | W3C TC + Baggage | W3C TC + Baggage | W3C TC + Baggage |
| Export | OTLP gRPC :4317 | OTLP gRPC :4317 | OTLP gRPC :4317 |
| Exemplars | TraceBasedExemplarFilter | TraceBasedExemplarFilter | OTEL_METRICS_EXEMPLAR_FILTER=trace_based (env var) |
| Log bridge | ILogger → OTel | logging → OTel | slog → OTel |
| StartRootSpan | `StartRootActivity()` | `start_root_span()` | `StartRootSpan()` |

---

## Shutdown Sequence

```
SIGTERM/SIGINT received
    → context cancelled
    → Shutdown(ctx) called
        → TracerProvider.Shutdown() — flushes BatchSpanProcessor
        → MeterProvider.Shutdown() — flushes PeriodicReader
        → LoggerProvider.Shutdown() — flushes BatchLogProcessor
    → errors.Join() returns combined error
    → os.Exit(0)
```

Consumer pattern:
```go
ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
defer stop()

shutdown, err := otelhelper.Setup(ctx)
if err != nil { log.Fatal(err) }
defer func() {
    shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
    defer cancel()
    if err := shutdown(shutdownCtx); err != nil {
        log.Printf("otel shutdown: %v", err)
    }
}()
```
