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
  "status": {"go": "pass", "python": "xfail:A3", "dotnet": "xfail:phase-c", "nodejs": "pass"}
}
```

| Field | Meaning |
|---|---|
| `env` | Environment for the case. The runner first removes every `OTEL_*`, `SERVICE_NAME` and `ENVIRONMENT` variable and sets `OTEL_HELPER_METRICS_PORT=0` (cases may override it). |
| `code` | Options set in code, snake_case: `service_name`, `environment`, `otel_endpoint`, `otlp_protocol`, `metric_exporters`, `sample_ratio`, `debug`, `export_interval_ms`, `resource_attributes` (also `traces_endpoint`, `metrics_endpoint`, `logs_endpoint`, `insecure`, `compression`, `export_timeout_ms`, `health_paths`, and `disabled_signals` since A19; `prometheus_metrics_port` since A29; `http_opt_in_metrics` since A30). A key an adapter can't map is an adapter error (hard failure), so a case for an API that doesn't exist yet is `skip:<task>-…` until that task adds the API and the mapping. |
| `input` | Kind-specific parameters (schema below). |
| Placeholders | `{receiver}` (plain + TLS ports), `{receiver_tls}` (TLS on the standard port 4317), `{receiver_ipv6}`, `{receiver_ca}`. |

### Kinds

| Kind | `input` | Does | Snapshot fields (`expect` keys) |
|---|---|---|---|
| `resolve` | — | Setup, read what it installed | `resource` (all attributes of the installed resource), `metric_exporters` (the **effective** list: `[]` when metrics are disabled — `OTEL_HELPER_DISABLED_SIGNALS`, code disabled signals — or the SDK is, `OTEL_SDK_DISABLED=true`; defined 2026-10-29, A19 fix round), `metric_export_interval_ms`, `log_level`, `prometheus_metrics_port` (the resolved `/metrics` listener port; added 2026-11-02, A21), `prometheus_metrics_host` (the resolved bind address from `OTEL_EXPORTER_PROMETHEUS_HOST`: `""` = every interface, else the IP literal without brackets, `localhost` → `127.0.0.1`, an IPv4-mapped address (`::ffff:a.b.c.d`, any spelling) → its IPv4 literal; added 2026-11-05, A29), `http_opt_in_metrics` (the resolved HTTP opt-in metrics switch, a boolean; added 2026-11-07, A30), `exemplar_filter` (`always_on`, `always_off` or `trace_based`: the filter of the **installed** SDK MeterProvider, absent when none is installed — Go reads the SDK's unexported pipeline field by reflection, Python the provider's `_sdk_config`, .NET the `MeterProviderSdk` member by reflection, Node.js has none (`skip:no-upstream-node-exemplar-filter`); added 2026-11-10, .NET C9 fix round 1, task A33) |
| `export` | — | Setup, emit one span + counter + log, shut down, read the receiver | `received{traces,metrics,logs}` → records `{proto, tls, port, path, raw_path, encoding, headers, timeout_ms}` (`path` decoded, `raw_path` as sent with escapes kept; `timeout_ms` from the gRPC `grpc-timeout` header) |
| `sampler` | `scenario`: `root`, `remote_sampled`, `remote_unsampled`, `remote_sampled_debug_tracestate`, `remote_sampled_debug_baggage`, `local_child_of_root`, `local_child_of_remote_unsampled` | Start one span | `sampled`, `attributes` |
| `propagation` | — | Inject inside a span with baggage `parity=1` | `headers` (sorted names) |
| `http_server` | `requests`, `traceparent`, `child_span`, `allow_server_error` (fix round 2 of C10, 2026-11-10: without it a 5xx response is an adapter error in every driver, so a failing request cannot satisfy `server_spans: 0` or `requests_counted`) | Serve the requests through the helper's HTTP server instrumentation; optionally the handler starts `handler-child` | `response_statuses` (each request's HTTP status), `server_spans`, `server_continues_inbound` (a SERVER span exists and every SERVER span is in the inbound `traceparent`'s trace; added 2026-11-10, C10 fix round 1), `handler_child_spans`, `requests_counted` (HTTP server duration `_count` from the helper's `/metrics`), `scrape` (that `/metrics` text; added 2026-11-02, Node B7) |
| `grpc_server` | `method` | One call on an instrumented server (`/parity.v1.Echo/NotFound` answers NOT_FOUND; added 2026-11-02, A22) | `server_spans`, `rpcs_counted`, `call_ok`, `server_span_status` (status code of the first SERVER span: `UNSET`, `ERROR`, `OK`, or `none`; added 2026-11-02, A22), `server_span_name` (name of the first SERVER span, or `none`; added 2026-11-05, A30), `client_span_name` (the same for the first CLIENT span; added 2026-11-08, A30) |
| `logging` | — | Emit one WARN record through the helper's logger | (common fields) |
| `metrics_scrape` | — | Increment `parity.requests` and `parity.payload` (unit `By`, by 5; added 2026-11-02, Node B7), record the Gauge `parity.utilization` (unit `1`, 0.5) and add 1 to the UpDownCounter `parity.inflight` (unit `1`; both A21), scrape the helper's `/metrics` | `scrape`, `scrape_status` (HTTP status of the mounted handler: 503 before/after/failed setup, 200 when Prometheus is deliberately inactive — design "Mounted /metrics handler status") |
| `metrics_listener` | `requests` (`"METHOD /path"` strings) | Set `OTEL_HELPER_METRICS_PORT` to a free port, set up, send each request to the helper's own listener (added 2026-11-02, A21) | `listener_statuses` (HTTP status per request) |
| `fork` | — | Python: import the helper, `os.fork()` twice, set up in each child (gunicorn `post_fork` model). Node.js: `cluster.fork()` twice, set up in each worker (design D-JS13). Go: `skip:no-prefork-model` | `instance_ids`, `instance_ids_distinct` |

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

A request carrying `x-parity-echo-error` (any value) is answered with an error
that echoes its `x-parity-*` headers, as a collector or proxy echoing the
request would: OTLP/HTTP `400` with a JSON body, OTLP/gRPC `INVALID_ARGUMENT`
with them in the status message (added 2026-11-05, A29; cases
`export.http_error_body_header_values_not_logged`,
`export.grpc_error_details_header_values_masked`). The request is still
recorded. *A29 fix round (2026-11-05):* the 400 body also carries the
body-only marker `parity-body-marker` (a case asserts it is never logged), and
`x-parity-echo-error: token` echoes only the last space-separated part of each
other `x-parity-*` value (the credential of `Bearer <token>`): in the HTTP
reason phrase (`400 Rejected invalid token <t>`, HTTP/1.1) and in the gRPC
status message.

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
| `{"lte": n}` / `{"gte": n}` | **extension**: numeric bounds. For a gRPC `timeout_ms` bound, `lte` is the configured value plus one encoding unit: `grpc-timeout` is rounded up to its unit (10 ms for 1–10 s in gRPC core) |
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
any `nodejs` status starts with `skip:B` (B14). For .NET the same variable
enables `Release_gate_no_dotnet_case_still_xfail_or_skipped_pending_a_task`,
which fails while any `dotnet` status is `xfail:*`, `skip:phase-c` or
`skip:C<n>-…` (DN11; C4; CI sets it on `dotnet/v*` tags). *Wired 2026-11-04 (B14):*
CI sets `PARITY_RELEASE=1` on release tags — `go-test` and `python-test` on
`v*`, `node-test` on `nodejs/v*` — and `testing/verify.sh --release` sets it
locally (the compose test services pass it through).

## Running

```bash
docker compose -f testing/docker-compose.yml run --rm go-tests
docker compose -f testing/docker-compose.yml run --rm python-tests
PY_VERSION=3.10 PY_DEPS=lower docker compose -f testing/docker-compose.yml run --rm python-tests
NODE_VERSION=26 docker compose -f testing/docker-compose.yml run --rm node-tests
docker compose -f testing/docker-compose.yml run --rm dotnet-tests

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
`tests/parity_driver.py`, Node runs `nodejs/test/parity/driver.cjs`, .NET runs
`StaffOps.OtelHelper.ParityDriver`): OTel and SDK state is set-once per process — e.g. the OTel globals, and
Python's SDK log module, which parses `OTEL_RESOURCE_ATTRIBUTES` when first imported. Coverage
from those processes is merged into each suite's gate (Go: `PARITY_GOCOVERDIR`
+ `go tool covdata`; Python: `PARITY_COVERAGE_DATA`, combined by pytest-cov;
Node: children inherit `NODE_V8_COVERAGE` and exit with `process.exit()`, and
the runner runs inside the core package's `npm test`, so c8 merges them; .NET:
the driver runs coverlet's instrumented assemblies from the test output folder
and always exits normally).

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
  getter; added in B3). No probe span is ever started for it. `metric_exporters`,
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

