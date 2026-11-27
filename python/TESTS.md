# Tests — staffops-otel-helper (Python)

How to run and extend the Python test suite. Audience: contributors changing
`python/staffops_otel_helper/`. Everything runs in Docker; no local Python is needed.

The suite has four layers:

| Layer | What it proves | Where |
|---|---|---|
| Unit tests | resolution rules, shims, lifecycle, logging, health exclusion, packaging | `python/tests/test_*.py` |
| Parity fixture | Go and Python resolve the same inputs to the same outputs (ADR-008) | `parity/cases.json`, run by `tests/test_parity.py` |
| Dependency matrix | the lower and upper ends of the declared OpenTelemetry ranges both work | `python/ci/lower.txt`, `python/ci/latest.txt` |
| e2e smoke | real exports reach a real Collector over gRPC, HTTP with a path prefix, a per-signal endpoint and TLS | `testing/e2e/` |

CI (`.github/workflows/ci.yml`) runs all four; the coverage gate is **≥ 90%
line coverage**, parity child processes included.

## Run the unit and parity tests

From the repository root:

```bash
docker compose -f testing/docker-compose.yml run --rm python-tests
```

This starts the fake OTLP receiver (`fakeotlp-py`), copies `python/` out of the
read-only mount, installs `.[dev,all]` and runs:

```bash
python -m pytest tests/ -p no:cacheprovider -q --cov=staffops_otel_helper --cov=otel_helper --cov-report=term --cov-fail-under=90
```

The receiver records what each `export` parity case sent (destination, protocol,
TLS, path, compression, timeout, headers — never payloads). Without it,
`PARITY_REQUIRE_FAKEOTLP=1` makes those cases fail instead of skipping, so
always use the compose service rather than a bare `docker run`.

Stop the receiver afterwards:

```bash
docker compose -f testing/docker-compose.yml stop fakeotlp-py
```

### Run a subset

```bash
docker compose -f testing/docker-compose.yml run --rm python-tests sh -ec '
  cp -r /src/python /work && cd /work
  pip install -q -e ".[dev,all]"
  python -m pytest tests/test_shims.py -k timeout -q'
```

## Lower and latest dependency legs

`PY_VERSION` picks the interpreter (default `3.13`); `PY_DEPS` picks the
constraints file (default `latest`):

```bash
PY_VERSION=3.10 PY_DEPS=lower  docker compose -f testing/docker-compose.yml run --rm python-tests
PY_VERSION=3.11 PY_DEPS=latest docker compose -f testing/docker-compose.yml run --rm python-tests
```

| File | Pins |
|---|---|
| `python/ci/lower.txt` | the oldest declared versions: OpenTelemetry 1.42.0, contrib 0.63b0, `protobuf` 5.26.0, `prometheus-client` 0.20.0, SQLAlchemy 2.0.0, version-dependent `grpcio` floors |
| `python/ci/latest.txt` | nothing: the newest versions allowed by the caps in `pyproject.toml` (`<1.46`, `<0.67b0`) |

CI runs Python 3.10, 3.11 and 3.13 against both files. Raising a cap is a pull
request that runs the `latest` leg.

## Parity fixture

`tests/test_parity.py` runs one test per case in `parity/cases.json`, each in a
fresh process (`tests/parity_driver.py`) so global OpenTelemetry state never
leaks between cases. Status `pass` must pass; `xfail:<task>` must fail on a
behavior mismatch (an unexpected pass fails the suite); `skip:<reason>` is
skipped. Never delete a case to make the suite pass; mark it `xfail` with the
task that will fix it. Format and adapter rules: `parity/README.md`.

## e2e smoke against a real Collector

```bash
testing/e2e/smoke.sh
```

The script starts `otel/opentelemetry-collector-contrib` (debug exporter,
config `testing/e2e/collector.yaml`), runs `example/python-process` once per
combination in `testing/e2e/combos.sh` (gRPC, http/protobuf with an `/otlp`
prefix, a per-signal traces endpoint, TLS with a generated CA), then checks that
every signal reached the expected receiver with the combination's
`service.name`. Each receiver tags its data with the resource attribute
`e2e.receiver`. Output: one `OK` / `MISS` / `LEAK` / `XFAIL` / `XPASS` line per
expectation; any `MISS`, `LEAK` or `XPASS` fails.

