# AGENTS.md — staffops-otel-libs

Canonical guide for agents and humans working in this repo. Tool-specific
files are one-line pointers here — `AGENTS.md` (imports this file via
`@AGENTS.md`), `.kiro/steering/project-conventions.md` — edit this file, not
those.

> Behavior described here reflects the shipped `0.2.0` code. Where the
> `0.3.0` contract changes it, the target is in
> [`specs/config-contract-v0.3/`](specs/config-contract-v0.3/). If this file
> conflicts with the code, treat it as a bug in one of the two and flag it —
> do not silently follow either side.

## Repository

Monorepo of OpenTelemetry helper libraries that must stay **functionally
equivalent**: `go/` (module `github.com/staffops/staffops-otel-libs/go` +
`ext/*` submodules), `python/` (`otel-helper`, extras `aws`/`redis`/`sql`),
`dotnet/` (`OtelHelper` + `.AWS`/`.Redis`/`.Sql`/`.Profiling`). Planned:
`nodejs/`. Shared Grafana dashboards in `dashboards/`, MkDocs site in
`docs/site/`, specs in [`specs/`](specs/README.md).

## Core principles

1. **OpenTelemetry as the single standard** — no vendor SDKs. Only
   `go.opentelemetry.io/*` / `opentelemetry-*` / `OpenTelemetry.*` and
   official OTel contrib packages.
2. **Everything via Collector** — SDK exports OTLP to the Collector, never
   directly to backends (Tempo, Loki, VictoriaMetrics). **One exception:** with
   no OTLP endpoint — or when `OTEL_METRICS_EXPORTER` includes `prometheus` —
   metrics are served on a Prometheus `/metrics` endpoint on port 9464
   (`OTEL_HELPER_METRICS_PORT`). This fallback stays in core
   (decided 2026-10-27, see [ADR-007](specs/decisions/ADR-007-keep-prometheus-in-core.md)).
3. **Sampling at the Collector** — tail sampling at the gateway, not in app
   code. Target default sampler: `parentbased_always_on`
   ([ADR-002](specs/decisions/ADR-002-parentbased-sampler.md)).
4. **Resource attributes at the Collector** — SDK sets only `service.name`
   and `deployment.environment.name` (semconv ≥ v1.27 key, never the legacy
   `deployment.environment`). `k8sattributes` enriches the rest. From 0.3
   (Go/Python) a per-process `service.instance.id` is added when the env does
   not provide one (`specs/config-contract-v0.3/design.md` D4).
5. **TLS by default** — `https://` or schemeless endpoints use TLS (system CA);
   plaintext needs `http://` or `OTEL_EXPORTER_OTLP_INSECURE=true`. *0.3 target
   (Go/Python): the scheme decides; `_INSECURE` applies only to schemeless gRPC
   endpoints, per spec; conflicts are warned (design D1.c).*
6. **Standard OTel env vars win over `OTEL_HELPER_*` vars.** Order: explicit
   code > standard OTel env var > `OTEL_HELPER_*`/legacy env > library
   default. Never invent a custom var when the OTel spec defines one.
7. **Don't re-implement what the SDK already does.** If an OTel SDK honors an
   env var per spec (endpoints, TLS, headers, timeout, compression), the
   helper must not override it ([ADR-001](specs/decisions/ADR-001-defer-otlp-env-to-sdk.md)).
8. **Failures must be loud** — never silently drop telemetry. *0.3 target
   (Go/Python): env-var parse errors warn and fall back; invalid values passed
   in code fail fast. Implemented on the branch for Go and Python (A3); .NET
   still raises on some env values (Phase C).*

## Environment variables (shipped 0.2.0 contract)

