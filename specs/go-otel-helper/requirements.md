# Requirements — Go OTel Helper Library

## Overview

Go implementation of the `otel-helper` library, following the same patterns established by the Python and .NET implementations. Provides a single-call setup function that configures traces, metrics, and logs via OTLP gRPC to the OpenTelemetry Collector.

Primary consumer: `anomaly-detection-controller` (Go gRPC service with background workers).

---

## User Stories & Acceptance Criteria

### US-1: Single-call Setup

**As a** Go developer,
**I want** to call `otelhelper.Setup()` once at application startup,
**So that** traces, metrics, and logs are configured without boilerplate.

**Acceptance Criteria:**
- `Setup(ctx context.Context, opts ...Option) (Shutdown, error)` configures TracerProvider, MeterProvider, and LoggerProvider globally.
- Returns a `Shutdown` function (`func(ctx context.Context) error`) for deferred cleanup.
- Returns an error on invalid config (does NOT panic).
- Double-call is a no-op (returns existing shutdown, nil error).
- With no options, resolves all config from environment variables.

---

### US-2: Configuration via TelemetryOptions

**As a** developer,
**I want** configuration resolved from env vars with programmatic overrides,
**So that** I can deploy the same binary across environments without code changes.

**Acceptance Criteria:**
- `Options` struct with fields: `ServiceName`, `Environment`, `OtelEndpoint`, `DebugLevel`, `ExtraInstrumentation`, `ExportTimeoutMs`, `SampleRatio`, `ResourceAttributes`.
- Env var resolution (same as Python/dotnet):

| Field | Env Var | Default |
|-------|---------|---------|
| ServiceName | `SERVICE_NAME` / `OTEL_SERVICE_NAME` | `my-service` |
| Environment | `ENVIRONMENT` | `LOCAL` |
| OtelEndpoint | `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://localhost:4317` |
| DebugLevel | `OTEL_HELPER_DEBUG_LEVEL` | `false` |
| ExtraInstrumentation | `OTEL_HELPER_EXTRA_INSTRUMENTATION` | `SQL` |
| SampleRatio | `OTEL_HELPER_SAMPLE_RATIO` | `1.0` |

- Functional options pattern: `WithServiceName("x")`, `WithEnvironment(PRD)`, `WithDebug()`, etc.
- Explicit option values override env vars.
- Validation (fail-fast): empty ServiceName → error, invalid endpoint → error, timeout ≤ 0 → error.

---

### US-3: Tracer and Meter Accessors

**As a** developer,
**I want** `GetTracer()` and `GetMeter()` helpers,
**So that** I can create spans and metrics without importing the OTel SDK directly.

**Acceptance Criteria:**
- `GetTracer(name ...string) trace.Tracer` — returns tracer from global provider; defaults to `"otel-helper"`.
- `GetMeter(name ...string) metric.Meter` — returns meter from global provider; defaults to `"otel-helper"`.
- Both work after `Setup()` has been called.

---

### US-4: Distributed Traces with HTTP/gRPC

**As a** developer building microservices,
**I want** automatic span creation for HTTP and gRPC calls,
**So that** distributed traces are captured without manual instrumentation.

**Acceptance Criteria:**
- HTTP middleware creates server spans with `http.method`, `http.route`, `http.status_code` attributes.
- gRPC interceptors create spans with `rpc.system`, `rpc.service`, `rpc.method` attributes.
- Context propagation works across HTTP → gRPC boundaries (W3C TraceContext headers + gRPC metadata).
- Child spans inherit parent trace ID from incoming request context.

---

### US-5: Health Path Filtering

**As an** operator,
**I want** health/readiness probes excluded from traces,
**So that** Kubernetes liveness checks don't pollute trace data.

**Acceptance Criteria:**
- Paths `/ping`, `/health`, `/healthz`, `/ready` produce NO spans.
- gRPC health service (`grpc.health.v1.Health/Check`) is filtered on server interceptors.
- Filtering is applied by default (no opt-in required).

---

### US-6: Sampling Strategy

**As an** operator,
**I want** configurable head sampling with ParentBased wrapping,
**So that** the SDK respects parent sampling decisions while allowing ratio-based sampling for root spans.

