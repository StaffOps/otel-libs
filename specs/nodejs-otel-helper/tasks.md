# Node.js OTel helper — tasks

**Status**: Draft r3 (2026-10-29). Spec: [requirements](requirements.md) ·
[design](design.md) · [research](research.md). Each task: implement + tests in
Docker, flip its parity cases (`skip:B<n>-…` → `pass`, or a D-JS14 status),
refutation (`observability` + `dev`), `code-review`, CHANGELOG, commit.

| Task | Depends on | Covers | Status |
|---|---|---|---|
| B0 Adversarial review r1 | — | format rule | ✅ 2026-10-29 (r2 written) |
| B0b Re-review r2 (observability 1 HIGH, dev 2 HIGH) | B0 | format rule | ✅ 2026-10-29 (r3 written) |
| B0c Re-review r3 (dev: none; observability: 1 HIGH, fixed in r3.1) | B0b | format rule | ✅ 2026-10-29 |
| B0d Confirm r3.1 (observability) | B0c | format rule | ✅ 2026-10-29 (no BLOCKING/HIGH) |
| B1a Workspaces, tsc CJS + ESM wrapper, eslint, publint/attw, node:test, c8 gate, `.gitignore` (node_modules, coverage, *.tgz), dual-load shared-state test | B0d | JS1, JS5, D-JS10, D-JS11 | ✅ 2026-10-29 (dev refutation + code-review approved after fixes) |
| B1b Compose `fakeotlp-node`/`node-tests`, CI matrix 22.15.0/22/24/26, `verify.sh` step, api-range install test | B1a | JS1, D-JS12 | ✅ 2026-10-29 (dev refutation + code-review approved after fixes) |
| B1c Spike gate: hooks on the four Node versions, duplicate-SDK check, context manager wiring, explicit gRPC credentials (no lazy env TLS read) | B1a | D-JS1, D-JS11 | ✅ 2026-10-29 (dev refutation + code-review approved after fixes) |
| B2 Parity runner + driver (all kinds wired), per-case `skip:B<n>` statuses, TLS receiver by DNS name for Node 26 (D-JS14 F5), `trace_matrix.py` JS support, `parity/README.md` Node notes | B1b, B1c | D-JS12 | ✅ 2026-10-29 (dev refutation + code-review approved after fixes) |
| B3 Resolver: identity, environment, resource, deprecations, diagnostics sink + DiagLogger | B2 | R4, R5, JS3, JS4, D-JS3–5 | ✅ 2026-10-29 (observability + dev refutation + code-review approved after fixes) |
| B4a Endpoints, protocol, TLS warnings, IPv6/zone ids, gRPC :80 (new fixture case `export.grpc_http_explicit_port_80`, run by the Go and Python suites in the same change); B3 review LOWs: `globals.ts` treats a registry whose `version` is not `1.10.x` as foreign (reported), `envBool` echoes via `truncateValue` | B3 | R1, R2, D-JS3 | ✅ 2026-10-29 (12 cases flipped, fixture case added with Node `skip:B4b-otlp-endpoint`; `envBool` already truncated since B3; dev refutation + code-review approved after fixes; Go/Python follow-ups in A17) |
| B4b Exporter construction (3.0 processors take `{exporter}` options — spike F7; gRPC exporters loaded only when a gRPC signal is configured, D-JS1 amended), env masking, headers, timeout + effective deadline, compression, BSP/BLRP/limits, gRPC credential errors (new fixture cases: unreadable cert, key without cert, bad PEM — run by Go/Python too) | B4a | R2, R8, JS2 | ✅ 2026-10-29 (observability + dev refutation and code-review approved after fixes; 84 cases flipped to `pass`; new cases `export.grpc_certificate_unreadable_falls_back`, `export.grpc_certificate_not_pem_falls_back` pass in Go/Python/Node; key-without-cert case not added — Go ignores the key silently, Python's grpc aborts the process: reported, needs a ruling; BSP/BLRP timeout precedence left open). Fix round 2026-10-29: refuters + code-review no BLOCKING; their rulings became contract task A18 (TLS files, export deadline, code timeout bound, header bytes, value-length limits), implemented in Go, Python and Node in the same change; Node quality items (lazy gRPC exporters, per-signal pipeline in `tryBuild`, `item #N` positions, warnings after the mask, null-prototype HTTP headers, shutdown error naming providers) |
| B4c Exporter lists, console, `OTEL_SDK_DISABLED`, `OTEL_CONFIG_FILE`, interval | B4b | R8, JS2 | ✅ 2026-10-30 (observability + dev refutation and code-review approved after fixes; open items became contract A19) |
| B5 Sampler (ParentBased, grammar, health wrapper outside it), propagators — installed also under `OTEL_SDK_DISABLED=true` (case `propagation.sdk_disabled_propagators_kept`; added 2026-10-29) — debug marker (`debug="true"` on no/remote parent) | B3 | R6, R7, R9 | ✅ 2026-11-02 (observability + dev refutation and code-review approved after fixes) |
| B7 `/metrics`: helper listener on `::`, mounted handler status table, unit-suffix adapter (+ fixture assertions on the HTTP histogram name and a counter with a unit, run by the Go and Python suites in the same change), Prometheus fallback; remove the B4c pending INFO report "prometheus metric exporter not implemented yet in Node" and its test filters (`test/otlp.test.cjs`, `test/setup.test.cjs`, `test/signals.test.cjs`; added 2026-10-29) | B4c | D-JS8, ADR-007 | ✅ 2026-11-02 (observability + dev refutation and code-review approved after fixes) |
| B6 HTTP/undici/express/gRPC instrumentation, health (HTTP + gRPC by name), client exclusions incl. OTLP endpoints, `http_server`/`grpc_server` cases; assert the span-name format `grpc.<service>/<method>` the health sampler matches and `url.path` at span start (B5 fix round, 2026-10-30) | B5, B7 | R7, R10, D-JS7 | ✅ 2026-11-02 (fix round, final review and dev re-probe applied; full compose suites green on Node 24/22.15.0/26 with export cases, Go, Python 3.13 latest / 3.10 lower) |
| B8 Logging: helper logger, stdout fallback, console exporter, R4 levels | B4c | JS6, R4 | ✅ 2026-11-02 (observability + dev refutation, dev re-probe and code-review applied; full compose suites green: Go, Python 3.13 latest / 3.10 lower, Node 24/22.15.0/26; `logging.console_when_no_exporter`, `logging.console_when_logs_exporter_none` and A23's `logging.stdout_time_utc_under_tz` pass on Node 22.15.0/24/26; the Go/Python divergences it found are ruled as contract task A23; OTel attribute values ruled the same day: objects as maps, arrays as arrays; then observability + dev refutation and code-review) |
| B9 Lifecycle: sync setup, rollback (G1), foreign globals, shutdown deadline, re-setup re-binding, `/testing` reset, register entry; `replaceGlobals` covers the propagator; re-setup releases only the helper's own providers, as B5 does for the propagator (`registeredGlobal(type) === active.<instance>`) (B5 fix round, 2026-10-30) | B6–B8 | JS5, JS7, D-JS10 | ✅ 2026-11-02 (re-reviews of the fix round clean, final LOW round applied, node-tests green on 24/22.15.0/26; implemented + tests: forwarding providers and propagator, G1 rollback at every install step, foreign globals → `Resolved.inactiveSignals`, `replaceGlobals`, `/register` calls `setup()`, ESM-without-hooks warning; refutation (`observability` + `dev`) and `code-review` done 2026-11-02: no BLOCKING, one HIGH (H1, callbacks unbound before the final collection) — fix round applied the same day with the owner's rulings (D-JS10, D-JS14 rows of 2026-11-02); full compose suites green on Node 24/22.15.0/26 after the fix round; fix-round re-review pending; no `skip:B9-*` parity case existed to flip; the ESM preload + `-sql` pg-span test moved to B10a; design D-JS10 "Implemented 2026-11-02", D-JS14 rows of 2026-11-02) |
| B9b `cluster` fork kind + multi-worker docs | B9 | JS9, D-JS13 | ☐ |
| B10a `-sql` (pg + mysql2, postgres/mysql services, pool metrics decision) + semconv dup; the D-JS11 test moved from B9: an ESM app with a custom preload (`import '@staffops/otel-helper/hooks'` + `setup({ instrumentations })`) and `-sql` asserts a pg span (B9, 2026-11-02) | B9 | JS8, D-JS9 | ☐ |
| B10b `-redis` (redis + ioredis, RESP fake) | B9 | JS8 | ☐ |
| B10c `-aws` (AWS SDK v3 against a local endpoint) | B9 | JS8 | ☐ |
| B10d `-pino` destination, `-winston` transport | B8 | JS6, D-JS6 | ☐ |
| B11a e2e `node-smoke` (+ 13 expectation rows, markers, CI job name) | B10a–d | acceptance | ☐ |
| B11b Examples api → backend (gRPC, notification.proto, proto-loader, health) → process; Dockerfile + Dockerfile.demo (npm pack tarballs) | B11a | acceptance | ☐ |
| B11c custom-signals (same names/shape as Go/Python) + EXAMPLES.md | B11b | acceptance | ☐ |
| B12 Docs: nodejs README/HOW-TO/TESTS; MkDocs nav + `docs/site/nodejs/` symlinks + `docs.yml` paths + `site_description`; root README (matrix, quick start, env table heading, opt-in, gaps, installing); AGENTS.md; CONSUMING (npmjs + GitHub Packages `.npmrc`); release + parity-case skills; `verify.sh` steps; dashboards: none needed (they read .NET example metrics); HOW-TO: with services in several languages use `parentbased_traceidratio`, not bare `traceidratio` (contract D1 row "Bare `traceidratio` across languages", A20); HOW-TO: the ref'd `shutdown()` deadline can keep a process with a hung provider alive up to `timeoutMs` (default 30 s); HOW-TO: await `shutdown()` before calling `setup()` again (a re-setup in the same tick can find the `/metrics` port still bound: EADDRINUSE on the sink; B7, 2026-11-02; and it moves the observable and batch callbacks to the new provider, so the old provider's final export loses them — B9 final round, 2026-11-02); HOW-TO: `node:http2` / h2c servers and clients are not instrumented (no official instrumentation; D-JS14, B6 fix round, 2026-11-02); HOW-TO: require routes modules (express routers, gRPC services and clients) after `setup()` — a router built before it gets a truncated `http.route` or none (D-JS14, B6 dev re-probe L1, 2026-11-02); HOW-TO troubleshooting: an OTLP endpoint answering `Retry-After: 0` makes the upstream retry print Node's `TimeoutNegativeWarning` (B6 fix round, 2026-11-02); HOW-TO lifecycle (B9, 2026-11-02): the SIGTERM pattern — the helper installs no signal handler (JS7): `process.once('SIGTERM', () => { shutdown().finally(() => process.exit(0)); })`; `replaceGlobals: true` replaces a TracerProvider/MeterProvider/LoggerProvider/propagator registered before `setup()` (never the context manager), and tracers the app took from the replaced provider stay on it; `Resolved.inactiveSignals` lists the signals a foreign global kept from the helper; a failed `setup()` throws after releasing everything (catch it to run without telemetry), `/register` only reports it; observable callbacks registered through the global meter follow every re-setup until removed; `setup()` in an ES module entry without `--import @staffops/otel-helper/register` (or the hooks entry) warns, and a `.js` entry run as ESM only by syntax detection is not recognized (D-JS14); the shutdown deadline bounds the promise, not process exit — keep `process.exit()` in `.finally` (B9 fix round); under `replaceGlobals`, tracers obtained earlier from the foreign provider stay on it; after `shutdown()` the app cannot register its own global providers without `trace.disable()` etc. first (Go can); worker threads: `--require @staffops/otel-helper/register` reaches every worker (hooks only, silently), `--import` only the main thread; a worker's own `setup()` starts no `/metrics` listener (D-JS14, B9 fix round M2); HOW-TO logging: `OTEL_LOGS_EXPORTER=console` is a debugging format (the SDK's multi-line `console.dir` dump, not line-parseable) — the stdout JSON fallback (no exporter, or `OTEL_LOGS_EXPORTER=none`) is the ingestible form (contract design D6, A23 fix round, 2026-11-02) | B11c | acceptance | ☐ |
| B13 Release: CI `node-test` job, `nodejs/v*` tag trigger, `build-lib`/publish `needs`, npm pack, example images, `publish-nodejs-release` (id-token, npmjs + GitHub Packages); exporter-env re-check checklist per SDK bump; SDK bump: `Span.status` writable in `onEnding` (`GrpcServerStatusProcessor`, test "A22 gRPC server status through a real TracerProvider"), InstrumentationBase still enabling in its constructor (B6 final review, 2026-11-02); API bump: the G1 rollback reads `logs._proxyLoggerProvider._setDelegate` (private) and the trace proxy's `setDelegate` (`setup.ts` `releaseGlobal`; test "M3" in `test/setup.test.cjs`) — re-check both, else it falls back to `disable()` (B9 final round, 2026-11-02) | B12 + owner claimed `staffops` npm org | JS10 | ☐ |
| B14 Final validation (refutation + code-review on the whole Node package), Node `0.3.0` rc | B13 | acceptance | ☐ |

**Test-only dependencies approved by the owner (2026-10-29)**: devDependencies
`express`, `@grpc/grpc-js`, `@grpc/proto-loader`, `grpc-health-check` (B6);
`pg`, `mysql2`, `redis`, `ioredis`, `@aws-sdk/client-sqs` (B10); `pino`,
`winston` (B10d); Docker test services `postgres`, `mysql`. None ship in the
published packages.

**Carried from B6 into B9 (2026-11-02)**: the instrumentations live on the
shared runtime (`Runtime.instrumentations`, built once per process by the
first setup that instruments); every setup calls `setTracerProvider` /
`setMeterProvider` with its own providers and enables them, `shutdown()`
disables them, `/testing` `reset()` disables them but keeps the instances
(their require/import hooks stay registered). B9 should: bind the stable
forwarding providers of D-JS10 instead (then a re-setup needs no re-bind);
decide whether `replaceGlobals` re-binds instrumentations bound to a foreign
provider (they hold the API proxy, whose tracer keeps the first delegate it
resolved); append the `setup({ instrumentations })` extensions of B10 to the
same holder (enable, bind, disable together); cover a rollback (G1) after the
instrumentations were enabled; the warning for an **ESM process without the
loader hooks** (JS5: `import express from 'express'` with no
`--import @staffops/otel-helper/register` is never patched by
require-in-the-middle, and the B6 re-require only reaches `require.cache`) —
name it explicitly (B6 fix round, 2026-11-02); once `/register` calls
`setup()`, revisit the "loaded before setup()" report text
(`instrumentation.ts` `enableInstrumentations`) so it can name `/register`
as the cure again (B6 dev re-probe M1, 2026-11-02).

**Carried from B8 (2026-11-02)**: B9 — the helper's loggers read
`logDestination()` (`setup.ts`) per record, which uses `Runtime.active` only
once a setup completed; a rollback (G1) must keep it that way, and
`replaceGlobals` does not change the loggers (they never use the global
LoggerProvider). B10d — `-pino`/`-winston` should apply the same rule through
`emitRecord()` (`src/internal/logging.ts`) rather than a copy of it;
`emitRecord` stays internal for now (owner ruling 2026-11-02): B10d decides
how the two packages reach it — a package-internal subpath export of the core
or another mechanism — and records the choice in D-JS6. The stdout rulings
are contract task A23 (D1 rows "Stdout fallback time zone", "Stdout fallback
fields", "OTel log scope name", ruled 2026-11-02).

**Checklist carried from the B3 review (2026-10-29)**:
- B9: stable forwarding providers (design D-JS10, decided 2026-10-29); flip the
  `todo` test "re-setup keeps tracers obtained earlier working" in
  `nodejs/packages/otel-helper/test/setup.test.cjs` to a normal test.
- B10a–c: once `setup({ instrumentations })` exists, reword the
  `OTEL_HELPER_EXTRA_INSTRUMENTATION` warning (`src/internal/resolve.ts`) to
  name `sqlInstrumentation()` / `redisInstrumentation()` / `awsInstrumentation()`
  and the option, and update its unit test.

**Owner actions**: claim the `staffops` org on npmjs before B13 (ADR-011).
