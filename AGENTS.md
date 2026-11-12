# AGENTS.md — otel-libs

Canonical guide for agents and humans working in this repo. Tool-specific
files are one-line pointers here — `CLAUDE.md` (imports this file via
`@AGENTS.md`), `.kiro/steering/project-conventions.md` — edit this file, not
those.

> Go and Python on this branch implement the `0.3.0` contract
> ([`specs/config-contract-v0.3/`](specs/config-contract-v0.3/), Phase A done);
> Node.js implements it too (Phase B, [`specs/nodejs-otel-helper/`](specs/nodejs-otel-helper/),
> release candidate `0.3.0-rc.1`); .NET implements it on this branch too (Phase C,
> [`specs/dotnet-otel-helper/`](specs/dotnet-otel-helper/), tasks C4–C17 done; release
> candidate `0.3.0-rc.1` after the final validation, task C19; the released .NET
> `0.2.1` keeps the 0.2 behavior). If this file conflicts
> with the code, treat it as a bug in one of the two and flag it — do not
> silently follow either side.

## Repository

Monorepo of OpenTelemetry helper libraries that must stay **functionally
equivalent**: `go/` (module `github.com/staffops/otel-libs/go` +
`ext/*` submodules), `python/` (`staffops-otel-helper`, import `staffops_otel_helper`, extras
`aws`/`redis`/`sql`; `otel_helper` is a deprecated re-export until 1.0 —
[ADR-011](specs/decisions/ADR-011-owner-prefixed-package-names.md)),
`dotnet/` (`StaffOps.OtelHelper` + `.AWS`/`.Redis`/`.Sql`/`.Profiling`; `0.2.x` released as `OtelHelper*`;
first published as `0.3.0-rc.1`, tag `dotnet/v0.3.0-rc.1`, after task C19), `nodejs/`
(npm workspaces: `@staffops/otel-helper` + `-sql`/`-redis`/`-aws`/`-pino`/`-winston`;
release candidate `0.3.0-rc.1`, tag `nodejs/v0.3.0-rc.1`, on npmjs with dist-tag `next` and on GitHub Packages; `latest` stays the deprecated bootstrap placeholder until the first stable release). Shared Grafana dashboards in `dashboards/`, MkDocs site in
`docs/site/`, specs in [`specs/`](specs/README.md).

## Core principles

1. **OpenTelemetry as the single standard** — no vendor SDKs. Only
   `go.opentelemetry.io/*` / `opentelemetry-*` / `OpenTelemetry.*` and
   official OTel contrib packages.
2. **Everything via Collector** — SDK exports OTLP to the Collector, never
   directly to backends (Tempo, Loki, VictoriaMetrics). **One exception:** with
   no OTLP endpoint — or when `OTEL_METRICS_EXPORTER` includes `prometheus` —
   metrics are served on a Prometheus `/metrics` endpoint on port 9464
   (`OTEL_EXPORTER_PROMETHEUS_PORT` > `OTEL_HELPER_METRICS_PORT`; bind address
   `OTEL_EXPORTER_PROMETHEUS_HOST`, unset = every interface, A29). This fallback stays in core
   (decided 2026-10-27, see [ADR-007](specs/decisions/ADR-007-keep-prometheus-in-core.md)).
   **Second exception (2026-11-12):** continuous profiles from the opt-in .NET
   package `StaffOps.OtelHelper.Profiling` are pushed by the Pyroscope native
   profiler straight to `PYROSCOPE_SERVER_ADDRESS` — no OTLP path exists yet
   ([ADR-015](specs/decisions/ADR-015-dotnet-profiles-push-to-pyroscope.md)).
3. **Sampling at the Collector** — tail sampling at the gateway, not in app
   code. Target default sampler: `parentbased_always_on`
   ([ADR-002](specs/decisions/ADR-002-parentbased-sampler.md)).
4. **Resource attributes at the Collector** — SDK sets only `service.name`
   and `deployment.environment.name` (semconv ≥ v1.27 key, never the legacy
   `deployment.environment`). `k8sattributes` enriches the rest. From 0.3
   (every language) a per-process `service.instance.id` is added when the env does
   not provide one (`specs/config-contract-v0.3/design.md` D4).
5. **TLS by default** — `https://` or schemeless endpoints use TLS (system CA).
   The scheme decides; `OTEL_EXPORTER_OTLP_INSECURE=true` only makes a
   schemeless endpoint plaintext, and a contradiction with an explicit scheme
   is warned and the scheme wins (every language on 0.3, design D1.c). The released
   .NET 0.2.x also applies `_INSECURE` to schemeless endpoints only, without the warning.
6. **Standard OTel env vars win over `OTEL_HELPER_*` vars.** Order: explicit
   code > standard OTel env var > `OTEL_HELPER_*`/legacy env > library
   default. Never invent a custom var when the OTel spec defines one.
