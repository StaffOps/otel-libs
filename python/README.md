# OTel Helper — Python

OpenTelemetry helper for Python services. One call configures traces, metrics
and logs with the shared cross-language contract: standard `OTEL_*` variables
first, export through an OpenTelemetry Collector, sampling and resource
enrichment at the Collector.

Audience: developers instrumenting a Python service. The practical guide is
[HOW-TO.md](HOW-TO.md); runnable examples are in [example/](example/README.md);
how to test the library is in [TESTS.md](TESTS.md).

## Requirements

- Python 3.10 or newer (CI tests 3.10, 3.11 and 3.13; the examples use 3.11).
- OpenTelemetry Python API/SDK `>=1.42,<1.46` and contrib `>=0.63b0,<0.67b0`.
  The two ranges move together (contrib betas pin one SDK minor), so pip
  resolves a matched pair.

## Installation

The package is **not on PyPI** (the name is not registered there, so never
install it by name). Install the release wheel, or pin its URL with a hash —
see [CONSUMING.md](../CONSUMING.md#python-github-release-assets):

```bash
pip install ./staffops_otel_helper-<version>-py3-none-any.whl
# requirements.txt, hash-pinned:
# staffops-otel-helper @ https://github.com/StaffOps/otel-libs/releases/download/<tag>/staffops_otel_helper-<version>-py3-none-any.whl#sha256=<digest>
```

> **Renamed in 0.3.0rc3 (ADR-011; `0.2.x` and `0.3.0rc2` are `otel-helper`).**
> Distribution `otel-helper` → `staffops-otel-helper`, import `otel_helper` →
> `staffops_otel_helper`, helper loggers `otel_helper.*` → `staffops_otel_helper.*`.
> `import otel_helper` keeps working during 0.3.x with one `DeprecationWarning`
> and one log line on `staffops_otel_helper.compat` (same objects, same
> submodules such as `otel_helper.ext.sql`, `.pyi` stubs for type checkers);
> it is removed in 1.0. Log filters on the old logger names must move: they
> are not aliased. Never keep both distributions installed — uninstall
> `otel-helper` first.

FastAPI, HTTPX, requests, gRPC (sync and `grpc.aio`), system metrics and the
`logging` bridge are part of the core install. SQLAlchemy, Redis and botocore
live in extras: `[sql]`, `[redis]`, `[aws]` or `[all]`, requested on the wheel
(`pip install "staffops-otel-helper[sql] @ file://$(pwd)/staffops_otel_helper-<version>-py3-none-any.whl"`).

## Quick start

```python
import staffops_otel_helper  # isort: skip  (see "Import order" below)

from staffops_otel_helper import setup_telemetry, shutdown_telemetry

setup_telemetry()
try:
    run_app()
finally:
    shutdown_telemetry()  # flush; also runs at exit, but not on an unhandled SIGTERM
```

Configure through the environment:

```bash
export OTEL_SERVICE_NAME=checkout-api
export OTEL_RESOURCE_ATTRIBUTES=deployment.environment.name=PRD
export OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317
```

Or in code (code values win over the environment):

```python
from staffops_otel_helper import setup_telemetry, TelemetryOptions

setup_telemetry(TelemetryOptions(
    service_name="checkout-api",
    resource_attributes={"app.component": "gateway"},
))
```

### Import order

`import staffops_otel_helper` must run before any `opentelemetry.*` import. The helper
makes unsupported `OTEL_PROPAGATORS` values (`b3`, `jaeger`, `xray`) warn and
be skipped; imported later, the OpenTelemetry API has already read the variable
and raises `ValueError` for them. isort sorts `opentelemetry` before
`staffops_otel_helper`, so keep the line first with `# isort: skip`.

## Environment variables

Precedence everywhere: **code (`TelemetryOptions`) > standard `OTEL_*`
variable > deprecated / `OTEL_HELPER_*` variable > helper default**. An
invalid environment value logs a warning on the `staffops_otel_helper` logger and falls
back to the next level; an invalid value passed in code raises `ValueError`
naming the field. Warnings never print header values, client keys or URL
credentials.

### Service identity

| Variable | Default | Notes |
|---|---|---|
| `OTEL_SERVICE_NAME` | _(unset)_ | Wins over `OTEL_RESOURCE_ATTRIBUTES` and `SERVICE_NAME`. Unset everywhere → SDK `unknown_service` + one warning. Whitespace-only = unset |
| `OTEL_RESOURCE_ATTRIBUTES` | _(unset)_ | Any resource attribute, e.g. `deployment.environment.name=PRD`. Never overwritten by a helper default. Parsed by the helper: values percent-decoded then trimmed; an item without `=`, with an empty key or a bad `%` escape is warned by position (never by value); a blank identity value (`service.name=`) counts as unset; `telemetry.sdk.*` are ignored (the SDK sets them). `OTEL_EXPERIMENTAL_RESOURCE_DETECTORS` is ignored (warned) |
| `SERVICE_NAME` | _(unset)_ | **Deprecated** (removed in 1.0) → `OTEL_SERVICE_NAME`. Used only when nothing else sets `service.name` |
| `ENVIRONMENT` | _(unset)_ | **Deprecated** alias → `OTEL_RESOURCE_ATTRIBUTES=deployment.environment.name=<value>`. Unset → attribute absent + one warning; unknown values are emitted verbatim |

`service.instance.id`: the SDK adds one per process on sdk ≥ 1.45; on 1.42–1.44
the helper adds a UUID per process. An id from code or the environment is never
rewritten (see HOW-TO, pre-fork servers).

### OTLP export

| Variable | Default | Notes |
|---|---|---|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | _(unset)_ | Generic endpoint. For `http/protobuf`, `/v1/<signal>` is appended (a path prefix such as `/otlp` is kept) |
| `OTEL_EXPORTER_OTLP_<SIGNAL>_ENDPOINT` | _(unset)_ | `TRACES`, `METRICS`, `LOGS`. Used as-is (no path appended). Configures that signal only |
| `OTEL_EXPORTER_OTLP[_<SIGNAL>]_PROTOCOL` | `grpc` | `grpc` or `http/protobuf`. Unset and endpoint port `4318` → `http/protobuf`. `http/json` is warned and ignored |
| `OTEL_EXPORTER_OTLP[_<SIGNAL>]_INSECURE` | `false` | Only for schemeless endpoints. `true` (any case) = plaintext; any other value = false, warned unless it is `false` |
| `OTEL_EXPORTER_OTLP[_<SIGNAL>]_TIMEOUT` | `10000` | **Milliseconds** (OTel spec; the Python SDK alone would read seconds). Plain decimals only, clamped to [1 ms, 24 h]; values below 100 warn "looks like seconds" |
| `OTEL_EXPORTER_OTLP[_<SIGNAL>]_COMPRESSION` | `gzip` | `gzip` or `none` (any case); anything else warns and uses `gzip` |
| `OTEL_EXPORTER_OTLP[_<SIGNAL>]_HEADERS` | _(unset)_ | `key=value,...`, values percent-decoded. Keys must match `[A-Za-z0-9._-]+`, are lowercased, last one wins; malformed items are skipped with a warning naming only their position |
| `OTEL_EXPORTER_OTLP[_<SIGNAL>]_CERTIFICATE`, `_CLIENT_CERTIFICATE`, `_CLIENT_KEY` | _(unset)_ | Read by the SDK. A certificate with a plaintext endpoint is warned |

Each per-signal variable falls back to the generic one, then to the default; an
invalid per-signal value is warned and falls back the same way. A signal is
"OTLP configured" when code, the generic endpoint or its own endpoint sets
one; with only `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`, traces are exported,
metrics use the Prometheus fallback and logs stay local.

### Signals, sampling, propagation

| Variable | Default | Notes |
|---|---|---|
| `OTEL_SDK_DISABLED` | `false` | `true` installs propagators only (one INFO line): no providers, exporters, `/metrics` listener or log handlers |
| `OTEL_CONFIG_FILE` / `OTEL_EXPERIMENTAL_CONFIG_FILE` | _(unset)_ | Declarative configuration is **not supported**: ignored with one warning per process naming the variable (never the path) |
| `OTEL_TRACES_EXPORTER` / `OTEL_LOGS_EXPORTER` | `otlp` | `otlp`, `console`, `none` or a list (`otlp,console`). `logging` is a deprecated alias of `console` (variables only: in `metric_exporters` it raises) |
| `OTEL_METRICS_EXPORTER` | _(inferred)_ | `otlp`, `prometheus`, `console`, a list, `none`. Unset: `otlp` when a metrics endpoint is configured, else `prometheus` |
| `OTEL_METRIC_EXPORT_INTERVAL` | `30000` | Milliseconds (the SDK default is 60000): ASCII digits with an optional `+` (any number of digits; leading zeros ignored), > 0, else warned and 30 000; above 2 147 483 647 clamped with a warning |
| `OTEL_TRACES_SAMPLER` / `OTEL_TRACES_SAMPLER_ARG` | `parentbased_always_on` | Spec names, case-insensitive. Unknown name → warning + default; invalid `_ARG` → warning + 1.0 |
| `OTEL_PROPAGATORS` | `tracecontext,baggage` | `tracecontext`, `baggage`, `none`. Other values are warned and skipped (needs the import order above) |
| `OTEL_SEMCONV_STABILITY_OPT_IN` | `http,database/dup` | Set by the helper when unset, before any instrumentation: stable HTTP metrics/attributes and both DB attribute generations. Your value wins (`http/dup` = both HTTP generations) |
| `OTEL_PYTHON_LOG_CODE_ATTRIBUTES` | `false` | Upstream variable: `true` adds `code.*` attributes to log records |

### Helper variables

| Variable | Default | Notes |
|---|---|---|
| `OTEL_HELPER_DEBUG_LEVEL` | `false` | DEBUG logs, every extension instrumentor, and `debug=true` on entry spans |
| `OTEL_HELPER_METRICS_PORT` | `9464` | Standalone Prometheus `/metrics` listener; `0` disables it (serve `metrics_app()` from the app instead: `app.add_route("/metrics", metrics_app())`) |
| `OTEL_HELPER_DISABLED_METRICS` | _(unset)_ | Comma list of wildcard instrument names dropped from every exporter |
| `OTEL_HELPER_SAMPLE_RATIO` | `1.0` | **Deprecated** → `OTEL_TRACES_SAMPLER=parentbased_traceidratio` + `OTEL_TRACES_SAMPLER_ARG`. Used only while `OTEL_TRACES_SAMPLER` is unset |
| `OTEL_HELPER_DISABLED_SIGNALS` | _(unset)_ | **Deprecated** → `OTEL_{TRACES,METRICS,LOGS}_EXPORTER=none`. A standard variable with a valid entry wins for its signal, and `metric_exporters` wins for metrics; unknown entries warn once |
| `OTEL_HELPER_EXTRA_INSTRUMENTATION` | `SQL` | **Deprecated** → call `staffops_otel_helper.ext.*` before setup. Still effective; warns whenever set (also when empty, which turns the `SQL` default off) |

Deprecated variables warn once per process, naming the replacement (never the
value), also when a standard variable or code wins. Behavior is unchanged
until 1.0.

## Endpoint and TLS rules

| Endpoint | Transport |
|---|---|
| `https://host:4317` | TLS (system CA store) |
| `http://host:4317` | Plaintext |
| `host:4317` (schemeless) | TLS; plaintext only with `OTEL_EXPORTER_OTLP_INSECURE=true` (or `TelemetryOptions(insecure=True)`). Deprecated for `http/protobuf` |

- The scheme always wins over `INSECURE`; a contradiction is warned (and in
  code raises).
- gRPC URL without a port → `:4317` with a warning; `http/protobuf` URL without
  a port → 443/80.
- URL user info, query and fragment are stripped (warned, redacted): put
  credentials in `OTEL_EXPORTER_OTLP_HEADERS`.
- `unix://`, `ftp://` and other schemes leave the signal unconfigured.

## What is configured automatically

| Signal | Captured |
|---|---|
| Traces | FastAPI server, HTTPX and requests clients, gRPC client and server (sync and aio). Health probes produce no server span |
| Metrics | HTTP server/client duration (stable semconv, seconds), system/process metrics, your instruments; trace-based exemplars; every 30 s |
| Logs | Standard `logging` records with trace/span ids, exported when a logs exporter is active; single-line JSON on stdout otherwise |

Without any OTLP endpoint: metrics on `http://<pod>:9464/metrics`, traces
in-process only (propagation still works), logs as JSON on stdout.

## Public API

| Name | Purpose |
|---|---|
| `setup_telemetry(options=None)` | Configure everything; later calls return the first resolved options |
| `shutdown_telemetry(timeout_ms=30000)` | Flush and shut down; raises `RuntimeError` on failure or timeout |
| `TelemetryOptions` | Code configuration (fields in HOW-TO §12) |
| `get_tracer(name=None)` / `get_meter(name=None)` | Tracer / Meter from the global providers (default scope name `otel-helper`, shared with Go) |
| `start_root_span(tracer, name, **kwargs)` | Context manager starting a new trace |
| `instrument_fastapi(app)` | Instrument an app whose `FastAPI` class was imported before setup |
| `metrics_app()` | ASGI app serving `/metrics` from the app's own server |
| `DEFAULT_HEALTH_PATHS` | `/health /healthz /ready /readyz /live /livez /ping /metrics` |
| `staffops_otel_helper.ext.{sql,redis,aws}` | `instrument_sql(engine=None)`, `instrument_redis()`, `instrument_aws()` → `bool` |

## Upgrading from 0.2

See [HOW-TO.md, "Migrating from 0.2 to 0.3"](HOW-TO.md#migrating-from-02-to-03)
and the root `CHANGELOG.md`. The biggest changes: stable HTTP metric names,
`OTEL_SERVICE_NAME` beats `SERVICE_NAME`, no `LOCAL` default environment,
timeouts in milliseconds, a parent-based sampler, and no OTel log handler
without a logs exporter.

## Architecture

```text
[ Python app ] --OTLP gRPC :4317 or HTTP :4318--> [ OTel Collector ] --> traces / metrics / logs backends
```

- The SDK samples parent-based always-on; tail sampling happens at the Collector.
- The SDK sets `service.name`, `deployment.environment.name` and
  `service.instance.id`; the Collector (`k8sattributes`) adds the rest.
- Nothing is exported directly to a backend; the only exception is the
  Prometheus `/metrics` fallback.
