# Tests — @staffops/otel-helper (Node.js)

How to run and extend the Node.js test suite. Audience: contributors changing
`nodejs/packages/`. Everything runs in Docker; no local Node.js is needed.

The suite has five layers:

| Layer | What it proves | Where |
|---|---|---|
| Unit tests | resolution rules, exporters, sampler, `/metrics`, logging, lifecycle, instrumentation, each opt-in package | `nodejs/packages/*/test/*.test.{cjs,mjs}` (`node:test`) |
| Parity fixture | Node.js resolves the shared inputs to the same outputs as Go and Python (ADR-008) | `parity/cases.json`, run by `nodejs/test/parity/runner.test.cjs` |
| Package checks | the published tarballs: `publint`, `attw` (type resolution under `node16`), the pack contents, an install next to the newest `@opentelemetry/api` the peer range admits | `npm run check`, `ci/check-pack.mjs`, `ci/api-range-install.sh` |
| Real services | the opt-in packages against PostgreSQL, MySQL and Redis containers, and the AWS SDK against an in-process fake endpoint | `testing/docker-compose.yml` (`sql-postgres`, `sql-mysql`, `redis-test`), `packages/otel-helper-aws/test/fixtures/aws-fake.cjs` |
| e2e smoke | real exports reach a real Collector over gRPC, HTTP with a path prefix, a per-signal endpoint and TLS | `testing/e2e/` (`node-smoke`) |

CI (`.github/workflows/ci.yml`) splits them across jobs: `node-test` runs the
unit tests, the parity fixture and the package checks on Node 22.15.0 (the
`engines` floor), 22, 24 and 26 with the database services;
`node-api-range` runs `ci/api-range-install.sh` once, on Node 24 (it needs
the npm registry); `e2e-smoke-node` runs the Collector smoke's Node rows on
Node 24 and 22.15.0 (`e2e-smoke` keeps Go and Python); `build-lib-node`
runs the release version tool's tests (`ci/version.test.mjs`), packs the six
tarballs, records their sha256 sums and runs `npm publish --dry-run` on the
versions not yet on npmjs. The
compose `node-tests` service below runs the first two jobs' steps in one
container. Coverage gates (c8, every
line of `dist/` and `esm/`): **≥ 90%** lines for the core and for `-sql` and
`-redis`, **99%** lines, branches, functions and statements for `-aws`,
`-pino` and `-winston`. The core's gate counts the parity runner's child
processes.

## Run everything

From the repository root:

```bash
docker compose -f testing/docker-compose.yml build fakeotlp-node   # once, and after fakeotlp changes
docker compose -f testing/docker-compose.yml run --rm node-tests
```

`node-tests` (`node:${NODE_VERSION:-24}-slim`) starts the fake OTLP receiver
(`fakeotlp-node`), PostgreSQL, MySQL and Redis, copies `nodejs/` out of the
read-only mount (without `node_modules`, `dist`, `coverage`), and runs:

```bash
for p in packages/*; do cmp /src/LICENSE "$p/LICENSE"; done   # every package ships the root LICENSE
npm ci --ignore-scripts
npm ls --all            # the installed tree is valid
npm run lint            # eslint
npm run build           # tsc → dist/ (CommonJS) + the esm/ wrappers
npm run typecheck       # including the type tests in packages/otel-helper/test/types
npm run check           # publint --strict + attw per package
node ci/check-pack.mjs  # every exports target, LICENSE, README.md and package.json in each tarball; no src/, test/ or *.map
npm test                # every package, with its coverage gate
sh ci/api-range-install.sh
```

`testing/verify.sh --only node` runs the same, rebuilding the receiver image
first, plus `node --check` on the e2e smoke program.

Stop the services afterwards (they keep no data: tmpfs only):

```bash
docker compose -f testing/docker-compose.yml stop fakeotlp-node sql-postgres sql-mysql redis-test
```

### Other Node.js versions

