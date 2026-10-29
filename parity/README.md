# Parity fixture

`cases.json` is the shared, language-neutral contract for the helper libraries
([ADR-008](../specs/decisions/ADR-008-shared-parity-fixture.md),
[design D2](../specs/config-contract-v0.3/design.md)). Every language's test
suite runs every case. **`cases.json` is hand-maintained**: edit it directly
(there is no generator). **Never delete a case to make CI green** — mark it
`xfail:<task>`.

## Case format

```json
{
  "id": "export.http_path_prefix",
  "kind": "export",
  "requirement": "R1",
  "note": "optional free text",
  "env": {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://{receiver}:4318/otlp"},
  "code": {},
  "input": {},
  "expect": {"received": {"traces": {"port": 4318, "path": "/otlp/v1/traces"}}},
  "status": {"go": "pass", "python": "xfail:A3", "dotnet": "xfail:phase-c", "nodejs": "skip:phase-b"}
}
```

| Field | Meaning |
|---|---|
| `env` | Environment for the case. The runner first removes every `OTEL_*`, `SERVICE_NAME` and `ENVIRONMENT` variable and sets `OTEL_HELPER_METRICS_PORT=0` (cases may override it). |
| `code` | Options set in code, snake_case: `service_name`, `environment`, `otel_endpoint`, `otlp_protocol`, `metric_exporters`, `sample_ratio`, `debug`, `export_interval_ms`, `resource_attributes`. A key an adapter can't map is an adapter error (hard failure), so a case for an API that doesn't exist yet is `skip:<task>-…` until that task adds the API and the mapping. |
| `input` | Kind-specific parameters (schema below). |
| Placeholders | `{receiver}` (plain + TLS ports), `{receiver_tls}` (TLS on the standard port 4317), `{receiver_ipv6}`, `{receiver_ca}`. |

### Kinds

| Kind | `input` | Does | Snapshot fields (`expect` keys) |
|---|---|---|---|
| `resolve` | — | Setup, read what it installed | `resource` (all attributes of the installed resource), `metric_exporters`, `metric_export_interval_ms`, `log_level` |
| `export` | — | Setup, emit one span + counter + log, shut down, read the receiver | `received{traces,metrics,logs}` → records `{proto, tls, port, path, encoding, headers, timeout_ms}` (`timeout_ms` from the gRPC `grpc-timeout` header) |
| `sampler` | `scenario`: `root`, `remote_sampled`, `remote_unsampled`, `remote_sampled_debug_tracestate`, `remote_sampled_debug_baggage`, `local_child_of_root`, `local_child_of_remote_unsampled` | Start one span | `sampled`, `attributes` |
| `propagation` | — | Inject inside a span with baggage `parity=1` | `headers` (sorted names) |
| `http_server` | `requests`, `traceparent`, `child_span` | Serve the requests through the helper's HTTP server instrumentation; optionally the handler starts `handler-child` | `server_spans`, `handler_child_spans`, `requests_counted` (HTTP server duration `_count` from the helper's `/metrics`) |
| `grpc_server` | `method` | One call on an instrumented server | `server_spans`, `rpcs_counted`, `call_ok` |
| `logging` | — | Emit one WARN record through the helper's logger | (common fields) |
| `metrics_scrape` | — | Increment `parity.requests`, scrape the helper's `/metrics` | `scrape`, `scrape_status` (HTTP status of the mounted handler: 503 before/after/failed setup, 200 when Prometheus is deliberately inactive — design "Mounted /metrics handler status") |
| `fork` | — | Python: import the helper, `os.fork()` twice, set up in each child (gunicorn `post_fork` model). Go: `skip:no-prefork-model` | `instance_ids`, `instance_ids_distinct` |

Common fields, every kind: `error` (present only when setup failed),
`diagnostics` (structured capture: Go — otel error handler, otel internal
logger, `slog`/`log` default; Python — every WARNING+ record and
`warnings.warn`), `helper_log` (the **helper's own** diagnostics only — Go:
`log`/`slog` records whose call site is a non-test file of the helper package;
Python: records from the `staffops_otel_helper` logger hierarchy; Node:
diagnostics-sink records with origin `helper`; helper diagnostics must use those
channels), `console` (process stdout + stderr) and `log`
(diagnostics + console). Redaction is asserted on `helper_log`: the SDKs echo
raw values in their own messages (Go `parse headers input`, Python's invalid
header warning), which R8 scopes out. Fixture errors fail the case whatever its status (both runners, plus a
well-formedness test in each suite): unknown or missing top-level keys
(allowed: `id kind requirement note env code input expect status`; required:
`id kind env code expect status`), unknown kind, an `expect`/`input` key
outside the kind's schema or with the wrong type, an object mixing matcher ops
with other keys (`{"regex": …, "contians": …}`), a non-compiling regex, an
invalid status or `python@sdk>=X.Y` override.

