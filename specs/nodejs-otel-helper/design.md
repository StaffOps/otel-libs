# Node.js OTel helper — design

**Status**: Draft r2 (2026-10-29). Requirements: [requirements.md](requirements.md);
evidence: [research.md](research.md) (all citations there).

## Layout and public API (reference)

```
nodejs/  (npm workspaces, private root; package-lock.json committed)
  packages/otel-helper            @staffops/otel-helper   (+ /register, /testing)
  packages/otel-helper-{aws,redis,sql,pino,winston}
  test/parity/  runner.test.cjs + driver.cjs
  example/  protos/notification.proto, node-api, node-backend, node-process,
            node-custom-signals, EXAMPLES.md
```

| Go / Python | Node |
|---|---|
| `Setup(ctx, opts…)` / `setup_telemetry(opts)` | `setup(options?) → Resolved` (sync) |
| `shutdown(ctx)` / `shutdown_telemetry()` | `shutdown(timeoutMs?) → Promise` |
| Python `options.resolved` | returned frozen `Resolved` (`serviceName`, `environment`, `metricExporters`, `metricExportIntervalMs`, `logLevel`, `debug`, …) |
| options (`With*` / `TelemetryOptions`) | camelCase: `serviceName`, `environment`, `otelEndpoint`, `tracesEndpoint`, `metricsEndpoint`, `logsEndpoint`, `otlpProtocol`, `insecure`, `exportTimeoutMs`, `compression`, `metricExporters`, `exportIntervalMs`, `prometheusMetricsPort` (0 = no listener), `disabledMetrics`, `disabledSignals`, `sampleRatio`, `healthPaths`, `debug`, `minimumLogLevel`, `resourceAttributes`, `instrumentations`, `installDiagLogger`, `replaceGlobals` |
| `StartRootSpan` / `start_root_span` | `startRootSpan(name, fn, {tracer?, attributes?})` — runs `fn(span)` on `ROOT_CONTEXT`, ends it, records exceptions + ERROR on throw/reject |
| `GetTracer`/`get_tracer` (default scope `otel-helper`) | `getTracer(name = 'otel-helper')`, `getMeter` |
| `MetricsHandler()` / `metrics_app()` | `metricsHandler(): (req, res) => void` (node:http, express) |
| `NewHTTPHandler`, gRPC options / FastAPI auto | nothing to wrap: http, undici, express, grpc instrumented by setup |
| `NewLogger`, `NewSlogHandler`, `DefaultLogLevel` | `createLogger`, `defaultLogLevel`, `DeploymentEnvironment` type |
| `reset_telemetry` | `@staffops/otel-helper/testing` → `reset()` (tests only) |
| (preload) | `@staffops/otel-helper/hooks` (hooks only), `/register` (hooks + env setup) |
| ext `Instrument(...)` / `instrument_*()` | `awsInstrumentation()`, `redisInstrumentation()`, `sqlInstrumentation()` passed to `setup({instrumentations})` |

## Decisions

Each decision: choice; justification; trade-offs; when wrong; reversal; alternatives.

### D-JS1 — Compose stable providers, context manager and propagators
**Choice**: helper builds `TracerProvider`/`MeterProvider`/`LoggerProvider`,
exporters, `registerInstrumentations`, `AsyncLocalStorageContextManager`
(enabled) and the composite propagator. **Construction order**: hooks and
`registerInstrumentations` first, then credentials, exporters and providers —
the exporter-base credential helpers `require('@grpc/grpc-js')`; loading it
after the gRPC instrumentation is registered is **defensive** — spike B1c F6
found grpc-js still patched when the exporter loads it first (B6 keeps the
assertion that the app's grpc-js is patched when only gRPC OTLP export is
configured).
**Justification**: ADR-012; 3.0
registers no context manager (without it no in-process parent link).
**Trade-offs**: more code than `NodeSDK`. **When wrong / reversal / alternatives**: ADR-012.

### D-JS2 — Version set
**Choice**: `~3.0.x` / `~0.300.x`; api peer = the SDK's own peer range;
contrib current minor; lower-deps CI leg via a checked-in `ci/lower.json`
`overrides` (npm has no lowest-resolution mode). **Justification**: internal
exact pins; duplicate SDK copies otherwise. **Trade-offs**: every SDK release
needs a matched bump. **When wrong**: upstream relaxes pins. **Reversal**:
caret ranges + lower/latest legs. **Alternatives**: carets now (rejected:
duplicate copies).

