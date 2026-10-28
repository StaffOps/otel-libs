# Tests — otel-helper (Python)

How to run and extend the Python test suite. Audience: contributors changing
`python/otel_helper/`. Everything runs in Docker; no local Python is needed.

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
python -m pytest tests/ -p no:cacheprovider -q --cov=otel_helper --cov-report=term --cov-fail-under=90
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
| `test_shims.py` | what the helper passes to the SDK exporters: absolute vs code endpoints, schemeless, portless gRPC, TLS conflicts, timeout ms → s, gzip default, headers, semconv default, `OTEL_PROPAGATORS`, `service.instance.id`, diagnostics channel |
| `test_sampler_precedence.py` | sampler precedence and decisions (ADR-002) |
| `test_health_a7.py` | health sampler, `health_paths`, `instrument_fastapi`, sync gRPC, client exclusion |
| `test_logging_a5.py` | handler selection, stdout JSON fallback, stderr diagnostics, uvicorn loggers, duplicate handler removal, level rules |
| `test_lifecycle.py` | all-or-nothing setup, idempotence, `shutdown_telemetry()` result and concurrency, setup after shutdown, fork warning |
| `test_metrics_contract.py` | `OTEL_METRICS_EXPORTER` / interval contract, `metrics_app()` status table, dedicated registry |
| `test_a9_deprecations_debug.py` | deprecation warnings, debug marker on entry spans |
| `test_a10_unsupported_instrumentation.py` | unsupported-version warning, `ext` return values |
| `test_a10_db_semconv.py` | `db.system.name` + `db.system` with real SQLAlchemy |
| `test_ext.py` | `otel_helper.ext` import paths and `ImportError` hints |
| `test_otlp_protocol.py` | exporter selection per protocol |
| `test_propagation.py` | W3C propagation through the global propagator |
| `test_signal_integration.py` | spans created and exported (in-memory exporter) |
| `test_setup.py`, `test_features.py` | setup basics, helpers, `start_root_span`, disabled signals |
| `test_packaging.py` | `py.typed` shipped, no `opentelemetry.*._internal` imports |
| `test_parity.py` | the shared fixture |

## Writing tests

- Tests ship with the change and keep coverage at or above 90%.
- Behavior that depends on root logger state or process globals runs in a
  fresh interpreter (see `_run` in `test_logging_a5.py`); the `conftest.py` fixture resets the OpenTelemetry
  globals between in-process tests.
- Assert on public behavior (decisions, emitted attributes, warnings), not on
  private SDK attributes.
- Wire behavior (endpoints, TLS, compression, timeouts) belongs in a parity
  `export` case, checked by the fake receiver.
