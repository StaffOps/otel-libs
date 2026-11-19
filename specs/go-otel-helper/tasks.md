# Tasks — Go OTel Helper Implementation Plan

## Status (2026-05-31)

| Phase | Status |
|-------|--------|
| 1. Foundation (module, config, options) | ✅ Done |
| 2. Core Telemetry (tracing, metrics, logging, processors) | ✅ Done |
| 3. Setup & Public API | ✅ Done |
| 4. Middleware (HTTP + gRPC interceptors) | ✅ Done |
| 5. Instrumentation helpers | ✅ Done (`InstrumentSQL` is a placeholder — see pending) |
| 6. Examples (go-api, go-backend, go-process + README) | ✅ Done (build OK; see Dockerfile.demo pending) |
| 7. CI & Docs | ⏳ Partial — docs done, CI pending |

**Lib state**: builds clean, `go vet` clean, **92.1% coverage**, 80 tests passing. 3 examples build.
**Reviewed**: code-review + security + observability passed; critical fixes applied (sync.Mutex, partial-failure cleanup, trace.WithNewRoot, health filter on all interceptors).
**Docs**: README.md, HOW-TO.md, example/README.md done and validated against code.

## Pending (open work)

### P1 — CI pipeline for Go (Task 7.1 / 7.3)
- [ ] Add Go job to CI with `go vet` + `go test -race -coverprofile` + **`-cov` gate ≥90%** (consistent with .NET/Python jobs in `.github/workflows/ci.yml`)
- [ ] Build the 3 examples in CI using `context=go/` (NOT the broken `Dockerfile.demo` — see P2)
- [ ] **DECISION NEEDED** — pipeline structure (esthetics):
  - **(A)** Separate workflow per language (`go.yml`/`dotnet.yml`/`python.yml`) + `paths:` filter — *recommended* for independent libs; a PR touching only `go/` runs only Go CI. Loses single `needs:` gate.
  - **(B)** Reusable workflows (`workflow_call`) — DRY + single gate kept; more files.
  - **(C)** Add `go` job to current monolithic `ci.yml` — simplest; every PR runs all 3 langs.

### P2 — Fix broken Go `Dockerfile.demo` (Task 6.x)
- [ ] `go/example/*/Dockerfile.demo` are broken for CI: they `echo 'replace => /lib'` (duplicates the `replace => ../../` already in go.mod) and use `COPY --from=lib` (build-context not declared).
- [ ] Confirmed fix: build with `context=go/` so the relative `replace ../../` resolves — no special demo Dockerfile needed. Either delete `Dockerfile.demo` or rewrite to a no-replace variant.

### P3 — InstrumentSQL placeholder (Task 5.1)
- [ ] `InstrumentSQL` currently returns the DB unchanged (TODO). Integrate `otelsql` (or document as intentionally manual) when a consumer needs SQL tracing.

### P4 — Version / status bump (deferred)
- [ ] Root `README.md` shows Go as "🚧 In Development". Bump to a real version + "✅ Production" only after the lib runs in a real service with measurable result (per `version-management` steering). Not now.

### P5 — Commits (needs user approval)
- [ ] Nothing committed yet. All Go lib + specs + docs + steering changes are uncommitted. Prepare per `git-conventions` (conventional commits, no push to main without approval).

---

## Phase 1: Foundation

### Task 1.1: Module Init & Dependencies
**Depends on:** nothing
**Covers:** US-16 (module structure)

- [ ] Create `go/` directory
- [ ] `go mod init github.com/staffops/staffops-otel-libs/go`
- [ ] Add OTel SDK dependencies (trace, metric, log)
- [ ] Add OTLP gRPC exporters
- [ ] Add `otelhttp` and `otelgrpc` contrib packages
- [ ] Add `otelslog` bridge package
- [ ] Add `google.golang.org/grpc`
- [ ] Run `go mod tidy` via Docker
- [ ] Verify: `docker run --rm -v $(pwd)/go:/src -w /src golang:1.22-alpine go build ./...`

### Task 1.2: Config & Options
**Depends on:** 1.1
**Covers:** US-2 (configuration), US-12 (conditional instrumentations)

