# Consuming otel-libs Packages

How to install and use the OTel helper packages from this private repository.

All packages require authentication — see [Creating a read:packages token](#creating-a-readpackages-token) below.

## Versions

| Language | Latest release | Next | Requirements |
|----------|----------------|------|--------------|
| .NET | `0.2.0` | 0.3 contract in Phase C | .NET 8 (runs on .NET 10 via roll-forward) |
| Python | `0.2.0` | `0.3.0` (unreleased; `0.3.0-rc.1` first) | Python ≥ 3.10; OpenTelemetry `>=1.42,<1.46`, contrib `>=0.63b0,<0.67b0` from 0.3 |
| Go | `go/v0.2.1` | `go/v0.3.0` (unreleased; rc first) | Go ≥ 1.22 for 0.2.x, **Go ≥ 1.26 from 0.3** |

The commands below install the latest release. When a 0.3 release candidate is
published, replace the version (Python release `v0.3.0-rc.1`; Go `@v0.3.0-rc.1`, which resolves the `go/v0.3.0-rc.1` tag) and read the
[migration guide](CHANGELOG.md#unreleased) before upgrading — 0.3 changes
defaults, metric names and APIs.

---

## .NET (GitHub Packages NuGet)

Registry: `https://nuget.pkg.github.com/StaffOps/index.json`

Packages: `OtelHelper` (core), `OtelHelper.AWS`, `OtelHelper.Redis`, `OtelHelper.Sql`, `OtelHelper.Profiling`. Subpackages pull the core transitively at the same version.

### Add the NuGet source (once per machine or CI)

```bash
dotnet nuget add source "https://nuget.pkg.github.com/StaffOps/index.json" \
  --name staffops \
  --username <github-username> \
  --password <PAT_with_read:packages> \
  --store-password-in-clear-text
```

In CI (GitHub Actions): `--password ${{ secrets.GITHUB_TOKEN }}`.

### Install packages

```bash
# Core package
dotnet add package OtelHelper --version 0.2.0

# Opt-in subpackages (each pulls OtelHelper core transitively)
dotnet add package OtelHelper.AWS --version 0.2.0
dotnet add package OtelHelper.Redis --version 0.2.0
dotnet add package OtelHelper.Sql --version 0.2.0
dotnet add package OtelHelper.Profiling --version 0.2.0
```

> **Note**: plain `dotnet add package OtelHelper` (no flags) also resolves to
> the latest stable version, since NuGet excludes prereleases from default
> resolution once a stable version exists. Pinning `--version 0.2.0` explicitly is
> still recommended for reproducible builds. Prerelease versions also exist
> (`-dev-<sha>` published on every push to `main`, `-rc.N` from RC tags) — use
> `--prerelease` only if you deliberately want to track those.

### Usage

```csharp
services.AddOtelHelper();
```

---

## Python (GitHub Release assets)

Distribution: wheel + sdist attached to GitHub Releases (NOT PyPI, NOT GitHub Packages).

Package name on import: `otel_helper`. pip/distribution name: `otel-helper`.

### Install using gh CLI (simplest)

```bash
gh release download v0.2.0 --repo StaffOps/otel-libs --pattern "*.whl"
pip install otel_helper-0.2.0-py3-none-any.whl
```

In CI: set `GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}` in the env.

### Install using the GitHub API (no gh CLI)

```bash
# Resolve the asset API URL, then download with auth
ASSET_URL=$(curl -s -H "Authorization: Bearer $GH_PAT" \
  "https://api.github.com/repos/StaffOps/otel-libs/releases/tags/v0.2.0" \
  | jq -r '.assets[] | select(.name | endswith(".whl")) | .url')
curl -sL -H "Authorization: Bearer $GH_PAT" -H "Accept: application/octet-stream" \
  "$ASSET_URL" -o otel_helper-0.2.0-py3-none-any.whl
pip install otel_helper-0.2.0-py3-none-any.whl
```

> **Important gotcha**: The downloaded file MUST keep the canonical wheel filename (`otel_helper-0.2.0-py3-none-any.whl`). pip rejects a renamed wheel with `ERROR: not a valid wheel filename`.

### Optional extras (standard OTel packages from PyPI)

```bash
pip install "otel-helper[aws,redis,sql] @ file://$(pwd)/otel_helper-0.2.0-py3-none-any.whl"
```

### Usage

```python
from otel_helper import setup_telemetry, TelemetryOptions
setup_telemetry(TelemetryOptions(service_name="orders-api"))
```

Prefer `OTEL_SERVICE_NAME` in the environment over a name in code. From 0.3,
call `shutdown_telemetry()` on SIGTERM — see `python/HOW-TO.md`.

---

## Go (direct from repo)

Consumed via `go get` from the private repo (no registry). Requires `GOPRIVATE` and git auth.

Module path: `github.com/staffops/otel-libs/go`

### Configure auth

```bash
# HTTPS + PAT (validated)
git config --global url."https://x:${GH_PAT}@github.com/".insteadOf "https://github.com/"

# SSH alternative (if keys are set up)
# git config --global url."git@github.com:".insteadOf "https://github.com/"

export GOPRIVATE=github.com/staffops/*
```

### Install modules

```bash
# Core module (repo tags are go/vX.Y.Z; `go get` takes the bare vX.Y.Z)
go get github.com/staffops/otel-libs/go@latest

# Opt-in ext modules (separate Go modules)
go get github.com/staffops/otel-libs/go/ext/otelaws@latest
go get github.com/staffops/otel-libs/go/ext/otelredis@latest
go get github.com/staffops/otel-libs/go/ext/otelsql@latest
```

### Usage

```go
import otelhelper "github.com/staffops/otel-libs/go"

shutdown, err := otelhelper.Setup(ctx, otelhelper.WithServiceName("orders-api"))
if err != nil {
    return err
}
defer shutdown(context.Background()) // from 0.3: use a fresh deadline on SIGTERM, see go/HOW-TO.md
```

---

## Creating a read:packages token

All methods require a GitHub token with package read access.

### Fine-grained token (recommended)

1. Go to https://github.com/settings/tokens?type=beta
2. Resource owner: **StaffOps**
3. Repository access: **otel-libs**
4. Organization permissions → Packages: **Read-only**

### Classic token

1. Go to https://github.com/settings/tokens
2. Scope: `read:packages`

### Best practices

- Never commit tokens to source control.
- Use short expiry (90 days max for personal use).
- In CI, prefer the built-in `GITHUB_TOKEN` — it has `read:packages` automatically for the same org.
