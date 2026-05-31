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
- **Sampling at the Collector** — SDK uses AlwaysOn, tail sampling at the gateway
- **Resource attributes at the Collector** — SDK only sets `service.name`

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
import "github.com/staffops/otel-helper-go"

shutdown, err := otelhelper.Setup(ctx)
defer shutdown(ctx)
```

## Environment Variables (all libs)

| Variable | Default | Description |
|----------|---------|-------------|
| `SERVICE_NAME` | `my-service` | Service name |
| `ENVIRONMENT` | `LOCAL` | Environment: LOCAL, DEV, HML, PRD |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://localhost` | Collector endpoint |
| `OTEL_HELPER_DEBUG_LEVEL` | `false` | Debug mode (DEBUG log, all instrumentations, attribute debug=true) |
| `OTEL_HELPER_EXTRA_INSTRUMENTATION` | `SQL` | Conditional instrumentations: SQL, AWS, REDIS |
| `OTEL_HELPER_SAMPLE_RATIO` | `1.0` | Head sampling ratio (0.0-1.0). 1.0 = AlwaysOn |

## Documentation

- [.NET — README](dotnet/README.md)
- [.NET — HOW-TO](dotnet/HOW-TO.md)
- [Python — README](python/README.md)
- [Python — HOW-TO](python/HOW-TO.md)
- [Go — README](go/README.md)
- [Go — HOW-TO](go/HOW-TO.md)