- [ ] Define `DeploymentEnvironment` type (iota enum: LOCAL, DEV, HML, PRD)
- [ ] Define `Options` struct with all fields and defaults
- [ ] Implement functional options: `WithServiceName`, `WithEnvironment`, `WithDebug`, `WithEndpoint`, `WithSampleRatio`, `WithExtraInstrumentation`, `WithResourceAttributes`, `WithExportTimeout`
- [ ] Implement `resolveFromEnv()` — same logic as Python's `resolve_from_env()`
- [ ] Implement `validate() error` (fail-fast: empty service name, invalid endpoint, timeout ≤ 0)
- [ ] Implement `HasInstrumentation(name string) bool` (case-insensitive, debug enables all)
- [ ] Implement `parseEnvironment(s string) DeploymentEnvironment`

### Task 1.3: Config Tests
**Depends on:** 1.2
**Covers:** US-17 (testing)

- [ ] Test defaults match spec (ServiceName="my-service", Environment=LOCAL, SampleRatio=1.0, etc.)
- [ ] Test env var resolution for each field
- [ ] Test priority: explicit option > env var
- [ ] Test `SERVICE_NAME` takes priority over `OTEL_SERVICE_NAME`
- [ ] Test `ENVIRONMENT` parsing (case-insensitive)
- [ ] Test invalid environment falls back to LOCAL
- [ ] Test `OTEL_HELPER_SAMPLE_RATIO` clamping (0.0–1.0)
- [ ] Test validation errors: empty service name, invalid endpoint, zero timeout
- [ ] Test `HasInstrumentation` case-insensitive, debug enables all
- [ ] Verify: `go test ./... -run TestOptions`

---

## Phase 2: Core Telemetry

### Task 2.1: Tracing Setup
**Depends on:** 1.2
**Covers:** US-6 (sampling with ParentBased), US-13 (OTLP gRPC export)

- [ ] Implement `configureTracing(ctx, resource, options) (*sdktrace.TracerProvider, error)`
- [ ] OTLP gRPC exporter with insecure + timeout + gzip compression
- [ ] Sampler: `ParentBased(root=AlwaysSample())` if ratio ≥ 1.0, else `ParentBased(root=TraceIDRatioBased(ratio))`
- [ ] BatchSpanProcessor
- [ ] Set global TracerProvider via `otel.SetTracerProvider()`

### Task 2.2: Debug Processor
**Depends on:** 2.1
**Covers:** US-7 (debug mode)

- [ ] Implement `debugProcessor` satisfying `sdktrace.SpanProcessor`
- [ ] `OnStart`: set `debug=true` attribute if `!s.Parent().IsValid()`
- [ ] No-op for `OnEnd`, `Shutdown`, `ForceFlush`
- [ ] Conditionally add to provider when `opts.DebugLevel == true`

### Task 2.3: StartRootSpan
**Depends on:** 2.1
**Covers:** US-9 (StartRootSpan for workers)

- [ ] Implement `StartRootSpan(ctx, tracer, name, ...opts) (context.Context, trace.Span)`
- [ ] Detach from parent by creating clean context with no valid span
- [ ] Preserve non-span context values (deadlines, cancellation)
- [ ] Return new ctx + span (caller calls `span.End()`)

### Task 2.4: Metrics Setup
**Depends on:** 1.2
**Covers:** US-10 (metrics with exemplars), US-13 (OTLP gRPC export)

- [ ] Implement `configureMetrics(ctx, resource, options) (*sdkmetric.MeterProvider, error)`
- [ ] OTLP gRPC metric exporter with gzip compression
- [ ] PeriodicReader with default interval
- [ ] `TraceBasedExemplarFilter` for trace-to-metric linking
- [ ] Set global MeterProvider

### Task 2.5: Logging Setup
**Depends on:** 1.2
**Covers:** US-11 (log correlation via slog), US-13 (OTLP gRPC export)

- [ ] Implement `configureLogging(ctx, resource, options) (*sdklog.LoggerProvider, error)`
- [ ] OTLP gRPC log exporter
- [ ] BatchProcessor
- [ ] Set global LoggerProvider
- [ ] Implement `NewSlogHandler() slog.Handler` — bridges slog to OTel logs with trace_id/span_id correlation

### Task 2.6: Tracing Tests
**Depends on:** 2.1, 2.2, 2.3
**Covers:** US-17 (testing)

