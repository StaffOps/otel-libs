# HOW-TO: Using staffops-otel-helper (Python)

Practical guide for Python developers instrumenting a service with
`staffops-otel-helper` 0.3. The variable reference is in [README.md](README.md#environment-variables);
runnable examples are in [example/](example/README.md).

Prerequisites: Python 3.10+, the `staffops-otel-helper` wheel installed (not on PyPI —
see [CONSUMING.md](../CONSUMING.md#python-github-release-assets)), and an OpenTelemetry
Collector reachable over OTLP (or none, for the Prometheus fallback).

- [1. Getting started](#1-getting-started)
- [2. FastAPI](#2-fastapi)
- [3. Workers and background tasks](#3-workers-and-background-tasks)
- [4. gRPC](#4-grpc)
- [5. Logs](#5-logs)
- [6. Custom signals: spans, metrics, logs](#6-custom-signals-spans-metrics-logs)
- [7. Sampling](#7-sampling)
- [8. Debug mode](#8-debug-mode)
- [9. Extension modules (`staffops_otel_helper.ext`)](#9-extension-modules-staffops_otel_helperext)
- [10. Metrics without a Collector (Prometheus `/metrics`)](#10-metrics-without-a-collector-prometheus-metrics)
- [11. OTLP endpoint, protocol, TLS and exporter options](#11-otlp-endpoint-protocol-tls-and-exporter-options)
- [12. `TelemetryOptions` reference](#12-telemetryoptions-reference)
- [13. Lifecycle: setup, shutdown, SIGTERM, pre-fork servers](#13-lifecycle-setup-shutdown-sigterm-pre-fork-servers)
- [14. Validation and diagnostics](#14-validation-and-diagnostics)
- [Migrating from 0.2 to 0.3](#migrating-from-02-to-03)
- [Troubleshooting](#troubleshooting)
- [FAQ](#faq)

---

## 1. Getting started

```python
# Keep this import first: it must run before any opentelemetry.* import.
import staffops_otel_helper  # isort: skip

from staffops_otel_helper import setup_telemetry

setup_telemetry()
```

```bash
export OTEL_SERVICE_NAME=orders-api
export OTEL_RESOURCE_ATTRIBUTES=deployment.environment.name=DEV
export OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317
```

> **Warning — import order**: `staffops_otel_helper` must be imported before any other
> OpenTelemetry module. Unsupported `OTEL_PROPAGATORS` values (`b3`, `jaeger`,
> `xray`, `ottrace`) are then warned and skipped; if `opentelemetry.propagate`
> was imported first, the API has already parsed the variable and raises
> `ValueError` at import. isort sorts `opentelemetry` before `staffops_otel_helper`:
> mark the line `# isort: skip` (or configure `force_to_top = ["staffops_otel_helper"]`).

Call `setup_telemetry()` once, at startup, before creating the web app, gRPC
servers/channels or HTTP clients. Under a pre-fork server, call it in each
worker ([§13](#pre-fork-servers)).

---

## 2. FastAPI

```python
import staffops_otel_helper  # isort: skip

from staffops_otel_helper import setup_telemetry

setup_telemetry()

from fastapi import FastAPI  # imported after setup: the class is already patched

app = FastAPI()


@app.get("/orders/{order_id}")
async def get_order(order_id: str):
    return {"id": order_id}
```

`setup_telemetry()` patches the `fastapi.FastAPI` class, which affects apps
built afterwards. If `from fastapi import FastAPI` runs **before** setup (the
usual import order), the app is built from the unpatched class: call
`instrument_fastapi(app)` after creating it. It is idempotent.

```python
from fastapi import FastAPI
from staffops_otel_helper import setup_telemetry, instrument_fastapi

setup_telemetry()
app = FastAPI()
instrument_fastapi(app)
```

### Health checks

| Path | Server span | HTTP server metric |
|---|---|---|
| `/health /healthz /ready /readyz /live /livez /ping /metrics` | none (and no child spans under the default parent-based sampler) | **counted** in `http.server.request.duration` |
| anything else, e.g. `/users/ping` | traced | counted |

- Matching is exact, case-insensitive, with trailing slashes and the query
  ignored. A probe that sends a sampled `traceparent` is dropped too.
- Replace the list with `TelemetryOptions(health_paths=[...])`; `[]` traces
  every path. Paths must be ASCII without `?`, `#` or `,`.
- Probes count in the metrics so failing probes stay visible. Exclude them
  from SLI queries:
  `http_route!~"/health|/healthz|/ready.*|/live.*|/ping|/metrics"`.
- `OTEL_PYTHON_FASTAPI_EXCLUDED_URLS` / `OTEL_PYTHON_EXCLUDED_URLS` keep their
  upstream meaning: span **and** metric dropped.
- With an explicit `OTEL_TRACES_SAMPLER=always_on` or `traceidratio` (not
  parent-based), spans started inside a health handler are sampled again.

### Outbound HTTP (httpx, requests)

Calls to the same health paths (including your `health_paths`) are not traced
and not counted: upstream `excluded_urls` with anchored exact-path patterns,
merged with your `OTEL_PYTHON_HTTPX_EXCLUDED_URLS` /
`OTEL_PYTHON_REQUESTS_EXCLUDED_URLS`. httpx reads its list from the environment
only, so the helper sets it for the duration of setup and restores it: clients
instrumented later with `HTTPXClientInstrumentor.instrument_client()` do not get
the health exclusion. Every other call carries `traceparent` automatically.

---

## 3. Workers and background tasks

Each iteration of a loop should be its own trace. `start_root_span` starts a
span with no parent even when a span is current:

```python
import time

from staffops_otel_helper import setup_telemetry, get_tracer, start_root_span

setup_telemetry()
tracer = get_tracer("billing-worker")

while True:
    with start_root_span(tracer, "billing.process-batch") as span:
        span.set_attribute("batch.size", 100)
        process_items()
    time.sleep(60)
```

Without it, spans of every iteration end up in the trace that was current when
the loop started. It is the Python equivalent of .NET's `StartRootActivity`.
A worker is a plain process: handle SIGTERM so buffered telemetry is flushed
([§13](#shutdown-and-sigterm)).

---

## 4. gRPC

The helper instruments `grpc.aio` and the sync `grpc.server()`,
`grpc.insecure_channel()` and `grpc.secure_channel()`. No interceptors to pass:

```python
import grpc
from staffops_otel_helper import setup_telemetry

setup_telemetry()  # before creating servers or channels

# Client: traceparent is injected into the metadata
async with grpc.aio.insecure_channel("backend:50051") as channel:
    stub = MyServiceStub(channel)
    response = await stub.MyMethod(request)

# Server: the incoming context is extracted
server = grpc.aio.server()
add_MyServiceServicer_to_server(MyServicer(), server)
```

- The whole `grpc.health.v1.Health` service (Check, Watch, List) is not traced,
  on servers and clients. Spans your health servicer starts become roots.
- `OTEL_PYTHON_GRPC_EXCLUDED_SERVICES` works as upstream documents; the helper
  folds it into its health filter.
- The Python gRPC instrumentation emits **no RPC metrics** (spans only).
- Upstream limitation: the aio server interceptor supports server-streaming
  handlers written as async generators (`yield`) only; a traced handler using
  `await context.write(...)` fails. Exclude such services with
  `OTEL_PYTHON_GRPC_EXCLUDED_SERVICES`.

---

## 5. Logs

Use the standard `logging` module. Records emitted inside a span carry its
`trace_id` and `span_id`.

```python
import logging

logger = logging.getLogger(__name__)


def process_order(order_id: str):
    logger.info("processing order", extra={"order.id": order_id})
```

### Where records go

| Configuration | Application records | Helper/SDK diagnostics |
|---|---|---|
| Logs exporter active (OTLP endpoint for logs, `OTEL_LOGS_EXPORTER=otlp`) | exported via OTLP; the helper adds no console output | stderr |
| `OTEL_LOGS_EXPORTER=otlp,console` | OTLP and the SDK console exporter (stdout) | stderr |
| No logs exporter (no endpoint, `OTEL_LOGS_EXPORTER=none`) | single-line JSON on stdout while the app configures no root handler | stderr |
| `OTEL_SDK_DISABLED=true` | untouched by the helper (stdlib defaults) | stderr |

- **OTLP handler.** The helper uses the `opentelemetry-instrumentation-logging`
  handler on the root logger and on uvicorn's non-propagating `uvicorn`,
  `uvicorn.error` and `uvicorn.access` loggers; each record is exported once.
  uvicorn's own console handlers keep printing (change them with uvicorn's
  `--log-config` / `--no-access-log`).
- **Kept across your config.** `logging.basicConfig()`,
  `logging.config.dictConfig()` and `fileConfig()` normally drop or skip
  existing handlers; the helper wraps them during setup so its handler stays. A
  reference captured before setup (`from logging.config import dictConfig`)
  bypasses the wrapper.
- **Stdout fallback.** Installed as `logging.lastResort`, so a later
  `logging.basicConfig()` simply takes over. One JSON object per line:
  `time` (RFC 3339, milliseconds, `Z` in UTC), `level`
  (`DEBUG/INFO/WARN/ERROR`), `msg`, `logger`, your `extra=` fields and
  `exception` (traceback). It carries no trace ids — correlation needs a logs
  exporter. Same shape as the Go helper, plus `logger`.
- **Diagnostics.** `staffops_otel_helper.*` (INFO and above; the helper sets that logger
  to INFO when you left it unset) and `opentelemetry.*` (WARNING and above)
  always reach stderr until the app configures a root handler, including with
  a logs exporter active. `opentelemetry.*` records are never exported through
  OTLP (an export failure cannot feed itself); `staffops_otel_helper.*` records are.
- **OTel Logs API.** The helper calls `set_logger_provider()`, so code using
  `opentelemetry._logs` exports too.
- **Code attributes** (`code.file.path`, `code.function.name`,
  `code.line.number`) are off by default; set `OTEL_PYTHON_LOG_CODE_ATTRIBUTES=true`.
- **`opentelemetry-instrument` / `LoggingInstrumentor().instrument()`** add a
  second OTel handler to root. The helper removes it with a warning so each
  record is exported once; set `OTEL_PYTHON_LOG_AUTO_INSTRUMENTATION=false` to
  avoid the warning.

### Log level

The helper sets the root level only when root has no handlers and is still at
the stdlib default `WARNING`, and quiets framework loggers (`urllib3`, `grpc`,
…) only when they are unset. `shutdown_telemetry()` restores what it changed.

| `deployment.environment.name` | Level |
|---|---|
| `LOCAL` | DEBUG |
| `DEV`, `HML` | INFO |
| `PRD` | WARNING |
| unset or any other value | INFO |

`OTEL_HELPER_DEBUG_LEVEL=true` forces DEBUG; `TelemetryOptions(minimum_log_level=logging.INFO)`
overrides both.

---

## 6. Custom signals: spans, metrics, logs

Automatic instrumentation covers requests, RPCs and clients. Business
operations need custom signals. The complete runnable version of this section
is `example/python-custom-signals/custom_signals.py`,
with the output it produces in [example/README.md](example/README.md#custom-signals).
It uses the same names and trace shape as the Go example, so the two can be
compared side by side.

### Spans

```python
from opentelemetry.trace import Status, StatusCode
from staffops_otel_helper import get_tracer, start_root_span

tracer = get_tracer("orders")  # instrumentation scope name, not the service name


class PaymentDeclinedError(Exception):
    pass


with start_root_span(tracer, "orders.process-batch", attributes={"batch.size": len(orders)}) as batch:
    for order in orders:
        with tracer.start_as_current_span(  # child of the batch span
            "order.process", attributes={"order.id": order.id, "order.type": order.kind},
        ) as span:
            span.add_event("order.validated", {"order.amount": order.amount})
            try:
                charge(order)
            except PaymentDeclinedError as e:  # handled: record it yourself
                span.record_exception(e)       # exception.type = __main__.PaymentDeclinedError
                span.set_status(Status(StatusCode.ERROR, "payment declined"))
                batch.set_status(Status(StatusCode.ERROR, "an order failed"))
```

- `tracer.start_as_current_span()` makes the span current, so spans and log
  records created inside become its children / carry its ids.
- An exception that **leaves** `start_as_current_span()` is recorded as an
  `exception` event and sets status ERROR automatically. Handled errors need
  `record_exception()` + `set_status()`.
- Attribute values: `str`, `bool`, `int`, `float` or sequences of one type.
  Never put user ids, emails or request ids in metric attributes; on spans they
  are fine when they are not secrets.
- `start_root_span(tracer, name, **kwargs)` accepts the
  `start_as_current_span()` keyword arguments (`attributes`, `kind`, `links`).

### Metrics

```python
from opentelemetry.metrics import CallbackOptions, Observation
from staffops_otel_helper import get_meter

meter = get_meter("orders")

processed = meter.create_counter("order.processed", unit="{order}")
duration = meter.create_histogram(
    "order.duration", unit="s",
    explicit_bucket_boundaries_advisory=[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1],
)
active = meter.create_up_down_counter("order.active", unit="{order}")


def observe_queue(options: CallbackOptions):
    yield Observation(queue.qsize(), {"queue.name": "orders"})


meter.create_observable_gauge("order.queue.depth", callbacks=[observe_queue], unit="{order}")

attrs = {"order.type": "express"}  # on failure add {"error.type": type(e).__name__}
active.add(1, attrs)
processed.add(1, attrs)
duration.record(0.042, attrs)
active.add(-1, attrs)
```

| Instrument | Use for | Name on the helper's `/metrics` |
|---|---|---|
| Counter | things that only go up | `order_processed_total` |
| Histogram | durations, sizes | `order_duration_seconds_bucket` / `_sum` / `_count` |
| UpDownCounter | in-flight work, pool sizes | `order_active` |
| Observable gauge | a value read at export time (queue depth, temperature) | `order_queue_depth` |

The names column is the helper's Prometheus `/metrics` endpoint. Over OTLP the
stored name depends on the backend's OTLP naming settings (suffixes, dot
translation); check your backend before writing queries.

- Name instruments with dots and put the unit in `unit=`; do not add `_total`
  (the exporter adds it to counters).
- The SDK's default histogram buckets (`0, 5, 10, … 10000`) suit
  milliseconds; give second-based histograms `explicit_bucket_boundaries_advisory`.
- Measurements made inside a sampled span get a trace exemplar automatically.
- Keep attribute values bounded (enums, status classes); every distinct set is a
  new series.
- `OTEL_HELPER_DISABLED_METRICS=orders.debug.*` drops instruments from every
  exporter.

### Logs correlated with the trace

```python
import logging

logger = logging.getLogger("orders")

with tracer.start_as_current_span("order.process"):
    try:
        charge(order)
    except PaymentDeclinedError as e:
        # Carries this span's ids; exc_info adds exception.type/message/stacktrace.
        logger.error("order failed", exc_info=e, extra={"order.id": order.id})
```

The exported record has the `trace_id` and `span_id` of the span that is
current when it is emitted (no code needed), its `extra=` fields as attributes
and the level as severity. `exc_info` adds `exception.type` (the bare class
name, e.g. `PaymentDeclinedError`), `exception.message` and
`exception.stacktrace`. In Grafana, a derived field / "logs for this span"
link joins them. The stdout JSON fallback has no ids: correlation needs a logs
exporter.

---

## 7. Sampling

### Default: `parentbased_always_on`

Root spans are always sampled; a span with a parent follows the parent's
decision, so a child of a remote `traceparent` with `sampled=0` is not
recorded. Tail sampling happens at the Collector. The health filter
([§2](#health-checks)) wraps whatever sampler is configured.

### Head sampling (optional)

For volumes where you want to cut traffic before the Collector:

```bash
OTEL_TRACES_SAMPLER=parentbased_traceidratio
OTEL_TRACES_SAMPLER_ARG=0.1  # 10% of root traces
```

Or in code (any `sample_ratio`, 1.0 included, builds `ParentBased(TraceIdRatioBased(x))` and beats the env):

```python
setup_telemetry(TelemetryOptions(sample_ratio=0.5))
```

- Sampler names are the spec names, case-insensitive. `jaeger_remote`,
  `parentbased_jaeger_remote` and `xray` are not supported by the helper
  (warned, default used).
- `_ARG` and `OTEL_HELPER_SAMPLE_RATIO` accept plain decimals only (`0.25`;
  not `2.5e-1`, `.5`, `+0.5`): anything else warns and uses 1.0.
- `OTEL_HELPER_SAMPLE_RATIO` is deprecated and only used while
  `OTEL_TRACES_SAMPLER` is unset.

> **Warning**: Head sampling may drop error traces before the Collector sees them. Use it
> only when volume justifies it.

---

## 8. Debug mode

`OTEL_HELPER_DEBUG_LEVEL=true` (or `TelemetryOptions(debug_level=True)`):

- log level DEBUG;
- every extension instrumentor (SQL, Redis, AWS) is turned on, with the
  implicit-instrumentation deprecation warning;
- `debug=true` on **entry spans**: spans with no parent or a **remote** parent
  (server spans of a called service included). Spans with a local parent are
  not marked.

Debug mode comes from local configuration only: inbound tracestate or baggage
never sets the marker, and the helper writes no tracestate. A Collector
tail-sampling policy on `debug = "true"` keeps every such trace; combine it
(`and`) with an environment allow-list and a `rate_limiting` sub-policy
(design D11), because a debug service marks every inbound request.

Limit: under the default ParentBased sampler an entry span whose remote parent
is **unsampled** (`traceparent` flag `00`) is dropped before the marker runs —
debug mode cannot rescue a trace an upstream caller did not sample.

---

## 9. Extension modules (`staffops_otel_helper.ext`)

SQLAlchemy, Redis and botocore instrumentation are extras, not part of core:

```bash
# The wheel, not the PyPI name (not registered there; see CONSUMING.md)
pip install "staffops-otel-helper[sql] @ file://$(pwd)/staffops_otel_helper-<version>-py3-none-any.whl"   # or [redis], [aws], [all]
```

Call the `ext` functions **before** `setup_telemetry()`:

```python
from staffops_otel_helper import setup_telemetry
from staffops_otel_helper.ext.aws import instrument_aws
from staffops_otel_helper.ext.redis import instrument_redis
from staffops_otel_helper.ext.sql import instrument_sql

instrument_sql()        # all engines; or instrument_sql(engine=my_engine)
instrument_redis()
instrument_aws()        # boto3 / botocore

setup_telemetry()
```

| Function | Instruments | Returns |
|---|---|---|
| `staffops_otel_helper.ext.sql.instrument_sql(engine=None)` | SQLAlchemy | `True` active, `False` refused |
| `staffops_otel_helper.ext.redis.instrument_redis()` | redis-py | same |
| `staffops_otel_helper.ext.aws.instrument_aws()` | botocore (S3, SQS, DynamoDB, …) | same |

- `False` means the installed contrib refused the installed library version.
  One warning on the `staffops_otel_helper` logger names the instrumentor, the contrib
  version, the installed version and the supported range (also when
  `setup_telemetry()` auto-instruments).
- **SQLAlchemy 2.1 is not supported** by `opentelemetry-instrumentation-sqlalchemy`
  0.63b0–0.66b1 (`< 2.1.0`): pin `sqlalchemy<2.1`. 2.0.x works. Redis 8.x and
  botocore 1.43 work.
- An `ImportError` from these functions says to install the staffops-otel-helper wheel
  with the `[<extra>]` extra;
  if the extra is already installed, the missing package is the library itself
  (`sqlalchemy`, `redis`, `botocore`).
- **Database attributes:** with the helper's default
  `OTEL_SEMCONV_STABILITY_OPT_IN=http,database/dup`, SQL spans carry
  `db.system.name` and the legacy `db.system`, like Go's `ext/otelsql`. Your
  value wins (`database` → new keys only; `http` alone → legacy only). Known
  gap: Python copies the dialect name unmapped (SQL Server → `mssql`, Go
  emits `microsoft.sql_server`); filter on both in cross-language queries.

### Deprecated: `OTEL_HELPER_EXTRA_INSTRUMENTATION`

Still effective in 0.3 and removed in 1.0. It turns extension instrumentors on
from a comma list (`SQL`, `REDIS`, `AWS`; default `SQL`, so SQLAlchemy is
instrumented globally when the `[sql]` extra and SQLAlchemy are installed).
Setting it — even to an empty value, which turns the `SQL` default off — logs a
deprecation warning, and so does the implicit `SQL` default when it instruments
something. Migrate to the explicit `ext` calls above; setup then finds the
instrumentors active and logs nothing. Without the extra, activation is skipped
silently.

---

## 10. Metrics without a Collector (Prometheus `/metrics`)

With no metrics endpoint configured, metrics are served on
`http://<host>:9464/metrics` (`OTEL_HELPER_METRICS_PORT`). The registry is
dedicated: only OTel-produced series, no `process_*` / `python_*` and nothing
your code registers on `prometheus_client.REGISTRY`.

### Choosing exporters

| `OTEL_METRICS_EXPORTER` | Behavior |
|---|---|
| _(unset)_ | OTLP when a metrics endpoint is configured, else Prometheus |
| `otlp` | OTLP only. Without a metrics endpoint: warning and the unset behavior |
| `prometheus` | `/metrics` only, even when an OTLP endpoint is set |
| `otlp,prometheus` | both, from one `MeterProvider` (same values) |
| `console` | SDK console exporter (stdout) |
| `none` | metrics off |

`TelemetryOptions(metric_exporters=[...])` wins over the variable and over the
deprecated `OTEL_HELPER_DISABLED_SIGNALS` (in code, `["otlp"]` without an
endpoint, an unknown value — `logging` included, an alias in the variable only
— or `none` with other values raises; duplicates are dropped, so
`["none", "none"]` disables metrics). `OTEL_METRIC_EXPORT_INTERVAL` /
`export_interval_ms` sets the push interval (default 30000 ms): the variable
takes ASCII digits with an optional `+` (any number of digits; leading zeros ignored), > 0, else warned and 30 000; above 2 147 483 647 clamped with a warning; `export_interval_ms` outside [1, 2 147 483 647] raises.

### Multi-worker servers

The standalone listener binds one port per process: under `gunicorn -w 4` the
second worker cannot bind it, and each worker would expose only its own
metrics. Mount the endpoint on the application instead:

```python
from fastapi import FastAPI
from staffops_otel_helper import setup_telemetry, metrics_app, TelemetryOptions

setup_telemetry(TelemetryOptions(
    metric_exporters=["prometheus"],   # or ["otlp", "prometheus"]
    prometheus_metrics_port=0,          # no standalone listener
))
app = FastAPI()
app.add_route("/metrics", metrics_app())   # serves exactly /metrics (200)
```

Register it on the exact path, with `app.add_route("/metrics", metrics_app())`
or `Route("/metrics", metrics_app())` in a Starlette route list. Do not use
`app.mount("/metrics", ...)`: a mount answers `/metrics` with a redirect to
`/metrics/`, so a scraper or probe that does not follow redirects sees a
non-200 response.

If the port is busy and not disabled, `setup_telemetry()` raises a
`RuntimeError` that names `metrics_app()` as the fix (and installs nothing).
For per-process series behind one port, see `prometheus_client`'s
multiprocess mode.

### `metrics_app()` status codes

| State | Status |
|---|---|
| Before setup, or setup failed | 503 |
| Prometheus off by configuration (`OTEL_SDK_DISABLED=true`, metrics disabled, `OTEL_METRICS_EXPORTER` without `prometheus`) | 200, empty body (the target stays `up`) |
| Running with a Prometheus reader | 200, the OTel series |
| After `shutdown_telemetry()` | 503 |

A target scraped before setup or after shutdown reports `up=0`: scrape only
while the app is serving.

`OTEL_HELPER_DISABLED_METRICS` applies to every exporter; Views are per
provider, not per reader. To expose fewer series on `/metrics` than you push,
filter at scrape time (`metric_relabel_configs`).

---

## 11. OTLP endpoint, protocol, TLS and exporter options

The helper resolves the endpoint, the TLS decision (scheme versus
`*_INSECURE` conflicts), headers, timeout and compression itself, per signal,
and passes them to the exporters explicitly. Certificates
(`*_CERTIFICATE`, `*_CLIENT_CERTIFICATE`, `*_CLIENT_KEY`) are read by the SDK.

### Endpoints

| Where | Value | What is used |
|---|---|---|
| env `OTEL_EXPORTER_OTLP_ENDPOINT` | `http(s)://host:port[/prefix]` | as given; for `http/protobuf` `/v1/<signal>` is appended after the prefix |
| env `OTEL_EXPORTER_OTLP_<SIGNAL>_ENDPOINT` | full URL | **used as-is** (no path appended) |
| code `otel_endpoint` | URL | `/v1/<signal>` always appended for `http/protobuf` (a value already ending in `/v1/traces` becomes `/v1/traces/v1/traces`: use `traces_endpoint`) |
| code `traces_endpoint` / `metrics_endpoint` / `logs_endpoint` | full URL | used as-is |
| env or code, schemeless | `host[:port]` | `https://` (or `http://` with `INSECURE=true`), port 4317/4318; deprecated for `http/protobuf` |

- gRPC URL without a port → `:4317` + "set an explicit port" warning.
  `http/protobuf` URL without a port → 443 / 80.
- User info, query and fragment are stripped and warned (redacted); put
  credentials in `OTEL_EXPORTER_OTLP_HEADERS`, e.g.
  `Authorization=Basic%20<base64>`.
- `unix://`, `ftp://` or `scheme:/path` values leave the signal unconfigured.
- Values are split by RFC 3986 (the authority ends at `/`, `?` or `#`). An
  unparseable value is always shown as `<unparseable URL>` (never partly) and
  falls back; in code it raises. Unparseable: a control character or a
  backslash anywhere, an empty host (`http://:4317`, `http:///x`, `//h:4317`),
  a port outside 1–65535 (port `0` included), a space, `%` or IPv6 zone id in
  the host, a host byte outside the RFC 3986 reg-name set (`a{b`), a host
  ending in a number that is not a canonical dotted quad (`127.1`,
  `1.2.3.256`), a non-ASCII host that fails IDNA, and — for `http/protobuf` —
  an invalid `%` escape in the path (`/%zz`). Surrounding whitespace is trimmed.
- Hosts are lowercased; non-ASCII hosts are converted to punycode with UTS #46
  non-transitional processing and IDNA 2008 validation (the `idna` package):
  `bücher.de` → `xn--bcher-kva.de`, `straße.de` → `xn--strae-oqa.de`. IDNA 2008
  rejects some hosts Go and Node.js accept (emoji, `_` next to non-ASCII text,
  labels over 63 characters). Ports compare numerically, whatever their length
  (`04318` = 4318). Valid path escapes are kept verbatim (`/a%2Fb` is sent as
  `/a%2Fb`).
- An empty `?` or `#` is removed with the userinfo/query warning. An `@` (or
  `%40`) in the path or query is warned ("misplaced credentials?", variable name
  only) and the endpoint is used as written; diagnostics never show such a
  path. An opaque value (`unix:/…`) is shown as its scheme only (`unix:`).
- IPv6 literals work (`http://[::1]:4318`); zone ids (`[fe80::1%eth0]`) are
  invalid.
- Kubernetes: a host that ends in a number must be a canonical dotted quad, so
  a short name in an all-digit namespace (`collector.2024`) is **invalid** —
  use the FQDN (`collector.2024.svc.cluster.local`) or `collector.2024.svc`.
  One trailing `.` is fine (`10.0.0.1.`).
- With an `@` (or `%40`) in the path or query, diagnostics show only
  `<scheme>://` (`<schemeless endpoint>` for a schemeless value).
- An unparseable `OTEL_EXPORTER_OTLP[_<SIGNAL>]_INSECURE` warns even when no
  endpoint is configured. An empty code `otlp_protocol` or `compression` is
  "not set": the environment, then the default, applies.

### Protocol

Code `otlp_protocol` > `OTEL_EXPORTER_OTLP_<SIGNAL>_PROTOCOL` >
`OTEL_EXPORTER_OTLP_PROTOCOL` > port `4318` on that signal's endpoint →
`http/protobuf` (logged at INFO) > `grpc`. `http/json` has no exporter here:
from the environment it is warned and ignored, in code it raises.

### TLS

| Endpoint | `INSECURE` | Transport |
|---|---|---|
| `https://…` | any | TLS (a `true` is warned) |
| `http://…` | any | plaintext (a `false` is warned) |
| schemeless | unset / not `true` | TLS |
| schemeless | `true` | plaintext |

`OTEL_EXPORTER_OTLP[_<SIGNAL>]_INSECURE` and `TelemetryOptions(insecure=...)`
only decide for schemeless endpoints. In code, `insecure` contradicting the
code URL's scheme raises. `OTEL_EXPORTER_OTLP_CERTIFICATE` (and per signal)
sets the CA; with a plaintext endpoint it is warned and ignored.

### Timeout, compression, headers

| Setting | Rule |
|---|---|
| `*_TIMEOUT` | Milliseconds, plain decimal (`500`, `2500.5`; not `1e3`, `.5`, `+5`), clamped to [1 ms, 24 h] with a warning. Below 100 warns "looks like seconds". Code: `export_timeout_ms` (default `None` = env, else SDK 10 s) |
| `*_COMPRESSION` | `gzip` or `none`, case-insensitive; anything else (`deflate`) warns and uses `gzip`. Default `gzip`. Code: `compression` |
| `*_HEADERS` | `k1=v1,k2=v2`; keys `[A-Za-z0-9._-]+`, lowercased, last wins; values percent-decoded and trimmed. A malformed item or invalid key is skipped; an invalid `%` escape keeps the whole value literal; a decoded control character skips the item. Warnings name the position only |
| Fallback order | invalid per-signal value → warning → generic value → default |

---

## 12. `TelemetryOptions` reference

`None` / empty means "not set in code"; the environment then decides.

| Field | Type, default | Meaning |
|---|---|---|
| `service_name` | `str \| None = None` | `service.name` |
| `environment` | `DeploymentEnvironment \| str \| None = None` | `deployment.environment.name`; any string is emitted verbatim |
| `resource_attributes` | `dict = {}` | Extra resource attributes; code wins per key over `OTEL_RESOURCE_ATTRIBUTES` ([resource attributes](#resource-attributes)) |
| `otel_endpoint` | `str = ""` | Generic endpoint (`/v1/<signal>` appended for HTTP) |
| `traces_endpoint`, `metrics_endpoint`, `logs_endpoint` | `str = ""` | Per-signal endpoints, used as-is |
| `otlp_protocol` | `str \| None = None` | `grpc` or `http/protobuf` |
| `insecure` | `bool \| None = None` | Plaintext for a schemeless code endpoint |
| `export_timeout_ms` | `int \| None = None` | Export timeout (ms) |
| `compression` | `str \| None = None` | `gzip` or `none` |
| `metric_exporters` | `list[str] \| tuple \| None = None` | `otlp`, `prometheus`, `console`, `none` (trimmed, lowercased, de-duplicated) |
| `export_interval_ms` | `int \| None = None` | Metric export interval in [1, 2 147 483 647] ms (default 30000) |
| `prometheus_metrics_port` | `int \| None = None` (→ env, then 9464) | Standalone `/metrics` port; `0` disables it |
| `disabled_metrics` | `list[str] \| tuple \| None = None` (→ env, then none; `[]` = none, beats env) | Wildcard instrument names to drop |
| `disabled_signals` | `list[str] \| tuple \| None = None` (→ env, then none; `[]` beats env) | `traces`, `metrics`, `logs`, trimmed, lowercased, de-duplicated (prefer `OTEL_*_EXPORTER=none`) |
| `sample_ratio` | `float \| None = None` | Any code value (1.0 included) → `parentbased_traceidratio`, beating `OTEL_TRACES_SAMPLER`; `None` → env, then `parentbased_always_on` |
| `health_paths` | `list[str] \| None = None` | HTTP health paths; `None` = `DEFAULT_HEALTH_PATHS`, `[]` = none |
| `debug_level` | `bool \| int \| None = None` (→ env, then off; `False` beats env; strings fail) | Debug mode ([§8](#8-debug-mode)) |
| `minimum_log_level` | `int \| None = None` | Overrides the environment log level |
| `extra_instrumentation` | `str \| None = None` (→ env, then `SQL`) | Deprecated mechanism ([§9](#deprecated-otel_helper_extra_instrumentation)) |

After `setup_telemetry()`, `service_name` and `environment` on the returned
options hold what was resolved. Every other field above keeps **only the value
set in code** (`None` if unset), so copying the object with
`dataclasses.replace()` or passing it to a later setup re-reads the environment
for those fields — **except `service_name` and `environment`**, which stay as
resolved and then count as code values; read effective values from the read-only `options.resolved` view (e.g.
`options.resolved.sample_ratio`, `options.resolved.prometheus_metrics_port`).
`options.resolved.metric_exporters` is the **configured** list (code, else
`OTEL_METRICS_EXPORTER`; `None` when inferred from the endpoint): with
`OTEL_METRICS_EXPORTER=none` it shows `['none']`. Node's
`Resolved.metricExporters` and the parity snapshot of every language show the
**effective** list instead (`[]` there, and also when metrics or the SDK are
disabled); Go exposes no public equivalent. It is not what runs: the effective exporters are `options.resolved_metric_exporters()`, and
none run when `options.is_signal_enabled("metrics")` is false or
`OTEL_SDK_DISABLED=true`.
`service_name` is `None` when nothing configured one, and `environment` is
`None` when unset or a plain `str` for values outside LOCAL/DEV/HML/PRD (code
reading `options.environment.value` must handle both).
`options.is_signal_enabled("traces")` reports whether a signal is on.
`resolve_insecure()` was removed in 0.3.

### Resource attributes

The helper builds the resource itself (never `Resource.create()`), lowest to
highest precedence: `OTEL_RESOURCE_ATTRIBUTES` < `resource_attributes` <
the resolved identity < the SDK's `telemetry.sdk.*`.

| Attribute | Order (a blank value — empty or whitespace — at any level counts as unset) |
|---|---|
| `service.name` | `service_name=` > `resource_attributes["service.name"]` > `OTEL_SERVICE_NAME` > `OTEL_RESOURCE_ATTRIBUTES` > `SERVICE_NAME` (deprecated) > `unknown_service:<interpreter>` + one warning |
| `deployment.environment.name` | `environment=` > `resource_attributes[...]` > `OTEL_RESOURCE_ATTRIBUTES` > `ENVIRONMENT` (deprecated) > absent + one warning |
| `service.instance.id` | `resource_attributes[...]` > `OTEL_RESOURCE_ATTRIBUTES` > the SDK's per-process id (SDK ≥ 1.45) > a helper UUID; kept across a fork (SDK ≥ 1.45) |
| `telemetry.sdk.*` | Always the SDK's. Env values are ignored silently; a code value is ignored with one warning naming the key |

```python
setup_telemetry(TelemetryOptions(
    service_name="orders-api",                  # beats resource_attributes["service.name"]
    resource_attributes={"team.name": "payments", "service.namespace": "shop"},
))
```

- `OTEL_RESOURCE_ATTRIBUTES` is parsed by the helper: split on `,` (blank
  items ignored), each item at its first `=`, values percent-decoded then
  trimmed. An item without `=`, with an empty or non-UTF-8 key, or a value
  with a bad `%` escape is reported **by position only**, once per process;
  the bad-escape value is kept literally. Values are never printed.
- In code, an empty key, a non-string key, or a non-string `service.name` /
  `deployment.environment.name` / `service.instance.id` raises `ValueError`
  in `setup_telemetry()`.
- The resource declares `schema_url = https://opentelemetry.io/schemas/1.43.0`.
- `OTEL_EXPERIMENTAL_RESOURCE_DETECTORS` is ignored (one warning): add
  host/process/cloud attributes in the Collector (`resourcedetection`,
  `k8sattributes`).

---

## 13. Lifecycle: setup, shutdown, SIGTERM, pre-fork servers

### Setup

- **Idempotent.** The first successful call wins; later calls return its
  resolved options and ignore their argument.
- **All-or-nothing.** Every signal is built before anything global is
  installed. If building fails (exporter error, `/metrics` port in use, an
  instrumentation error), what was built is shut down, instrumentors are
  undone, the previous propagator and log levels stay, and a later call can
  retry.

### Shutdown and SIGTERM

`shutdown_telemetry(timeout_ms=30000)` flushes and shuts down the trace, metric
and log providers concurrently under one deadline, undoes the instrumentation,
removes the helper's log handler (restoring log levels) and stops the
standalone `/metrics` listener. The first call does the work; later and
concurrent calls get the same result — `None` or the same `RuntimeError` —
until the next setup. It is also registered with `atexit`.

**Python skips `atexit` on an unhandled SIGTERM** (`docker stop`, a Kubernetes
rollout): buffered spans and logs (up to the batch delay) and the last metric
interval are lost. The helper installs no signal handler; the application owns
its signals. uvicorn and gunicorn turn SIGTERM into a lifespan shutdown, so call
`shutdown_telemetry()` there:

```python
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from staffops_otel_helper import setup_telemetry, shutdown_telemetry, instrument_fastapi

setup_telemetry()


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    try:
        shutdown_telemetry(timeout_ms=5_000)  # inside docker stop's 10 s grace period
    except RuntimeError as e:  # a provider failed or hit the deadline
        logging.getLogger(__name__).warning("telemetry shutdown incomplete: %s", e)


app = instrument_fastapi(FastAPI(lifespan=lifespan))
```

A plain script or worker must handle SIGTERM itself (Python's default handler
kills the process without running `finally` blocks or `atexit`). In an asyncio
program, as `example/python-process/workers.py` and
`example/python-backend/server.py` do, turn the signal into an event:

```python
import asyncio, contextlib, signal

async def main():
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):   # before starting servers/workers
        loop.add_signal_handler(sig, stop.set)
    workers = asyncio.gather(worker_a(), worker_b())
    await asyncio.wait({workers, asyncio.ensure_future(stop.wait())},
                       return_when=asyncio.FIRST_COMPLETED)
    workers.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await workers   # re-raises a worker's own exception

try:
    asyncio.run(main())
finally:
    shutdown_telemetry(timeout_ms=5_000)  # inside docker stop's 10 s grace period
```

A synchronous job sets a flag that its loop checks, as
`example/python-custom-signals` does:
`signal.signal(signal.SIGTERM, lambda *_: stop.set())` with a
`threading.Event`, then `shutdown_telemetry()` in a `finally`.

### Setup after shutdown (Python limitation)

The OpenTelemetry Python API sets the global Tracer/MeterProvider once per
process. A `setup_telemetry()` after `shutdown_telemetry()` re-enables logs and
propagators but **not traces or metrics**: a warning says so and
`options.is_signal_enabled("traces")` / `("metrics")` return `False`. The Go
helper rebuilds all three. Restart the process to reconfigure traces/metrics.

### Pre-fork servers

gunicorn (also with `--preload`), uWSGI and similar servers fork workers. Call `setup_telemetry()` **only in each worker**, after the fork — never in the
preloaded module. Setup in the parent is inherited as-is (on sdk 1.42–1.44 with
one shared `service.instance.id`), and the second call in a worker is a no-op.

```python
# gunicorn.conf.py
def post_fork(server, worker):
    from staffops_otel_helper import setup_telemetry
    setup_telemetry()
```

uWSGI: decorate the same call with `@uwsgidecorators.postfork`. A worker forked
after setup ran in the parent logs a warning on its next helper call. Do not set
`service.instance.id` yourself in multi-worker servers: every worker would
report the same id (warned when the helper can detect the fork). Pod identity
belongs in `k8s.pod.uid`, which the Collector adds.

---

## 14. Validation and diagnostics

| Input | On error |
|---|---|
| Value in `TelemetryOptions` (wrong type, `sample_ratio` outside [0, 1] or NaN, port outside [0, 65535], unknown signal name, `insecure` vs scheme, `http/json`) | `ValueError` naming the field |
| Environment variable | warning on an `staffops_otel_helper.*` logger naming the variable, fallback to the next level |
| Deprecated variable | one warning per process naming the replacement |

Messages start with `[OtelHelper]` and never print `*_HEADERS`, `*_CLIENT_KEY`
or URL credentials. Call `setup_telemetry()` where a startup failure is
visible (application startup, `post_fork`), not lazily inside a request.

---

## Migrating from 0.2 to 0.3

| Area | 0.2.0 | 0.3.0 | Action |
|---|---|---|---|
| HTTP metrics | `http.server.duration` (ms; `http.method`, `http.target`) | `http.server.request.duration` (s; `http.request.method`, `http.route`, `url.scheme`); same for the client | update dashboards/alerts, or `OTEL_SEMCONV_STABILITY_OPT_IN=http/dup` during the transition |
| DB span attributes | `db.system` | `db.system.name` and `db.system` | none |
| Service name | `SERVICE_NAME` beat `OTEL_SERVICE_NAME`; default `my-service` | `OTEL_SERVICE_NAME` > `OTEL_RESOURCE_ATTRIBUTES` > `SERVICE_NAME` (deprecated); unset → `unknown_service` + warning | set `OTEL_SERVICE_NAME` |
| Environment | default `LOCAL`; unknown → `LOCAL` | unset → attribute absent, log level INFO; unknown emitted verbatim; `ENVIRONMENT` deprecated | `OTEL_RESOURCE_ATTRIBUTES=deployment.environment.name=<env>` |
| Env endpoint | path dropped, port defaulted to 4317 | used as given (path prefix kept; HTTP without port → 443/80); per-signal endpoints honored | check endpoints with a path or without a port |
| Code `otel_endpoint` ending in `/v1/traces` | kept | `/v1/<signal>` appended again | use `traces_endpoint` |
| Timeout env | read as seconds by the SDK | milliseconds; `10` = 10 ms (warned) | `OTEL_EXPORTER_OTLP_TIMEOUT=10000` |
| `export_timeout_ms` | always 10000 passed | `None` (env, else SDK 10 s) | none |
| Compression | none | `gzip` default | `OTEL_EXPORTER_OTLP_COMPRESSION=none` to opt out |
| Credentials in the URL | `https://user:pass@host` | stripped + warning | `OTEL_EXPORTER_OTLP_HEADERS=Authorization=Basic%20<base64>` |
| Header keys | any key | `[A-Za-z0-9._-]+` only (`x+y` skipped), lowercased | rename keys |
| Sampler | `AlwaysOn` / bare `TraceIdRatioBased` | `ParentBased(...)`: unsampled remote parents are honored | none, or set a non-parent-based sampler explicitly |
| Health probes | `excluded_urls`: no span, no metric; `/users/ping` also dropped | no span, **counted** in HTTP metrics; exact match | exclude health routes in SLI queries |
| `HEALTH_PATHS` | `staffops_otel_helper.tracing.HEALTH_PATHS` | removed | `staffops_otel_helper.DEFAULT_HEALTH_PATHS` |
| Logs without exporter | SDK handler with no processor (records lost) | no OTel handler; JSON on stdout while the app configures none | none |
| `code.*` log attributes | always added | off | `OTEL_PYTHON_LOG_CODE_ATTRIBUTES=true` |
| Root log level | always set | set only when root is unconfigured | none |
| `/metrics` | global registry (`process_*`, `python_*`, your metrics); always 200 | dedicated registry; 503 before setup / after shutdown | serve your own registry yourself; scrape while serving |
| Second `setup_telemetry()` | returned its own options | returns the first resolved options | none |
| Shutdown | none (provider `atexit` hooks) | `shutdown_telemetry()`; handle SIGTERM | add it to your lifespan / signal handler |
| `ext.instrument_*()` | returned `None` | return `bool` | check the result if you need to |
| `resolve_insecure()` | public | removed | TLS follows the scheme |

TLS (`INSECURE`) before and after, gRPC:

| Endpoint | `OTEL_EXPORTER_OTLP_INSECURE` | 0.2.0 | 0.3.0 |
|---|---|---|---|
| `http://collector:4317` | unset / `true` | plaintext | plaintext |
| `http://collector:4317` | `false`, `yes` | TLS | plaintext + warning |
| `https://collector:4317` | `true` | plaintext | TLS + warning |
| `https://collector:4317` | unset / `false` | TLS | TLS |
| `collector:4317` | `true` | plaintext | plaintext |
| `collector:4317` | unset / `yes` | TLS | TLS (`yes` warned) |

For `http/protobuf` the scheme already decided in 0.2. Dependencies are now
bounded: OpenTelemetry `>=1.42,<1.46`, contrib `>=0.63b0,<0.67b0`, plus the new
core dependency `opentelemetry-instrumentation-logging`.

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `ValueError: ... propagator` at import | `opentelemetry.propagate` imported before `staffops_otel_helper` with `OTEL_PROPAGATORS=b3` (or another unsupported value): import `staffops_otel_helper` first (`# isort: skip`) |
| Service shows as `unknown_service` | no `OTEL_SERVICE_NAME`; the warning on stderr says so |
| Nothing reaches the Collector, no error | gRPC to an `http(s)://` URL without a port uses 4317; `http/protobuf` uses 443/80. Check the startup warnings on stderr; check `OTEL_*_EXPORTER=none` |
| Traces exported, metrics on `:9464`, logs on stdout | only `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` is set: "configured" is per signal |
| Export timeouts after upgrading | `*_TIMEOUT` is milliseconds now (`10` = 10 ms) |
| `Address already in use` on 9464 | multi-worker server: `prometheus_metrics_port=0` + `metrics_app()` ([§10](#multi-worker-servers)) |
| Scrape target `up=0` | `metrics_app()` answers 503 before setup and after shutdown |
| FastAPI requests not traced | `FastAPI` imported before setup: `instrument_fastapi(app)` |
| No HTTP metrics named `http_server_duration_*` | stable semconv: `http_server_request_duration_seconds` |
| Logs printed twice / not exported | `opentelemetry-instrument` added a second handler (removed with a warning): `OTEL_PYTHON_LOG_AUTO_INSTRUMENTATION=false`. With no logs exporter, records go to stdout only |
| Spans and logs lost on `docker stop` | unhandled SIGTERM skips `atexit` ([§13](#shutdown-and-sigterm)) |
| Traces/metrics missing after a re-setup in tests | setup after shutdown restores logs only ([§13](#setup-after-shutdown-python-limitation)) |
| `instrument_sql()` returns `False` | SQLAlchemy 2.1: pin `sqlalchemy<2.1` |
| `... only instruments "httpx >= 0.18.0", but no installed version was found` on stderr | upstream contrib message for an always-on instrumentor whose library is not installed; harmless |
| Same `service.instance.id` in every worker | setup ran before the fork: call it in `post_fork` |

---

## FAQ

**Do I need to pass interceptors for gRPC?** No. Call `setup_telemetry()`
before creating channels and servers.

**Do I need to configure sampling?** No. The default is
`parentbased_always_on`; sampling decisions belong to the Collector.

**Do I set `k8s.pod.name` and other resource attributes?** No. The Collector's
`k8sattributes` processor adds them.

**How are logs correlated with traces?** Automatically, for records emitted
inside a span while a logs exporter is active ([§6](#logs-correlated-with-the-trace)).

**Can I call `setup_telemetry()` more than once?** Yes; only the first
successful call takes effect ([§13](#setup)).

**Which Python versions?** 3.10 and newer; CI runs 3.10, 3.11 and 3.13 against
both the lowest and the newest allowed OpenTelemetry versions.
