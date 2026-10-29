# Consuming otel-libs Packages

How to install and use the OTel helper packages. The repository is **public**:
Go modules and Python wheels need no credentials. Only the .NET packages
(GitHub Packages NuGet) need a token — GitHub requires one even for public
packages; see [Creating a read:packages token](#creating-a-readpackages-token).

## Versions

| Language | Latest release | Next | Requirements |
|----------|----------------|------|--------------|
| .NET | `0.2.0` | 0.3 contract in Phase C (from now on .NET releases are tagged `dotnet/vX.Y.Z`) | .NET 8 (runs on .NET 10 via roll-forward) |
| Python | `0.2.0` (`otel-helper`) | `0.3.0-rc.2` published as `otel-helper` (tag `v0.3.0-rc.2`, wheel `otel_helper-0.3.0rc2-py3-none-any.whl`); next `0.3.0-rc.3` (tag `v0.3.0-rc.3`, wheel `staffops_otel_helper-0.3.0rc3-py3-none-any.whl`) — the first under the new name (ADR-011) | Python ≥ 3.10; OpenTelemetry `>=1.42,<1.46`, contrib `>=0.63b0,<0.67b0` from 0.3 |
| Go | `go/v0.2.1` | `v0.3.0-rc.2` (tags `go/v0.3.0-rc.2`, `go/ext/<module>/v0.3.0-rc.2`; new module path, see ADR-010) | Go ≥ 1.22 for 0.2.x, **Go ≥ 1.26 from 0.3** |

The commands below install the latest release. When a 0.3 release candidate is
published, replace the version (Python release `v0.3.0-rc.2`, still named `otel-helper`, or `v0.3.0-rc.3`+ named `staffops-otel-helper`; Go `@v0.3.0-rc.2`, which resolves the `go/v0.3.0-rc.2` tag) and read the
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

### Map `OtelHelper*` to the GitHub source (required)

The `OtelHelper*` package IDs are **not registered on nuget.org**. Without
source mapping, NuGet queries every configured source and takes the best
match from any of them, so anyone who registers `OtelHelper` (or
`OtelHelper.Sql`, …) on nuget.org with a higher version would be restored
instead of ours — dependency confusion. Commit a `nuget.config` next to your
solution that pins those IDs to the GitHub source and everything else to
nuget.org (NuGet 6.0+ / .NET 6 SDK or later):

```xml
<?xml version="1.0" encoding="utf-8"?>
<configuration>
  <packageSources>
    <clear />
    <add key="nuget.org" value="https://api.nuget.org/v3/index.json" protocolVersion="3" />
    <add key="staffops" value="https://nuget.pkg.github.com/StaffOps/index.json" />
  </packageSources>
  <packageSourceMapping>
    <!-- The most specific pattern wins: OtelHelper* only ever comes from the GitHub source. -->
    <packageSource key="staffops">
      <package pattern="OtelHelper" />
      <package pattern="OtelHelper.*" />
    </packageSource>
    <packageSource key="nuget.org">
      <package pattern="*" />
    </packageSource>
  </packageSourceMapping>
  <packageSourceCredentials>
    <!-- Read from the environment; never commit the token itself. -->
    <staffops>
      <add key="Username" value="%GITHUB_USERNAME%" />
      <add key="ClearTextPassword" value="%GITHUB_PACKAGES_TOKEN%" />
    </staffops>
  </packageSourceCredentials>
</configuration>
```

With this file in the repository, the `dotnet nuget add source` step above is
not needed: set `GITHUB_USERNAME` and `GITHUB_PACKAGES_TOKEN` (a
`read:packages` token; `${{ secrets.GITHUB_TOKEN }}` in GitHub Actions)
instead. The `<clear />` drops sources inherited from user or machine
configs, so the mapping is the whole story. Restore fails with `NU1100` if a
package matches no mapped source — that is the mapping working.

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

Distribution (pip) name: `staffops-otel-helper`. Import name:
`staffops_otel_helper`. Wheel file: `staffops_otel_helper-<version>-py3-none-any.whl`
(ADR-011, from `0.3.0rc3`). `0.2.x` and `0.3.0rc2` shipped as `otel-helper` /
`otel_helper-<version>-py3-none-any.whl` with `import otel_helper`; rc.3 and
later use `staffops-otel-helper` / `staffops_otel_helper-<version>-py3-none-any.whl`.
From rc.3, `import otel_helper` still works during 0.3.x (a `DeprecationWarning`,
removed in 1.0) — see the migration guide in `CHANGELOG.md`. Never keep both
distributions installed: `pip uninstall -y otel-helper` before installing
`staffops-otel-helper`.

In the commands below `<tag>` is the release tag (`v0.3.0-rc.3` or later) and
`<version>` the package version in the wheel name (`0.3.0rc3` or later). For
`v0.2.0` / `v0.3.0-rc.2`, use the old names instead: wheel
`otel_helper-<version>-py3-none-any.whl`, requirement name `otel-helper`.

### Install using gh CLI (simplest)

```bash
gh release download <tag> --repo StaffOps/otel-libs --pattern "*.whl"
pip install staffops_otel_helper-<version>-py3-none-any.whl
```

(`gh` needs a login; for anonymous downloads use the URL below.)

### Install without gh (anonymous)

```bash
curl -fsSLO https://github.com/StaffOps/otel-libs/releases/download/<tag>/staffops_otel_helper-<version>-py3-none-any.whl
pip install staffops_otel_helper-<version>-py3-none-any.whl
```

Verify the download against the SHA-256 shown on the release page
(`shasum -a 256 <file>`).

> **Never `pip install staffops-otel-helper` by name.** The package is not on
> PyPI and this project does not publish there: installing it by name — or
> listing a bare `staffops-otel-helper` in `requirements.txt` — makes pip look
> it up on the public index, where anything under that name is not ours
> (dependency confusion). Install the wheel file, or pin it by URL **with its
> hash** (next section).

### Pin by URL in `requirements.txt`

```text
staffops-otel-helper @ https://github.com/StaffOps/otel-libs/releases/download/<tag>/staffops_otel_helper-<version>-py3-none-any.whl#sha256=<digest>
```

`<digest>` is the SHA-256 shown on the release page (or `shasum -a 256` of the
downloaded wheel). pip checks the downloaded file against the `#sha256=`
fragment and refuses a mismatch, so a replaced release asset cannot slip in.
For the whole dependency tree, use hash-checking mode: `pip install
--require-hashes -r requirements.txt`, which needs every requirement —
transitive ones included — pinned with `==` and a `--hash=sha256:…` (generate
the file with `pip-compile --generate-hashes`); the wheel line then becomes
`staffops-otel-helper @ <url> --hash=sha256:<digest>`.

> **Important gotcha**: The downloaded file MUST keep the canonical wheel filename (`staffops_otel_helper-<version>-py3-none-any.whl`). pip rejects a renamed wheel with `ERROR: not a valid wheel filename`.

### Optional extras (standard OTel packages from PyPI)

```bash
pip install "staffops-otel-helper[aws,redis,sql] @ file://$(pwd)/staffops_otel_helper-<version>-py3-none-any.whl"
```

### Usage

```python
import staffops_otel_helper  # isort: skip  (before any opentelemetry.* import)
from staffops_otel_helper import setup_telemetry, TelemetryOptions
setup_telemetry(TelemetryOptions(service_name="orders-api"))
```

Prefer `OTEL_SERVICE_NAME` in the environment over a name in code. From 0.3,
call `shutdown_telemetry()` on SIGTERM — see `python/HOW-TO.md`.

---

## Go (direct from repo)

Consumed via `go get` through the public Go module proxy — no credentials.

Module path: `github.com/staffops/otel-libs/go`

### Do not set `GOPRIVATE` for this module

Older versions of this page asked for `GOPRIVATE=github.com/staffops/*` and a
git token. The repository is public: remove that setting for this module —
`GOPRIVATE` also disables checksum-database verification (`sum.golang.org`)
for every matching module, silently.

If you set `GOPRIVATE` for *other* private modules under the same owner, do not
use a wildcard such as `github.com/staffops/*`: it matches this module too.
List those private modules explicitly instead
(`GOPRIVATE=github.com/staffops/private-a,github.com/staffops/private-b`).

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

Needed only for the .NET packages (GitHub Packages NuGet requires a token even for public packages).

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