### D-JS3 — Resolution and ownership (Go model)
**Choice**: one resolver produces a frozen `Resolved`; the ownership table
below lists every D1 row and edge-case ruling with its Node owner and the
parity cases that prove it (`tools/trace_matrix.py` reads the case ids).
Exporters get resolved values; helper-owned OTLP env vars are hidden during
construction (synchronous; restored in `finally`); TLS files stay visible.
**Justification**: research matrix — rows partial/missing/divergent.
**Trade-offs**: masking touches `process.env` briefly (single-threaded; no
`await` inside). **When wrong**: an exporter starts reading masked vars after
construction. **Reversal**: pass explicit values for that var and drop its
masking; B13 checklist re-checks every exporter release. **Alternatives**:
document the env fallbacks (rejected: R8 leak, rejected headers on the wire).

**Ownership table (reference)** — owner H = helper, S = SDK/exporter:

| Row / ruling | Owner | Notes |
|---|---|---|
| identity (`OTEL_SERVICE_NAME`, resource attrs, aliases, trim set, warnings) | H | JS4 |
| `deployment.environment.name` / ADR-003 | H | |
| `service.instance.id` | H | D4 |
| generic/per-signal endpoints, `/v1/<signal>`, per-signal → generic → default fallback | H | |
| schemeless → `https://` + default port; portless gRPC → `:4317` + warning | H | |
| gRPC `http://h:80` → target `h:80` + insecure credentials | H | `URL.host` drops :80 → grpc-js 443 |
| gRPC channel credentials | H | always passed explicitly — `createInsecureCredentials` / `createSslCredentials` of `@opentelemetry/otlp-grpc-exporter-base` (bounded `~0.300.x` like the other experimental packages — *corrected 2026-10-29 (spike B1c F4): an exact pin would duplicate the copy the exporters already pin exactly on a patch release; one copy, asserted by `nodejs/ci/api-range-install.sh` (path count per package)*), cert/key files read by the helper — so TLS never depends on `process.env` after construction (spike check in B1c). Invalid input never throws from `setup()` (R8), mirroring the SDK's lazy path: unreadable file → warn naming the variable, continue without it; only one of client key / cert → warn, server-auth only; `createSsl` throws (bad PEM, key/cert mismatch) → warn, fall back to system roots. Fixture cases in B4b |
| userinfo/query strip; unsupported/opaque schemes → signal not configured; control chars → `<unparseable URL>` | H | |
| IPv6 brackets kept; zone ids → warn `<unparseable URL>`, fall back | H | Node-only limitation (WHATWG URL rejects zone ids; Go accepts) — D-JS14 |
| protocol (default grpc, 4318 inference INFO once, `http/json` warn+ignore) | H | |
| INSECURE parse (warn on garbage), scheme-vs-INSECURE conflict warning, certificate + plaintext warning | H | TLS selection itself S (spec-compliant) |
| certificates / mTLS files | S (HTTP) / H (gRPC) | HTTP exporter reads them per spec; for gRPC the helper reads the files to build explicit credentials (row above) |
| headers (own parser, percent-decoding rules, key rules, last-wins, lowercase) | H | masked env |
| timeout (plain-decimal ms, clamp [1 ms, 24 h]) | H | |
| effective deadline: BSP/BLRP `exportTimeoutMillis` = explicitly set `OTEL_BSP_EXPORT_TIMEOUT` / `OTEL_BLRP_EXPORT_TIMEOUT`, else the resolved OTLP timeout; metric reader timeout = min(timeout, interval) + warning when capped | H | Go always overrides BSP env, Python lets the SDK read it — contract follow-up (config-contract tasks); Node difference recorded (D-JS14) |
| compression (case-insensitive, default gzip) | H | |
| `OTEL_{TRACES,METRICS,LOGS}_EXPORTER` (`none` semantics, `console`, `prometheus`, `logging`→console, env otlp w/o endpoint warns / code fails) | H | |
| sampler (six names, trimmed, case-insensitive, plain-decimal arg, code ratio wins) | H | |
| propagators (lowercase, b3 warned, `none`) | H | |
| `OTEL_SDK_DISABLED` (boolean rules; propagators + context manager kept) | H | |
| `OTEL_METRIC_EXPORT_INTERVAL` (default 30 000) | H | |
| `OTEL_SEMCONV_STABILITY_OPT_IN` | H | D-JS9 |
| `OTEL_BSP_*`, `OTEL_BLRP_*`, limits | H | spec defaults |
| `OTEL_METRICS_EXEMPLAR_FILTER` | gap | not exported by sdk-metrics 3.0 |
| code values: set-in-code wins whatever the value; out-of-range / contradicting URL fail fast | H | |
| deprecations D8 (`SERVICE_NAME`, `ENVIRONMENT`, `OTEL_HELPER_SAMPLE_RATIO`, `OTEL_HELPER_DISABLED_SIGNALS`, schemeless http/protobuf; `OTEL_HELPER_EXTRA_INSTRUMENTATION` accepted, no effect, warned — as Go G9) | H | |
| `OTEL_LOG_LEVEL`, `OTEL_CONFIG_FILE` | H | Node-only rows |

