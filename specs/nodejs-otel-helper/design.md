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
| options (`With*` / `TelemetryOptions`) | camelCase: `serviceName`, `environment`, `otelEndpoint`, `tracesEndpoint`, `metricsEndpoint`, `logsEndpoint`, `otlpProtocol`, `insecure`, `exportTimeoutMs`, `compression`, `metricExporters`, `exportIntervalMs`, `prometheusMetricsPort` (0 = no listener), `disabledMetrics`, `disabledSignals`, `sampleRatio`, `healthPaths`, `debug`, `minimumLogLevel`, `resourceAttributes`, `instrumentations`, `installDiagLogger`, `replaceGlobals`; test/advanced (never a fixture `code` key): `spanProcessors` |
| `StartRootSpan` / `start_root_span` | `startRootSpan(name, fn, {tracer?, attributes?})` — runs `fn(span)` on `ROOT_CONTEXT`, ends it, records exceptions + ERROR on throw/reject *Implemented 2026-11-03 (B11b, ruled by the owner): `startRootSpan(name, fn, {tracer?, attributes?, kind?, links?})` in `src/internal/api.ts` starts the span with `root: true` on the **active** context rather than `ROOT_CONTEXT`, so baggage and other context values are kept as in Go (`WithNewRoot` on `ctx`) and Python (invalid span set on the current context); the sampler sees no parent; default kind INTERNAL; attributes at start; sync and async `fn` (ended when the promise settles); throw/reject → exception event + ERROR `"<name>: <message>"` (Python `start_as_current_span` format; Go records nothing itself), rethrown* *Fix round 2026-11-03: the result is always a real `Promise` for a thenable (`Promise.resolve(result).then(…)`: settles once, a throwing `then` is a rejection), overloads give `Promise<T>` for a `PromiseLike<T>` (type checks in `test/types/api.test-d.ts`); a non-string name or non-function `fn` throws `TypeError`; recording never throws and the span always ends. Caveats: `exception.type` is the error's `name` (the event is recorded from a plain `{name, message, stack}`), whereas the SDK's own `recordException` and the instrumentations prefer `code` (gRPC `14`, errno `ECONNREFUSED`); and `err.name` is what the error sets — a subclass that does not set `name` reports `Error`, where Python writes the class name and Go the type* |
| `GetTracer`/`get_tracer` (default scope `otel-helper`) | `getTracer(name = 'otel-helper')`, `getMeter` *Implemented 2026-11-03 (B11b): an empty name also gives `otel-helper` (Go `name[0] != ""`, Python `name or`); an optional `version` is passed through* |
| `MetricsHandler()` / `metrics_app()` | `metricsHandler(): (req, res) => void` (node:http, express) |
| `NewHTTPHandler`, gRPC options / FastAPI auto | nothing to wrap: http, undici, express, grpc instrumented by setup |
| `NewLogger`, `NewSlogHandler`, `DefaultLogLevel` | `createLogger`, `defaultLogLevel`, `DeploymentEnvironment` type |
| `reset_telemetry` | `@staffops/otel-helper/testing` → `reset()`, `setDiagnosticSink()`, `getResource()` (tests only) |
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
*Amended 2026-10-29 (B4b review): the gRPC exporter packages are loaded only
when a gRPC signal is configured (they load `@grpc/grpc-js`, so
`require('@staffops/otel-helper')` never does). The construction order is
therefore not load-bearing for the helper's own grpc-js load; an app that
loads grpc-js later is patched by the instrumentation's require hooks (spike
B1c F6) — B6 keeps the assertion.*
*Amended 2026-11-02 (B6; rewritten in the B6 fix round, same day): the
instrumentations are built and enabled **after** the providers are
registered. Their constructors read the API globals (`trace.getTracer`,
`metrics.getMeter`, `logs.getLogger`), so building them after registration
means they never resolve a global before the helper has decided ownership;
the cost is that the gRPC exporters' grpc-js is loaded before the
instrumentation is enabled. Nothing is lost by that: the first enable
re-requires the cached main entry of every instrumented package (express,
`@grpc/grpc-js`), which patches their prototypes in place —
`test/instrumentation.test.cjs` asserts the app's grpc-js is patched (client
and server) when only gRPC OTLP export is configured, and is not reported as
"loaded before setup()" (the helper loaded it). The real trade-off is the
app's own early loads: in CommonJS, express or grpc-js required before
`setup()` was silently left uninstrumented by require-in-the-middle (it
patches on `require`); upstream's `_warnOnPreloadedModules` never fires there
because it reads `globalThis.require`, undefined in CJS and ESM modules
(`instrumentation/dist/platform/node/instrumentation.cjs:75-81`). The fix
round re-requires them and reports once per package. *Reworded 2026-11-02
(B6 dev re-probe M1): the earlier text told users to use `/register`, which
does not call `setup()` until B9 and so fired on a correct run; it now reads
`<module> was loaded before setup(); apps, routers and clients created before setup() may lack spans or have an incomplete http.route — call setup() in a preload (e.g. --import ./otel.mjs that imports the hooks entry and calls setup()) before the app loads`. Not reported when the loader hooks are installed and the
package was imported through ESM (import-in-the-middle already handed it to
the instrumentation; Node gives an ESM-loaded CJS `Module` no `parent`); CJS
cache hits are still reported.* *Reworded 2026-11-02 (B9): `/register` now
calls `setup()`, so the report names it as the cure again:
`<module> was loaded before setup(); apps, routers and clients created before setup() may lack spans or have an incomplete http.route — start the process with --import @staffops/otel-helper/register (or a preload that imports @staffops/otel-helper/hooks and calls setup()) so setup() runs before the app loads`.*
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
**Trade-offs**: masking touches `process.env` briefly (`SHARE_ENV` workers see the mask, D-JS14; single-threaded; no
`await` inside). **When wrong**: an exporter starts reading masked vars after
construction. **Reversal**: pass explicit values for that var and drop its
masking; B13 checklist re-checks every exporter release. **Alternatives**:
document the env fallbacks (rejected: R8 leak, rejected headers on the wire).

> **Corrected 2026-11-04 (B14, obs M2).** The Choice sentence above is
> wrong on two points: the table does not list every D1 row (the rows added
> by contract tasks A21–A28, the resource-detector variables and
> `telemetry.sdk.*` are missing) and it names no parity cases —
> `tools/trace_matrix.py` reads requirement ids (`JS<n>`, `R<n>`) from the
> tests and `parity/cases.json`, not case ids from this table. The
> maintained record is: contract design D1 and its edge-case rulings, whose
> rows carry their Node behaviour in dated notes (each later row names its
> Node task, e.g. "Node B10c ruling Q1"), plus the requirement → test matrix
> of `python3 tools/trace_matrix.py`. This table stays as the B2-era
> reference of the original rows and is not extended row by row.

**Ownership table (reference)** — owner H = helper, S = SDK/exporter:

| Row / ruling | Owner | Notes |
|---|---|---|
| identity (`OTEL_SERVICE_NAME`, resource attrs, aliases, trim set, warnings) | H | JS4 |
| `deployment.environment.name` / ADR-003 | H | |
| `service.instance.id` | H | D4 |
| generic/per-signal endpoints, `/v1/<signal>`, per-signal → generic → default fallback | H | |
| schemeless → `https://` + default port; portless gRPC → `:4317` + warning | H | |
| gRPC `http://h:80` → target `h:80` + insecure credentials | H | `URL.host` drops :80 → grpc-js 443 |
| gRPC channel credentials | H | always passed explicitly — `createInsecureCredentials` / `createSslCredentials` of `@opentelemetry/otlp-grpc-exporter-base` (bounded `~0.300.x` like the other experimental packages — *corrected 2026-10-29 (spike B1c F4): an exact pin would duplicate the copy the exporters already pin exactly on a patch release; one copy, asserted by `nodejs/ci/api-range-install.sh` (path count per package)*), cert/key files read by the helper — so TLS never depends on `process.env` after construction (spike check in B1c). Invalid input never throws from `setup()` (R8), mirroring the SDK's lazy path: unreadable file → warn naming the variable, continue without it; only one of client key / cert → warn, server-auth only; `createSsl` throws (bad PEM, key/cert mismatch) → warn, fall back to system roots. Fixture cases in B4b. *Amended 2026-10-29 (B4b): grpc-js `createSsl` does not throw for a CA buffer without a certificate (empty, garbage, broken base64) — it silently trusts nothing — so the helper checks that the CA file holds at least one certificate `crypto.X509Certificate` parses; if not → warn naming the variable, system roots (Go: `create tls cert pool` error, system roots). Unreadable file: the warning names the variable and the errno, never the path. Fixture cases `export.grpc_certificate_unreadable_falls_back` and `export.grpc_certificate_not_pem_falls_back` pass in all three languages; a key-without-certificate case was not added: Go ignores the incomplete pair silently (server authentication works), Python's grpc aborts the process (`grpc_tls_certificate_distributor.cc` CHECK) — open, needs a contract ruling*. *Superseded 2026-10-29 by the contract D1 row "TLS files" (A18): each file per-signal then generic with fallback (M2), the pair checked with `tls.createSecureContext` before use, a pair gRPC still rejects retried with the CA only (M1), then the system roots; the same material goes to the http/protobuf exporters through `httpAgentOptions`, and the TLS variables are masked. Cases `export.grpc_client_key_without_certificate_server_auth`, `export.grpc_client_pair_mismatch_server_auth`, `export.grpc_certificate_per_signal_bad_generic_good` pass in all three languages* |
| userinfo/query strip; unsupported/opaque schemes → signal not configured; control chars → `<unparseable URL>` | H | |
| IPv6 brackets kept; zone ids → warn `<unparseable URL>`, fall back | H | Node-only limitation (WHATWG URL rejects zone ids; Go accepts) — D-JS14 |
| protocol (default grpc, 4318 inference INFO once, `http/json` warn+ignore) | H | |
| INSECURE parse (warn on garbage), scheme-vs-INSECURE conflict warning, certificate + plaintext warning | H | TLS selection itself S (spec-compliant) |
| certificates / mTLS files | S (HTTP) / H (gRPC) | HTTP exporter reads them per spec; for gRPC the helper reads the files to build explicit credentials (row above) |
| headers (own parser, percent-decoding rules, key rules, last-wins, lowercase) | H | masked env. *Amended 2026-10-29 (B4b): kept out of `Resolved` (secrets). Superseded the same day by the contract D1 row "Header bytes" (A18): a decoded value must be printable ASCII, else it is skipped at parse time with a warning naming the variable and the item position in the env value (`item #N`); `-bin` keys carry the value's bytes (a `Buffer`, as grpc-js requires)* |
| timeout (plain-decimal ms, clamp [1 ms, 24 h]) | H | |
| effective deadline: BSP/BLRP `exportTimeoutMillis` = explicitly set `OTEL_BSP_EXPORT_TIMEOUT` / `OTEL_BLRP_EXPORT_TIMEOUT`, else the resolved OTLP timeout; metric reader timeout = min(timeout, interval) + warning when capped | H | *Corrected 2026-10-29 (A18, D1 row "Export deadline"): the resolved OTLP timeout is the single deadline of BSP, BLRP and reader; `OTEL_BSP/BLRP/METRIC_EXPORT_TIMEOUT` warn and are not honored. The earlier note "Go always overrides BSP env, Python lets the SDK read it" was wrong: Go passed it only when a timeout was set, and the Python batch processors ignore their timeout. The reader cap stays a Node difference (D-JS14); its warning names the timeout's source (`exportTimeoutMs` or the env variable)* |
| compression (case-insensitive, default gzip) | H | |
| `OTEL_{TRACES,METRICS,LOGS}_EXPORTER` (`none` semantics, `console`, `prometheus`, `logging`→console, env otlp w/o endpoint warns / code fails) | H | *B4c (2026-10-29): `signals.ts`; the console exporters are the SDK ones (`ConsoleSpanExporter`, `ConsoleMetricExporter`, `ConsoleLogRecordExporter`) behind batch processors / a periodic reader, as in Go and Python; `prometheus` is resolved into `Resolved.metricExporters`, its reader and listener are B7. `OTEL_HELPER_DISABLED_SIGNALS` maps to `none` for each listed signal unless that signal's `OTEL_<SIGNAL>_EXPORTER` is set (contract D1 rows "Signal on/off" and "Deprecated `OTEL_HELPER_DISABLED_SIGNALS`", A19). Code `metricExporters` rejects `logging` (contract D1 row "Exporter alias `logging`", A19)* |
| sampler (six names, trimmed, case-insensitive, plain-decimal arg, code ratio wins) | H | *B5 (2026-10-30): `tracing.ts` (`resolveSampler`, `buildSampler`); `Resolved.sampler` `{name, ratio}`; the SDK 3.0 `TracerProvider` reads no sampler variable, the helper always passes one. Code `sampleRatio` throws outside [0, 1] (NaN, non-number). An invalid `OTEL_HELPER_SAMPLE_RATIO` warns and is ignored, as in Go (Python clamped a value above 1 to 1 with a warning: same sampler; *A20, 2026-10-30: one message and no clamp in every language, contract D1 row "Deprecated sample ratio"*). Under `OTEL_SDK_DISABLED=true` the sampler variables are not read (no TracerProvider), as in Go and Python; `Resolved.sampler` is then the default. Env names fold ASCII only (A20)* |
| propagators (lowercase, b3 warned, `none`) | H | *Propagators: B5 (2026-10-29). They must be installed under `OTEL_SDK_DISABLED=true` too (case `propagation.sdk_disabled_propagators_kept`). Implemented 2026-10-30 (`tracing.ts` `resolvePropagators`; `Resolved.propagators`, `[]` = `none`): a `CompositePropagator` of the core W3C propagators, registered unless a propagator is already registered (kept, reported); released with the other globals. `none` with other values warns, as Go does (Python was silent; *A20, 2026-10-30: contract D1 row "Propagator diagnostics" — every token scanned, one warning per skipped token, `using` = the effective list, in every language*). A propagator registered before `setup()` is kept (D-JS14); the helper releases only its own instance (`Active.propagator`)* |
| `OTEL_SDK_DISABLED` (boolean rules; propagators + context manager kept) | H | *B4c: identity is still resolved and warned; no provider, exporter or resource is built, so the API no-op providers stay; `Resolved.sdkDisabled`, all exporter lists `[]`* |
| `OTEL_METRIC_EXPORT_INTERVAL` (default 30 000) | H | *B4c: contract D1 row "Metric export interval" (A19, 2026-10-29): `^\+?[0-9]+$` after trimming, > 0, clamp and code bound 2^31-1 ms* |
| `OTEL_SEMCONV_STABILITY_OPT_IN` | H | D-JS9 |
| `OTEL_BSP_*`, `OTEL_BLRP_*`, limits | H | spec defaults. *A18: a value-length limit of 0 is invalid (warn, next level); a batch size above the queue size warns and uses the queue size* |
| `OTEL_METRICS_EXEMPLAR_FILTER` | gap | not exported by sdk-metrics 3.0 |
| code values: set-in-code wins whatever the value; out-of-range / contradicting URL fail fast | H | |
| deprecations D8 (`SERVICE_NAME`, `ENVIRONMENT`, `OTEL_HELPER_SAMPLE_RATIO`, `OTEL_HELPER_DISABLED_SIGNALS`, schemeless http/protobuf; `OTEL_HELPER_EXTRA_INSTRUMENTATION` accepted, no effect, warned — as Go G9) | H | |
| `OTEL_LOG_LEVEL`, `OTEL_CONFIG_FILE` | H | Node-only rows. *Corrected 2026-10-29 (A19): `OTEL_CONFIG_FILE` is now a contract row (design D1 "`OTEL_CONFIG_FILE`"), warned in every language; `OTEL_LOG_LEVEL` stays Node-only* |

