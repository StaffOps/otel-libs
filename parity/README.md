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
Python: records from the `otel_helper` logger hierarchy; helper diagnostics must
use those channels), `console` (process stdout + stderr) and `log`
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
| `{"prefix": "…"}` / `{"regex": "…"}` | prefix / RE2-compatible regex (Go `regexp`, Python `re.search`) |
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

`pass` | `xfail:<task-or-phase>` | `skip:<reason>`. **Strict**: an `xfail`
case that passes fails the build; only a behavior mismatch satisfies an
`xfail` — fixture and adapter errors never do. `python@sdk>=X.Y` overrides
`python` when the installed `opentelemetry-sdk` is at least X.Y (the highest
matching key wins).

**Release gate:** `skip:A…` (a case waiting for a task's API) must not survive a
release. `PARITY_RELEASE=1` enables `TestParityNoTaskSkipsAtRelease` (Go) and
`test_no_task_skips_at_release` (Python), which fail while any remain; the
release task (A11) runs them.

## Running

```bash
docker compose -f testing/docker-compose.yml run --rm go-tests
docker compose -f testing/docker-compose.yml run --rm python-tests
PY_VERSION=3.10 PY_DEPS=lower docker compose -f testing/docker-compose.yml run --rm python-tests

# one case
docker compose -f testing/docker-compose.yml run --rm go-tests go test -run 'TestParity/export.grpc_plaintext' -v .
docker compose -f testing/docker-compose.yml run --rm python-tests sh -c \
  "cp -r /src/python /work && cd /work && pip install -q -e '.[dev,all]' && pytest tests/test_parity.py -k export.grpc_plaintext"
```

Without `FAKEOTLP_HOST`, cases that need the receiver skip locally. With
`PARITY_REQUIRE_FAKEOTLP` set (compose, CI) they fail instead, including the
IPv6 and `{receiver_tls}` cases when those addresses are missing.

Each case runs in its own process (Go re-executes the test binary, Python runs
`tests/parity_driver.py`): OTel and SDK state is set-once per process — Go's
`resource.Default()` caches `OTEL_RESOURCE_ATTRIBUTES` on first use. Coverage
from those processes is merged into each suite's gate (Go: `PARITY_GOCOVERDIR`
+ `go tool covdata`; Python: `PARITY_COVERAGE_DATA`, combined by pytest-cov).

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
`otel_helper*` loggers (Python) — not `otel.Handle`, `fmt.Fprint*(os.Std…)` or
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
- Go `grpc.health_{check,watch}_no_span` are `xfail:A7-go`. In 0.2.0 the
  unary-server filter read `InterceptorInfo.Method`, which otelgrpc v0.56 left
  empty, so health checks were traced; the stream interceptors recorded no RPC
  metric, so `Watch` wasn't counted. Since A2 the helper uses otelgrpc v0.72
  stats handlers **without** `WithFilter` (it would drop metrics too — design
  D5): every health RPC is traced and counted until A7-go's sampler drops the
  spans.
