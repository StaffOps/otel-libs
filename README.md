# OpenTelemetry Helper Libraries

Standardized OpenTelemetry instrumentation libraries for your applications.

## Libs

| Language | Directory | Package | Status |
|----------|-----------|---------|--------|
| .NET | [`dotnet/`](dotnet/) | `OtelHelper` (NuGet) | ✅ Production |
| Python | [`python/`](python/) | `otel-helper` (PyPI) | ✅ Production |
| Go | [`go/`](go/) | `otelhelper` (Go module) | 🚧 In Development |

## Dashboards

Shared Grafana dashboards in [`dashboards/`](dashboards/) — compatible with any language.

## Architecture

```
[ Application (.NET / Python / Go) ]
        ↓ OTLP gRPC :4317
[ OpenTelemetry Collector ]
        ↓
┌──────────┬──────────┬──────────┐
│ Traces   │ Metrics  │ Logs     │
│ (Tempo)  │ (VM)     │ (Loki)   │
└──────────┴──────────┴──────────┘
```

## Principles

- **OpenTelemetry as the single standard** — no vendor SDKs
- **Everything via Collector** — SDK does not export directly to backends
- **Prometheus fallback** — when no OTLP endpoint is configured, metrics are exposed via `/metrics` on port 9464 (Prometheus scrape compatible). `OTEL_METRICS_EXPORTER` (`otlp`, `prometheus`, `otlp,prometheus`, `none`) can run OTLP push and `/metrics` simultaneously — see each language's HOW-TO
- **Standard OTel env vars win over `OTEL_HELPER_*` ones** — never invent a proprietary knob when the spec defines one (e.g. `OTEL_METRIC_EXPORT_INTERVAL`, `OTEL_TRACES_SAMPLER`)
- **Sampling at the Collector** — SDK uses AlwaysOn, tail sampling at the gateway
- **Resource attributes at the Collector** — SDK only sets `service.name` and `deployment.environment.name`, identically across all three languages
- **TLS by default** — `https://` or schemeless endpoints use TLS (system CA); use `http://` or `OTEL_EXPORTER_OTLP_INSECURE=true` for a plaintext local collector
- **Metrics exported every 30s** — default interval for all languages (SDK default is 60s)

## Opt-in Extensions

AWS, Redis, and SQL instrumentations are available as **opt-in packages** in all three languages. Core packages remain lightweight — add only what you need.

| Language | AWS | Redis | SQL |
|----------|-----|-------|-----|
| .NET | `OtelHelper.AWS` | `OtelHelper.Redis` | `OtelHelper.Sql` |
| Python | `otel-helper[aws]` | `otel-helper[redis]` | `otel-helper[sql]` |
| Go | `ext/otelaws` | `ext/otelredis` | `ext/otelsql` |

See each language's README for usage details.

## Quick Start

### .NET
```csharp
services.AddOtelHelper();
```

### Python
```python
from otel_helper import setup_telemetry
setup_telemetry()
```

### Go
```go
import otelhelper "github.com/staffops/staffops-otel-libs/go"

shutdown, err := otelhelper.Setup(ctx)
defer shutdown(ctx)
```

## Environment Variables (all libs)

| Variable | Default | Description |
|----------|---------|-------------|
| `SERVICE_NAME` | `my-service` | Service name |
| `ENVIRONMENT` | `LOCAL` | Environment: LOCAL, DEV, HML, PRD |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://localhost` | Collector endpoint |
| `OTEL_EXPORTER_OTLP_INSECURE` | _(unset)_ | TLS override: `true` = plaintext, `false` = TLS. Unset = derived from scheme (secure by default) |
| `OTEL_HELPER_DEBUG_LEVEL` | `false` | Debug mode (DEBUG log, all instrumentations, attribute debug=true) |
| `OTEL_HELPER_EXTRA_INSTRUMENTATION` | `SQL` | Conditional instrumentations: SQL, AWS, REDIS |
| `OTEL_HELPER_SAMPLE_RATIO` | `1.0` | Head sampling ratio (0.0-1.0). 1.0 = AlwaysOn. Ignored if the standard `OTEL_TRACES_SAMPLER` is set |
| `OTEL_HELPER_METRICS_PORT` | `9464` | Standalone Prometheus `/metrics` listener port (`0` disables it) |
| `OTEL_METRICS_EXPORTER` | legacy inference | Metric exporter(s): `otlp`, `prometheus`, `otlp,prometheus`, `none`. Unset = OTLP if endpoint set, else Prometheus fallback |
| `OTEL_METRIC_EXPORT_INTERVAL` | `30000` | OTLP metric export interval in ms (standard OTel var; helper default is 30s, not the SDK's 60s) |
| `OTEL_TRACES_SAMPLER` | _(unset)_ | Standard OTel sampler config — takes priority over `OTEL_HELPER_SAMPLE_RATIO` when set |

**Precedence rule:** explicit code config > standard OTel env var (`OTEL_METRICS_EXPORTER`, `OTEL_METRIC_EXPORT_INTERVAL`, `OTEL_TRACES_SAMPLER`) > `OTEL_HELPER_*` env var > library default. `OTEL_HELPER_*` vars keep working when the standard var is absent — they're convenience defaults, not a replacement for the spec.

## Installing from GitHub Packages (private)

All packages are private and require a GitHub token with `read:packages` scope. .NET uses GitHub Packages NuGet, Python uses GitHub Release wheel assets, Go uses `go get` directly from the repo.

See [CONSUMING.md](CONSUMING.md) for full, validated per-language instructions (auth setup, install commands, CI variants, and gotchas).

## Documentation

- [.NET — README](dotnet/README.md)
- [.NET — HOW-TO](dotnet/HOW-TO.md)
- [Python — README](python/README.md)
- [Python — HOW-TO](python/HOW-TO.md)
- [Go — README](go/README.md)
- [Go — HOW-TO](go/HOW-TO.md)
- [Consuming packages](CONSUMING.md)
- [Changelog](CHANGELOG.md)
