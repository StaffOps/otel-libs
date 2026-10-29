<!-- Generated from the root README.md (links rewritten for MkDocs). Edit README.md, then regenerate. -->

# OpenTelemetry Helper Libraries

Standardized OpenTelemetry instrumentation for Go, Python and .NET services:
one call sets up traces, metrics and logs with the same configuration
contract and defaults in every language; emitted telemetry differs where noted
([known gaps](#known-emitted-telemetry-gaps)).

**Audience:** application engineers adding telemetry to a service, and platform
engineers who run the Collector it exports to.

## Libraries

| Language | Directory | Package | Version | Contract |
|----------|-----------|---------|---------|----------|
| Go | [`go/`](go/README.md) | `github.com/staffops/otel-libs/go` + `ext/*` modules | `0.3.0` (unreleased, this branch); latest release `go/v0.2.1` | 0.3 |
| Python | [`python/`](python/README.md) | `staffops-otel-helper` (extras `aws`, `redis`, `sql`) | `0.3.0` (unreleased, this branch); latest release `0.2.0` | 0.3 |
| .NET | [`dotnet/`](dotnet/README.md) | `OtelHelper` + `.AWS` / `.Redis` / `.Sql` / `.Profiling` (NuGet) | `0.2.0` | 0.2.x — the 0.3 contract lands in Phase C |
| Node.js | `nodejs/` (planned) | — | — | 0.3 from day one (Phase B) |

The rollout order is Go + Python, then Node.js, then .NET
([ADR-004](https://github.com/StaffOps/otel-libs/blob/main/specs/decisions/ADR-004-phased-rollout-go-python-first.md)). Until a
language catches up, its differences are tracked as `xfail` cases in the
[parity fixture](#cross-language-parity). Upgrading from 0.2? Read the
[migration guide](CHANGELOG.md#unreleased) first.

## Architecture

```
[ Application (Go / Python / .NET) ]
        │ OTLP (gRPC :4317 or http/protobuf :4318)
        ▼
[ OpenTelemetry Collector ]  ← tail sampling, k8s attributes, routing
        │
┌───────┴───────┬───────────────┐
│ Traces        │ Metrics       │ Logs
│ (Tempo)       │ (VictoriaM.)  │ (Loki)
└───────────────┴───────────────┘

No OTLP endpoint → metrics on a Prometheus /metrics endpoint (:9464),
logs as JSON on stdout, traces not exported.
```

## Principles

1. **OpenTelemetry as the single standard** — no vendor SDKs; only official
   OTel and OTel contrib packages.
2. **Everything via the Collector** — the SDK exports OTLP to the Collector,
   never directly to backends. One exception: with no OTLP endpoint, or when
   `OTEL_METRICS_EXPORTER` includes `prometheus`, metrics are served on
   `/metrics` (port 9464, `OTEL_HELPER_METRICS_PORT`)
   ([ADR-007](https://github.com/StaffOps/otel-libs/blob/main/specs/decisions/ADR-007-keep-prometheus-in-core.md)).
3. **Sampling at the Collector** — tail sampling at the gateway. The SDK
   default is `parentbased_always_on`
   ([ADR-002](https://github.com/StaffOps/otel-libs/blob/main/specs/decisions/ADR-002-parentbased-sampler.md); .NET 0.2.x uses
   a bare AlwaysOn sampler).
4. **Resource attributes at the Collector** — the helper sets only
   `service.name`, `deployment.environment.name` (never the legacy
   `deployment.environment`) and, in Go/Python 0.3, a per-process
   `service.instance.id` when none is configured. `k8sattributes` adds the rest.
5. **TLS by default** — `https://` and schemeless endpoints use TLS (system CA).
   The scheme decides; `OTEL_EXPORTER_OTLP_INSECURE=true` only turns a
   schemeless endpoint into plaintext.
6. **Standard OTel variables win** — explicit code > standard `OTEL_*` env >
   legacy / `OTEL_HELPER_*` env > library default. The helper never invents a
   variable the OTel spec already defines.
7. **Don't re-implement the SDK** — where the SDK honors a variable per spec,
   the helper does not override it
   ([ADR-001](https://github.com/StaffOps/otel-libs/blob/main/specs/decisions/ADR-001-defer-otlp-env-to-sdk.md)).
8. **Failures are loud, env never crashes the app** — an invalid env value
   warns (naming the variable, never printing secrets) and falls back to the
   default; an invalid value passed in code fails setup. Telemetry is never
   dropped silently.

## Quick start

### Go

```go
import otelhelper "github.com/staffops/otel-libs/go"

ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, os.Interrupt)
defer stop()

shutdown, err := otelhelper.Setup(ctx)
if err != nil {
    return err
}
defer func() {
    flushCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
    defer cancel()
    _ = shutdown(flushCtx)
}()
```

### Python

```python
import staffops_otel_helper  # before any opentelemetry.* import
from staffops_otel_helper import setup_telemetry, shutdown_telemetry

setup_telemetry()
# ... on SIGTERM or in the server's shutdown hook:
shutdown_telemetry()
```

### .NET (0.2.x)

```csharp
builder.Services.AddOtelHelper();
```

Then configure the service from the environment:

```bash
OTEL_SERVICE_NAME=orders-api
OTEL_RESOURCE_ATTRIBUTES=deployment.environment.name=DEV
OTEL_EXPORTER_OTLP_ENDPOINT=https://otel-collector.example.com:4317
```

Per-language guides (custom spans, metrics and logs; framework integration;
shutdown):

| | Overview | HOW-TO | Examples |
|---|---|---|---|
| Go | [README](go/README.md) | [HOW-TO](go/HOW-TO.md) | [examples](go/example/README.md) |
| Python | [README](python/README.md) | [HOW-TO](python/HOW-TO.md) | [examples](python/example/README.md) |
| .NET | [README](dotnet/README.md) | [HOW-TO](dotnet/HOW-TO.md) | [examples](dotnet/example/README.md) |

## Environment variables (0.3 contract — Go and Python)

Precedence for every setting: **explicit code > standard `OTEL_*` env >
legacy / `OTEL_HELPER_*` env > library default**. Per-signal variables fall
back per-signal → generic → default. The full resolution table and edge-case
rulings are in [design D1](https://github.com/StaffOps/otel-libs/blob/main/specs/config-contract-v0.3/design.md).

### Identity and resource

| Variable | Default | Behavior |
|----------|---------|----------|
| `OTEL_SERVICE_NAME` | _(unset)_ → SDK `unknown_service:<exe>` + warning | Service name. Beats `OTEL_RESOURCE_ATTRIBUTES` and `SERVICE_NAME` |
| `OTEL_RESOURCE_ATTRIBUTES` | _(unset)_ | Set `deployment.environment.name=<env>` here; beats `ENVIRONMENT` and is never overwritten by a helper value. The resolved environment sets the log level: `LOCAL` DEBUG, `DEV`/`HML` INFO, `PRD` WARN; unset or any other value (emitted verbatim) INFO |
| `SERVICE_NAME` | — | **Deprecated** (warned once; removed in 1.0) → `OTEL_SERVICE_NAME` |
| `ENVIRONMENT` | — | **Deprecated alias** → `deployment.environment.name`. With neither set, the attribute is absent and one warning is logged — never `LOCAL` ([ADR-003](https://github.com/StaffOps/otel-libs/blob/main/specs/decisions/ADR-003-environment-unset-not-local.md)) |

### OTLP export

| Variable | Default | Behavior |
|----------|---------|----------|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | _(unset)_ | Collector endpoint. Unset (and no per-signal endpoint) → that signal is not exported over OTLP: metrics fall back to Prometheus `/metrics`, logs to stdout JSON. HTTP: `/v1/<signal>` appended, path prefix kept, no port → 443/80. gRPC URL without a port → `:4317` + warning. Schemeless `host:port` → `https://` (deprecated for `http/protobuf`) |
| `OTEL_EXPORTER_OTLP_<SIGNAL>_ENDPOINT` | _(unset)_ | Used verbatim for `TRACES`, `METRICS`, `LOGS`; alone, it configures that signal |
| `OTEL_EXPORTER_OTLP[_<SIGNAL>]_PROTOCOL` | port inference | `grpc` or `http/protobuf`. Unset → `http/protobuf` when the resolved endpoint's port is `4318`, else `grpc`. `http/json` from env warns and is ignored |
| `OTEL_EXPORTER_OTLP[_<SIGNAL>]_INSECURE` | _(unset)_ | Only for schemeless endpoints: `true` → plaintext. Contradicting an explicit scheme → the scheme wins, warned. Values other than `true` are false (warned if not `false`) |
| `OTEL_EXPORTER_OTLP[_<SIGNAL>]_HEADERS` | _(unset)_ | `key=value,…`; keys `[A-Za-z0-9._-]+` (lowercased, last wins), values percent-decoded; malformed items skipped and warned by position. Put credentials here, not in the URL (userinfo/query are stripped) |
| `OTEL_EXPORTER_OTLP[_<SIGNAL>]_TIMEOUT` | `10000` | Milliseconds, plain decimal, clamped to [1 ms, 24 h] |
| `OTEL_EXPORTER_OTLP[_<SIGNAL>]_COMPRESSION` | `gzip` | `gzip` or `none` (case-insensitive) |
| `OTEL_EXPORTER_OTLP[_<SIGNAL>]_CERTIFICATE`, `_CLIENT_CERTIFICATE`, `_CLIENT_KEY` | _(unset)_ | Custom CA / mTLS (SDK). Ignored with a warning on a plaintext endpoint |

### Signals, sampling and propagation

| Variable | Default | Behavior |
|----------|---------|----------|
| `OTEL_SDK_DISABLED` | `false` | `true` → no providers, exporters, `/metrics` listener or log handlers; propagators still installed |
| `OTEL_TRACES_EXPORTER`, `OTEL_LOGS_EXPORTER` | `otlp` when configured | `otlp`, `console`, `none` (or a list); `logging` = deprecated `console` |
| `OTEL_METRICS_EXPORTER` | OTLP if an endpoint is configured, else `prometheus` | `otlp`, `prometheus`, `otlp,prometheus`, `console`, `none`. Env `otlp` without an endpoint warns and falls back |
| `OTEL_METRIC_EXPORT_INTERVAL` | `30000` | Milliseconds (helper default 30 s; SDK default 60 s) |
| `OTEL_TRACES_SAMPLER` / `OTEL_TRACES_SAMPLER_ARG` | `parentbased_always_on` | The six spec samplers (case-insensitive). Unknown names warn and use the default |
| `OTEL_PROPAGATORS` | `tracecontext,baggage` | Supported: `tracecontext`, `baggage`, `none`. Others (`b3`, `jaeger`, `xray`, …) are warned and skipped |
| `OTEL_SEMCONV_STABILITY_OPT_IN` | Python: `http,database/dup` | Python sets this default before instrumenting (stable HTTP semconv, both DB keys); a user value wins. Go instrumentations emit stable HTTP semconv natively |

### Helper-specific

| Variable | Default | Behavior |
|----------|---------|----------|
| `OTEL_HELPER_DEBUG_LEVEL` | `false` | Debug log level and `debug=true` on entry spans (no parent or a remote parent) for Collector tail-sampling policies ([ADR-006](https://github.com/StaffOps/otel-libs/blob/main/specs/decisions/ADR-006-debug-span-attribute.md)); the Collector policy needs an environment allow-list and a rate limit ([prerequisites](#collector-and-platform-prerequisites)). Python debug mode also turns on every installed `ext` instrumentor (SQL, Redis, AWS). Do not leave on in production |
| `OTEL_HELPER_METRICS_PORT` | `9464` | Standalone Prometheus `/metrics` listener port; `0` disables the listener (the mountable handler keeps working) |
| `OTEL_HELPER_DISABLED_METRICS` | _(unset)_ | Wildcard patterns of metric names to drop (all exporters) |
| `OTEL_HELPER_SAMPLE_RATIO` | `1.0` | **Deprecated** → `OTEL_TRACES_SAMPLER=parentbased_traceidratio` + `OTEL_TRACES_SAMPLER_ARG`. Used only while `OTEL_TRACES_SAMPLER` is unset; builds `ParentBased(TraceIdRatioBased(x))` |
| `OTEL_HELPER_DISABLED_SIGNALS` | _(unset)_ | **Deprecated** → `OTEL_{TRACES,METRICS,LOGS}_EXPORTER=none` |
| `OTEL_HELPER_EXTRA_INSTRUMENTATION` | Python: `SQL` | **Go: removed** (no effect, warns). **Python: deprecated** — call `staffops_otel_helper.ext.instrument_sql()` / `instrument_redis()` / `instrument_aws()` before `setup_telemetry()` |

Health checks are configured in code, not env: by default server spans for
`/health /healthz /ready /readyz /live /livez /ping /metrics` (exact,
case-insensitive) and the `grpc.health.v1.Health` service are not traced, but
they **are counted** in the HTTP server metrics (and, in Go,
`rpc.server.call.duration`) — exclude them in SLI queries. Override with
`WithHealthPaths(...)` (Go) or `TelemetryOptions(health_paths=[...])` (Python).

### .NET 0.2.x differences

.NET still implements the 0.2 contract: `SERVICE_NAME` beats `OTEL_SERVICE_NAME`
and defaults to `my-service`; `ENVIRONMENT` defaults to `LOCAL` and unknown
values become `LOCAL`; per-signal OTLP variables, `OTEL_PROPAGATORS`,
`OTEL_EXPORTER_OTLP_TIMEOUT` and `OTEL_{TRACES,LOGS}_EXPORTER` are not honored;
`http/json` fails startup validation; the sampler is a bare AlwaysOn; the health
filter is `/ping /health /healthz /ready` (traces only; metrics still count
those requests). `OTEL_HELPER_EXTRA_INSTRUMENTATION` has no effect. See
[`dotnet/README.md`](dotnet/README.md) and the Phase C backlog in
[`specs/config-contract-v0.3/tasks.md`](https://github.com/StaffOps/otel-libs/blob/main/specs/config-contract-v0.3/tasks.md).

## Opt-in extensions

AWS, Redis and SQL instrumentations are separate packages; the core stays
lightweight.

| Language | AWS | Redis | SQL |
|----------|-----|-------|-----|
| Go | `ext/otelaws` | `ext/otelredis` | `ext/otelsql` |
| Python | `staffops-otel-helper[aws]` | `staffops-otel-helper[redis]` | `staffops-otel-helper[sql]` |
| .NET | `OtelHelper.AWS` | `OtelHelper.Redis` | `OtelHelper.Sql` |

## Cross-language parity

Parity is enforced, not promised. [`parity/cases.json`](https://github.com/StaffOps/otel-libs/blob/main/parity/cases.json) is a
hand-maintained, language-neutral fixture (204 cases: 187 after A13, 17 added by the Node B3 review)
that every language's test suite runs — config resolution, wire-level export
against a fake OTLP receiver, sampling, propagation, health exclusion, logging
and `/metrics` status
([ADR-008](https://github.com/StaffOps/otel-libs/blob/main/specs/decisions/ADR-008-shared-parity-fixture.md)). A language that
lags marks a case `xfail:<reason>`; cases are never deleted to make CI green.
Format and runner details: [`parity/README.md`](https://github.com/StaffOps/otel-libs/blob/main/parity/README.md); the
end-to-end smoke against a real Collector: [`testing/`](https://github.com/StaffOps/otel-libs/blob/main/testing/README.md).

### Known emitted-telemetry gaps

The configuration contract is shared; what the instrumentations emit is not
always identical:

| Area | Gap |
|------|-----|
| Python gRPC | Spans carry the legacy `rpc.system`, `rpc.service`, `rpc.method` and `rpc.grpc.status_code` attributes, and no RPC metrics are emitted (upstream `opentelemetry-instrumentation-grpc`). Go emits the current RPC semconv and `rpc.server.call.duration` / `rpc.client.call.duration` |
| .NET | Still on the 0.2.x contract and instrumentation set until Phase C ([differences](#net-02x-differences)) |

## Collector and platform prerequisites

The libraries assume a Collector and platform configured for them. These items
live outside this repo and block the 0.3.0 rollout; the full checklist, with
the reason for each item, is
[design D11](https://github.com/StaffOps/otel-libs/blob/main/specs/config-contract-v0.3/design.md#d11--collector--platform-prerequisites-reference-checklist):

1. Tail sampling: a default branch for spans without
   `deployment.environment.name`, at the PRD rate.
2. The `debug=true` policy combined (`and`) with an environment allow-list and
   a `rate_limiting` sub-policy. Debug mode marks every entry span in every
   language — Go and Python spans with no parent or a remote parent, .NET spans
   with no in-process parent (which also prepend `debug=true` to the propagated
   tracestate) — so a debug-enabled service marks every inbound request.
3. Gateway `loadbalancingexporter` routed by trace ID; `decision_wait` ≥ the
   observed p99 trace duration, and the sampled-decision cache enabled for late
   spans.
4. Charts set `OTEL_SERVICE_NAME` and `OTEL_RESOURCE_ATTRIBUTES` with
   `deployment.environment.name`; no `service.instance.id` for multi-worker
   containers.
5. Istio / mesh tracing at 100% or disabled (it interacts with the
   ParentBased sampler).
6. VictoriaMetrics OTLP ingest ignores volatile resource attributes
   (`-opentelemetry.ignoreResourceAttributes`, e.g. `process.pid`) and keeps
   `service.instance.id`.
7. A Collector `filterprocessor` dropping health spans, as defense in depth.
8. An explicit `:4317` on portless gRPC URLs.

## Installing (private packages)

Packages require a GitHub token with `read:packages`. .NET uses GitHub Packages
NuGet, Python uses GitHub Release wheel assets, Go uses `go get` from the
repository. See [CONSUMING.md](CONSUMING.md) for auth, install commands and CI
variants.

## Dashboards

Shared Grafana dashboards in [`dashboards/`](https://github.com/StaffOps/otel-libs/tree/main/dashboards/) query the example
apps' business metrics, Collector `spanmetrics` and Tempo. They are language
agnostic.

## More

- [Changelog and migration guide](CHANGELOG.md)
- [Specs and decision records](https://github.com/StaffOps/otel-libs/blob/main/specs/README.md)
- [Contributor and agent guide](https://github.com/StaffOps/otel-libs/blob/main/AGENTS.md)
- [Source repository](https://github.com/StaffOps/otel-libs)

## License

[Apache License 2.0](https://github.com/StaffOps/otel-libs/blob/main/LICENSE) — the same license as OpenTelemetry.