| Variable | Default | Description |
|----------|---------|-------------|
| `SERVICE_NAME` | `my-service` | Service name (`OTEL_SERVICE_NAME` also honored). 0.3: deprecated, `OTEL_SERVICE_NAME` wins, unset → SDK `unknown_service:*` |
| `ENVIRONMENT` | `LOCAL` | LOCAL, DEV, HML, PRD. 0.3: deprecated alias, unset → attribute absent, unknown → verbatim ([ADR-003](specs/decisions/ADR-003-environment-unset-not-local.md)) |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | _(empty)_ | Collector endpoint; empty triggers the Prometheus metrics fallback. 0.3: per-signal `OTEL_EXPORTER_OTLP_<SIGNAL>_ENDPOINT` honored and counts as configured for that signal |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | _(unset)_ | `grpc` or `http/protobuf`; unset → port 4318 infers http, else grpc. 0.3: per-signal `_<SIGNAL>_PROTOCOL` honored |
| `OTEL_EXPORTER_OTLP_INSECURE` | _(unset)_ | `true` = plaintext, `false` = TLS; unset = derived from scheme |
| `OTEL_METRICS_EXPORTER` | _(unset)_ | `otlp`, `prometheus`, `otlp,prometheus`, `none` (see `specs/metrics-exporter-contract/`) |
| `OTEL_METRIC_EXPORT_INTERVAL` | `30000` | Metric export interval in ms (helper default 30s, SDK default 60s) |
| `OTEL_TRACES_SAMPLER` / `_ARG` | _(unset)_ | Standard sampler; wins over `OTEL_HELPER_SAMPLE_RATIO` |
| `OTEL_HELPER_DEBUG_LEVEL` | `false` | Debug mode |
| `OTEL_HELPER_EXTRA_INSTRUMENTATION` | `SQL` | Effective in Python only (dead in Go/.NET). 0.3: removed in Go, deprecated in Python, .NET unchanged until Phase C |
| `OTEL_HELPER_SAMPLE_RATIO` | `1.0` | Head sampling ratio; deprecated in 0.3 |
| `OTEL_HELPER_DISABLED_SIGNALS` | _(empty)_ | traces, metrics, logs; deprecated in 0.3 for `OTEL_*_EXPORTER=none` |
| `OTEL_HELPER_DISABLED_METRICS` | _(empty)_ | Wildcard patterns of metrics to drop (all exporters) |
| `OTEL_HELPER_METRICS_PORT` | `9464` | Prometheus `/metrics` listener port |

## Cross-language parity

- Same env vars, same precedence, same defaults, same emitted attributes,
  same propagators (W3C TraceContext + Baggage), same export interval, same
  health-path filtering (the last is a 0.3 target — today it diverges, F7).
- Parity is enforced by the shared fixture `parity/cases.json` (from `0.3.0`),
  run by every language's test suite. A language that lags marks its cases
  `xfail:<reason>` — never delete a case to make CI green.
- **Rollout order (decided 2026-10-27,
  [ADR-004](specs/decisions/ADR-004-phased-rollout-go-python-first.md)):**
  Go + Python first → Node.js → .NET. A contract change lands in Go and
  Python together; Node.js and .NET catch up in their phase, tracked by
  `xfail` cases. This replaces the former "all three in the same release" rule.
- Any change to the env-var contract or emitted attributes needs a spec in
  `specs/` first.

## Packaging

- **Core stays lightweight.** AWS/Redis/SQL instrumentations are opt-in:
  `ext/otel{aws,redis,sql}` (Go), `otel-helper[aws,redis,sql]` (Python — core
  must NOT depend on them), `OtelHelper.{AWS,Redis,Sql}` (NuGet).
- **Libraries use version ranges, not exact pins.** Exact pins only in
  example/demo lockfiles. Python contrib betas move in lockstep with the SDK —
  bound both sides as a matched pair.

## Development workflow

- All builds and tests run via Docker (no local SDK dependency):
  `golang` (unreleased 0.3.0 / branch work: go.mod floor `go 1.26`, CI on 1.26.x and 1.27.x; shipped 0.2.0 was `go 1.22`), `python:3.x-slim`,
  `mcr.microsoft.com/dotnet/sdk`. Use
  `docker compose` with a real `otel/opentelemetry-collector-contrib` when a
  behavior crosses the wire (endpoints, TLS, protocols, logs).
- Unit tests with a ≥ 90% coverage CI gate per language.
- Each language has 3 example apps (api, backend, process) demonstrating
  HTTP → gRPC distributed tracing.
- **Change loop** for anything non-trivial:
  1. spec / decision record in `specs/` (format: `specs/README.md`);
  2. implement + tests (Docker);
  3. independent validation — an `observability` agent (spec / semconv /
     backend conformance) and a `dev` agent (reproduce, idioms) try to
     refute the change; every claim needs file:line or spec evidence;
  4. `code-review` on the diff;
  5. a `CHANGELOG.md` (Unreleased) entry in the same change. During the 0.3
     work the full docs pass — root `README.md`, `CONSUMING.md`, per-language
     `README`/`HOW-TO`/`TESTS`, this file, the MkDocs site (`docs/site/`,
     `mkdocs.yml`) and per-language examples of custom traces, metrics and
     logs — is **mandatory and runs last** (task A13 in
     `specs/config-contract-v0.3/tasks.md`), after everything is validated.
     The pre-commit hook in `.claude/settings.json` reminds you to check
     README, CHANGELOG and the per-language HOW-TOs.
- Helper diagnostics (config warnings, deprecations): Go → the package's stderr
  `*slog.Logger` (never `slog.Default()`, `fmt.Fprint*`, or `otel.Handle`, which
  is for SDK pipeline errors); Python → `logging.getLogger("otel_helper…")`.
  Never print secrets (`*_HEADERS`, `*_CLIENT_KEY`, URL userinfo/query).
- Commit messages: Conventional Commits with a language scope, e.g.
  `fix(go): ...`, `feat(all): ...`.