7. **Don't re-implement what the SDK already does.** If an OTel SDK honors an
   env var per spec (endpoints, TLS, headers, timeout, compression), the
   helper must not override it ([ADR-001](specs/decisions/ADR-001-defer-otlp-env-to-sdk.md)).
8. **Failures must be loud** — never silently drop telemetry. Every language on
   0.3: env-var parse errors warn and fall back; invalid values passed in code fail
   fast (R8). The released .NET 0.2.x still fails startup on some env values.

## Environment variables

Full table with defaults: root [`README.md`](README.md#environment-variables-03-contract);
resolution rules: design D1. Summary of the 0.3 rows, and where .NET differs on this
branch (the released 0.2.x behavior, row by row: the .NET migration guide in
[`CHANGELOG.md`](CHANGELOG.md)):

Node.js follows the 0.3 column; its own rows (`OTEL_LOG_LEVEL`,
`OTEL_NODE_RESOURCE_DETECTORS`, no semconv default, `OTEL_HELPER_EXTRA_INSTRUMENTATION`
without effect; the code option `replaceGlobals`, not an env var) are in [`nodejs/README.md`](nodejs/README.md#environment-variables).

| Variable | 0.3 — every language (this branch) | .NET on this branch, where it differs |
|----------|----------------------------------|------------|
| `OTEL_SERVICE_NAME` / `SERVICE_NAME` | `OTEL_SERVICE_NAME` > `OTEL_RESOURCE_ATTRIBUTES` > `SERVICE_NAME` (deprecated) > SDK `unknown_service:*` + warning | Same; code `ServiceName` and `ResourceAttributes["service.name"]` come first |
| `ENVIRONMENT` | Deprecated alias; `OTEL_RESOURCE_ATTRIBUTES` wins; unset → attribute absent + warning, INFO; unknown → verbatim ([ADR-003](specs/decisions/ADR-003-environment-unset-not-local.md)) | Same |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | Unset → Prometheus `/metrics` + stdout logs. Spec URL handling (path kept, HTTP 443/80); schemeless → `https://` + default port | Same; paths normalized by `System.Uri` (`/a/../v1` → `/v1`) |
| `OTEL_EXPORTER_OTLP_<SIGNAL>_*` | Honored; a per-signal endpoint configures that signal | Same |
| `OTEL_EXPORTER_OTLP[_<SIGNAL>]_PROTOCOL` | `grpc` / `http/protobuf`, port-4318 inference; `http/json` env → warned, ignored | Same |
| `OTEL_EXPORTER_OTLP[_<SIGNAL>]_INSECURE` | Schemeless only; conflict → scheme wins, warned | Same |
| `_HEADERS` / `_TIMEOUT` / `_COMPRESSION` | Helper-parsed (ms, clamped; gzip default) | Same, sent on the helper's own `HttpClient`, which refuses redirects ([ADR-013](specs/decisions/ADR-013-dotnet-helper-owns-env-resolution.md)) |
| `OTEL_{TRACES,METRICS,LOGS}_EXPORTER` | `otlp`, `console`, `none` (+ `prometheus` for metrics); `logging` = warned alias of `console` in env only — code `logging` fails (A19) | Same |
| `OTEL_TRACES_SAMPLER` / `_ARG` | Helper-parsed; default `parentbased_always_on` | Same; process environment only (an `appsettings.json`-only value warns once) |
| `OTEL_PROPAGATORS` | `tracecontext`, `baggage`, `none`; others warned | Same; also applied to the runtime propagator `HttpClient` and ASP.NET Core use |
| `OTEL_SDK_DISABLED` | No-op providers, propagators kept, one INFO line | Same; read from `IConfiguration`, as the SDK reads it |
| `OTEL_METRIC_EXPORT_INTERVAL` | `30000`; `^\+?[0-9]+$` (any length), > 0, clamped at 2 147 483 647; code outside [1, 2 147 483 647] fails (A19) | Same; process environment only |
| `OTEL_CONFIG_FILE`, `OTEL_EXPERIMENTAL_CONFIG_FILE` | Not supported: warned once per process (name only), ignored (A19) | Same, once per diagnostics sink (container) |
| `OTEL_HELPER_DEBUG_LEVEL` | `debug=true` on entry spans (no or remote parent) | Same; the tracestate is no longer touched (0.2.x prepended `debug=true`) |
| `OTEL_HELPER_SAMPLE_RATIO`, `OTEL_HELPER_DISABLED_SIGNALS` | Deprecated, warned once → standard vars. A set `OTEL_<SIGNAL>_EXPORTER` (a valid entry, `none` included) — and code metric exporters, for metrics — wins over `OTEL_HELPER_DISABLED_SIGNALS`; its unknown entries warn once (A19) | Same; the deprecation lines are once per diagnostics sink (container), not per process |
| `OTEL_HELPER_EXTRA_INSTRUMENTATION` | Go and Node.js: removed (warns). Python: deprecated, default `SQL` | No effect, warns once: add `StaffOps.OtelHelper.Sql` / `.AWS` / `.Redis` |
| `OTEL_HELPER_DISABLED_METRICS`, `OTEL_HELPER_METRICS_PORT` | Supported (`9464`, `0` = no listener) | Same; the listener binds IPv4 only and answers any method and `/metrics/*` (upstream `HttpListener`) |
| `OTEL_HELPER_HTTP_OPT_IN_METRICS` | `false`: only the two HTTP durations; `true` adds `http.server.active_requests` and the body sizes where upstream has them (A30) | `true` adds `http.server.active_requests` and the .NET client metrics (`http.client.active_requests`, `open_connections`, `connection.duration`, `request.time_in_queue`); no body sizes upstream |
| `OTEL_EXPORTER_PROMETHEUS_PORT` / `_HOST` | Honored (A29): port beats `OTEL_HELPER_METRICS_PORT` (code wins, `0` = no listener); host = bind address, unset = every interface (not the spec's `localhost`) | Same, but no IPv6 bind: `::1` binds `127.0.0.1` (warned), `::` every IPv4 interface (warned), another IPv6 address no listener (one ERROR) |
| gRPC endpoint with a path | Path ignored, warned once per variable, name only (A29) | Same |

## Cross-language parity

- Same env vars, same precedence, same defaults, same emitted attributes,
  same propagators (W3C TraceContext + Baggage), same export interval, same
  health-path filtering (every language shares one list and semantics — design
  D5, A7; .NET since task C11). Emitted attributes
  are the target, not yet the fact: current gaps (Python and Node.js gRPC
  legacy `rpc.*` attributes and no RPC metrics; .NET Redis status, AWS
  attribute values and the `/metrics` exposition) are listed under "Known
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
  `ext/otel{aws,redis,sql}` (Go), `staffops-otel-helper[aws,redis,sql]` (Python — core
  must NOT depend on them), `StaffOps.OtelHelper.{AWS,Redis,Sql}` (NuGet),
  `@staffops/otel-helper-{aws,redis,sql}` (npm; peer-depend on the core at the
  same version). Node.js log libraries likewise: `-pino`, `-winston`.
- **Libraries use version ranges, not exact pins.** Exact pins only in
  example/demo lockfiles. Python contrib betas move in lockstep with the SDK —
  bound both sides as a matched pair.

## Development workflow

- All builds and tests run via Docker (no local SDK dependency):
  `golang` (unreleased 0.3.0 / branch work: go.mod floor `go 1.26`, CI on 1.26.x and 1.27.x; shipped 0.2.0 was `go 1.22`), `python:3.x-slim`,
  `node:<22.15.0|22|24|26>-slim` (`docker compose -f testing/docker-compose.yml run --rm node-tests`),
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
     `README`/`HOW-TO`/`TESTS` (Node.js also a `README.md` per package, which
     `nodejs/ci/check-pack.mjs` requires in each tarball), this file, the MkDocs site (`docs/site/`,
     `mkdocs.yml`) and per-language examples of custom traces, metrics and
     logs — is **mandatory and runs last** (task A13 in
     `specs/config-contract-v0.3/tasks.md`), after everything is validated.
     The pre-commit hook in `.claude/settings.json` reminds you to check
     README, CHANGELOG and the per-language HOW-TOs.
- **Tooling for the loop** ([`specs/repo-tooling/`](specs/repo-tooling/)):
  `testing/verify.sh` (the CI risk matrix locally — run before a tag or PR),
  `tools/spec_lint.py` (the `specs/README.md` rules), `tools/trace_matrix.py`
  (requirement → test matrix), `tools/regen_index.py` (`docs/site/index.md`
  from `README.md`). Claude Code: project skills `change-loop`, `release`,
  `parity-case`; agent `spec-refuter`; hooks in `.claude/hooks/` — a guard
  that asks before Docker prune/rm/`down -v` and force-push/tag deletion, a
  commit reminder, and a session-start summary of open spec tasks.
- Helper diagnostics (config warnings, deprecations): Go → the package's stderr
  `*slog.Logger` (never `slog.Default()`, `fmt.Fprint*`, or `otel.Handle`, which
  is for SDK pipeline errors); Python → `logging.getLogger("staffops_otel_helper…")`;
  Node.js → the diagnostics sink (`report()`, one `otelhelper:` line on
  `process.stderr`; never `console.*` or `process.emitWarning`); .NET → the
  diagnostics sink (`OtelHelperDiagnostics`, one per service provider, one
  `otelhelper:` line on standard error; SDK `EventSource` warnings forwarded
  through it, design D-NET5).
  Never print secrets (`*_HEADERS`, `*_CLIENT_KEY`, URL userinfo/query).
  SDK error text is cleaned too (A29): response bodies dropped, configured
  OTLP header values masked — Go's OTel error handler (wraps an app handler
  set before `Setup`), Python's filter on the exporter loggers, Node.js's
  DiagLogger.
- Commit messages: Conventional Commits with a language scope, e.g.
  `fix(go): ...`, `feat(all): ...`.