### D-JS4 — Resource
**Choice**: helper-resolved identity attributes + `telemetry.sdk.*`
(`defaultResource()`) + service instance id; no detectors. **Justification**:
env detector untrimmed and silent on errors; `command_args` leaks secrets.
**Trade-offs**: no pid/host on the SDK side (Collector enriches, principle 4).
**When wrong**: platform needs SDK-side host attrs. **Reversal**: opt-in
`resourceDetectors`. **Alternatives**: SDK env detector (rejected above).

### D-JS5 — Diagnostics
**Choice**: one stderr sink `otelhelper: <msg> key=value…`, hookable; the
helper's default `DiagLogger` (WARN, unless `OTEL_LOG_LEVEL` or opt-out)
writes to it; the parity driver hooks the sink (not `diag`).
**Justification**: `diag` no-op by default; Go/Python surface SDK errors on
stderr. **Trade-offs**: overrides an app `diag` logger set earlier (both
loggers get a warning — documented). **When wrong**: apps routinely own `diag`.
**Reversal**: default `installDiagLogger` to false. **Alternatives**:
`console.warn` (patched by apps), `process.emitWarning` (wrong channel).

### D-JS6 — Logging
**Choice**: core helper logger (JS6); `-pino` exports `createPinoDestination()`
and `-winston` an `OtelHelperTransport`, both applying the per-record rule;
contrib instrumentations used only for trace-context injection, with log
sending disabled. **Justification**: instrumentation-pino always tees via
multistream; app destinations can't be told apart. **Trade-offs**: apps add
one line of config. **When wrong**: users expect zero-config bridges.
**Reversal**: enable contrib log sending and record the D6 tee as a gap.
**Alternatives**: rewriting app destinations (rejected: intrusive, undecidable).