- [ ] Test StartRootSpan creates span with no parent (new trace ID)
- [ ] Test StartRootSpan inside existing span still creates independent trace
- [ ] Test DebugProcessor sets `debug=true` on root spans only
- [ ] Test DebugProcessor does NOT set attribute on child spans
- [ ] Test ParentBased sampler: child inherits parent sampling decision
- [ ] Test sampler selection (ratio < 1 → ParentBased(TraceIDRatioBased))
- [ ] Use `tracetest.InMemoryExporter` for assertions

---

## Phase 3: Setup & Public API

### Task 3.1: Setup Function
**Depends on:** 2.1, 2.4, 2.5
**Covers:** US-1 (single-call setup), US-8 (W3C propagation), US-14 (resource attributes), US-15 (graceful shutdown)

- [ ] Implement `Setup(ctx context.Context, opts ...Option) (Shutdown, error)`
- [ ] `sync.Mutex` guard for double-init (returns `noopShutdown` on error, allows retry)
- [ ] Build resource with `service.name` + `deployment.environment` + custom attributes
- [ ] Call configureTracing, configureMetrics, configureLogging
- [ ] Call `configurePropagators()` (W3C TraceContext + Baggage)
- [ ] Return composite `Shutdown` function (errors.Join for all providers)
- [ ] Return error on validation failure (before any provider is created)

### Task 3.2: Accessors
**Depends on:** 3.1
**Covers:** US-3 (GetTracer/GetMeter)

- [ ] `GetTracer(name ...string) trace.Tracer` — from global provider, default name `"otel-helper"`
- [ ] `GetMeter(name ...string) metric.Meter` — from global provider, default name `"otel-helper"`

### Task 3.3: Setup Tests
**Depends on:** 3.1, 3.2
**Covers:** US-17 (testing)

- [ ] Test Setup returns non-nil Shutdown on valid config
- [ ] Test Setup returns error on invalid config (empty service name)
- [ ] Test double-call returns same shutdown (no error)
- [ ] Test Shutdown calls provider shutdowns (no goroutine leaks)
- [ ] Test GetTracer/GetMeter return non-nil after Setup
- [ ] Test env-only setup (no options, env vars set)

---

## Phase 4: Middleware

### Task 4.1: HTTP Middleware
**Depends on:** 3.1
**Covers:** US-4 (distributed traces HTTP), US-5 (health path filtering)

- [ ] Implement `NewHTTPHandler(handler http.Handler, operation string) http.Handler`
- [ ] Health path filter: `/ping`, `/health`, `/healthz`, `/ready` — no spans created
- [ ] Wraps `otelhttp.NewHandler` with filter option
- [ ] Span attributes: `http.method`, `http.route`, `http.status_code`

### Task 4.2: gRPC Interceptors
**Depends on:** 3.1
**Covers:** US-4 (distributed traces gRPC), US-5 (health path filtering)

- [ ] `UnaryServerInterceptor() grpc.UnaryServerInterceptor`
- [ ] `StreamServerInterceptor() grpc.StreamServerInterceptor`
- [ ] `UnaryClientInterceptor() grpc.UnaryClientInterceptor`
- [ ] `StreamClientInterceptor() grpc.StreamClientInterceptor`
- [ ] Health filter: `/grpc.health.v1.Health/Check` excluded on server interceptors
- [ ] Span attributes: `rpc.system`, `rpc.service`, `rpc.method`

### Task 4.3: Middleware Tests
**Depends on:** 4.1, 4.2
**Covers:** US-17 (testing)

- [ ] Test HTTP handler creates span for `/api/data`
- [ ] Test HTTP handler does NOT create span for `/health`
- [ ] Test gRPC interceptor creates span for service method
- [ ] Test gRPC health check is filtered
- [ ] Use `httptest.NewServer` + in-memory exporter

---

## Phase 5: Instrumentation Helpers

### Task 5.1: Conditional Instrumentation
**Depends on:** 1.2
**Covers:** US-12 (conditional instrumentations)

- [ ] `InstrumentSQL(db *sql.DB) *sql.DB` — wraps with otelsql (if SQL enabled)
- [ ] `InstrumentAWS()` — placeholder/docs for otel AWS SDK instrumentation
- [ ] `InstrumentRedis()` — placeholder/docs for redis instrumentation
- [ ] Each checks `HasInstrumentation()` and returns input unchanged if disabled

---

## Phase 6: Examples