## .NET runner

`dotnet/StaffOps.OtelHelper.Tests/ParityTests.cs` (design D-NET12; task C4,
2026-11-09): the xUnit runner, with `ParityHarness.cs` holding the schema,
placeholders, child execution and matcher (the `harness.cjs` rules), and the
console project `dotnet/StaffOps.OtelHelper.ParityDriver` as the per-case
child (`dotnet StaffOps.OtelHelper.ParityDriver.dll <case.json>`).

```bash
docker compose -f testing/docker-compose.yml run --rm dotnet-tests
# a few cases (the theory still lists them by id)
docker compose -f testing/docker-compose.yml run --rm -e PARITY_ONLY=export.grpc_plaintext,svc.code_beats_env dotnet-tests
```

- **Execution.** `ParityTests.Case` is one theory row per non-skipped case.
  The first row runs every selected case once for the class: non-export cases
  in parallel (at most `min(8, 2 × CPUs)` children), then, one at a time,
  cases that bind a fixed listener port and the export cases. The parent kills
  a child after 120 s; the driver gives export cases 75 s (others 15 s) to shut
  down and answers within 110 s in any case. `PARITY_ONLY` (comma-separated
  ids) narrows both the theory rows and the run; `PARITY_RESULTS=<path>` writes
  every outcome, snapshot included, as JSON (used to record statuses).