The fake receiver records only request headers whose name starts with
`x-parity` (plus `grpc-encoding` / `content-encoding` and `grpc-timeout`):
cases and probes that assert `headers` must use that prefix.

Protocol, URL and TLS are asserted only by `export` cases (per signal, from
what reached the wire), never from resolved options.

### `expect`

| Form | Meaning |
|---|---|
| `"field": value` | exact (numbers compare as floats, lists in order) |
| `{"prefix": "…"}` / `{"regex": "…"}` | prefix / regex search (Go `regexp`, Python `re.search`, JS `RegExp` with the `u` flag). Dialect: the common subset of RE2, Python `re` and JS — no inline flags (`(?i)`), lookaround or backreferences |
| `{"absent": true}` | field not present |
| `{"not_contains": "…"}` | **extension**: substring must not appear (RE2 has no lookahead) |
| `{"all": [m1, m2]}` | **extension**: every matcher must hold |
| `{"lte": n}` / `{"gte": n}` | **extension**: numeric bounds |
| nested object | matched key by key (partial) |
| `"error": "substr"` | setup fails with an error containing it; without an `error` key any error fails the case |
| `"warning": "substr"` | `log` contains it |
| `"warning_count": {"substr": n}` | **extension**: exactly `n` `diagnostics` lines contain it ("warned once") |
| `"received": {"<signal>": {...}}` | ≥ 1 record for the signal and **every** record matches; `{"absent": true}` = none |

Wire records are classified by signal from the gRPC service name or the HTTP
path. Only records sent to the receiver's addresses count. Records are not
filtered by port on purpose: a case expecting 443 must see the record that
went to 4317 to fail for the right reason.

### `status`

`pass` | `xfail:<task-or-phase>` | `skip:<reason>`. Node.js statuses waiting
for a Phase B task are `skip:B<n>-<reason>`, `B<n>` being the task in
`specs/nodejs-otel-helper/tasks.md` that flips them; the Node suite fails on a
skip that names no task and is not a recorded D-JS14 difference. **Strict**: an `xfail`
case that passes fails the build; only a behavior mismatch satisfies an
`xfail` — fixture and adapter errors never do. `python@sdk>=X.Y` overrides
`python` when the installed `opentelemetry-sdk` is at least X.Y (the highest
matching key wins).