### Task 6.1: go-api Example
**Depends on:** 4.1
**Covers:** US-18 (examples)

- [ ] `main.go`: net/http server with `NewHTTPHandler` middleware
- [ ] Endpoints: `POST /notify`, `GET /health`, `GET /metrics/summary`
- [ ] Manual spans for business logic
- [ ] Metrics: request counter, latency histogram (with exemplars)
- [ ] `Dockerfile` + `Dockerfile.demo`

### Task 6.2: go-backend Example
**Depends on:** 4.2
**Covers:** US-18 (examples)

- [ ] `main.go`: gRPC server with interceptors
- [ ] Service: `AnomalyService` with `Detect` and `GetStatus` RPCs
- [ ] Manual spans inside handlers
- [ ] `example/protos/anomaly.proto`
- [ ] `Dockerfile` + `Dockerfile.demo`

### Task 6.3: go-process Example
**Depends on:** 2.3
**Covers:** US-18 (examples), US-9 (StartRootSpan), US-15 (graceful shutdown)

- [ ] `main.go`: multiple goroutine workers (mirrors anomaly-detection-controller)
- [ ] Workers: `anomalyScanner` (30s), `alertDispatcher` (60s), `cleanupWorker` (3m)
- [ ] Each iteration uses `StartRootSpan` → independent trace
- [ ] Metrics: up-down counter for queue depth, counter for processed items
- [ ] Graceful shutdown via `signal.NotifyContext` (SIGTERM/SIGINT)
- [ ] Health endpoint on `:8080`
- [ ] `Dockerfile` + `Dockerfile.demo`

### Task 6.4: Example README
**Depends on:** 6.1, 6.2, 6.3

- [ ] Architecture diagram (process → api → backend)
- [ ] Build commands (Docker)
- [ ] Run commands (docker network)
- [ ] Test curl commands
- [ ] Expected trace structure

---

## Phase 7: CI & Documentation

### Task 7.1: Dockerfile.test
**Depends on:** Phase 3 complete
**Covers:** US-19 (CI pipeline)

```dockerfile
FROM golang:1.22-alpine
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN go test ./... -coverprofile=coverage.out -covermode=atomic
RUN go build ./...
```

### Task 7.2: .dockerignore
**Depends on:** 6.1

- [ ] Exclude: `.git`, `example/`, `*.md`, `coverage.out`

### Task 7.3: CI Pipeline Definition
**Depends on:** 7.1
**Covers:** US-19 (CI pipeline)

Stages:
1. **unit-test**: `go test ./... -coverprofile=coverage.out` via Docker
2. **build-dev**: `go build ./...` — compilation check
3. **demo**: Build `Dockerfile.demo` for each example, verify health endpoint responds

### Task 7.4: README.md
**Depends on:** all above

- [ ] Quick start (3-line setup)
- [ ] Configuration table (env vars — same 6 as Python/dotnet)
- [ ] API reference (Setup, GetTracer, GetMeter, StartRootSpan, middleware)
- [ ] Architecture diagram
- [ ] Testing instructions (Docker-based)

### Task 7.5: HOW-TO.md
**Depends on:** 7.4

- [ ] HTTP API setup recipe
- [ ] gRPC server setup recipe
- [ ] Background worker setup recipe
- [ ] Custom spans and metrics
- [ ] Distributed tracing across services
- [ ] Debug mode usage
- [ ] slog integration

### Task 7.6: Update Root README
**Depends on:** 7.4

- [ ] Add Go row to the libs table
- [ ] Add Go quick start section

---

## Dependency Graph

```
1.1 ─→ 1.2 ─→ 1.3
         │
         ├─→ 2.1 ─→ 2.2 ─→ 2.6
         │    │       │
         │    │       └─→ 2.3 ─→ 2.6
         │    │
         │    └─────────────────→ 3.1 ─→ 3.2 ─→ 3.3
         │                         │
         ├─→ 2.4 ──────────────────┘
         │                         │
         ├─→ 2.5 ──────────────────┘
         │                         │
         │                         ├─→ 4.1 ─→ 4.3 ─→ 6.1
         │                         │
         │                         ├─→ 4.2 ─→ 4.3 ─→ 6.2
         │                         │
         └─→ 5.1                   └─→ 6.3
                                        │
                                        └─→ 6.4 ─→ 7.1 ─→ 7.3
                                                    │
                                                    └─→ 7.4 ─→ 7.5 ─→ 7.6
```

