# Tests — otelhelper (Go)

Unit tests plus the shared parity fixture (`parity_test.go` runs every case of
[`parity/cases.json`](https://github.com/StaffOps/otel-libs/blob/main/parity/README.md),
[ADR-008](https://github.com/StaffOps/otel-libs/blob/main/specs/decisions/ADR-008-shared-parity-fixture.md)).
Coverage gate: **≥ 90 %** line coverage, including the parity child processes
(`PARITY_GOCOVERDIR`, merged into the profile). Everything runs in Docker; no
local Go toolchain is needed.

## Unit + parity (the CI gate)

From the repo root:

```bash
docker compose -f testing/docker-compose.yml run --rm go-tests
docker compose -f testing/docker-compose.yml stop fakeotlp-go   # when done
```

This starts `fakeotlp-go` (a fake OTLP receiver on `127.0.0.2`, `127.0.0.3`
and `[::1]`, see [`testing/README.md`](https://github.com/StaffOps/otel-libs/blob/main/testing/README.md))
and runs, in its network namespace, on `golang:1.26`:

```bash
go test -race -count=1 ./... -coverprofile=/tmp/coverage.out -covermode=atomic
# + parity child coverage merged, then:
go tool cover -func=/tmp/coverage.out | tail -1
```

`PARITY_REQUIRE_FAKEOTLP=1` is set, so receiver-dependent `export` cases fail
instead of skipping. The suite and the Python one have separate receivers and
can run at the same time; two Go runs at once need separate compose projects.

### Without the receiver

Quick run of the unit tests only (the parity `export` cases skip). From the
repository root — the whole repo is mounted because the parity tests read
`../parity/cases.json`:

```bash
docker run --rm -v "$(pwd):/src:ro" -w /src/go golang:1.26 go test ./...
```

### One test or one parity case

```bash
docker compose -f testing/docker-compose.yml run --rm go-tests \
  go test -race -count=1 -run 'TestParity/export.http_path_prefix' .
docker compose -f testing/docker-compose.yml run --rm go-tests \
  go test -race -count=1 -run 'TestHealthSampler' -v .
```

Parity case ids, the `pass` / `xfail:<task>` / `skip:<task>` statuses and the
case format are documented in `parity/README.md`. Never delete a case to make
CI green.

## Ext modules

Each `ext/*` module has its own `go.mod` and tests (no network: in-memory
SQLite and a fake `database/sql` driver, `miniredis`, an SQS client on a stub
HTTP transport):

```bash
docker run --rm -v "$(pwd)/go:/src:ro" -w /src golang:1.26 sh -c '
  for d in ext/otelaws ext/otelredis ext/otelsql; do
    (cd "$d" && go vet ./... && go test -race ./...) || exit 1
  done'
```

## Examples (build + vet)

```bash
docker run --rm -v "$(pwd)/go:/src:ro" -w /src/example golang:1.26 sh -c '
  for d in go-api go-backend go-process go-custom-signals; do
    (cd "$d" && go vet ./... && go build -o /dev/null .) || exit 1
  done'
```

Running them against a real Collector, with the expected output, is in
[example/README.md](example/README.md).

## End-to-end smoke (real Collector)

```bash
testing/e2e/smoke.sh
```

Starts `otel/opentelemetry-collector-contrib` (profile `e2e`) and runs the Go
smoke program (`testing/e2e/go-smoke`) once per combination: gRPC;
http/protobuf with an `/otlp` path prefix; a per-signal traces endpoint; TLS
with a generated CA. Each span, metric and log must reach the expected
receiver with the combination's `service.name` (the per-signal traces must
*not* reach the default receiver). Output: one `OK` / `MISS` / `LEAK` /
`XFAIL` / `XPASS` line per expectation; non-zero exit on any failure. Stop the
Collector afterwards:

```bash
docker compose -f testing/docker-compose.yml --profile e2e stop collector
```

## Test files

| File | Covers |
|------|--------|
| `config_contract_test.go` | 0.3.0 resolution contract: service name / environment precedence, `service.instance.id`, per-signal "OTLP configured", endpoint shims, TLS conflicts, timeout / compression / header parsing, env warnings vs code errors, redaction |
| `endpoint_parse_test.go` | A17 endpoint rulings: RFC 3986 split, port range and leading zeros, host characters, IDN punycode, `<unparseable URL>` rendering, `@` in the path (name-only warning), empty query/fragment |
| `a18_test.go` | A18 rulings: TLS files (per-signal then generic, client pair → server authentication), export deadline (BSP/BLRP/reader timeout variables not honored), code timeout bound, header bytes, value-length limit 0, batch size above queue size |
| `a19_test.go` | A19 rulings: `OTEL_HELPER_DISABLED_SIGNALS` vs a set standard variable and `WithMetricExporters`, unknown entries warned once, `WithDisabledSignals` normalization, interval grammar / clamp / code bound, `OTEL_CONFIG_FILE` and `OTEL_EXPERIMENTAL_CONFIG_FILE`, echoed-value truncation |
| `otlp_protocol_test.go` | Protocol selection (code > per-signal > generic env > port-4318 inference > grpc) for every signal |
| `options_test.go`, `coverage_test.go` | `With*` options, validation errors naming the option |
| `sampler_test.go` | Sampler precedence (code ratio > `OTEL_TRACES_SAMPLER`/`_ARG` > `OTEL_HELPER_SAMPLE_RATIO` > `parentbased_always_on`), unknown/unsupported names, `_ARG` grammar |
| `health_test.go` | Health sampler: default paths, normalization, `WithHealthPaths`, remote parents, gRPC health service, client-side filters |
| `tracing_test.go`, `signal_integration_test.go` | Spans exported with the configured resource, `StartRootSpan`, debug marker on entry spans only |
| `metrics_contract_test.go` | `OTEL_METRICS_EXPORTER` / `WithMetricExporters` table, export interval, dual OTLP + Prometheus, mounted handler, listener |
| `metrics_status_test.go` | `MetricsHandler()` status: 503 before/after, 200 empty when Prometheus is off |
| `logging_test.go` | Per-record handler switch (before `Setup`, bridge, after `Shutdown`), stdout JSON shape, `exception`, levels |
| `logging_spanctx_test.go` | Stdout `trace_id` / `span_id` for records in a span context: placement after `msg` / `logger`, attributes of either name dropped, held-back `With` ids, `WithGroup` replay, none without a span (A23 amendment, A45) |
| `failure_test.go` | `WithSpan` / `RecordFailure` (R11, design D15): ERROR without description + `error.type`, no event, one record per span with its ids, the three attributes and `logger` otel-helper (stdout and bridge), Unset on success and cancellation (`context.Canceled`, gRPC `Canceled`, `%w`, `http.ErrAbortHandler` panic), deadline and `errors.Join(own, ctx.Err())` = failure, override (an empty one does not hide a deeper one), 3 nested spans = 3 records, `%w` layers, new roots, panics (slice, `error`, nil), `Goexit`, uncomparable values, typed-nil `ErrorType()`, bounded chain walks, nil tracer, level-disabled record, stdout write error reported once, errgroup fan-out under `-race`, `exception.stacktrace` without the leading helper frames (first frame = the caller, also for `RecordFailure` inside a `WithSpan` fn; for a panic with a slice or an `error`, and in both layers of a nested panic, `panic(...)` then the panic site), `trimHelperFrames` table (no helper frames, header only, a later `WithSpan` kept, a frame without its file line, nested re-panics, `created by` trailer), a text of helper frames only kept, the 12 KiB cap cut in the middle on the real 100-frame shape (innermost and outermost frames, `created by` kept, one marker), the runtime marker kept on a short stack, the package prefix |
| `lifecycle_test.go` | Failed `Setup` rollback, idempotent/concurrent `Shutdown`, `Setup` after `Shutdown` |
| `deprecation_test.go`, `extra_test.go` | Deprecated variables warn once; `OTEL_HELPER_EXTRA_INSTRUMENTATION` warns and has no effect |
| `diag_channel_test.go` | Source scan: helper diagnostics only through the stderr diag logger |
| `middleware_test.go`, `otelhelper_test.go` | HTTP/gRPC wrappers, span names, `GetTracer` / `GetMeter` defaults |
| `exception_detail_test.go` | A37 (ADR-014 decision 4): otelgrpc CLIENT and SERVER spans carry no status description for INTERNAL, a plain error, FAILED_PRECONDITION, NOT_FOUND, code 99 and OK while the code, `rpc.response.status_code` and the caller's full status stay; the application's own `SetStatus` on the RPC span is kept when the RPC ends OK and replaced (empty) when it fails with INTERNAL; a bidi stream; `withoutStatusMessage` copies only an End with a message and leaves the original. `urlredact_test.go` adds a failing dial with a presigned URL: ERROR without a description, no credential anywhere on the span (ruling R3) |
| `errortype_test.go` | D16 / R12 (A40, A46): HTTP server `error.type` on span and metric for 500 / 503 / 600 / 999, none for 200 / 404 / 499 / nothing written, implicit 200 on `Write` and on `Flush`, first `WriteHeader` wins, 1xx ignored but 101 counted, `http.response.status_code` agreement; metric `error.type=503` for an inbound `traceparent …-00` and a health path whose span is dropped; `RecordFailure` + 500 keeps the application value on span and metric (also over a Labeler value); a Labeler `app_value` + 500 gives the metric `app_value` with one Labeler entry; `Flusher` / `Hijacker` preserved and not added; a panicking handler gets none; non-SDK spans untouched; gRPC name table; bufconn client/server spans for FAILED_PRECONDITION, ALREADY_EXISTS, INTERNAL, NOT_FOUND, OK, code 99, a plain error (UNKNOWN), a preset server value kept, one failing bidi stream, a filtered health call leaving the caller's span alone, and one made inside a server handler that then returns OK leaving the server span without `error.type` (fix round 1); `RecordFailure` + 500 on an unsampled request (`-00`, health path) gives the metric the application value; a failing `WithSpan` child + 500 gives `500` sampled and unsampled; a mux inside the handler keeps the route on span and metric; fix round 2: traces off (no-op TracerProvider) with an inbound sampled `traceparent` gives `500` for `RecordFailure` and for a failing child, and a parsed `MultipartForm` is handed back |
| `parity_test.go`, `parity_match_test.go` | Parity fixture runner and its expectation matcher. D16 (2026-11-18, A46): the `http_server` input `response_status`, the snapshot fields `server_span_error_type` / `client_span_error_type` (`paritySpanErrorType`) and the bufconn handler's FailedPrecondition and Internal methods; the four cases `http.server_5xx_error_type`, `grpc.client_failed_precondition_error_type`, `grpc.server_internal_error_type`, `grpc.server_not_found_no_error_type` pass. Review round (2026-11-18): the negative cases `http.server_4xx_no_error_type` and `grpc.server_ok_no_error_type` pass; a non-string `error.type` is reported as `<non-string:T>` (`errorTypeValue`); `response_status` is checked as written (`c.raw` decoded with `UseNumber`, `TestParityResponseStatusLiteral`: integer literals 200–599 only, `500.0` and `5e2` rejected); unset, the handler writes no explicit status, as before |
| `ext/otelsql/sql_test.go` | `db.system.name` mapping, legacy `db.system` and `OTEL_SEMCONV_STABILITY_OPT_IN`, `closeDB` unregistering pool metrics |
| `ext/otelsql/d16_test.go` | D16 / R12 (A38, A44): DSN parse table (PostgreSQL URL and keyword, quoted and escaped values, MySQL with `/` in the password, SQL Server URL / ADO / ODBC, SQLite, garbage), `WithDBNamespace` > DSN > `PGDATABASE` (pgx, pgx/v5, postgres, postgresql; not mysql), the unknown-namespace stderr line (driver only, no DSN) and none for sqlite/sqlite3, legacy `db.name` and `OTEL_SEMCONV_STABILITY_OPT_IN=database`; error getter table (pgx-shaped, wrapped, application SQLSTATE, `EPIPE` and other rejected shapes, MySQL 1062 / value / wrapped / joined, 2003 / 2013 / 2000–2999 bounds, nil pointer, plain error); a fake driver: 23505 / 1062 / code-less on span and `db.client.operation.duration`, no message text in attributes, `driver.ErrSkip` → Unset span and no `error.type` / status code on span and metric; a real SQLite failure through `Open` (`_OTHER`, no namespace); fix round 1: `TestNamespaceNeverLeaksSecrets` (URL userinfo with `/`, `?`, `#`, an empty `dbname=`, MySQL without `/dbname`, ADO/ODBC braces and quotes: no fragment of the secret or the host), pgx `database=` aliases, `service=` skipping `PGDATABASE`, invalid names rejected, the line once per driver name, `FuzzNamespacePassword` (seeds run in every `go test`; `go test -fuzz FuzzNamespacePassword` to explore); fix round 2: `@` in the URL query or fragment (`?password=p@ss/…`, `#frag@x/…`, SQL Server `&password=a@b?database=…`), a base64 `password=abc123==`, MySQL `u:a/b`; round 3: MySQL `/orders`, an empty `?dbname=` / `?database=`, the `%2F` socket host (with and without a password) |
| `ext/otelredis/redis_test.go` | Spans without command values, `db.system.name=redis`, pool metrics |
| `ext/otelaws/aws_test.go` | AWS SDK v2 middleware spans (semconv v1.43) |