**Release gate:** `skip:A…` (a case waiting for a task's API) must not survive a
release. `PARITY_RELEASE=1` enables `TestParityNoTaskSkipsAtRelease` (Go) and
`test_no_task_skips_at_release` (Python), which fail while any remain; the
release task (A11) runs them. For Node the same variable enables
`release gate: no nodejs case still skipped pending a task`, which fails while
any `nodejs` status starts with `skip:B` (B14).

## Running

```bash
docker compose -f testing/docker-compose.yml run --rm go-tests
docker compose -f testing/docker-compose.yml run --rm python-tests
PY_VERSION=3.10 PY_DEPS=lower docker compose -f testing/docker-compose.yml run --rm python-tests
NODE_VERSION=26 docker compose -f testing/docker-compose.yml run --rm node-tests

# one case
docker compose -f testing/docker-compose.yml run --rm go-tests go test -run 'TestParity/export.grpc_plaintext' -v .
docker compose -f testing/docker-compose.yml run --rm python-tests sh -c \
  "cp -r /src/python /work && cd /work && pip install -q -e '.[dev,all]' && pytest tests/test_parity.py -k export.grpc_plaintext"
docker compose -f testing/docker-compose.yml run --rm node-tests sh -ec \
  "mkdir -p /work && tar -C /src/nodejs --exclude=node_modules --exclude=dist -cf - . | tar -C /work -xf - && cd /work \
   && npm ci --ignore-scripts && npm run build && node --test --test-name-pattern='export.grpc_plaintext' 'test/parity/*.test.cjs'"
```

Without `FAKEOTLP_HOST`, cases that need the receiver skip locally. With
`PARITY_REQUIRE_FAKEOTLP` set (compose, CI) they fail instead, including the
IPv6 and `{receiver_tls}` cases when those addresses are missing.

Each case runs in its own process (Go re-executes the test binary, Python runs
`tests/parity_driver.py`, Node runs `nodejs/test/parity/driver.cjs`): OTel and SDK state is set-once per process — Go's
`resource.Default()` caches `OTEL_RESOURCE_ATTRIBUTES` on first use. Coverage
from those processes is merged into each suite's gate (Go: `PARITY_GOCOVERDIR`
+ `go tool covdata`; Python: `PARITY_COVERAGE_DATA`, combined by pytest-cov;
Node: children inherit `NODE_V8_COVERAGE` and exit with `process.exit()`, and
the runner runs inside the core package's `npm test`, so c8 merges them).

## Node.js runner

`nodejs/test/parity/` (design D-JS12): `runner.test.cjs` is the `node:test`
suite, `harness.cjs` the schema, placeholders, child execution and matcher (a
port of `python/tests/test_parity.py`, same rules as Go), `driver.cjs` the
per-case child. `npm run test:parity` (from `nodejs/`) runs it alone; quote
the glob (`node --test "test/parity/*.test.cjs"`): `node --test test/parity/`
fails on Node 22/24.

- **Execution.** Each case is one `node:test` test (timeout 150 s). Non-export
  cases run in a `describe` with concurrency `min(8, 2 × CPUs)`, export cases
  in a second one with concurrency 1. A case is validated before its child is
  spawned. Children are spawned asynchronously; the parent kills a child after
  120 s, and the driver gives export cases 75 s (others 15 s) to shut down
  before it exits. `--test-name-pattern` selects cases; only selected cases
  spawn a child. A harness exception is reported as a crash of that case.
- **Shared receiver.** The runner runs inside the core package's `npm test`,
  next to the package's other test files, which `node --test` runs in
  parallel processes. Rule: no other test file may export to the receiver
  addresses (127.0.0.2, 127.0.0.3, `[::1]:24318`, or the names below). A test
  that needs the receiver goes into the parity runner, or into its own
  `node --test` invocation under the same c8 run.
- **Environment.** As Go/Python: `OTEL_*`, `SERVICE_NAME` and `ENVIRONMENT`
  removed, `OTEL_HELPER_METRICS_PORT=0`, then the case env. `PARITY_CASES`
  overrides the fixture path (compose: `/src/parity/cases.json`).
- **Receiver by DNS name.** Node 26 refuses an IP literal as TLS ServerName
  (grpc-js sets it from the target host; Node 22/24 print DEP0123), so the Node
  legs set `FAKEOTLP_HOST=fakeotlp` and `FAKEOTLP_HOST_TLS=fakeotlp-tls`,
  mapped to 127.0.0.2 / 127.0.0.3 (`extra_hosts` on `fakeotlp-node` in compose,
  `/etc/hosts` in CI) and present in the receiver certificate's SANs. Go and
  Python keep the IP literals. A runner test checks the TLS handshake by name
  on every leg. The IP-literal gRPC TLS path is a recorded Node difference
  (D-JS14).
- **Statuses.** `nodejs` only (no per-version override). A `skip:B<n>-…` must
  name a task row of `specs/nodejs-otel-helper/tasks.md`. `adapter_pending`
  (the helper API a kind needs does not exist yet) fails like an adapter error
  unless the case is skipped.
- **`helper_log`, `diagnostics`, `console`.** The driver replaces the
  helper's diagnostics sink with `setDiagnosticSink()` from
  `@staffops/otel-helper/testing`. The sink receives
  `(line, {origin, level})` (design D-JS5) and the driver still writes every
  line to stderr.
  - `helper_log`: records with origin `helper` (the helper's own
    diagnostics, any level). SDK `diag` messages the helper's DiagLogger
    forwards (origin `diag`) are excluded: redaction is asserted on this field.
  - `diagnostics`: records at `warn` or `error` level of any origin, plus Node
    process warnings (`process.on('warning')`, e.g. DEP0123 from a
    dependency) — the Node counterpart of Python's "every WARNING+ record and
    `warnings.warn`". `warning_count` counts its lines.
  - `console`: the child's stdout + stderr.
- **Adapter seams.** The resource comes from `getResource()` of `/testing`
  (the installed TracerProvider's resource; the 3.0 provider has no public
  getter; B3 adds it). No probe span is ever started for it. `metric_exporters`,
  interval and `log_level` come from the frozen `Resolved` that `setup()`
  returns. Span capture (`http_server`, `grpc_server`) passes an in-memory
  `SimpleSpanProcessor` through the `setup()` option `spanProcessors`
  (test/advanced, not a fixture `code` key; SDK 3.0 accepts processors only at
  construction). The `/metrics` scrape calls `metricsHandler()` with in-memory
  request/response objects (the response surface is listed in design D-JS8),
  so no instrumented server sees it. `http_server` serves an `express` app;
  its requests are raw HTTP/1.1 over `node:net` (an instrumented client would
  inject its own `traceparent`). `http`, `express` and `@grpc/grpc-js` are
  required after `setup()`. Export logs go through the `@opentelemetry/api`
  logs API.
- **`fork`.** The driver runs `cluster.fork()` twice; each worker is a fresh
  process that sets up and reports over IPC. Unlike Python's `os.fork()`, no
  state is inherited, so `instance_ids_distinct` is close to trivially true
  under `cluster`; the cases still check that each worker generates (or keeps
  from the env) its id and warns per worker.

### Text matchers apply to strings only

In the Node runner `prefix`, `regex` and `not_contains` on a present
non-string value are a mismatch (a number, list or object is never
stringified — Go's `fmt.Sprint`, Python's `str()` and JSON would disagree).
This is the intended rule for all three runners; Go and Python follow in a
config-contract task. No fixture case relies on stringification today.

## Adapter boundaries

Adapters read what setup installed: the resource on a span of the installed
TracerProvider (Go) / `TracerProvider.resource` (Python), the helper's
`/metrics` handler, the global providers (public SDK API), and the wire.
Exceptions, and why:

- **Go `metric_exporters`, interval, `log_level`** come from `installedOptions()`,
  the options the last successful `Setup` resolved and acted on (an unexported
  seam; the interval is the one `Setup` gave the periodic readers). Python uses
  the options `setup_telemetry()` returns.
- **Go `logging` uses `NewLogger`**, the helper's public logger constructor:
  Go apps build their logger with it; the helper installs none.

**Diagnostics channel contract (for A3/A9):** helper configuration
diagnostics MUST go through `log`/`slog` from the helper package (Go) or the
`staffops_otel_helper*` loggers (Python) — not `otel.Handle`, `fmt.Fprint*(os.Std…)` or
`warnings.warn` — or `helper_log` won't see them. Go enforces it with
`go/diag_channel_test.go` (AST check of non-test files; `otel.Handle` only
for the runtime errors on its allow-list). Go helper diagnostics go to the
package's stderr `diagLogger`, which the Go runner hooks.

## Not expressible yet (by design, tracked elsewhere)

- **HTTP `OTEL_EXPORTER_OTLP[_<SIGNAL>]_TIMEOUT`** (R2): OTLP/HTTP carries no
  timeout on the wire; A3 covers it with unit tests. gRPC timeouts are
  asserted through `grpc-timeout` (`export.timeout_env_grpc`,
  `export.per_signal_timeout_grpc`).
- **Database semconv keys** (R10, `db.system.name` + `db.system`): no fixture
  kind drives a SQL driver. Covered per language: Python
  `tests/test_a10_db_semconv.py` (contrib stability mode always; real
  SQLAlchemy + sqlite spans when SQLAlchemy is installed), Go `ext/otelsql`
  tests. Adding a `sql` kind would need a driver step in both runners.

## Adding or flipping a case

1. Add the case (ids are `<area>.<behavior>`), with `requirement` pointing at
   the spec statement.
2. Run both suites; set each language's status to what the code **does today**
   (`pass`, or `xfail:<task that will fix it>` per `specs/config-contract-v0.3/tasks.md`).
3. A task that changes behavior flips its cases to `pass` in the same PR.

## Notes from building the fixture

- `grpc.health_*_counted_in_metrics` are `skip:no-upstream-rpc-metrics` in
  Python: the contrib gRPC instrumentation emits no RPC metrics (R7, amended).
- Go `grpc.health_{check,watch}_no_span` pass since A7-go. In 0.2.0 the
  unary-server filter read `InterceptorInfo.Method`, which otelgrpc v0.56 left
  empty, so health checks were traced. Since A2 the helper uses otelgrpc v0.72
  stats handlers **without** `WithFilter` (it would drop metrics too — design
  D5), and the A7 health sampler drops the spans while the RPC stays counted.
