# Changelog

All notable changes to this project are documented here. Format based on
[Keep a Changelog](https://keepachangelog.com/); versioning follows
[Semantic Versioning](https://semver.org/).

This is a monorepo; each language package is versioned independently but is
currently aligned at the same version.

## [Unreleased]

### Fixed

- **Python: `[aws]`, `[redis]`, `[sql]` extras are now real** — the SQLAlchemy,
  Redis, and botocore instrumentations were incorrectly bundled in the core
  package, making the extras no-ops and the core heavier than documented. They
  now install only via their extras (new `[all]` meta-extra added). The
  `otel_helper.ext` helpers raise an actionable `ImportError`
  (`pip install otel-helper[aws]`) when the extra is missing.
- **Python: library-style dependency ranges** — OTel dependencies were pinned
  with `==`, causing pip resolution conflicts for any app depending on a
  different OTel SDK version. Now `>=1.42,<2` (stable) / `>=0.63b0`
  (instrumentations).
- **Python: custom OTLP endpoint port no longer discarded** —
  `OTEL_EXPORTER_OTLP_ENDPOINT=https://gateway:14317` was silently rewritten to
  port 4317. The port is now preserved; 4317 applies only when absent (parity
  with Go/.NET).

## [0.1.0-rc.1] - 2026-07-02

First published pre-release. Consumed and validated end-to-end from the
registries (see [CONSUMING.md](CONSUMING.md)).

### Added

- **Opt-in instrumentation subpackages/extensions** (all languages). The core
  package stays lightweight; add only what you need.
  - .NET: `OtelHelper.AWS`, `OtelHelper.Redis`, `OtelHelper.Sql`,
    `OtelHelper.Profiling` (Pyroscope) — `services.AddOtelHelperAws()` etc.
  - Python: `otel_helper.ext.{aws,redis,sql}` with `instrument_aws()` /
    `instrument_redis()` / `instrument_sql()`; install via
    `pip install otel-helper[aws,redis,sql]`.
  - Go: separate modules `ext/otelaws`, `ext/otelredis`, `ext/otelsql`.
- **Prometheus `/metrics` fallback** (all languages). When
  `OTEL_EXPORTER_OTLP_ENDPOINT` is not set, metrics are exposed via a
  Prometheus HTTP endpoint on port 9464 (configurable via
  `OTEL_HELPER_METRICS_PORT`) instead of OTLP push. Traces run in-process only
  and logs go to stdout — the standard Kubernetes scrape pattern.
- **TLS OTLP export** (all languages). Transport is derived from the endpoint
  scheme, secure by default:
  - `https://host:4317` → gRPC over TLS (system CA trust store)
  - `http://host:4317` → plaintext
  - `host:4317` (no scheme) → TLS (secure default)
  - Override with the standard `OTEL_EXPORTER_OTLP_INSECURE` env var; explicit
    code config wins over env/scheme.
- **30-second metric export interval** (all languages), replacing the SDK
  default of 60s.
- `CONSUMING.md` — validated per-language install guide (auth, registry
  sources, CI variants, gotchas) plus a `read:packages` token guide.

### Fixed

- **Python/Go — schemeless `OTEL_EXPORTER_OTLP_ENDPOINT` silently dropped
  telemetry.** An endpoint like `collector.svc:4317` (no scheme) resolved to an
  invalid `scheme://None`, so the exporter failed quietly. Endpoint resolution
  now prepends a scheme and preserves the host.
- **CI published only the core .NET package.** `build-lib`, `publish-dotnet`
  (dev) and `publish-dotnet-release` (tags) now pack and publish all five
  packages (core + AWS/Redis/Sql/Profiling).
- **CI demo image glob matched subpackages.** `OtelHelper.*.nupkg` matched
  `OtelHelper.AWS.nupkg` etc., breaking version extraction; now
  `OtelHelper.[0-9]*.nupkg` isolates the core version.

### Changed

- **Namespace/module migration to the `StaffOps` org.** NuGet source and
  repository URLs use `github.com/StaffOps`; Go modules, imports and examples
  use `github.com/staffops/staffops-otel-libs/go`.
- **Tests hardened with in-memory exporters and behavioral assertions** across
  all three languages (signal disabling actually drops data, sampler type
  verified, endpoint/TLS resolution covered, validation throws). Coverage:
  .NET ~94%, Python ~90%, Go ~94% — all above the 90% CI gate.

### Notes

- Pre-release only: not yet run in production. Per the versioning policy, a
  stable `0.1.0` is cut once the library is validated in a real workload.
- For a TLS gateway with a valid cert, consume with
  `OTEL_EXPORTER_OTLP_ENDPOINT=https://<gateway>:4317` — no extra flags.
