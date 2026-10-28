# AGENTS.md — otel-libs

Canonical guide for agents and humans working in this repo. Tool-specific
files are one-line pointers here — `CLAUDE.md` (imports this file via
`@AGENTS.md`), `.kiro/steering/project-conventions.md` — edit this file, not
those.

> Go and Python on this branch implement the `0.3.0` contract
> ([`specs/config-contract-v0.3/`](specs/config-contract-v0.3/), Phase A done);
> .NET still ships the `0.2.x` behavior until Phase C. If this file conflicts
> with the code, treat it as a bug in one of the two and flag it — do not
> silently follow either side.

## Repository

Monorepo of OpenTelemetry helper libraries that must stay **functionally
equivalent**: `go/` (module `github.com/staffops/otel-libs/go` +
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
5. **TLS by default** — `https://` or schemeless endpoints use TLS (system CA).
   The scheme decides; `OTEL_EXPORTER_OTLP_INSECURE=true` only makes a
   schemeless endpoint plaintext, and a contradiction with an explicit scheme
   is warned and the scheme wins (Go/Python, design D1.c). .NET 0.2.x also
   applies `_INSECURE` to schemeless endpoints only.
6. **Standard OTel env vars win over `OTEL_HELPER_*` vars.** Order: explicit
   code > standard OTel env var > `OTEL_HELPER_*`/legacy env > library
   default. Never invent a custom var when the OTel spec defines one.
7. **Don't re-implement what the SDK already does.** If an OTel SDK honors an
   env var per spec (endpoints, TLS, headers, timeout, compression), the
   helper must not override it ([ADR-001](specs/decisions/ADR-001-defer-otlp-env-to-sdk.md)).
8. **Failures must be loud** — never silently drop telemetry. Go/Python:
   env-var parse errors warn and fall back; invalid values passed in code fail
   fast (R8). .NET 0.2.x still fails startup on some env values (Phase C).

## Environment variables

Full table with defaults: root [`README.md`](README.md#environment-variables-03-contract--go-and-python);
resolution rules: design D1. Summary of what differs between the two contracts:

| Variable | 0.3 — Go / Python (this branch) | .NET 0.2.x |
|----------|----------------------------------|------------|
| `OTEL_SERVICE_NAME` / `SERVICE_NAME` | `OTEL_SERVICE_NAME` > `OTEL_RESOURCE_ATTRIBUTES` > `SERVICE_NAME` (deprecated) > SDK `unknown_service:*` + warning | `SERVICE_NAME` > `OTEL_SERVICE_NAME` > `my-service` |
| `ENVIRONMENT` | Deprecated alias; `OTEL_RESOURCE_ATTRIBUTES` wins; unset → attribute absent + warning, INFO; unknown → verbatim ([ADR-003](specs/decisions/ADR-003-environment-unset-not-local.md)) | Default `LOCAL`; unknown → `LOCAL` |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | Unset → Prometheus `/metrics` + stdout logs. Spec URL handling (path kept, HTTP 443/80); schemeless → `https://` + default port | Unset → same fallback; URLs normalized to `scheme://host:port` (path dropped, default 4317) |
| `OTEL_EXPORTER_OTLP_<SIGNAL>_*` | Honored; a per-signal endpoint configures that signal | Not used |
| `OTEL_EXPORTER_OTLP[_<SIGNAL>]_PROTOCOL` | `grpc` / `http/protobuf`, port-4318 inference; `http/json` env → warned, ignored | Same values; `http/json` fails validation |
| `OTEL_EXPORTER_OTLP[_<SIGNAL>]_INSECURE` | Schemeless only; conflict → scheme wins, warned | Schemeless only |
| `_HEADERS` / `_TIMEOUT` / `_COMPRESSION` | Helper-parsed (ms, clamped; gzip default) | Headers via SDK; timeout fixed by `ExportTimeoutMs` (10 s); compression not set by the helper |
| `OTEL_{TRACES,METRICS,LOGS}_EXPORTER` | `otlp`, `console`, `none` (+ `prometheus` for metrics) | `OTEL_METRICS_EXPORTER` only (`otlp`, `prometheus`, `none`) |
| `OTEL_TRACES_SAMPLER` / `_ARG` | Helper-parsed; default `parentbased_always_on` | SDK, only when no code sampler; default bare AlwaysOn |
| `OTEL_PROPAGATORS` | `tracecontext`, `baggage`, `none`; others warned | Not read (W3C fixed) |
| `OTEL_SDK_DISABLED` | No-op providers, propagators kept | SDK |
| `OTEL_METRIC_EXPORT_INTERVAL` | `30000` | `30000` |
| `OTEL_HELPER_DEBUG_LEVEL` | `debug=true` on entry spans (no or remote parent) | `debug=true` attribute on entry spans (no in-process parent, so also spans continuing a remote trace) + `debug=true` prepended to the propagated tracestate |
| `OTEL_HELPER_SAMPLE_RATIO`, `OTEL_HELPER_DISABLED_SIGNALS` | Deprecated, warned once → standard vars | Supported |
| `OTEL_HELPER_EXTRA_INSTRUMENTATION` | Go: removed (warns). Python: deprecated, default `SQL` | Read but no effect |
| `OTEL_HELPER_DISABLED_METRICS`, `OTEL_HELPER_METRICS_PORT` | Supported (`9464`, `0` = no listener) | Supported |

## Cross-language parity

- Same env vars, same precedence, same defaults, same emitted attributes,
  same propagators (W3C TraceContext + Baggage), same export interval, same
  health-path filtering (Go and Python share one list and semantics since A7 —
  design D5; .NET keeps its 0.2.x filter until Phase C). Emitted attributes
  are the target, not yet the fact: current gaps (Python gRPC legacy `rpc.*`
  attributes and no RPC metrics; .NET on 0.2.x) are listed under "Known
  emitted-telemetry gaps" in [`README.md`](README.md#known-emitted-telemetry-gaps).
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