### D-JS7 — Instrumentation and health (D5)
**Choice**: core registers `instrumentation-http`, `-undici`, `-grpc`,
`-express` (`ignoreLayersType: ['middleware']`). The health sampler **wraps
the whole resolved sampler (outside ParentBased)** — as Go `health.go`:
SERVER spans with no or remote parent matching `url.path` (D5 normalization)
→ NOT_RECORD keeping the parent's tracestate; gRPC: span name
`grpc.<svc>/<method>` with `grpc.` stripped, `grpc.health.v1.Health/*` —
SERVER with no/remote parent and CLIENT with any parent; HTTP clients via
`ignoreOutgoingRequestHook` / undici `ignoreRequestHook` (D5-normalized path),
which also exclude the resolved OTLP endpoints (the exporter's own traffic
would otherwise count in `http.client.request.duration`). gRPC has no RPC
metrics → `skip:no-upstream-rpc-metrics` (as Python).
**Justification**: research facts (url.path at start; ignoreGrpcMethods matches
the method only; ignoreIncomingRequestHook drops metrics). **Trade-offs**:
name-based gRPC match depends on the span-name format (asserted by a unit
test). **When wrong**: span name format changes. **Reversal**: gRPC server
interceptor filter. **Alternatives**: `ignoreGrpcMethods` (rejected: drops
every service's Check/Watch).

### D-JS8 — `/metrics`
**Choice**: `exporter-prometheus` with `preventServerStart: true` always; the
helper owns `http.createServer(metricsHandler)` listening with **host
omitted** (Node picks `::` dual-stack when IPv6 exists, else `0.0.0.0`), port
`OTEL_HELPER_METRICS_PORT` (default 9464, 0 = none), serving only `/metrics`
and **excluded from HTTP instrumentation** (the server instance is in a
WeakSet checked by `ignoreIncomingRequestHook`, which also drops its metrics —
Go's listener has no otelhttp, Python's is an uninstrumented wsgiref; a test
asserts two scrapes leave the HTTP server duration count unchanged; the parity
driver — which runs with `OTEL_HELPER_METRICS_PORT=0` — calls `metricsHandler`
on an uninstrumented server it owns, so a scrape never counts itself); `server.on('error')` is
attached **before** `listen` and the server is **`unref()`'d** after it, so it
never keeps the process alive (Go goroutine / Python thread parity; a test
asserts a script that calls `setup()` and returns exits by itself). Because
`setup()` is synchronous, a busy port is a loud error on the sink and
`metricsHandler` stays usable — a recorded Node difference (Go fails Setup).
Under `cluster` workers with a non-zero port the helper warns (the primary
shares the handle round-robin: each scrape hits a random worker). The handler
applies the D13 status table and serializes `exporter.collect()` itself with
Prometheus unit translation (`s`→`seconds`, `ms`→`milliseconds`,
`By`→`bytes`, `1`→`ratio` on gauges, `{…}` annotations dropped, unit before
`_total`), so names match Go/Python; new fixture assertions check the HTTP
histogram name and one counter with a unit. **Justification**: exporter listens on `localhost`
(unreachable from a pod scrape) and drops units. **Trade-offs**: one more
server in the helper. **When wrong**: upstream adds unit suffixes / host option
semantics change. **Reversal**: drop the adapter / own server. **Alternatives**:
async setup (rejected: breaks `--require` preload).

### D-JS9 — Semconv opt-in
**Choice**: helper parses `OTEL_SEMCONV_STABILITY_OPT_IN` (default
`http,database/dup`): `database/dup` → a SpanProcessor `onStart` copies
`db.system.name` → `db.system` on the helper's DB instrumentation spans
(value mapping table, flag differing vendors); `database` → new keys only;
`http/dup` or old-only values → warn once (HTTP is stable-only in 0.300) and
a recorded gap. Metric-attribute duplication: recorded gap if contrib metrics
lack it (B10 verifies). **Justification**: contrib ignores the variable.
**Trade-offs**: copy logic per instrumentation. **When wrong**: contrib adds
the opt-in. **Reversal**: drop the processor. **Alternatives**: gap only
(rejected: contract default is dup).

### D-JS10 — Lifecycle and single instance
**Choice**: implementation compiled once to CJS by `tsc`; ESM entry is a thin
wrapper re-exporting the CJS named exports; state on a `Symbol.for` global.
`setup` sync, rollback on failure (G1), does not override foreign globals unless
`replaceGlobals` (foreign providers: warn per signal, signal inactive,
setup returns — Python parity); the global state records the helper version and a second
copy of a different version reports an error on the sink on first access and
stays inactive (no throw — a preload must not crash the app); `shutdown` =
`Promise.allSettled` raced with an `unref()`'d
timer, result cached; re-setup calls `trace/metrics/logs/propagation/context
.disable()`, rebuilds, and re-binds instrumentations (`setTracerProvider`,
`setMeterProvider`, `setLoggerProvider`). **Justification**: dual-package
hazard (two states); 3.0 globals refuse duplicates; instrumentations keep
their providers. **Trade-offs**: CJS core under an ESM wrapper (`require(esm)`
not needed). **When wrong**: ESM-only consumers need tree-shaking.
**Reversal**: ESM-only build relying on `require(esm)` (Node ≥ 22.12).
**Alternatives**: separate ESM/CJS builds (rejected: hazard).

### D-JS11 — Loading, packaging, registry
**Spike B1c (2026-10-29, `spike-B1c.md`)**: all gates pass on 22.15.0
(async hooks), 22.23.4, 24.22.2, 26.13.0 (sync hooks); `installHooks()` is a
no-op on Node's internal hooks thread (preloads also run there); iitm 3.5.3
sync hooks from a `--require` preload crash `node -e` on 24.22.2 (file entry
points fine; upstream bug, documented).
> **Amended 2026-10-29 (B1 review).** F1 reproduces on every sync-hooks
> version (22.23.4, 24.22.2, 26.13.0), including `NODE_OPTIONS=--require=…/register`
> + `node -e` (container HEALTHCHECKs). `installHooks()` therefore never installs
> sync hooks under `--eval`/`--print`: the async `module.register` loader before
> Node 26, no ESM hook (`cjs-only`) plus one diagnostic line on Node >= 26
> (DEP0205). iitm range `^3.5.3` (a caret subset of instrumentation's `^3.0.0`,
> so one copy), checked by the install test.
**Choice**: the `/hooks` entry installs import-in-the-middle sync hooks when
`supportsSyncHooks()` else `module.register` (iitm minor pinned); `/register`
imports `/hooks` then calls `setup()`; `setup()` warns when it runs in an ESM
process without the hooks; a B9 test runs an ESM app with a custom preload +
`-sql` and asserts a pg span; go/no-go
spike in B1c on 22.15.0/22/24/26. Opt-in packages are separate (peer on core,
same version). Tooling: npm workspaces, `tsc`, eslint + typescript-eslint,
`publint`, `@arethetypeswrong/cli`, `node:test` against `dist/`, `c8` per
package (≥ 90%). Publish npmjs (trusted publishing/provenance) + GitHub
Packages, tags `nodejs/vX.Y.Z`. **Justification**: research; hazard. **Trade-offs**:
two registries. **When wrong**: trusted publishing unavailable for a new
package's first publish. **Reversal**: one bootstrap publish with a scoped
token, then trusted. **Alternatives**: pnpm (extra tool), tsup (extra tool).

### D-JS12 — Parity runner
**Choice**: `node:test` parent (`runner.test.cjs`) ports the Python runner:
env reset (`OTEL_*`, `SERVICE_NAME`, `ENVIRONMENT` removed;
`OTEL_HELPER_METRICS_PORT=0`), placeholders, `_dest_ips` filtering, matcher,
strict xfail/skip, `PARITY_REQUIRE_FAKEOTLP`, `PARITY_RELEASE`. Children via
async `spawn` (`driver.cjs`, `require` of http/express/grpc after `setup`):
non-export cases concurrent (`availableParallelism()`), export cases serial
(shared receiver log); child timeout 120 s, in-driver deadline 75 s for
export, `process.exit()` so `NODE_V8_COVERAGE` is written. B2 rewrites
`nodejs: skip:phase-b` to `skip:B<n>-<reason>` per case; each task flips its
own; B2 also adds `JS<n> pending B<x>` lines to `tools/trace-allow.txt`,
each task deleting its own line. Fixture additions (`export.grpc_http_explicit_port_80`,
the `/metrics` name assertions) run in the Go and Python suites in the same
change (`pass` or a recorded status). Compose `fakeotlp-node` + `node-tests` (`node:${NODE_VERSION}-slim`).
`tools/trace_matrix.py` learns `JS`, `.cjs/.mjs/.js/.ts`, `test(`/`it(`/
`describe(` starts, `nodejs/**/test/**`, excluding `node_modules`.
**Justification**: Go/Python runners (serial export only). **Trade-offs**:
spawn cost (unmeasured; Python ≈ 3.5–5 min/leg). **When wrong**: too slow.
**Reversal**: batch non-export cases per child. **Alternatives**: one process
(rejected: set-once globals).

### D-JS13 — Prefork (`cluster`)
**Choice**: `fork` kind = `cluster.fork()` twice; per-worker instance ids;
docs: per-worker metrics port or OTLP-only under cluster. **Justification**:
cluster/pm2 are Node's prefork model (not `skip:no-prefork-model`).
**Trade-offs**: shared 9464 is unusable under cluster. **When wrong**: n/a.
**Reversal**: skip with reason. **Alternatives**: skip (rejected: real model).

### D-JS14 — Known Node differences (reference; each with a fixture status)
| Difference | Status |
|---|---|
| gRPC RPC metrics absent (`grpc.health_*_counted_in_metrics`) | `skip:no-upstream-rpc-metrics` (as Python) |
| Busy `/metrics` port doesn't fail sync setup | recorded; loud error |
| Metric reader timeout capped at the interval | recorded; warning |
| `OTEL_METRICS_EXEMPLAR_FILTER` not honored | recorded gap |
| `http/dup` / old HTTP semconv not available | recorded gap |
| DB metric attributes without `db.system` (if B10 confirms) | recorded gap |
| Fastify not instrumented | recorded until `@fastify/otel` supports SDK 3 |
| IPv6 zone ids in endpoints rejected (WHATWG URL) | recorded; warning + fallback |
| `cluster` workers share the metrics port (round-robin scrapes) | warning; docs recommend per-worker port or OTLP |
| BSP/BLRP export-timeout env precedence differs between Go and Python | contract follow-up; Node: explicit env wins, else OTLP timeout |
| gRPC over TLS to an **IP-literal** endpoint fails on Node 26 (Node refuses an IP as TLS ServerName; grpc-js sets it from the target host; DEP0123 warning on 22/24) — spike B1c F5 | **proposed** (decided in B2/B4b): helper warns for IP-literal gRPC TLS endpoints; fixture TLS receivers resolved by DNS name (fakeotlp cert SAN, shared with Go/Python) or `skip:node26-ip-servername` on the 26 leg |
| `module.register` runtime-deprecated on Node 26 (DEP0205) — spike B1c F3 | `/hooks` uses sync hooks there; the `module.register` fallback runs only on versions where it is not deprecated |