- **`propagation` is the OpenTelemetry propagator only.** The kind injects with
  `Propagators.DefaultTextMapPropagator`. .NET also writes context through the
  runtime's `DistributedContextPropagator` (`HttpClient` egress, ASP.NET Core
  ingress); that path is covered by unit tests, not by the fixture
  (`SamplerPropagatorDebugTests`, `dotnet/TESTS.md`, task C10, 2026-11-10).
- **Coverage.** The test project references the driver, so the driver runs
  from the test output folder with the assemblies coverlet instrumented; it
  always exits 0 (`Environment.Exit`, after writing the snapshot), and
  coverlet merges the child hits into the gate. The driver itself is excluded
  (`/p:Exclude="[xunit*]*%2c[StaffOps.OtelHelper.ParityDriver]*"`).
- **Stdout is the snapshot.** The driver keeps the real stdout for one JSON
  object and points `Console.Out` at stderr; `console` is the child's stderr.
  A stdout that is not one JSON value fails the case as a driver crash.
- **Statuses.** `dotnet` only. A `skip:C<n>-…` must name a task row of
  `specs/dotnet-otel-helper/tasks.md` that is not ✅; any other skip must be a
  D-NET15 difference (`skip:no-prefork-model`, `skip:no-detector-variable`,
  `skip:no-upstream-rpc-metrics`, `skip:no-upstream-dotnet-prom-*`,
  `skip:no-upstream-dotnet-attribute-limit`,
  `skip:no-upstream-dotnet-grpc-tls-files`,
  `skip:no-upstream-dotnet-http-body-size`, exact; the `prom-*` family by
  pattern, e.g. `skip:no-upstream-dotnet-prom-mounted-handler`). `PARITY_RELEASE=1` fails while an
  `xfail:*`, `skip:phase-c` or `skip:C<n>-…` remains (DN11; CI sets it on
  `dotnet/v*` tags). Without
  `FAKEOTLP_HOST` and `PARITY_REQUIRE_FAKEOTLP`, receiver cases are reported
  as passed with a "not run" line (xUnit 2 has no runtime skip).
- **Environment.** As the other runners, plus the keys the .NET host maps into
  `IConfiguration` (which OTel .NET reads): every key matching
  `^(DOTNET_|ASPNETCORE_)?(OTEL_|SERVICE_NAME$|ENVIRONMENT$)` (any case) and
  `Logging__*` is removed before the case env is applied.
- **`helper_log`, `diagnostics`, `console`.** Since task C5 (2026-11-10) the
  driver registers the helper's diagnostics sink (design D-NET5) before
  `AddOtelHelper` — an internal type, reached through `InternalsVisibleTo`;
  applications cannot replace it — writing every record to stderr (so
  `console` sees it) and keeping it for the snapshot. `diagnostics` holds
  the WARN and ERROR records of both origins — the helper's own and the
  OpenTelemetry SDK `EventSource` events the helper forwards (origin `sdk`,
  redacted; `ForwardSdkDiagnostics`) — and `helper_log` every record of
  origin `helper`, INFO included. The C4 field `sdk_events` and the driver's own
  `EventListener` are gone: forwarded SDK events are real output now and reach
  `diagnostics`, `console` and `log` like any record. The helper does not
  forward the Prometheus listener's cancel once the host is stopping, the
  SDK's view-drop notices, nor the SDK's parse warnings for
  `OTEL_EXPORTER_OTLP_ENDPOINT`, `_PROTOCOL` and `_COMPRESSION` (it resolves
  and warns about those itself). A case whose warning only the SDK writes
  (`sampler.*`, `OTEL_SDK_DISABLED`, `OTEL_EXPORTER_PROMETHEUS_PORT` until
  C9, C10, C12) also asserts `helper_log`, so an SDK line cannot pass it
  (C5 fix round, 2026-11-10). .NET escapes more characters in a record than Node: C0, C1, DEL,
  U+2028/U+2029 and also the bidirectional format characters (U+061C,
  U+200E, U+200F, U+202A–U+202E, U+2066–U+2069). The code-error regexes
  accept the .NET option names qualified (`TelemetryOptions\.SampleRatio`,
  `TelemetryOptions\.OtelCollectorEndpoint`, …).