> **Corrected 2026-11-03 (B12).** Since contract task A18 (D1 row "TLS
> files", 2026-10-29) the Choice sentence "TLS files stay visible" and the
> row "certificates / mTLS files | S (HTTP) / H (gRPC)" are wrong: the helper
> reads and validates `OTEL_EXPORTER_OTLP[_<SIGNAL>]_CERTIFICATE`,
> `_CLIENT_KEY` and `_CLIENT_CERTIFICATE` for **both** protocols (per-signal,
> then generic) and passes the result explicitly — gRPC channel credentials,
> HTTPS agent options (`httpAgentOptions`) — and the three variables are
> **masked** with the other helper-owned OTLP variables while the exporters
> are built (JS2 amendment). Owner: H for both protocols.

### D-JS4 — Resource
**Choice**: helper-resolved identity attributes + `telemetry.sdk.*`
(`defaultResource()`) + service instance id; no detectors. **Justification**:
env detector untrimmed and silent on errors; `command_args` leaks secrets.
**Trade-offs**: no pid/host on the SDK side (Collector enriches, principle 4).
**When wrong**: platform needs SDK-side host attrs. **Reversal**: opt-in
`resourceDetectors`. **Alternatives**: SDK env detector (rejected above).
*Amended 2026-10-29 (A16): `telemetry.sdk.*` always come from
`defaultResource()` (env/code values ignored) and the resource declares
`https://opentelemetry.io/schemas/1.43.0`, per the contract design D1 row
"`telemetry.sdk.*` and the resource schema URL".*
*Amended 2026-10-29 (B3, after the B3 review): `OTEL_RESOURCE_ATTRIBUTES`
parsing follows the contract ruling (config-contract design D1 edge-case row
"`OTEL_RESOURCE_ATTRIBUTES` parsing", 2026-10-29; Go/Python in task A16): split
on `,`; blank items ignored; each item split at the first `=`; key and value
trimmed with the contract trim set after percent-decoding; an item without `=`
or with an empty key is skipped with a warning naming its position only; an
invalid percent sequence or invalid UTF-8 keeps that value literal with a
positional warning; values are never echoed (R8). Per the other rows of that
date: a blank `service.name`, `deployment.environment.name` or
`service.instance.id` (env or code `resourceAttributes`) counts as not
provided; the dedicated option (`serviceName`, `environment`) beats the same key
in code `resourceAttributes`, in `Resolved` and in the emitted resource; a
code-only `resourceAttributes['service.name']` resolves without the
missing-name warning. Not in the contract (Node API): unknown `setup()` keys
throw a `TypeError` (a typo must not fall back to the env), so options of later
tasks are rejected until their task lands; a key set to `undefined` counts as
not set; `setup()` reads each option once into a frozen snapshot.*

### D-JS5 — Diagnostics
**Choice**: one stderr sink `otelhelper: <msg> key=value…`, hookable; the
helper's default `DiagLogger` (WARN, unless `OTEL_LOG_LEVEL` or opt-out)
writes to it; the parity driver hooks the sink (not `diag`).
**Justification**: `diag` no-op by default; Go/Python surface SDK errors on
stderr. **Trade-offs**: overrides an app `diag` logger set earlier (both
loggers get a warning — documented). **When wrong**: apps routinely own `diag`.
**Reversal**: default `installDiagLogger` to false. **Alternatives**:
`console.warn` (patched by apps), `process.emitWarning` (wrong channel).
*Amended 2026-10-29 (B2 review): the sink is
`(line, meta: {origin: 'helper' | 'diag', level: 'error' | 'warn' | 'info' | 'debug'})`;
`report()` defaults to a helper warning and the DiagLogger (B3) passes
`origin: 'diag'` with the SDK message's level. The parity driver's
`helper_log` is origin `helper` only; `diagnostics` is warn and above of any
origin plus Node process warnings (Python's "every WARNING+ record and
`warnings.warn`"). Without the origin, forwarded SDK messages would land in
`helper_log`, where R8 redaction is asserted.*
*Amended 2026-10-29 (B3, after the B3 review): a forwarded SDK message is one
record `otelhelper: opentelemetry diag level=<level> msg=<text>`, the text
rendered with `util.formatWithOptions` (depth 2, `maxStringLength` 2048,
`maxArrayLength` 20) and cut at 2048 characters (`… (N more characters)`).
Like every attribute value, `msg` is JSON-quoted whenever it holds whitespace,
quotes, `=`, backslashes or control characters (C0, DEL, C1) or U+2028/U+2029
— the last three escaped as `\uXXXX`, which JSON.stringify does not do — so a
record can never be split or forged. Echoed env values (`OTEL_LOG_LEVEL`,
`OTEL_HELPER_DEBUG_LEVEL`) are cut to 64 characters plus `…`. `verbose` maps to
level `debug`. `OTEL_LOG_LEVEL` is trimmed and case-insensitive (`NONE`,
`ERROR`, `WARN`, `INFO`, `DEBUG`, `VERBOSE`, `ALL`); any other value warns and
uses WARN. The DiagLogger is installed before resolution, so SDK messages
raised while setup builds are captured. `suppressOverrideMessage` is passed only
while `diag` still holds the helper's logger (read from the API's global
registry); a re-setup with `installDiagLogger: false` removes the logger an
earlier setup installed. Foreign globals are detected in that registry before
any `set*Global*` call, so the API's duplicate-registration error (with a stack
trace) never reaches `diag`.*
*Amended 2026-11-04 (B14, code-review M1 and the observability defence-in-depth
item): helper-authored records never go through `diag`. The opt-in packages'
reports (`internal/contrib`: `setConfig()` ignored, a library not loaded or not
resolved) and the `/metrics` collection errors (one ERROR record per scrape:
`count` and the first error, redacted) are `report()` records with `origin:
'helper'` (`report`/`reportOnce` are exported from `internal/contrib` for the
packages); a helper debug record is written only when `OTEL_LOG_LEVEL` is
DEBUG, VERBOSE or ALL (`debugEnabled()`), as a `diag.debug` call was before. The
forwarded SDK text (`msg`) is redacted by `redactText` (the rules of
`redactErrorText`, without its 64-character cut). Tests: core
`test/contrib.test.cjs`, `test/metrics.test.cjs`.*
*Corrected 2026-11-04 (B14 re-probe M1): the record above claimed the
forwarded text was free of credentials; it was not. With
`OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf`, `OTEL_LOG_LEVEL=all` and an endpoint
answering 400/500 with the request headers echoed in its body, the SDK's global
error handler (`@opentelemetry/core` `loggingErrorHandler`: `diag.error(JSON.stringify(flattened error))`)
forwarded the `OTLPExporterError` `data` (the response body, escaped JSON
`\"api-key\":\"…\"`) unredacted: `SECRET_PAIR` missed escaped quotes and
util.inspect single-quoted keys, `credential`/`session`/`signature` names,
`&`-joined pairs and multi-pair `Cookie` values, and an echoed header can have
any name. Now three layers, in order: (1) `withoutResponseBodies` drops a
response body before rendering — an Error with a `data` field becomes
`<name>: <message> (code N) <response body omitted: N characters>`, a
`Uint8Array` its size, the same one level deep in a plain object or array, and a
message string that is the JSON of an error (`stack` or `name`, plus `data`)
gets its `data` replaced; (2) `maskLiterals` replaces the values of
`OTEL_EXPORTER_OTLP_HEADERS` / `OTEL_EXPORTER_OTLP_<SIGNAL>_HEADERS` (read at
each record, raw and decoded, plain and JSON-escaped once and twice; values
under 4 characters skipped) before the 2048-character cut; (3) `redactText`
with `SECRET_PAIR` widened (optional `\"`, `"` or `'` around the name, quoted or
escaped-quoted values, unquoted values ending at whitespace, `,`, `;` or `&`;
names containing `credential`, `session`, `signature` added), a
`COOKIE_HEADER` rule (the whole `Cookie`/`Set-Cookie` value) and a `DIGEST`
rule. Layer 3 stays pattern-based; HOW-TO §14 says so. Tests: core
`test/diagnostics.test.cjs` (inspect, escaped JSON, names, cookie, digest,
bounded on hostile inputs, `maskLiterals`), `test/setup.test.cjs` (each layer
through `diag`; end to end against a local server echoing the headers in a
400 and a 500 body). Go has the same exposure through the SDK default error
handler (backlog row in `specs/config-contract-v0.3/tasks.md` Deferred);
Python logs only the status code and reason phrase.*
*Corrected 2026-11-04 (B14 third round, code-review M2/H1/L2/L3/L4/L5, dev
re-probe L-a/L-b): the correction above names `OTEL_LOG_LEVEL=all` as a
condition; it is not one. `loggingErrorHandler` calls `diag.error`, so the
leak reproduced at the default WARN too; HOW-TO §14 no longer implies that
WARN/ERROR alone prevented it (layer 1 drops the body at every level).
Further changes: `forward()` never throws into the SDK — the whole rendering
is in a try/catch, and an unrenderable message (a throwing getter or
`inspect.custom`, a Symbol `name`, a revoked Proxy) is one record with
`msg="<diag message could not be rendered>"`; the text is redacted before the
2048-character cut; `configuredHeaderValues()` is memoized on the raw
variables, and `maskLiterals` also masks the util.inspect-escaped form;
`SECRET_PAIR` names gain `pwd` and `jwt`, and status words
(`(UN)AUTHENTICATED`, `(un)authori[sz]ed`, `authentication[-_]failed|error|required`)
are kept, so `16 UNAUTHENTICATED: denied` stays readable while
`authorization=…` is still redacted [both rules corrected below, round 4];
backslash runs are bounded (`\\{0,7}`, whole runs only in a quoted value), so
a 4096-backslash line takes well under a millisecond instead of ~50 ms
[corrected below, round 4]; the unquoted `Cookie` value runs to a line
end or a comma (`Cookie: a="b"; sid=…` leaked `sid`). `/metrics` `errorText`
never throws (`<unprintable error>`). The opt-in packages' library reports
have constant messages — `<package>: a library is not instrumented` and
`<package>: a library could not be resolved from the app directories;
instrumented if it loads` — with `library`, `dirs` and the redacted load
`error` (which keeps the "Reinstall …" hint) as attributes.*
*Corrected 2026-11-04 (B14 fourth round, re-review HIGH/MEDIUM/LOW): two of
the third-round rules leaked. (1) The status-word exemption returned the whole
match, so the pairs inside its value leaked (`unauthorized: token=abc`), and
it exempted bare names (`Authentication: x`, `authentication=x`,
`"authenticated":"x"`, `unauthorized=x`, `authentication_failed: x`). Now
`STATUS_NAME` is `^(?:un)?(?:authenticated|authori[sz]ed)$` only (the bare and
suffixed `authentication…` names are dropped), it applies only to an unquoted
name before a `:` separator (never `=`, never quoted), and the value is
redacted again pair by pair (`Cookie` and credential rules; it is shorter
than the match, so the recursion ends): `16 UNAUTHENTICATED: denied` stays
readable, `16 UNAUTHENTICATED:api_key=x` keeps the word and redacts the key.
(2) The `\\{0,7}` bound made a value escaped 4 or more times, or holding a run
of 8+ backslashes, fall through to the unquoted alternative and leak after its
first space. Quotes are `\\*["']` again, the value body
`(?:[^"\\]|\\+(?![\\"]))*` (whole runs: one parse), and a match never starts
inside a backslash run or on a quote right after a backslash
(`(?!(?<=\\)[\\"'])`), which removes the quadratic rescans: 2000 backslashes +
`"key` + 2000 backslashes + `"` takes about 0.1 ms on Node 24 (the bound in
the test is 25 ms); an escaped newline before a name (`\\napi_key=…`) still
matches. A quoted value not closed before the end of the scanned text is
redacted to the end. Forwarded text: twice the 2048-character record is
redacted and the record keeps the first 2048 characters of that, so only
scanned text is written. Library reports: the `error` attribute is the load
error's first line, redacted, cut at 512 code points.
Tests: core `test/diagnostics.test.cjs` "fourth round", `test/contrib.test.cjs`
first-line test.*
*Corrected 2026-11-04 (B14 fifth round, re-review HIGH): the fourth-round
status-word rule kept the name and re-redacted the value, but an unquoted
value is one token, so the next `name: secret` was swallowed as the value of
the word and kept (`401 Unauthorized: x-api-key: v`, `Unauthorized:
authorization: v`, `UNAUTHENTICATED: session: v`, `unauthorized: secret: v`;
and for any credential name, `Authorization: x-api-key: v`). Now status words
never match as a name — `SECRET_PAIR` has
`(?!(?:un)?(?:authenticated|authori[sz]ed)\s*:)` after the start guard, and
`STATUS_NAME` and the recursive `pair` are removed — and the unquoted value is
`(?!<credential name>:\s)[^\s,;&]+`: a credential name followed by `: ` is never
taken as a value, so the scan resumes at it and redacts its own value.
Deviation from the reviewer's suggested `(?![\w-]{1,64}:\s)`: only
credential names are excluded, so `token: abc: def` still redacts `abc`. The
`Cookie` value runs to a line end or comma, so a nested name inside it is
redacted with it. Known pattern limits (HOW-TO §14): a name right after a
letter and an escaped quote, a `"` inside a secret value, escaped separators,
and the unterminated-quote alternative's over-redaction. Tests: core
`test/diagnostics.test.cjs` "fourth round" block, round-5 lines.*