```bash
NODE_VERSION=22.15.0 docker compose -f testing/docker-compose.yml run --rm node-tests
NODE_VERSION=26      docker compose -f testing/docker-compose.yml run --rm node-tests
```

### Variables

| Variable | Default (compose) | Effect |
|---|---|---|
| `NODE_VERSION` | `24` | The `node:<version>-slim` image |
| `NODE_TESTS_OFFLINE` | _(unset)_ | `1` skips `ci/api-range-install.sh` (it needs the npm registry). CI always runs it |
| `SQL_TEST_POSTGRES`, `SQL_TEST_MYSQL` | the compose databases | Connection URLs of the `-sql` database tests (throwaway credentials) |
| `SQL_TEST_REQUIRE` | `1` | A missing database URL fails the suite instead of skipping |
| `REDIS_TEST_URL`, `REDIS_TEST_REQUIRE` | the compose Redis, `1` | The same for `-redis` |

### Run a subset

```bash
docker compose -f testing/docker-compose.yml run --rm node-tests sh -ec '
  mkdir -p /work && tar -C /src/nodejs --exclude=node_modules --exclude=dist -cf - . | tar -C /work -xf -
  cd /work && npm ci --ignore-scripts && npm run build
  cd packages/otel-helper && node --test --test-name-pattern "shutdown" test/setup.test.cjs'
```

## Parity fixture