**Acceptance Criteria:**
- Sampler is `ParentBased(root=TraceIDRatioBased(ratio))`.
- When `SampleRatio >= 1.0`, uses `ParentBased(root=AlwaysSample())`.
- Parent decisions are always honored (child inherits parent's sampling decision).
- Default ratio is `1.0` (AlwaysOn) — tail sampling happens at the Collector.

---

### US-7: Debug Mode

**As an** operator,
**I want** debug mode to tag root spans with `debug=true`,
**So that** the Collector tail-sampling policy forces 100% retention for debug traces.

**Acceptance Criteria:**
- When `DebugLevel=true`, a SpanProcessor sets attribute `debug=true` on root spans (`parent.IsValid() == false`).
- Child spans are NOT tagged (Collector inherits decision from root).
- Debug mode also enables ALL conditional instrumentations.
- Matches the Collector `string_attribute` tail-sampling policy used by Python/dotnet.

---

### US-8: W3C Context Propagation

**As a** developer,
**I want** W3C TraceContext and Baggage propagation configured by default,
**So that** traces are correlated across services regardless of language.

**Acceptance Criteria:**
- Propagators: `TraceContext` + `Baggage` (composite).
- HTTP: `traceparent` and `tracestate` headers injected/extracted.
- gRPC: propagation via metadata (automatic with `otelgrpc` interceptors).
- Compatible with .NET and Python implementations (same W3C headers).

---

### US-9: StartRootSpan for Workers

**As a** developer building background workers,
**I want** `StartRootSpan()` to create a new independent trace,
**So that** each worker iteration is a separate trace (not a child of a previous one).

**Acceptance Criteria:**
- `StartRootSpan(ctx context.Context, tracer trace.Tracer, name string, opts ...trace.SpanStartOption) (context.Context, trace.Span)`.
- Detaches from parent context — returned span has no parent (new trace ID).
- Caller is responsible for `span.End()`.
- Works correctly even when called inside an existing span context.

---

### US-10: Metrics with Exemplars

**As an** operator,
**I want** metrics linked to traces via exemplars,
**So that** I can jump from a metric spike to the exact trace that caused it.

**Acceptance Criteria:**
- MeterProvider configured with `TraceBasedExemplarFilter` (exemplars attached when span is sampled).
- PeriodicReader exports metrics via OTLP gRPC.
- Histograms and counters automatically include trace_id/span_id exemplars.
- Compatible with Grafana Tempo → Mimir exemplar linking.

---

### US-11: Log Correlation via slog

**As a** developer,
**I want** structured logs correlated with traces,
**So that** I can navigate from a log entry to its trace context.

**Acceptance Criteria:**
- LoggerProvider configured with OTLP gRPC exporter + BatchProcessor.
- `NewSlogHandler()` returns an `slog.Handler` bridging to OTel logs.
- Logs include `trace_id` and `span_id` when emitted within a span context.
- Compatible with Go 1.21+ `log/slog`.

---

### US-12: Conditional Instrumentations

**As a** developer,
**I want** optional instrumentations (SQL, AWS, Redis) enabled via config,
**So that** I only pay the overhead for what I use.

**Acceptance Criteria:**
- `HasInstrumentation(name string) bool` — checks `ExtraInstrumentation` comma-separated list.
- Debug mode enables ALL instrumentations.
- Case-insensitive matching.
- Instrumentations are opt-in helpers (e.g., `InstrumentSQL(db *sql.DB)`) — not auto-magic.

---

### US-13: OTLP gRPC Export

**As an** operator,
**I want** all signals exported via OTLP gRPC to the Collector,
**So that** the architecture follows the "everything via Collector" principle.

**Acceptance Criteria:**
- All three signals (traces, metrics, logs) use OTLP gRPC exporters.
- Default endpoint: `http://localhost:4317` (port 4317).
- Connection: insecure by default (TLS terminated at mesh/sidecar level).
- Timeout: configurable via `ExportTimeoutMs` (default 10s).
- Compression: gzip.
- No direct-to-backend exports (Tempo, Loki, etc.) — Collector only.

---

### US-14: Resource Attributes

**As an** operator,
**I want** the SDK to set only `service.name` as resource attribute,
**So that** the Collector enriches with k8s/cloud attributes (single source of truth).

**Acceptance Criteria:**
- Resource includes `service.name` from config.
- Resource includes `deployment.environment` from config.
- Additional attributes via `WithResourceAttributes(map[string]string)` option.
- SDK does NOT set `k8s.*`, `cloud.*`, `host.*` — Collector's `k8sattributes` processor handles these.

---

### US-15: Graceful Shutdown

**As a** developer,
**I want** telemetry providers to flush and shut down cleanly,
**So that** no data is lost on application termination.

**Acceptance Criteria:**
- `Shutdown(ctx context.Context) error` flushes all providers (traces, metrics, logs).
- Shutdown respects context deadline (bounded wait).
- Errors from individual provider shutdowns are joined (`errors.Join`).
- Works with `signal.NotifyContext` for SIGTERM/SIGINT handling.
- No goroutine leaks after shutdown.

---

### US-16: Module Structure

**Acceptance Criteria:**
```
go/
├── go.mod                    # module github.com/staffops/staffops-otel-libs/go
├── go.sum
├── doc.go                    # Package documentation
├── otelhelper.go             # Setup(), Shutdown type, GetTracer(), GetMeter()
├── options.go                # Options struct, functional options, env resolution
├── config.go                 # DeploymentEnvironment enum, validation, defaults
├── tracing.go                # configureTracing(), StartRootSpan()
├── metrics.go                # configureMetrics()
├── logging.go                # configureLogging(), NewSlogHandler()
├── processors.go             # DebugProcessor
├── middleware.go             # NewHTTPHandler(), gRPC interceptors
├── instrumentation.go        # HasInstrumentation(), InstrumentSQL(), etc.
├── otelhelper_test.go        # Integration tests
├── options_test.go           # Config/env resolution tests
├── tracing_test.go           # StartRootSpan, DebugProcessor tests
├── middleware_test.go        # HTTP/gRPC middleware tests
├── testutil_test.go          # Shared test helpers (in-memory exporter)
├── Dockerfile.test           # Docker-based test runner
├── example/
│   ├── README.md
│   ├── protos/
│   │   └── anomaly.proto
│   ├── go-api/              # net/http example
│   ├── go-backend/          # gRPC server example
│   └── go-process/          # Worker example (anomaly-detection pattern)
└── .dockerignore
```

- Go 1.22+
- Single flat package `otelhelper` (no sub-packages).

---

### US-17: Testing

**Acceptance Criteria:**
- Unit tests use `sdktrace.NewTracerProvider` + `tracetest.InMemoryExporter` (no network).
- Config tests: defaults, env var resolution, validation errors, priority (explicit > env).
- Tracing tests: StartRootSpan creates independent trace, DebugProcessor tags root only, ParentBased sampler behavior.
- Setup tests: double-init guard, shutdown flushes, error on invalid config.
- All tests runnable via Docker: `docker run --rm -v $(pwd)/go:/src -w /src golang:1.22-alpine go test ./...`
- Target: ≥80% coverage on config + setup + tracing.

---

### US-18: Example Applications

**Acceptance Criteria:**

| Example | Pattern | Key Demonstrations |
|---------|---------|-------------------|
| `go-api` | `net/http` server | HTTP middleware, manual spans, metrics counter, exemplars |
| `go-backend` | gRPC server | gRPC interceptors, service handler spans, context propagation |
| `go-process` | Background workers | `StartRootSpan` per iteration, metrics gauges, graceful shutdown |

- Each example has a `Dockerfile` and `Dockerfile.demo`.
- Examples demonstrate distributed tracing across services (HTTP → gRPC propagation).
- `go-process` mirrors the anomaly-detection-controller pattern.

---

### US-19: CI Pipeline

**Acceptance Criteria:**
- **unit-test**: `go test ./... -coverprofile=coverage.out` via Docker.
- **build-dev**: `go build ./...` — validates compilation.
- **demo**: Build example Dockerfiles, verify health endpoint responds.
- All stages use `golang:1.22-alpine` base image.
- No local Go SDK dependency — everything via Docker.

---

## Non-Functional Requirements

| Requirement | Target |
|-------------|--------|
| Go version | 1.22+ |
| Thread safety | `sync.Mutex` for Setup (allows retry on validation failure), no race conditions |
| Zero vendor SDKs | Only `go.opentelemetry.io/*` + `google.golang.org/grpc` |
| Startup overhead | < 50ms for Setup() |
| No goroutine leaks | Shutdown closes all background exporters |
| W3C Trace Context | TraceContext + Baggage propagators |
| OTLP gRPC export | Port 4317, insecure, gzip compression |
| Resource attributes | Only `service.name` + `deployment.environment` — Collector enriches the rest |
| Fail-fast validation | Invalid config → error at startup, not silent degradation |
| Cross-language compat | Same env vars, same Collector policies as .NET/Python |
