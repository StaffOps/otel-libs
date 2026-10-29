# Node.js OTel helper (Phase B) — requirements

**Status**: Draft r3 (2026-10-29) | **Owner**: repo owner ("tem que acompanhar
as mesmas coisas que as outras 2 linguagens, pois quero paridade"; "assim como
libs acessorias opt-in" — 2026-10-29) | **Relates to**:
[config-contract-v0.3](../config-contract-v0.3/requirements.md) (R1–R10 apply
unchanged), [ADR-004](../decisions/ADR-004-phased-rollout-go-python-first.md),
[ADR-011](../decisions/ADR-011-owner-prefixed-package-names.md),
[ADR-012](../decisions/ADR-012-node-helper-owns-env-resolution.md),
[research](research.md)

> **r2 (2026-10-29)** after the B0 review (`observability` + `dev`): single
> package instance across ESM/CJS; explicit context manager; health sampler
> outside ParentBased (HTTP and gRPC); helper-owned `/metrics` listener;
> ext activation model; effective export deadline; full lifecycle (G1-like
> rollback, re-binding instrumentations); D1 ownership table; prefork
> (`cluster`) model; logging bridges as explicit destinations.
>
> **r3 (2026-10-29)** after the r2 re-review: separate `/hooks` entry for
> custom ESM preloads; the helper's `/metrics` listener is uninstrumented and
> `unref()`'d; foreign global providers follow Python (warn, signal inactive);
> explicit gRPC credentials; cluster port warning; version check on the
> shared state.

## Context

Phase B adds a Node.js helper at parity with Go and Python on the `0.3.0`
contract. Parity is measured by `parity/cases.json` (187 cases, today
`nodejs: skip:phase-b`). OpenTelemetry JS 3.0 / 0.300 reads no env in its
stable SDK, so the helper composes the SDK and owns env resolution (ADR-012).

## Scope

Packages (ADR-011): core `@staffops/otel-helper`; opt-in
`@staffops/otel-helper-{aws,redis,sql}` (instrumentation) and
`@staffops/otel-helper-{pino,winston}` (log destinations); parity driver,
unit tests (≥ 90% gate per package), e2e smoke, examples (api → backend gRPC
→ process, custom signals), docs, CI and release wiring.

### Out of scope
- Browser/edge runtimes, Bun, Deno.
- Fastify until `@fastify/otel` supports SDK 3.x (tracked in tasks).
- Runtime metrics (`instrumentation-runtime-node`) — language-specific.

## Cross-language requirements

**R1–R10 apply as written.** A fixture case passes for Node or carries
`xfail:<task>` / `skip:<reason>` with the reason recorded (design table
D-JS14); no case is deleted. Node-specific differences that cannot be closed
are listed in the root README "Known emitted-telemetry gaps".

## Node-only requirements (JS)

### JS1 — Runtime and dependency set
- `engines.node >=22.15.0`; CI on **22.15.0**, 22 latest, 24, 26.
- OTel stable `~3.0.x`, experimental `~0.300.x` (one generation);
  `@opentelemetry/api` a peerDependency with the range the SDK 3.0 packages
  themselves declare; contrib at their current minor; a CI install test with
  the newest `api` the range admits.
- The helper SHALL register the `AsyncLocalStorageContextManager`
  (`@opentelemetry/context-async-hooks`) and its propagators explicitly
  (3.0 registers neither) — also under `OTEL_SDK_DISABLED`.

### JS2 — Composition and helper-owned resolution
- `setup()` SHALL NOT use `NodeSDK`; it resolves every D1 row itself (design
  D-JS3 ownership table) and passes resolved values to every exporter.
- During exporter construction every helper-owned `OTEL_EXPORTER_OTLP[_<SIGNAL>]_*`
  variable (endpoint, headers, timeout, compression, protocol, insecure) SHALL
  be hidden from `process.env`, so rejected values never reach the wire and
  the SDK never warns raw values (R8); certificate/client-key/client-cert
  variables stay visible (gRPC reads them lazily).
- WHEN `OTEL_CONFIG_FILE` is set THEN warn once that it is not supported.
- Settings the 3.0 SDK no longer reads (`OTEL_BSP_*`, `OTEL_BLRP_*`,
  attribute/span limits) SHALL be read by the helper with the spec defaults;
  `OTEL_METRICS_EXEMPLAR_FILTER` is a recorded gap until the SDK exports it.

### JS3 — Diagnostics are loud, never leak
- Helper diagnostics go to one module-level sink writing one line per record
  to `process.stderr` (never `console.*`, never `process.emitWarning`), hookable
  by the parity runner; deprecations go through it too.
- WHEN `OTEL_LOG_LEVEL` is unset and the app did not opt out
  (`installDiagLogger: false`) THEN setup installs a `DiagLogger` at WARN
  writing to the same sink (`suppressOverrideMessage` on re-setup); WHEN set
  THEN that level is used (`OTEL_LOG_LEVEL` is a Node-only row in the env table).
- Secrets SHALL never be printed (R8).

### JS4 — Resource and identity
- The helper resolves identity itself: default < `SERVICE_NAME` /
  `ENVIRONMENT` aliases (deprecated, warn once) < `OTEL_RESOURCE_ATTRIBUTES` <
  `OTEL_SERVICE_NAME` < code, with the contract trim set (` \t\n\r\v\f`) and a
  warning on resource-attribute parse errors; `service.instance.id` per process
  (D4) unless set; no process/host detector attributes by default.

### JS5 — One instance, loading, formats
- One implementation SHALL serve both `require` and `import` (no dual-package
  state split); state lives on a `Symbol.for('staffops.otel-helper.state.v1')`
  global. A test loads the package through both and asserts shared state.
- `setup()` is **synchronous** (usable from a `--require` preload).
- `@staffops/otel-helper/hooks` installs the module-loading hooks (ESM and
  CJS); `@staffops/otel-helper/register` = hooks + `setup()` from env, for
  `node --import` / `--require`; apps using opt-in packages or code options
  preload their own file: `import '@staffops/otel-helper/hooks'` then
  `setup({...})`. WHEN `setup()` runs in an ESM process without the hooks
  THEN it SHALL warn on the sink (later imports would stay uninstrumented). Docs state that modules
  imported before setup are not instrumented and meters obtained before
  setup are permanent no-ops.

### JS6 — Logging
- `createLogger({name, level?})`: level from R4 (`defaultLogLevel(env, debug)`),
  per record OTel Logs API while a logs exporter is active, else one JSON line
  on stdout with the same fields as the Go/Python fallback (no trace ids, as
  Python). `OTEL_LOGS_EXPORTER=console` uses the SDK console exporter (as Go/Python).
- `-pino` / `-winston` export an explicit destination/transport the app
  passes; it applies the same per-record rule; the helper never rewrites app
  destinations (D6: no double ingestion unless the app configures it).

### JS7 — Lifecycle
- `setup()` idempotent (first call wins, returns the frozen resolved options);
  a failed setup releases everything it built, leaves globals as they were
  and makes `metricsHandler()` answer 503 (Go G1); WHEN the app already set a
  global provider THEN setup warns on the sink for that signal, marks it
  inactive in the resolved options and returns normally (Python behavior;
  `/metrics` 503 when the meter provider is foreign) unless `replaceGlobals`
  is set.
- `shutdown(timeoutMs?)` flushes all providers concurrently under one
  deadline, caches its result; setup after shutdown rebuilds and re-binds every
  instrumentation to the new providers.
- No signal handlers installed; docs show the SIGTERM pattern.

### JS8 — Opt-in instrumentation packages
- Each of `-aws`, `-redis` (redis + ioredis), `-sql` (pg + mysql2) exports a
  factory passed to `setup({instrumentations: [...]})`, peer-depends on the core
  at the same version, emits DB semconv per R10 (design D-JS9), never records
  command values or connection strings, and throws the D14-worded error when
  the instrumented library (resolved from the app) is missing.
- Tests run against real protocol endpoints in Docker (postgres, mysql, a RESP
  fake, a local HTTP endpoint for the AWS SDK).

### JS9 — Prefork model
- Under `cluster`, each worker SHALL have its own `service.instance.id` (D4);
  the `fork` fixture kind runs with `cluster.fork()`; docs cover the shared
  9464 port (per-worker port or OTLP only).

### JS10 — Publication
- CI publishes identical versions to npmjs (provenance, trusted publishing)
  and GitHub Packages on `nodejs/vX.Y.Z` tags; the owner claims the `staffops`
  npm org first (ADR-011).

## Acceptance
- [ ] Every fixture case `pass` for Node or `xfail`/`skip` with a reason from
      the D-JS14 table.
- [ ] Per-package coverage ≥ 90%; CI on 22.15.0/22/24/26; e2e smoke green.
- [ ] Examples and docs complete (root README, AGENTS.md, CONSUMING, MkDocs).
- [ ] `observability` + `dev` refutation and `code-review`; CHANGELOG.