- **Adapter seams.** Setup is `AddOtelHelper(…)` on
  `Host.CreateApplicationBuilder()` (Kestrel `WebApplication` for
  `http_server` and `grpc_server`).
  - Logging: `builder.Logging.ClearProviders()` runs before `AddOtelHelper`,
    so only what the helper installs logs (no host console provider; C13).
  - Scope `parity`: traces through `AdditionalActivitySources`, metrics through
    `AdditionalMeters` (added by task C5; C4 used
    `ConfigureOpenTelemetryMeterProvider(b => b.AddMeter("parity"))`).
  - An in-memory span processor captures spans; `resource` is
    `TracerProvider.GetResource()`.
  - `metric_exporters`, the interval and the port come from the helper's
    internal resolved state (`ResolvedTelemetry`, through `InternalsVisibleTo`;
    since C5 `IOptions<TelemetryOptions>` holds code values only, and the SDK
    has no public getter for a reader's interval); `log_level` is the installed
    `LoggerFilterOptions.MinLevel`; `metric_exporters` is `[]` when metrics are
    disabled or `OTEL_SDK_DISABLED` is true, which the helper reads from
    `IConfiguration` as the SDK does (the providers expose no public flag).
  - `prometheus_metrics_host` is omitted: the helper has no such setting yet
    (it binds the IPv4-only `+` prefix, whatever the environment says; task
    C12). `http_opt_in_metrics` is the resolved `HttpOptInMetrics` (since C11).
  - The helper has no mounted `/metrics` handler (D-NET11), so `scrape` comes from the
    helper's own listener: the driver sets `OTEL_HELPER_METRICS_PORT` to a free
    port only when the case sets no port (env or code) and scrapes the resolved
    port (a resolved `0` is an adapter error); `scrape_status` is `0` when
    nothing listens.
  - `grpc_server` sets
    `OTEL_DOTNET_EXPERIMENTAL_ASPNETCORE_ENABLE_GRPC_INSTRUMENTATION=true`
    (O7 (b)) and serves raw-byte methods (no protoc). Requests to the test
    servers go over plain sockets: the helper instruments `HttpClient`. The
    gRPC client is the instrumented `Grpc.Net.Client` in the same process, so
    a health call's dropped client span sends an unsampled parent: the
    `grpc.health_*_no_span` cases pass through the client filter and the
    ParentBased default together; the server filter alone is covered by
    `HttpGrpcInstrumentationTests` (an uninstrumented client).
    *Superseded 2026-11-10 (C11 fix round 1):* `grpc.health.v1.Health/*` calls now
    come from an **uninstrumented** client (`SuppressInstrumentationScope` and a
    `SocketsHttpHandler` with no `ActivityHeadersPropagator`: no client span, no
    `traceparent`), as a kubelet or `grpc_health_probe` call does, so the
    `grpc.health_*_no_span` cases need the server filter (checked: with the 0.2.x
    server filter both fail). Other methods keep the instrumented client, whose
    CLIENT span the snapshot reports. The HTTP counterpart already exists: the
    `http_server` requests go over a plain socket without `traceparent` unless the
    case sets one.
    *Note 2026-11-10 (C11 fix round 2, measured):* with the server-side gRPC health
    drop neutralized in a scratch copy, Python fails both `grpc.health_*_no_span`
    cases (strong: its filtered client injects nothing), Node.js passed them on the
    unsampled parent alone (weak). The Node.js driver now makes `grpc.health.v1.Health/*`
    calls under `suppressTracing` (`@opentelemetry/core`): no client span, no
    `traceparent`; re-measured, both cases then fail without the server drop. Go needs
    no change (its filtered otelgrpc client injects nothing).
    *Note 2026-11-10 (C11 fix round 1, coordinator decision):* the .NET driver
    enables `OTEL_DOTNET_EXPERIMENTAL_ASPNETCORE_ENABLE_GRPC_INSTRUMENTATION`, so
    `grpc.server_span_name_semconv` and `grpc.server_not_found_status_unset` describe
    a correctly configured app (O7 (b): the app sets the key; the examples do). A
    default app without the key emits HTTP-shaped server spans (`POST /pkg.Svc/Method`,
    status from HTTP 200) — documented in `dotnet/README.md` and covered by the unit
    tests `Without_The_Key_Grpc_Server_Spans_Are_Http_Shaped` and
    `Mapped_Grpc_Endpoints_Without_The_Key_Warn_Once`.
- **`code` keys.** Mapped to `TelemetryOptions` members (`CodeMapper`); since
  task C5 every fixture key has one (`traces_endpoint` → `TracesEndpoint`,
  `insecure`, `compression`, `sample_ratio`, `health_paths`,
  `export_interval_ms` as `long`, `environment` as the string), so no case is
  `skip:C5-no-api` any more; a key without a member is an adapter error.
  `http_opt_in_metrics` maps to `HttpOptInMetrics` (since C11).
  .NET errors name the option in its C# spelling (`TelemetryOptions.SampleRatio`),
  which some fixture regexes (`sample_ratio|sampleRatio`, `otel_endpoint|otelEndpoint`,
  the substring `insecure`) do not match yet: those cases stay `xfail:C<n>`.
- **`fork`.** Never spawned (`skip:no-prefork-model`).
- **Signals and exporters (task C9, 2026-11-10).** `metric_exporters` is
  `ResolvedMetricExporters()` and `metric_export_interval_ms` the resolved
  interval the helper hands the OTLP and console readers. `console` in an
  `export` case is the SDK console exporter's output: the driver points
  `Console.Out` at stderr, so it reaches `console` with the helper's
  diagnostics. `config.disabled_signals_standard_var_wins` is `xfail:C16`, not
  `xfail:C9`: the precedence passes (traces are received), the warning it
  asserts is the `OTEL_HELPER_DISABLED_SIGNALS` deprecation, which task C16
  owns. *Fix round 1 (2026-11-10):* the case is split — it now asserts only the
  precedence (.NET `pass`), and
  `deprecation.disabled_signals_standard_var_set_still_warns` asserts the
  deprecation (.NET `xfail:C16`).

### Rules the fixture cannot express (A30, 2026-11-08)

Redis span names and `db.operation.name`; AWS span names, kinds, RPC modes and
attribute values (`messaging.system`, `db.system.name`, `server.*`); URL query
redaction. The fixture has no Redis, AWS or HTTP-client kind, so these stay
unit- and opt-in-package tests per language (a future `aws_call` / `redis_call`
kind backed by fakes would close it).

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

## R10 language-specific extra attributes

R10 requires the shared HTTP metric attributes (`http.request.method`,
`http.response.status_code`, `http.route` when known, `url.scheme` on the
server) to match, and allows language-specific extras listed here (added
2026-11-02, Node B6 fix round; observability and dev reviews). The fixture
matches metric names and counts, never the full label set, so these do not
fail a case.

| Language | Metric | Extra or missing attributes |
|---|---|---|
| Go (`otelhttp` v0.72) | `http.server.request.duration` | Extra `server.address`, `server.port`, `network.protocol.name`; no `error.type` on 5xx |
| Node.js (`instrumentation-undici` 0.34) | `http.client.request.duration` (fetch / undici) | Extra `url.scheme` (the `node:http` client metric has none) |
| Node.js | `http.client.request.duration` | `instrumentation-http` and `instrumentation-undici` both register the instrument, under different scopes and with different descriptions; the `/metrics` exposition has one family, so a single `# HELP` text is shown for both (dev review L3) |

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

- **Exemplars on the wire** (`OTEL_METRICS_EXEMPLAR_FILTER`, ADR-013, task C9,
  2026-11-10): no fixture kind reads exemplars themselves. The .NET helper
  parses the variable and always sets the filter (default `trace_based`; the
  .NET SDK default is `AlwaysOff`), covered by the unit test
  `ExemplarFilter_DefaultsToTraceBased_AndHonorsEnv`. *Fix round 1
  (2026-11-10):* the resolved filter itself is a `resolve` field
  (`exemplar_filter`, cases `metrics.exemplar_filter_*`); Python ignores the
  variable until task A33.

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
