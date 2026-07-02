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
- **Prometheus fallback** — when no OTLP endpoint is configured, metrics are exposed via `/metrics` on port 9464 (Prometheus scrape compatible)
- **Sampling at the Collector** — SDK uses AlwaysOn, tail sampling at the gateway
- **Resource attributes at the Collector** — SDK only sets `service.name`
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
| `OTEL_HELPER_DEBUG_LEVEL` | `false` | Debug mode (DEBUG log, all instrumentations, attribute debug=true) |
| `OTEL_HELPER_EXTRA_INSTRUMENTATION` | `SQL` | Conditional instrumentations: SQL, AWS, REDIS |
| `OTEL_HELPER_SAMPLE_RATIO` | `1.0` | Head sampling ratio (0.0-1.0). 1.0 = AlwaysOn |
| `OTEL_HELPER_METRICS_PORT` | `9464` | Prometheus `/metrics` port when no OTLP endpoint is configured |

## Installing from GitHub Packages (private)

### .NET

Add the GitHub NuGet source (once per machine/CI):

```bash
dotnet nuget add source "https://nuget.pkg.github.com/StaffOps/index.json" \
  --name github-staffops \
  --username StaffOps \
  --password <GITHUB_PAT_WITH_READ_PACKAGES>
```

Then reference in your `.csproj`:

```xml
<PackageReference Include="OtelHelper" Version="0.1.0" />
```

In CI (GitHub Actions), use `GITHUB_TOKEN` automatically:

```yaml
- run: dotnet nuget add source "https://nuget.pkg.github.com/StaffOps/index.json"
        --name github --username github --password ${{ secrets.GITHUB_TOKEN }}
```

### Python

Download the wheel from the GitHub Release:

```bash
# Authenticate with gh CLI
gh release download v0.1.0 --repo StaffOps/staffops-otel-libs --pattern "*.whl"
pip install otel_helper-0.1.0-py3-none-any.whl
```

Or install directly (requires PAT with `repo` scope):

```bash
pip install "otel-helper @ https://github.com/StaffOps/staffops-otel-libs/releases/download/v0.1.0/otel_helper-0.1.0-py3-none-any.whl" \
  --extra-index-url https://<PAT>@raw.githubusercontent.com/
```

In CI (GitHub Actions):

```yaml
- run: gh release download v0.1.0 --repo StaffOps/staffops-otel-libs --pattern "*.whl"
  env:
    GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
- run: pip install otel_helper-*.whl
```

### Go

Go modules are consumed directly from the private repo:

```bash
# Configure git to use SSH (or PAT) for private repos
git config --global url."git@github.com:".insteadOf "https://github.com/"

# Set GOPRIVATE
export GOPRIVATE=github.com/staffops/*

go get github.com/staffops/staffops-otel-libs/go@latest
```

## Documentation

- [.NET — README](dotnet/README.md)
- [.NET — HOW-TO](dotnet/HOW-TO.md)
- [Python — README](python/README.md)
- [Python — HOW-TO](python/HOW-TO.md)
- [Go — README](go/README.md)
- [Go — HOW-TO](go/HOW-TO.md)