The custom-signals example runs against the same Collector; see
[example/README.md](example/README.md#custom-signals).

## Test modules

| Module | Covers |
|---|---|
| `test_config.py` | resolution contract (design D1): service name and environment precedence, env values that warn and fall back, `OTEL_*_EXPORTER`, per-signal "OTLP configured", protocol precedence, code validation, URL redaction |
| `test_exemplar_filter.py` | A33: `OTEL_METRICS_EXEMPLAR_FILTER` known values (contract trim, ASCII case), an unknown value warns once and uses `trace_based` (NBSP, 64-character cut), the warning also with `OTEL_SDK_DISABLED`, the resolved filter reaches the helper `MeterProvider` and decides which exemplars are recorded |
| `test_shims.py` | what the helper passes to the SDK exporters: absolute vs code endpoints, schemeless, portless gRPC, TLS conflicts, timeout ms → s, gzip default, headers, semconv default, `OTEL_PROPAGATORS`, `service.instance.id`, diagnostics channel |
| `test_endpoint_a17.py` | A17 endpoint rulings: RFC 3986 split, port range and leading zeros, host characters and zone ids, IDN punycode, `<unparseable URL>` rendering, `@` in the path, empty query/fragment, INSECURE without an endpoint, empty code `otlp_protocol` / `compression` |
| `test_resource_a16.py` | A16 resource rulings: identity precedence, `OTEL_RESOURCE_ATTRIBUTES` parsing and warnings, `telemetry.sdk.*`, schema URL, `service.instance.id` |
| `test_a18.py` | A18 rulings: TLS files, export deadline, code timeout bound, header bytes (`-bin` keys as bytes), value-length limit 0, batch size above queue size |
| `test_a19.py` | A19 rulings: `OTEL_HELPER_DISABLED_SIGNALS` vs a set standard variable and `metric_exporters`, unknown entries warned once, code list normalization (`["none","none"]`), interval grammar / clamp / code bound (any number of digits), `OTEL_CONFIG_FILE` and `OTEL_EXPERIMENTAL_CONFIG_FILE`, echoed-value truncation |
| `test_sampler_precedence.py` | sampler precedence and decisions (ADR-002) |
| `test_health_a7.py` | health sampler, `health_paths`, `instrument_fastapi`, sync gRPC, client exclusion |
| `test_logging_a5.py` | handler selection, stdout JSON fallback, stderr diagnostics, uvicorn loggers, duplicate handler removal, level rules |
| `test_lifecycle.py` | all-or-nothing setup, idempotence, `shutdown_telemetry()` result and concurrency, setup after shutdown, fork warning |
| `test_metrics_contract.py` | `OTEL_METRICS_EXPORTER` / interval contract, `metrics_app()` status table, dedicated registry |
| `test_a9_deprecations_debug.py` | deprecation warnings, debug marker on entry spans |
| `test_a10_unsupported_instrumentation.py` | unsupported-version warning, `ext` return values |
| `test_a10_db_semconv.py` | `db.system.name` + `db.system` with real SQLAlchemy |
| `test_ext.py` | `staffops_otel_helper.ext` import paths and `ImportError` hints |
| `test_otlp_protocol.py` | exporter selection per protocol |
| `test_propagation.py` | W3C propagation through the global propagator |
| `test_signal_integration.py` | spans created and exported (in-memory exporter) |
| `test_setup.py`, `test_features.py` | setup basics, helpers, `start_root_span`, disabled signals |
| `test_a37_exception_detail.py` | A37 (ADR-014 decision 4, design D15 amendment 2026-11-19): scope rule and `HelperTracerProvider`; client spans (requests, httpx, ASGI INTERNAL) type-only event once per object, no description, no record; server spans one record with the span ids, `error.type` and `http.*` keys (never URL keys), description-only and gRPC abort bodies, cancellation, unsampled spans through `use_span` and through `record_exception`/`end`; real FastAPI 500 sampled and unsampled, contrib's own 500 path (the description is overwritten), requests and httpx failures, sync and aio gRPC raise and abort (one `AbortError` event, one record) sampled and unsampled; review round: records only for failed server spans (gRPC code table, NOT_FOUND none, a handled exception with 200 / 404 none, FastAPI `/handled` and `HTTPException(404)` none), the same `error.type` sampled and unsampled, the `use_span` hook warnings, internal failures warned by type |
| `test_a45_failure.py` | A45 failure rule (R11, D15): `traced` (context manager, sync and `async def` decorators), `record_failure`, `start_root_span`; ERROR without description, `error.type` and override, no event; one record with the span ids and the three attributes; cancellation (incl. gRPC `CANCELLED` and exception groups) and timeouts; one record per span (3 nested spans with built-in and dataclass exceptions); one `traced` object shared by 2 asyncio tasks and 2 threads; failure logger level vs the diagnostics level, `propagate=False`; unsampled and non-current spans; non-`str` name, forbidden kwargs, generator functions, awaitable results; routing through the stdout fallback, `dictConfig` with and without an exporter, before setup; `exception.stacktrace` without the helper frames (with block, sync and async decorators, the propagating traceback unchanged, a traceback of helper frames only kept, module-name match) |
| `test_d16_error_type.py` | D16 / R12 protocol span error attributes (A44, A46). SQL: the `sql_error_code` table on fake driver exceptions (psycopg2 `pgcode` and `diag.sqlstate`, psycopg 3 `sqlstate` and `diag`, asyncpg through `__cause__`, an application code `P0001`, pymysql 1062 / 1064 and the client range 2003 / 2013, range edges, `OSError` candidates (aiomysql 2013 from `ConnectionResetError(104)`, `OSError(32)`, a bare `ConnectionRefusedError`), `EPIPE` and other bad shapes, a raising property, other systems); the end hook (both system keys, the description guard on `sys.exc_info()`, `record_exception` on `connect`, values already on the span kept (a preset `db.response.status_code` becomes `error.type`), Unset/OK untouched, never raises); real SQLite in a subprocess (`_OTHER`, no `db.response.status_code`, no description, a value set on the open span kept, the failed `connect` span, the file-path `db.namespace`/`db.name` and span-name residual). gRPC: the name table (16 names, 0 / 17 / bool / str → `_OTHER`), the end hook on spans (code 0 or none on ERROR → `_OTHER`, preset kept), the sync-client `wrapper_fn` bind / unbind, its warning on another contrib layout, the unbind after `reset_telemetry()` and after a refused or raising `instrument()`, and a seam test on contrib's `wrapper_fn` source; an in-process grpcio server through `setup_telemetry()`: sync unary (client FAILED_PRECONDITION / ALREADY_EXISTS / NOT_FOUND with the SERVER span Unset, INTERNAL on both, OK, a handler-set value kept), `.future()`, `with_call`, unary-stream, stream-unary, one bidi call, aio client + aio server, and the documented SERVER `UNKNOWN` gap (a raising handler) |
| `test_packaging.py` | `py.typed` shipped, no `opentelemetry.*._internal` imports |
| `test_import_compat.py` | the deprecated `otel_helper` import (ADR-011): one `DeprecationWarning`, same objects and submodules as `staffops_otel_helper` |
| `test_parity.py` | the shared fixture. D16 (2026-11-18, A46): `parity_driver.py` answers `input.response_status` from the FastAPI handler, adds FailedPrecondition and Internal to the grpc.aio Echo service and reports `server_span_error_type` / `client_span_error_type`; the four cases `http.server_5xx_error_type`, `grpc.client_failed_precondition_error_type`, `grpc.server_internal_error_type`, `grpc.server_not_found_no_error_type` pass (opentelemetry-sdk 1.45.2, instrumentation-grpc 0.66b2, `ci/latest.txt`, python:3.13-slim). Review round (2026-11-18): the negative cases `http.server_4xx_no_error_type` and `grpc.server_ok_no_error_type` pass; a non-string `error.type` is reported as `<non-string:T>`; `TestHarness.test_response_status_is_an_integer_literal_200_to_599`; without `response_status` the handler is the previous one (returns `"ok"`) |

## Writing tests

- Tests ship with the change and keep coverage at or above 90%.
- Behavior that depends on root logger state or process globals runs in a
  fresh interpreter (see `_run` in `test_logging_a5.py`); the `conftest.py` fixture resets the OpenTelemetry
  globals between in-process tests.
- Assert on public behavior (decisions, emitted attributes, warnings), not on
  private SDK attributes.
- Wire behavior (endpoints, TLS, compression, timeouts) belongs in a parity
  `export` case, checked by the fake receiver.