`test/parity/runner.test.cjs` (run by the core package's `npm test`) runs one
test per case in `parity/cases.json`, each in a fresh process
(`test/parity/driver.cjs`) so global OpenTelemetry state never leaks between
cases. Status `pass` must pass; `xfail:<task>` must fail on a behavior
mismatch; `skip:<reason>` is skipped. Node.js today: every case `pass` except
three upstream gaps recorded in design D-JS14 —
`skip:no-upstream-rpc-metrics` (2, no gRPC RPC metrics, as Python) and
`skip:no-upstream-resource-proto-key` (1). Never delete a case to make the
suite pass. The `export` cases need the fake receiver (`PARITY_REQUIRE_FAKEOTLP=1`
fails them instead of skipping); the Node.js legs reach it by DNS name
(`fakeotlp`, `fakeotlp-tls`) because Node 26 refuses an IP literal as the TLS
server name — see [testing/README.md](https://github.com/StaffOps/otel-libs/blob/main/testing/README.md).
Format and adapter rules: [parity/README.md](https://github.com/StaffOps/otel-libs/blob/main/parity/README.md).

## e2e smoke against a real Collector

```bash
testing/e2e/smoke.sh
```

The script starts `otel/opentelemetry-collector-contrib` and runs the Go,
Python and Node.js smoke programs once per combination in
`testing/e2e/combos.sh`. The Node.js program (`testing/e2e/node-smoke/smoke.cjs`,
run by `run-node.sh` in the `node-smoke` service) emits the shared markers
(`e2e-smoke-span`, `e2e.smoke.runs`, `e2e-smoke-log`); 16 expectation rows per
language check each signal reached the expected receiver, no per-signal leak,
and that the log record carries the marker span's trace id. Output: one `OK` /
`MISS` / `LEAK` / `XFAIL` / `XPASS` line per expectation. One language on
another Node line, as the CI floor leg:

```bash
SMOKE_LANGS=node NODE_VERSION=22.15.0 testing/e2e/smoke.sh
```

The example apps run against the same Collector: see
[example/README.md](example/README.md).

## Test modules

### Core (`packages/otel-helper/test`)

| Module | Covers |
|---|---|
| `resolve.test.cjs` | resolver building blocks: trim set, booleans, `OTEL_RESOURCE_ATTRIBUTES`, log levels, code-option validation |
| `setup.test.cjs` | identity resolution, deprecations, diagnostics and the DiagLogger (forwarded SDK text: response bodies dropped, OTLP header values masked, end to end against a server echoing the headers in a 400/500 body); the lifecycle — rollback at every install step, foreign globals, `replaceGlobals`, re-setup, the shutdown deadline |
| `otlp.test.cjs` | endpoints, protocol, TLS settings, IPv6, host rules |
| `exporters.test.cjs` | exporter construction: env masking, headers, timeout and export deadline, compression, BSP/BLRP and limits, gRPC credentials, TLS files |
| `signals.test.cjs` | `OTEL_{TRACES,METRICS,LOGS}_EXPORTER`, console exporters, `OTEL_SDK_DISABLED`, `OTEL_CONFIG_FILE`, the metric interval |
| `tracing.test.cjs` | sampler, health sampler, propagators, debug marker |
| `instrumentation.test.cjs` | http, undici, express and gRPC instrumentation, health exclusion, the gRPC status rule, the loaded-before-setup report |
| `metrics.test.cjs` | the `/metrics` listener, `metricsHandler()` status table, unit suffixes, busy port |
| `exit.test.cjs` | the `beforeExit` flush of a process that ends on its own: exported once, no loop, no repeat after an app `shutdown()`, no stacking on re-setup, none after a failed setup or in a worker thread, removed by `reset()`, a failure reported |
| `logging.test.cjs` | `createLogger()`: OTel path vs stdout, levels, attribute conversion, the bridges' flushes |
| `forwarding.test.cjs` | the forwarding providers and propagator across re-setups |
| `cluster.test.cjs` | `cluster` workers: instance ids, the explicit-id warning, per-worker ports, re-fork on a slot, the flush of a worker the primary disconnects. Each run has a 20 s deadline that kills every worker and fails |
| `hooks.test.cjs` | the `/hooks` and `/register` entries, worker threads, the ESM-without-hooks warning |
| `opt-in.test.cjs` | `setup({ instrumentations })`, name conflicts, the `OTEL_SEMCONV_STABILITY_OPT_IN` HTTP warning |
| `api.test.cjs` | `getTracer`, `getMeter`, `startRootSpan` |
| `db.test.cjs`, `contrib.test.cjs` | the internal entries the opt-in packages share |
| `dual-load.test.cjs`, `esm-wrapper.test.mjs`, `state.test.cjs`, `version.test.cjs`, `diagnostics.test.cjs` | one implementation and one state for `require` and `import`, the ESM wrappers, the shared state, the version, the diagnostics sink and redaction |
| `types/` | type-level checks (`tsc`) of the public API |

### Opt-in packages

| Package | Modules |
|---|---|
| `-sql` | `sql.test.cjs` (no database: options, detection, statements, errors), `db.test.cjs` (real PostgreSQL and MySQL), `package.test.cjs`, `esm.test.mjs` |
| `-redis` | `redis.test.cjs` (RESP fake), `server.test.cjs` (real Redis; also one span per command issued before ioredis is `ready`), `package.test.cjs`, `esm.test.mjs` |
| `-aws` | `aws.test.cjs` (no SDK), `sdk.test.cjs` (real `@aws-sdk/client-*` against the fake endpoint, the secrets sweep), `package.test.cjs`, `esm.test.mjs` |
| `-pino` | `pino.test.cjs` (real pino; OTLP records decoded from a local receiver), `package.test.cjs` (package contract), `esm.test.mjs` |
| `-winston` | `winston.test.cjs` (real winston; the same receiver), `package.test.cjs`, `esm.test.mjs` |

## Writing tests

- Tests ship with the change and keep each package at or above its gate.
- Behavior that depends on process globals (the API registry, signal
  handlers, `cluster`, preloads) runs in a child process; in-process tests
  call `@staffops/otel-helper/testing` `reset()` between cases.
- Assert on public behavior — resolved values, emitted attributes, the
  diagnostics sink — not on private SDK fields. Where the helper must read a
  private field (the G1 rollback), the test names it, so an API bump fails
  loudly.
- Wire behavior (endpoints, TLS, compression, timeouts) belongs in a parity
  `export` case, checked by the fake receiver.
- Never print a secret in a test's output; the AWS suite sweeps every
  attribute, event, status, diagnostic line and `/metrics` line for the fake
  credentials.