### D-JS6 — Logging
**Choice**: core helper logger (JS6); `-pino` exports `createPinoDestination()`
and `-winston` an `OtelHelperTransport`, both applying the per-record rule;
contrib instrumentations used only for trace-context injection, with log
sending disabled. **Justification**: instrumentation-pino always tees via
multistream; app destinations can't be told apart. **Trade-offs**: apps add
one line of config. **When wrong**: users expect zero-config bridges.
**Reversal**: enable contrib log sending and record the D6 tee as a gap.
**Alternatives**: rewriting app destinations (rejected: intrusive, undecidable).
*Implemented 2026-11-02 (B8, core logger only; `-pino`/`-winston` are B10d):*
`createLogger({name, level?})` (`src/internal/logging.ts`) returns a frozen
logger with `debug`/`info`/`warn`/`error(message, attributes?)`; invalid
options throw a `TypeError` naming the option (R8). The destination is read
**per record** (Go `activeLogBridge`): the active setup's own LoggerProvider
while it is not shut down and has at least one export processor (OTLP and/or
`OTEL_LOGS_EXPORTER=console`, the SDK console exporter); otherwise one JSON
line on `process.stdout`. The level is the logger's `level`, else the level
the last successful setup resolved (`Resolved.logLevel`: `minimumLogLevel`,
else `defaultLogLevel(environment, debug)`), kept after `shutdown()` (Go
`installedLogLevel`), `info` before the first setup; it filters both paths
(contract D6 A13 ruling). Stdout shape: `time` (UTC, ms, `Z`), `level`,
`msg`, `logger`, the attributes, `exception` — see the contract D1 rows
"Stdout fallback time zone" and "Stdout fallback fields" (2026-11-02,
ruled the same day as contract task A23: UTC and reserved keys unified, the
rest accepted). On the OTel path the logger name is the
instrumentation scope (as Python; contract D1 row "OTel log scope name"), the
first `Error` attribute becomes the record `exception`, and the active
context gives trace correlation. Like Go and Python, the helper's loggers
keep exporting through the helper's provider when a **foreign global
LoggerProvider** is registered; the two registration reports for logs say so
(`OpenTelemetry Logs API callers use it, the helper's loggers (createLogger)
still export through the helper's provider`). A logging call never throws.
*Implemented 2026-11-02 (B10d, `-pino` and `-winston`):* **Record emission**:
the packages write through `emitBridgeRecord()` / `recordEnabled()` of a new
**non-public** core subpath, `@staffops/otel-helper/internal/bridge`
(`src/bridge.ts`, CJS + ESM wrapper like the other entries; publint, attw and
`ci/check-pack.mjs` pass), so the per-record rule, the level filter, the
converter and the stdout shape are the core's own code (`src/internal/logging.ts`
`write()`, shared with `emitRecord`), not a copy. The entry is documented as
not part of the API and may change in any release; the packages peer-depend
on the core at the same version, so the two always match. *Alternatives*:
copying the rule (rejected: two stdout shapes to keep equal); a global-symbol
hook on the shared state (rejected: a second, invisible contract); making
`emitRecord` public (rejected by the owner ruling of 2026-11-02). *Reversal*:
if a public extension point is wanted later, promote the entry's
`emitBridgeRecord` under a public name and keep the subpath as an alias for
one minor. The bridge extends the core with `trace` and `fatal`
(`RecordLevel`: TRACE 1 / FATAL 21; stdout DEBUG / ERROR, as Python
`_slog_level`), a source `severityText`, a record `time`, and a **leading**
error (the record's own: the exception on both paths; `Error` attributes are
then written as their stack text, and an attribute named `exception` is left
out on stdout). `trace` ranks below every minimum, so trace records are always
dropped (Go `NewSlogHandler` drops levels below Debug; Python drops levels
below DEBUG by default — *wording corrected 2026-11-03*); the
helper level (the option `minimumLogLevel`, else the installed one, per
record) applies on top of the library's own level, as Go `NewSlogHandler`.
**pino** (`createPinoDestination`): an object destination with
`Symbol.for('pino.metadata')`; it parses the line pino serialized, so
redaction, serializers, formatters and mixins apply (pino's metadata
`lastObj` is the unredacted object, so it is never read), and takes from
the metadata the numeric level, the logger's `messageKey`/`errorKey` (pino
symbols) and its level label (`severityText`). Levels by bucket: ≥60 fatal,
≥50 error, ≥40 warn, ≥30 info, ≥20 debug, else trace. The `name` field is the
logger (else the option `name`, else `pino`); `level`, `time`, `pid`,
`hostname` are not fields (the contrib pino bridge also drops `pid` and
`hostname`); the record time is now (pino writes inside the call; its time
formats vary, `unixTime` is seconds). An error-like object under the error key
(`{type, message, stack}` from pino's serializer) is rebuilt as the leading
error, its stack kept. A line that is not a JSON object becomes the body.
**winston** (`OtelHelperTransport`): extends `winston.Transport`, loaded
when the module loads (a missing winston makes the constructor throw);
`log()` writes then calls `next()` synchronously. Level by name against the
anchors the logger's `levels` define (`error`, `warn`/`warning`, `info`,
`debug`): more severe than `error` fatal (syslog `emerg`/`alert`/`crit`),
then the first anchor at or above it, below `debug` trace (npm `silly`, cli
`prompt`/`verbose`/`input`/`silly`; npm `http`/`verbose` are debug); no
levels or an unknown name: by name, else info. The raw level is read from
`Symbol.for('level')`, so `format.colorize()` does not change it. The info
itself when it is an `Error` (`logger.error(err)`), else the first `Error` of
the splat (`logger.error(msg, err)`, whose copied `stack` field is then left
out), is the leading error; other fields are read by the core through
getters, so a throwing accessor becomes `[unreadable accessor]`. **Contrib
instrumentations: not used** (amends the Choice above, 2026-11-02): the
destination and the transport are called inside the logging call, so the
active context already gives trace correlation, and the contrib injection
writes `trace_id`/`span_id`/`trace_flags` into every record, which would add
fields to the stdout line (the core line has no trace ids, as Python) and
would need `setup({ instrumentations })` (B10a). ~~*Trade-off*: a winston
logger under backpressure from a slower transport buffers the record and
delivers it later, without the caller's context (D-JS14 row of 2026-11-02).~~
*Corrected 2026-11-03 (B10d fix round, HIGH-1): that trade-off was wrong in
kind — a buffered record was written with the context active **at
delivery**, which is another span's (both reviewers reproduced records on
the wrong trace, and a record logged outside any span landing in a span).
Fixed without contrib: see the fix-round note below.* *Reversal*: add
`@opentelemetry/instrumentation-winston` injection
(`disableLogSending: true`) and read its fields as the record context,
removing them from the attributes. **Missing library**: `createPinoDestination()`
/ `new OtelHelperTransport()` throw `<package> is installed but the library
'<lib>' is not. Install it (e.g. npm install <lib>)` for a `MODULE_NOT_FOUND`
of the library itself, else `<package>: failed to load <lib>: <error>`
(contract D14 wording). Peer ranges `pino` `^10.0.0`, `winston` `^3.10.0`
(tested with 10.4 and 3.19). The root `lint` script builds the core first:
type-aware lint of a package that imports the core needs its declarations.
*Fix round 2026-11-03 (B10d; reviews: observability, dev, code-review — no
BLOCKING, 3 HIGH):* **winston context (HIGH-1)**: on the transport's `'pipe'`
event (winston-transport `modern.js` sets `this.parent` there) the transport
wraps that logger's `write` once (marker `Symbol.for('@staffops/otel-helper-winston.wrapped')`)
and stamps each info with the context active at the call and the call time,
under a private, enumerable symbol (so winston's child loggers,
`format.errors()` and transport formats, which `Object.assign` the info,
carry it). `BridgeRecord` gained `context` (passed to `emit({ context })`);
the transport passes the stamp's context, else **`ROOT_CONTEXT`** (an
unstamped record — the transport used outside a logger ~~, winston's exception
handlers~~ — is uncorrelated, never on the span active at delivery; *corrected
2026-11-03, fix round 2: winston's exception handler logs through
`logger.log`, so its records are stamped; test "probe-q3"*), and the
stamp's time as the record time (`BridgeRecord.time` is used for this). The
bridge entry also exports `activeContext()` and `ROOT_CONTEXT`, so the
packages do not depend on `@opentelemetry/api`. Regression test: two
concurrent spans and a slow transport (`highWaterMark: 1`), zero
misattributed records, one record outside a span with no trace ids; against
the old code it fails (records of span B on another trace). **pino one
argument (HIGH-2)**: the destination has `writable: true`, which
`pino(destination)` needs to treat it as the stream (pino `lib/tools.js`).
**Bodies**: a non-string message is converted by the core's attribute
converter — an AnyValue map (or array, number…) body on OTel, its JSON text as
the stdout `msg`; no message: no OTel body and `msg` `""` (createLogger
keeps its string message). **pino**: `messageKey`/`errorKey` are found on
`lastLogger` by their symbols' descriptions (`pino.messageKey`,
`pino.errorKey`) up the prototype chain, so another pino copy works; the
logger name is `lastLogger.bindings().name` (guarded), a per-call `name`
that differs stays a field; `pid`/`hostname` are dropped only when they are
this process's pid and `os.hostname()` (pino's default `base`); the serialized
error's other keys (`statusCode`, `details`, `code`…, i.e. minus
`type`/`message`/`stack`/`name`) stay a map under the error key; the
exception type is a string `name` before `type` (the SDK's `exception.type`
is still the `code` when there is one, core rule); pino's `message` and
`stack` already include the causes (pino-std-serializers). Integers past
`Number.MAX_SAFE_INTEGER` keep their digits as text (`JSON.parse` reviver
with source text, present on the 22.15.0 floor); a chunk with several lines
is one record per line (each line's own level; one line: pino's metadata
level); `flush(cb)` (`logger.flush(cb)`) calls the LoggerProvider's
`forceFlush` first (`flushBridgeRecords()`, never rejects, reports a failure
once); the default export throws "pino.transport({ target }) is not
supported … pass createPinoDestination() to pino() instead" (a worker thread
has no helper setup). **winston**: winston's exception-handler shape
(`info.exception === true && info.error` an Error, from `getAllInfo`) makes
`info.error` the leading error and leaves out `error`, `exception`, `trace`
and the copied `stack`; when no Error is found and `info.stack` is a string
whose first line is `<Name>: <message>` (`format.errors({ stack: true })`
flattened the Error), the error is rebuilt from it and `stack` left out;
`'logged'` is emitted with `setImmediate` before `next()`; `close()` and
`_final()` (before `'finish'`, `logger.end()`) flush the logs pipeline.
**D14**: another load failure keeps only the error message's first line (the
rest is a require stack of local paths), the original as `cause` (as B10a).
**Peer floors**: `ci/api-range-install.sh` installs pino 10.0.0 and winston
3.10.0 next to the packed bridges and writes one record through each; both
package suites also passed against those floors (27/27, 23/23), so the ranges
stay. **Core**: with a leading error an `exception` attribute is left out on
the OTel path too (as on stdout); the bridge symbols carry `@internal`.
*Fix round 2, 2026-11-03 (re-reviews: no BLOCKING; code-review and dev 2 new
HIGH each):* **winston stamp (HIGH-1)** — the enumerable-symbol stamp above
is replaced (it leaked into other transports' output, e.g. `prettyPrint`, and
went stale on a reused object): the stamp lives in a `WeakMap` keyed by the
info object, in a process-wide registry (`Symbol.for('@staffops/otel-helper-winston.registry.v1')`,
non-enumerable on `globalThis`) shared by every installed copy of the
package; nothing is added to the info and no Context sits in an enumerable
property. Every call of the wrapped `logger.write` sets the stamp, replacing
an older one (a reused Error or info gets the current call's context and
time; no re-entrancy flag is needed: winston's child loggers call the
parent's wrapped `write` once, and a nested call is a real call). The
transport overrides `_write` (and `_writev`, chunk by chunk) to look the
incoming info up **before** winston-transport's format copy
(`Object.assign({}, info)`, `modern.js`), so a transport's own format keeps
correlation; a logger-level format that returns a new object
(`format.errors()` for an Error) leaves the record unstamped:
`ROOT_CONTEXT` (D-JS14 row of 2026-11-03). Only a winston logger source
(`typeof src.add === 'function'`) is wrapped; attachments are counted per
logger across transports and copies (`'pipe'` / `'unpipe'`), and the last
detach restores the logger's own `write` (deleting the own property, or
putting back an own `write` the logger had), unless something wrapped it
since. **Overlapping flushes (HIGH-2)**: sdk-logs 3.0
`BatchLogRecordProcessorBase._flushAll` returns at once while another flush
runs (`if (this._flushing) return`), so a concurrent `forceFlush()` — and a
`shutdown()` during a flush — resolved without exporting what it was asked
to. `flushBridgeRecords()` moved to `setup.ts` and is serialized (one in
flight; requests meanwhile share one queued follow-up `forceFlush`), and
`shutdown()` waits for any in-flight or queued bridge flush before shutting
the LoggerProvider down (D-JS14 row of 2026-11-03; an upstream issue is to
be filed). **pino overhead**: the bound name is cached per logger, keyed by
its serialized bindings (pino's `chindings` symbol, found by description),
so `setBindings` invalidates it; the big-integer reviver runs only when the
line has 16+ consecutive digits; the split runs only for a chunk with an
inner newline; pino's own single-line write is level-checked from the
metadata before parsing. **winston exception-handler shape**: the body is
the message's first line; `date`, `process` (its `argv` can carry secrets)
and `os` are left out too. **Stack rebuild**: only a stack with at least one
`\n    at ` frame; heads `Name`, `Name: message`, `Name [CODE]: message`
(Node's coded errors); a bare `Name` takes its message from `info.message`;
the info's `code` (else the bracketed code) is the error's `code`, the SDK's
`exception.type`, as for a live Error. **Core**: `emitRecord` checks the level
before converting the message. **pino ESM types**: `esm/index.d.mts`
declares the default export itself (a re-export of a CommonJS default would
type the whole `module.exports`).
*Final round, 2026-11-03 (re-reviews: no BLOCKING, no HIGH):* **winston
`_transform`**: while attached, the logger's `_transform` is wrapped too: it
looks up the incoming info's stamp and, for that call only, wraps `push` so
an object the logger's format returns instead (`format.errors()` for an
Error) gets the same stamp when it has none; `detach()` restores `write` and
`_transform` (and `push` is put back after every call, an own `push`
included). A sync and a buffered logger-level `format.errors()` in span S
are both on trace S (the D-JS14 row is narrowed to the reused-object case).
The stamp is the call's Context and a Date only; the `WeakMap` keeps it while
the app keeps the info object reachable. **Flushes per provider**:
`flushBridgeRecords()` serializes per LoggerProvider (`Map<LoggerProvider,
{current, queued}>`), so a flush for a new setup's provider does not queue
behind an older provider's hung flush, and `shutdown()` waits only for the
flushes of the provider it shuts down that were pending when it was called
(the queued one, else the current one — no waiting loop, no starvation).
Test: a hung receiver, `shutdown(500)` (times out), `setup()` against a good
receiver, a flush resolves in well under a second and `shutdown()` resolves.
A flush failure's reason is read guarded (a throwing `message` getter gives
the type) and redacted (`redactErrorText`); the flush never rejects.
**`'logged'`** is emitted for every record winston hands the transport,
including one the helper level then drops.

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
*Implemented 2026-10-30 (B5): `HealthCheckSampler` (`tracing.ts`) decides from the span kind, the parent and what the span carries at start — `url.path`, else `http.target` (instrumentation-http 0.300 never sets it: kept as the Go/Python fallback); gRPC by **span name only**, in the instrumentation's form `grpc.grpc.health.v1.Health/<method>` (*corrected 2026-10-30, B5 fix round: instrumentation-grpc 0.300 sets `rpc.*` after start, with the bare method, so no attribute is matched; a user service in package `health.v1`, span `grpc.health.v1.Health/<method>`, is kept; the `/svc/Method` and `svc/Method` forms are only for a full-method value, `isGrpcHealthMethod`, for B6's client exclusion*) — so B6 only has to make the instrumentations start their spans with those; unit-tested directly in `test/tracing.test.cjs`. HTTP clients are not matched by the sampler (B6 hooks). Option `healthPaths` (Go/Python rule: ASCII, leading `/`, no `?#,`; `[]` = none) and `Resolved.healthPaths`. The debug marker is a span processor placed before the exporters' (R9)*
*Implemented 2026-11-02 (B6): `instrumentation.ts`. The four instrumentations
are built once per process (kept on the shared runtime), each constructor's
self-enable undone at once (not `enabled: false`: instrumentation-undici
checks `config.enabled` on every request), then bound with
`setTracerProvider`/`setMeterProvider` — the helper's providers when they are
the globals, else the global ones (a foreign provider) — and enabled by every
setup; `shutdown()` disables them (Python uninstruments at shutdown); none
under `OTEL_SDK_DISABLED=true` (Python: no instrumentation) or a foreign API
registry. After enabling, `node:http`/`node:https` are required once more so a
copy the app loaded before `setup()` is patched now. Hooks:
`ignoreIncomingRequestHook` = `isHelperServer(req.socket.server)` only;
`ignoreOutgoingRequestHook` and undici `ignoreRequestHook` = a health path
(D5 normalization) or one of the helper's OTLP/HTTP endpoints (scheme, host,
port with the scheme default, path; query ignored; only signals exported over
OTLP). The OTLP filter is needed although the SDK exports under
`suppressTracing`: instrumentation-http 0.300 records
`http.client.request.duration` even then (`_closeHttpSpan`). gRPC OTLP needs
none (http2 is not instrumented by instrumentation-http; instrumentation-grpc
records no metrics; its spans are suppressed). gRPC client health calls are
dropped by the sampler (CLIENT spans named `grpc.grpc.health.v1.Health/…`), so
`isGrpcHealthMethod` is not used by B6. Asserted by tests: `url.path` at span
start, the gRPC span names `grpc.<service>/<method>` seen by the sampler on
client and server, two scrapes of the listener leave the HTTP server count
unchanged. `OTEL_SEMCONV_STABILITY_OPT_IN` (D-JS9) is not parsed by B6 (task
B10a); instrumentation-http 0.300 is stable-only.*
*Amended 2026-11-02 (B6 fix round, after observability, dev and
code-review): (1) the first enable re-requires express / grpc-js loaded
before `setup()` and reports them (see D-JS1); every enable requires
`node:http`/`node:https` again and calls `module.syncBuiltinESMExports()`
(also after disabling), so `import { request } from 'node:http'` taken
before `setup()` sees the patched function; (2) express ignores the
`router` layer too (zero-duration `router - <path>` spans; request-handler
spans kept); (3) undici `startSpanHook` rewrites `url.full` as
instrumentation-http writes it for `node:http` clients (userinfo
`REDACTED:REDACTED`, the values of `sig`, `Signature`, `AWSAccessKeyId`,
`X-Goog-Signature`, `X-Amz-Signature`, `X-Amz-Credential`,
`X-Amz-Security-Token` redacted) and drops `url.query` (*corrected
2026-11-02, B6 dev re-probe L3: instrumentation-http writes `url.query` on
incoming requests only, `utils.cjs:387-390`, and Go writes none, so undici
client spans no longer carry it*); (4) client health matching reads the
URL pathname of an absolute-form target (a proxied `path:
'http://target/healthz'`); (5) health paths are percent-decoded before
matching (contract D1 row "Percent-encoded health paths", A22); (6) gRPC
SERVER span status follows the RPC semconv server rule
(`GrpcServerStatusProcessor`, first span processor, `onEnding`; contract D1
row "gRPC server span status", A22) — not an exporter wrapper, which would
leave the app's own `spanProcessors` with the upstream ERROR; (7) a failed
build is retried by the next setup and reported each time; `/testing`
`reset()` disables the instrumentations unconditionally. Two behaviors to
know: a gRPC client health call still injects a `traceparent` (sampled=0)
— the CLIENT span is dropped by the sampler, not suppressed, as for any
NOT_RECORD span; and with a **foreign** global TracerProvider the
instrumentations bind to it, so neither the helper's health sampler nor its
gRPC status rule applies to their spans (the foreign provider's sampler
decides).*
*Amended 2026-10-29 (B2 review): extra span processors (the parity driver's
in-memory capture) go through the `setup()` option `spanProcessors`
(test/advanced, never a fixture `code` key), added after the helper's own
processors. SDK 3.0 accepts processors only at construction; a mutable
composite kept for a test seam would cost on every span in production.*

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
*Amended 2026-10-29 (B2 review): the parity driver calls `metricsHandler()`
with in-memory request/response objects instead of an uninstrumented server
(the instrumentation patches `http` globally once setup ran). The handler may
use only this response surface: `statusCode`, `headersSent`,
`setHeader`/`getHeader`/`removeHeader`, `writeHead(status[, headers])`,
`write(chunk[, encoding][, cb])`, `end([chunk][, encoding][, cb])`; it reads
`req.method`, `req.url`, `req.headers`. B7 also unit-tests the handler on a real
`http.Server` (the mock alone could drift from Node's `ServerResponse`).*
*Implemented 2026-11-02 (B7): `metrics-endpoint.ts`. The reader is
`new PrometheusExporter({preventServerStart: true})` (its own server is created
by the constructor but never listens); the handler collects it and writes the
text with the package's exported `PrometheusSerializer`, fed a copy of the
collection whose metric names are already the Go/Python names
(`prometheusName`: unit table of the OTel Prometheus-compatibility spec, `a/b`
→ `a_per_b`, `{…}` dropped, `1` → `ratio` on Gauges only (Go writes none on an UpDownCounter, verified 2026-11-02),
a `_total` already in a counter's name moved after the unit) and whose unit is
cleared (no `# UNIT` line, as in Go/Python); the serializer's
`# no registered metrics` placeholder is dropped. Content-Type
`text/plain; version=0.0.4; charset=utf-8`; `Content-Length` set; HEAD answers
without a body; a failed collection answers 500 (503 when the setup went away
meanwhile). The listener serves the path `/metrics` only (query ignored,
absolute-form targets parsed; anything else 404), `headersTimeout` 5 s (Go
`ReadHeaderTimeout`), and is started only when the reader was built and the
global MeterProvider is the helper's. A bind failure is one ERROR record on the
sink naming the port and the mount alternative. Shutdown closes the listener
(`close` + `closeAllConnections`) before the MeterProvider flushes, inside the
deadline. `OTEL_HELPER_METRICS_PORT` / option `prometheusMetricsPort`:
`^[+-]?[0-9]+$` in [0, 65535] (*superseded 2026-11-02 by the contract row
"Metrics port grammar", A21: `^\+?[0-9]+$`*), invalid env → warning, 9464; invalid code → TypeError;
`Resolved.prometheusMetricsPort`. `isHelperServer()` is the WeakSet check B6's
`ignoreIncomingRequestHook` uses; the "two scrapes leave the HTTP server
duration count unchanged" test lands with B6 (no HTTP instrumentation before).
`OTEL_HELPER_DISABLED_METRICS` / `disabledMetrics` were not implemented in the
first B7 pass (no ruling; the sdk-metrics 3.0 View `instrumentName` wildcard
expands only the first `*` and escapes `?`, `PatternPredicate.escapePattern`).*
*Amended 2026-11-02 (contract A21, D1 rows "Disabled-metric patterns",
"Unit 1", "Metrics port grammar", "Metrics listener"): `globMatch` (`*` any run,
`?` one character, everything else literal, case-sensitive);
`droppingExporter` wraps every push exporter (OTLP, console) and the scrape
filters the same predicate — chosen over Views (the SDK View wildcard cannot
express the glob, and a View per instrument is impossible: views are fixed at
MeterProvider construction); dropped instruments are still aggregated.
Option `disabledMetrics` (array of strings, `[]` wins), `Resolved.disabledMetrics`.
`1` → `ratio` on Gauges only (Go verified: no suffix on an UpDownCounter). Port
grammar `^\+?[0-9]+$` (`-0` now invalid). The listener answers 405 with
`Allow: GET, HEAD` to other methods on `/metrics`.*
*Amended 2026-11-02 (B7/A21 fix round): the listener `unref()`s every accepted
connection and sets `connectionsCheckingInterval: 1_000` (the header/request
timeouts are only checked at that period, 30 s by default), so a stalled or
keep-alive client never keeps the process alive (a test runs a child whose
script returns while a client holds half a request: it exits in under 2 s);
404 and 405 carry Go's `text/plain` bodies; labels are merged by
`mergedLabels` (contract D1 row "Colliding label names") and the scope labels
are written by the helper; names follow the contract D1 row "Suffix
semantics".*

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
*Implemented 2026-11-02 (B10a), with two changes to the choice above:*
*(1) not a SpanProcessor but a wrapping TracerProvider/MeterProvider that
`@staffops/otel-helper-sql` binds its contrib instrumentations to
(`src/internal/semconv.ts`): the tracer copies `db.system.name` into
`db.system` on the span-start attributes (`startSpan`, every
`startActiveSpan` form), the meter on every synchronous measurement
(counter, up-down counter, histogram, gauge; observables pass through, none
used by pg/mysql2) — so `db.client.operation.duration` carries both keys, as
Go's `otelsql.WithAttributes` does on spans and measurements, which a span
processor could not reach. A processor would also have needed to tell DB
spans from others. Value table: `postgresql`→`postgresql`, `mysql`→`mysql`
(identical, as Go `ext/otelsql` `systemAttributes`); an unknown value is
copied as it is. (2) the database tokens are read by `-sql` when
`sqlInstrumentation()` is called (Go reads at `otelsql.Open`): legacy keys
unless the value holds `database` without `database/dup` — Go's
`legacyKeysEnabled` rule, ASCII-folded and trimmed with the contract set.
A set value without any database token keeps both keys, as Go (Python
contrib switches to the legacy keys only: D-JS14 row of 2026-11-02). The
core parses only the HTTP side: a value without the `http` token, or with
`http/dup`, is warned once (`resolve.ts` `warnSemconvOptIn`, variable name
only) and changes nothing. Tests: `-sql` `test/sql.test.cjs` (grammar table,
wrappers), `test/db.test.cjs` (spans and the duration metric against real
PostgreSQL/MySQL, `database` mode in a child); core `test/opt-in.test.cjs`
(HTTP warning).* *Amended 2026-11-03 (B10a fix round):* the tracer wrapper is `dbTracerProvider` (it also drops ERROR descriptions, A25, and names mysql2 spans), the meter wrapper `dbMeterProvider` (legacy key plus a per-bind generation), both forwarding members they do not change; the database mode is read **once per process** by the first `sqlInstrumentation()`; Python and Go follow the same opt-in rule since A25. *Amended 2026-11-03 (B10b):* `dbTracerProvider`, `dbMeterProvider`, `legacyKeysEnabled`/`withLegacyKey` and the once-per-process mode moved from `-sql` to the core's internal entry `@staffops/otel-helper/internal/db` (not public API, like `internal/bridge`; tests `packages/otel-helper/test/db.test.cjs`), shared by `-sql` and `-redis`; the tracer takes a generic `spanName` rule (`-sql` passes the mysql2 one); the mode is read once per process by the first `sqlInstrumentation()` **or** `redisInstrumentation()`; `redis`→`redis` joins the value table. *Amended 2026-11-03 (B10c):* `@staffops/otel-helper-aws` binds its contrib to the same `dbTracerProvider` (`legacyKeys` from the shared once-per-process mode, so the first `awsInstrumentation()` can read it too): DynamoDB spans carry `db.system.name=dynamodb` and the legacy `db.system=dynamodb` (an unknown value is copied as is; Python botocore writes `db.system=dynamodb`), only `db.system.name` in `database` mode (test `packages/otel-helper-aws/test/sdk.test.cjs`). `dbTracerProvider` gained an optional `sanitizeAttributes` rule (applied to the span-start attributes after the legacy key, and to every `setAttribute`/`setAttributes`; core test `test/db.test.cjs`), which `-aws` uses for the attribute rules of D-JS14 (rows of 2026-11-03, AWS). The contrib-instance machinery moved to the core entry `internal/contrib` (D-JS11 amendment of 2026-11-03). *Amended 2026-11-04 (B14, code-review L2):* the `-sql` test-only re-export `src/internal/semconv.ts` was deleted; `-sql`'s `test/sql.test.cjs` imports the rule from `@staffops/otel-helper/internal/db` directly.

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
*Corrected 2026-10-29 (B3 review): the shutdown deadline timer is **ref'd**,
not `unref()`'d, and cleared in `finally`. An unref'd timer let a process whose
only pending work was a hung provider exit without ever settling the shutdown
promise (no rejection, no diagnostic); a ref'd timer keeps it alive for at most
the deadline (default 30 000 ms, clamped to 2^31-1), and a healthy
fire-and-forget `shutdown()` still exits at once because the timer is cleared
when the providers finish. Each provider's shutdown is called as
`Promise.resolve().then(() => p.shutdown())`, so a synchronous throw is
reported through `allSettled` (an `AggregateError`). Child-process tests cover
a hang without a handle, a hang with an open socket and fire-and-forget.*
*Decided 2026-10-29 (B3 review; implemented in B9): instead of
`trace.disable()` + a new SDK provider on every rebuild, the helper registers
**one stable, helper-owned forwarding TracerProvider** (and MeterProvider /
LoggerProvider when B4/B8 add them) once per process and swaps its SDK delegate
on rebuild. Tracers (and meters, loggers) obtained before a re-setup keep
working; with the B3 shape they stay bound to the old, shut-down provider. A
`todo` test in `test/setup.test.cjs` pins the target behavior until B9.*
*Implemented 2026-11-02 (B9; `src/internal/forwarding.ts`, `setup.ts`). This
supersedes the "re-setup calls `…disable()`, rebuilds, and re-binds" text
above:*
- *One forwarding TracerProvider, MeterProvider, LoggerProvider and
  propagator per process, registered as the API globals by the first setup
  that owns them and **kept registered across `shutdown()`**; every setup
  points them at its SDK providers and its propagator. `shutdown()` keeps each
  provider as the delegate until that provider's own `shutdown()` settles,
  then points the forwarder at nothing (API no-ops) if it still forwards to
  that provider; the propagator stays (Go and Python keep theirs installed).
  Forwarding instruments resolve the delegate's instrument on each call;
  observable and batch callbacks are moved to every new **defined** delegate
  (and off the previous one) and stay on the last SDK instruments while there
  is none. The context manager is also registered once and reused.*
  *Corrected 2026-11-02 (B9 fix round H1, found by observability, dev and
  code-review independently): the first B9 version pointed the forwarders at
  nothing synchronously at the start of `shutdown()`, on the claim that Go
  stops advertising the pipeline first. Go stops only its log bridge and
  `/metrics` first and keeps the global providers through the flush
  (`go/otelhelper.go` `pipeline.shutdown`); Python keeps them too. The early
  unbind moved every observable and batch callback off the SDK instruments
  before the final collection, so the last OTLP export lacked them.
  Regression test "H1 regression" in `test/setup.test.cjs`.*
  *Decided 2026-11-02 (B9 fix round M1, M7): the forwarders have
  `forceFlush()` (to the current delegate; resolves without one); tracers,
  meters and loggers are cached by (name, version, schema URL); an observable
  and its meter are tracked only while they hold callbacks or batches (no
  retention of callback-less instruments). Since B9 the delegate of the API's
  global proxy is the forwarder, not the SDK provider (CHANGELOG).*
  *Decided 2026-11-02 (B9 fix round L1): without a delegate the tracers use a
  local no-op tracer (`NOOP_TRACER`: `wrapSpanContext` of a valid parent,
  none for `root: true`, the `startActiveSpan` overloads) instead of the
  deprecated `ProxyTracerProvider`.*
- *Ownership is by identity: the helper records the registry entry it
  registered (for `trace` the API's proxy) and owns the global while the
  registry still holds that entry. A re-setup therefore never unregisters
  anything; a global someone replaced is reported as `no longer the
  helper's` and kept. Only `testing.reset()` unregisters, and only the
  entries still the helper's; it also discards the forwarders, so a test's
  tracers, meters and observable callbacks never reach the next test.* *Ruled 2026-11-02 (owner): `reset()` discards the forwarders.*
- *Instrumentations bind to the forwarding providers when they are the
  globals (no re-bind needed on re-setup), else to the foreign global; every
  setup binds them again, so a `replaceGlobals` setup moves them to the
  helper's (carry-over decision of B6).*
- *Foreign globals: kept, reported per global, and `traces` / `metrics`
  listed in the new `Resolved.inactiveSignals` (JS7; a foreign LoggerProvider
  lists nothing, the helper's loggers still export). `replaceGlobals: true`
  replaces a foreign TracerProvider, MeterProvider, LoggerProvider and
  propagator (one INFO line each); never the context manager (D-JS14).*
  *Ruled 2026-11-02 (owner): `Resolved.inactiveSignals` stays, Node only, and
  covers global ownership only — a signal whose exporter failed to build is
  not listed (that failure is reported on the sink). `replaceGlobals` never
  replaces the context manager; the kept one is reported once per process
  under `replaceGlobals`.*
- *G1: `setup()` records an undo step for every change (DiagLogger, context
  manager, propagator and its delegate, the three providers and their
  delegates, instrumentations, listener). Any throw after resolution runs
  them in reverse, shuts down the built providers in the background, leaves
  `Runtime.active` untouched (so `metricsHandler()` answers 503 and the
  helper's loggers write stdout, as before the call) and rethrows; the next
  call starts over. `/register` reports a failed setup instead of throwing.
  Tested at every install step through the internal `setupFault` seam.*
  *Ruled 2026-11-02 (owner, B9 fix round): a failed `setup()` throws after the
  rollback, as Go returns and Python raises; `/register` reports it instead.
  The `setupFault` seam stays, tagged `@internal`. Fix round: an undo step is
  recorded **before** each replacement changes the registry (M4); for
  `trace` and `logs` a registration with no previous entry is undone through
  the registry, leaving the API's own proxy in place with its delegate reset,
  so tracers and Logs API loggers obtained before `setup()` join the
  provider the app registers next (M3; `releaseGlobal`, falling back to
  `disable()` if the API's shape changes); the providers a failed setup built
  are shut down at every step from `built` on, and the pipelines alone when a
  provider constructor throws (M5, L). The rollback report goes through
  `redactErrorText` (URL userinfo/query, credential pairs, bearer tokens).*
- *`setup()` in an ES module process without the loader hooks warns once
  (JS5) when the instrumentations are enabled; the entry format comes from
  `process.argv[1]` / `--input-type` (D-JS14 for what it misses).*

> **Corrected 2026-11-03 (B12).** Natural process end (fix round, dev H1):
> the lifecycle above flushed only on an explicit `shutdown()`, so a script or
> job whose event loop simply empties exported nothing. `setupFrom()` now calls
> `registerExitHook()` after a successful `install()` (`setup.ts`): one
> `process.once('beforeExit', hook)` per process at a time
> (`Runtime.exitHook`), skipped when `!isMainThread`. The hook clears itself,
> and calls `shutdown()` (default deadline) only when `rt.active` exists and
> `active.shutdown` is unset, so an app's own `shutdown()` is never repeated
> and the event loop the flush refills cannot fire it again; a rejection is
> reported on the sink (`shutdown() at process exit (beforeExit) failed; …`),
> never thrown. A re-setup registers no second listener; a setup after the
> listener fired registers one again; a failed setup registers none (the call
> comes after `install()`); `resetForTests()` removes it. `beforeExit` is not
> emitted on `process.exit()` or a fatal signal: the SIGTERM pattern and
> `await shutdown()` before `process.exit()` stay the app's (JS7 correction of
> 2026-11-03). A `cluster` worker the primary disconnects ends on its own
> unless a handle keeps it alive, so this flush covers it too (D-JS13
> correction below). Trade-off: a natural end now waits for the flush, up to
> the shutdown deadline when the endpoint hangs. Tests: `test/exit.test.cjs`.
>
> **Corrected 2026-11-03 (B12).** Re-review additions (code-review N2, N5;
> dev N1, N2): there is **no opt-out** of the listener (an app bounds the exit
> by calling `await shutdown(<ms>)` itself, after which the listener does
> nothing); the delay of a natural exit is up to the export timeout
> (`OTEL_EXPORTER_OTLP_TIMEOUT`, 10 s measured against an unreachable
> endpoint), bounded by the 30 s default `shutdown()` deadline; telemetry an
> app emits from its own `beforeExit` listener, or from work that listener
> schedules, may be dropped (the helper's listener, registered at `setup()`,
> may already have flushed); a worker thread is not flushed when it ends
> (no listener outside the main thread) — it must `await shutdown()` itself.
> Documented in `nodejs/HOW-TO.md` §4 and §13.

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
`-sql` and asserts a pg span (*moved to B10a, 2026-11-02: `-sql` does not
exist before B10a; B9 tests `/register` calling `setup()` and the ESM warning
with the core instrumentations*); go/no-go
spike in B1c on 22.15.0/22/24/26. *Done 2026-11-02 (B10a):
`packages/otel-helper-sql/test/db.test.cjs` runs `node --import
test/fixtures/esm-preload.mjs test/fixtures/esm-app.mjs` (the preload
imports `@staffops/otel-helper/hooks`, then calls `setup({
instrumentations: [sqlInstrumentation()] })`; the app `import pg from 'pg'`)
and asserts the `pg.query` span on 22.15.0, 24 and 26; the control without
the hooks import gets the JS5 warning.* *Amended 2026-11-03 (B10c, carry-over code-review M4 of the B10b fix round):*
the contrib-instance machinery duplicated in `-sql` and `-redis` moved to a
new non-public core entry, **`@staffops/otel-helper/internal/contrib`**
(`src/contrib.ts`, `esm/contrib.mjs`; core test `test/contrib.test.cjs`):
`contribRegistry(spec)` holds one opt-in package's per-process state — the
contrib instrumentations built once, disabled until `setup()` and shared by
every factory result; the enable count per child; the TracerProvider each
child was bound to (the same again is not re-bound) and the meter generation
guard (every bind is a new generation, restored when the bind throws); the
re-require of cached patched files (`cachedFiles`, every copy; a main entry
only when cached) on the first enable; the `#children` result class with
`libraries`, `coveredInstrumentations` and the ignored `setConfig()` (warned
once per package, on the next enable); the one-time unresolved/failed-load
reports; and `instrument(selection)`, the factory body (a load failure throws
for a library named `true` or when none loads, else is reported; an unresolved
library is a WARN when named `true`, a debug line otherwise). It also exports
`appDirectories`, `resolvesFromApp(names, dirs)` and `contribLoadError(label,
pkg, module, err)` (the D14 wording). Each package keeps only what is its
own: the contrib configuration (`build`), the patched-file list, the
`tracerProvider`/`meterProvider` hooks (the DB ones in `internal/db`), its
options and its D14 label. A sibling entry and not `internal/db`: the
machinery is not about database spans (`-aws` uses it for every AWS service),
and `internal/db`'s export test keeps that entry to the database rules.
`-sql` and `-redis` were moved onto it with no behavior change (their suites
pass unchanged: `libraries.ts` keeps `appDirectories`, `resolvesFromApp` and
`instrumentationLoadError` as thin wrappers, the `childrenForTests` seam
stays); `-aws` is its third user.
Opt-in packages are separate (peer on core,
same version). Tooling: npm workspaces, `tsc`, eslint + typescript-eslint,
`publint`, `@arethetypeswrong/cli`, `node:test` against `dist/`, `c8` per
package (≥ 90%). Publish npmjs (trusted publishing/provenance) + GitHub
Packages, tags `nodejs/vX.Y.Z`. **Justification**: research; hazard. **Trade-offs**:
two registries. **When wrong**: trusted publishing unavailable for a new
package's first publish. **Reversal**: one bootstrap publish with a scoped
token, then trusted. **Alternatives**: pnpm (extra tool), tsup (extra tool).

> **Corrected 2026-11-03 (B12).** Wherever the spec or tasks imply that an
> ES module app needs `--import` (tasks.md "Carried from B6 into B9", the B12
> row's "without `--import @staffops/otel-helper/register`"): `--require
> @staffops/otel-helper/register` installs the ES module hooks too
> (`installHooks()` registers them from a CommonJS preload), so both preloads
> instrument an ES module app — checked 2026-11-03 on Node 24.22.2 with an
> express ES module app: route-named SERVER and `request handler` spans under
> either flag, no JS5 warning. The two differ only in worker threads
> (`--require` runs again in each, `--import` only in the main thread;
> D-JS14 row "Worker threads") and under `--eval`/`--print` (F1 above).

> **Corrected 2026-11-03 (B12).** The note above is wrong on worker threads
> (fix round, obs M1): `--import` and `--require` preloads both run in every
> **file-based** worker thread (`new Worker('./file.js')`); only `--require`
> runs in an `eval` worker — checked on Node 22.15.0, 24 and 26 (a preload
> printing `isMainThread` under each flag, a file and an `eval` worker). The
> D-JS14 "Worker threads" row, observed with `eval` workers only, is
> corrected below. `/register` stays correct either way (hooks only, no
> `setup()`, outside the main thread); a custom preload must guard
> `setup()` with `isMainThread` (HOW-TO §1, §13).

*Amended 2026-11-04 (B14 re-probe L1): no source maps are built or published
(`tsconfig.base.json` `sourceMap: false`): the published maps pointed at
`../src`, which is not in the tarball. The dist JavaScript keeps its comments
and stays close to the TypeScript (target ES2023), and c8 measures it
directly — tsc's `__importStar` helpers (emitted for `import * as`) now count,
so `-aws` and `-redis` import `join` from `node:path` by name (no helper) to
keep their gates. `ci/check-pack.mjs` fails on any `*.map` in a tarball. The opt-in
packages' root exports are listed in each package README and in README "API
stability (0.x)"; `-pino` and `-winston` export `VERSION` too, kept by
`ci/version.mjs` with the other four constants.*

*Amended 2026-11-04 (B14 release prep, owner decision): **allow-publish, not
staged publishing.** The bootstrap (the Reversal above) is done: the six
packages exist on npmjs, each with a trusted publisher (GitHub Actions,
`StaffOps/otel-libs`, `ci.yml`, environment `nodejs-release`; verified with
`npm trust list`) and publishing access "Require two-factor authentication
and disallow tokens". npm offers staged publishing (the CI publish is staged and a maintainer
approves it on npmjs: a manual npm approval per release) and marks
"Allow npm publish" — the trusted publisher publishing directly — as "Not
recommended". The owner ticked "Allow npm publish" and left "Allow npm
dist-tag" off. **Justification**: the human gate already sits before the
publish job — the required reviewer of the `nodejs-release` environment
(`can_admins_bypass=false`, tags `nodejs/v*` only), the tag ruleset and the
org's required 2FA; a second, manual npm approval per release adds a step
with no new check (the tarballs are the CI-tested bytes, `registry-state`
refuses other bytes). CI runs `npm publish --provenance --tag <latest|next>`
only (`ci.yml` `publish-nodejs-npmjs`), never `npm dist-tag`, so the
dist-tag permission is not needed. **Trade-off**: a compromised workflow run
that passes the environment approval publishes with no further stop; a
published version cannot be replaced (fix forward). **When wrong**: the
environment approval is ever bypassed or automated, or npm makes staged
publishing the only path for trusted publishers — then switch to staged
publishing and approve on npmjs after each CI publish.*

*Amended 2026-11-04 (B14, after the rc.1 publish): the owner removed the
required reviewer from the `nodejs-release` environment (rules now
`branch_policy` only: tags `nodejs/v*`, `can_admins_bypass=false`). The human
gate is now the tag push itself — only admins can create `nodejs/v*` (tag
ruleset) and the org requires 2FA; there is no approval click. This is the
"environment approval is ever bypassed or automated" case above: the
trade-off is accepted (a tag pushed by mistake publishes; fix forward with
the next rc), staged publishing stays the documented alternative.*

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
*B2 (2026-10-29): the schema/matcher/child execution live in `harness.cjs`
next to the runner (requiring a `node:test` file runs its tests); the runner
runs inside the core package's `npm test` so c8 merges the children's
coverage into that gate; a kind whose helper API is missing reports
`adapter_pending` (a hard failure unless the case is skipped). Review
amendments (same day): the resource comes from `/testing` `getResource()`
(B3; Python reads `provider.resource`), never from a probe span, which would
reach the wire in export cases; span capture passes `spanProcessors` to
`setup()` (D-JS7); non-export concurrency is `min(8, 2 × CPUs)`; a case is
validated before its child is spawned; text matchers (`prefix`, `regex`,
`not_contains`) apply to strings only and regexes compile with the `u` flag
(parity/README.md).*
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
*Implemented 2026-11-02 (B9b): B9b enabled the existing driver `fork` kind
(`nodejs/test/parity/driver.cjs` `kindFork`/`forkWorker`), which forks two workers that each call `setup()` and report
their resource id over IPC; the primary does not call `setup()`, as the Python
driver's parent does not. `service.instance.id` is `randomUUID()` per
`resolvePlan()` (`resolve.ts`), and a cluster worker is a fresh process, so no
worker can inherit the primary's id (unlike `os.fork()`). `test/cluster.test.cjs`
adds what the fixture does not see: a primary that also set up has a third,
distinct id; the code-option branch of the explicit-id warning; a
`child_process.fork()` child; the listener cases below, and a worker
re-forked on its slot (D-JS14 rows of 2026-11-02). The primary waits for real
events (`cluster` `'listening'`, worker reports, a polled "could not bind"
line) under a 20 s deadline that kills every worker and fails the run; a
worker that exits before reporting fails it too (B9b fix round, 2026-11-02).*

> **Corrected 2026-11-03 (B12).** The documented shutdown pattern was
> incomplete: the primary's SIGTERM handler sets `shuttingDown` and calls
> `worker.disconnect()`, but a disconnected worker receives **no SIGTERM**
> (only `docker stop`'s PID 1, the primary, does), so a worker that flushes
> only in `process.once('SIGTERM', …)` exits with its batched spans unexported
> (reproduced on Node 24: exit within ~7 ms, span lost). The worker must also
> flush on `process.once('disconnect', () => { shutdown().finally(() =>
> process.exit(0)); })`. Test "D-JS13 a worker the primary disconnects
> flushes its spans only with a disconnect handler" in `test/cluster.test.cjs`
> pins both outcomes; `nodejs/HOW-TO.md` §13 "cluster and pm2" shows it.

> **Corrected 2026-11-03 (B12).** Fix round: since the `beforeExit` flush
> (D-JS10 correction above), a disconnected worker with nothing else open
> ends on its own and is flushed without a handler; the `'disconnect'` handler
> is needed when a timer, pool or socket keeps the worker alive. The worker
> also flushes on `process.once('SIGTERM')` and `process.once('SIGINT')`: pm2
> and process-group signals (Ctrl-C) reach the workers directly (obs M4); for
> pm2, `kill_timeout` must exceed the shutdown deadline (pm2 still untested).
> Any preload on the primary's command line, not only `NODE_OPTIONS`, sets up
> in the primary (obs M5); `cluster.setupPrimary({ execArgv: ['--import',
> '@staffops/otel-helper/register'] })` preloads the workers only (checked on
> Node 24). The test now has two cases — no handler and nothing open: flushed
> by `beforeExit`; a keep-alive handle and the handler: flushed and exited —
> with `OTEL_BSP_SCHEDULE_DELAY=60000` and a deadline that kills the workers.

### D-JS14 — Known Node differences (reference; each with a fixture status)
| Difference | Status |
|---|---|
| gRPC RPC metrics absent (`grpc.health_*_counted_in_metrics`) | `skip:no-upstream-rpc-metrics` (as Python) |
| gRPC spans carry the legacy `rpc.system`, `rpc.service`, `rpc.method`, `rpc.grpc.status_code` (instrumentation-grpc 0.300; Go emits the current RPC semconv) (B6, 2026-11-02). *Extended 2026-11-02 (B6 fix round):* span **names** differ in all three languages — Go `pkg.Svc/Method` (semconv `{service}/{method}`), Python `/pkg.Svc/Method`, Node `grpc.pkg.Svc/Method`; Node error spans carry the non-semconv `grpc.error_name` / `grpc.error_message` and no `error.type` | recorded; README "Known emitted-telemetry gaps". Rename deferred to a ruling (contract A22 follow-up (a)); the Node health sampler matches the current Node name. No fixture case asserts gRPC span attributes or names |
| `node:http2` (and h2c) is not instrumented: instrumentation-http patches `http`/`https` only; no official HTTP/2 instrumentation (B6 fix round, 2026-11-02) | recorded; README gap; B12 HOW-TO item. Go `otelhttp` and Python ASGI see HTTP/2 requests through the same handler |
| Express spans: one SERVER span named `GET <route>` plus one INTERNAL `request handler - <route>` span per matched handler (middleware and router layers ignored). Go `otelhttp`: the SERVER span only. Python ASGI: the SERVER span plus `http send` / `http receive` INTERNAL spans (B6 fix round, 2026-11-02) | recorded; no case (span counts assert SERVER spans only) |
| HTTP metrics: Node emits only `http.server.request.duration` and `http.client.request.duration`. Go also emits request/response body sizes; Python emits body sizes and `http.server.active_requests` (B6 fix round, 2026-11-02) | recorded; README gap; contract A22 follow-ups (b), (c) |
| `error.type` on transport errors: instrumentation-http and -undici write the JS error name (`Error`, `TypeError`), Go the Go error type, Python the exception class name (B6 fix round, 2026-11-02) | recorded; no case |
| `http.route` only with express (an instrumented router); a plain `node:http` server has none, so span names and the server metric have no route (Go: only with a `ServeMux` pattern; Python: FastAPI routes) (B6 fix round, 2026-11-02) | recorded; README gap |
| URL redaction: Node `node:http` and `fetch` clients write userinfo as `REDACTED:REDACTED` and redact the values of the signature parameters (`sig`, `Signature`, `AWSAccessKeyId`, `X-Goog-Signature`, `X-Amz-*`) in `url.full`; Node servers redact the same list in `url.query` (other values, e.g. `token`, are kept). Go `otelhttp` v0.72 drops userinfo from `url.full` (`internal/semconv/client.go:113-121`), redacts no query value and writes no server `url.query`. Python: not verified in the fix round (B6 fix round, 2026-11-02). *Resolved 2026-11-03 (contract task A28):* Go and Python now redact the same parameter list in client `url.full` and (Python) server `url.query`; Node unchanged (contract D1 "URL query redaction"). *Amended 2026-11-02 (B6 dev re-probe L3–L5):* no client span carries `url.query` (undici's is dropped, as `node:http` and Go); in `url.full`, `node:http` re-serializes the query through `URLSearchParams` (upstream) while undici keeps it as written unless a name is redacted (`flag` stays `flag`, `%20` stays `%20`) — intentional, the helper rewrites no more than it must; parameter names are matched **case-sensitively** (`SIG` is not redacted), as upstream does  *Status amended 2026-11-04 (B14, obs L2):* the Python comparison is closed by the A28 resolution above. | recorded; README gap |
| A foreign global TracerProvider: the instrumentations bind to it, so the helper's health sampler and gRPC status rule do not apply to their spans (they keep upstream's ERROR) (B6 fix round, 2026-11-02). *Extended 2026-11-02 (B9):* a later setup with `replaceGlobals: true` binds them to the helper's forwarding providers | recorded; README gap |
| Routers or apps created before `setup()`: express wires its route layers when the router is built, so a router or app built before the instrumentation is enabled gives a truncated `http.route` (`GET /api` for `/api/orders/:id`) or none, and may miss request-handler spans; the preload report names this. Routes modules must be required after `setup()` (B6 dev re-probe L1, 2026-11-02) | recorded; the report text (D-JS1); B12 HOW-TO item |
| A gRPC handler that **throws synchronously** leaves its SERVER span unended (never exported): upstream instrumentation-grpc, reproduced without the helper; Go and Python record the call as UNKNOWN (B6 dev re-probe L2, 2026-11-02) | recorded; README gap; no case (the drivers' "throw" is not equivalent across languages: a Go handler would panic) |
| The gRPC status rule also clears an app's own `setStatus(ERROR)` on a gRPC SERVER span whose `rpc.grpc.status_code` is not one of the six error codes (B6 final review, 2026-11-02) | recorded; the processor cannot tell who set the status |
| Busy `/metrics` port doesn't fail sync setup | recorded; loud error |
| Metric reader timeout capped at the interval | recorded; warning. *Amended 2026-10-29 (B4c): with a configurable interval the cap also applies when only the interval is short (e.g. `OTEL_METRIC_EXPORT_INTERVAL=5000` with the 10 000 ms default timeout); the warning then names the interval's source (`OTEL_METRIC_EXPORT_INTERVAL` or `exportIntervalMs`), otherwise the timeout's source plus `interval=<source>`. Unit-tested in `test/signals.test.cjs`*. *Amended 2026-10-29 (A19 fix round): a very short interval makes OTLP metric exports time out (the deadline is the interval); when the cap makes the reader timeout shorter than 1000 ms the helper warns once more, naming the interval's source* |
| ~~Metric export interval bounded by the Node timer limit (2^31-1 ms): an env value above it is clamped with a warning, a code `exportIntervalMs` above it throws (B4c, 2026-10-29)~~ | *Moved 2026-10-29 to the contract (design D1 row "Metric export interval", A19): every language clamps and bounds the interval the same way; no longer a Node difference* |
| Console metric reader timeout = min(30 000, interval) (B4c, 2026-10-29) | recorded; Go and Python leave the console reader at their SDK default timeout. Unit-tested in `test/signals.test.cjs` |
| The DiagLogger is still installed under `OTEL_SDK_DISABLED=true` (B4c, 2026-10-29) | recorded; Go and Python have no equivalent (their SDK diagnostics go to their own loggers) |
| `withMaskedEnv` hides the helper-owned OTLP variables from `process.env` for the synchronous construction; a `worker_threads` worker started with `env: SHARE_ENV` shares `process.env` and would see them missing during that window (B4b review, 2026-10-29) | recorded; setup runs before workers in practice. No fixture case |
| `OTEL_METRICS_EXEMPLAR_FILTER` not honored | recorded gap |
| `http/dup` / old HTTP semconv not available | recorded gap. *Since 2026-11-02 (B10a): a value without `http` or with `http/dup` is warned once, naming only the variable (D-JS9)* |
| ~~DB metric attributes without `db.system` (if B10 confirms)~~ | *Corrected 2026-11-02 (B10a): no gap for pg — the `-sql` meter wrapper (D-JS9) adds `db.system` to `db.client.operation.duration`, asserted in `test/db.test.cjs`. instrumentation-mysql2 0.70 records no metric at all (next rows)* |
| Fastify not instrumented | recorded until `@fastify/otel` supports SDK 3 |
| IPv6 zone ids in endpoints rejected (WHATWG URL) | recorded; warning + fallback. *Since 2026-10-29 (B4a refutation) the contract D1 "Host characters" row makes zone ids invalid in every language (A17 for Go/Python), so this is no longer a Node-only difference* |
| WHATWG host normalizations kept after the RFC 3986 split (B4a refutation, 2026-10-29): host lowercased, IDN → punycode (`bücher.example` → `xn--bcher-kva.example`), IPv4 shorthand (`127.1`, `0x7f.1` → `127.0.0.1`), IPv4-mapped IPv6 compressed (`[::ffff:127.0.0.1]` → `[::ffff:7f00:1]`) | recorded; cosmetic for lowercase and IPv6. IDN and IPv4 shorthand may **resolve differently than in Go**, which passes the name to the resolver as written (a shorthand like `127.1` is a DNS lookup there, an address in Node). Unit-tested in `test/otlp.test.cjs`. *Amended 2026-10-29: IDN punycoding is now a contract ruling (D1 "IDN hosts"), so it is no longer a difference; a host that fails IDNA is invalid. The IDNA validation itself varies by Node version (ada): `xn--zz` (invalid punycode) is rejected on 22.15.0 and accepted on 26; the fixture uses U+FFFD, rejected on every leg* *Corrected 2026-10-29 (A17 refutation): **row superseded** by the contract D1 "One host rule" row — Node now checks the host itself (RFC 3986 reg-name bytes, canonical dotted-quad IPv4 when the host ends in a number, IPv6 via `net.isIPv6`, lowercased and otherwise as written) and uses WHATWG only for IDNA (`url.domainToASCII`, non-ASCII hosts only). `127.1` and `0x7f.1` are now invalid instead of `127.0.0.1`; `[::ffff:127.0.0.1]` is kept as written instead of `[::ffff:7f00:1]`; `xn--zz` (ASCII) is no longer sent to IDNA, so the 22.15.0/26 difference is gone. No Node-only host difference remains* *Corrected 2026-10-29 (A17 final review): the 22.15.0/26 difference is gone **for ASCII hosts only** — a non-ASCII host with an invalid punycode label (`xn--zz.bü`) still goes through ada and still varies by Node version. `domainToASCII` now runs on the host plus a guard label (`.x`) so its WHATWG IPv4 parsing cannot accept fullwidth shorthand (`１２７.１`); empty labels and more than 253 characters are rejected as in Go and Python* |
| `cluster` workers share the metrics port (round-robin scrapes) | warning; docs recommend per-worker port or OTLP. *Verified 2026-11-02 (B9b and its fix round, `test/cluster.test.cjs`): (a) workers on one port: each warns once and the primary round-robins connections — six scrapes on new connections reach both workers; (b) a primary that also ran `setup()` (e.g. `--require …/register` in `NODE_OPTIONS`) owns the port and serves its own, app-less registry: every worker warns and then reports `could not bind port <n> … error=EADDRINUSE` on the sink (the primary's cluster handle cannot bind) — loud, not silent; (c) per-worker ports work — each port serves one worker — and each worker still logs the warning once, since a worker cannot see the others' ports. The port must come from a **stable slot owned by the primary**: `cluster.fork({ OTEL_HELPER_METRICS_PORT: String(base + slot) })`, re-forked with the same slot only on an unexpected exit — `w.on('exit', () => { if (!w.exitedAfterDisconnect && !shuttingDown) fork(slot); })`, the primary's SIGTERM handler setting `shuttingDown` and calling `worker.disconnect()` (B9b re-review LOW, 2026-11-02) — (tested: the replacement serves the same port); under pm2, `base + Number(process.env.NODE_APP_INSTANCE)`. **`cluster.worker.id` is never reused** (each fork gets the next id), so `base + cluster.worker.id` moves a restarted worker to a new port and its metrics stop reaching the configured scrape target silently (dev review HIGH, B9b fix round, 2026-11-02).* *Ruled 2026-11-02 (owner, B9b): the warning stays at WARN, once per worker, worded as a conditional — `cluster worker serving /metrics on port <n>: if several workers use this port, each scrape reaches one of them at random; give each worker its own port (e.g. base + a per-worker slot) or use OTLP only` (it was `…the workers share the port…`, which read as wrong on a per-worker port; the example said `base + cluster.worker.id` until the fix round)* |
| An explicit `service.instance.id` (code or `OTEL_RESOURCE_ATTRIBUTES`) in a `cluster` worker is kept and warned once per worker (contract D4). Node detects **every** cluster worker (`cluster.isWorker`, set from `NODE_UNIQUE_ID`, which Node removes from the worker's `process.env`; pm2's cluster mode is built on `node:cluster` but is not tested here), whether the helper was loaded before or after the fork; Python warns only when the helper was imported before `os.fork()` (it cannot tell otherwise); Go has no prefork model. The primary is never warned, and a `child_process.fork()` child of a worker or a worker thread is not a cluster worker (B9b, 2026-11-02). *Amended 2026-11-02 (B9b fix round, contract A24): the warning is a conditional, the same wording except the worker kind (`forked`/`cluster`) as Python: `service.instance.id is set explicitly in a cluster worker: unless each worker sets a different value, every worker reports the same id; leave it unset so each process gets its own`* | `fork.env_instance_id_warns_and_kept` pass (regex on the shared wording, either worker kind), `fork.instance_ids_distinct_post_fork` asserts its absence; code branch, primary and `child_process.fork()` child unit-tested in `test/cluster.test.cjs` |
| BSP/BLRP export-timeout env precedence differs between Go and Python | *Closed 2026-10-29: contract ruling "Export deadline" (A18) — not honored in any language, warned* |
| gRPC over TLS to an **IP-literal** endpoint fails on Node 26 (Node refuses an IP as TLS ServerName; grpc-js sets it from the target host; DEP0123 warning on 22/24) — spike B1c F5 | recorded; the helper warning for IP-literal gRPC TLS endpoints lands in B4b (*implemented 2026-10-29: every Node version, naming the endpoint variable; unit-tested in `test/exporters.test.cjs`*). *Decided 2026-10-29 (B2): the Node legs reach the fake receiver by DNS name — `fakeotlp` → 127.0.0.2, `fakeotlp-tls` → 127.0.0.3 (new SAN in fakeotlp's default `TLS_HOSTS`; `extra_hosts` on `fakeotlp-node`, which `node-tests` shares through `network_mode: service:`; `/etc/hosts` in the CI `node-test` job); Go and Python keep the IP literals, so their cases and validators are unchanged. Rejected: `skip:node26-ip-servername` on the 26 leg — the validators accept no per-version Node status, the extension would touch all three runners, and it would leave the six gRPC TLS cases unrun on 26 (with DEP0123 noise in `diagnostics` on 22/24). Evidence: a runner test completes a CA-verified TLS handshake by name with both TLS receivers on 22.15.0, 22, 24 and 26; the mapping is visible in the joined container (`docker run --network container:…` sees the owner's `--add-host`). Cost: the IP-literal gRPC TLS path is not exercised by the Node fixture (unit test in B4b).* |
| A `__proto__` resource attribute (env or code) is kept by the helper as an own data property but dropped by `@opentelemetry/resources` 3.0: `Resource.attributes` builds a plain object with `attrs[k] = v` (A16, 2026-10-29) | `skip:no-upstream-resource-proto-key` on `resource_attributes.proto_key_kept`; helper side unit-tested via `getRawAttributes()` |
| `unknown_service:<suffix>`: Node `process.argv0` (`node`), Go the binary name, Python the interpreter name (helper-computed on every SDK version since A16) | recorded; the fixture asserts the `unknown_service` prefix only (R5) |
| A propagator registered before `setup()` is kept: the 3.0 API refuses a second registration; Go and Python replace it (B5 fix round, 2026-10-30) | recorded; reported `a global propagator is already registered; the helper keeps it and OTEL_PROPAGATORS is not applied`; `Resolved.propagators` is what the helper installs when it owns the global; under `OTEL_SDK_DISABLED=true` the INFO line then says `the helper installed no propagator`. B9 `replaceGlobals` covers the propagator (*implemented 2026-11-02*). Unit-tested in `test/tracing.test.cjs` |
| `/metrics` scope labels: `otel_scope_version` / `otel_scope_schema_url` are omitted when empty (Go and Python write `""`), and an instrument without a description gets `# HELP <name> description missing` (Go/Python: empty HELP) — the upstream `PrometheusSerializer` (B7, 2026-11-02) | recorded; equivalent for Prometheus (an empty label value is an absent label). Unit-tested in `test/metrics.test.cjs`. *Amended 2026-11-02 (B7/A21 fix round): the helper now writes the three scope labels itself (serializer `withoutScopeInfo`), empty values included, as Go and Python, so only `description missing` remains. Scope **attributes** (`otel_scope_<key>` in Go and Python) are not written: the JS 3.0 `InstrumentationScope` has no attributes (`@opentelemetry/core` `common/types.d.ts`: `name`, `version`, `schemaUrl`), so there is nothing to write — recorded* |
| `module.register` runtime-deprecated on Node 26 (DEP0205) — spike B1c F3 | `/hooks` uses sync hooks there; the `module.register` fallback runs only on versions where it is not deprecated |
| `exception.type` of a helper log record on the OTel path comes from the error's `code` when it has one (`ECONNREFUSED`), else its `name`; `exception.message` and `exception.stacktrace` from the error (`@opentelemetry/sdk-logs` 3.0 `LogRecordImpl._setException`, upstream); the **first** `Error`-valued attribute is the record exception and leaves the attributes (B8, 2026-11-02). *Compared 2026-11-02 (A23):* **Go** — otelslog 0.21.0 takes an error-valued attribute out of the attributes as the record error, the **last** one winning (`handler.go:474-478`, `b.err = err`); sdk/log 1.47.0 derives `exception.message` = `Error()` and `exception.type` = the `ErrorType()` method or `<pkg path>.<type name>` (`logger.go:176-210`, `errorType` `logger.go:226-241`), **no stacktrace**, user-set `exception.*` attributes win. **Python** — only `exc_info` makes an exception (contrib `LoggingHandler._get_attributes`, 0.66b2 `handler.py:159-170`): `exception.type` = class `__name__`, `exception.message` = `str(args[0])`, `exception.stacktrace` = the traceback; an exception object passed in `extra` stays an ordinary attribute (rendered by `str()`) | recorded; three native forms, as the stdout `exception` (contract D1 row "Stdout fallback fields", A23). First vs last error and the `code`-based type are Node/Go upstream choices. No case: the `export` kind emits through the global LoggerProvider, not the helper's logger, and asserting exception attributes on the wire needs a record-level input in three drivers (not cheap). Unit-tested in `test/logging.test.cjs` (`exception.message` on the wire) |
| Helper log attribute values on the OTel path. *Ruled 2026-11-02 (A23, owner):* plain objects are sent as **OTLP maps** and arrays as **OTLP arrays**, recursively and with any element types, as Go (otelslog 0.21.0 `convert.go:25-134`: maps → map values, slices/arrays → array values) and Python (opentelemetry-api 1.45.2 `attributes/__init__.py:41-98`, `_clean_attribute_value`: `Mapping` → map, `Sequence` → array) do; strings, numbers (NaN included) and booleans as they are, `Uint8Array` as bytes, an `Error` as its stack, a cycle as `[Circular]`, nesting deeper than 100 as `<max-depth-exceeded>` (Go's limit and marker; Go replaces the whole top-level value, Node the node at the limit). **Stay text:** `Date` (ISO 8601; Go writes `time.Time` as int64 Unix nanoseconds, Python `str(datetime)` — they disagree, so Node keeps Python's text form), bigint (`String`), symbols and functions, and other non-plain objects (`Set`, `Map`, class instances: their JSON text; Go: struct `%+v` text, Python: `str()`). *Before the ruling (B8):* objects and mixed arrays were sent as JSON text. *Amended 2026-11-02 (A23 fix round):* a `Map` is an OTLP map (keys coerced to strings) and a `Set` an OTLP array, on the stdout path an object and an array; functions are `[Function: name]`; every attribute read and conversion is guarded — a throwing getter or Proxy trap gives `[unreadable accessor]`, a value whose listing or `toJSON` throws `[unreadable <type>]`; only an attributes object that cannot be listed at all drops the record, reported once (`logging:emit-failed`, the reason cut to 64 characters, never a value). One converter serves both paths (`convert`, mode `json` / `otel`). *Corrected 2026-11-02 (A23 review):* a top-level `undefined` attribute is `null` on stdout and dropped on the OTel path (as `null`); the depth cap (100, `<max-depth-exceeded>`) applies on stdout too; a value whose prototype lookup throws (a Proxy trap, a revoked Proxy) is `[unreadable object]`, also at the top level; a `toJSON` returning `this` lists the own keys; the record exception is handed to the SDK as a plain `{name, message, stack, code}` read through guarded reads, so a hostile getter on the app's Error never runs inside the SDK nor reaches the emit-failed report | ruled; `src/internal/logging.ts` `otelValue`. Unit-tested in `test/logging.test.cjs` (nested object, homogeneous array, mixed array, cycle, depth limit; a map on the wire). No case (needs wire attributes with a record-level input in three drivers) |
| Under a registry of another `@opentelemetry/api` version (telemetry inactive, D-JS10) the helper's loggers write the stdout fallback (B8, 2026-11-02) | recorded; Go and Python have no equivalent. Unit-tested in `test/logging.test.cjs` |
| `setup()` after `shutdown()` rebuilds **all three** signals and keeps every earlier tracer, meter, instrument and Logs API logger working through the forwarding providers (Go: rebuilds and sets new globals, but a tracer obtained earlier stays on the provider it came from — the API's default provider delegates once, `delegateTraceOnce` in `go.opentelemetry.io/otel` `internal/global/state.go` `SetTracerProvider`; Python: OpenTelemetry Python sets the global Tracer/MeterProvider once per process, so traces and metrics stay off after a re-setup, design D13) (B9, 2026-11-02) | recorded; unit-tested in `test/setup.test.cjs` and `test/forwarding.test.cjs`; no fixture case (one process per case) |
| Foreign global providers: Node checks the registry **before** registering and keeps a foreign TracerProvider/MeterProvider (warned, `Resolved.inactiveSignals`), as Python's outcome; Go has no such check (`otel.SetTracerProvider` replaces). `replaceGlobals: true` (Node only) replaces the providers and the propagator, never the context manager — the app's asynchronous context depends on it; tracers the app obtained from a replaced provider keep using it (the API proxy caches its delegate) (B9, 2026-11-02). *Ruled 2026-11-02 (owner): `replaceGlobals` never replaces the context manager (reported once per process); `Resolved.inactiveSignals` is Node only and covers global ownership only, not a failed exporter build* | recorded; unit-tested in `test/setup.test.cjs`; B12 HOW-TO item |
| A failed `setup()` (after resolution) throws its error after the rollback, as Go returns it and Python raises it; the built providers are shut down in the background with no deadline of their own (Go: detached 5 s per component, Python: 200 ms) — nothing waits on them. `@staffops/otel-helper/register` reports the failure on the sink instead of throwing (a preload must not crash the app) (B9, 2026-11-02). *Ruled 2026-11-02 (owner): keep throwing after rollback; `/register` reports. Fix round: providers and pipelines are shut down at every step (M5); the report is redacted* | recorded; unit-tested (`test/setup.test.cjs`, `test/hooks.test.cjs`) |
| The ESM-without-hooks warning (JS5) recognizes an ES module entry by `--input-type=module`, an `.mjs`/`.mts` script, or a `.js`/`.ts`/extensionless script in a `"type": "module"` package (or under `--experimental-default-type=module`). A `.js` file that Node runs as ESM only by **syntax detection** (no `type` field) is not recognized and gets no warning; nor does a CommonJS entry that `import()`s ES modules later (B9, 2026-11-02) | recorded; unit-tested in `test/hooks.test.cjs`; B12 HOW-TO item |
| Observable callbacks registered through a meter obtained from the global MeterProvider follow every re-setup (they are moved to each new SDK provider) until the app removes them; `testing.reset()` discards the forwarders, so they do not leak across tests (B9, 2026-11-02). *Ruled 2026-11-02 (owner): `reset()` discards the forwarders. Fix round: they stay on the last provider through its final collection (H1), and only observables with callbacks are tracked (M7)* | recorded; unit-tested in `test/forwarding.test.cjs`; B12 HOW-TO item |
| Worker threads (B9 fix round M2, 2026-11-02): Node runs a `--require` preload (also `NODE_OPTIONS=--require=…`) again in every worker thread, an `--import` preload only in the main thread (observed with `eval` workers on 22.15.0, 24.22.2, 26.13.0). `/register` therefore installs the hooks in a worker under `--require` (*silently since 2026-11-02, B9 final round: the one INFO line per worker was dropped rather than lowered to debug, the simpler of the two*), but calls `setup()` only in the main thread; a worker that calls `setup()` itself gets its own providers and **no `/metrics` listener** (one line; the main thread owns the port), as `cluster` workers get the cluster port warning. Go has no threads of this kind; Python threads share one process state | recorded; tested in `test/hooks.test.cjs` and `test/setup.test.cjs`; B12 HOW-TO item |
| After `shutdown()` the helper's forwarders stay the API globals (pointing at nothing), so the app cannot register its **own** TracerProvider/MeterProvider/LoggerProvider/propagator without calling `trace.disable()` etc. first (the 3.0 API refuses a second registration). Go allows it (`otel.SetTracerProvider` replaces); Python cannot either (set once) (B9 fix round, 2026-11-02) | recorded; B12 HOW-TO item |
| Shutdown deadline: only the SDK 3.0 MeterProvider takes one (`shutdown({ timeoutMillis })`, passed); the TracerProvider and LoggerProvider `shutdown()` take none, so their processors use their own export timeouts and the helper's race bounds the promise. The deadline bounds the **promise**, not process exit: a provider still exporting or any other handle can keep the process alive, so the SIGTERM pattern ends in `process.exit()` (B9 fix round, 2026-11-02). *Final round 2026-11-02:* when the deadline passes, every provider stops being the forwarders' delegate before the rejection (a hung one included). A re-setup that starts before the previous `shutdown()` settled moves the observable and batch callbacks to the new provider, so the old provider's final export lacks them (no code change; B12 HOW-TO: await `shutdown()` before `setup()` again) | recorded; `shutdown()` JSDoc; B12 HOW-TO item |
| A failed setup that had registered the `trace`/`logs` globals for the first time undoes them through the registry and resets the API's own proxy (it reads the `logs` API's private `_proxyLoggerProvider`; `disable()` as fallback if the shape changes), so tracers and loggers obtained before `setup()` join the provider the app registers next (B9 fix round M3, 2026-11-02). A replaced foreign provider is put back with `set…Global…()`, a new API proxy: tracers the app took from the old one before setup stay on it | recorded; tested ("M3", "M4" in `test/setup.test.cjs`); re-check on API bumps (B13) |
| DB pool metrics (B10a "pool metrics decision", 2026-11-02): **names differ in all three languages; Node emits what upstream emits.** Node pg (instrumentation-pg 0.77, `dist/instrumentation.cjs:28-57`): `db.client.operation.duration` (s) and the current-semconv pool instruments `db.client.connection.count` (`state` `used`/`idle`), `db.client.connection.max`, `db.client.connection.pending_requests`, labelled `db.client.connection.pool.name` only (no `db.system.name`); the pool name is `host:port/database` from the Pool options, `unknown_host:unknown_port/unknown_database` when the pool is given a `connectionString` (`dist/utils.cjs:138`). Node mysql2 (instrumentation-mysql2 0.70): **no metric** (no meter use in `dist/instrumentation.cjs`). Go (XSAM otelsql v0.44, `instruments.go:26-108`): `db.client.operation.duration` and `db.sql.connection.{max_open,open,wait,wait_duration,closed_max_idle,closed_max_idle_time,closed_max_lifetime}`, every one with `db.system.name` and `db.system`. Python (contrib sqlalchemy 0.66b1, `__init__.py:205-209`): `db.client.connections.usage` only (unit `connections`), no duration *Amended 2026-11-03 (fix round, obs M1, M1):* the numbers are accurate for **one pool per pool name** only: instrumentation-pg keeps one connection-counter state per instrumentation, so two pools built from a `connectionString` share the name `unknown_host:unknown_port/unknown_database` and their series merge — pinned by `test/db.test.cjs` (max 3 + 2 → `5`, `used` 1, `idle` 0, `pending_requests` 0 after one query each). `db.client.connection.pending_requests` uses unit `{connection}` where semconv says `{request}` (upstream). Re-setup: pg re-creates its instruments on every `setMeterProvider` and moves the pool max by subtracting from the old instrument; through the stable forwarding MeterProvider both changes reached the new SDK provider and the max read 0. The `-sql` meter wrapper now gives each bind a generation, and an older generation's instruments record nothing (`providers.ts`), so the max survives (test "M1"). *Not as briefed:* skipping the re-bind for the same provider would leave the max absent from the new SDK provider; the meter is re-bound every time, the tracer is skipped when unchanged. *Amended 2026-11-03 (second fix round, N2, L-a, L-b):* the used/idle counts are wrong after a release: pg-pool emits `release` before the client returns to idle (pg-pool 3.14.0 `index.js:384-389`), and instrumentation-pg reads the pool on that event (`dist/instrumentation.cjs:283-285`), so one query leaves `used` 1 and `idle` 0 (pinned). With a **foreign** MeterProvider (not the helper's forwarder) a re-setup re-binds to the same provider and pg adds the max again, so it is double-counted; `replaceGlobals` moving the instrumentation from a foreign provider to the helper's leaves the old provider's max stale. Not fixed (the `-sql` package cannot tell a forwarder from a foreign provider without a core API); recorded. | recorded; no fixture case (the drivers have no database kind). Not implemented by the helper: a pool-metrics layer of its own would be a fourth naming. A case asserting DB metrics would take `nodejs` `skip:no-upstream-mysql2-metrics` for mysql2 |
| DB statement text (B10a, 2026-11-02): every language records `db.query.text` as the app wrote it — placeholders kept, a literal inlined by the app included — and never the bound values. instrumentation-mysql2 0.70 by default **formats the bound values into the text** when the query has values (`dist/utils.cjs:29-33`); `-sql` sets `maskStatement: true` with an identity `maskStatementHook`, so the statement stays as written (the upstream default mask would also replace inlined literals, which Go and pg keep). pg: `enhancedDatabaseReporting` (`db.postgresql.values`, `dist/utils.cjs:100`) forced off; `addSqlCommenterCommentToQueries` off in both. No connection string or user is recorded: `server.address`, `server.port`, `db.namespace` only (a pool `connectionString` is parsed for those three). A failed query's span **status message** is the server's error text (pg `dist/instrumentation.cjs:255-265`, mysql2 `endSpan`), which can quote a value the statement carried (e.g. `invalid input syntax for type integer: "…"`); pg's exception event carries only `PostgreSQL error of type '<name>' occurred (code: <SQLSTATE>)` (`dist/utils.cjs` `sanitizedErrorMessage`). Go XSAM records the same text in the exception event (`span.RecordError(err)`) with an empty status message (`utils.go:57-58`); Python not compared. Upstream behavior, not changed *Amended 2026-11-03 (fix round R1, task A25):* the status message is no longer the server text: every language keeps ERROR with no description (contract D1 row "DB error status"; Node `dbTracerProvider` wraps `setStatus`), asserted for a pg invalid-input error and a mysql2 duplicate key and parse error that quote bound values; the residual is `exception.message` (contract D1 row "DB error text in exception events", README gap). *Added 2026-11-03 (obs L4):* recording non-parameterized text as written deviates in **all** languages from the semconv SHOULD (sanitize or omit `db.query.text` that is not parameterized) — contract follow-up, no change. *Amended 2026-11-03 (second fix round, N1):* when the Node wrapper drops an ERROR description and the span has no `error.type`, it sets `error.type=_OTHER` (semconv fallback value); an existing one is kept (`test/sql.test.cjs`, `test/db.test.cjs`). Go and Python set no `error.type` on DB spans (unchanged). The WARN for an unresolved library named `true` lists only the entry directory and cwd; the full lookup chain is on a debug line (L-d, N3). | recorded; `test/db.test.cjs` asserts no bound value or password in any span attribute, event or status for the tested statements |
| pg `error.type` on `db.client.operation.duration`: set (SQLSTATE, e.g. `42P01`) only by the **callback** form (`pool.query` uses it); a `client.query()` promise that rejects records the duration without `error.type` (instrumentation-pg 0.77 `dist/utils.cjs:126` vs `dist/instrumentation.cjs:255-265`) (B10a, 2026-11-02) *Amended 2026-11-03 (obs L3):* no language sets `error.type` or `db.response.status_code` on DB **spans** (Go XSAM: `error.type` and `db.operation.name` on the metric only, `utils.go:70-72`; Node pg on the metric only; mysql2 none). Not added by the Node status wrapper: it sees the status, not the error (taking `err.code` would mean patching the callbacks) — recorded gap. | recorded; asserted in `test/db.test.cjs` |
| DB span names: pg `pg.query:<OPERATION> <database>`, `pg.connect`, `pg-pool.connect`; mysql2 the statement's first word (`SELECT`); Go XSAM the method, `sql.conn.query`, `sql.conn.exec`, `sql.connector.connect`, … (`methods.go:25-43`) (B10a, 2026-11-02; Python not compared) *Amended 2026-11-03 (fix round obs M3):* mysql2 names are normalized by the `-sql` tracer from `db.query.text`: leading whitespace and comments (`--`, `#`, `/* */`) skipped, the first word uppercased and cut at 32 characters, fallback `db.system.name` (`mysql`) — `providers.ts` `statementSpanName`, tests for a template literal, a comment and a no-space statement (`SELECT(1)`). *Added 2026-11-03 (obs M2):* `db.operation.name` differs: Node pg the SQL verb from the text (`SELECT`, on the metric), Go XSAM the method (`sql.conn.query`, on the metric, `utils.go:70`). | recorded; no case |
| `OTEL_SEMCONV_STABILITY_OPT_IN` set **without any database token** (e.g. `http`): Node `-sql` and Go keep both keys (`dup \|\| !stable`, `go/ext/otelsql/sql.go` `legacyKeysEnabled`); Python's contrib switches to its old mode and emits only `db.system` (`python/staffops_otel_helper/_semconv.py` docstring: "`http` alone keeps only the legacy database keys"). The contract (R10 "Database semconv") rules only unset and `database` without `/dup` (B10a, 2026-11-02) *Ruled 2026-11-03 (task A25, contract D1 row "Database semconv opt-in"):* Go and Node are the target; Python now normalizes the variable (ASCII fold; `database/dup` appended to an empty or database-less value), so `http`, an empty value and `DATABASE` behave alike in all three; Go parses with the contract trim set and ASCII folding (NBSP no longer trimmed). Node cases: empty → both keys, `DATABASE` → new key only (`test/sql.test.cjs`). | recorded; Go/Python divergence the contract does not rule — reported for a ruling, no Node change |
| A library loaded before `sqlInstrumentation()` (B10a, 2026-11-02): the first enable requires the cached `pg`, `pg-pool`, `mysql2`, `mysql2/promise.js` and `mysql2/lib/connection.js` again by path, so require-in-the-middle patches their prototypes in place (as the core does for express and grpc-js); clients built before are instrumented too, nothing is reported. The contrib instrumentations are built **once per process** and shared by every `sqlInstrumentation()` result: a second set would register its hook after the library was handed to require-in-the-middle and patch nothing *Amended 2026-11-03 (fix round):* pg `lib/client.js` and `lib/native/client.js` re-required too, a main entry only when it is cached (L1, code-review L1); covered without a database by a stub `node_modules/pg` (R4). Library detection (R3): every library whose contrib module loads is included; one the app dirs (entry, cwd, `require.main.paths`) do not resolve is still patched when the app loads it (a nested, non-hoisted copy) and reported once on the first enable, `'pg' could not be resolved from <dirs>; instrumented if it loads`, through the helper's DiagLogger — *amended 2026-11-03 (owner):* at **WARN only for a library named `true`**, at `diag` debug in default mode (a pg-only app must not warn about mysql2 on every start; visible with `OTEL_LOG_LEVEL=DEBUG`); `{ pg: true }` never fails on resolution; only a contrib module that does not load throws (no library at all, or the one named `true`). The shared contrib instrumentations count enables: each is on while any result is enabled; the database mode is read once per process (first `sqlInstrumentation()`). Pass one `sqlInstrumentation()` per `setup()`. | recorded; `test/db.test.cjs` "pg loaded before sqlInstrumentation() and setup()" |
| DB connection attributes (fix round obs M2, 2026-11-03): Node pg and mysql2 spans carry `db.namespace`, `server.address`, `server.port` (from the connection options or a parsed `connectionString`); Go `ext/otelsql` spans carry none — XSAM offers `AttributesFromDSN` (`helpers.go:28`), which the helper does not pass; Python contrib sqlalchemy sets them from the engine URL | recorded; **backlog (contract level, R10):** Go adopting `AttributesFromDSN` — not implemented in this change |
| mysql2 `query(sql, values)` with **no callback** (the event-emitter form with values): instrumentation-mysql2 0.70 wraps only the one-argument emitter form and the callback forms, so this call's span is never ended or exported (`dist/instrumentation.cjs` `_patchQuery`) (dev L2, 2026-11-03) | recorded; upstream issue to be filed by the owner; the promise API and callbacks are unaffected |
| instrumentation-pg 0.77 has `@types/pg` and `@types/pg-pool` as **runtime** dependencies, so installing `-sql` adds them to the app's tree (code-review L10, 2026-11-03) | recorded; B12 CONSUMING item |
| Opt-in instrumentations are matched **by name** (M2, 2026-11-03): two for one library (`instrumentationName`, plus a composite's `coveredInstrumentations`) or one the core installs itself (`@opentelemetry/instrumentation-{http,undici,express,grpc}`) make `setup()` throw a TypeError — a raw `MySQL2Instrumentation` next to `sqlInstrumentation()` would patch twice and drop the statement mask. The same object twice is still enabled once. Every opt-in object of a call that does not enable it is turned off: a rollback at any step, an inactive copy, another API version's registry, `OTEL_SDK_DISABLED=true`, and a `setup()` while one is active (those not used by the active setup; named in its report) (M3) | recorded; core `test/opt-in.test.cjs` |
| Log bridges, stdout `level` of a level outside DEBUG/INFO/WARN/ERROR (pino `trace`/`fatal`, winston `emerg`, `silly`…): Node writes `DEBUG` below info and `ERROR` above error, as Python `_slog_level` (`CRITICAL` → `ERROR`); Go `NewSlogHandler` writes slog's offset text (`DEBUG-4`, `ERROR+4`) (B10d, 2026-11-02) | recorded; Python behavior. Unit-tested in `otel-helper-pino/test/pino.test.cjs` and `otel-helper-winston/test/winston.test.cjs` |
| Log bridges, OTel `severityText`: the source level name upper-cased (`INFO`, `HTTP`, `NOTICE`, `EMERG`, a pino custom label); Python writes the stdlib `levelname` (`WARNING`, `CRITICAL`), Go otelslog slog's text. All three carry the source's own text; the texts differ because the sources do. `severityNumber` is the bucket (TRACE 1, DEBUG 5, INFO 9, WARN 13, ERROR 17, FATAL 21) (B10d, 2026-11-02). *Added 2026-11-03 (fix round 2): every level of a range gets the range's lowest number (winston `http` and `verbose` are both DEBUG 5, syslog `emerg`/`alert`/`crit` all FATAL 21, a pino custom 35 is INFO 9), so the ordering within a range is not kept in `severityNumber`; `severityText` keeps the name. Go otelslog maps slog's offsets inside a range (`DEBUG+1` → DEBUG2); the Python mapping inside a range not checked*  *Corrected 2026-11-04 (B14, obs L1):* the Python statement above was wrong. Python writes `levelname` except `WARNING` → `WARN` and `CRITICAL` → `FATAL` (`opentelemetry-instrumentation-logging` 0.66b2 `handler.py:184-188`, `_python_to_otel_severity_text`, read in `python:3.13-slim` on 2026-11-04), as contract D1 row "OTel log scope name" says; `DEBUG`, `INFO`, `ERROR` and custom level names pass through. | recorded; asserted on decoded OTLP records in both suites |
| pino destination: `level`, `time`, `pid`, `hostname` of pino's line are not record fields, and the record time is the write time, not pino's `time` (B10d, 2026-11-02). *Corrected 2026-11-03 (fix round): `pid`/`hostname` are left out only when they equal this process's pid and `os.hostname()` (pino's default `base`); other values stay fields* | recorded; Node-only (Go and Python have no pino). Unit-tested |
| winston transport, trace correlation: from the active context when winston calls the transport — inside the logging call, except when a slower transport of the same logger applies backpressure (winston buffers the record and delivers it later, uncorrelated). Go correlates only through the context-taking slog calls; Python always at handler time (B10d, 2026-11-02). *Corrected 2026-11-03 (fix round, HIGH-1): "uncorrelated" was wrong — a buffered record took the context active at delivery, i.e. another span's. Now the transport stamps each record with its call's context and time (wrapped `logger.write`, D-JS6 fix-round note); a record without a stamp (the transport used outside a logger, winston's exception handlers) is written with `ROOT_CONTEXT`, uncorrelated. Buffered records keep their call's trace and call time*. *Corrected 2026-11-03 (fix round 2): winston's exception handlers log through `logger.log`, so their records are stamped too; the stamp now lives in a process-wide `WeakMap` (nothing added to the info, D-JS6 "Fix round 2")* | recorded; regression test "HIGH-1" in `otel-helper-winston/test/winston.test.cjs` (two concurrent spans, slow transport, zero misattribution) |
| Log bridges, non-string message: the body is the converted value — an OTLP map (or array, number…) on the OTel path, its JSON text as the stdout `msg` (winston `logger.info({a: 1})` makes the object the message; pino `{msg: {…}}`). No message: no OTel body, stdout `msg` `""`. Python's stdlib `logging` renders a non-string message with `str()` (`{'a': 1}`); Go slog messages are always strings, no equivalent; `createLogger` keeps its string message (B10d fix round, 2026-11-03) | recorded; unit-tested in both suites and `test/logging.test.cjs` |
| pino serialized error extras: the keys of pino's serialized `err` other than `type`, `message`, `stack`, `name` (`statusCode`, `details`, `code`, custom properties) stay one **map attribute under the logger's error key** (`err` by default), next to the record exception; none: no such field. The exception type is a string `name` before pino's `type`; pino's `message` and `stack` already contain the causes. Go/Python have no pino; their Error attributes keep no extra properties (B10d fix round, 2026-11-03) | recorded; unit-tested |
| winston `format.errors({ stack: true })` (logger level) flattens the Error before the transport: when no Error is found and `info.stack` is a string whose first line is `<Name>: <message>`, the transport rebuilds the record exception from it (name, message, stack) and leaves out `stack`. A transport-level format copies the info with `Object.assign` (own enumerable keys only), so `logger.error(err)` reaches such a transport without the Error's message and stack — winston behavior, not fixable in the transport (B10d fix round, 2026-11-03). *Updated 2026-11-03 (final round; the rule changed in fix round 2): the rebuild needs a real stack (a head line and at least one `\n    at ` frame); the head is `Name`, `Name: message` or `Name [CODE]: message`; a bare `Name` takes its message from `info.message`; the info's `code` (else the bracketed code) is the error's `code`, so `exception.type` is the code, as for a live Error; and since the final round the object `format.errors()` returns keeps the call's trace (logger `_transform` wrapped)* | recorded; unit-tested; HOW-TO item for B12 |
| pino `pino.multistream([{ stream: createPinoDestination() }, { stream: process.stdout }])`: before `setup()` (or without a logs exporter) both streams write stdout, so each record appears twice. Do not combine the destination with `process.stdout`; no option (B10d fix round, 2026-11-03) | recorded; HOW-TO item for B12 |
| pino integers past `Number.MAX_SAFE_INTEGER` (pino serializes BigInt as digits) keep their digits as **text** on both paths (`JSON.parse` reviver with source text, available on the 22.15.0 floor); Go and Python keep integers exact (B10d fix round, 2026-11-03) | recorded; unit-tested |
| Log bridges, record time: pino records take the write time (synchronous); winston records take the **call** time from the transport's stamp, also when buffered; an unstamped winston record the delivery time. `format.timestamp()` leaves its own `timestamp` field (a string attribute) next to the record time (B10d fix round, 2026-11-03) | recorded; unit-tested (call-order times under backpressure) |
| sdk-logs 3.0 `BatchLogRecordProcessorBase._flushAll` returns at once while another flush runs (`if (this._flushing) return`): a concurrent `forceFlush()` resolves before its records are exported, and a `shutdown()` during a flush shuts the exporter down with records still buffered. The helper serializes the bridges' flushes (`flushBridgeRecords`, one in flight plus one shared follow-up) and `shutdown()` waits for them first; an app calling `LoggerProvider.forceFlush()` itself concurrently is not covered. Go and Python not checked for the same early return (B10d fix round 2, 2026-11-03). *Amended 2026-11-03 (final round): serialized **per LoggerProvider** — a flush for a new setup's provider never queues behind an older (perhaps hung) provider's, and `shutdown()` waits only for the flushes of the provider it shuts down that were pending when it was called (no waiting loop); a flush failure is reported redacted (`redactErrorText`), read guarded* | recorded; tests "HIGH-2" in `test/logging.test.cjs`. **Upstream issue to file** (opentelemetry-js, sdk-logs `_flushAll` early return); re-check on SDK bumps (B13) |
| winston: ~~a logger-level format that returns a **new** info object (e.g. `format.errors()` for an Error, a custom format building a fresh object) leaves the record without its call stamp, so it is written with `ROOT_CONTEXT` (uncorrelated) and the delivery time.~~ The same holds for a reused info or Error that winston buffers twice before delivering it: the later call's stamp replaces the earlier. A transport-level format is fine: the transport finds the stamp before winston-transport copies the info (B10d fix round 2, 2026-11-03). *Narrowed 2026-11-03 (final round): the logger's `_transform` is wrapped too, so the object a logger-level format returns gets the incoming info's stamp (sync and buffered `format.errors()` tested on span S); what remains is the **reused-object** case (a reused info or Error buffered twice gets the later call's context and time). Corner cases not covered: an object replaced outside the logger's `_transform` (a custom stream piped between the logger and the transport, or a record written before the transport was attached — uncorrelated, root context); another transport's own format only changes that transport's copy* | recorded; tested ("our transport's own format keeps correlation; a logger-level format that replaces the info keeps it too", "final round: logger-level format.errors() in span S") |
| winston `handleExceptions` / `handleRejections` records (`getAllInfo`): the error is the record exception; the body is the first line of winston's message (`uncaughtException: <msg>`); `error`, `exception`, `trace`, the copied `stack`, `date`, `process` (its `argv` can carry secrets) and `os` are not fields. winston-specific; nothing to compare in Go or Python (B10d fix round 2, 2026-11-03) | recorded; tested through `getAllInfo` and `_uncaughtException` |
| pino destination: a chunk with inner newlines is one record per line, so a **direct** write of non-JSON text containing newlines (not pino's own output) becomes several records, each with the text of its line as the body (B10d fix round 2, 2026-11-03) | recorded; unit-tested |
| Redis span names (B10b, 2026-11-03; **ruled 2026-11-03, Q3**): the contribs name spans node-redis `redis-<COMMAND>` (`redis-SET`, `redis-connect`) and ioredis the lower-case command (`set`, `connect`). `-redis` renames them (`commandSpanName`, the `internal/db` tracer `spanName` rule) to the command uppercased (`SET`, as Python and semconv `{db.operation.name}`) and connection spans to `connect`. MULTI/pipeline: one span per command (both contribs), each named by its command; Python names its one pipeline span `SET GET`. Go keeps `set`, `redis.pipeline set get`, `redis.dial` (contract D1 row "Redis span names": parity gap, Go rename on the backlog) *Reworded 2026-11-03 (A26 fix round, obs M2/dev L2):* uppercasing is a parity choice with Python, **not semconv** (semconv: `{db.operation.name} {target}`, `db.operation.name` as provided); the helper keeps `db.operation.name` as provided and omits `{target}` (contract D1 "Redis span names") | tested (`test/redis.test.cjs`, `test/fixtures/scenario.cjs`); no case (no redis parity case exists) |
| Redis `db.query.text` (B10b, 2026-11-03): Node and Python `SET ? ?` (the command and one `?` per argument; Node uppercases the ioredis command, cut at 1000 characters as Python); Go records no statement (`WithDBStatement(false)`), both allowed by contract D14. Node replaces upstream's `defaultDbStatementSerializer`, which keeps keys and, for `GET`/`DEL`/`EVAL`/`SISMEMBER`/`LREM`/`ZSCORE`/..., every argument. In dup mode Python also writes the legacy `db.statement`; Node copies only `db.system`, as `-sql` (**ruled 2026-11-03, Q4: kept**; Python's extra legacy keys are the recorded difference) | recorded; tested (`test/fixtures/scenario.cjs`) |
| Redis span attributes (B10b, 2026-11-03): Node emits `db.operation.name` (node-redis `SET`, `MULTI`, `PIPELINE`; ioredis `set`, `MULTI set`, `PIPELINE set`); Go and Python emit none. Python adds `db.redis.database_index`, `db.redis.args_length`, `network.transport`; Go `db.redis.num_cmd` on pipelines and `code.*`. `server.address`/`server.port` everywhere *Amended 2026-11-03 (A26 fix round):* Go now adds `db.operation.name` as provided (`set`; contract D1 row "Redis `db.operation.name`"), Python still none (backlog). `db.namespace` (the database index) is **absent in all three** (Python has `db.redis.database_index` instead). On a failed span all three set `error.type` and `db.response.status_code` from the reply code (D1 "DB error status") *Decided 2026-11-04 (B14 re-probe L3): `db.operation.name` stays as the client provides it — node-redis `SET`, ioredis `set` — with no case normalization: semconv `docs/db/redis.md` (Development) footnote [1] "It is RECOMMENDED to capture the value as provided by the application without attempting to do any case normalization", and contract D1 row "Redis `db.operation.name`" already says "as provided" (Go `set` follows go-redis). The span name is the normalized field (uppercased, Q3). Documented in HOW-TO §9 and the `-redis` README; no test change (`test/fixtures/scenario.cjs` pins both casings).* | recorded |
| Redis spans per call (B10b, 2026-11-03): node-redis and ioredis contribs make **one span per command** in MULTI and pipelines (Go and Python one span per pipeline); a failed node-redis pipeline marks every command span ERROR (upstream fills the error for all when the reply is not a `MultiErrorReply`). node-redis connection setup is one `connect` span (contrib `redis-connect`; its HELLO/AUTH/CLIENT handshake is not traced; nor are SUBSCRIBE/UNSUBSCRIBE/QUIT, which bypass `sendCommand`); ioredis traces `connect` and every handshake command (`HELLO`, `CLIENT`, `INFO` after the Q3 rename) and SUBSCRIBE. ioredis `connect` ends at once (OK even when the connection is then refused) *Amended 2026-11-04 (B14 re-probe M2): ioredis sends a queued command a second time through `Redis.prototype.sendCommand` when it replays its offline queue (a command issued before `ready`, a resend after a reconnect or `reconnectOnError` = 2; ioredis 6 `built/Redis.js:625`, `:766-768`), and instrumentation-ioredis 0.72 started a second span for the same command object (two spans, both ended by the reply). `-redis` wraps the contrib's `_traceSendCommand` patch factory (`oneSpanPerCommand`, `src/internal/redis-instrumentation.ts`): a command object already seen goes straight to ioredis's `sendCommand`, so the first span — which covers the queueing — is the only one. Relies on a private contrib method: when it is renamed the instance is returned unchanged and `test/server.test.cjs` "one span per command issued before ready" fails (real Redis). Tests: that case (ioredis before/after `ready`, node-redis before `connect()` resolves).* | recorded; tested |
| Redis metrics (B10b, 2026-11-03): instrumentation-redis/-ioredis 0.72 record **no metrics**; Go `ext/otelredis` emits the redisotel pool metrics (`db.client.connections.*`, D14); Python none. `-redis` binds the meter wrapper anyway (legacy key, generation guard) for a contrib release that adds them | recorded; README gap (B12) |
| Redis ERROR description (B10b, 2026-11-03): Node drops it with `error.type=_OTHER` (A25 rule applied to Redis, parity with `-sql`). Go and Python kept it before A26 — reproduced 2026-11-03 against `redis:7.4.12-alpine`: Go `nosuchcmd` status `ERR unknown command 'NOSUCHCMD', with args beginning with: 'secret-arg'`, Python `ResponseError: unknown command 'NOSUCHCMD', with args beginning with: 'secret-arg'`. **Ruled 2026-11-03 (Q1, contract task A26): A25 covers every DB client**; Go `ext/otelredis` and Python `ext.redis` now drop it too (Node alone sets `error.type=_OTHER`). The exception event keeps the server text in all three (**Q2: the accepted A25 residual**, contract D1 row "DB error text in exception events") *Amended 2026-11-03 (A26 fix round, obs M4):* `-redis` passes `internal/db` `responseStatusCode` (`redisResponseCode`): `error.type` and `db.response.status_code` are the reply's leading code (`WRONGTYPE`, `ERR`, `NOAUTH`, `WRONGPASS`; a Lua custom code too), `_OTHER` without one (`connect ECONNREFUSED`, `Connection is closed.`); `-sql` keeps `_OTHER` | ruled; tested in all three languages (A26) |
| ES module apps without the loader hooks (B10b, 2026-11-03): node-redis is instrumented (its patched file, `@redis/client` `dist/lib/client/index.js`, is loaded by `require()` inside the package), **ioredis is not** (its patched file is the package entry, which an ES module `import` loads without `require()`; the JS5 warning). So "CommonJS libraries work without the hooks" (B10a) holds only when the patched file is not the entry | recorded; tested; B12 HOW-TO item |
| Database semconv mode read **once per process** by the first DB factory (B10b, 2026-11-03): since the rule moved to the core `internal/db` entry, the first `sqlInstrumentation()` **or** `redisInstrumentation()` reads `OTEL_SEMCONV_STABILITY_OPT_IN` for both packages (before B10b, `-sql` alone, at its first call). A change to the variable after that call has no effect in either package. Go reads it per `otelsql.Open` call; Python normalizes it before contrib reads it | recorded; tested (`packages/otel-helper/test/db.test.cjs`) |
| ioredis `requireParentSpan: false` (B10b decision; consequences recorded 2026-11-03, A26 fix round dev M1/code-review M5): spans without a parent, as Go, Python and node-redis, so app commands outside a request are traced. Consequence: every connection and **every reconnect** emits root traces — `connect`, then the handshake `HELLO`, `CLIENT` (SETINFO ×2), `INFO` (ready check), plus `AUTH`/`SELECT` when configured — and they are ERROR roots while a server is down or failing over; ioredis Cluster adds `CLUSTER SLOTS` refreshes, Sentinel its sentinel queries (`SENTINEL get-master-addr-by-name`, roles). **Not suppressed:** the handshake cannot be told apart reliably at the tracer the helper wraps — the contrib ends `connect` synchronously (the handshake runs later, outside its context), and the client's `status` is not visible there; matching command names would also drop an app's own `INFO`/`CLIENT` calls. Mitigation is sampling at the Collector (the tail-sampling policy drops root spans of those names) or a parent span around the client's lifetime *Corrected 2026-11-03 (A26 final round, dev L2):* while the server is down each retry emits **one OK `connect` root** (ioredis ends it at once; nothing else is sent); the handshake roots (`HELLO`, `CLIENT`, `INFO`) are ERROR when the server **rejects the auth or is loading**; Cluster `CLUSTER SLOTS` refreshes run on reconnects and topology errors, and periodically only when `slotsRefreshInterval` is set. A Collector drop policy for these names is **something to add** (not in place today) | recorded; B12 HOW-TO item |
| Go `CLIENT MAINT_NOTIFICATIONS` false ERROR (A26 fix round, 2026-11-03): go-redis v9.23 sends `CLIENT MAINT_NOTIFICATIONS ON …` in its handshake; Redis before 8 answers `ERR unknown subcommand`, which go-redis ignores but redisotel records as an ERROR `client` span (`error.type=ERR` since A26) on every connection (reproduced on redis:7.4.12-alpine). node-redis sends it too but does not trace its handshake; ioredis does not send it | recorded; B12 HOW-TO item (Go docs) |
| AWS SDK v3 spans (B10c, 2026-11-03): `@opentelemetry/instrumentation-aws-sdk` 0.79 writes the legacy RPC keys `rpc.system=aws-api`, `rpc.service` (`S3`, `DynamoDB`), `rpc.method` (`PutObject`) and `cloud.region`, as Python botocore 0.63b0–0.66b1; Go `otelaws` v0.72 writes the current `rpc.system.name=aws-api` and `rpc.method=<Service>/<Operation>` (`SQS/ListQueues`, `go/ext/otelaws/aws_test.go`). Span names: Node and Python `<Service>.<Operation>` for most calls, messaging `<queue> send`/`receive` and `<topic> send`, Lambda `<function> Invoke`. The contrib writes the non-semconv `aws.request.id`; `-aws` also writes the semconv `aws.request_id` (Go, Python) and `aws.region` (Go) next to `cloud.region` (`attributes.ts` `ALIASES`; both keys kept), and on errors the request id from `$metadata.requestId` (written as `aws.request_id` directly; the contrib reads only a body `RequestId`). *Removal trigger (code-review L2, 2026-11-03):* the duplicate `aws.request.id` is dropped when the contrib writes the semconv `aws.request_id` itself, or at 1.0, whichever comes first. Python also writes `server.address`/`server.port` and `retry_attempts`; Node has neither (the contrib does not see the endpoint) | recorded; *ruled 2026-11-03 (Q3): a parity gap with a backlog item, contract D1 row "AWS RPC keys"*; README "Known emitted-telemetry gaps" (B12); no fixture case asserts AWS spans |
| AWS HTTP spans under the SDK span (B10c, 2026-11-03): the SDK sends through `node:http`, so without suppression every `send()` attempt also made an instrumentation-http CLIENT span (with `url.path` `/<bucket>/<key>` and `url.query`). `-aws` sets the contrib's `suppressInternalInstrumentation: true`: one SDK span per `send()`, retries included, and no HTTP span under it — as Python botocore (`suppress_http_instrumentation()`) and Go (otelaws is middleware; the SDK's HTTP client is not otelhttp). An app request outside an SDK call is still traced (test: the presigned `fetch` in `sdk.test.cjs`). *Amended 2026-11-03 (code-review M1/L6):* a streamed response body (S3 `GetObject` `Body`) emits its events in the suppressed context of the SDK's HTTP request, so a `fetch` in its `end` listener records no span (reproduced on Node 24). Not fixed: instrumentation-http already binds the response EventEmitter to that context and `context-async-hooks` keeps the first bind (`_bindEventEmitter` returns early), so a re-bind in a contrib `responseHook` is a no-op (tried 2026-11-03); patching the stream's listener methods ourselves was judged too invasive. **Documented gap** (B12 HOW-TO): save `context.active()` before reading the body and wrap the listener's work in `context.with(saved, …)` (test M1 in `sdk.test.cjs` pins both behaviors). The suppression also covers the HTTP **client metrics** of the SDK's own requests (`http.client.request.duration` is not recorded for AWS calls; Python likewise) | decided; tests `sdk.test.cjs` |
| AWS ERROR description and `error.type` (B10c, 2026-11-03): the contrib sets `setStatus({ code: ERROR, message: err.message })` and records the exception. `-aws` binds it to `internal/db` `dbTracerProvider`, so the status keeps ERROR without description (the A25 rule; AWS messages can quote request values, e.g. a DynamoDB `ValidationException`, or name parameters) and `error.type` is the AWS error code, from the contrib's `exceptionHook`: the SDK error's `name` (a service exception's name is its code: `NoSuchKey`, `AccessDenied`, `SignatureDoesNotMatch`, `ThrottlingException`, `QueueDoesNotExist`, `NotFoundException`), else its `Code`, when it matches `^[A-Z][A-Za-z0-9]{1,63}$` (AWS codes are a bounded set of PascalCase identifiers; a dotted query code such as `AWS.SimpleQueueService.NonExistentQueue` does not match); else `_OTHER`. A transport error gives its Node `code` when it has the code shape (`ECONNREFUSED`; code-review L3, 2026-11-03), else its JS class (`Error`, `TypeError`), as instrumentation-http. No `aws.*` key: semconv defines none for the error code. The semconv `exception` event keeps the message (the A25 residual, as SQL and Redis). Go `otelaws` and Python botocore kept the description (`span.set_status(StatusCode.ERROR, str(error))`, botocore `__init__.py:298, 304`) and set no AWS-code `error.type`; *ruled 2026-11-03 (Q1): the rule is the contract's in all three languages, task A27 (contract D1 row "AWS error status")* | decided (A27); tests `sdk.test.cjs`, `aws.test.cjs` |
| AWS payloads and credentials (B10c, 2026-11-03): never recorded — no `dynamoDBStatementSerializer` (no `db.query.text`, which would serialize the request input), `sqsExtractContextPropagationFromPayload: false`, no request/response hooks; the contrib records no bodies, headers or URLs of the SDK request (`url.full` of SQS is the queue URL). Two contrib attributes are changed (`attributes.ts`, applied through `sanitizeAttributes` at span start and on every `setAttribute`): `aws.dynamodb.item_collection_metrics` is **dropped** (it holds `ItemCollectionKey`, the partition-key value; **Python botocore records it**), and an SNS `Publish` to a phone number writes `messaging.destination` / `messaging.destination.name` as `phone_number:**` in the attributes and the span name (the contrib writes the number; Python botocore redacts it the same way). *Narrowed 2026-11-03 (code-review M2):* the exception event keeps the AWS message, and SNS `InvalidParameter` quotes the number there (`Invalid parameter: PhoneNumber Reason: <number> is not valid to publish to`): the A25 residual, covered by the secrets sweep (attributes, name, status, diagnostics and metrics never carry it). The redaction needs `messaging.system` and the destination in the same attribute batch (the contrib sets both at span start; a unit case pins it, L4). A Lambda function that threw (`200` + `X-Amz-Function-Error: Unhandled`) is a successful AWS call: no ERROR, and the error payload is not recorded (as Python botocore; L5). The sweep (M3) also checks the diagnostics (DEBUG) and the helper's `/metrics` output. A presigned URL the app fetches is redacted by the core HTTP instrumentations (D-JS14 URL redaction row). Bedrock `gen_ai.*` request parameters (stop sequences) are recorded as the contrib writes them | decided; secrets sweep `sdk.test.cjs` (access key id, secret, session token, request signatures, presigned query values, every body); *ruled 2026-11-03 (Q2): Python drops `item_collection_metrics` too, task A27 (contract D1 row "AWS payload attributes")* |
| AWS SDK detection and loading (B10c, 2026-11-03): one library, `aws-sdk`, resolved via `@smithy/core`, `@smithy/smithy-client` or `@aws-sdk/smithy-client` (what the contrib patches: `Client.prototype.send`); unresolved → a debug line, never an error; the factory throws only when the contrib module does not load (JS8 amendment, D14 wording `AWS instrumentation not installed` / `failed to import the AWS instrumentation`). A client built before `setup()` is instrumented by re-requiring the cached `@smithy/core` `dist-cjs/submodules/client/index.js` (the `send` patch then patches the client's own middleware stack on each call). `awsInstrumentation()` takes no options yet (any key is a TypeError). ES module apps: `@aws-sdk/client-*` 3.x have no `exports` map, so `import` loads `dist-cjs` and the patched file by `require()` — instrumented with or without the loader hooks; the JS5 warning names no AWS package | decided; tests `aws.test.cjs`, `sdk.test.cjs` (preload, ESM preload with and without hooks) |
| AWS metrics (B10c, 2026-11-03): the contrib records metrics only for Bedrock (`gen_ai.client.*`), re-created on each MeterProvider bind and held only by the contrib, so `-aws` binds the MeterProvider as is (no generation guard needed); Go `ext/otelaws` installs tracing middleware only (`Instrument`, contrib `AppendMiddlewares`); Python botocore 0.66b1 records metrics only through its Bedrock extension | recorded |
| AWS `aws.extended_request_id` (B10c fix round, 2026-11-03, obs L5): the contrib writes the S3 host id as the non-semconv `aws.request.extended_id`; `-aws` also writes the semconv `aws.extended_request_id` (`attributes.ts` `ALIASES`). Removal trigger: as `aws.request.id` (the contrib writes the semconv key, or 1.0) | decided; tests `aws.test.cjs`, `sdk.test.cjs` |
| S3 presign (`getSignedUrl`, B10c fix round, 2026-11-03, dev L1): `@aws-sdk/s3-request-presigner` resolves the client's middleware stack without sending, and the contrib's patched `resolve` still opens an SDK span, so presigning records a phantom `S3.GetObject` (or `PutObject`) CLIENT span with no HTTP request behind it (reported by the B10c dev review; not reproduced here: the presigner is not a test dependency). Go's `s3.PresignClient` runs the middleware stack too, so `ext/otelaws` can record the same phantom span; Python's `generate_presigned_url` does not call the instrumented `_make_api_call` (dev L4, 2026-11-03) | recorded; B12 HOW-TO (drop it by name/attributes in the Collector if it matters); contract D1 "AWS error status" |
| URL query shape after redaction (A28 fix round, 2026-11-03, dev L2): Node clients (`node:http`) re-serialise the query through `URLSearchParams` when a listed name is present — duplicates collapsed to one, names decoded, `;`/`=` encoded on the server `url.query`, the fragment dropped — while Go and Python rewrite only the listed values and keep every other byte. The same names are redacted in all three | recorded (contract D1 "URL query redaction") |
| Hand-injected `traceparent` on `fetch` (B11b, 2026-11-03): instrumentation-undici 0.34 injects the client span's context by **appending** headers (`request.addHeader` / `headers.push`, `dist/undici.cjs` lines 137–143), so an app that also calls `propagation.inject()` into the `fetch` headers sends `traceparent` twice; the B11b run with manual injection gave the receiving `POST /notify` SERVER span no parent (a new trace each time), and the run without it gave one trace across node-process → node-api → node-backend. `node-process` injects nothing. *Fix round 2026-11-03 (obs M3, L6):* python-process's own manual injection was a no-op — the httpx instrumentation writes the header through httpx `Headers.__setitem__`, which replaces an existing `traceparent` — and was removed from `python/example/python-process/workers.py`; the evidence for the failure mode is fetch/undici only (`node:http` was not tested) | recorded; B12 HOW-TO item (never `propagation.inject()` into the headers of an instrumented `fetch`); no case |
| `http.route` template syntax follows the framework (B11b, 2026-11-03): express writes `/notify/:notificationId`, FastAPI `/notify/{notification_id}`, Go `ServeMux` `/status/{id}`; span names and the `http.server.request.duration` label differ with it, so a query on the route value is per language | recorded; EXAMPLES.md (B11c) notes it; no case |
| ~~`startRootSpan`, `getTracer`, `getMeter` (design "Layout and public API" table) are not exported by the core yet (B11b, 2026-11-03): no task implements them. The examples use `@opentelemetry/api` (`trace.getTracer`, `metrics.getMeter`, `tracer.startActiveSpan(name, {}, ROOT_CONTEXT, fn)`)~~ | *Ruled 2026-11-03 (owner): implemented in the core for parity (B11b; `src/internal/api.ts`, `test/api.test.cjs`, type checks `test/types/api.test-d.ts`); the examples use them. Row closed* |
| Example HTTP error responses (B11b fix round, 2026-11-03, dev M3): node-api answers JSON in every case — 422 `{"detail": "<field>: …"}` (one string) for an invalid body, an explicit `null` included; 400 `{"detail": "Bad Request"}` for malformed JSON; 404 `{"detail": "Not Found"}`; 500 `{"detail": "Internal Server Error"}` after a `Request failed` log record — a 4xx/5xx `detail` is the fixed `http.STATUS_CODES` text, never the error's message (final round, 2026-11-03). FastAPI (python-api): 422 `{"detail": [{loc, msg, type, …}]}` (a list) for an invalid body **and** for malformed JSON (no 400); 404 the same body; 500 `Internal Server Error` as **plain text**, the error logged by uvicorn, not through the helper | recorded; B11c EXAMPLES.md item; no case (example apps, not the helper) |
| Example probe server instrumented (B11b fix round, 2026-11-03, obs M2): node-process's `/health`/`/healthz` server on `:8080` is a `node:http` server, so the helper instruments it — no span (default health paths) but a `http.server.request.duration` point per probe; python-process's aiohttp probe server is not instrumented (no aiohttp server instrumentation in the helper), so its probes record nothing. Go `go-process` serves `/health` on `:8080` with a plain `ServeMux` (`go/example/go-process/main.go:112-117`, not wrapped in `NewHTTPHandler`), so it is not instrumented either | recorded; README of `nodejs/example`; no case |
| Integer instruments need `ValueType.INT` (B11c fix round, 2026-11-03, obs M2): the JS API's `valueType` defaults to `ValueType.DOUBLE`, so a counter, up-down counter or gauge created without it exports `1` as `Double(1)` (`Value: 1.000000` in the Collector's debug output; reproduced 2026-11-03 on collector-contrib 0.160.0), while Go's `Int64*` instruments and Python's `int` adds export ints. The examples pass `valueType: ValueType.INT` on every count instrument (`node-custom-signals`, and the 7 counters / up-down counters of node-api, node-backend and node-process). The OTLP → Prometheus translation is the same for both types; a backend that keeps the point type (`asInt` vs `asDouble`) sees the difference | recorded; examples + EXAMPLES.md (B11c); B12 HOW-TO item (custom metrics); no case |
| Log instrumentation scope per language (B11c fix round, 2026-11-03, obs M2): the scope of a helper log record is the `createLogger({ name })` value in Node (`node-custom-signals`, `node-api`), the `logging` logger name in Python (`custom_signals`; `main` / `__main__` in the notification service), and `otel-helper` in Go for every record (`go/logging.go:92`, `otelslog.NewHandler("otel-helper", …)`; `NewLogger`/`NewSlogHandler` take no name). A log query or dashboard on the scope name is per language | recorded; EXAMPLES.md (B11c); no case |

> **Corrected 2026-11-03 (B12).** Three rows above, fix round:
> - **"Worker threads"**: "an `--import` preload only in the main thread" was
>   observed with `eval` workers only. With a **file-based** worker both
>   `--import` and `--require` run again in the worker; only `--require` runs
>   in an `eval` worker (Node 22.15.0, 24, 26; obs M1). Custom preloads guard
>   `setup()` with `isMainThread`.
> - **"Go `CLIENT MAINT_NOTIFICATIONS` false ERROR"**: not every connection —
>   the **first connection of each client**. go-redis's default mode is
>   `auto`; on the error it disables the feature for that client
>   (`maintnotifications/config.go:18-20`, `redis.go:1218-1225`, v9.23.0). Not
>   harmless either: the ERROR `client` span sits in the trace of the request
>   that opened the connection. Fix: `MaintNotificationsConfig:
>   &maintnotifications.Config{Mode: maintnotifications.ModeDisabled}`
>   (go/HOW-TO §12; obs M3).
> - **New difference, process exit (dev H1)**: Go needs a deferred
>   `Shutdown` (nothing flushes at exit otherwise), Python's SDK shuts down
>   from `atexit`, Node registers a `process.once('beforeExit')` flush at the
>   first successful setup (D-JS10 correction). None of the three flushes on
>   an explicit `process.exit()`/`os.Exit` or a fatal signal: Python skips
>   `atexit` on an unhandled SIGTERM, Node skips `beforeExit` on
>   `process.exit()`. Status: recorded; `test/exit.test.cjs`; no case.