---

## Requirements Coverage Matrix

| Requirement | Task(s) |
|-------------|---------|
| US-1: Single-call Setup | 3.1 |
| US-2: Configuration | 1.2, 1.3 |
| US-3: Accessors | 3.2 |
| US-4: Distributed Traces | 4.1, 4.2 |
| US-5: Health Filtering | 4.1, 4.2 |
| US-6: Sampling (ParentBased) | 2.1 |
| US-7: Debug Mode | 2.2 |
| US-8: W3C Propagation | 3.1 |
| US-9: StartRootSpan | 2.3 |
| US-10: Metrics + Exemplars | 2.4 |
| US-11: Log Correlation (slog) | 2.5 |
| US-12: Conditional Instr. | 1.2, 5.1 |
| US-13: OTLP gRPC Export | 2.1, 2.4, 2.5 |
| US-14: Resource Attributes | 3.1 |
| US-15: Graceful Shutdown | 3.1, 6.3 |
| US-16: Module Structure | 1.1 |
| US-17: Testing | 1.3, 2.6, 3.3, 4.3 |
| US-18: Examples | 6.1, 6.2, 6.3, 6.4 |
| US-19: CI Pipeline | 7.1, 7.3 |

---

## Estimated Effort

| Phase | Tasks | Estimate |
|-------|-------|----------|
| 1. Foundation | 3 | 2h |
| 2. Core Telemetry | 6 | 4h |
| 3. Setup & API | 3 | 2h |
| 4. Middleware | 3 | 2h |
| 5. Instrumentation | 1 | 1h |
| 6. Examples | 4 | 4h |
| 7. CI & Docs | 6 | 3h |
| **Total** | **26** | **~18h** |

---

## Build & Test Commands (all via Docker)

```bash
# Run tests
docker run --rm -v $(pwd)/go:/src -w /src golang:1.22-alpine go test ./... -v

# Run tests with coverage
docker run --rm -v $(pwd)/go:/src -w /src golang:1.22-alpine go test ./... -coverprofile=coverage.out

# Build check
docker run --rm -v $(pwd)/go:/src -w /src golang:1.22-alpine go build ./...

# Lint
docker run --rm -v $(pwd)/go:/src -w /src golangci/golangci-lint:v1.59-alpine golangci-lint run

# Build example
docker build -t go-process -f go/example/go-process/Dockerfile go/
```

---

## Post-Spec Additions (implemented, not in original plan)

These features were added during implementation and are now part of the codebase:

### Runtime Metrics
- `go.opentelemetry.io/contrib/instrumentation/runtime` started in `configureMetrics()`
- Automatically collects goroutines, GC, memory stats

### NewHTTPTransport (HTTP Client Instrumentation)
- `NewHTTPTransport(base http.RoundTripper) http.RoundTripper`
- Wraps outgoing HTTP requests with OTel tracing + health path filter
- Used in go-api example for backend calls

### Logging Helpers
- `DefaultLogLevel(env, debug) slog.Level` — returns appropriate level per environment
- `NewLogger(env, debug) *slog.Logger` — full logger with OTel bridge + level filter
- `NewSlogHandler() slog.Handler` — raw otelslog bridge handler

### OTEL_METRICS_EXEMPLAR_FILTER Auto-Set
- `configureMetrics()` sets `OTEL_METRICS_EXEMPLAR_FILTER=trace_based` via env var
- Programmatic API (`sdkmetric.WithExemplarFilter`) not exported in SDK v1.31.0

### gRPC Health Filter on All Interceptors
- Health filter (`/grpc.health.v1.Health/Check`) applied to all 4 interceptors (unary/stream × server/client), not just UnaryServerInterceptor

### sync.Mutex Instead of sync.Once
- `Setup()` uses `sync.Mutex` + `setupDone` flag instead of `sync.Once`
- Allows retry on validation failure (sync.Once would permanently block)
- Returns `noopShutdown` on error so callers always get a safe function
- Cleans up already-created providers on partial failure

### StartRootSpan Uses trace.WithNewRoot()
- Uses `trace.WithNewRoot()` span option instead of context cleaning
- Simpler and more idiomatic; SDK handles parent detachment internally
