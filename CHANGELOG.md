# Changelog

All notable changes to this project are documented here. Format based on
[Keep a Changelog](https://keepachangelog.com/); versioning follows
[Semantic Versioning](https://semver.org/).

This is a monorepo; each language package is versioned independently but is
currently aligned at the same version.

## [Unreleased]

Target: **`0.3.0` for Go and Python** (one coordinated release, preceded by
`0.3.0-rc.1`; design D10). **Release candidate: `0.3.0-rc.2`** (2026-10-28) — tags
`v0.3.0-rc.2` (Python `0.3.0rc2`), `go/v0.3.0-rc.2` and
`go/ext/{otelaws,otelredis,otelsql}/v0.3.0-rc.2`. `rc.1` was tagged the same
day but its CI failed (SQLAlchemy test floor on Python 3.13, e2e VCS stamping),
so no Python release was published for it; its Go tags exist and are superseded
by `rc.2`. .NET releases are now tagged
`dotnet/vX.Y.Z` (design D10 A11 ruling). **The Python package rename
(ADR-011: `otel-helper` → `staffops-otel-helper`, `import staffops_otel_helper`)
lands after rc.2**: `0.3.0rc2` was published as `otel-helper`; the next
candidate, `0.3.0rc3`, is the first under the new name. It implements the cross-language config contract in
[`specs/config-contract-v0.3/`](https://github.com/StaffOps/otel-libs/tree/main/specs/config-contract-v0.3) (ADR-001 to
ADR-008). **.NET is unchanged and stays on 0.2.x**: it adopts the same contract
in Phase C (ADR-004); until then its differences are `xfail` cases in
`parity/cases.json`.

**Node.js release candidate: `0.3.0-rc.1`** (2026-11-04) — tag
`nodejs/v0.3.0-rc.1`, the first npm release of the six packages
`@staffops/otel-helper`, `-sql`, `-redis`, `-aws`, `-pino` and `-winston`,
all at `0.3.0-rc.1` (the opt-in packages pin the core at that exact
version). Published by CI to npmjs with dist-tag `next` and provenance
through trusted publishing (OIDC, no token), then to GitHub Packages with
the same bytes. It implements the same 0.3 contract (Phase B,
[`specs/nodejs-otel-helper/`](https://github.com/StaffOps/otel-libs/tree/main/specs/nodejs-otel-helper)).
Install an exact version (`npm install @staffops/otel-helper@0.3.0-rc.1`):
until the first stable release, npm `latest` points at the deprecated
`0.0.0-bootstrap.0` placeholder that trusted publishing needed.

Read the migration guide for your language first; the sections after it are
the detailed record of every change.

### Migration guide 0.2 → 0.3 — Go

**First: the module path changed** ([ADR-010](https://github.com/StaffOps/otel-libs/blob/main/specs/decisions/ADR-010-go-module-path-matches-repo.md)).
`github.com/staffops/staffops-otel-libs/go` → `github.com/staffops/otel-libs/go`
(and `…/go/ext/{otelaws,otelredis,otelsql}`), matching the repository name.
Rewrite imports, e.g.
`grep -rl staffops-otel-libs --include='*.go' --include=go.mod . | xargs sed -i 's#staffops/staffops-otel-libs#staffops/otel-libs#g'`,
then `go get github.com/staffops/otel-libs/go@v0.3.0-rc.2 && go mod tidy`.
Pinned `go/v0.2.x` releases keep the old path.

Build and API (compile errors):

1. **Toolchain ≥ Go 1.26** — older toolchains fail with
   `go: go.mod requires go >= 1.26.0` (ADR-005).
2. **gRPC interceptors are gone** (`undefined: otelhelper.UnaryServerInterceptor`,
   and the stream/client variants): use
   `grpc.NewServer(otelhelper.GRPCServerOption())` and
   `grpc.NewClient(target, otelhelper.GRPCDialOption())`.
3. **`WithExtraInstrumentation`, `Options.ExtraInstrumentation` and
   `Options.HasInstrumentation` are removed** — they never changed anything;
   import `ext/otelsql`, `ext/otelredis`, `ext/otelaws`.
4. **`ext/otelsql.Open` returns `(db, closeDB, err)`** — `defer closeDB()`
   instead of `db.Close()`.

Configuration (behavior changes, no compile error):

5. **Identity:** set `OTEL_SERVICE_NAME` and
   `OTEL_RESOURCE_ATTRIBUTES=deployment.environment.name=<env>`. There is no
   `my-service` / `LOCAL` default any more: an unset name gives the SDK's
   `unknown_service:<exe>`, an unset environment leaves the attribute absent and
   the log level at INFO (ADR-003). Standard variables now beat `SERVICE_NAME`
   / `ENVIRONMENT`, which are deprecated (warned once).
6. **TLS follows the scheme.** `https://…` + `OTEL_EXPORTER_OTLP_INSECURE=true`
   is no longer plaintext — use `http://`. `INSECURE` only applies to
   schemeless `host:port` endpoints.
7. **Endpoints follow the OTLP spec.** `https://host` without a port under
   `http/protobuf` goes to **443** (was 4317); path prefixes are kept
   (`…/otlp` → `/otlp/v1/<signal>`), so drop any `/v1/<signal>` you added as a
   workaround; a gRPC URL without a port still gets `:4317` but warns — add the
   port. Per-signal `OTEL_EXPORTER_OTLP_<SIGNAL>_*` variables are honored and a
   per-signal endpoint alone now exports that signal.
8. **Credentials:** URL userinfo/query are stripped (warned, redacted). Put
   basic auth in a header:
   `OTEL_EXPORTER_OTLP_HEADERS=Authorization=Basic%20<base64>`. Header keys must
   match `[A-Za-z0-9._-]+` (others, e.g. `x+y`, are skipped with a warning) and
   are lowercased; values are percent-decoded.
9. **Timeouts** are milliseconds (`OTEL_EXPORTER_OTLP[_<SIGNAL>]_TIMEOUT`) and
   now actually apply; **gzip** is the default compression for every signal.
10. **Deprecated, warned once, removed in 1.0:** `SERVICE_NAME`, `ENVIRONMENT`,
    `OTEL_HELPER_SAMPLE_RATIO` (→ `OTEL_TRACES_SAMPLER=parentbased_traceidratio`
    + `OTEL_TRACES_SAMPLER_ARG`), `OTEL_HELPER_DISABLED_SIGNALS`
    (→ `OTEL_{TRACES,METRICS,LOGS}_EXPORTER=none`), schemeless `http/protobuf`
    endpoints. `OTEL_HELPER_EXTRA_INSTRUMENTATION` has no effect and warns.
11. **Bad env values never fail `Setup`** (warn + default); the same invalid
    value passed in code now fails `Setup` with an error naming the option.
12. **`NewSlogHandler` now drops records below the environment level on the
    OTLP path too** (LOCAL=Debug, DEV/HML/unset=Info, PRD=Warn — before, only
    its stdout fallback was filtered, so PRD exported DEBUG/INFO). There is no
    option to turn this off: to export a lower level, build the logger with
    `NewLogger(env, debug)` and the level you want (e.g.
    `NewLogger(otelhelper.DEV, false)` in PRD, or `debug=true` for DEBUG), or
    set `OTEL_HELPER_DEBUG_LEVEL=true` / `WithDebug()` (also marks entry spans).
    Code values equal to the default now always win over the environment:
    `WithSampleRatio(1.0)` beats `OTEL_TRACES_SAMPLER`, `WithPrometheusMetricsPort(9464)`
    beats `OTEL_HELPER_METRICS_PORT`, a non-nil empty `WithDisabledMetrics` /
    `WithDisabledSignals` list beats the env list (`nil` still means "use env").

Runtime and operations:

13. **Shutdown:** handle `SIGTERM` (`signal.NotifyContext`) and call `Shutdown`
    with a fresh deadline — the helper installs no signal handler. After a
    `Shutdown` + new `Setup`, obtain tracers, meters and instrumented handlers
    again.
14. **`MetricsHandler()`** answers 503 before `Setup` and after `Shutdown`, and
    200 with an empty body when Prometheus is deliberately off.
15. **Dashboards and alerts:** Go HTTP, gRPC and runtime metrics are renamed and
    HTTP server spans are named `<METHOD> <route>` — see the telemetry table
    under "Breaking — telemetry output changes" below.
    Health requests are now **counted** in `http.server.request.duration` and
    `rpc.server.call.duration` (still not traced): exclude probe routes and
    `grpc.health.v1.Health/*` from SLI queries. Temporary bridges:
    `OTEL_GO_X_DEPRECATED_RUNTIME_METRICS=true`,
    `OTEL_SEMCONV_STABILITY_OPT_IN=rpc/dup`.
16. **`ext/otelredis` spans no longer carry command values** (`db.statement`)
    and gain pool metrics; `ext/otelsql` emits `db.system.name` (legacy
    `db.system` kept until 1.0).
17. **Debug marker:** with debug mode on, `debug=true` is now set on spans with a
    **remote** parent too, so a debug service marks every inbound request — see
    the Collector prerequisites below.
18. **Resource identity (design D1 rows of 2026-10-29, A16):** a blank
    `service.name`, `deployment.environment.name` or `service.instance.id`
    (in `OTEL_RESOURCE_ATTRIBUTES` or `WithResourceAttributes`) means "not
    provided": the next source applies (except a blank env
    `deployment.environment.name=`, which the Go SDK re-adds as `""` — known
    gap). `WithServiceName` / `WithEnvironment` now win over the same key in
    `WithResourceAttributes` on the wire too. `OTEL_RESOURCE_ATTRIBUTES`
    values are trimmed with the contract set after percent-decoding
    (`%20x%09` → `x`); `telemetry.sdk.*` set there or in code are ignored (a
    code value warns once). An empty key in `WithResourceAttributes` now
    **fails `Setup`**. The identity warnings are written once per process.
19. **BREAKING — endpoint hosts, ports and paths (design D1 rows of
    2026-10-29, A17).** Write the host as a canonical dotted quad or a DNS
    name: `127.1`, `0x7f.0.0.1`, `2130706433`, `01.2.3.4` and `1.2.3.256` are
    now **invalid** (env: warned as `<unparseable URL>`, the next endpoint
    applies; code: ``Setup` fails`). Port `0` is invalid. An invalid `%` escape in an
    http/protobuf path (`/%zz`) is invalid. Hosts are lowercased and non-ASCII
    hosts punycoded (UTS #46 non-transitional: `straße.de` →
    `xn--strae-oqa.de`). **Kubernetes:** a namespace made only of digits makes a
    short name end in a number — `collector.2024` is invalid; use the FQDN
    (`collector.2024.svc.cluster.local`) or `collector.2024.svc`. With an `@`
    in the path or query, diagnostics show only `<scheme>://`.
20. **BREAKING — export deadline, TLS files, header bytes (design D1 rows of
    2026-10-29, A18).** `OTEL_BSP_EXPORT_TIMEOUT`, `OTEL_BLRP_EXPORT_TIMEOUT`
    and `OTEL_METRIC_EXPORT_TIMEOUT` are no longer honored (they warn): set
    `OTEL_EXPORTER_OTLP_TIMEOUT` / `OTEL_EXPORTER_OTLP_<SIGNAL>_TIMEOUT`, which
    is now always the deadline of the exporter, the batch processors and the
    periodic reader (10 s by default; it used to be the SDKs' 30 s when no
    timeout was set). `WithExportTimeout` above 86 400 000 ms **fails
    `Setup`**. The helper builds the TLS configuration from the
    `OTEL_EXPORTER_OTLP[_<SIGNAL>]_CERTIFICATE` / `_CLIENT_KEY` /
    `_CLIENT_CERTIFICATE` files itself: each file per-signal, then generic; an
    incomplete or rejected client pair warns and uses server authentication
    (the log exporter used to drop the CA too). A decoded header value that is
    not printable ASCII is skipped with a warning (it used to fail every gRPC
    export). A value-length limit of `0` warns and the next level applies
    (per-signal, then generic, then no limit; it used to truncate every value
    to `""`). `OTEL_BSP_MAX_EXPORT_BATCH_SIZE` above `OTEL_BSP_MAX_QUEUE_SIZE`
    (and the `OTEL_BLRP_*` pair) warns naming both and uses the queue size
    (the span processor used to clamp silently to min(512, queue)).

21. **BREAKING — exporter lists, disabled signals, interval (design D1 rows
    of 2026-10-29, A19).** `OTEL_HELPER_DISABLED_SIGNALS` no longer disables
    a signal whose `OTEL_<SIGNAL>_EXPORTER` is set: if you set both, remove
    the deprecated variable and use `OTEL_<SIGNAL>_EXPORTER=none`.
    A standard variable counts only when it holds a valid entry (`none`
    included; only unknown entries = unset), and `WithMetricExporters` also
    wins for metrics. `WithMetricExporters("logging")` now **fails `Setup`**:
    use `"console"` (`logging` stays a warned alias in the `OTEL_*_EXPORTER`
    variables only). `WithDisabledSignals` entries are trimmed, lowercased
    and de-duplicated (`" Traces "` used to validate and disable nothing).
    `WithExportInterval` outside [1, 2 147 483 647] ms fails `Setup`, and an
    `OTEL_METRIC_EXPORT_INTERVAL` above 2 147 483 647 is clamped with a
    warning (beyond int64 it used to fall back to 30 000). `OTEL_CONFIG_FILE`
    and `OTEL_EXPERIMENTAL_CONFIG_FILE` are not supported: they are now
    warned once and ignored. Env values echoed in warnings are cut to 64
    characters; `OTEL_SDK_DISABLED=true` logs one INFO line.
22. **BREAKING — ASCII-only case folding (design D1 row "ASCII case
    folding" of 2026-10-30, A20).** Env enum values (sampler and exporter
    names, propagators, OTLP protocol, compression, booleans, known
    environments) are compared ASCII case-insensitively. A non-ASCII
    look-alike such as `PARENTBASED_TRACEİDRATIO` (U+0130), which
    `strings.ToLower` used to accept silently, now warns and falls back to
    the default: spell these values in ASCII.
23. **BREAKING — Prometheus fallback rulings (design D1 rows of 2026-11-02,
    A21).** `OTEL_HELPER_DISABLED_METRICS` / `WithDisabledMetrics` patterns
    are no longer `path.Match`: `*` matches any run of characters (`/`
    included), `?` one character and every other character is literal — a
    `[...]` class such as `http.[sc]*` now matches nothing; rewrite it as one
    pattern per alternative (`http.s*,http.c*`). Code patterns are trimmed and
    blank ones ignored, like env items. `OTEL_HELPER_METRICS_PORT` must be
    ASCII digits with an optional `+` (`-0` now warns and uses 9464). The
    standalone listener answers only `GET`/`HEAD /metrics`: other methods get
    405 (`MetricsHandler()` on your own mux is unchanged). *Amended
    (B7/A21 fix round):* the method is checked by a wrapper, not a
    `GET /metrics` pattern, so `GODEBUG=httpmuxgo121=1` keeps the behavior.
    Known Go differences, unchanged: names drop a unit word, `total` or
    `ratio` found inside the name (`req.total.count` → `req_count_total`;
    Python and Node.js write `req_total_count_total`), and a data-point
    attribute named `otel.scope.name` fails the scrape.
24. **BREAKING (narrow) — stdout log fallback reserved keys (design D1 row
    "Stdout fallback fields" of 2026-11-02, A23).** With no logs exporter,
    a top-level attribute named `time`, `level` or `msg` (record or `With`)
    is no longer written as a second key of the JSON line — it is dropped
    (slog wrote `{"msg":"m","msg":"dup"}`, which pipelines read as either
    value); an error under one of those keys becomes `exception` like any
    other error. A top-level `logger` attribute is written once as the
    line's `logger` field, before the record's own attributes (after any `With`
    attributes, which slog writes first), the last one winning (a record
    attribute over a `With` one). Inside a `WithGroup` nothing changes. If a
    dashboard or pipeline read such an attribute from Go's stdout lines,
    rename it (e.g. `msg` → `message_detail`).

25. **BREAKING (narrow) — `OTEL_SEMCONV_STABILITY_OPT_IN` tokens (A25).**
    Only the contract trim set is trimmed and only A–Z fold, so a token
    padded with a non-breaking space or other Unicode whitespace is no longer
    read as `database`: write the value in plain ASCII.

26. **BREAKING (narrow) — Redis error status (A26).** A failed Redis command
    span from `ext/otelredis` keeps status Error but no longer carries the
    server's error text as its status description (it could quote a command
    argument: `ERR unknown command 'X', with args beginning with: '<arg>'`).
    Alerts or queries that matched on that text must use the status code;
    the text is still in the span's `exception` event.
27. **BREAKING (narrow) — AWS error status (A27).** A failed AWS SDK call
    span from `ext/otelaws` keeps status Error but no longer carries
    `err.Error()` as its status description (an AWS error message can quote
    request values), and `error.type` is now the AWS error code (`NoSuchKey`,
    `AccessDenied`, `ValidationException`; `_OTHER` when there is none)
    instead of the Go error type (the modeled type name, e.g.
    `QueueDoesNotExist`, when the code is a dotted query code; a transport
    error without an AWS error is `_OTHER`). Alerts or queries that matched on
    the description or the old `error.type` values must use the status code
    and the AWS codes. Spans no longer carry `aws.s3.key`,
    `aws.s3.copy_source` or `aws.s3.delete`.
28. **BREAKING (narrow) — URL query redaction (A28).** Client spans of
    `NewHTTPTransport` write the values of `sig`, `Signature`,
    `AWSAccessKeyId`, `X-Goog-Signature`, `X-Amz-Signature`,
    `X-Amz-Credential` and `X-Amz-Security-Token` as `REDACTED` in
    `url.full` (a presigned S3 URL kept its credential, signature and session
    token). Queries on those values in `url.full` no longer match.
29. **BREAKING (narrow) — Redis span names and AWS attribute values (A30).**
    `ext/otelredis` spans are named as in Python and Node.js: the command
    uppercased (`set` → `SET`, `cluster info` → `CLUSTER INFO`), a pipeline
    by its command names uppercased (`redis.pipeline set get` → `SET GET`),
    the dial span `connect` (was `redis.dial`); `db.operation.name` keeps
    go-redis's lower-case value. `ext/otelaws` SNS spans carry
    `messaging.system="aws.sns"` (was `aws_sns`), SQS spans no longer carry
    the queue URL as `server.address`, every AWS span carries
    `server.address`/`server.port` of the request, and DynamoDB spans add the
    legacy `db.system="dynamodb"` next to `db.system.name="aws.dynamodb"`
    unless `OTEL_SEMCONV_STABILITY_OPT_IN` holds `database` without
    `database/dup`. URL query redaction also covers `access_token`,
    `X-Goog-Credential` and `GoogleAccessId`. Update span-name queries,
    dashboards and alerts on those names and values.
30. **BREAKING — HTTP opt-in metrics and AWS RPC keys (A30, owner rulings
    of 2026-11-06).** `NewHTTPHandler` / `NewHTTPTransport` record only
    `http.server.request.duration` and `http.client.request.duration` by
    default: the body-size histograms (`http.server.request.body.size`,
    `http.server.response.body.size`, `http.client.request.body.size`) are
    gone until you set `OTEL_HELPER_HTTP_OPT_IN_METRICS=true` or
    `WithHTTPOptInMetrics(true)`. `ext/otelaws` spans carry the **legacy**
    RPC keys by default — `rpc.system="aws-api"`, `rpc.service="SQS"`,
    `rpc.method="SendMessage"` — instead of the 0.3.0 release candidates'
    `rpc.system.name` and `rpc.method="SQS/SendMessage"`; set
    `OTEL_SEMCONV_STABILITY_OPT_IN=rpc` to keep the current keys, or `rpc/dup`
    for both (`rpc.method` then holds `SQS/SendMessage`); `rpc/dup` also
    switches otelgrpc to dup, doubling the RPC metric series (`rpc` is
    harmless: otelgrpc's default is already the new mode).
    `messaging.operation.name` on SQS and SNS spans is the AWS operation
    (`Publish`, `SendMessage`; was `publish_input` on SNS, absent on SQS).
    Span names stay `<Service>.<Operation>`; SQS/SNS send and publish spans
    become PRODUCER and SQS receive spans CONSUMER (they were CLIENT); other
    calls stay CLIENT.
    **Trace-derived telemetry (Go):** SQS/SNS service-graph edges change with
    the kinds: a root SQS receive — one with no parent span — becomes a server-side edge
    that expires as `client="user"`, `connection_type=virtual_node`: Go and
    Python gain these edges on upgrade. The edge counts polls, empty ones
    included, and its latency includes the long-poll wait; a receive with a
    parent span adds none; the DynamoDB node moves from
    `server="aws.dynamodb"` to `server="dynamodb.<region>.amazonaws.com"`
    (`server.address` outranks the db name), and in the Collector connector
    from `aws.dynamodb` to `dynamodb` (the new legacy `db.system`); other AWS
    nodes appear by host too. Spanmetrics series change with `span_kind` and
    with the Redis `span_name` values. Collector `filter` / tail-sampling rules
    and TraceQL on the old Redis names or `kind = client` for those calls stop
    matching. Alerts, recording rules and KEDA or HPA triggers on the removed
    body-size series go quiet without an error unless they use `absent()`.
    Check rules keyed on `span_kind`, `connection_type`, `server`,
    `client="user"` or span names.

### Migration guide 0.2 → 0.3 — Python

**First: the package name changed** ([ADR-011](https://github.com/StaffOps/otel-libs/blob/main/specs/decisions/ADR-011-owner-prefixed-package-names.md)).

| | 0.2.x and 0.3.0rc2 | 0.3.0rc3 and later |
|---|---|---|
| Install (distribution) name | `otel-helper` | `staffops-otel-helper` (extras `aws`, `redis`, `sql`, `all` unchanged) |
| Wheel file | `otel_helper-<v>-py3-none-any.whl` | `staffops_otel_helper-<v>-py3-none-any.whl` |
| Import | `import otel_helper` | `import staffops_otel_helper` (also `staffops_otel_helper.ext.*`, `.metrics`, `.config`, …) |
| Helper loggers | `otel_helper.*` | `staffops_otel_helper.*` |

- **Never have both distributions installed.** pip treats `otel-helper` and
  `staffops-otel-helper` as different projects, and both ship an
  `otel_helper/` directory: installing the new one over the old one makes the
  old wheel's files and the compatibility module overwrite each other, and a
  later `pip uninstall otel-helper` deletes the shim. Order: `pip uninstall -y
  otel-helper`, then install `staffops-otel-helper`. If both were already
  installed, uninstall both and reinstall the new one
  (`pip install --force-reinstall <wheel>`). In a lockfile or
  `requirements.txt`, replace the `otel-helper @ …` line — never add a second one.
- Update `requirements.txt` URL pins (name before `@` and the wheel file name),
  then `import` statements — e.g. `perl -pi -e 's/\botel_helper\b/staffops_otel_helper/g' $(git ls-files '*.py')`
  (covers `from otel_helper...` / `otel_helper.ext...`; BSD `sed` has no `\b`,
  so don't use `sed` on macOS for this), and isort `force_to_top` if you set it.
- **`import otel_helper` keeps working during 0.3.x**: the wheel ships a
  compatibility module that re-exports the public API — the same objects, and
  `otel_helper.<sub>` resolves to the same module as
  `staffops_otel_helper.<sub>` — and emits one `DeprecationWarning` naming the
  new import. It is **removed in 1.0**.
- **Logger names are not aliased.** Logging config, filters or alerts keyed on
  `otel_helper` / `otel_helper.*` must move to `staffops_otel_helper.*`.
- Environment variables (`OTEL_HELPER_*`) and the instrumentation scope name
  (`otel-helper`, shared with Go) do not change. The plain-text body of the
  503 that `metrics_app()` returns before setup / after shutdown now starts
  with `staffops_otel_helper:` instead of `otel_helper:` (status unchanged).

Dependencies and API:

1. **Lockstep bounds:** `opentelemetry-api/sdk/exporter-otlp` `>=1.42,<1.46`,
   contrib `>=0.63b0,<0.67b0`; resolve your app's OpenTelemetry pins inside
   that window. **Pin SQLAlchemy `<2.1`** (no supported contrib release yet;
   the helper warns when an instrumentor refuses the installed version).
2. **Removed:** `TelemetryOptions.resolve_insecure()`,
   `otel_helper.tracing.HEALTH_PATHS` / `otel_helper.instrumentation.HEALTH_PATHS`
   (use `staffops_otel_helper.DEFAULT_HEALTH_PATHS`).
3. **`TelemetryOptions` after setup:** `service_name` may be `None`;
   `environment` may be `None` or a plain `str` (values outside
   LOCAL/DEV/HML/PRD) — don't assume `options.environment.value`.
   `debug_level`, `extra_instrumentation`, `sample_ratio`, `disabled_signals`,
   `disabled_metrics`, `prometheus_metrics_port`, `export_interval_ms` and
   `metric_exporters` default to `None` and keep **only the value set in
   code**: read the effective value (code > env > default) from
   `options.resolved` (e.g. `options.resolved.sample_ratio`), not from the
   field. Any value set in code wins over the env, also one equal to the
   default (`sample_ratio=1.0`, `debug_level=False`, `disabled_metrics=[]`).
   **Stricter types:** `disabled_signals` / `disabled_metrics` must be a list
   or tuple of strings and `debug_level` a bool or int — a string such as
   `debug_level="false"` (which used to mean *true*) now raises `ValueError`.
4. **A second `setup_telemetry()`** returns the first resolved options and
   ignores its argument. Call it once per worker (gunicorn `post_fork`), not in
   the preloaded module.
5. **`otel_endpoint` in code** always gets `/v1/<signal>` appended for
   `http/protobuf`; a value already ending in `/v1/traces` must move to the new
   `traces_endpoint` (also `metrics_endpoint`, `logs_endpoint`, used verbatim).
6. **`staffops_otel_helper.ext.instrument_sql()` / `instrument_redis()` /
   `instrument_aws()`** return `True`/`False`; call them **before**
   `setup_telemetry()` and stop relying on `OTEL_HELPER_EXTRA_INSTRUMENTATION`
   (deprecated, warned) or its implicit `SQL` default.
7. **Import order:** when `OTEL_PROPAGATORS` may contain values other than
   `tracecontext`, `baggage`, `none`, import `staffops_otel_helper` before any
   `opentelemetry.*` module (isort sorts `opentelemetry` first — mark the import
   `# isort: skip`).

Configuration:

8. **Identity:** as Go (item 5 above) — `OTEL_SERVICE_NAME` and
   `OTEL_RESOURCE_ATTRIBUTES=deployment.environment.name=<env>`; no
   `my-service` / `LOCAL` default; unset environment → INFO.
9. **Timeouts are milliseconds:** `OTEL_EXPORTER_OTLP[_<SIGNAL>]_TIMEOUT=10`
   now means 10 ms (warned as "looks like seconds") — use `10000`.
   **gzip is now the default compression for every signal** (0.2 never set
   compression, so exports were uncompressed); set
   `OTEL_EXPORTER_OTLP_COMPRESSION=none` (or the per-signal variable) to opt out.
10. **TLS follows the scheme** (`INSECURE` only for schemeless endpoints; a
    non-`true` value is false and warned):

    | Endpoint | `OTEL_EXPORTER_OTLP_INSECURE` | 0.2.0 | 0.3.0 |
    |---|---|---|---|
    | `https://c:4317` | `true` | plaintext | TLS, warned |
    | `http://c:4317` | `false` | TLS | plaintext, warned |
    | `http://c:4317` | `yes` | TLS | plaintext (`yes` = false, warned) |
    | `c:4317` | `true` | plaintext | plaintext (`http://`) |
    | `c:4317` | _(unset)_ | TLS | TLS (`https://`) |

11. **Endpoints:** absolute env URLs are left to the SDK — path prefixes kept,
    HTTP without a port → 443/80, IPv6 literals work, except zone ids, which
    are invalid (item 20), per-signal variables
    honored (per-signal endpoints are used as-is). Credentials and header rules
    are the same as Go (item 8 above).
12. **Sampler is ParentBased:** a child of a remote `traceparent` with
    `sampled=0` is no longer recorded; `sample_ratio < 1` and
    `OTEL_HELPER_SAMPLE_RATIO` (deprecated) build
    `ParentBased(TraceIdRatioBased(x))`.
13. **Deprecated, warned once:** same list as Go item 10, plus
    `OTEL_HELPER_EXTRA_INSTRUMENTATION`.

Telemetry and runtime:

14. **HTTP metrics use the stable semconv:** `http.server.duration` (ms) →
    `http.server.request.duration` (s, `http.request.method`, `http.route`),
    same for the client; `OTEL_SEMCONV_STABILITY_OPT_IN=http/dup` emits both
    while you migrate. SQLAlchemy spans carry `db.system.name` and `db.system`.
15. **Health requests** are not traced but are now **counted** in
    `http_server_request_duration_seconds`; exclude probe routes in SLIs.
    Client-side exclusion is exact (`/users/ping` is traced again).
16. **`/metrics` serves only OTel series** (dedicated registry: no `process_*`,
    `python_*`, nor metrics registered on `prometheus_client.REGISTRY`);
    `metrics_app()` served from the app answers 503 before setup and after
    shutdown; register it on the exact path with
    `app.add_route("/metrics", metrics_app())` (`app.mount` redirects
    `/metrics` to `/metrics/`).
17. **Logging:** no OTel log handler without a logs exporter (stdout JSON
    fallback instead); root level is only set when root is untouched;
    `code.*` attributes are off unless `OTEL_PYTHON_LOG_CODE_ATTRIBUTES=true`;
    a second OTel `LoggingHandler` (`opentelemetry-instrument`) is removed —
    set `OTEL_PYTHON_LOG_AUTO_INSTRUMENTATION=false`.
18. **Shutdown:** call `shutdown_telemetry()` from your SIGTERM handler or the
    server's shutdown hook — Python skips `atexit` on an unhandled SIGTERM.
    Traces and metrics cannot be re-enabled by a setup after shutdown in the
    same process (API limitation).
19. **BREAKING — resource (design D1 rows of 2026-10-29, A16):** the helper
    builds the resource itself instead of `Resource.create()`, so
    `OTEL_EXPERIMENTAL_RESOURCE_DETECTORS` (`process`, `os`, `host`, contrib
    detectors) is **ignored** and warned once — add those attributes in the
    Collector (`resourcedetection`, `k8sattributes`); on SDK ≥ 1.45 the SDK's
    after-fork refresh no longer re-adds them either. `telemetry.sdk.*` set in
    `OTEL_RESOURCE_ATTRIBUTES` or `resource_attributes` are ignored (the SDK's
    values win; a code value warns once), the resource declares the schema URL
    `https://opentelemetry.io/schemas/1.43.0`, and the default service name is
    `unknown_service:<interpreter>` on every SDK version (1.42–1.44 had no
    suffix). A blank `service.name`, `deployment.environment.name` or
    `service.instance.id` now means "not provided", and `service_name=` /
    `environment=` beat the same key in `resource_attributes` on the wire.
    `resource_attributes` with an empty or non-string key, or a non-string
    `service.name` / `deployment.environment.name` / `service.instance.id`,
    now **raises `ValueError`**. An explicit `service.instance.id` now
    survives a fork on SDK ≥ 1.45 (the SDK replaced it before). The identity
    and parse warnings are written once per process (they were once per
    options object).
20. **BREAKING — endpoint hosts, ports and paths (design D1 rows of
    2026-10-29, A17).** Write the host as a canonical dotted quad or a DNS
    name: `127.1`, `0x7f.0.0.1`, `2130706433`, `01.2.3.4` and `1.2.3.256` are
    now **invalid** (env: warned as `<unparseable URL>`, the next endpoint
    applies; code: ``ValueError``). Port `0` is invalid. An invalid `%` escape in an
    http/protobuf path (`/%zz`) is invalid. Hosts are lowercased and non-ASCII
    hosts punycoded (UTS #46 non-transitional: `straße.de` →
    `xn--strae-oqa.de`). **Kubernetes:** a namespace made only of digits makes a
    short name end in a number — `collector.2024` is invalid; use the FQDN
    (`collector.2024.svc.cluster.local`) or `collector.2024.svc`. With an `@`
    in the path or query, diagnostics show only `<scheme>://`. IPv6 zone ids (`[fe80::1%eth0]`)
    are invalid too (they used to work over gRPC).
21. **BREAKING — export deadline, TLS files, header bytes (design D1 rows of
    2026-10-29, A18).** `OTEL_METRIC_EXPORT_TIMEOUT` is no longer honored by
    the periodic reader, and `OTEL_BSP_EXPORT_TIMEOUT` /
    `OTEL_BLRP_EXPORT_TIMEOUT` (which the SDK never used) now warn: set
    `OTEL_EXPORTER_OTLP_TIMEOUT` / `OTEL_EXPORTER_OTLP_<SIGNAL>_TIMEOUT`, which
    is now always the deadline of the exporter and the metric reader (10 s by
    default; the reader used 30 s; the 1.4x batch processors ignore any
    timeout). `export_timeout_ms`
    above 86 400 000 **raises `ValueError`**. The helper reads the TLS files
    itself (each per-signal, then generic) and passes them explicitly: a
    per-signal CA that cannot be used falls back to the generic one (traces
    used to fail), and an incomplete or rejected client pair warns and uses
    server authentication instead of aborting the process. A decoded header
    value that is not printable ASCII is skipped with a warning; `-bin` gRPC
    header keys now work (their value is sent as bytes). A value-length limit
    of `0` warns and the next level applies (per-signal, then generic, then no
    limit). `OTEL_BSP_MAX_EXPORT_BATCH_SIZE` above `OTEL_BSP_MAX_QUEUE_SIZE`
    (and the `OTEL_BLRP_*` pair) warns naming both and uses the queue size
    instead of failing setup.

22. **BREAKING — interval grammar and bound (design D1 rows of 2026-10-29,
    A19).** `OTEL_METRIC_EXPORT_INTERVAL` must be ASCII digits with an
    optional `+`: `5_000` and non-ASCII digits, which `int()` accepted, now
    warn and use 30 000; a value above 2 147 483 647 is clamped with a
    warning; any number of digits is accepted (it used to fall back to
    30 000). `export_interval_ms` above 2 147 483 647
    **raises `ValueError`**. Unknown `OTEL_HELPER_DISABLED_SIGNALS` entries
    now warn (they were dropped silently). A standard `OTEL_<SIGNAL>_EXPORTER`
    now wins over `OTEL_HELPER_DISABLED_SIGNALS` only when it holds a valid
    entry (`OTEL_TRACES_EXPORTER=bogus` no longer re-enables traces), and
    `metric_exporters` wins for metrics. `metric_exporters` and
    `disabled_signals` are de-duplicated after normalization:
    `["none", "none"]` disables metrics instead of raising.
    `OTEL_CONFIG_FILE` and `OTEL_EXPERIMENTAL_CONFIG_FILE` are not supported:
    they are now warned once and ignored. Env values echoed in warnings are
    cut to 64 characters. `options.resolved.metric_exporters` is the
    configured list, not the effective one (see the HOW-TO).

23. **BREAKING — Prometheus fallback rulings (design D1 rows of 2026-11-02,
    A21).** Gauges in unit `1` scrape with the `_ratio` suffix, as Go writes
    them (`system_cpu_utilization` → `system_cpu_utilization_ratio`; an
    UpDownCounter in `1` keeps its name): update dashboards and alerts that
    read such Gauges from `/metrics` (OTLP is unchanged). The standalone
    listener is now the helper's own server, not `prometheus_client`'s
    `start_http_server`: it binds dual-stack (`[::]`, IPv4 fallback) and
    serves only `GET`/`HEAD /metrics` — scrape configs pointing at another
    path (`/`) get 404 and must use `/metrics`; other methods get 405.
    `OTEL_HELPER_DISABLED_METRICS` / `disabled_metrics` patterns: `[` is
    literal (no classes), and a pattern with an upper-case letter matches
    nothing (the SDK lower-cases instrument names and patterns are
    case-sensitive): use lower-case patterns. Code items are trimmed, blanks
    ignored. `OTEL_HELPER_METRICS_PORT` must be ASCII digits with an
    optional `+`: `9_464`, `-0` and non-ASCII digits, which `int()`
    accepted, now warn and use 9464. *Amended (B7/A21 fix round):* attribute
    keys that collide once sanitized (`a.b` and `a_b`) now become one label
    with the values sorted and joined by `;` (`a_b="x;y"`, as Go), on
    series and on `target_info` — it used to keep only the last value; every
    method reaches the path check (TRACE/PROPFIND: 405 on `/metrics`, 404
    elsewhere, no more 501); idle connections are closed after 10 s; a failed
    collection answers 500 and is logged; no standalone listener runs when
    another MeterProvider was installed first (the mounted `metrics_app()`
    answers 503); an upper-case disabled-metric pattern is warned once.
24. **BREAKING — stdout log fallback time in UTC (design D1 row "Stdout
    fallback time zone" of 2026-11-02, A23).** With no logs exporter, the
    JSON lines on stdout carry `time` in **UTC with `Z`** whatever the
    process zone, as Go and Node.js write it. Before, a process with `TZ`
    set (or a host zone other than UTC) wrote local time with its offset
    (`2026-11-02T18:00:00.000-03:00`, now `2026-11-02T21:00:00.000Z`).
    Containers in UTC see no change. If a log pipeline parses `time` with a
    fixed offset layout or relies on local time, switch it to RFC 3339 in UTC.
    Non-finite float extras (`NaN`, `inf`) are now written as the strings
    `"nan"`, `"inf"`, `"-inf"`; before, the line held a bare `NaN`, which
    strict JSON parsers reject. The line uses compact separators
    (`{"time":"…","level":"INFO",…}`, no space after `:` and `,`), as Go
    and Node.js: a parser is unaffected, a grep or regex that expects
    `"level": "ERROR"` must drop the space (or allow both: `"level": ?"ERROR"`).

25. **BREAKING (narrow) — SQLAlchemy spans (A25).** A failed statement's span
    keeps status ERROR but no longer carries the database error text as
    its status description; alerts or queries that matched on that text must
    use the status code (the statement text is still recorded).
    `OTEL_SEMCONV_STABILITY_OPT_IN=http` (or an empty value) now emits
    `db.system.name` as well as `db.system`; to keep only the new keys, set
    `database` without `/dup`.

26. **BREAKING (narrow) — Redis spans (A26).** A failed Redis command's span
    (`instrument_redis()` or the deprecated `REDIS` flag of
    `setup_telemetry()`) keeps status ERROR but no longer carries
    `"<ExceptionType>: <server text>"` as its status description (it could
    quote a command argument). Match on the status code; the text is still in
    the `exception` event. If other code instruments Redis before the helper,
    that instrumentation is kept and a warning says failed commands may carry
    the text.
27. **BREAKING (narrow) — AWS spans (A27).** A failed botocore call's span
    (`instrument_aws()` or the deprecated `AWS` flag of `setup_telemetry()`)
    keeps status ERROR but no longer carries the error text as its status
    description (it could quote request values); `error.type` is the AWS
    error code (`NoSuchKey`, `AccessDenied`; `QueueDoesNotExist` for an SQS
    query-compatible code, `NotFound` for a body-less 404; a transport error
    without an AWS error gives its class name) or `_OTHER`. DynamoDB spans no
    longer carry `aws.dynamodb.item_collection_metrics` (it held the
    partition-key value). The text is still in the `exception` event. If other
    code instruments botocore before the helper, that instrumentation is kept
    and a warning says failed calls may carry the text.
28. **BREAKING (narrow) — URL query redaction (A28).** Every span the helper's
    TracerProvider starts writes the values of `sig`, `Signature`,
    `AWSAccessKeyId`, `X-Goog-Signature`, `X-Amz-Signature`,
    `X-Amz-Credential` and `X-Amz-Security-Token` as `REDACTED` in `url.full`,
    `http.url`, `http.target` and the server `url.query` (contrib redacted
    four names in client URLs and none in `url.query`).
29. **BREAKING (narrow) — gRPC span names, Redis and AWS attributes (A30).**
    gRPC client and server spans are named `pkg.Svc/Method` (semconv, as Go
    and Node.js) instead of contrib's `/pkg.Svc/Method`; the `rpc.*`
    attributes are unchanged. Redis command spans carry `db.operation.name`
    (`SET`; none on pipeline spans). botocore SQS spans carry
    `messaging.system="aws_sqs"` (was `aws.sqs`), and DynamoDB spans add
    `db.system.name="aws.dynamodb"` next to `db.system="dynamodb"` (the legacy
    key is dropped when `OTEL_SEMCONV_STABILITY_OPT_IN` holds `database`
    without `database/dup`). URL query redaction also covers `access_token`,
    `X-Goog-Credential` and `GoogleAccessId`. Update span-name queries and
    dashboards that match the leading `/`, and queries on `aws.sqs`.
30. **BREAKING — HTTP opt-in metrics and AWS spans (A30, owner rulings of
    2026-11-06).** Only `http.server.request.duration` and
    `http.client.request.duration` are recorded by default: contrib's
    `http.server.active_requests` and the HTTP body-size histograms (both
    semconv generations) are dropped until you set
    `OTEL_HELPER_HTTP_OPT_IN_METRICS=true` or
    `TelemetryOptions(http_opt_in_metrics=True)`. Every botocore call span is
    named `<Service>.<Operation>` (an SNS publish was `<topic> send`); SQS
    send spans become PRODUCER and receive spans CONSUMER (contrib's were
    CLIENT), SNS publishes stay PRODUCER, other calls CLIENT; SQS and SNS
    spans carry `messaging.operation.name` = the AWS operation.
    The RPC keys stay the legacy `rpc.*` unless `OTEL_SEMCONV_STABILITY_OPT_IN`
    holds `rpc` (`rpc.system.name`, `rpc.method="<Service>/<Operation>"`) or
    `rpc/dup` (both): set `http,rpc` / `http,rpc/dup`, because a value
    without `http` moves contrib to the old HTTP conventions. A portless
    `http://` endpoint records `server.port=80` (was 443); an out-of-range
    port records none.
    **Trace-derived telemetry (Python):** SQS service-graph edges change with
    the kinds: a root SQS receive — one with no parent span — becomes a server-side edge
    that expires as `client="user"`, `connection_type=virtual_node`: Go and
    Python gain these edges on upgrade. The edge counts polls, empty ones
    included, and its latency includes the long-poll wait; a receive with a
    parent span adds none. TraceQL `kind = client` on SQS spans stops matching. Spanmetrics series change with
    the gRPC names (`/pkg.Svc/Method` → `pkg.Svc/Method`), the SNS name
    (`<topic> send` → `SNS.Publish`) and `span_kind`. Collector `filter` /
    tail-sampling rules and TraceQL on `/grpc.health.v1.Health/Check`, other
    leading-`/` names or `<topic> send` stop matching. Alerts, recording rules
    and KEDA or HPA triggers on `http_server_active_requests` and the
    body-size series go quiet without an error unless they use `absent()`:
    move them to `http_server_request_duration_seconds_count`, or set the
    switch. Check rules keyed on `span_kind`, `connection_type`, `server`,
    `client="user"` or span names.

### Migration guide 0.2 → 0.3 — .NET (in progress, Phase C)

.NET `0.3.0` is not released; this guide grows with each Phase C task
([`specs/dotnet-otel-helper/`](https://github.com/StaffOps/otel-libs/tree/main/specs/dotnet-otel-helper)).
It has two parts, API and telemetry (requirement DN4). The telemetry part
starts when the first behavior task lands; the rename below changes no
emitted telemetry.

**API part.**

1. **The packages and the namespace were renamed** ([ADR-011](https://github.com/StaffOps/otel-libs/blob/main/specs/decisions/ADR-011-owner-prefixed-package-names.md), task C2).
   There is no `OtelHelper` 0.3 compatibility package (owner decision O2):
   the `0.2.x` packages stay published under the old IDs, and `0.3.0-rc.1`
   and later exist only under the new ones. The compiler lists every place
   to change.

   | 0.2.x | 0.3 |
   |---|---|
   | `<PackageReference Include="OtelHelper" Version="0.2.*" />` | `<PackageReference Include="StaffOps.OtelHelper" Version="0.3.0-rc.1" />` (exact: a floating `0.3.*` matches no prerelease; use it once stable `0.3.0` is out) |
   | `OtelHelper.AWS`, `OtelHelper.Redis`, `OtelHelper.Sql`, `OtelHelper.Profiling` | `StaffOps.OtelHelper.AWS`, `StaffOps.OtelHelper.Redis`, `StaffOps.OtelHelper.Sql`, `StaffOps.OtelHelper.Profiling` |
   | `using OtelHelper;` (also `OtelHelper.AWS`, `.Redis`, `.Sql`, `.Profiling`, `.Tracing`, `.Metrics`, `.Logging`) | `using StaffOps.OtelHelper;` (and the same sub-namespaces under `StaffOps.OtelHelper`) |
   | fully qualified names `OtelHelper.TelemetryOptions` | `StaffOps.OtelHelper.TelemetryOptions` |
   | `nuget.config` source mapping `OtelHelper`, `OtelHelper.*` | add `StaffOps.OtelHelper`, `StaffOps.OtelHelper.*` ([`CONSUMING.md`](https://github.com/StaffOps/otel-libs/blob/main/CONSUMING.md)) |

   Unchanged: the entry points `AddOtelHelper()`, `AddOtelHelper(Action<TelemetryOptions>)`
   and `AddOtelHelper{Aws,Redis,Sql,Profiling}()`, the type names, and every
   environment variable name. The Profiling package's logger category stays
   `OtelHelper.Profiling` (its prerequisite warning still names it): do not
   rename `Logging:LogLevel` filters for that category.

2. **The helper's `ActivitySource` and `Meter` are keyed DI services** (task
   C5, requirement DN5, N1). 0.2.x registered them unkeyed, which replaced the
   `ActivitySource` ASP.NET Core resolves for its server spans (they were
   exported under the service's scope instead of `Microsoft.AspNetCore`). Inject
   them with `[FromKeyedServices(OtelHelperKeys.Instrumentation)] ActivitySource source`
   (and `Meter`), or `GetRequiredKeyedService<ActivitySource>(OtelHelperKeys.Instrumentation)`.
   Unkeyed injection still compiles and gives no error: in a worker it resolves to
   `null`; in a web host an unkeyed `ActivitySource` **silently** resolves to the
   `Microsoft.AspNetCore` source, so custom spans are exported under that scope;
   an unkeyed `Meter` is `null`. Search for `ActivitySource` and `Meter`
   constructor parameters and `GetService<ActivitySource>()`.
3. **`TelemetryOptions` holds only what the application sets** (task C5, design
   D-NET4, requirement DN4). Every option with an environment counterpart is
   nullable, `null` meaning "not set in code"; a value set in code wins over the
   environment whatever it is, including a value equal to the default (0.2.x
   compared with the default, so `ServiceName = "my-service"` or
   `Environment = LOCAL` in code lost to the environment). A blank string counts
   as not set. Reading `IOptions<TelemetryOptions>.Value` no longer gives the
   resolved values (environment, defaults): those are internal now.

   | 0.2.x | 0.3 |
   |---|---|
   | `string ServiceName = "my-service"`, `[Required]` | `string? ServiceName` |
   | `DeploymentEnvironment Environment = LOCAL` (enum `LOCAL/DEV/HML/PRD`) | `string? Environment`, written as is; the enum is removed, constants in `DeploymentEnvironments` (`DeploymentEnvironments.PRD` …) |
   | `static GetDefaultLogLevel(DeploymentEnvironment, bool debugLevel = false)` | `static GetDefaultLogLevel(string? environment, bool debug = false)`; an unknown name gives Information |
   | `string OtelCollectorEndpoint = ""` | `string? OtelCollectorEndpoint`; new `TracesEndpoint`, `MetricsEndpoint`, `LogsEndpoint` (used as written, no `/v1/<signal>` appended) |
   | `bool DebugLevel` | `bool? DebugLevel` |
   | `string ExtraInstrumentation = "SQL"` | `[Obsolete] string? ExtraInstrumentation`: no effect (it never had a production caller), one warning when set |
   | `int ExportTimeoutMs = 10_000` | `int? ExportTimeoutMs`, in [1, 86 400 000] |
   | `Sampler Sampler = new AlwaysOnSampler()` | `Sampler? Sampler`; new `double? SampleRatio` in [0, 1] → `ParentBased(TraceIdRatioBased(ratio))` |
   | `string DisabledSignals = ""`, `DisabledMetrics = ""`, `MetricExporters = ""` | `string?` each |
   | `int? ExportIntervalMs` | `long? ExportIntervalMs`, in [1, 2 147 483 647] (`int` assignments still compile) |
   | `int PrometheusMetricsPort = 9464` | `int? PrometheusMetricsPort`, in [0, 65535] |
   | — | new `bool? Insecure`, `string? Compression` (`gzip`/`none`), `List<string>? HealthPaths`, `List<string> AdditionalMeters`, `bool ForwardSdkDiagnostics = true` |
   | — | new constants `TelemetryOptions.TracesSamplerArgEnvVar` (`OTEL_TRACES_SAMPLER_ARG`) and `TelemetryOptions.PropagatorsEnvVar` (`OTEL_PROPAGATORS`) (task C10) |
   | public `IsSignalEnabled()`, `ResolvedMetricExporters()`, `ResolvedOtlpProtocol()` | removed (they answered from code-only state) |
   | public `TelemetryOptionsValidator` | internal |
   | — | new `IServiceProvider.ForceFlushOtelHelper(int timeoutMilliseconds = Timeout.Infinite)` (`TelemetryFlushExtensions`, task C12): flushes the helper's traces and logs and collects the push metric readers it built, returning their result; `MeterProvider.ForceFlush()` is `false` whenever a pull-only reader (the Prometheus listener in dual mode, a mounted scraping endpoint) is registered. .NET-only (contract D13 note); throws `ObjectDisposedException` on a disposed provider |
4. **Invalid values: environment warns, code fails at start** (task C5, design
   D-NET6). 0.2.x threw `OptionsValidationException` inside the
   `AddOtelHelper()` call for some environment values (`OTEL_METRICS_EXPORTER=console`,
   `OTEL_EXPORTER_OTLP_PROTOCOL=http/json`, an out-of-range
   `OTEL_HELPER_METRICS_PORT`, `otlp` without an endpoint). Now an environment
   value the helper cannot use is warned on standard error (telemetry row 7)
   and replaced by the default; the app starts. The same value set in code
   fails host start (`ValidateOnStart`, not `AddOtelHelper()` any more) with an
   `OptionsValidationException` that names the option (`TelemetryOptions.OtlpProtocol`,
   not `OTEL_EXPORTER_OTLP_PROTOCOL`) and never echoes URL userinfo or a query.
   A test that expected `AddOtelHelper()` to throw now sees the exception at
   `host.StartAsync()` (or at the first `IOptions<TelemetryOptions>.Value`).
   `ServiceName = ""` is no longer an error (blank = not set).
5. **Options bound from `IConfiguration` count as code** (requirement DN4):
   `services.Configure<TelemetryOptions>(configuration.GetSection("Otel"))`
   now beats `OTEL_*` environment variables — the reverse of the usual .NET
   order (environment over `appsettings.json`). Move a value you want the
   deployment to override out of the bound section.
6. **`Configure<TelemetryOptions>` after `AddOtelHelper()` is honored** (N4):
   0.2.x resolved the options inside `AddOtelHelper()`, so a later
   `Configure` changed `IOptions` but not the pipeline. Now every value,
   including whether the Prometheus listener binds, follows the final options.
7. **Smaller changes in code options:** a code `PrometheusMetricsPort` beats
   `OTEL_HELPER_METRICS_PORT` (N8; 0.2.x let the variable win); `ExportIntervalMs`
   is `long?`, but `OTEL_METRIC_EXPORT_INTERVAL` is still read as an `int` until
   task C9 (a larger value is ignored); a schemeless
   code endpoint (`collector:4317`) is resolved like the environment value
   (`https://`, or `http://` with `Insecure`), not taken as a URL with scheme
   `collector` (N10); a code `Sampler = new AlwaysOnSampler()` now beats
   `OTEL_TRACES_SAMPLER` (0.2.x let the variable win while the code sampler
   stayed `AlwaysOnSampler`); a second `AddOtelHelper()` call still changes
   nothing and now warns once.
8. **Code `ResourceAttributes` are validated, and the identity options follow
   the contract** (task C6, contract A16). An empty or whitespace-only key, a
   `null` value or a value the SDK `Resource` cannot hold (anything but a
   string, bool, number or an array of them) fails host start naming
   `TelemetryOptions.ResourceAttributes` (0.2.x: an empty key was passed to the SDK, an
   unsupported value threw `ArgumentException` while a provider was built);
   `service.name`, `deployment.environment.name` and `service.instance.id`
   there take strings only. A dedicated option beats the same key in
   `ResourceAttributes` in the emitted resource too (`ServiceName = "a"` with
   `ResourceAttributes["service.name"] = "b"` exports `a`). `telemetry.sdk.*`
   keys in `ResourceAttributes` are ignored with one warning per key.
   `TelemetryOptions.GetDefaultLogLevel` matches the environment name ignoring
   ASCII case after trimming (`"prd"` → Warning; 0.2.x matched the exact
   spelling only, so `"prd"` gave Information). A `ConfigureResource` the app
   registered before `AddOtelHelper()` keeps its attributes, except
   `service.name`, which the resolved name replaces with one warning; its
   `deployment.environment.name` and an explicit `service.instance.id`
   (`AddService(serviceInstanceId: "x")`) are kept when the helper resolves
   none of its own — used only then, below every helper source (the environment
   then also picks the default log level; one set only by a callback registered
   after `AddOtelHelper()` does not),
   else the resolved value wins with one warning per key; an
   `AddDetector(sp => …)` factory detector registered there is kept, and the
   helper then adds its attributes without rebuilding the resource (one
   warning, naming `deployment.environment.name` when the helper resolved
   none).
9. Supersedes the last sentence of item 4: **a blank code `ServiceName` or
   `Environment` fails at start** (task C6 fix round 1, as Go and Python): `""` or whitespace is an invalid code value
   (`OptionsValidationException` naming `TelemetryOptions.ServiceName` /
   `TelemetryOptions.Environment`); `null` is "not set". 0.2.x and C5 read a
   blank value as not set. An `appsettings.json` placeholder such as
   `"ServiceName": ""` bound into `TelemetryOptions` now fails at start:
   remove the key, or give it a value.
   *Same kind of placeholder (task C9 fix round 2, 2026-11-10):* a bound
   `"MetricExporters": ""` is now a set empty list and turns metrics off (C5
   read it as not set); the helper says so once at INFO
   (`TelemetryOptions.MetricExporters is empty: no metric exporter`; Go,
   Python and Node.js are silent). A bound `"DisabledSignals": ""` disables
   nothing and beats `OTEL_HELPER_DISABLED_SIGNALS`. Remove the keys to get
   the environment's values.
10. **Register `ConfigureResource` before `AddOtelHelper()`** (task C6 fix
   round 1): the SDK runs resource callbacks in registration order and has no
   hook after them, so a `ConfigureResource` (or `AddOpenTelemetry().ConfigureResource`)
   registered **after** `AddOtelHelper()` overrides the helper's resource on
   every signal — an `AddService("x")` there replaces `service.name` and the
   instance id, and its schema URL (1.44.0) conflicts with the helper's
   (1.43.0), so the resource has none; a `SetResourceBuilder(...)` registered
   after it replaces the helper's whole resource for that signal. At host start,
   after the providers are built, the helper compares each provider's resource
   with what it set and warns once naming the signals and the changed keys
   (`signals=traces changed=service.name,…`); a schema-URL merge conflict is one
   helper warning with both URLs (instead of the SDK's "conflicting Schema URLs"
   notice). An app without a host (a bare `ServiceProvider`) gets no such check.
11. **Code endpoints follow the contract grammar** (task C7a, contract D1,
   A17). The validator runs the resolver's own rules on
   `OtelCollectorEndpoint`, `TracesEndpoint`, `MetricsEndpoint` and
   `LogsEndpoint`, each with its signal's protocol and INSECURE setting, so a
   code value that passes is one the helper uses. Newly rejected at start,
   naming the option and showing at most `<unparseable URL>`: a control
   character or backslash anywhere, an empty host (`http:///x`), port 0, a
   space, `%` or IPv6 zone id in the host, `127.1` and other non-canonical
   numeric hosts, `a{b`, a host that fails IDNA, and for `http/protobuf` an
   invalid `%` escape in the path; `TelemetryOptions.Insecure` contradicting
   the scheme of a code URL (`Insecure = true` with `https://…`). Newly
   accepted: a schemeless per-signal value (`TracesEndpoint = "collector:4318"`;
   C5 required a URL) and a schemeless value with userinfo
   (`user:pw@collector:4317`; C5 read `user:` as a scheme) — the userinfo is
   removed with a warning. A generic value that all three per-signal options
   shadow is not checked (it is never used), as in Go and Python.

12. **Exporter settings are the helper's** (task C8, ADR-013). The export
   timeout of every signal is `TelemetryOptions.ExportTimeoutMs`, else
   `OTEL_EXPORTER_OTLP_<SIGNAL>_TIMEOUT`, else `OTEL_EXPORTER_OTLP_TIMEOUT`,
   else 10 000 ms; it also bounds the batch processors and the periodic
   reader, so an application `Configure<BatchExportActivityProcessorOptions>`
   or `Configure<MetricReaderOptions>` export timeout no longer applies to the
   helper's exporters (their queue size, delay and batch size still do).
   `TelemetryOptions.Compression` unset now means `gzip`, not `none`.
   *Fix round 1:* the same holds for `Configure<LogRecordExportProcessorOptions>`
   (`BatchExportProcessorOptions.ExporterTimeoutMilliseconds`). Each of the three
   options, when set while its variable (`OTEL_BSP_EXPORT_TIMEOUT`,
   `OTEL_BLRP_EXPORT_TIMEOUT`, `OTEL_METRIC_EXPORT_TIMEOUT`) is unset, warns once
   naming the option and pointing to `TelemetryOptions.ExportTimeoutMs`. With
   `OTEL_EXPORTER_OTLP_METRICS_DEFAULT_HISTOGRAM_AGGREGATION`, the helper sets the
   named reader's timeout last, so a `Configure<MetricReaderOptions>("StaffOps.OtelHelper.OtlpMetrics", …)`
   cannot change it. On Windows the client certificate's key is imported into the
   user key store for the life of the certificate (SChannel cannot use an
   ephemeral key); on Linux it stays ephemeral.
13. **Signal and exporter options** (task C9, contract A19, R8).
   `TelemetryOptions.MetricExporters` accepts `console`; `logging` there is an
   unknown exporter and fails start (it is a warned alias of `console` only in
   the variables). `TelemetryOptions.DisabledSignals` is trimmed, lowercased
   and de-duplicated, and an entry that is not `traces`, `metrics` or `logs`
   now fails start naming the option (0.2.x ignored it), as in Go and Python.
   `TelemetryOptions.ExportIntervalMs` keeps its [1, 2 147 483 647] bound; this
   supersedes the interval sentence of item 7: `OTEL_METRIC_EXPORT_INTERVAL` now
   follows the contract grammar (telemetry row 18). An exemplar filter set in
   code must be registered after `AddOtelHelper()`
   (`ConfigureOpenTelemetryMeterProvider(b => b.SetExemplarFilter(…))`), which
   then wins over the helper's.
   *Fix round 1 (2026-11-10):* "set in code" means not null for
   `DisabledSignals` and `MetricExporters`: `DisabledSignals = ""` disables
   nothing and still beats `OTEL_HELPER_DISABLED_SIGNALS`, `MetricExporters = ""`
   means no metric exporter (0.2.x and the first C9 round read a blank string
   as "not set"), as Go, Python and Node.js treat an empty list. A
   `SetExemplarFilter` registered before `AddOtelHelper()` is overridden by the
   helper's without a warning, and
   `OTEL_DOTNET_EXPERIMENTAL_METRICS_EXEMPLAR_FILTER_HISTOGRAMS` has no effect
   (the helper always sets a filter; 0.2.x did too).

14. **Health paths, HTTP opt-in metrics and meters** (task C11, contract R7,
   D5, A30; requirement DN5, DN7, N5). `TelemetryOptions.HealthPaths` replaces
   the default list (an empty list traces every HTTP path); each path must now
   be ASCII, start with `/` and have no `?`, `#` or `,`, or host start fails
   naming `TelemetryOptions.HealthPaths item #<n>` (0.2.x had no such option).
   New `bool? HttpOptInMetrics` (environment `OTEL_HELPER_HTTP_OPT_IN_METRICS`,
   constant `TelemetryOptions.HttpOptInMetricsEnvVar`); see telemetry row 20.
   `AdditionalMeters` (added in C5) is now documented with the
   `<ServiceName>.*` rule: a `Meter` named `<ServiceName>` or
   `<ServiceName>.<anything>` is exported without listing it (0.2.x exported
   only a `Meter` named exactly `ServiceName`; other meters needed
   `ConfigureOpenTelemetryMeterProvider(b => b.AddMeter(…))`, which still works).
   Apps with gRPC services set
   `OTEL_DOTNET_EXPERIMENTAL_ASPNETCORE_ENABLE_GRPC_INSTRUMENTATION=true` in
   their configuration (environment or `appsettings.json`) to get RPC server
   spans; the helper does not set it and warns once when gRPC endpoints are
   mapped without it (owner decision O7 (b)).

**Telemetry part.** Each row: before (0.2.x) → after, and what to check in
dashboards and alerts.

1. **OpenTelemetry .NET 1.15.x → 1.20.x** (task C3, owner decision O6; still
   `net8.0`). Measured on the three examples, same .NET 8 runtime: HTTP server
   and client spans, OTLP metric names and log records unchanged;
   `telemetry.sdk.version` `1.20.1`, the instrumentation scope versions follow
   the packages, and the OTLP `ResourceSpans`/`ResourceMetrics`/`ResourceLogs`
   carry the schema URL `https://opentelemetry.io/schemas/1.44.0` (was empty).
   Series of versioned instrumentation meters get a new `otel_scope_version`
   label value (`OpenTelemetry.Instrumentation.Runtime` `1.15.1` → `1.20.1`)
   wherever the scope version becomes a label (the Prometheus fallback; on the
   OTLP path, if the Collector or backend promotes it): a one-time series
   churn. Check recording rules and alerts that match the full label set, and
   `absent()` or `count` panels across the deploy.
2. **gRPC client spans** (`OpenTelemetry.Instrumentation.GrpcNetClient`
   1.16.0, semconv 1.42): `rpc.system=grpc` → `rpc.system.name=grpc`;
   `rpc.service` removed; `rpc.method` is the full name
   (`CancelOrder` → `order.OrderService/CancelOrder`);
   `rpc.grpc.status_code=0` → `rpc.response.status_code=OK` (a string: a
   failed call gives `UNIMPLEMENTED` where 0.2.x gave `12`), and a failed call
   adds `error.type` (`UNIMPLEMENTED`; measured with a consumer, the examples
   make no failing call). For a target given as an IP address,
   `server.socket.address` is removed and replaced by `network.peer.address`,
   and `network.peer.port` and `server.address` are added (0.2.x emitted only
   `server.port` beside it); a host-name target keeps `server.address` and
   `server.port`, as before. The GrpcNetClient scope carries the schema URL
   `https://opentelemetry.io/schemas/1.42.0`. Span names and parentage are
   unchanged (the HttpClient CLIENT span stays a child of the gRPC CLIENT
   span; one CLIENT span per call comes later, task C11). Check queries,
   dashboards and alerts that use the `rpc.*` or `server.socket.address` keys
   on .NET clients.
3. **Prometheus `/metrics` fallback** (`OpenTelemetry.Exporter.Prometheus.HttpListener`
   1.9.0-beta.2 → 1.20.1-beta.1). Still reachable from other pods: the
   listener binds `http://+:<port>/metrics/` (was `http://*:<port>/`); both bind
   every IPv4 address (`IPAddress.Any`); the upstream default `localhost`
   would not be reachable. No IPv6 bind, as before: on an IPv6-only cluster a
   pod-IP scrape fails. Still a fresh collection per scrape (no response
   cache). Same answers per path and method (`/metrics`, `/metrics/` and
   `/metrics/<x>` 200; `/` and `/METRICS` 404). `OTEL_EXPORTER_PROMETHEUS_HOST`
   / `_PORT` have no effect (use `OTEL_HELPER_METRICS_PORT`). Exposition
   changes, measured in both formats:
   - both: a `target_info` gauge with the resource attributes as labels
     (`service_name`, `service_instance_id`, `deployment_environment_name`,
     `telemetry_sdk_*` and the app's own); no sample timestamps; the
     `otel_scope_version` churn of row 1;
   - both: every counter whose name already ends in `<unit>_total` no longer
     gets the unit and `_total` again — in the examples `errors_total`,
     `external_calls_total`, `heavy_work_items_total`,
     `heavy_work_batches_total`, `heavy_work_errors_total` and
     `api_health_checks_total` (was `errors_total_errors_total` and so on); a
     name that does not end in its unit keeps the doubled form
     (`orders.received_total`, unit `orders` → `orders_received_total_orders_total`).
     Check recording rules, dashboards and alerts on the old doubled names;
   - Prometheus text format (`text/plain; version=0.0.4`): no `# UNIT` lines
     and no `# EOF` line;
   - OpenMetrics 1.0.0 (what a Prometheus server negotiates first; the
     `Content-Type` adds `escaping=underscores`): `_created` series for
     counters and histograms, exemplars (`trace_id`, `span_id`) on counters and
     histogram buckets, and no `otel_scope_info` series (the scope stays in
     the `otel_scope_name`/`otel_scope_version` labels); `# UNIT` and `# EOF`
     kept.
   The fallback series carry no `service_name` label (only `target_info`
   does), as in 0.2.x, so the repository dashboards, which filter on
   `service_name`, work on the OTLP → Collector path only; do not change them
   for the fallback.
4. **Attribute limits, only when the app sets the `OTEL_*_LIMIT` variables**
   (OTLP exporter 1.20.0): a `byte[]` attribute is truncated to
   `OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT` bytes (was exported whole); event and
   link attribute counts fall back to `OTEL_ATTRIBUTE_COUNT_LIMIT` (was
   `OTEL_SPAN_ATTRIBUTE_COUNT_LIMIT`), as does the instrumentation-scope
   attribute count (`ActivitySource` tags). Measured with
   `OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT=10`, `OTEL_SPAN_ATTRIBUTE_COUNT_LIMIT=2`,
   `OTEL_ATTRIBUTE_COUNT_LIMIT=3`: 100-byte array → 10 bytes; event, link and
   scope attributes 2 → 3.
5. **Known regression on this branch — OTLP `http/protobuf` behind a
   redirect** (OTLP exporter 1.20.1): when the exporter uses its own
   `HttpClient` (the app registers no `IHttpClientFactory`), a `3xx` answer is
   a failed export and **traces, metrics and logs are all dropped with no
   diagnostic**; 0.2.x followed the redirect. When the app calls
   `services.AddHttpClient()`, the exporter uses a factory client, which still
   follows redirects (both measured). Worker services without an `HttpClient`
   and an HTTP→HTTPS redirect at a load balancer are the exposed case. It must
   be fixed before the .NET release (task C8: the helper's own client refuses
   redirects loudly, with a diagnostic); until then point endpoints at the
   final URL.
6. Upstream fixes now included, visible only in the affected cases:
   - ASP.NET Core server spans and `http.server.request.duration` get
     `error.type` for a 5xx answer without an unhandled exception
     (AspNetCore 1.20.0; measured with a consumer: the 500 server span and the
     metric gain `error.type=500`, so 5xx series get an `error_type` label —
     alerts on `http_response_status_code=~"5.."` are unaffected, label-set
     matches split);
   - SqlClient spans no longer put the exception message in the status
     description (SqlClient 1.20.0; still recorded as an exception event with
     `RecordException`), and the query-sanitization fixes of SqlClient 1.16.0,
     1.18.0 and 1.20.0 (bracketed identifiers in `FROM`, `)` inside
     `IN (...)`, quoted identifiers, nested comments, PostgreSQL `E'...'`,
     quoted logins) remove literals that leaked into `db.statement` /
     `db.query.text` and change some `db.query.summary` values — and with
     them the span names that use the summary `[TO VERIFY C14a]`;
   - AWS client spans add `cloud.region` (AWS 1.16.0/1.17.0). The SNS/SQS
     `messaging.*` attributes and `aws.sns.topic.arn` (1.16.0) appear only
     with `SemanticConventionVersion.V1_40_0`/`Latest`, not under the default
     `V1_28_0`, which the helper keeps;
   - the OTLP exporters' own HttpClient Information logs are suppressed by
     default (OTLP 1.20.0/1.20.1): with `IHttpClientFactory` at Debug level
     those records no longer reach stdout or the log backend;
   - `OTEL_EXPORTER_OTLP_COMPRESSION=gzip` set by the deployment now
     compresses (OTLP exporter 1.16.0; the helper sets no compression yet).
     *Since task C5 (2026-11-10) the helper assigns the compression itself:
     the same variable, default none until task C8 makes `gzip` the default.*
   From the upstream CHANGELOGs unless marked measured.
7. **Helper diagnostics on standard error** (task C5, design D-NET5): 0.2.x
   wrote none. Each record is one line `otelhelper: <INFO|WARN|ERROR> <message> key=value…`
   (values cut at 64 code points, control characters escaped, no header value,
   URL userinfo or query). The OpenTelemetry SDK's own warnings and errors
   (its `EventSource` events, invisible in 0.2.x — export failures among them)
   are forwarded as `otelhelper: <LEVEL> sdk <EventSource>: <text>`, with the
   response body of a failed export dropped and the configured OTLP header
   values and endpoint credentials masked; `ForwardSdkDiagnostics = false`
   turns the forwarding off for that host's sink. One listener serves the
   process: each event is written once, to the most recently started host that
   forwards, with the secrets of every live host masked (also of a host that
   opted out). Three kinds of SDK events are not forwarded: the Prometheus
   listener's cancel once any host is stopping (always, in a service provider
   without a host), the "View configuration asks to drop this instrument" notices,
   and the SDK constructor's parse warnings for `OTEL_EXPORTER_OTLP_ENDPOINT`,
   `_PROTOCOL` and `_COMPRESSION` (the helper resolves those and warns itself);
   its warnings about the other OTLP variables (timeout, headers, certificate
   files, temporality) are forwarded. A log pipeline that collects the
   container's standard error sees these lines; they do not go through
   `ILogger`. A code value that fails validation is also reported once as an
   ERROR line when a provider or the host's first logger is built before
   start; start then fails with the `OptionsValidationException`.
8. **The helper builds the OTLP exporters** (task C5, ADR-013). Helper-owned
   `OTEL_EXPORTER_OTLP_*` rows come from the process environment only: a key
   that exists only in `IConfiguration` (`appsettings.json`, command line,
   `DOTNET_`/`ASPNETCORE_`-prefixed variables) no longer affects the endpoint,
   protocol, timeout, compression, headers or TLS files (0.2.x and the SDK read
   them from there). `OTEL_SDK_DISABLED` is still read from `IConfiguration`,
   as the SDK reads it. The exporters use the helper's own `HttpClient` (one
   per exporter, no redirects followed): the named `IHttpClientFactory` clients
   `OtlpTraceExporter`, `OtlpMetricExporter` and `OtlpLogExporter` an app could
   configure are no longer used, and with `services.AddHttpClient()` a `3xx`
   answer is no longer followed either, so the redirect regression of row 5
   now applies to every app until task C8. Unchanged on the wire: endpoint,
   protocol, compression (`OTEL_EXPORTER_OTLP_COMPRESSION`, default none), the
   10 s timeout, the SDK `User-Agent` (`OTel-OTLP-Exporter-Dotnet/<version>`)
   and, for an `https://` target, the CA and client certificate files — the
   parity export cases that passed before still pass. Changed in
   `OTEL_EXPORTER_OTLP_HEADERS`: a malformed item (no `=` or an empty key) is
   skipped with one warning naming the variable, where the SDK failed provider
   construction; each item's key and value are percent-decoded on their own
   (the SDK decoded the whole string, so a `%2C` split an item), and an invalid
   escape such as `%zz` is sent as written, without a warning until task C8; a
   repeated key keeps its last value (the SDK threw); an item whose decoded key
   or value holds a control character, or whose name the HTTP client rejects,
   is skipped with a warning naming the key.
   An unreadable CA file now warns and falls back to the system trust store;
   for an `http://` target the certificate files are not read at all (the
   warning that they are ignored there comes with task C8). The metric
   temporality (`OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE`) is still
   read from `IConfiguration` (by the helper now, into the default
   `MetricReaderOptions`, so an app's own `Configure<MetricReaderOptions>`
   still wins); an invalid value (also a number, which upstream accepted)
   warns and is cumulative. `OTEL_EXPORTER_OTLP_METRICS_DEFAULT_HISTOGRAM_AGGREGATION`
   still works (owner decision, 2026-11-10) when it is set to
   `base2_exponential_bucket_histogram` in the **process environment** (or in
   an `IConfiguration` instance registered before `AddOtelHelper`) as
   `AddOtelHelper` runs: the helper then keeps the SDK's `AddOtlpExporter`
   metric reader, which sends exponential histograms, and gives it the
   helper's endpoint, protocol, headers and client and the resolved interval
   (always the helper's value); from the default `MetricReaderOptions` it takes
   only the temporality and the export timeout, so an app's
   `Configure<MetricReaderOptions>` (default name, or that reader's name) still
   wins for those. Set only in `appsettings.json` or another host configuration
   source, it is **warned, not applied** (the host registers its configuration
   as a factory, which `AddOtelHelper` cannot read). If the process environment holds
   a different value while a source that outranks it holds
   `base2_exponential_bucket_histogram` (the command line; in the default host
   the environment outranks `appsettings.json`), the warning names the value
   `AddOtelHelper` used.
   `explicit_bucket_histogram` is the default and changes nothing; any other
   value — also a padded one, which the SDK ignores — warns and is ignored. If
   OTLP metrics turn out inactive, that reader only collects; on `ForceFlush`,
   shutdown and every ~24.8 days (interval `int.MaxValue`) it exports to a
   client that answers locally, never over the network, and the helper warns
   once; it still aggregates every subscribed series a second time, the app's
   meters included (the idle-reader cost of row 9). Without the variable the
   helper builds its own reader, as before. **Do not combine `UseOtlpExporter()`
   with `AddOtelHelper()`**: both export, so every signal is sent twice (the
   helper warns once at provider build when it exports over OTLP itself). With
   the variable set as well, `UseOtlpExporter()` called before `AddOtelHelper()`
   keeps the helper from registering its histogram reader (two warnings: no
   second reader, and the doubling; `UseOtlpExporter`'s own reader applies the
   variable), and called after it makes the SDK fail the
   `MeterProvider` build with `NotSupportedException`, which the helper
   announces with one ERROR line naming the variable. An env endpoint with `://` that is
   not a usable `http(s)` URL (another scheme, a port out of range, no host)
   is warned and configures no OTLP endpoint (0.2.x rewrote it into a
   schemeless `https://http://…` target).
9. **"Off" is no exporter, not no instrumentation** (design D-NET3): with a
   signal in `OTEL_HELPER_DISABLED_SIGNALS` (or `DisabledSignals`) the
   instrumentations are still registered and spans are still created; only
   the source, meters and exporter of that signal are not added. The
   Prometheus listener reader always exists: when it is not active (port 0,
   no `prometheus` exporter, metrics or SDK disabled) it binds nothing. When
   metrics are disabled or no metric exporter is configured (`none`), a view
   drops the helper's own instrumentation meters (the service meter, the
   additional meters, `Microsoft.AspNetCore.*`, `System.Net.*`,
   `System.Runtime`, `OpenTelemetry.Instrumentation.*`), so the idle reader
   aggregates nothing of the helper's; the application's other meters, and
   its readers for them, are untouched. While helper metrics are off, the same
   view also drops an app's own ASP.NET Core or `HttpClient` metrics
   instrumentation and any meter named after the service (they share the
   helper's meter names), and the SDK's "will be ignored" notice for the
   service meter is not forwarded. `prometheus` with `PrometheusMetricsPort = 0` (the
   mounted-endpoint recipe of the HOW-TO) counts as an active exporter. With OTLP only, the idle Prometheus reader still
   aggregates every series a second time: measured on SDK 1.20.1 with 1 500
   series each of a counter and a histogram, the managed heap after recording
   grew 1 005 544 bytes with the OTLP reader alone and 1 950 832 bytes with
   the OTLP reader plus the idle listener reader (three runs, same numbers) —
   about 1.9×, roughly 315 bytes per extra metric stream. The log level and
   the `Microsoft`/`System.Net.Http` filters now apply also when logs are
   disabled.
10. **ASP.NET Core server spans change instrumentation scope** (task C5, N1,
   requirement DN5): measured on a minimal web app with `ServiceName =
   "scope-probe"`, the `GET /x` SERVER span had scope `scope-probe` with 0.2.x
   (the helper's unkeyed `ActivitySource` replaced the one ASP.NET Core
   resolves) and has scope `Microsoft.AspNetCore` now, as in every other
   language's HTTP server instrumentation. Span names and attributes are
   unchanged. Check queries, dashboards and sampling or filter rules that
   select server spans by `otel_scope_name` / `InstrumentationScope.Name` equal
   to the service name — also Collector `filter` / tail-sampling rules on the
   scope and TraceQL `instrumentation.name` queries.
11. **Service name precedence and default** (task C6, contract R5, ADR-003):
   0.2.x read `SERVICE_NAME` before `OTEL_SERVICE_NAME` and fell back to
   `my-service`. Now: `ServiceName` > `ResourceAttributes["service.name"]` >
   `OTEL_SERVICE_NAME` > `service.name` in `OTEL_RESOURCE_ATTRIBUTES` >
   `SERVICE_NAME` (deprecated) > `unknown_service:<process name>` with one
   warning naming `OTEL_SERVICE_NAME`; a blank value is not set at any level.
   A pod with both variables now reports `OTEL_SERVICE_NAME`; a service with
   none reports `unknown_service:<process>` instead of `my-service` — check
   dashboards, alerts and Collector routing on `service.name="my-service"`.
   The helper's `ActivitySource` and `Meter` keep the resolved name, so a
   service with no name configured now has a source and a meter named
   `unknown_service:<process>`. A `service.name` in `OTEL_RESOURCE_ATTRIBUTES`
   was overwritten by the helper's `AddService` in 0.2.x; it is now honored and
   beats `SERVICE_NAME`.
12. **Environment: no `LOCAL` default, unknown values kept** (task C6, R4,
   ADR-003): 0.2.x emitted `deployment.environment.name=LOCAL` when
   `ENVIRONMENT` was unset or unknown (`staging` → `LOCAL`) and ignored
   `deployment.environment.name` in `OTEL_RESOURCE_ATTRIBUTES` (overwritten by
   `ENVIRONMENT`). Now: `Environment` > `ResourceAttributes` >
   `OTEL_RESOURCE_ATTRIBUTES` > `ENVIRONMENT` (deprecated; a known name in any
   case is canonicalized, `prd` → `PRD`, any other value is emitted as
   written) > absent, with one warning; the default log level is Information
   when the environment is unset or unknown (0.2.x: Debug, through `LOCAL`).
   Check queries on `deployment_environment_name="LOCAL"` and services that
   relied on the Debug level without setting `ENVIRONMENT=LOCAL`.
13. **`OTEL_RESOURCE_ATTRIBUTES` parsed by the helper, the SDK detectors
   cleared** (task C6, A16, design D-NET7): 0.2.x left the SDK's
   `OtelEnvResourceDetector`, which trims Unicode whitespace, decodes `+` as a
   space, keeps an empty key and reads `IConfiguration`. Now the helper parses
   the process environment with the contract rules (blank items ignored, an
   item without `=` or with an empty key skipped, strict `%XX` decoding then an
   ASCII trim, an invalid escape kept literally, problems warned once by item
   position, values never echoed) and clears the SDK's detectors.
   Consequences: `a+b` stays `a+b`; a key with a leading U+00A0 keeps it;
   `OTEL_SERVICE_NAME` or `OTEL_RESOURCE_ATTRIBUTES` set only in
   `appsettings.json` / `IConfiguration` no longer reach the resource (set
   them in the process environment, or in `TelemetryOptions`); values for
   `telemetry.sdk.*` there are ignored silently. Code `ResourceAttributes` beat
   `OTEL_RESOURCE_ATTRIBUTES` per key, as before.
14. **Resource: `service.instance.id` and the schema URL** (task C6, D4, A16):
   `service.instance.id` is `ResourceAttributes` > `OTEL_RESOURCE_ATTRIBUTES` >
   one id per process (the one the SDK's `AddService` generates; 0.2.x always
   exported that generated id, so an id set in `OTEL_RESOURCE_ATTRIBUTES` was
   overwritten). The resource declares the contract schema URL
   `https://opentelemetry.io/schemas/1.43.0`, as Go, Python and Node.js
   (0.2.x on OpenTelemetry 1.15: none; since task C3 on this branch: 1.44.0).
   `telemetry.sdk.*` always come from the SDK. With an earlier factory
   detector (API item 8) the resource keeps the SDK's 1.44.0.
15. **Identity warnings are once per container** (task C6): the "no service
   name" and "environment not set" warnings, and the parse warnings of
   `OTEL_RESOURCE_ATTRIBUTES`, are written once per `IServiceProvider`
   (diagnostics sink), not once per process as in Go and Python. In a host the
   environment warning is written at start, after the three providers are
   built: "not set" only when no resource carries `deployment.environment.name`,
   and one "differs between signals" line naming the signals that lack it when
   an earlier `ConfigureResource` set it for some only; without a host it is
   written when a provider builds its resource.
16. **OTLP endpoints follow the 0.3 contract** (task C7a, contract R1, R2,
   D1.a–c, A17). Before (0.2.x) → after:

   | Input | 0.2.x | 0.3 (this branch) |
   |---|---|---|
   | URL with a path, `https://gw.example.com/otlp` | path dropped: `https://gw.example.com:4317` (+ `/v1/<signal>` for HTTP) | HTTP keeps it: `https://gw.example.com/otlp/v1/traces`; gRPC dials `https://gw.example.com:4317` |
   | URL without a port, `http/protobuf` | `:4317` added | scheme default port (80 or 443), as the OTLP specification says |
   | URL without a port, gRPC | `:4317` added silently | `:4317` added, one warning to set an explicit port |
   | explicit `:80` / `:443` | replaced by `:4317` (`Uri.IsDefaultPort`) | kept |
   | schemeless `host` with `http/protobuf` | `:4317` | `:4318`, plus a deprecation warning (use a URL) |
   | userinfo, query, fragment | dropped silently | removed with one warning per variable and value |
   | `OTEL_EXPORTER_OTLP_<SIGNAL>_ENDPOINT`, `_PROTOCOL`, `_INSECURE` | not read | read; they beat the generic variable for that signal (a per-signal endpoint is used as written) |
   | `OTEL_EXPORTER_OTLP_INSECURE=yes` | false, silently | false, warned (also with no endpoint) |
   | INSECURE contradicting an explicit scheme | scheme wins silently | scheme wins, warned |
   | `OTEL_EXPORTER_OTLP_PROTOCOL=http/json` | `OptionsValidationException` inside `AddOtelHelper()` | warned, ignored (port inference, else gRPC); since task C5 on this branch already warned |
   | port 4318, no protocol | `http/protobuf` inferred silently | inferred per signal (the endpoint that signal uses), logged once at INFO |
   | IDN host `bücher.de` | not converted by the helper | punycode `xn--bcher-kva.de` (UTS #46 non-transitional, the runtime's ICU) |
   | unparseable value (backslash, control character, zone id, space or `%` in the host, port 0 or > 65535, `127.1`, a host that fails IDNA, an invalid `%` escape with `http/protobuf`) | parsed by `Uri` or rewritten as a schemeless value, no warning | environment: warned with `<unparseable URL>`, the next value is used (per-signal → generic → none); code: host start fails |
   | unsupported scheme (`ftp://`, `unix:/path`) | rewritten as a schemeless value, no warning (task C5 on this branch: warned, no endpoint) | warned, no OTLP exporter for that signal |
   | `@` in the path | path dropped | used as parsed (HTTP sends the path), warned ("misplaced credentials?") naming the variable only |

   Check: an `http/protobuf` URL without a port now goes to 80 or 443 (0.2.x:
   4317), and the path of the generic URL is kept — a collector behind a path
   prefix now receives `/<prefix>/v1/<signal>`. The helper's diagnostics show
   at most `scheme://host:port/path`
   (`scheme://` when the path or query holds an `@`, `<scheme>:` for
   `unix:/…`). Not yet in this task: the per-signal `_HEADERS`, `_TIMEOUT`,
   `_COMPRESSION` and TLS-file variables (task C8) and the warning for a path
   on a gRPC endpoint (task C7b).
   *Update (task C7b):* the gRPC path warning now exists — a path on a gRPC
   endpoint (any source, schemeless too; not a bare `/`) is ignored as before
   and warned once per variable, naming only the variable (0.2.x: dropped
   silently). Still to come: the per-signal variables of task C8.

17. **Exporter settings follow the 0.3 contract** (task C8, contract A18,
   R2, R8, G10). Before (0.2.x) → after:

   | Input | 0.2.x | 0.3 (this branch) |
   |---|---|---|
   | `OTEL_EXPORTER_OTLP_HEADERS` | parsed by the SDK: the whole value unescaped before the split; an item without `=` or a duplicate key throws and the provider build fails | parsed by the helper: a malformed item (no `=`, a key outside `[A-Za-z0-9._-]`, an encoded key) skipped and warned by position; a value decoded as a whole, or kept literally on an invalid escape or non-UTF-8 bytes (warned); a value outside printable ASCII skipped; the last of a key wins |
   | `OTEL_EXPORTER_OTLP_<SIGNAL>_HEADERS` | not read | replaces the generic headers for that signal (no merge); with no valid item the generic ones apply |
   | gRPC header key ending in `-bin` | sent as written | value sent base64 (gRPC binary metadata: `abc` → `YWJj`) |
   | `OTEL_EXPORTER_OTLP[_<SIGNAL>]_TIMEOUT` | fixed 10 s: `ExportTimeoutMs` masked the variable | decimal milliseconds (`^[0-9]+(\.[0-9]+)?$`), clamped to [1, 86 400 000]; `0`, signs, exponents, a bare dot warn and the next level applies; the deadline of the exporter, `HttpClient.Timeout`, the batch processors and the periodic reader; gRPC sends `grpc-timeout` |
   | `OTEL_BSP_EXPORT_TIMEOUT`, `OTEL_BLRP_EXPORT_TIMEOUT`, `OTEL_METRIC_EXPORT_TIMEOUT` | read by the SDK (default 30 s) | not honored, one warning each pointing to `OTEL_EXPORTER_OTLP_TIMEOUT` |
   | compression | none sent (the helper set none; 1.15 had no compression) | `gzip` by default; `OTEL_EXPORTER_OTLP_<SIGNAL>_COMPRESSION` read; an unknown value warns and the next level applies |
   | TLS files | the generic CA and client pair, read by the SDK; no per-signal variables; nothing said on a plaintext target | per-signal, then generic, read and validated by the helper; an unreadable file or a CA with no PEM certificate warns and the next level applies (then the system roots); an incomplete or rejected client pair warns and server authentication is used; on `http://` (or schemeless with INSECURE) the files are ignored with one warning naming them |
   | batch size above queue size | lowered to the queue size silently by the SDK | lowered, one warning naming both variables |
   | value-length limit `0` | attribute values sent empty | still sent empty (no public override in the .NET exporter), now warned naming the variable |
   | `3xx` answer from the endpoint | redirect followed (1.15 client) | not followed: that export is dropped and one ERROR names the signal and the status code |

   Check: the Collector (or a proxy in front of it) must accept
   `Content-Encoding: gzip` (the OTLP receivers do); an endpoint that
   redirects must be replaced by its final URL; dashboards on attribute
   lengths are unaffected. *Update of row 16:* the per-signal `_HEADERS`,
   `_TIMEOUT`, `_COMPRESSION` and TLS-file variables listed there as task C8
   are read now.
   *Fix round 1 (2026-11-10):* the 0.2.x TLS cell, made precise against
   OpenTelemetry .NET 1.15.3 (`OtlpExporterOptions.cs`, `ApplyMtlsConfiguration`,
   added in 1.15.0): the SDK read the generic `OTEL_EXPORTER_OTLP_CERTIFICATE`,
   `_CLIENT_CERTIFICATE` and `_CLIENT_KEY` only on modern .NET (`#if NET`), and
   applied them only through its default client — an app that registered
   `IHttpClientFactory` (`services.AddHttpClient()`) got no CA and no client
   certificate. Since C8 the helper's own client applies them in every case.
   Also: a fractional timeout (`250.5`) is rounded to whole milliseconds (Go
   keeps the fraction); gRPC sends the configured deadline as `grpc-timeout`,
   not the time left on an SDK retry; a TLS file over 1 MiB (or a device) is
   "cannot be read"; the plaintext warning is one line per set of variables,
   naming every signal (`signals=traces,metrics,logs`). **Redirects (owner
   decision 2026-11-10):** the contract now refuses redirects in every
   language (task A31); until A31, Go and Python follow 307/308 and Node.js
   fails the export without a helper line.

18. **Signals and exporters follow the 0.3 contract** (task C9, contract A19,
   R2, R8, ADR-013). Before (0.2.x) → after:

   | Input | 0.2.x | 0.3 (this branch) |
   |---|---|---|
   | `OTEL_TRACES_EXPORTER`, `OTEL_LOGS_EXPORTER` | not read (only `OTEL_METRICS_EXPORTER` was) | `otlp` (default), `console` (OpenTelemetry console exporter on standard output), `none` (signal off); `logging` = warned alias of `console`; unknown entries warned and dropped; `none` with others warned, `none` wins |
   | `OTEL_METRICS_EXPORTER=console` or `logging` | start failed (`Unknown metric exporter(s) 'console'`) | console exporter at the export interval (`logging` warned) |
   | `OTEL_METRICS_EXPORTER=otlp` without an endpoint | start failed | one warning, `otlp` dropped, the default applies (`prometheus`) |
   | `OTEL_HELPER_DISABLED_SIGNALS` next to a set `OTEL_<SIGNAL>_EXPORTER` | the deprecated variable disabled the signal | the standard variable wins for that signal (a code `MetricExporters` too, for metrics); unknown entries warn once each |
   | `OTEL_METRIC_EXPORT_INTERVAL` | `int.TryParse`: `" 9464"` and `"+9464"` accepted, a value above 2 147 483 647 or garbage ignored silently | `^\+?[0-9]+$` after the contract trim, any length, > 0; anything else warns and uses 30 000; above 2 147 483 647 clamped with a warning; process environment only |
   | Exemplar filter | `SetExemplarFilter(TraceBased)` in code, so `OTEL_METRICS_EXEMPLAR_FILTER` had no effect | the helper parses `OTEL_METRICS_EXEMPLAR_FILTER` (`always_on`, `always_off`, `trace_based`; others warn) and always sets the filter, default `trace_based` — the .NET SDK default would be `AlwaysOff` |
   | `OTEL_SDK_DISABLED` | handled by the SDK alone: no line; `maybe` silently meant enabled | still read from `IConfiguration` as the SDK reads it, plus one INFO line on `true` (task C5) and a warning naming the variable for a value other than `true`/`false` (means `false`) |

   Check: a deployment with `OTEL_METRICS_EXPORTER=otlp` and no endpoint now
   starts and serves `/metrics` instead of failing; dashboards that relied on
   exemplars keep them (same default); a pod with `OTEL_TRACES_EXPORTER=none`
   or `console` stops sending spans over OTLP. The console exporter is a
   debugging aid: its multi-line output is not the contract's JSON log line
   (the stdout log fallback is task C13).

19. **Sampler, propagators and the debug marker follow the 0.3 contract**
   (task C10, contract R6, R8, R9, A20; ADR-002, ADR-006). Before (0.2.x) →
   after:

   | Input | 0.2.x | 0.3 (this branch) |
   |---|---|---|
   | No sampler configured | bare `AlwaysOnSampler`: a span under an unsampled remote parent was sampled | `parentbased_always_on` (ADR-002): roots sampled, children follow the parent's flag |
   | `OTEL_TRACES_SAMPLER` / `_ARG` | read by the SDK, only when no code sampler was set and the variable was in the process environment (the helper checked the environment first, so a value only in `IConfiguration` was already ignored, silently); an unknown name was warned by the SDK only | parsed by the helper from the process environment, which always sets the sampler; the six spec names (ASCII case-insensitive); unsupported and unknown names, and an argument that is not a plain decimal in [0, 1], warn naming the variable; an `IConfiguration`-only value is still not read and now warns once |
   | `OTEL_HELPER_SAMPLE_RATIO` | clamped to [0, 1]; below 1.0 a bare `TraceIdRatioBasedSampler`; garbage ignored silently | `parentbased_traceidratio` for a plain decimal in [0, 1]; anything else warns `sample ratio is not a number in [0, 1]` and the default applies (not clamped) |
   | `OTEL_PROPAGATORS` | not read: W3C TraceContext + Baggage always | read per token: `tracecontext`, `baggage`, `none`; other tokens warned and skipped; installed also under `OTEL_SDK_DISABLED=true`, in both directions: the OpenTelemetry propagator, the runtime propagator `HttpClient` injects with (`DistributedContextPropagator.Current`) and the one ASP.NET Core extracts with (replaced in the container) all carry only the configured tokens — `none` and `baggage` start a new trace for an inbound `traceparent`, `tracecontext` sends no `baggage`; the default list behaves as 0.2.x |
   | `OTEL_HELPER_DEBUG_LEVEL=true` | attribute `debug=true` **and** `debug=true` prepended to the tracestate on spans with `Activity.Parent == null`: `debug=true,debug=true` under a remote parent, and in-process children started from an explicit context marked too | attribute only, on entry spans (`ParentSpanId == default` or `HasRemoteParent`); the tracestate is not written |
   | `StartRootActivity` | cleared `Activity.Current`; after the root was disposed it stayed null | the previous `Activity.Current` is current again after the root is disposed; when no activity is created it is left as it was |
   | Traces disabled (`OTEL_HELPER_DISABLED_SIGNALS`, `DisabledSignals`) or `OTEL_SDK_DISABLED=true` | a root request sent `traceparent …-01` of a new trace downstream; under the disabled SDK an inbound `traceparent` reached `HttpClient` only when logging made ASP.NET Core create a request activity | as Go's and Python's no-op tracer: a root sends no trace context, an inbound one is passed on unchanged (same `traceparent`, flags and `tracestate`); `OTEL_TRACES_EXPORTER=none` keeps the SDK behavior (a root starts a sampled trace) |

   Check: a service behind a caller that does not sample (`traceparent …-00`)
   now drops those spans instead of exporting them; a Collector policy that
   matched `debug=true` in the tracestate must match the span attribute
   (`string_attribute`, as for Go and Python); an app that set
   `OTEL_TRACES_SAMPLER` only in `appsettings.json` (never applied) now gets
   a warning — move it to the environment or to `TelemetryOptions.Sampler`;
   a service with `OTEL_PROPAGATORS=none` no longer joins its callers' traces;
   an app that registers its own `DistributedContextPropagator` service or
   sets `DistributedContextPropagator.Current` keeps it (one warning when the
   configuration needs another).

20. **HTTP and gRPC instrumentation and health** (task C11, contract R7, R10,
   D5, A22, A30; requirement DN7; design D-NET8). Before (0.2.x) → after:

   | Input | 0.2.x | 0.3 (this branch) |
   |---|---|---|
   | Health list | `/ping /health /healthz /ready`, matched against `HttpRequest.Path` case-insensitively, nothing else (no trailing-slash or encoding handling) | the contract list `/health /healthz /ready /readyz /live /livez /ping /metrics`, or `TelemetryOptions.HealthPaths`; the raw request path is cut at `?`/`#`, percent-decoded (a malformed escape keeps the raw path), ASCII-lowercased and stripped of trailing slashes before the exact match: `/Health/`, `/healthz?x=1`, `/%68ealth` and `/HEALTH%2f` get no span; `/users/ping` and `/healthcare` are still traced |
   | Health requests in metrics | counted in `http.server.request.duration` | unchanged (the framework meter does not see the filter); children of a dropped request are dropped under the ParentBased default (C10) |
   | HttpClient calls to a health path | client span dropped for the four 0.2.x paths, matched on the escaped `AbsolutePath` | same list and normalization as the server; `/grpc.health.v1.Health/*` too; `http.client.request.duration` still counts them (the `System.Net.Http` meter has no path to filter on) |
   | gRPC client call | two CLIENT spans: GrpcNetClient's `pkg.Svc/Method` and HttpClient's `POST` under it | one CLIENT span, `pkg.Svc/Method` (`SuppressDownstreamInstrumentation`); the server span is its child; calls to `grpc.health.v1.Health/*` get no client span |
   | gRPC server call | no gRPC server tracing: HTTP-shaped `POST /pkg.Svc/Method` spans, status from the HTTP status (200 also for a failed RPC); `grpc.health.v1.Health/*` traced | unchanged without the key; with `OTEL_DOTNET_EXPERIMENTAL_ASPNETCORE_ENABLE_GRPC_INSTRUMENTATION=true` in the app configuration, RPC spans `pkg.Svc/Method` with `rpc.system.name`, `rpc.method`, `rpc.response.status_code`, ERROR only for the six A22 codes (upstream); `grpc.health.v1.Health/*` (Check, Watch, List) gets no server span whatever the key and the health list; one warning when gRPC endpoints are mapped without the key |
   | gRPC calls in metrics | counted in `http.server.request.duration` with `http.response.status_code=200`, also when the RPC fails | unchanged: no RPC metric exists in the .NET instrumentations (`grpc.health_*_counted_in_metrics` stay `skip:no-upstream-rpc-metrics`) |
   | HTTP opt-in metrics | `http.server.active_requests`, `http.client.active_requests`, `http.client.open_connections`, `http.client.connection.duration`, `http.client.request.time_in_queue` exported by default | dropped unless `OTEL_HELPER_HTTP_OPT_IN_METRICS=true` or `HttpOptInMetrics = true` (one INFO line when on); only the `Microsoft.AspNetCore.Hosting` and `System.Net.Http` instruments, so an app meter's instrument of the same name is kept; `DisabledMetrics` still drops one; .NET has no body-size instrument |
   | `Meter` named `<ServiceName>.<x>` | not exported | exported (N5) |

   Check: dashboards or alerts on `http_server_active_requests`,
   `http_client_open_connections`, `http_client_active_requests`,
   `http_client_connection_duration_seconds` or
   `http_client_request_time_in_queue_seconds` go empty until the switch is
   set; spanmetrics and service-graph series of the HttpClient `POST` child of
   gRPC calls disappear (duplicate `http.client.*` span series, a leaf CLIENT
   edge); probes on `/live`, `/livez`, `/readyz`, `/metrics` and the
   trailing-slash and encoded forms no longer produce spans; gRPC server span
   names, `span_name` and `status_code` change only for apps that set the key
   (a fleet can show both shapes); a health-path `HttpClient` call made inside
   a sampled request still sends a `traceparent` with the sampled flag cleared
   (the filtered activity's), as 0.2.x did.

21. **Prometheus fallback: port, host, disabled metrics, flush** (task C12,
   contract A21, A29, N8; ADR-007; design D-NET11, D-NET3). This row
   supersedes the `OTEL_EXPORTER_PROMETHEUS_HOST`/`_PORT` sentence of
   telemetry row 3 and the "the Prometheus listener reader always exists"
   sentence of row 9. Before (0.2.x) → after:

   | Input | 0.2.x | 0.3 (this branch) |
   |---|---|---|
   | `OTEL_EXPORTER_PROMETHEUS_PORT` | no effect | the listener port: code `PrometheusMetricsPort` > `OTEL_EXPORTER_PROMETHEUS_PORT` > `OTEL_HELPER_METRICS_PORT` > 9464; `0` = no listener on either variable; an invalid value warns once and the next level applies |
   | `OTEL_HELPER_METRICS_PORT` grammar | `int.TryParse` (a sign, Unicode spaces accepted); an unparseable value silently used 9464 | `^\+?[0-9]+$` after the contract trim, in [0, 65535], leading zeros allowed; anything else (`9_100`, `-0`, a 40-digit string) warns once (`invalid port`) and falls back |
   | `OTEL_EXPORTER_PROMETHEUS_HOST` | no effect (every IPv4 interface) | the bind address: an IPv4 literal binds that address only; `localhost` binds `127.0.0.1`; unset, `0.0.0.0`, `::` and `[::]` bind every IPv4 interface (`+`, as 0.2.x); any other value warns once (`invalid host, not applied: the listener binds every interface, wider than a restriction …`) and binds every IPv4 interface (fails open, owner decision) |
   | an IPv6 host | — | the .NET `HttpListener` cannot bind an IPv6 literal (measured on .NET 8.0.33 and 10.0.14: `Invalid port in prefix`). Fix round 1 (2026-11-11): `::1` / `[::1]` binds `127.0.0.1` (one warning); `::` / `[::]` every IPv4 interface (one warning); any other IPv6 address gets no listener (one ERROR; start continues, never widened) |
   | a scrape of a host-bound listener | — | the managed `HttpListener` routes a specific-host bind by the `Host` header. Fix round 1: `127.0.0.1` (and `localhost`, `::1`) also registers a `localhost` prefix, so `http://localhost:9464/metrics` answers 200 from loopback for Go (Prometheus, vmagent) and .NET `HttpClient`; curl and Node.js (`AI_ADDRCONFIG`) get 404 in a container whose only IPv6 address is `::1` (use `127.0.0.1`); any other address answers only requests naming it |
   | a busy port, an address the machine does not own | the build failed (busy port) | one ERROR naming the address, the port and what set them (fix round 1), then the build fails (`InvalidOperationException`, "HttpListener could not be started") |
   | `OTEL_HELPER_DISABLED_METRICS` / `DisabledMetrics` | case-insensitive; `*` only, `?` literal; items trimmed with Unicode whitespace | the contract glob: **case-sensitive** on the instrument name, `*` any run, `?` one character, everything else literal; items trimmed with the contract set (ASCII) |
   | Prometheus listener reader | always registered (a non-listening reader when inactive) | registered only when the listener binds (`prometheus` exporter active, port > 0, metrics and SDK enabled), decided at provider build, so a late `Configure<TelemetryOptions>` still decides it |
   | `MeterProvider.ForceFlush()` / `Shutdown()`, OTLP only | no pull reader (0.2.x added the listener only for `prometheus` with a port above 0), so the OTLP result | the OTLP result again (on this branch before C12 both returned `false` with every request received) |
   | `MeterProvider.ForceFlush()` / `Shutdown()`, dual mode `otlp,prometheus` | `false` | still `false` (upstream: a pull-only reader fails every collection outside a scrape); new `IServiceProvider.ForceFlushOtelHelper(timeoutMilliseconds)` flushes the helper's traces and logs providers and collects only the push metric readers the helper built, and returns their result (a collector answering 500 → `false`) |
   | an application `AddPrometheusHttpListener()` next to an inactive helper listener | its options left alone | left alone (on this branch before C12 the helper's options cleared its prefixes) |
   | an application `AddPrometheusHttpListener()` next to an active helper listener | (0.2.x: the helper's own options delegate only) | both run on their own ports: the helper's reader and options are named `StaffOps.OtelHelper.Prometheus` (fix round 1; before, both got the helper's prefix and the second start failed) |

   Check: a deployment that set `OTEL_EXPORTER_PROMETHEUS_PORT` (or `_HOST`)
   for another purpose now moves (or restricts) the listener; scrape configs
   and `NetworkPolicy` follow the new port; a `OTEL_HELPER_DISABLED_METRICS`
   pattern written in another case than the instrument name no longer drops
   it; shutdown code that tested `MeterProvider.ForceFlush()` in dual mode
   should call `ForceFlushOtelHelper()` instead (HOW-TO §15). For traces and
   logs `ForceFlushOtelHelper` returns the SDK's result, which means "handed to
   the exporter in time", not "accepted by the collector". With OTLP only,
   `MeterProvider.Shutdown()` returns `true` even after a 500 (upstream,
   measured): only `ForceFlushOtelHelper()` reports the metric export result.
22. **Logs: stdout fallback, levels and filters** (task C13, contract D6, R4,
   A23; design D-NET10, requirement DN8, N9). Before (0.2.x) → after:

   | Input | 0.2.x | 0.3 (this branch) |
   |---|---|---|
   | no logs exporter (no endpoint, `OTEL_LOGS_EXPORTER=none`, logs disabled, `OTEL_SDK_DISABLED=true`, an endpoint the helper cannot use) | the OpenTelemetry logger provider was added with no processor: records were dropped there; stdout showed only what the host's own console provider wrote, in its format | the helper's provider (alias `OtelHelperStdout`) writes one JSON object per line on standard output: `{"time":"2026-11-11T12:00:00.000Z","level":"WARN","msg":"…","logger":"<category>",<state values>,"exception":"…"}` — `time` in UTC with milliseconds and `Z`, `level` one of `DEBUG` (Trace, Debug), `INFO`, `WARN`, `ERROR` (Error, Critical), `logger` the category, the record's name/value pairs after it (`{OriginalFormat}` left out, the first of duplicate names kept), `exception` the exception's `ToString()`; a value named `time`, `level`, `msg` or `logger` is dropped; numbers and booleans keep their JSON type, non-finite numbers and other values are text (`NaN`, `Infinity`); ILogger scopes are not written. The OpenTelemetry provider is held at `None`. The host's console provider is not removed: an app that keeps it gets both lines (`builder.Logging.ClearProviders()` before `AddOtelHelper()` keeps only the contract line, HOW-TO §4) |
   | a logs exporter active (OTLP with an endpoint, `console`) | OTLP | unchanged; the stdout provider writes nothing (no tee, D6) |
   | `OTEL_LOGS_EXPORTER=console` | not read | the OpenTelemetry console exporter (row 18): a multi-line debugging dump, not the JSON line |
   | the helper's level (R4: LOCAL Debug, DEV/HML/unset/unknown Information, PRD Warning) | `LoggerFilterOptions.MinLevel`, for **every** provider; any rule without a provider (`Logging:LogLevel:Default` in the template `appsettings.json`) beat it, so a PRD app with that file exported Information | on the helper's two providers only; precedence (owner decision 2026-11-11, as Python and 0.2.x): code `MinimumLogLevel` > your rule for the helper's provider without category (`Logging:OpenTelemetry:LogLevel:Default`) > your default for every provider (`Logging:LogLevel:Default`, `AddFilter(null, level)`, `SetMinimumLevel`) > the environment default. A PRD app with the template file still exports Information. `MinLevel` and the application's own providers are left as they are |
   | `Microsoft.*` and `System.Net.Http*` above Debug | held at Error on **every** provider (the host console included) | held at Error on the helper's providers only; the application's providers show framework warnings again |
   | an application rule without a provider for a category (`Logging:LogLevel:Microsoft.AspNetCore`, `AddFilter("Orders", …)`) | applied to every provider; a rule for exactly `Microsoft` or `System.Net.Http` lost to the helper's Error rule (registered later) | still applied to the helper's providers (copied as a rule for each), also when registered after `AddOtelHelper()`; a rule for exactly `Microsoft` or `System.Net.Http` now replaces the helper's Error rule |
   | a rule for one of the helper's providers (`Logging:OpenTelemetry:LogLevel:…`, `Logging:OtelHelperStdout:LogLevel:…`) | applied | applied; the helper adds no rule of its own for that category |
   | the stdout fallback next to the host's console provider | — | each record twice on stdout; one warning at host start names `ClearProviders()` (fix round 1) |
| the Profiling prerequisite warning | category `OtelHelper.Profiling` | unchanged (decision 2026-11-11, design D-NET10 note): `Logging:LogLevel:OtelHelper.Profiling` filters keep working; it is an application-side record, so it reaches the helper's providers at their level (Warning in PRD) |

   Check: log pipelines that tail stdout of a pod without a Collector now get
   one JSON object per line from the helper (and, if the app keeps the host's
   console provider, the console lines too — remove one); OTLP log volume per
   level is unchanged for apps with `Logging:LogLevel:Default` (it still wins)
   and for apps without one (the environment default, as 0.2.x's `MinLevel`);
   app-side consoles show `Microsoft.*` warnings again. Loki: `| json | level="ERROR"` matches .NET Error and
   Critical, as in Go, Python and Node.js; OTLP records keep the SDK's severity
   number and text (design D-NET10).

### Collector / platform prerequisites (before rolling 0.3.0 out)

Owned outside this repo (design D11); each item blocks the rollout, not the rc:

1. Tail sampling: a default branch for spans **without**
   `deployment.environment.name` at the PRD rate (ADR-003).
2. Tail sampling: the `debug=true` policy combined (`and`) with an environment
   allow-list and a `rate_limiting` sub-policy, so a debug-enabled PRD service
   cannot force 100% retention of every trace it touches (ADR-006). This
   covers every language: Go and Python 0.3 mark spans with no parent or a
   remote parent, and .NET 0.2.x marks entry spans (no in-process parent,
   which includes spans continuing a remote trace) and prepends `debug=true`
   to the propagated tracestate.
3. Gateway: `loadbalancingexporter` with `routing_key: traceID`;
   `decision_wait` ≥ the observed p99 trace duration (measure it first); the
   sampled-decision cache enabled for late spans (ADR-006 relies on it).
4. Charts: set `OTEL_SERVICE_NAME` and `OTEL_RESOURCE_ATTRIBUTES` with
   `deployment.environment.name`. Do **not** set `service.instance.id` for
   multi-worker containers (design D4); single-process pods may use the
   semconv K8s recipe.
5. Istio: mesh tracing at 100% or disabled — it interacts with the
   ParentBased sampler (ADR-002).
6. VictoriaMetrics OTLP ingest promotes every resource attribute to a label:
   use `-opentelemetry.ignoreResourceAttributes` for volatile attributes (e.g.
   `process.pid`); keep `service.instance.id` (design D4).
7. A Collector `filterprocessor` dropping health spans, as defense in depth
   (design D5).
8. Consumers using portless gRPC URLs: add an explicit `:4317` (design D1.b
   warning).

### .NET

No changes in this release. .NET stays on 0.2.x; the 0.3 contract and the
.NET backlog (`specs/config-contract-v0.3/tasks.md`, Phase C) land later.

Phase C work on this branch (no .NET release yet; the first will be
`dotnet/v0.3.0-rc.1`, spec
[`specs/dotnet-otel-helper/`](https://github.com/StaffOps/otel-libs/tree/main/specs/dotnet-otel-helper)):

- **.NET: packages, assemblies and namespace renamed to `StaffOps.OtelHelper*`
  (task C2, ADR-011, N13). BREAKING (source).** Package IDs
  `StaffOps.OtelHelper` and `StaffOps.OtelHelper.{AWS,Redis,Sql,Profiling}`,
  root namespace `StaffOps.OtelHelper`, project folders and
  `dotnet/StaffOps.OtelHelper.sln`; no `OtelHelper` 0.3 compatibility package
  (owner decision O2). Same code and behavior as `0.2.x` (the 179 existing
  test cases pass unchanged, line coverage unchanged at 95.96%). Migration:
  the .NET guide above. CI (`dotnet-test`, `build-lib`, `publish-dotnet`,
  `publish-dotnet-release`), the example `Dockerfile`/`Dockerfile.demo`s and
  `CONSUMING.md` use the new names; `build-lib`, `publish-dotnet` and
  `publish-dotnet-release` fail unless they packed exactly the five
  `StaffOps.OtelHelper*` packages (the release job: each at the tag version).
- **.NET: build hygiene (task C2, N12).** New `dotnet/Directory.Build.props`
  for every project: deterministic build, warnings as errors (no blanket
  `CS1591` exemption; NuGet warnings that depend on the network or on advisory
  data, `NU1801` and `NU1900`–`NU1905`, stay warnings so the tree builds
  offline from a filled cache, as does `NU5104`, stable package with a
  prerelease dependency), and SourceLink for the libraries (the SDK's
  built-in one, with `PublishRepositoryUrl`, `EmbedUntrackedSources` and the
  PDB embedded in the assembly, because GitHub Packages hosts no symbol
  packages). `dotnet/Directory.Build.targets` gives the packable
  `StaffOps.OtelHelper*` projects their package metadata (`Authors`,
  `PackageLicenseExpression`, `PackageProjectUrl`, `RepositoryUrl`,
  `RepositoryType`); tests and examples get none. The CI pack
  steps pass `/p:ContinuousIntegrationBuild=true`. The 26 public members that
  had no XML doc comment (`DeploymentEnvironment` and its values, the
  `TelemetryOptions` `*EnvVar` constants, `ServiceName`, `Environment`,
  `OtelCollectorEndpoint`, `TelemetryOptionsValidator`, `TelemetryExtensions`)
  are documented. The nuspec of every package carries the repository URL and
  commit (before, only the CI pack steps passed `RepositoryUrl` for the core).
  CI `dotnet-test` sets `VSTEST_TESTHOST_SHUTDOWN_TIMEOUT=10000`: vstest
  killed the test host before `coverlet.msbuild` wrote its hit files, so the
  coverage total sometimes dropped below the gate with every test green. The
  example proto's generated namespace is `Examples.Grpc` (was
  `OtelHelper.Grpc`; example code only, the gRPC service names on the wire are
  unchanged).
- **.NET: OpenTelemetry 1.15.x → 1.20.x (task C3, owner decision O6, design
  D-NET1).** Core `OpenTelemetry`, `.Api`, `.Extensions.Hosting`,
  `.Exporter.OpenTelemetryProtocol` 1.20.1; `Exporter.Prometheus.HttpListener`
  1.20.1-beta.1 (was 1.9.0-beta.2); `Instrumentation.AspNetCore`, `.Http`,
  `.Runtime`, `.SqlClient` 1.20.1; `.GrpcNetClient` and `.StackExchangeRedis`
  1.20.1-beta.1; `.AWS` 1.20.3; still `net8.0`. Tests: `Microsoft.Extensions.*`
  8.0.1 (was 9.0.0), `OpenTelemetry.Exporter.InMemory` 1.20.1. Telemetry
  changes: the .NET migration guide above, telemetry part. The Prometheus
  listener keeps the 0.2.x behavior on the new exporter: bound on every IPv4
  address with the `http://+:<port>/metrics/` prefix (the upstream default
  `localhost` is unreachable from other pods, and `Host = "*"` aborts the
  build; no IPv6 bind, as before), no response cache, and
  `OTEL_EXPORTER_PROMETHEUS_HOST`/`_PORT` without effect (the one-time
  warning comes with task C12). Known regression until task C8: an
  `http/protobuf` endpoint that redirects drops all telemetry silently when the
  app registers no `IHttpClientFactory` (migration guide, telemetry row 5).
  New `testing/e2e/dotnet-prom-scrape.sh` (verify step `dotnet-scrape`, CI job
  `dotnet-prom-scrape` on PRs, `main` and `dotnet/v*`, required by
  `publish-dotnet` and `publish-dotnet-release`) scrapes the `dotnet-process`
  example from another container in the text and OpenMetrics formats; four new
  unit tests (`PrometheusListenerTests`, no network needed); suite 183/183,
  line coverage 96.31%.
  The `release` skill has a .NET SDK-bump checklist.
- **.NET: parity runner (task C4, design D-NET12, DN11).** The .NET suite now
  runs every case of `parity/cases.json` in its own process
  (`StaffOps.OtelHelper.ParityDriver`, not packed) against the shared fake
  OTLP receiver, with strict xfail and the release gate; compose service
  `dotnet-tests`, `testing/verify.sh` step `dotnet`, CI `dotnet-test` with the
  receiver. The fixture now records the 0.2 behavior of .NET: 91 `pass`, 252
  `xfail:C<n>`, 13 `skip:C5-no-api`, 12 known-difference skips. No library
  change; suite 571/571 on SDK 8 and on SDK 10 (roll-forward), line coverage
  98.77%.
- **.NET: resolver core (task C5, ADR-013, design D-NET3–D-NET6). BREAKING
  (source and behavior).** `AddOtelHelper()` no longer builds a bootstrap
  service provider: services are registered eagerly (instrumentations, the
  logger provider and the Prometheus listener reader, always) and every value
  is applied when the providers are built, from options resolved once per
  container (`Configure<TelemetryOptions>` after `AddOtelHelper()` is honored,
  N4). The OTLP exporters are constructed by the helper with every option
  assigned and the helper's `HttpClient` factory (headers and TLS files from
  the process environment; contract parsing of those comes with task C8).
  `TelemetryOptions` gets nullable code fields and the new members of the
  design API table (`TracesEndpoint`, `MetricsEndpoint`, `LogsEndpoint`,
  `Insecure`, `Compression`, `SampleRatio`, `HealthPaths`, `AdditionalMeters`,
  `ForwardSdkDiagnostics`, `long? ExportIntervalMs`); `Environment` is a
  string; the service `ActivitySource`/`Meter` are keyed DI services. One
  diagnostics channel on standard error (`otelhelper: <LEVEL> …`, redacted,
  cut, line-forging characters escaped) with the SDK `EventSource` warnings
  forwarded; environment values warn, code values fail at start naming the
  option; `OTEL_CONFIG_FILE` / `OTEL_EXPERIMENTAL_CONFIG_FILE` warned once by
  name; the `OTEL_SDK_DISABLED=true` INFO line (read from `IConfiguration`, as
  the SDK); a second `AddOtelHelper()` warns. Migration: the .NET guide above,
  API items 2–7 and telemetry rows 7–10. Parity: the 13 `skip:C5-no-api` cases
  now run.
  Fixture: 133 `pass` (was 91), 223 `xfail:C<n>`, 12 known-difference skips,
  no `skip:C5-no-api` left; four warning cases also assert `helper_log`, and
  the code-error regexes accept the .NET option names (`OtelCollectorEndpoint`,
  `TracesEndpoint`, `SampleRatio`, `Insecure`, qualified `TelemetryOptions.`); suite 782/782, line coverage
  98.72% (cobertura line rate 0.9872; SDK 10 roll-forward 98.8%; the figure varies by about ±0.1 point between runs), on SDK 8 and SDK 10 (roll-forward).
- **.NET: identity and resource (task C6, contract R4, R5, D4, A16, A24;
  design D-NET7). BREAKING (telemetry).** Service name and environment follow
  the 0.3 precedence (standard variables first, `SERVICE_NAME` and
  `ENVIRONMENT` deprecated aliases read last; no `my-service` or `LOCAL`
  default: `unknown_service:<process>` and an absent environment, each warned
  once); the R4 log levels (unset or unknown → Information, names matched
  ignoring ASCII case); the helper parses `OTEL_RESOURCE_ATTRIBUTES` with the
  contract rules and rebuilds each provider's resource (`Build()` of what
  earlier `ConfigureResource` callbacks added, then `Clear()`: env < earlier
  app attributes < code attributes < identity < `telemetry.sdk.*`, schema URL
  `https://opentelemetry.io/schemas/1.43.0`); `service.instance.id` from code,
  env, or one id per process; invalid code `ResourceAttributes` fail at start.
  Migration: the .NET guide above, API item 8 and telemetry rows 11–14.
  Parity: the 29 `xfail:C6` cases pass (the C6 group's 14 others already
  passed: 43 of the 46 now pass; `fork.*` and the detector case stay skipped). Fixture: 162 `pass` (was 133), 194 `xfail:C<n>`, 12 known-difference skips.
  Fix round 1: a blank code `ServiceName`/`Environment` fails at start (API
  item 9; two new cases `config.code_blank_service_name_fails` and
  `config.code_blank_environment_fails`, passing in every language); a
  `ConfigureResource` registered after `AddOtelHelper()` is detected and
  warned (API item 10); earlier app identity keys handled per key, an app
  environment picks the log level (API item 8). Fixture: 370 cases; .NET 164
  `pass`, 194 `xfail:C<n>`, 12 known-difference skips.
  Fix round 2: the later-change check runs at host start on the built
  providers (each detector runs once per provider; a later
  `SetResourceBuilder` is detected too, naming the signal); the schema-URL
  conflict warning carries both URLs. Suite 854/854 on SDK 8 (three runs) and SDK 10 (roll-forward), line coverage 98.64%.
  Fix round 3: in a host the environment warning moves to the start-time
  check over the three built resources (telemetry row 15); the check skips an
  empty resource and `OTEL_SDK_DISABLED`. Suite 858/858 on SDK 8 and SDK 10 (roll-forward), line coverage 98.67%.
- **.NET: endpoint parsing and diagnostics (task C7a, contract R1, R2,
  D1.a–c, A17). BREAKING (telemetry and configuration).** The helper parses
  every OTLP endpoint by RFC 3986 with the rules Go, Python and Node.js use
  (`Resolution/EndpointGrammar.cs`, `Resolution/EndpointResolver.cs`): ports
  compared numerically (1–65535), host rules (no space, `%` or zone id; an
  IPv6 literal or an RFC 3986 name; canonical dotted quads only), IDN hosts in
  punycode (UTS #46 non-transitional, through `IdnMapping`), userinfo, query
  and fragment removed with one redacted warning per variable and value, an
  `@` in the path warned naming the variable only, `<unparseable URL>` for
  anything that cannot be parsed. URLs keep their path and the scheme default
  port for `http/protobuf` (0.2.x rebuilt `scheme://host:4317`); a portless
  gRPC URL gets `:4317` with a warning; schemeless values get `https://`
  (`http://` with INSECURE=true) and the protocol's port, deprecated for
  `http/protobuf`. Per signal: `OTEL_EXPORTER_OTLP_<SIGNAL>_ENDPOINT`,
  `_PROTOCOL` and `_INSECURE` are read (code per-signal > code generic >
  env per-signal > env generic; an unparseable environment value falls back,
  an unsupported scheme leaves the signal unconfigured), the 4318 inference
  uses the endpoint each signal resolves to and is logged once at INFO, and
  `http/json` or unknown protocol values are warned and skipped. INSECURE is
  `true`/`false` (ASCII case-insensitive), anything else false and warned even
  without an endpoint; a contradiction with an explicit scheme is warned (the
  scheme wins), and `TelemetryOptions.Insecure` contradicting a code URL fails
  at start. Code endpoints are validated by the same rules (schemeless
  per-signal values now accepted). Migration: the .NET guide above, API item
  11 and telemetry row 16. Parity: the 45 `xfail:C7a` cases pass, so the
  whole C7a group passes (62 cases by the ownership rule: the 60 of C4 plus
  the two C6 fix-round `config.code_blank_*` cases); 26 `xfail:C7b` cases
  pass as well (per-signal export and URL handling on the wire), leaving 4
  of the C7b group's 55 to C7b: the gRPC-path warning cases (A29).
  Fixture: 235 `pass` (was 164), 123 `xfail:C<n>`, 12 known-difference skips.
  Measured (2026-11-10): compose `dotnet-tests` on `sdk:8.0` 1026/1026 and
  SDK 10.0.403 roll-forward 1026/1026, line coverage 98.45% (branch 96.26%,
  method 99.32%); 20 mutations of the new rules each fail a test. The IDN
  conversion needs ICU: in globalization-invariant mode a non-ASCII host is
  invalid (warned or failing at start, "needs ICU"), never dialed unconverted.
  Fix round 1 (2026-11-10): hosts that `System.Uri` refuses (sub-delims
  other than `-` and `_`, such as `a!b`, and empty labels such as `a..b` or
  `1.2.3.4..`) are invalid with the grammar's warning (Go accepts them); the
  path reshaping by `System.Uri` (`/a/../v1` → `/v1`, `%7E` → `~`) is
  documented, not fixed; environment endpoint failures use Go's wording
  (`<why>; ignored`, the variable as an attribute), and the userinfo warning's
  `using` is the URL the signal uses; warnings are deduplicated by a hash of
  the raw value; `AuthenticationException: …` and other exception or error
  type names are no longer read as credential names by the SDK-text
  redaction; the IDN divergence is recorded in the contract (D1 row "IDN hosts:
  Python residual divergence") and the root README gap list. Suite 1038/1038
  on `sdk:8.0` and SDK 10.0.403 roll-forward, line coverage 98.5%.
  Fix round 2: that exemption is narrowed to a type name (`^[A-Z][A-Za-z0-9]*(Exception|Error)$`),
  `:` plus whitespace and a value that starts like prose, so `TokenError: <token>`,
  `AuthError=x` and `X-Auth-Error: x` stay masked. Suite 1046/1046 on `sdk:8.0` (line 98.51%)
  and SDK 10.0.403 roll-forward (98.45%).
- **.NET: OTLP exporters per signal on the wire (task C7b, contract R1, R2,
  D1 row "Path on a gRPC endpoint", A29).** A path on a gRPC endpoint is
  ignored (the target stays `host:port`) and warned once per variable, naming
  the variable only: `endpoint path is ignored by the gRPC exporter
  variable=OTEL_EXPORTER_OTLP_ENDPOINT` — for the generic and per-signal
  variables, code options and schemeless values; a bare `/` is no path, an
  HTTP path is not warned, and a value the resolution skipped is not warned.
  Code values only warn (a path is ignored, not invalid). The rest of the C7b
  scope (explicit generic, per-signal and code targets, `/v1/<signal>` for
  generic HTTP only, per-signal "configured", the TLS scheme rule, IPv6, path
  escapes) already passed with C7a; unit tests now also pin per-signal HTTP
  targets for metrics and logs. Parity: the last 4 `xfail:C7b` cases pass
  (`export.grpc_endpoint_path_ignored_warned`, `_per_signal_`, `_code_`,
  `_schemeless_`); the C7b group (55 cases) passes in full. Fixture: 239
  `pass`, 119 `xfail:C<n>`, 12 known-difference skips. Suite 1062/1062 on `sdk:8.0`
  and SDK 10.0.403 roll-forward, line coverage 98.45%.

- **.NET: exporter settings (task C8, contract A18, R2, R8, G10, ADR-013).**
  The helper resolves `OTEL_EXPORTER_OTLP[_<SIGNAL>]_HEADERS`, `_TIMEOUT`,
  `_COMPRESSION`, `_CERTIFICATE`, `_CLIENT_KEY` and `_CLIENT_CERTIFICATE` per
  signal (per-signal, then generic, then the default) and hands each exporter
  the result on its own `HttpClient`; `OtlpExporterOptions.Headers` stays
  empty. Headers follow the contract grammar (keys `[A-Za-z0-9._-]+`, not
  decoded; values decoded as a whole or kept literally; non-printable-ASCII
  values skipped; last key wins; warnings by item position, never a value); a
  per-signal value replaces the generic one; a gRPC `-bin` value is sent
  base64. The timeout is decimal milliseconds, clamped to [1, 86 400 000],
  invalid values warn and fall back; it is the deadline of the exporter, the
  client (`HttpClient.Timeout`), the batch span and log processors and the
  periodic reader, and gRPC sends it as `grpc-timeout`. `OTEL_BSP_EXPORT_TIMEOUT`,
  `OTEL_BLRP_EXPORT_TIMEOUT` and `OTEL_METRIC_EXPORT_TIMEOUT` are not honored
  and warn. Compression defaults to `gzip`. TLS files are read and validated
  by the helper (a CA with no PEM certificate, an unreadable file, an
  incomplete or rejected client pair: warned by variable, next level or
  server authentication); on a plaintext target they are ignored with one
  warning naming them. A batch size above the queue size
  (`OTEL_BSP_*`, `OTEL_BLRP_*`) warns naming both variables and the queue size
  is used. A value-length limit of 0 is warned; the .NET OTLP exporter has no
  public override and still sends those values empty (D-NET15 skip
  `no-upstream-dotnet-attribute-limit`).
  **Release blocker closed:** a `3xx` answer is no longer dropped silently —
  the helper client never follows a redirect and writes one ERROR per signal
  and status code (`the OTLP endpoint for traces answered HTTP 307 (a
  redirect); redirects are not followed …`, never the `Location` value);
  tests per signal against a local listener answering 301, 302, 307 and 308.
  Parity: the 37 remaining `xfail:C8` cases pass, the two value-length cases
  are `skip:no-upstream-dotnet-attribute-limit`, and a new case
  `tls.certificate_readable_with_plaintext_warns` (a readable CA on a
  plaintext target: the plaintext warning, not an unreadable-file one) passes
  in every language. Fixture: 371 cases; .NET 277 `pass`, 80 `xfail:C<n>`, 14
  known-difference skips. Suite 1143/1143 on `sdk:8.0` (line coverage 98.5%,
  branch 96.64%) and SDK 10.0.403 roll-forward (98.55%).
- **.NET: C8 fix round 1 (2026-11-10).** Owner decision: OTLP redirects are
  refused in every language (contract D1 row "OTLP export redirects", task
  A31). New cases `export.http_redirect_refused_warned` (.NET `pass`; Go,
  Python, Node.js `xfail:A31`; the fake receiver answers
  `/parity-redirect-<letter>/…` with a 3xx status and a `Location` holding a
  token) and `export.headers_bin_short_value_unpadded` (a 2-byte `-bin`
  value: Go, Python and .NET send `YWI`; Node's grpc-js sends `YWI=` and takes
  the D-JS14 skip `skip:no-upstream-node-bin-padding`, task A32). App-set export
  timeouts warn naming the option; the histogram path applies the deadline
  last; TLS files are read with a 1 MiB bound; the plaintext TLS warning names
  every signal; the SDK-text drop rule covers the per-signal OTLP keys; the
  batch clamp names the options when the values came from code; the client
  key is ephemeral on Linux. Found and left to task C12:
  `MeterProvider.ForceFlush()` returns `false` because of the pull-only
  Prometheus reader, even when the OTLP export succeeded.
  Fixture: 373 cases; .NET 279 `pass`, 80 `xfail:C<n>`, 14 known-difference
  skips. Suite 1155/1155 on `sdk:8.0` (line coverage 98.58%) and SDK 10.0.403
  roll-forward (98.53%).
- **.NET: C8 fix round 2 (2026-11-10).** `export.http_redirect_refused_warned`
  now matches the contract wording (fragment `redirects are not followed`
  with `signal=` and `status=` per signal; the receiver prefix is
  `/parity-redirect-c/`, letters a–d for 301/302/307/308, so the path holds no
  status) and a followed redirect fails `received`. The contract row is
  amended: one shared wording, both protocols (gRPC on an HTTP/2 3xx), once
  per process (once per container in .NET), and Node.js does not follow
  redirects (its exporter fails a 3xx), so for Node.js task A31 is only the
  helper line. `export.headers_bin_short_value_unpadded` keeps the exact
  unpadded `YWI` for Go, Python and .NET; Node's padded form is the D-JS14
  skip `skip:no-upstream-node-bin-padding` (task A32 closed as a recorded
  difference). An application timeout on the histogram path's named
  reader options is warned when the deadline replaces it. Measured for task
  C12: `MeterProvider.Shutdown()` also returns `false` with the always-registered
  Prometheus reader.
- **.NET: signals and exporters (task C9, contract A19, R2, R8, ADR-013).**
  `OTEL_TRACES_EXPORTER` and `OTEL_LOGS_EXPORTER` are read (`otlp`, `console`,
  `none`) and `OTEL_METRICS_EXPORTER` gains `console`; `logging` is a warned
  alias of `console` in the variables and fails in code; the console exporters
  are the SDK's (new dependency `OpenTelemetry.Exporter.Console` 1.20.1,
  Apache-2.0, the same OpenTelemetry .NET release as the core packages),
  constructed by the helper at provider build. An environment `otlp` without
  a metrics endpoint warns and falls back to the default. The deprecated
  `OTEL_HELPER_DISABLED_SIGNALS` no longer disables a signal whose standard
  exporter variable holds a valid entry (for metrics also a code
  `MetricExporters`); its unknown entries warn once each; a code
  `DisabledSignals` with an unknown signal fails start. `OTEL_METRIC_EXPORT_INTERVAL`
  follows the contract grammar (any length, clamped at 2 147 483 647, garbage
  warned). The helper parses `OTEL_METRICS_EXEMPLAR_FILTER` and always sets the
  filter (default `trace_based`; the .NET SDK default is `AlwaysOff`), checked
  by `ExemplarFilter_DefaultsToTraceBased_AndHonorsEnv` on the built provider.
  An unparseable `OTEL_SDK_DISABLED` warns naming the variable. Parity: 19 of
  the 20 `xfail:C9` cases pass; `config.disabled_signals_standard_var_wins`
  moves to `xfail:C16` (its precedence passes; the warning it asserts is the
  deprecation task C16 adds). The C9 group (36 cases) is 35 `pass` and 1
  `xfail:C16`. Fixture: 373 cases; .NET 298 `pass`, 61 `xfail:C<n>`, 14
  known-difference skips. Suite 1233/1233 on `sdk:8.0` (line coverage 98.6%)
  and SDK 10.0.403 roll-forward (98.65%); 13 mutants of the new rules each
  fail a test.
- **.NET: C9 fix round 1 (2026-11-10).** A code `DisabledSignals` or
  `MetricExporters` that is set but empty wins over the environment (new case
  `config.code_disabled_signals_empty_wins`, passing in Go, Python, .NET and
  Node.js). The SDK's own parse lines for `OTEL_METRIC_EXPORT_INTERVAL` and
  `OTEL_SDK_DISABLED` are no longer forwarded (the helper resolves and warns
  about both; `+1000` was accepted by the helper while the SDK's "invalid
  value" line still printed). The unknown-entry and "`none` combined" warnings
  of one exporter variable no longer share a once-key, so `bogus,none,otlp`
  prints both. New `resolve` snapshot field `exemplar_filter`, read from the
  installed provider in every driver (Go and .NET by reflection, Python from
  the provider's SDK configuration; Node.js has no exemplar filter and takes
  the permanent skip `skip:no-upstream-node-exemplar-filter`), and two cases:
  `metrics.exemplar_filter_default_trace_based` (Go, Python, .NET pass) and
  `metrics.exemplar_filter_env_always_off` (Go and .NET pass; Python
  `xfail:A33` — it passes `TraceBasedExemplarFilter()` and ignores the
  variable; new contract task A33, before the next Python release
  candidate). `config.disabled_signals_standard_var_wins` now asserts only
  the precedence (.NET `pass`); the deprecation warning moved to the new case
  `deprecation.disabled_signals_standard_var_set_still_warns` (.NET
  `xfail:C16`). Fixture: 377 cases; .NET 302 `pass`, 61 `xfail:C<n>`, 14
  known-difference skips. Suite 1251/1251 on `sdk:8.0` and SDK 10.0.403
  roll-forward (line coverage 98.6% on both).
- **.NET: C9 fix round 2 (2026-11-10).** An `OTEL_METRIC_EXPORT_INTERVAL`
  set only in `IConfiguration` (`appsettings.json`) is not read (ADR-013) and
  the SDK's parse line for it is dropped, so the helper now warns once
  naming the variable (`… is set only in IConfiguration and is not read`),
  for a valid and an invalid value alike (principle 8). An empty code
  `MetricExporters` writes one INFO line. The parity driver requires exactly
  one `ExemplarFilter` member on `MeterProviderSdk` (an auto-property and its
  backing field count as one). Suite 1255/1255 on `sdk:8.0` and SDK 10.0.403
  roll-forward (line 98.61%).
- **.NET: sampler, propagators and debug marker (task C10, contract R6, R8,
  R9, A20; ADR-002, ADR-006; design D-NET9, N2, N3).** The helper parses
  `OTEL_TRACES_SAMPLER`/`_ARG` and always sets the sampler (default
  `parentbased_always_on`, also when traces are off), and the deprecated
  `OTEL_HELPER_SAMPLE_RATIO` builds `parentbased_traceidratio` or warns. An
  `OTEL_TRACES_SAMPLER`, `_ARG` or `OTEL_PROPAGATORS` set only in
  `IConfiguration` warns once and is not read. `OTEL_PROPAGATORS` is parsed
  per token with the A20 diagnostics and installed with
  `Sdk.SetDefaultTextMapPropagator` when the helper's settings resolve (host
  start), so also under `OTEL_SDK_DISABLED=true`; without `tracecontext` the
  runtime's `DistributedContextPropagator.Current` is replaced by its
  no-output propagator (measured: `HttpClient` then sends neither
  `traceparent` nor `tracestate`), undone by a later container whose list has
  `tracecontext`, and an application's own runtime propagator is kept with
  one warning. Debug mode sets only the `debug=true` attribute, on spans with
  `ParentSpanId == default` or `HasRemoteParent` (new
  `Tracing/DebugMarkerProcessor.cs` replaces `DebugTraceStateProcessor.cs`).
  `StartRootActivity` restores the previous `Activity.Current` when the root
  stops (the runtime's `_previousActiveActivity` field, set by reflection;
  checked on .NET 8 and 10). Decision: with traces off the provider keeps the
  resolved sampler — `Traces_Off_Server_Passes_The_Inbound_Trace_On` shows
  the inbound trace id and sampled flag reaching the next `HttpClient` call,
  while an `AlwaysOffSampler` would forward `00`. Parity: the 12 `xfail:C10`
  cases pass (C10 group: 32 `pass`); the ParentBased default also makes
  `http.health_child_span_dropped` (C11 group) pass. Fixture: 377 cases; .NET
  315 `pass`, 48 `xfail:C<n>` (C11 12, C12 26, C13 3, C16 7), 14
  known-difference skips. Suite 1336/1336 on `sdk:8.0` and SDK 10.0.403
  roll-forward (line coverage 98.55%); 16 mutants of the new rules each fail
  a test.
- **.NET: C10 fix round 1 (2026-11-10).** The runtime propagation path follows
  `OTEL_PROPAGATORS` in both directions: a helper `DistributedContextPropagator`
  (delegating to the runtime default, dropping what is not listed) is
  `DistributedContextPropagator.Current` for `HttpClient` and replaces the
  container's `DistributedContextPropagator` service ASP.NET Core extracts
  with (measured: the web host's `TryAddSingleton` runs before the
  application's services in `WebApplication`, before or after in `HostBuilder`;
  the replacement wins in all three). With `none` or `baggage` an inbound
  `traceparent` now starts a new trace; with `tracecontext` no `baggage` is
  sent on; the default list is unchanged. Traces disabled
  (`OTEL_HELPER_DISABLED_SIGNALS`, `DisabledSignals`) and `OTEL_SDK_DISABLED`
  propagate as a no-op tracer: a root sends no trace context, an inbound one is
  passed on unchanged (sampler `ParentBased(root: AlwaysOff)`; under the
  disabled SDK a non-recording listener lets ASP.NET Core continue an inbound
  trace without logging); `OTEL_TRACES_EXPORTER=none` keeps the SDK behavior.
  New contract row "Propagation with traces off" and parity snapshot field
  `server_continues_inbound` (`http_server`, all four drivers); five cases
  `propagation.default_server_continues_inbound_traceparent`,
  `propagation.none_server_ignores_inbound_traceparent`,
  `propagation.baggage_only_server_ignores_inbound_traceparent`,
  `propagation.tracecontext_only_drops_baggage`,
  `propagation.traces_disabled_root_no_traceparent` — Go and .NET pass all
  five; Python `xfail:A34` on the `none` server case (with
  `OTEL_PROPAGATORS=none` the helper's empty propagator returns no context and
  the ASGI request fails; new contract task A34, before the next Python
  release candidate; the Python parity driver now records a failed request as
  a 500 instead of crashing); Node.js `xfail:A35` on the disabled-traces case
  (a root still injects `traceparent`; new contract task A35). Fixture: 382
  cases; .NET 320 `pass`, 48 `xfail:C<n>`, 14 skips. Suite 1364/1364 on
  `sdk:8.0` and SDK 10.0.403 roll-forward (line coverage 98.44%); 9 more
  mutants each fail a test.
- **.NET: C10 fix round 2 (2026-11-10).** `AddOtelHelper` already sets
  `DistributedContextPropagator.Current` (from `OTEL_PROPAGATORS`, environment
  only), so an `HttpClient` created before the host starts follows the list;
  the disabled-traces part is added at host start. A `Current` the application
  sets after `AddOtelHelper` is kept for later handlers and warned; a
  `DistributedContextPropagator` service the application registered is kept,
  with a warning that ASP.NET Core extracts with it. Pass-through uses only a
  remote parent (`HasRemoteParent`). With traces or the SDK disabled the API's
  expected "Failed to inject activity context … 'Invalid context'" line is
  not forwarded. Parity drivers (Go, Python, Node.js, .NET): `http_server`
  records `response_statuses`, and any 5xx is an adapter error unless the case
  sets `input.allow_server_error` (only
  `propagation.none_server_ignores_inbound_traceparent`, which also expects
  `[200]`). Suite 1371/1371 on `sdk:8.0` and SDK 10.0.403 roll-forward (line
  coverage 98.37%).
- **.NET: HTTP/gRPC instrumentation and health (task C11, contract R7, R10,
  D5, A22, A30; requirement DN5, DN7; design D-NET8, N1, N5; implemented
  2026-11-10, awaiting review).** New `Tracing/HealthMatching.cs`: the
  contract default list, the D5 normalization (cut at `?`/`#` on the raw
  target, strict percent-decoding — a malformed escape or non-UTF-8 bytes keep
  the raw path —, cut again, ASCII lowercase, trailing slashes) and the whole
  `grpc.health.v1.Health/` service, used by the ASP.NET Core `Filter` (on the
  raw request target, `IHttpRequestFeature.RawTarget`, since Kestrel keeps
  `%2F` encoded in `HttpRequest.Path`), the HttpClient
  `FilterHttpRequestMessage` and the gRPC client `Filter`.
  `TelemetryOptions.HealthPaths` is validated as in Go, Python and Node.js.
  `GrpcClientTraceInstrumentationOptions.SuppressDownstreamInstrumentation =
  true` gives one CLIENT span per gRPC call (measured with HTTP
  instrumentation 1.20.1: the HttpClient `POST` span is gone and the server
  span's parent is the gRPC client span). New `Tracing/GrpcServerTracingCheck.cs`
  (O7 (b)): at `ApplicationStarted`, when traces are exported, gRPC endpoints
  are mapped (endpoint metadata `Grpc.AspNetCore.Server.GrpcMethodMetadata`,
  by type name; the name verified at Grpc.AspNetCore.Server 2.62.0) and
  `OTEL_DOTNET_EXPERIMENTAL_ASPNETCORE_ENABLE_GRPC_INSTRUMENTATION` in the app
  `IConfiguration` is not `true` (`bool.TryParse`, as the instrumentation
  reads it), one warning. `OTEL_HELPER_HTTP_OPT_IN_METRICS` / `HttpOptInMetrics`
  (A30): a view drops the opt-in names of the `Microsoft.AspNetCore.Hosting`
  and `System.Net.Http` meters unless the switch is on (one INFO line then).
  Meters `<ServiceName>.*` are subscribed (the SDK wildcard; also dropped by
  the idle view and quiet in the SDK "not subscribed" filter). The parity
  driver maps the code key `http_opt_in_metrics` and writes the snapshot field
  `http_opt_in_metrics`; it already registered the `parity` meter through
  `AdditionalMeters`. The `dotnet-backend` example sets the O7 key in its
  `appsettings.json`. Parity: the 12 `xfail:C11` cases pass (C11 group: 25
  `pass`, the two `no-upstream-rpc-metrics` and three
  `no-upstream-dotnet-http-body-size` skips stay). Fixture: 382 cases; .NET
  332 `pass`, 36 `xfail:C<n>` (C12 26, C13 3, C16 7), 14 known-difference
  skips; only `status.dotnet` changed. Tests: `HttpGrpcInstrumentationTests`
  (114 cases); 11 mutants of the new rules each fail a test. Suite 1485/1485
  on `sdk:8.0` and SDK 10.0.403 roll-forward (line coverage 98.44%).
- **.NET: C11 fix round 1 (2026-11-10).** An HttpClient call to a health path
  is sent without trace context, as Go's `NewHTTPTransport` (before, the
  downstream received `traceparent …-01` naming a span that is never
  exported): the HttpClient filter removes the context headers the
  instrumentation injected, and the helper's runtime propagator writes none
  for such a request. This corrects the last sentence of .NET migration
  telemetry row 20 ("still sends a `traceparent` with the sampled flag
  cleared"): the measured header was sampled (`-01`), and nothing is sent now.
  The server filter decodes an absolute-form target once (`PathBase + Path` is
  already decoded: `GET http://host/%2568ealth` is traced). The .NET parity
  driver makes `grpc.health.v1.Health/*` calls from an uninstrumented client
  (no client span, no `traceparent`, as a kubelet or `grpc_health_probe`), so
  `grpc.health_check_no_span` and `grpc.health_watch_no_span` need the server
  filter; the driver keeps enabling the O7 key, so the `grpc.server_*` cases
  describe a correctly configured app. Documented: options registered after
  `AddOtelHelper()` replace the helper's filters (HOW-TO §9); .NET framework
  series beyond the two HTTP durations and gRPC calls counted as HTTP (root
  README gap rows); the gRPC key and metadata type in the release skill's .NET
  SDK-bump checklist. Fixture unchanged. Suite 1498/1498 on `sdk:8.0` (line
  coverage 98.49%) and SDK 10.0.403 roll-forward (98.42%); 3 more mutants each
  fail a test.
- **.NET: C11 fix round 2 (2026-11-10).** The HttpClient health filter
  removes only the trace context the propagators wrote (a `traceparent` or
  `Request-Id` naming the client activity, a `tracestate` equal to its trace
  state): an application's own `baggage` or `tracestate` on a health request
  is kept, and the runtime propagator still writes baggage on it (round 1
  removed every context header). With traces disabled the pass-through
  propagator writes no trace context on a health request either. This narrows
  round 1's statement to "a sampled client call": an unsampled health call
  keeps `traceparent …-00` (it names no exported span). The health list the
  runtime propagator checks is process-wide (the last container wins), as the
  C10 propagation settings. Parity drivers: the Node.js driver makes
  `grpc.health.v1.Health/*` calls under `suppressTracing`, so
  `grpc.health_check_no_span` and `grpc.health_watch_no_span` need the
  server-side drop in Node.js too (measured: they passed on the unsampled parent
  alone before; Python already needed it). Fixture unchanged. Suite 1502/1502
  on `sdk:8.0` (line coverage 98.39%) and SDK 10.0.403 roll-forward (98.31%);
  3 more mutants each fail a test.
- **.NET: Prometheus fallback (task C12, contract A21, A29, N8; ADR-007;
  design D-NET11, D-NET3; implemented 2026-11-11, awaiting review).** Port:
  code > `OTEL_EXPORTER_PROMETHEUS_PORT` > `OTEL_HELPER_METRICS_PORT` > 9464,
  both variables on the A21 grammar (`TelemetryResolver.TryParseMetricsPort`),
  an invalid value warned once with the echo redacted; the unparseable
  `OTEL_HELPER_METRICS_PORT` is no longer silent. Host:
  `OTEL_EXPORTER_PROMETHEUS_HOST` parsed as Go's `parsePrometheusHost`
  (`TryParsePrometheusHost`: strict dotted quad, IPv6 without zone, IPv4-mapped
  → IPv4, all-zero → every interface, `localhost` → `127.0.0.1`) and bound
  through the listener prefix (`http://<ip>:<port>/metrics/`, `+` for every
  interface and `0.0.0.0`); an IPv6 host is one ERROR line and a failed build
  (no IPv6 bind in the managed `HttpListener`, measured). The listener reader
  is added at provider build only when it binds: `AddPrometheusHttpListener()`
  without a configure delegate registers no service, only a reader factory, so
  the deferred step can call it (revises D-NET3; it also drops the idle
  reader's second aggregation of every series, measured at about 1.9× the
  managed heap in the C5 note). With it, `MeterProvider.ForceFlush()` and
  `Shutdown()` return `true` in an OTLP-only setup (were `false`, the C8
  release blocker). New public `TelemetryFlushExtensions.ForceFlushOtelHelper`
  for dual mode: tracer and logger provider flushes plus a `Collect` of each
  push reader the helper built (OTLP, console); on the exponential-histogram
  path it uses `MeterProvider.ForceFlush()` (the SDK builds that reader).
  `OTEL_HELPER_DISABLED_METRICS` follows the contract glob (case-sensitive,
  `?`, literal rest, contract trim; `MetricsSetup.DisabledMetricPatterns`).
  Parity driver: snapshot field `prometheus_metrics_host`. Parity: of the 26
  `xfail:C12` cases, 23 pass and 3 are upstream exposition residuals —
  `metrics.scrape_gauge_unit_one_ratio` and `metrics.scrape_ratio_inside_name`
  `skip:no-upstream-dotnet-prom-ratio` (no `_ratio` for a unit-`1` Gauge),
  `metrics.scrape_colliding_label_values_sorted`
  `skip:no-upstream-dotnet-prom-label-order` (values joined in key order,
  `a_b="y;x"`); the exporter's serializer is internal and a View rename would
  rename the OTLP stream too. Fixture: 382 cases; .NET 355 `pass`, 10
  `xfail:C<n>` (C13 3, C16 7), 17 known-difference skips; only
  `status.dotnet` changed. `testing/e2e/dotnet-prom-scrape.sh` adds the
  bind-host negative control: a second app with
  `OTEL_EXPORTER_PROMETHEUS_HOST=127.0.0.1` answers on loopback inside its
  container and refuses another container (curl exit 7; exit 0 on the
  pre-C12 tree). Tests: `PrometheusFallbackC12Tests` (non-loopback connect
  refused with `127.0.0.1`, answered when unset); 7 mutants of the new rules
  each fail a test. Suite 1598/1598 on `sdk:8.0` (line coverage 98.41%) and
  SDK 10.0.403 roll-forward (98.44%).
- **.NET: C12 fix round 1 (2026-11-11).** Loopback: a `127.0.0.1` bind (also
  `localhost`) registers `http://127.0.0.1:<port>/metrics/` and
  `http://localhost:<port>/metrics/`, so a Go sidecar (Prometheus, vmagent)
  scraping `localhost` gets 200 (it got 404: the `HttpListener` routes by
  `Host`); still loopback only (measured: another container's connect is
  refused). `localhost` binds the resolver's first address (`::1` when the host
  has an IPv6 loopback, `127.0.0.1` with IPv6 off, measured on .NET 8.0.33 and
  10.0.14); Go and .NET `HttpClient` dial `::1` first. Residual (measured):
  curl and Node.js resolve with `AI_ADDRCONFIG`, so in a container whose only
  IPv6 address is `::1` they connect to `127.0.0.1` with `Host: localhost` and
  get 404; scrape `127.0.0.1` with them. IPv6 (coordinator decision, never widen, never fail start for an env
  value): `::1` → `127.0.0.1` with one warning, `::` → every IPv4 interface with
  one warning, any other IPv6 address → no listener and one ERROR (was a failed
  build). A failed bind writes one ERROR naming the address, the port and what
  set them before the upstream exception. The helper's reader and listener
  options are named `StaffOps.OtelHelper.Prometheus`, so an application's own
  `AddPrometheusHttpListener()` runs beside it. `OTEL_EXPORTER_PROMETHEUS_PORT`
  and `_HOST` join the helper-owned SDK "invalid value" noise (no SDK line for
  `+9464`, one line for `abc`). The glob anchors with `\z`. The two
  non-loopback bind tests are skipped with a reason offline.
  `ForceFlushOtelHelper` documents `ObjectDisposedException`. New parity case
  `metrics.listener_prometheus_host_ipv6_loopback` and the optional
  `metrics_listener` input `connect_host` (`127.0.0.1` or `localhost`) in the
  four runners; the case passes in Go, Python, Node.js and .NET (.NET through
  the `127.0.0.1` mapping). Root README gap rows and contract notes (Prometheus
  host row, D13 `.NET flush result`).
- **.NET: C12 round 2 (2026-11-11).** The bind-failure ERROR is written only
  when the exporter's exception wraps an `HttpListenerException` (a busy port,
  errno 98, and an address this machine does not own, 99, measured on .NET
  8.0.33 and 10.0.14), and echoes its OS text; another failure of the listener
  reader's factory (an options `Configure` that throws) propagates without it.
- **.NET: Logging (task C13, contract D6, R4, A23; design D-NET10, DN8, N9;
  implemented 2026-11-11, awaiting review).** Stdout fallback: with no logs
  exporter active (no endpoint, `OTEL_LOGS_EXPORTER=none`, logs disabled,
  `OTEL_SDK_DISABLED=true`, an unusable endpoint) the new provider
  `Logging/JsonLineLoggerProvider.cs` (alias `OtelHelperStdout`) writes one
  JSON object per line on standard output — `time` (UTC, milliseconds, `Z`),
  `level` (`DEBUG`/`INFO`/`WARN`/`ERROR`), `msg`, `logger` (the category), the
  state values, `exception` — with the reserved keys dropped (A23); it writes
  nothing while OTLP or `console` is active. Before, the OpenTelemetry provider
  had no processor there and the records were lost unless the host's console
  provider printed them. The level (R4) and the `Microsoft` /
  `System.Net.Http` Error rules are now provider-specific rules on the
  helper's two providers only (`LoggingSetup.ApplyHelperRules`, a
  post-configure step); `LoggerFilterOptions.MinLevel` and the application's
  own providers and rules are no longer changed, the application's category
  rules still reach the helper's providers, and the idle helper provider is
  held at `None`. Fix round 1 (2026-11-11, owner decision): the application's
  explicit default (`Logging:LogLevel:Default`, `AddFilter(null, level)`,
  `SetMinimumLevel`, a rule for the helper's provider) wins over the R4
  environment default, code `MinimumLogLevel` over everything; one warning at
  host start when the fallback writes next to the host's console provider;
  `JsonLine.Format` inside the logger's guard with a minimal fallback line;
  lone surrogates written as U+FFFD; characters outside the BMP raw UTF-8
  when `Console.OutputEncoding` is UTF-8 (escaped otherwise, round 2);
  collections capped at 1000 items; new case `logging.stdout_logger_field`
  (Go `skip:no-logger-name`, A23 (b)). The Profiling logger category stays `OtelHelper.Profiling` (decision
  2026-11-11: C15 moves that warning to the diagnostics sink). Migration
  guide: telemetry item 22. Parity: the 3 `xfail:C13` cases pass; new cases
  `logging.stdout_line_shape` and `logging.stdout_reserved_keys_dropped` and
  the `logging` kind input `attributes` in the four runners (pass in Go,
  Python, Node.js and .NET); 386 cases, .NET 362 `pass`, 7 `xfail:C16`, 17
  known-difference skips. The .NET driver's `log_level` reads the level a
  logger enables instead of `MinLevel`. Tests: `StdoutLoggingTests`; 12
  mutants, plus the fix-round ones, each fail a test. Suite and coverage:
  `dotnet/TESTS.md`.

### Breaking — telemetry output changes (dashboards/alerts)

Go (otel v1.47 / contrib v0.72) and Python (lockstep bounds) change what
reaches the backend. Review dashboards, recording rules and alerts before
upgrading.

| Signal | 0.2.0 | 0.3.0 | Escape hatch |
|---|---|---|---|
| **BREAKING** Go runtime metrics | `process.runtime.go.*` (`goroutines`, `gc.count`, `gc.pause_ns`, `mem.heap_*`), `runtime.uptime` | `go.memory.used`, `go.memory.limit` (only when `GOMEMLIMIT` is set), `go.memory.allocated`, `go.memory.allocations`, `go.memory.gc.goal`, `go.goroutine.count`, `go.processor.limit`, `go.config.gogc` | `OTEL_GO_X_DEPRECATED_RUNTIME_METRICS=true` emits the old `process.runtime.go.*` / `runtime.uptime` set **in addition to** the new one (additive in contrib `runtime` v0.72) |
| **BREAKING** Go HTTP server / client metrics | `http.server.duration` / `http.client.duration` (ms; `http.method`, `http.status_code`, `net.host.name`) | `http.server.request.duration` / `http.client.request.duration` (s; `http.request.method`, `http.response.status_code`, `server.address`) | none: otelhttp v0.72 emits only the stable semconv |
| **BREAKING** Go HTTP size metrics | `http.server.request.size`, `http.server.response.size`, `http.client.request.size`, `http.client.response.size` (counters) | `http.server.request.body.size`, `http.server.response.body.size`, `http.client.request.body.size` (histograms); `http.client.response.size` has no replacement | none |
| **BREAKING** Go gRPC metrics | `rpc.server.duration` / `rpc.client.duration` (ms; `rpc.system`, `rpc.service`, `rpc.method="Method"`, `rpc.grpc.status_code` int) | `rpc.server.call.duration` / `rpc.client.call.duration` (s; `rpc.system.name`, `rpc.method="pkg.Svc/Method"`, `rpc.response.status_code`) | `OTEL_SEMCONV_STABILITY_OPT_IN=rpc/dup` (both sets) or `rpc/old` (otelgrpc v0.72); `rpc/old` is not a full restore: the status attribute is `rpc.response.status_code` (string) even then |
| **BREAKING** Go `ext/otelaws` attributes | semconv v1.21 (`rpc.system` + `rpc.service`) | semconv v1.43 (`rpc.system.name`) | none |
| **BREAKING** Go HTTP server span names | the `operation` argument of `NewHTTPHandler` | `<METHOD> <route>` from the `http.ServeMux` pattern (`GET /orders/{id}`), or `<METHOD>` without a pattern | none; `dashboards/03-traces-reliability.json` groups by span name, so those series break |
| Go gRPC health RPCs | `Check` traced and counted in `rpc.server.duration` (the server filter never matched); `Watch` and all client RPCs emitted no metrics; client-side health spans dropped | `Check` and `Watch` counted in `rpc.server.call.duration` (only `Watch` counting is new), their server spans dropped by the health sampler (A7-go); client RPCs counted in the new `rpc.client.call.duration`, except health calls (no client span, no client metric) | exclude `rpc_method=~"grpc.health.v1.Health/.*"` in SLO queries (long-lived `Watch` streams inflate tail latency) |
| **BREAKING** Go HTTP health requests | `/ping /health /healthz /ready` (exact) through `NewHTTPHandler`: no span **and no metric** | `/health /healthz /ready /readyz /live /livez /ping /metrics` (exact, case-insensitive, trailing slash and query ignored): no server span, but **counted** in `http.server.request.duration` | exclude health routes (`http_route` / probe paths) in SLI queries; `WithHealthPaths(...)` changes the list |
| Go resource `schema_url` | `https://opentelemetry.io/schemas/1.26.0` | `https://opentelemetry.io/schemas/1.43.0`, the contract literal shared with Python and Node.js (the helper builds its resource with `resource.NewWithAttributes`, no merge with `resource.Default()`) | — |
| Python resource | — | on sdk ≥ 1.45 the SDK adds `service.instance.id` (one value per process: a new label per restart where resource attributes become labels) | — |
| **BREAKING** Python Prometheus names of Gauges in unit `1` (A21) | `system_cpu_utilization` (no suffix) | `system_cpu_utilization_ratio`, as Go (OTel Prometheus compatibility spec); OTLP names unchanged | none; update the PromQL |
| **BREAKING** Python resource `schema_url`, `telemetry.sdk.*`, extra detectors (A16) | no schema URL; `telemetry.sdk.*` overridable through `OTEL_RESOURCE_ATTRIBUTES`; `OTEL_EXPERIMENTAL_RESOURCE_DETECTORS` attributes (`process.*`, `os.*`, `host.*`, …) | `https://opentelemetry.io/schemas/1.43.0`; `telemetry.sdk.*` always the SDK's; detector attributes gone (warned) | add host/process attributes in the Collector (`resourcedetection`) |
| **BREAKING** gRPC span names (A30) | Python `/pkg.Svc/Method`, Node.js `grpc.pkg.Svc/Method` | `pkg.Svc/Method` in every language (semconv `{rpc.method}`; Go unchanged) | none |
| **BREAKING** Go Redis span names (A30) | `set`, `redis.pipeline set get`, `redis.dial` | `SET`, `SET GET`, `connect` (as Python and Node.js; `db.operation.name` stays `set`) | none |
| **BREAKING** AWS attribute values (A30) | Go SNS `messaging.system=aws_sns`, Go SQS `server.address=<queue URL>`, Python SQS `messaging.system=aws.sqs`, Node.js `db.system.name=dynamodb` | `aws.sns`, the request host (+ `server.port`, every Go AWS span), `aws_sqs`, `aws.dynamodb`; DynamoDB `db.system=dynamodb` in every language unless the stable database mode | `OTEL_SEMCONV_STABILITY_OPT_IN=database` drops the legacy `db.system` only |
| **BREAKING** HTTP opt-in metrics (A30, 2026-11-06) | Go: `http.{server,client}.request.body.size`, `http.server.response.body.size`; Python: also `http.server.active_requests` and the old `*.size` names | only the two durations by default | `OTEL_HELPER_HTTP_OPT_IN_METRICS=true` (code: `WithHTTPOptInMetrics`, `http_opt_in_metrics`, `httpOptInMetrics`) |
| **BREAKING** Go AWS RPC keys (A30, 2026-11-06) | `rpc.system.name=aws-api`, `rpc.method=<Service>/<Operation>` (0.3.0 release candidates) | legacy `rpc.system`, `rpc.service`, `rpc.method=<Operation>` (as Python and Node.js); SNS `messaging.operation.name` `Publish` (was `publish_input`) | `OTEL_SEMCONV_STABILITY_OPT_IN=rpc` (current keys) or `rpc/dup` (both) |
| **BREAKING** Python AWS span names and kinds (A30, 2026-11-06/08) | SNS publish `<topic> send`; SQS spans CLIENT; portless `http://` endpoint `server.port=443` | `SNS.Publish`; SQS send PRODUCER, receive CONSUMER; `server.port=80` | none |
| **BREAKING** Node.js AWS span names (A30, 2026-11-06/08) | `<queue> send` / `<queue> receive` / `<topic> send` / `<function> Invoke` (kinds PRODUCER/CONSUMER unchanged) | `SQS.SendMessage`, `SQS.ReceiveMessage`, `SNS.Publish`, `Lambda.Invoke` | none |
| **BREAKING** Go AWS span kinds (A30, 2026-11-08) | every call CLIENT | SQS/SNS send and publish PRODUCER, SQS receive CONSUMER, others CLIENT | none |
| Trace-derived telemetry (A30, all languages) | service-graph edges, spanmetrics `span_name` / `span_kind` series, Collector `filter` / tail-sampling rules and TraceQL keyed on the old names or kinds; Go DynamoDB node `server="aws.dynamodb"`; alerts and KEDA/HPA triggers on Python `http_server_active_requests` and the body-size series | see "Trace-derived telemetry" in each language's migration item 30 and the Node.js note under Internal | update the rules; `absent()`-free alerts go silent on removed series |

Python HTTP metrics switch to the stable semconv too in 0.3.0 (R10/D12 — see
the Python section below), so both languages emit `http.server.request.duration`
in seconds. Python gRPC emits no RPC metrics (upstream
`opentelemetry-instrumentation-grpc`), so RPC latency queries apply to Go
services only.

Span-name queries: filter on `http.route` instead, or on
`name =~ "<METHOD> /…"`. The shared dashboards in `dashboards/` query only
application-defined metrics, Collector `spanmetrics` and custom span names, so
none of the renamed metric names above appear in them; the "Span Duration P95
by Operation" panel groups by `span_name` and shows the new Go HTTP span names
after the upgrade.

A30 renames more spans (see the table above): gRPC `pkg.Svc/Method` in Python
and Node.js, Go Redis `SET` / `SET GET` / `connect`, Python and Node.js AWS
`<Service>.<Operation>`. A panel or spanmetrics dimension on `span_name` shows
the new names after the upgrade, and series on the old names stop; the same
for `span_kind` on Go SQS/SNS spans and Python SQS spans (a Python SNS publish
was already PRODUCER). Collector `filter` and
tail-sampling rules, TraceQL queries and alerts on the old names or kinds stop
matching without an error. The repo's dashboards are safe: no panel uses a
renamed span or a dropped metric.

### Go — detailed changes

- **A30 owner rulings of 2026-11-06 (design D1 "HTTP opt-in metrics", "AWS
  RPC keys", "AWS service attribute values").** `Options.HTTPOptInMetrics` /
  `WithHTTPOptInMetrics(bool)` / `OTEL_HELPER_HTTP_OPT_IN_METRICS` (code >
  env > false; the contract boolean grammar): off, an SDK view
  (`httpOptInMetricsView`) drops the semconv opt-in HTTP metrics from every
  scope — otelhttp's body sizes in practice (otelhttp v0.72 has no
  active-requests instrument). `ext/otelaws` rewrites otelaws's span-start
  RPC keys by `OTEL_SEMCONV_STABILITY_OPT_IN` (`rpcAttributes`: legacy by
  default, `rpc` current, `rpc/dup` both with the current `rpc.method`), sets
  the kind of SQS/SNS send and publish (PRODUCER) and SQS receive (CONSUMER)
  at `Start`, and writes `messaging.operation.name` = the AWS operation on SQS
  and SNS spans (`publish_input` → `Publish`). The opt-in view is scoped to
  the contrib instrumentation scopes, so an application's own instrument of
  those names is kept. Tests: `a30_test.go`, `ext/otelaws` `TestRPCModes`,
  `TestSNSOperationName`, `TestMessagingSpanKinds`.

- **A30 instrumentation parity backlog (design D1 rows "Redis span names",
  "AWS service attribute values", "URL query redaction", "gRPC span names",
  amended or added 2026-11-05).** `ext/otelredis` renames redisotel's spans in
  `sanitizingTracer.Start` (`spanName`): the uppercased command, a pipeline's
  uppercased command names, `connect` for the dial span; `db.operation.name`
  is taken before the rename. `ext/otelaws` maps `messaging.system` `aws_sns`
  → `aws.sns`, drops otelaws's SQS queue-URL `server.address`, writes
  `server.address`/`server.port` of the finalized request on every call
  (`serverAddressMiddleware`: the URL's port, else 443/80 by scheme) and the
  legacy `db.system=dynamodb` on DynamoDB spans in the legacy-keys mode
  (`OTEL_SEMCONV_STABILITY_OPT_IN`, the `ext/otelsql` rule). The URL query
  redaction list gains `access_token`, `X-Goog-Credential` and
  `GoogleAccessId` (ten names; `RedactedQueryParams()` returns them). gRPC
  span names unchanged (already `pkg.Svc/Method`). Tests:
  `ext/otelredis` `TestSpanName`, `TestSpanNamesAndOperationName`;
  `ext/otelaws` `TestA30AttributeValues`, `TestMapAttributes`,
  `TestServerAttributes`; `urlredact_test.go`.

- **A29 fix round (2026-11-05; three reviews, no BLOCKING).** The masked
  forms now include each space-separated part of a configured header value of
  4+ characters (the token of `Bearer <token>`: an auth proxy echoing only the
  credential leaked it), the form-encoded form and the HTML-escaped JSON form
  JSON-escaped again; all forms are replaced in one pass, longest first. The
  handler receives a sanitized chain: every `errors.Unwrap` level has its own
  sanitized text, `errors.Is` checks the original, `status.FromError` gives the
  code and the sanitized text without details, and `errors.As` into the SDK's
  own error types no longer finds them. An `(empty)` body is 0 bytes; every
  flattened `body: ` is cut; `releaseOnFailure` logs a sanitized error. An
  invalid `OTEL_EXPORTER_PROMETHEUS_HOST` now warns `invalid host, not applied:
  the listener binds every interface, wider than a restriction`, and echoed
  invalid host/port values lose URL userinfo and query. `::ffff:a.b.c.d` binds
  as IPv4. Tests: `go/a29_test.go` (`TestA29SanitizedChainHidesBodiesAndValues`,
  `TestA29MaskFormsAndParts`, `TestA29FailedSetupLeavesTheHandler`,
  `TestA29SkippedEndpointPathNotWarned`, …). The "SDK errors" section of the Go
  HOW-TO moved to §17 Troubleshooting.
- **Upgrade note — A29 Prometheus variables (Go, Python and Node.js).** Grep
  your manifests for `OTEL_EXPORTER_PROMETHEUS_` before upgrading: these
  variables were ignored until now and are honored from this release.
  `OTEL_EXPORTER_PROMETHEUS_HOST=localhost` (a common copy from upstream
  examples) now binds `127.0.0.1` only and **breaks pod-to-pod scraping**; a
  stray `OTEL_EXPORTER_PROMETHEUS_PORT` (for example set for a .NET sibling)
  moves the listener away from the port your scrape config uses; and
  `OTEL_EXPORTER_PROMETHEUS_PORT=0` now disables the listener even when
  `OTEL_HELPER_METRICS_PORT` is set.
- **A29 SDK error text, gRPC endpoint paths, Prometheus variables (design D1
  rows of 2026-11-05 "SDK error text: response bodies and header values",
  "Path on a gRPC endpoint", "Prometheus exporter variables"; not yet
  independently reviewed).** `Setup` installs an OTel error handler
  (`errorhandler.go`) that replaces an OTLP/HTTP response body in SDK errors
  by `body: <response body omitted: N bytes>` and masks the values of
  `OTEL_EXPORTER_OTLP[_<SIGNAL>]_HEADERS` (4 characters or more; raw,
  decoded, JSON-escaped once and twice, `strconv.Quote` form) as
  `<redacted>`; the original error stays reachable with `errors.Unwrap`. An
  application handler set before `Setup` is wrapped and receives the
  sanitized error; without one the helper writes `otelhelper: OpenTelemetry
  SDK error` on its stderr logger (the `OTEL_RESOURCE_ATTRIBUTES` echo filter's
  `log.Print` fallback now writes there too); a handler set after `Setup`
  replaces it and gets raw errors (HOW-TO §17, "SDK errors"). `Shutdown` restores the
  handler it found and returns a sanitized error. A path on a gRPC endpoint
  (env generic, per-signal or code) is ignored with the warning `endpoint
  path is ignored by the gRPC exporter` (variable only). New
  `EnvPrometheusPort` / `EnvPrometheusHost`: `OTEL_EXPORTER_PROMETHEUS_PORT`
  beats `OTEL_HELPER_METRICS_PORT` (code still wins, `0` = no listener,
  invalid → warned, next level); `OTEL_EXPORTER_PROMETHEUS_HOST` is the
  listener's bind address (IP literal or `localhost`; an IPv4 literal binds
  `tcp4`; unset or invalid = every interface). Tests: `go/a29_test.go`.
- **BREAKING (narrow) — A28 URL query redaction (design D1 row "URL query
  redaction" of 2026-11-03; migration item 28).** `NewHTTPTransport` passes
  otelhttp a tracer provider wrapper (`urlredact.go`, resolving the global
  provider at each span start) whose spans write the values of
  `RedactedQueryParams` as `REDACTED` in `url.full`, names matched exactly,
  every other byte kept. Tests: `TestRedactQuery`,
  `TestNewHTTPTransportRedactsPresignedURL`. *Fix round (2026-11-03):* the
  provider is the request span's `TracerProvider()` when there is one (as
  otelhttp); `RedactedQueryParams()` returns a copy; a listed name without
  `=` is kept.
- **A27 fix round (2026-11-03).** `ext/otelaws`: `error.type` falls back to
  the modeled error's type name when `ErrorCode()` is not PascalCase
  (`QueueDoesNotExist`); the call context keeps the SDK span and the code
  travels in a private context value; `aws.s3.key`, `aws.s3.copy_source`
  and `aws.s3.delete` are dropped; `Instrument` is documented as once per
  config. Tests: `TestContextKeepsSDKSpan`, `TestErrorCode`, HeadObject 404
  and SQS cases in `TestErrorStatusDropsDescription`; HEAD 403/503
  (`Forbidden`, `ServiceUnavailable`) cases.
- **BREAKING (narrow) — A27 AWS error status (design D1 rows "AWS error
  status", "AWS payload attributes", "AWS RPC keys" of 2026-11-03; migration
  item 27).** `ext/otelaws` passes contrib otelaws v0.72 a tracer wrapper
  (`WithTracerProvider`): a failed call's span keeps Error with an empty
  description (otelaws wrote `err.Error()`, `aws.go:76-79`) and `error.type`
  is the AWS error code from `smithy.APIError.ErrorCode()` (an inner
  Initialize middleware) when it matches `^[A-Z][A-Za-z0-9]{1,63}$`, else
  `_OTHER` (was the Go error type). The same rule in Python and Node.js. Go
  records no DynamoDB item collection metrics and no SNS phone number (no
  change). `service/s3`, `service/dynamodb` and `smithy-go` became direct
  requirements of the `ext/otelaws` module (test and middleware). Tests:
  `TestErrorStatusDropsDescription` (httptest fake: NoSuchKey, AccessDenied,
  a ValidationException quoting the item), `TestErrorType`,
  `TestSanitizingSpanPassesOtherStatusesAndAttributes`. The RPC keys stay as
  otelaws writes them (`rpc.system.name`, `rpc.method=<Service>/<Operation>`;
  Python and Node.js write the legacy keys: a recorded parity gap).

- **BREAKING (narrow) — A26 Redis DB error status (design D1 row "DB error
  status", amended 2026-11-03; migration item 26).** `ext/otelredis` wraps
  each redisotel span so an Error status keeps its code and drops the
  description (redisotel v9.23 `recordError` sets `err.Error()`). The
  exception event keeps the text (the accepted A25 residual). New test
  `TestErrorStatusDropsDescription` (miniredis; a real Redis with
  `OTELREDIS_TEST_ADDR`/`OTELREDIS_TEST_PASSWORD`). Span names stay
  redisotel's lower-case ones (`set`); Node.js and Python use the uppercased
  command, recorded as a parity gap with a Go rename on the backlog.

- **A26 fix round (2026-11-03).** `ext/otelredis`: a failed span carries the
  reply's code (`WRONGTYPE`, `ERR`, `NOAUTH`, ...) as
  `db.response.status_code` and `error.type` (`_OTHER` without one, e.g. a
  dial error); command spans carry `db.operation.name` as provided (`set`);
  `OTEL_SEMCONV_STABILITY_OPT_IN=database` (without `/dup`) drops the legacy
  `db.system` from spans, read at each `Instrument` call (the pool metrics
  keep it: redisotel adds it to their attributes). Tests for pipelines,
  dials to a closed port, `EVAL` and the opt-in; compose `go-tests` and CI
  `go-test` run them against a real Redis (`OTELREDIS_TEST_ADDR`). On Redis
  before 8 every connection records a harmless `client` ERROR span (go-redis
  sends `CLIENT MAINT_NOTIFICATIONS`).
  *Corrected 2026-11-03 (B12 fix round):* not every connection and not
  harmless — the **first connection of each client** (Redis < 8; go-redis's
  `auto` mode then disables the feature for that client) records the ERROR
  `client` span in the trace of the request that opened it; turn it off with
  `MaintNotificationsConfig: &maintnotifications.Config{Mode:
  maintnotifications.ModeDisabled}` (`go/HOW-TO.md` §12; see Changed).

- **A26 final round (2026-11-03).** `db.response.status_code`/`error.type`
  take only known Redis reply codes (an allowlist shared with Python and
  Node.js, checked against the Redis 7.4 source); anything else, such as a
  Lua `error_reply` whose first word is data, is `_OTHER`. The span context
  keeps the SDK span (the wrapper is only the handle redisotel uses), and an
  empty `db.operation.name` is skipped.

- **BREAKING (narrow) — A25 database semconv opt-in parsing (design D1 row
  of 2026-11-03, "Database semconv opt-in").** `ext/otelsql` reads
  `OTEL_SEMCONV_STABILITY_OPT_IN` tokens with the contract trim set and ASCII
  folding, as Python and Node.js. A token with a non-breaking space
  (`database\u00a0`) is no longer trimmed into `database`, so the legacy
  `db.system` stays. Failed DB spans already had an empty status description
  (A25 ruling, no change).

- **BREAKING (narrow) — A23 stdout fallback rulings from the Node B8 work
  (design D1 rows of 2026-11-02: "Stdout fallback time zone", "Stdout
  fallback fields", "OTel log scope name"; migration item 24).** The slog
  stdout JSON fallback (`NewLogger`, `NewSlogHandler` with no logs exporter)
  no longer writes a duplicate key: a top-level attribute named `time`,
  `level` or `msg` is dropped, a top-level `logger` becomes the line's
  `logger` field (once, before the record's own attributes, the last one winning — record over
  `With`; an error under `logger` never becomes the field), and an error
  under any of these keys is renamed to `exception` like any other error,
  or dropped when an `exception` key already exists (an `exception` key
  inside an inline `slog.Group("", …)` counts, and reserved-key errors inside
  inline groups follow the same rule). Record and `With` attributes are covered, members of
  an inline `slog.Group("", …)` too (a `LogValuer` is resolved first);
  inside a `WithGroup` they nest and are kept; the OTel bridge path is
  unchanged. The reserved scan shares the one pass the `exception` rename
  makes and allocates only when something changes
  (`BenchmarkStdoutReserved`: 288 B / 4 allocs → 32 B / 2 allocs per record,
  ~2.2 µs → ~1.6 µs; plain and error records unchanged at 32 B / 2 allocs).
  Recorded, no change: `exception` holds the `Error()` text, non-JSON values
  are written as `!ERROR:json: …` text by slog, and the OTel scope is
  `otel-helper`. Tests: `TestStdoutReservedKeysDropped`, benchmarks in
  `go/bench_stdout_test.go`; parity case `logging.stdout_time_utc_under_tz`
  (324 cases) passes in Go unchanged (Go already writes UTC).

- **BREAKING (narrow) — A21 rulings from the Node B7 work (design D1 rows of
  2026-11-02: "Disabled-metric patterns", "Metrics port grammar", "Metrics
  listener").** `globMatch` replaces `path.Match` in the disabled-metrics
  View (`*` crosses `/`, `?` one rune, `[` literal, case-sensitive); code
  patterns are trimmed and blank ones skipped. `parseMetricsPort`:
  `^\+?[0-9]+$` in [0, 65535], leading zeros of any length accepted, a long
  digit string never converted; `-0` is invalid. The listener's mux pattern
  is `GET /metrics` (HEAD included; other methods 405). Tests:
  `go/a21_test.go`; the parity driver records `prometheus_metrics_port`,
  adds the Gauge `parity.utilization` and UpDownCounter `parity.inflight`
  (unit `1`) to `metrics_scrape` and runs the new kind `metrics_listener`.
  **Fix round (2026-11-02):** the listener registers `/metrics` with a
  `getOrHead` wrapper (405, `Allow: GET, HEAD`) instead of the `GET /metrics`
  pattern; `TestA21ListenerMethodsAndPaths` clears inherited `OTEL_*`
  variables and asserts `Allow`; `parity_match_test.go` is gofmt-clean.

- **BREAKING (narrow) — A20 rulings from the Node B5 review (design D1 rows of
  2026-10-30: "Propagator diagnostics", "Deprecated sample ratio", "ASCII case
  folding").** Environment enum values (sampler, exporter and propagator
  names, OTLP protocol, compression, booleans, known environments) fold ASCII
  only: `asciiLower`/`asciiUpper` replace `strings.ToLower`, `ToUpper` and
  `EqualFold`, so a non-ASCII spelling such as `PARENTBASED_TRACEİDRATIO`
  (U+0130) or `falſe` (U+017F) is now unknown and warned instead of accepted
  silently; health-path matching folds ASCII only too (`/KUBE` no longer matches `/kube`). Diagnostics only: `OTEL_PROPAGATORS` warnings name the effective
  list as `using` (`foo,baggage` → `using=baggage`) and `none` with an
  unsupported token also warns; an invalid `OTEL_HELPER_SAMPLE_RATIO` warns
  with `using=parentbased_always_on` (was `1.0`; same sampler).

- **BREAKING — A19 rulings from the Node B4c work (design D1 rows of
  2026-10-29: "Deprecated `OTEL_HELPER_DISABLED_SIGNALS`", "Exporter alias
  `logging`", "Metric export interval", "`OTEL_CONFIG_FILE`"; migration item
  21).** An `OTEL_HELPER_DISABLED_SIGNALS` entry is skipped when that
  signal's `OTEL_<SIGNAL>_EXPORTER` is set (non-blank). `normalizeExporters`
  rejects `logging` in code (`WithMetricExporters`) as an unknown exporter.
  `OTEL_METRIC_EXPORT_INTERVAL` is parsed by `parseExportInterval`
  (`^\+?[0-9]+$`, > 0, clamped at 2 147 483 647 with a warning, any number of
  digits); `WithExportInterval` is bounded to [1, 2 147 483 647].
  `OTEL_CONFIG_FILE` warns once per process naming the variable only. Seven
  new cases (288 at the time) pass in Go, Python and Node:
  `config.disabled_signals_standard_var_wins`,
  `config.disabled_signals_unknown_warns`,
  `config.code_exporter_logging_fails`,
  `metrics.interval_env_underscore_invalid`,
  `metrics.interval_env_above_max_clamped`,
  `config.code_interval_above_max_fails`, `config.config_file_warns`.
  **Fix round (observability + dev refutation, code-review; BREAKING):** the
  standard variable counts as set only with a valid entry, and
  `WithMetricExporters` beats `OTEL_HELPER_DISABLED_SIGNALS` for metrics;
  `WithDisabledSignals` entries are trimmed, lowercased and de-duplicated
  (`IsSignalEnabled` and `validate` disagreed); a repeated unknown entry
  warns once (`seen` set); `OTEL_EXPERIMENTAL_CONFIG_FILE` is warned like
  `OTEL_CONFIG_FILE` (`warnUnsupported`); `warnEnv` truncates echoed values to
  64 code points; `OTEL_SDK_DISABLED=true` logs the INFO line Python logs; the
  parity adapter reports the effective `metric_exporters` and maps
  `disabled_signals`. Seven more cases (295 in total; the last is Node
  `skip:B5-propagators`): `metrics.interval_env_long_digits_clamped`,
  `config.disabled_signals_unknown_only_standard_var_unset`,
  `config.disabled_signals_code_metric_exporters`,
  `config.experimental_config_file_warns`, `config.code_disabled_signals_trimmed`,
  `config.code_metric_exporters_none_duplicated`,
  `propagation.sdk_disabled_propagators_kept`.
- **BREAKING — A18 rulings from the Node B4b review (design D1 rows of
  2026-10-29: "TLS files", "Export deadline", "Code export timeout bound",
  "Header bytes", "Value-length limits"; migration item 20).**
  `resolveTimeout` returns the 10 s default instead of 0, so the exporter,
  `BatchSpanProcessor`, log `BatchProcessor` and `PeriodicReader` always get
  the resolved deadline; `OTEL_BSP_EXPORT_TIMEOUT`, `OTEL_BLRP_EXPORT_TIMEOUT`
  and `OTEL_METRIC_EXPORT_TIMEOUT` warn once ("not honored").
  `WithExportTimeout` outside [1, 86 400 000] ms fails `Setup`. For a TLS
  endpoint with any TLS file variable set, the helper reads the CA and the
  client pair (per-signal, then generic; unreadable or non-PEM files warned by
  variable and errno, never the path), validates the pair with
  `tls.X509KeyPair` and passes a `tls.Config` (`WithTLSCredentials` /
  `WithTLSClientConfig`); an incomplete or rejected pair warns and the signal
  uses server authentication with the resolved CA. The SDK still reads the
  files at `New()` and prints their paths in its own errors (documented
  limitation, D1 row "SDK's own log output"). Header values must be printable
  ASCII (`%C3%A9` is skipped by position; grpc-go used to fail the whole
  export). `OTEL_*_VALUE_LENGTH_LIMIT=0` warns and the next level applies
  (`WithRawSpanLimits`, log `WithAttributeValueLengthLimit`); the Go log SDK
  still reads only `OTEL_LOGRECORD_ATTRIBUTE_VALUE_LENGTH_LIMIT`. An export
  batch size above the queue size warns naming both variables and the queue
  size is passed (`WithMaxExportBatchSize` / `WithExportMaxBatchSize`).
- **BREAKING — endpoint rulings from the Node B4a review (design D1 rows of
  2026-10-29, task A17; migration item 19).** Endpoints are split by RFC 3986 (the helper's own
  parser, not `url.Parse`): a port outside 1–65535 — port `0` included — is
  invalid; leading-zero ports compare numerically (`:04318` now selects
  http/protobuf by the 4318 inference); a backslash anywhere, an empty
  authority (`http:///x`) or a schemeless value starting with `/` (`//h:4317`)
  is invalid. Every invalid value is shown as `<unparseable URL>` in warnings
  and `Setup` errors, never as a host-less rendering such as `http://:4317`.
  Non-ASCII hosts are converted to punycode (UTS #46 as WHATWG URL runs it,
  `golang.org/x/net/idna` — already in the module graph, now a direct
  requirement); a host failing IDNA is invalid. An empty fragment (`#`) is
  removed with the userinfo/query warning. An `@` in the path or query warns
  "endpoint path contains '@' — misplaced credentials?" naming the variable
  only, and diagnostics no longer print a path or query holding an `@` (they
  did: R8, `redaction.credentials_in_path_never_echoed`). **Known Go
  limitation:** the OTLP/HTTP exporters decode the endpoint path
  (`WithEndpointURL` keeps `url.URL.Path`), so `/a%2Fb` is sent as `/a/b`;
  the helper now warns once per variable ("endpoint path has percent-escapes
  the Go OTLP/HTTP exporter sends decoded", variable name only) and
  `export.http_path_escape_verbatim` is `skip:no-upstream-go-http-raw-path`.
  Refutation round (same task): hosts are lowercased; an ASCII host must be
  an RFC 3986 reg-name or an IP literal, and a host ending in a number must be
  a canonical dotted quad (`127.1`, `0x7f.0.0.1`, `2130706433`, `1.2.3.256`
  are invalid); an invalid `%` escape in an http/protobuf path is invalid in
  every language (the exporter would fall back to its default endpoint); an
  opaque value renders as `unix:` only; `%40` in a path counts as `@`. Final review round: with an `@` (or `%40`) in the path or query every
  diagnostic shows only `<scheme>://` (`<schemeless endpoint>` for a schemeless
  value) — `http://user:1234/x@host` used to show `http://user:1234`; a
  non-ASCII host with an empty label or over 253 characters after punycode is
  invalid; a dotted quad with one trailing `.` (`10.0.0.1.`) is accepted; an
  http/protobuf value with an invalid path escape renders as
  `<unparseable URL>`.
- **Fix — identity and `OTEL_RESOURCE_ATTRIBUTES` rulings (design D1 rows of
  2026-10-29, task A16).** `WithServiceName` / `WithEnvironment` now beat the
  same key in `WithResourceAttributes` in the emitted resource, not only in
  the resolved value. A blank identity value (empty or contract-trim-set only)
  in `OTEL_RESOURCE_ATTRIBUTES` or `WithResourceAttributes` counts as not
  provided — no more `service.name=""` with a `using=""` warning; a blank
  code environment falls back; a blank instance id gets a generated UUID. A
  code-only `WithResourceAttributes({"service.name": …})` resolves to that
  value without the "no service name" warning. The helper now parses
  `OTEL_RESOURCE_ATTRIBUTES` itself (no `resource.Default()`): blank items
  ignored, split at the first `=`, percent-decode then trim with the contract
  set; an item without `=`, with an empty or non-UTF-8 key is skipped, and an
  invalid escape or non-UTF-8 decode keeps the value literal (one U+FFFD per
  invalid raw byte), each with one warning naming the item **positions** only
  (R8). The SDK providers still re-parse the variable (`WithResource` merges
  `resource.Environment()`); while `Setup` builds them a scoped
  `otel.ErrorHandler` filter drops exactly the SDK's echo of malformed items
  (`resource.ErrPartialResource`, bare `url.EscapeError`) and forwards every
  other error to the previous handler, which is restored afterwards. Still a
  known gap (G7): a blank env `deployment.environment.name=` comes back as
  `""`, and a key with surrounding Unicode whitespace appears twice — parity
  cases `identity.blank_env_environment_absent` and
  `resource_attributes.unicode_space_key_verbatim` are
  `skip:no-upstream-go-resource-env-merge`. `telemetry.sdk.*` always come
  from the SDK (a code value warns once, naming the key); the schema URL
  stays semconv 1.43.0, now the contract literal shared by Go, Python and
  Node.js. An empty key in `WithResourceAttributes` fails `Setup`. The
  identity and parse warnings are written once per process. The helper's
  instance id is used even with `OTEL_GO_X_RESOURCE=true`.
- **Fix — `NewSlogHandler` level-filters records exported through the OTel
  bridge (R4, design D6 A13 ruling).** Only the stdout fallback was filtered,
  so with an OTLP logs exporter in PRD the handler exported DEBUG/INFO. Both
  paths now use the level of the environment the last `Setup` resolved
  (LOCAL=Debug, DEV/HML/unset=Info, PRD=Warn; Info before the first `Setup`),
  read per record, so a handler created before `Setup` follows it; `Setup`
  publishes the level before the new bridge. `NewLogger(env, debug)` is
  unchanged and is the workaround for a different level (Go migration item 12).
- **Fix — code options equal to the helper default no longer lose to the
  environment (design D1 edge-case rulings, A13).** `WithSampleRatio(1.0)` now
  beats `OTEL_TRACES_SAMPLER` and `OTEL_HELPER_SAMPLE_RATIO`
  (`ParentBased(TraceIDRatioBased(1))`); `WithPrometheusMetricsPort(9464)` and
  `WithoutMetricsListener()` beat `OTEL_HELPER_METRICS_PORT`;
  a non-nil empty `WithDisabledMetrics([]string{})` /
  `WithDisabledSignals([]string{})` beats `OTEL_HELPER_DISABLED_METRICS` /
  `OTEL_HELPER_DISABLED_SIGNALS`; `nil` means "not set" and the env applies
  (same as Python's `None`).
- The opt-in `ext/` module documentation is now part of the package comment
  (`go doc` shows it); it was an unattached comment in `instrumentation.go`.

Config contract, task A3 (`specs/config-contract-v0.3/`, ADR-001, ADR-003).
Behavior changes, with migration notes:

- **BREAKING — OTLP env resolution follows the spec (ADR-001, second
  correction).** Endpoint, compression, timeout, headers and the plaintext TLS
  override are helper-resolved and passed explicitly for every configured
  signal (the SDK never re-reads a raw env endpoint the helper judged
  differently); certificates and protocol defaults stay with the SDK. Now
  honored: `OTEL_EXPORTER_OTLP_<SIGNAL>_*` (endpoint verbatim,
  protocol, insecure, headers, timeout, compression), path prefixes
  (`…/otlp` → `/otlp/v1/<signal>`), `OTEL_EXPORTER_OTLP[_<SIGNAL>]_CERTIFICATE`
  / `_CLIENT_CERTIFICATE` / `_CLIENT_KEY` (G8: the helper used to pass an
  empty `tls.Config`, so a custom CA failed with `x509: certificate signed by
  unknown authority`). *Migration:* `https://host` without a port under
  http/protobuf now goes to **443** (was 4317); remove `/v1/<signal>` you may
  have added to work around the dropped prefix.
- **OTLP is configured per signal**: only `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`
  set → traces exported, metrics fall back to Prometheus, logs to stdout
  (before: nothing exported, no error).
- **Shims** (design D1.b–d): schemeless `host:port` → `https://` (`http://`
  only with `OTEL_EXPORTER_OTLP_INSECURE=true`), default port 4317/4318,
  deprecation warning for http/protobuf (before: the SDK failed to parse it
  and silently dialed `localhost:4317`); a gRPC URL without a port gets
  `:4317` plus a warning ("set an explicit port"); `INSECURE` contradicting
  the scheme → the scheme wins for that signal, warned (before: `INSECURE`
  overrode the scheme); `gzip` is the default for every signal unless env or
  code sets compression (HTTP metrics/logs were uncompressed); a TLS file
  (`OTEL_EXPORTER_OTLP[_<SIGNAL>]_CERTIFICATE`, `_CLIENT_*`) with a plaintext
  `http://` (or schemeless + `INSECURE=true`) endpoint is warned and ignored
  for that signal, both protocols — gRPC used to dial TLS and lose the signal,
  http/protobuf failed `Setup`.
- **Helper-resolved env (design D1 edge-case rulings):** every per-signal
  value falls back per-signal → generic → default with a warning. Compression
  is case-insensitive `gzip`/`none` (`deflate`, `zstd`, … warn); `*_TIMEOUT`
  is decimal milliseconds (garbage/`0`/negative/hex warn; clamped to
  [1 ms, 24 h]; was an SDK error or a ~30 s deadline); `*_HEADERS` keys must
  match `[A-Za-z0-9._-]+`, are compared case-insensitively (last wins) and not
  decoded, values are percent-decoded (an invalid escape or non-UTF-8 result
  keeps the whole value literal and warns; a decoded control character skips
  the item), malformed items are skipped and warned by position only, and the
  result goes to every signal (the log SDK dropped all headers on one bad
  item); an unparseable or empty-host per-signal endpoint falls back to the
  generic one; an unsupported scheme (`unix://`, `unix:/path`) warns and the
  signal is not exported; endpoint whitespace (e.g. a trailing newline, which
  lost all logs) is trimmed; userinfo/query in any endpoint are stripped with a
  redacted warning.
- **Effective export deadline:** the resolved timeout is also given to the
  batch span/log processors and the periodic metric reader (their own 30 s
  timeouts used to cap it). `WithCompression("none")` now wins over an env
  `gzip` on gRPC traces/metrics too (identity compressor).
- **Code values fail fast:** `WithSampleRatio` ∉ [0,1]/NaN,
  `WithPrometheusMetricsPort` ∉ [0,65535], unknown `WithDisabledSignals`
  names. New constants `CompressionGzip` / `CompressionNone`.
  *Migration:* `OTEL_EXPORTER_OTLP_INSECURE=true` with `https://` no longer
  gives plaintext — use `http://`.
- **Code endpoints** accept URLs: `WithEndpoint("http://c:4318/otlp")` appends
  `/v1/<signal>` for http/protobuf (before: `too many colons`). New
  `WithTracesEndpoint` / `WithMetricsEndpoint` / `WithLogsEndpoint` (verbatim)
  and `WithCompression("gzip"|"none")`. `WithInsecure` now applies to
  schemeless endpoints; contradicting a code URL's scheme fails `Setup`.
- **G10**: no default timeout is passed any more (`ExportTimeoutMs` default
  0 = unset), so `OTEL_EXPORTER_OTLP[_<SIGNAL>]_TIMEOUT` (ms) works;
  `WithExportTimeout` now applies to all three signals (was traces only).
- **BREAKING — identity (ADR-003).** `service.name`: code >
  `OTEL_SERVICE_NAME` > `OTEL_RESOURCE_ATTRIBUTES` > `SERVICE_NAME` > SDK
  `unknown_service:<exe>` + warning (was: `SERVICE_NAME` first, default
  `my-service`). `deployment.environment.name`: code >
  `OTEL_RESOURCE_ATTRIBUTES` > `ENVIRONMENT` > **absent** + one warning (was
  `LOCAL`); unknown values emitted verbatim (were coerced to `LOCAL`); a helper
  value never overwrites an `OTEL_RESOURCE_ATTRIBUTES` key. Unset/unknown
  environment → `DefaultLogLevel` INFO. A per-process UUID
  `service.instance.id` is added unless code or `OTEL_RESOURCE_ATTRIBUTES`
  set one. *Migration:* set `OTEL_SERVICE_NAME` and
  `OTEL_RESOURCE_ATTRIBUTES=deployment.environment.name=…`; local development
  without an environment no longer logs at DEBUG; the Collector needs a
  tail-sampling branch for spans without the attribute (design D11).
- **R8 — env never fails Setup.** Unknown/unparseable env values warn and use
  the default (`OTEL_METRICS_EXPORTER=statsd`, `OTEL_EXPORTER_OTLP_PROTOCOL`,
  `OTEL_METRIC_EXPORT_INTERVAL=0|abc`, `OTEL_SDK_DISABLED`, booleans, ratio,
  port); env `OTEL_METRICS_EXPORTER=otlp` without an endpoint warns and falls
  back (was a Setup error). The same invalid values in code fail Setup with an
  error naming the option. `http/json` from env is warned and ignored.
- **New env support:** `OTEL_{TRACES,METRICS,LOGS}_EXPORTER` = `otlp`,
  `console` (compact JSON, not parity-checked; official stdout exporters,
  regular dependencies imported only by `go/console.go`:
  `go.opentelemetry.io/otel/exporters/stdout/{stdouttrace,stdoutmetric}` v1.47.0
  and `stdoutlog` v0.23.0), `none`; `logging` = deprecated `console` (warned);
  `none,x` = none (warned). `OTEL_SDK_DISABLED=true` installs no-op
  providers. `OTEL_PROPAGATORS` (`tracecontext`, `baggage`, `none`; `b3`,
  `jaeger`, `xray`, `ottrace` are warned and skipped; with nothing supported
  left, the default `tracecontext,baggage` stays active).
- **G7**: the helper no longer calls `os.Setenv` (it forced
  `OTEL_METRICS_EXEMPLAR_FILTER=trace_based`, the SDK default anyway).
- **Diagnostics**: helper warnings (including a failed Go runtime-metrics
  start) go to a package stderr `slog` logger (never
  `slog.Default()`); helper diagnostics never print header values,
  client-key paths, or URL userinfo/query. Enforced by
  `go/diag_channel_test.go`. *Limitation:* the OTel Go SDK's own global logger
  may still echo raw env values (e.g. a malformed `OTEL_EXPORTER_OTLP_HEADERS`
  item, an unparseable endpoint) when it parses env itself; the helper cannot
  mask env (G7). See `go/HOW-TO.md` §15.

- **Lifecycle (G1, G2, design D13).** `Setup` builds every signal before
  installing anything: on error it releases what was already built in reverse
  order (span/log batch processors and their exporters, metric readers, the
  `/metrics` listener — its port is free at once; no goroutine is left), leaves
  the global providers and propagator as they were, and can be retried.
  `Shutdown` flushes logs, metrics (listener, then provider) and traces
  **concurrently** under the caller's context, errors joined; it is
  idempotent (later or concurrent calls return the first result) and resets
  the helper, so a following `Setup` builds a fresh pipeline; a `Setup` called
  while a `Shutdown` is in flight waits for it. *Caveat:* tracers, meters and
  instrumented handlers obtained **before** that new `Setup` stay bound to the
  shut-down providers (the global delegates forward once) — obtain them after.
- **BREAKING — `MetricsHandler()` status.** 503 before `Setup`, after a failed
  `Setup` and after `Shutdown`; **200 with an empty body** when Prometheus is
  deliberately off (`OTEL_SDK_DISABLED=true`, or `OTEL_METRICS_EXPORTER`
  without `prometheus`) — previously 503, which marks the target `up=0`.
- **Examples** handle `SIGTERM` (`signal.NotifyContext`) and call `Shutdown`
  with a fresh deadline: before, `docker stop` killed them unflushed, and the
  deferred call used the already-cancelled context. They now drain HTTP
  (bounded `srv.Shutdown`) and go-process workers before the flush, return
  errors instead of `log.Fatal` (which skipped it), and go-process logs via
  `NewLogger`. The helper installs no signal handlers (see `go/HOW-TO.md` §8).
  A `Setup` waiting on an in-flight `Shutdown` returns `ctx.Err()` when its
  own context ends first.
- **Sampler (A4, design D3).** The helper parses `OTEL_TRACES_SAMPLER` /
  `_ARG` itself (ADR-001 owned inputs) in one `buildSampler`: the six spec
  names, case-insensitive and trimmed. An unknown name warns naming the
  variable, and `jaeger_remote` / `parentbased_jaeger_remote` / `xray` warn
  "unsupported by this helper"; both use `parentbased_always_on`. `_ARG` and
  the deprecated `OTEL_HELPER_SAMPLE_RATIO` share the timeout grammar
  (`^[0-9]+(\.[0-9]+)?$`: no hex, sign, exponent or `_`): invalid or out of
  [0, 1] warns and uses 1.0 (the alias used to clamp silently and accept
  hex). A code ratio below 1 or the alias (only while `OTEL_TRACES_SAMPLER` is
  unset; blank counts as unset) → `ParentBased(TraceIDRatioBased(x))`.
  *Limitation:* the SDK still parses the same env when the provider is built,
  so an unknown name or invalid `_ARG` is reported once more via `otel.Handle`
  (`unsupported sampler: …` / invalid argument); the helper cannot mask env (G7).
- **Health-check exclusion in the sampler (A7-go, R7, design D5).** `Setup`
  wraps `buildSampler` so a SERVER span with no local parent (none, or a
  remote `traceparent` — a sampled probe is still dropped) is not recorded
  when its `url.path` (`http.target` fallback) is a health path, or its name /
  `rpc.method` is in the `grpc.health.v1.Health/` service (Check, Watch, List); the parent's
  tracestate is kept, and spans the handler starts follow the DROP through
  ParentBased (the default; `always_on` / `traceidratio` ignore the parent, so
  they sample those children as they would under any unsampled parent).
  Defaults: `/health /healthz /ready /readyz /live /livez /ping /metrics`,
  exact, case-insensitive, trailing slash and query ignored (`/users/ping`,
  `/healthcare`, `/health/live` stay traced). New `WithHealthPaths(paths...)`
  / `Options.HealthPaths` replaces the list (`WithHealthPaths()` = none); a
  path that is not ASCII, does not start with `/` or carries `?`, `#` or `,`
  fails `Setup`. **BREAKING:**
  `NewHTTPHandler` no longer installs an otelhttp `WithFilter`, so health
  requests now appear in `http.server.request.duration` (see the table
  above). Client side (design D6 "A5 validation rulings"): `NewHTTPTransport`
  now uses the same list and matching (was exact `/ping /health /healthz
  /ready`; `WithHealthPaths` applies too) and `GRPCClientHandler` /
  `GRPCDialOption` filter the whole `grpc.health.v1.Health/` service (Check,
  Watch, List; the server sampler matches the same way) — health client calls get
  **no client span and no client metric** (upstream filter behavior;
  **BREAKING** for `rpc.client.call.duration`, which counted them). The
  `go-api` example now wraps `/health` so probes stay in the RED metrics.
- **Per-record log handler (A5, G3, design D6).** `NewLogger` and
  `NewSlogHandler` share one handler that decides **per record** (atomic):
  while `Setup` has a logs exporter installed (OTLP and/or
  `OTEL_LOGS_EXPORTER=console`; `otlp,console` feeds both) records go through
  the OTel bridge, bound to that pipeline's `LoggerProvider`; otherwise —
  before `Setup`, with no endpoint, `OTEL_LOGS_EXPORTER=none`, or after
  `Shutdown` — they are written to stdout as JSON (`time`/`level`/`msg`, the
  0.2.0 fallback shape). Fixed: `NewSlogHandler` with no endpoint dropped every
  record (`Enabled(Info)=false`); loggers created before `Setup` stayed on
  stdout forever; loggers kept across `Shutdown` wrote to the closed provider
  (records lost) and, after a new `Setup`, kept writing to the old one.
  Stdout shape (shared with Python): one JSON line with `time` (RFC 3339,
  milliseconds, UTC `Z`; slog's default was local time), `level`
  (`DEBUG/INFO/WARN/ERROR`), `msg`, the attributes, and `exception` (the
  text of the first error-valued attribute passed with the record, which is
  renamed — not when an `exception` key already exists; rendered like slog:
  typed-nil → `<nil>`, panicking `Error()` → `!PANIC: …`). Both handlers apply the environment level on stdout (`NewSlogHandler`
  the one the last `Setup` resolved, Info before it); on the bridge
  both enforce their level in `Handle` too (for `NewSlogHandler` since the A13
  fix below; before it the `LoggerProvider` decided). Trace↔log correlation needs the `*Context` slog
  calls (`InfoContext`…).
- **Deprecations (A9, design D8).** Setting `SERVICE_NAME`, `ENVIRONMENT`,
  `OTEL_HELPER_SAMPLE_RATIO` or `OTEL_HELPER_DISABLED_SIGNALS` logs one warning
  per process on the helper's diagnostics logger, naming the variable and its
  replacement (`OTEL_SERVICE_NAME`; `deployment.environment.name` in
  `OTEL_RESOURCE_ATTRIBUTES`; `OTEL_TRACES_SAMPLER`/`_ARG`;
  `OTEL_{TRACES,METRICS,LOGS}_EXPORTER=none`) — also when the standard
  variable wins. Behavior is unchanged; removal is planned for 1.0. The
  schemeless `http/protobuf` endpoint deprecation (A3) is now also once per
  process.
- **BREAKING — removed `WithExtraInstrumentation`, `Options.HasInstrumentation`
  and the `Options.ExtraInstrumentation` field (A9, G9).** They never changed
  what the helper instruments in Go; import `ext/otelsql`, `ext/otelredis` or
  `ext/otelaws`. `OTEL_HELPER_EXTRA_INSTRUMENTATION` has no effect and warns
  once when set.
- **Debug marker on entry spans (A9, R9, ADR-006).** With
  `OTEL_HELPER_DEBUG_LEVEL=true` (or `WithDebug`) `debug=true` is set on every
  span whose parent is absent **or remote** (was: no parent only, so a service
  called by another never marked its spans). Spans with a local parent are not
  marked; inbound tracestate/baggage never sets it; tracestate is not written.
- **`ext/otelsql` (A10, G6, design D14).** Driver names map to `db.system.name`
  (semconv v1.43.0): `pgx`, `pgx/v5`, `postgres` → `postgresql`; `sqlite`,
  `sqlite3` → `sqlite`; `mysql`; `sqlserver`, `mssql` →
  `microsoft.sql_server`; anything else → `other_sql` (was the raw driver name
  in `db.system`). The legacy `db.system` key is still emitted until 1.0 with
  the semconv v1.26.0 values (`postgresql`, `sqlite`, `mysql`, `mssql`,
  `other_sql`), on spans and on the `db.sql.*` pool metrics, unless
  `OTEL_SEMCONV_STABILITY_OPT_IN` contains `database` without `database/dup`
  (then only `db.system.name`). **BREAKING:** `Open` returns
  `(db, closeDB, err)` — call `defer closeDB()` instead of `db.Close()`; it
  unregisters the pool-metrics callback (which previously kept reporting a
  closed pool) and closes the pool. `Open` also returns an error (and closes
  the database) when the pool metrics can't be registered, instead of
  dropping it. `github.com/XSAM/otelsql` v0.35.0 → v0.44.0.
- **`ext/otelredis` (A10, G6, design D14).** `Instrument` also installs pool
  metrics (`redisotel.InstrumentMetrics` with `WithSemConvCompliantMetrics`):
  `db.client.connections.*` — Prometheus `db_client_connections_usage`,
  `_max`, `_idle_max`, `_idle_min`, `_waits_total`,
  `_waits_duration_nanoseconds`, `_timeouts_total`, `_hits_total`,
  `_misses_total`, `_create_time_milliseconds_*`, `_use_time_milliseconds_*`.
  Spans gain `db.system.name=redis` and no longer carry command values
  (`db.statement`, previously the full command including values) or
  `db.connection_string`. Call once per client; when the metrics step fails
  the error is returned and tracing is already installed. go-redis and
  `redisotel` v9.7.3 → v9.23.0.
- **Ext module tests (A10).** `ext/otelsql` (in-memory `modernc.org/sqlite`),
  `ext/otelredis` (`miniredis`), `ext/otelaws` (SQS client on a stub HTTP
  transport); test-only dependencies approved by the owner:
  `modernc.org/sqlite` v1.61.0 (BSD-3-Clause), `github.com/alicebob/miniredis/v2`
  v2.40.0 (MIT), `github.com/aws/aws-sdk-go-v2/{credentials,service/sqs}`
  (Apache-2.0). `aws-sdk-go-v2` v1.47.1 → v1.47.5.

### Python — detailed changes

- **A30 owner rulings of 2026-11-06 (design D1 "HTTP opt-in metrics", "AWS
  RPC keys", "AWS service attribute values").** `TelemetryOptions.http_opt_in_metrics`
  / `OTEL_HELPER_HTTP_OPT_IN_METRICS` (`resolved.http_opt_in_metrics`): off,
  SDK views drop `metrics.HTTP_OPT_IN_METRICS` (active requests and body
  sizes, both HTTP semconv generations) from the contrib instrumentation
  scopes the helper installs, so an application's own instrument is kept. The botocore wrapper
  (`_aws_status.aws_call_span`) names every AWS call span
  `<Service>.<Operation>` (Bedrock GenAI spans excepted), with kind PRODUCER
  on SQS/SNS send and publish, CONSUMER on SQS receive and CLIENT otherwise,
  adds `messaging.operation.name` on SQS and SNS spans, and maps the RPC keys by `rpc_mode()`
  (legacy / `rpc` / `rpc/dup`); contrib's `get_server_attributes` is pointed
  at `server_attributes` (port 80 for a portless `http://` endpoint). Tests:
  `tests/test_a30.py`.

- **A30 instrumentation parity backlog (design D1 rows "gRPC span names",
  "Redis `db.operation.name`", "AWS service attribute values", "URL query
  redaction", 2026-11-05).** `GrpcSpanNameProcessor` (on the helper's
  TracerProvider) renames contrib grpc spans named `/<rpc.service>/<rpc.method>`
  to `<rpc.service>/<rpc.method>` at start; the health sampler still decides
  on the original name. The Redis tracer wrapper writes `db.operation.name`
  (the command as contrib names the span; `FT.SEARCH`/`FT.CREATE`) when a
  command span ends, none on pipeline spans. The botocore wrapper maps
  `messaging.system` `aws.sqs` → `aws_sqs` and adds
  `db.system.name=aws.dynamodb` to DynamoDB spans, dropping contrib's legacy
  `db.system` in the stable database mode (`_semconv.legacy_db_keys_enabled`).
  `REDACTED_QUERY_PARAMS` gains `access_token`, `X-Goog-Credential` and
  `GoogleAccessId`. Tests: `tests/test_a30.py`, `tests/test_a28_url_redaction.py`.

- **A29 fix round (2026-11-05).** The exporter-log filter masks each
  space-separated part of 4+ characters of a configured header value (the
  token of `Bearer <token>`), the HTML-escaped JSON form (and it JSON-escaped
  again) and the form-encoded form, in one pass, longest first; the
  `ensure_ascii` form is dropped. The bind-failure hint names what set the port
  (`prometheus_metrics_port`, `OTEL_EXPORTER_PROMETHEUS_PORT` or
  `OTEL_HELPER_METRICS_PORT`) and `OTEL_EXPORTER_PROMETHEUS_HOST` when one was
  set; the startup line names the host; the listener binds without a reverse
  DNS lookup. `::ffff:a.b.c.d` binds as IPv4 (`::ffff:0.0.0.0` is no longer
  "every interface"). Invalid host warnings use the shared fail-open text and
  never echo URL userinfo or query. The upgrade note on the Prometheus
  variables is in the Go section above and applies here.
- **A29 SDK error text, gRPC endpoint paths, Prometheus variables (design D1
  rows of 2026-11-05; not yet independently reviewed).** The OTLP exporters
  never log a response body, but they log server text: the HTTP reason phrase
  and, in `opentelemetry-exporter-otlp-proto-grpc` 1.45, the gRPC status
  details. `setup_telemetry()` adds a filter (`_sdk_log_mask.py`, once per
  process) to the SDK OTLP exporter loggers that masks the configured
  `OTEL_EXPORTER_OTLP[_<SIGNAL>]_HEADERS` values (4 characters or more; raw,
  decoded, JSON-escaped once and twice, `repr` form) in the message and the
  exception text (a masked traceback is folded into the message, `exc_info`
  cleared). A path on a gRPC endpoint is ignored with the warning
  `[OtelHelper] <variable>: endpoint path is ignored by the gRPC exporter.`
  `OTEL_EXPORTER_PROMETHEUS_PORT` beats `OTEL_HELPER_METRICS_PORT` (code
  `prometheus_metrics_port` still wins); `OTEL_EXPORTER_PROMETHEUS_HOST` sets
  the listener's bind address, read back as the new
  `ResolvedOptions.prometheus_metrics_host` (`""` = every interface). Tests:
  `tests/test_a29.py`.
- **BREAKING (narrow) — A28 URL query redaction (design D1 row "URL query
  redaction" of 2026-11-03; migration item 28).** A
  `UrlQueryRedactionProcessor` (`_url_redaction.py`), first on the helper's
  TracerProvider, writes the values of `REDACTED_QUERY_PARAMS` as `REDACTED`
  in `url.full`, `http.url`, `http.target` and `url.query` at span start
  (contrib util-http redacts only four names in client URLs; ASGI writes
  `url.query` as received). Tests: `tests/test_a28_url_redaction.py`. The
  list is a superset of contrib's default. *Fix round (2026-11-03):* the
  processor also patches the live span, so attributes written after start
  (ASGI rewrites `url.query`), the status description and exception events
  (a failed `requests` call) are redacted; tests go through requests, httpx
  and FastAPI.
- **A27 fix round (2026-11-03).** `error.type` falls back to
  `Error.QueryErrorCode`, the modeled exception class
  (`QueueDoesNotExist`) and, for an all-digit code, the HTTP reason
  phrase from a fixed table of Go's `http.StatusText` values (`NotFound`,
  `Forbidden`, `ServiceUnavailable`), as Go and Node; contrib's Bedrock `error.type` writes
  are dropped (every value comes from `aws_error_type()`); the 3.10 lower
  leg runs on `botocore==1.34.0`. aiobotocore is not covered.
- **BREAKING (narrow) — A27 AWS error status (design D1 rows "AWS error
  status", "AWS payload attributes" of 2026-11-03; migration item 27).**
  `instrument_aws()` and the deprecated `AWS` flag of `setup_telemetry()`
  bind contrib botocore to a new TracerProvider wrapper
  (`staffops_otel_helper/_aws_status.py`, a subclass of the `_db_status` one):
  a failed call keeps ERROR without description (contrib sets
  `str(error)`), `error.type` is the AWS error code
  (`ClientError.response["Error"]["Code"]`) when it matches
  `^[A-Z][A-Za-z0-9]{1,63}$`, else `_OTHER` (a transport error without an
  AWS code: its class name when that matches), and
  `aws.dynamodb.item_collection_metrics` is never recorded. The SNS phone
  number was already redacted by contrib (`phone_number:**`). botocore
  instrumented elsewhere without the wrapper is kept and warned once.
  `instrument_aws()` called twice is a no-op that returns True. Tests:
  `tests/test_a27_aws.py` (in-process fake: S3, DynamoDB, SNS). Test-only dev
  dependency `botocore>=1.34,<2` (Apache-2.0).

- **A25: `instrument_sql(engine)` after `setup_telemetry()` (2026-11-03).** An
  engine created before `setup_telemetry()` was never traced, and
  `instrument_sql(engine)` still returned True. It now attaches the wrapped
  per-engine tracer, built the same way contrib's `instrument(engine=...)`
  builds it (`EngineTracer`, 0.63b0 and 0.66b2). An engine already traced is
  left alone. If the contrib layout is not the expected one, it warns and
  returns False.

- **A25 second fix round (2026-11-03).** The default `SQL` path of
  `setup_telemetry()`, the deprecated `OTEL_HELPER_EXTRA_INSTRUMENTATION`
  default, now instruments SQLAlchemy through the same status wrapper as
  `instrument_sql()`. Before this round it still wrote the database error
  text as the status description, and a later `instrument_sql()` did nothing.
  If other code instrumented SQLAlchemy first, `instrument_sql()` keeps that
  instrumentation and warns once; re-instrumenting would drop the listeners
  of engines already created. An empty or whitespace-only
  `OTEL_SEMCONV_STABILITY_OPT_IN` is treated as unset (`http,database/dup`).
  Every token is ASCII-folded, so `HTTP` and `HTTP/DUP` now select stable and
  dup HTTP. In dup mode the full new and full legacy attribute sets are
  emitted. The normalized value is written back to `os.environ`, so child
  processes inherit it.

- **BREAKING (narrow) — A26 Redis DB error status (design D1 row "DB error
  status", amended 2026-11-03; migration item 26).** `instrument_redis()` and
  the `REDIS` flag of `setup_telemetry()` bind contrib redis to the same
  TracerProvider wrapper as `ext.sql` (moved to `staffops_otel_helper._db_status`):
  a failed command keeps status ERROR without its description. A second
  `instrument_redis()` call returns True; a Redis instrumented elsewhere
  without the wrapper is kept and warned once. New test-only dev dependency
  `redis>=5.0,<9`; tests `tests/test_a26_redis.py` (an in-process RESP fake,
  and a real Redis with `REDIS_TEST_URL`).

- **BREAKING (narrow) — A26 fix round (2026-11-03).** A failed Redis span
  carries the reply's code as `db.response.status_code` and `error.type`
  (`_OTHER` when redis-py stripped it, e.g. `ERR`, `NOAUTH`). A Redis
  pipeline error's `exception` event no longer carries the failing command's
  arguments: redis-py writes `Command # N (<command and args>) of pipeline
  caused error: …`, now recorded as `Command # N (LPUSH ? ?) …` in
  `exception.message` and in a rebuilt `exception.stacktrace` (sync and
  asyncio). The wrapped mark is cleared on `uninstrument()`, and the
  `SQL`/`REDIS` flags of `setup_telemetry()` warn once when other code
  instrumented SQLAlchemy or Redis without the helper's wrapper. The
  test-only `redis>=5.0,<9` dependency is owner-approved; `ci/lower.txt`
  pins `redis==5.0.0`; compose `python-tests` and CI `python-test` run the
  real-Redis cases.

- **A26 final round (2026-11-03).** The Redis reply code comes from the
  exception's `status_code` (redis-py 8.x), an allowlisted leading code, or
  the redis-py exception class; on redis-py < 8 a plain `ResponseError` is
  `ERR` (redis-py strips it). Unknown codes, including a Lua `error_reply`'s
  first word, are `_OTHER` (redis-py 8.x) or `ERR` (older). The Redis wrapper
  never raises into the instrumented call: on an internal error it records a
  minimal sanitized event. Chained exceptions' messages are sanitized in the
  stacktrace too, and any text with `of pipeline` loses what is between `(`
  and `) of pipeline`.

- **BREAKING (narrow) — A25 DB error status and database semconv opt-in
  (design D1 rows of 2026-11-03: "DB error status", "Database semconv
  opt-in").** `instrument_sql()` binds contrib sqlalchemy to a TracerProvider
  wrapper. A failed statement's span (and a failed `connect`) keeps status
  ERROR **without its description**, which was the database error text
  (`str(exc)`). `OTEL_SEMCONV_STABILITY_OPT_IN` is normalized before contrib
  reads it: the value is ASCII-folded, and an empty or database-less value
  gets `database/dup` appended. So `http` (or an empty value) now emits both
  `db.system.name` and `db.system`, not `db.system` only, and `DATABASE`
  means `database`. The rule is now the same in Go and Node.js.

- **A24 explicit instance id in a worker (design D1 row of 2026-11-02,
  "Explicit instance id in a worker").** The warning for a
  `service.instance.id` set in code or `OTEL_RESOURCE_ATTRIBUTES` in a worker
  forked after the helper was imported is now a conditional, with the
  same wording as Node.js except the worker kind (`forked`/`cluster`):
  `service.instance.id is set explicitly in a
  forked worker: unless each worker sets a different value, every worker
  reports the same id; leave it unset so each process gets its own` (it
  stated the collision as a fact and named the two sources). Log searches on
  the old text need updating. The value is still kept.
- **BREAKING — A23 stdout fallback rulings from the Node B8 work (design D1
  rows of 2026-11-02; migration item 24).** The stdout JSON fallback
  (`_console.JSONFormatter`): `time` in UTC with `Z` whatever the process
  zone (`_rfc3339_ms`; it wrote local time with its offset when `TZ` was not
  UTC); **compact separators** `(",", ":")` as Go and Node.js
  (`{"time":"…","level":"INFO",…}` instead of `{"time": "…", "level": …}`;
  the example output in `python/example/README.md` relied on the old
  spacing and is updated); valid JSON for every value — the fast path is
  `json.dumps(…, default=_text, allow_nan=False)` (a raising `__str__`
  gives `[unprintable <type>]`), and only when it raises are the failing
  fields retried one by one and sanitized: non-finite floats as `"nan"` / `"inf"` / `"-inf"` (`json.dumps`
  wrote bare `NaN` / `Infinity`), a cycle as `"[Circular]"` (the record used
  to be dropped with a logging error on stderr), dict keys json cannot
  encode (tuples, objects, NaN/inf) as `str(key)`, and a value the
  sanitizer cannot walk (too deep, a dict subclass whose `items()` raises)
  as its text — no extra can cost the record. New parity case `logging.stdout_time_utc_under_tz`
  (`TZ=America/Sao_Paulo`; it fails on the old code) and tests in
  `tests/test_logging_a5.py` (time zone, reserved extras, non-finite floats,
  cycles, tuple keys, depth 900 still encoded as before, deeper as text).
  Recorded, no change: extras named `time`, `level` or `logger` are skipped;
  `exception` keeps the traceback (with `exc_info` it replaces an
  `exception` extra); the OTel scope is the logger name.

- **BREAKING — A21 rulings from the Node B7 work (design D1 rows of
  2026-11-02).** The helper's Prometheus reader rewrites the unit of Gauges in
  `1` to `ratio` in `_receive_metrics` before the upstream exporter
  translates them (its `_mapping.py` maps `1` to no suffix), so they scrape
  with `_ratio` as in Go. The standalone listener is a helper-owned
  `ThreadingHTTPServer` (`IPV6_V6ONLY` off, IPv4 fallback when IPv6 is
  unavailable; a busy port still raises) serving `generate_latest` of the
  dedicated registry on `GET`/`HEAD /metrics` only (404 elsewhere, 405 for
  other methods, requests not logged), replacing `start_http_server`.
  Disabled-metric Views escape `[` (`[[]`) and are not created for patterns
  with an upper-case letter (the SDK lower-cases names and View patterns);
  code items are trimmed. The port grammar (`^\+?[0-9]+$`, at most five
  significant digits converted) replaces `int()`. Tests: `tests/test_a21.py`.
  **Fix round (2026-11-02):** the reader adapter also merges colliding label
  names (data points and resource, sorted values joined by `;`); the listener handler
  routes every method through one check (`__getattr__` for `do_*`), reads
  the path with `urlsplit`, has `timeout = 10`, answers 500 when
  `generate_latest` raises and logs it, and its servers log `handle_error`
  on the helper logger; the listener is stopped when a foreign MeterProvider
  wins; an upper-case pattern warns once (variable named, pattern not
  echoed).

- **A20 rulings from the Node B5 review (design D1 rows of 2026-10-30).**
  `OTEL_HELPER_SAMPLE_RATIO` that is not a plain decimal in [0, 1] (`2`,
  `nan`) is warned `sample ratio is not a number in [0, 1];
  using=parentbased_always_on` and ignored instead of clamped (the sampler is
  the same: not breaking). `OTEL_PROPAGATORS`: every token is scanned (no
  early return at `none`), one warning per skipped token — `propagator not
  supported by the Python helper, ignored` for `b3`/`b3multi`/`jaeger`/`xray`/
  `ottrace`, else `unknown propagator, ignored` — with `variable`, `value` and
  `using` (the effective list), and `none` with other values warns `"none"
  combined with other propagators; propagation disabled`; the combined
  "unsupported propagator(s)" line is gone. Env enum values fold ASCII only
  (`ascii_lower`/`ascii_upper`: `str.lower()` maps U+212A to `k`,
  `str.upper()` U+0131 to `I`), also for health-path matching and the
  `OTEL_SEMCONV_STABILITY_OPT_IN` token check.

- **BREAKING — A19 rulings from the Node B4c work (design D1 rows of
  2026-10-29; migration item 22).** `OTEL_METRIC_EXPORT_INTERVAL` is checked
  against `^\+?[0-9]+$` before `int()` (no `_`, no non-ASCII digits) and
  clamped at 2 147 483 647 with a warning; `export_interval_ms` above it
  raises `ValueError`. Unknown `OTEL_HELPER_DISABLED_SIGNALS` entries warn
  once. `OTEL_CONFIG_FILE` warns once per process naming the variable only.
  The standard variable already won over `OTEL_HELPER_DISABLED_SIGNALS`, and
  a code `logging` metric exporter already failed. Same seven new cases.
  **Fix round (BREAKING):** `_resolve_export_interval` accepts any number of
  digits (the unreleased A19 draft raised above 4300 digits in `int()`): the
  `+` and leading zeros are dropped and more than 10 significant digits
  clamp, as in Go. The
  standard variable counts as set only with a valid entry, and
  `metric_exporters` wins for metrics; code `metric_exporters` and
  `disabled_signals` are de-duplicated after normalization (`["none",
  "none"]` disables metrics instead of raising); `OTEL_EXPERIMENTAL_CONFIG_FILE`
  warns like `OTEL_CONFIG_FILE`; `_warn_once` truncates echoed string values
  to 64 code points; the parity driver reports the effective
  `metric_exporters` and maps `disabled_signals`. Same seven fix-round cases.
- **BREAKING — A18 rulings from the Node B4b review (design D1 rows of
  2026-10-29; migration item 21).** Every OTLP signal gets an explicit
  `timeout` (10 s by default), which is also the `export_timeout_millis` of
  its `PeriodicExportingMetricReader` (`OTEL_METRIC_EXPORT_TIMEOUT` is no
  longer honored; the three timeout variables warn). The batch processors get
  the same value for consistency, but the 1.4x SDK ignores it. An export batch
  size above the queue size warns naming both variables and the queue size is
  passed as `max_export_batch_size` (the SDK raised `ValueError`). `export_timeout_ms` above
  86 400 000 raises `ValueError`. New module `_tls`: the helper resolves and
  validates the TLS files (`ssl` `load_verify_locations` / `load_cert_chain`
  with a non-prompting password callback) and passes gRPC channel credentials
  or the HTTP file paths, with the TLS variables masked during exporter
  construction. Header values must be printable ASCII; `-bin` keys are sent
  as `bytes`. A value-length limit of `0` warns and the next level applies
  (`SpanLimits`; `LogRecordLimits` where `LoggerProvider` accepts
  `log_record_limits` — 1.45 does, 1.42 does not: documented limitation).
- **BREAKING — endpoint rulings from the Node B4a review (design D1 rows of
  2026-10-29, task A17; migration item 20): zone ids, non-canonical IPv4 and
  non-reg-name hosts, port 0, invalid path escapes.** Endpoints are split by RFC 3986 instead of
  `urlsplit`: a backslash anywhere (also in userinfo, where it used to leak
  the password into diagnostics — `redaction.password_with_backslash`), an
  empty host (`http://:4317`, `http:///x`, `//h:4317`), port `0`, a space, a
  percent-encoded byte or an **IPv6 zone id** in the host make the value
  invalid — zone ids used to work over gRPC; they now fall back (env) or
  raise (code). Invalid values are always shown as `<unparseable URL>`. A
  control character beats an opaque scheme (`unix:/x\x7f` is invalid, not
  "unsupported"). Non-ASCII hosts are punycoded with UTS #46 non-transitional
  processing via the `idna` package; a host failing it is invalid. An empty `?` or `#` is removed with the
  userinfo/query warning; an `@` in the path or query warns "endpoint path
  contains '@' — misplaced credentials?" naming the variable only. An
  unparseable `OTEL_EXPORTER_OTLP[_<SIGNAL>]_INSECURE` now warns even when no
  endpoint is configured. An empty code `otlp_protocol` or `compression`
  (`""`) is "not set" instead of raising `ValueError`. Valid path escapes are
  kept verbatim on the wire (`/a%2Fb`); an invalid one (`/%zz`) in an
  http/protobuf path is invalid (warn and fall back / raise) — `requests`
  would otherwise double-encode every escape of the URL. Refutation round
  (same task): a port of any length is parsed (leading zeros stripped; more
  than 4300 digits used to raise `ValueError` out of the resolver); IDN hosts
  use UTS #46 non-transitional with IDNA 2008 validation (`straße.de` →
  `xn--strae-oqa.de`, ZWJ/ZWNJ invalid; emoji and some `_` / long labels Go and
  Node accept are invalid — README gaps); an ASCII host must be an RFC 3986
  reg-name or an IP literal, and a host ending in a number a canonical dotted
  quad (`127.1`, `1.2.3.256` invalid); an opaque value renders as `unix:` only;
  `%40` in a path counts as `@`. Final review round: with an `@` (or `%40`) in the path or query every
  diagnostic shows only `<scheme>://` (`<schemeless endpoint>` for a schemeless
  value) — `http://user:1234/x@host` used to show `http://user:1234`; a
  non-ASCII host with an empty label or over 253 characters after punycode is
  invalid; a dotted quad with one trailing `.` (`10.0.0.1.`) is accepted; an
  http/protobuf value with an invalid path escape renders as
  `<unparseable URL>`.
- **New core dependency `idna>=3.7,<4`** (BSD-3-Clause; 3.7 fixes
  CVE-2024-3651) for UTS #46 host conversion (design D1 IDN row, A17). It was
  already installed through `requests`; it is now declared. Lower CI leg pins
  `idna==3.7`.
- **BREAKING — identity and resource rulings (design D1 rows of 2026-10-29,
  task A16; migration item 19).** `service_name=` / `environment=` beat the
  same key in `resource_attributes` in the emitted resource, not only in the
  resolved value. A blank `service.name`, `deployment.environment.name` or
  `service.instance.id` (empty or contract-trim-set only) in
  `OTEL_RESOURCE_ATTRIBUTES` or `resource_attributes` counts as not provided:
  the next precedence level applies (SDK default + warning; absent + ADR-003
  warning; SDK/helper-generated id). The helper parses
  `OTEL_RESOURCE_ATTRIBUTES` itself (`parse_resource_attributes`, no
  `OTELResourceDetector`): blank items ignored, split at the first `=`,
  percent-decode then trim with the contract set; an item without `=`, with
  an empty or non-UTF-8 key is skipped and an invalid `%` escape or non-UTF-8
  decode keeps the value literal, each with one warning per process naming
  item **positions** only. The SDK's own warning, which printed the raw item
  when `opentelemetry.sdk._logs` called `Resource.create()` at import, is
  dropped by a process-local `logging.Filter` on `opentelemetry.sdk.resources`
  during the helper's import and setup (only that record; the environment is
  never touched). The resource is built without `Resource.create()`:
  `telemetry.sdk.*` always come from the SDK (a code value warns once, naming
  the key), the schema URL is semconv 1.43.0 (the contract literal), the
  default service name is `unknown_service:<interpreter>` on every SDK
  version, and `OTEL_EXPERIMENTAL_RESOURCE_DETECTORS` is ignored with a
  once-per-process warning. On SDK ≥ 1.45 the helper restores its resource
  after the SDK's fork refresh, so detector attributes are not re-added and an
  explicit (code/env) `service.instance.id` survives the fork — the SDK used
  to replace it; a generated id still becomes the SDK's fresh per-process id
  (precedence: code > env > SDK detector on ≥ 1.45 > helper UUID). New
  `ValueError`s: an empty or non-string key in `resource_attributes`, and a
  non-string identity value (`service.name`, `deployment.environment.name`,
  `service.instance.id`). The "no service name" and ADR-003 warnings are now
  once per process (were once per options object).
- **Breaking — package renamed to `staffops-otel-helper` / `staffops_otel_helper`
  (ADR-011, from `0.3.0rc3`; `0.3.0rc2` was still `otel-helper`).** The
  distribution, the import package and the helper loggers carry the owner
  prefix, so a third party cannot squat the install name. The wheel also ships
  a deprecated `otel_helper` compatibility module (0.3.x only, removed in 1.0):
  one `DeprecationWarning` attributed to the importing line plus the same
  notice logged once on `staffops_otel_helper.compat` (design D8), re-exports
  of the public API, submodule imports (`otel_helper.ext.sql`,
  `otel_helper.metrics`, …) aliased to the same module objects so the helper
  keeps one copy of its state (safe under concurrent first imports), and `.pyi`
  stubs so type checkers resolve the old submodule paths. Logger names are not
  aliased. Design D6/D8/D14 amended 2026-10-29; CI, examples, e2e and docs use
  the new names (migration guide above).
- **Packaging:** the wheel and sdist now carry `LICENSE` (Apache-2.0 §4a), and
  the sdist excludes local coverage, cache and build leftovers.
- **Fix — the missing-extra `ImportError` no longer says `pip install
  otel-helper[<extra>]`.** The package is not on PyPI and the name was not
  registered there, so installing it by name could fetch an unrelated package.
  `staffops_otel_helper.ext.{aws,redis,sql}` now say "Install the staffops-otel-helper wheel
  with the [<extra>] extra (not on PyPI; see CONSUMING.md)"; `python/README.md`
  and `python/HOW-TO.md` install the wheel or a hash-pinned URL instead of the
  bare name (design D14, corrected 2026-10-28).

- **Fix — code options equal to the helper default no longer lose to the
  environment (design D1 edge-case rulings, A13).** `sample_ratio=1.0` beats
  `OTEL_TRACES_SAMPLER` and `OTEL_HELPER_SAMPLE_RATIO`;
  `prometheus_metrics_port=9464` beats `OTEL_HELPER_METRICS_PORT`;
  `disabled_metrics=[]` / `disabled_signals=[]` beat their env lists;
  `debug_level=False` beats `OTEL_HELPER_DEBUG_LEVEL=true`;
  `extra_instrumentation="SQL"` beats `OTEL_HELPER_EXTRA_INSTRUMENTATION`;
  `minimum_log_level=logging.NOTSET` is honored. **API change:** these
  fields (plus `export_interval_ms` and `metric_exporters`) default to `None`
  and only ever hold the code value; the effective values are on the new
  read-only `options.resolved` (`ResolvedOptions`, in `staffops_otel_helper.config`).
  So a value assigned after construction counts as code,
  `dataclasses.replace()` of a resolved object and a second
  `setup_telemetry()` with the same object (after `shutdown_telemetry()`)
  re-read the environment for everything code left unset, and an
  `OTEL_HELPER_SAMPLE_RATIO` value does not stick. Code types:
  `disabled_signals`/`disabled_metrics` accept lists or tuples of strings,
  `debug_level` bools or ints; other types raise `ValueError` (migration item 3).
- **Fix — `metrics_app()` can be served on the exact path.** It returns an ASGI
  callable-class instance, so `app.add_route("/metrics", metrics_app())` /
  `Route("/metrics", metrics_app())` answer `GET /metrics` with 200 (before,
  `add_route` raised `TypeError`). `app.mount("/metrics", …)` still works but
  redirects `/metrics` → `/metrics/` (307); use the route form. Served on its
  own, it completes the ASGI `lifespan` handshake and ignores non-HTTP scopes.
- **Fix — `staffops_otel_helper.ext.*` import errors name the right cause.** "library
  'X' is not installed" only for a `ModuleNotFoundError` of that extra's
  library (`botocore`, `redis`, `sqlalchemy`); "instrumentation not installed"
  only when the contrib instrumentor module (`opentelemetry.instrumentation.<lib>`)
  or one of its parent packages is missing; anything else (version skew,
  another missing `opentelemetry.*` module such as
  `opentelemetry.semconv._incubating.attributes`, a missing transitive
  dependency, no module name) reads `failed to import the <SQL|Redis|AWS>
  instrumentation: <error>`.

Config contract, task A3 (`specs/config-contract-v0.3/`, ADR-001, ADR-003,
design D1/D4/D12). Behavior changes, with migration notes:

- **BREAKING — stable HTTP semantic conventions (R10/D12).** `setup_telemetry()`
  and the `staffops_otel_helper.ext.*` entry points default `OTEL_SEMCONV_STABILITY_OPT_IN`
  to `http,database/dup` (A10 validation: was `http`) before any instrumentor runs: `http.server.duration` (ms, `http.method`,
  `http.target`) becomes `http.server.request.duration` (s, `http.request.method`,
  `http.route`, `url.scheme`), same for the client. A user value always wins;
  `http/dup` emits both generations. A value without `http`, or an
  instrumentation initialized before the helper, logs one warning. This
  matches the Go HTTP row above.
- **BREAKING — OTLP env vars are left to the SDK (ADR-001).** An absolute
  `http(s)://` env endpoint is no longer rewritten: the path prefix is kept
  (`.../otlp` → `/otlp/v1/<signal>`), an HTTP URL without a port uses 443/80
  instead of 4317, IPv6 literals work, and `OTEL_EXPORTER_OTLP_<SIGNAL>_ENDPOINT`
  / `_PROTOCOL` / `_HEADERS` / `_COMPRESSION` / `_TIMEOUT` are honored.
  "OTLP configured" is per signal: only a traces endpoint → traces exported,
  metrics fall back to Prometheus, logs stay local (no silent `localhost:4317`).
- **Helper-owned shims** (each warned where the input is suspicious):
  schemeless `host[:port]` → `https://` (`http://` only with
  `OTEL_EXPORTER_OTLP_INSECURE=true`), default port 4317/4318, deprecated for
  http/protobuf; a gRPC URL without a port → 4317 + "set an explicit port";
  `INSECURE` contradicting the scheme → the scheme wins for that signal;
  `CERTIFICATE` with a plaintext endpoint → warning; URL userinfo/query are
  stripped from gRPC targets.
- **BREAKING — `OTEL_EXPORTER_OTLP[_<SIGNAL>]_TIMEOUT` is milliseconds** (spec;
  the Python SDK reads seconds — D1.e). `10` now means 10 ms (warned as "looks
  like seconds"); use `10000`. `TelemetryOptions.export_timeout_ms` defaults to
  `None` (SDK default 10 s) instead of always passing 10 000.
- **gzip by default** when neither code (`TelemetryOptions.compression`, new)
  nor `OTEL_EXPORTER_OTLP[_<SIGNAL>]_COMPRESSION` sets it (D1.d). Env values are
  case-insensitive `gzip`/`none` (`none` no longer crashes the gRPC exporter);
  anything else, including `deflate`, warns and uses gzip.
- **Edge-case rulings** (design "D1 edge-case rulings"): boolean env values
  other than `true` (case-insensitive) are false with a warning
  (`INSECURE=yes` was ignored before); `OTEL_SDK_DISABLED=true` installs only
  propagators (no providers, exporters, `/metrics` listener or log handlers);
  `OTEL_EXPORTER_OTLP[_<SIGNAL>]_HEADERS` are parsed by the helper (malformed
  items skipped, warned by position only); URL userinfo/query/fragment are
  stripped from every endpoint (put credentials in `*_HEADERS`) — this also
  fixes `.../otlp?x=1` losing every HTTP signal; an invalid per-signal endpoint
  or timeout falls back to the generic one; `unix://`/`ftp://` endpoints leave
  the signal unconfigured; whitespace-only `OTEL_SERVICE_NAME`/`SERVICE_NAME` is
  unset; forking after setup on sdk 1.42–1.44 (`--preload`) warns. Header
  keys must match `[A-Za-z0-9._-]+` (case-insensitive, last wins; values
  percent-decoded, invalid escapes kept and warned); env timeouts are decimal
  milliseconds only (`^[0-9]+(\.[0-9]+)?$`: `1e3`, `.5`, `+5` warn and fall back)
  clamped to [1 ms, 24 h]; unparseable endpoints — including a control
  character inside the URL — are shown as `<unparseable URL>` and fall back;
  endpoint env values are trimmed; an invalid `%` escape or non-UTF-8 header
  value is kept whole and literal, a decoded control character skips the item;
  IPv6 zone ids warn over http/protobuf (urllib3 can't resolve them).
- **Code values fail fast**, naming the option: wrong types, `sample_ratio`
  outside [0, 1] or NaN, `prometheus_metrics_port` outside [0, 65535],
  unknown `disabled_signals`, `insecure` contradicting the code URL's scheme.
- **BREAKING — service name (R5).** `OTEL_SERVICE_NAME` and
  `OTEL_RESOURCE_ATTRIBUTES` now beat `SERVICE_NAME`; with none set the helper
  sets nothing (SDK `unknown_service*`) and warns; the `my-service` default is
  gone (`TelemetryOptions.service_name` defaults to `None`).
- **BREAKING — environment (R4, ADR-003).** Unset → `deployment.environment.name`
  absent + one warning, log level INFO (was `LOCAL`/DEBUG); unknown values are
  emitted verbatim (were `LOCAL`); `OTEL_RESOURCE_ATTRIBUTES` beats `ENVIRONMENT`
  and is never overwritten. `TelemetryOptions.environment` defaults to `None`
  and also accepts a string.
- **`service.instance.id` (D4).** When neither code nor `OTEL_RESOURCE_ATTRIBUTES`
  nor the SDK (≥ 1.45) provides one, the helper adds a per-process UUID (sdk
  1.42–1.44). An explicit id in a worker forked after import warns (never
  rewritten). Call `setup_telemetry()` only in each worker (gunicorn
  `post_fork`), not in the preloaded module — a second call in a worker is a
  no-op; a worker forked after setup ran in the parent warns on its next
  helper call.
- **Env parsing never raises (R8).** Unknown/unparseable env values warn and
  fall back: `OTEL_METRICS_EXPORTER` (unknown, `none,x` → none, `otlp` without a
  metrics endpoint → legacy inference), `OTEL_METRIC_EXPORT_INTERVAL` ≤ 0 or
  garbage, `OTEL_EXPORTER_OTLP_PROTOCOL`, `_INSECURE`, `_TIMEOUT`,
  `_COMPRESSION`, `OTEL_SDK_DISABLED`, `OTEL_HELPER_*` numbers/booleans. Invalid
  values passed in code still raise `ValueError` naming the field.
- **`OTEL_{TRACES,METRICS,LOGS}_EXPORTER`**: `none`, `console` (new),
  deprecated `logging` (→ `console` + warning) and lists; they beat the
  deprecated `OTEL_HELPER_DISABLED_SIGNALS` for their signal.
- **`OTEL_PROPAGATORS` is honored** for `tracecontext`, `baggage` and `none`;
  any other value (`b3`, `jaeger`, `xray`, …) is warned and skipped (W3C default
  when nothing supported is left) instead of the API's `ValueError`; casing and
  empty tokens are normalized. This needs `staffops_otel_helper` imported before any
  other OpenTelemetry module.
- **New options**: `traces_endpoint` / `metrics_endpoint` / `logs_endpoint`
  (used verbatim), `compression`. A generic `otel_endpoint` set in code always
  gets `/v1/<signal>` appended for http/protobuf: a code value already ending in
  `/v1/traces` now becomes `/v1/traces/v1/traces` — pass it as
  `traces_endpoint` instead. `resolve_insecure()` was removed (TLS follows the
  scheme).
- **`TelemetryOptions` after setup**: `service_name` is `None` when nothing
  configures one, and `environment` is `None` when unset or a plain `str` for
  values outside LOCAL/DEV/HML/PRD — code reading `options.environment.value`
  must handle both.
- **Examples**: `python-api` / `python-backend` pin grpcio 1.76 and
  `protobuf>=6.33.5,<7`, and drop the redundant contrib `0.63b1` pins. The old
  `protobuf==5.27.0` pin crashed at start with `VersionError`: the unpinned
  `googleapis-common-protos` (1.76.0, a dependency of the OTLP exporters, whose
  `google/rpc/*_pb2.py` the gRPC exporter imports) is generated with protobuf
  6.33.5 (opentelemetry-proto itself is 5.26.1 gencode).
- **Diagnostics** go only to the `staffops_otel_helper.*` loggers and never print
  `*_HEADERS`, `*_CLIENT_KEY` or URL userinfo/query values; a source-scan test
  enforces the channel.

Sampler, task A4 (R6, design D3, ADR-002):

- **BREAKING — the sampler is ParentBased.** Default is `parentbased_always_on`:
  a child of a remote `traceparent` with `sampled=0` is no longer recorded
  (was: always sampled). `sample_ratio < 1.0` in code and the deprecated
  `OTEL_HELPER_SAMPLE_RATIO` (used only while `OTEL_TRACES_SAMPLER` is unset)
  now build `ParentBased(TraceIdRatioBased(x))` instead of a bare ratio sampler.
  Migration: prefer `OTEL_TRACES_SAMPLER=parentbased_traceidratio` +
  `OTEL_TRACES_SAMPLER_ARG`.
- `OTEL_TRACES_SAMPLER` / `_ARG` are parsed by the helper (spec names,
  case-insensitive, whitespace trimmed). An unknown name logs one warning on
  `staffops_otel_helper.tracing` and uses the default; an invalid or out-of-range `_ARG`
  warns and uses 1.0 (SDK 1.42 raised `ValueError` at setup for e.g. `_ARG=2`).
  The spec names the helper does not implement (`jaeger_remote`,
  `parentbased_jaeger_remote`, `xray`) warn "unsupported by this helper".
- `_ARG` and `OTEL_HELPER_SAMPLE_RATIO` accept plain decimals only
  (`^[0-9]+(\.[0-9]+)?$`, as the timeouts): `2.5e-1`, `0.2_5`, `+0.5`, `.5` and
  full-width digits now warn and use 1.0 (were accepted by `float()`).

Packaging and Prometheus, task A8 (P14, P17):

- **BREAKING — `/metrics` uses a dedicated `CollectorRegistry`.** The standalone
  listener and `metrics_app()` serve only OTel-produced series: no `process_*`,
  `python_gc_*`, `python_info`, and no metrics the application registered on
  `prometheus_client.REGISTRY` (serve those yourself if you need them). The OTel
  series no longer appear on the global `REGISTRY`. A failed listener bind now
  unregisters the reader.
- The package ships `py.typed` and no longer imports `opentelemetry.*._internal`
  (`TraceBasedExemplarFilter` comes from `opentelemetry.sdk.metrics`); a test
  enforces both.

Lifecycle, task A6 (P12; mirrors Go G1/G2):

- **BREAKING — a second `setup_telemetry()` returns the first resolved options**
  (was: the options passed to it, unresolved), ignoring its argument.
- **Setup is all-or-nothing.** Every signal is built before anything global is
  installed. If building fails (exporter error, `/metrics` port in use, an
  instrumentation error), what was built is shut down and the instrumentors it
  turned on are uninstrumented, the previous propagator and log levels stay, no
  handler is attached, and a later call can retry. Before, a busy port left a
  global TracerProvider installed and the helper half set up.
- **New `shutdown_telemetry(timeout_ms=30000)`**: flushes and shuts down the
  trace, metric and log providers concurrently under one deadline, uninstruments
  what setup instrumented, removes and closes the helper's log handler (restoring
  the log levels it changed) and stops the standalone `/metrics` listener.
  Thread-safe: the first call does the work, and every later or concurrent call
  repeats its result — `None`, or the same `RuntimeError` (joining every error or
  timeout) — until the next successful setup. It is registered with `atexit`
  (errors logged there) and replaces the SDK's per-provider exit hooks.
  **Python skips `atexit` on an unhandled SIGTERM** (`docker stop`, Kubernetes):
  handle SIGTERM in the app or call it from the server's shutdown hook (HOW-TO
  "Shutdown and SIGTERM"; `example/python-process` shows it). The helper
  installs no signal handler.
- **Limitation, Python only:** the OpenTelemetry Python API sets the global
  Tracer/MeterProvider once per process, so a setup after `shutdown_telemetry()`
  re-enables logs and propagators but not traces/metrics (a warning says so, and
  `options.is_signal_enabled("traces"/"metrics")` returns False). Go rebuilds all
  three.
- **BREAKING — mounted `metrics_app()` status codes** (same table as Go's
  `MetricsHandler`): **503** before setup, after a failed setup and after
  shutdown; **200 with an empty body** when Prometheus is off by configuration
  (`OTEL_SDK_DISABLED=true`, metrics disabled, or `OTEL_METRICS_EXPORTER` without
  `prometheus`); the OTel series otherwise. 0.2.0 always answered 200 and served
  the global registry (`process_*`/`python_*` even with no helper metrics), so a
  target scraped before setup or after shutdown now reports `up=0` and can fire
  TargetDown-style alerts — scrape only while the app is serving.
- `reset_telemetry()` (testing) now calls `shutdown_telemetry()`.

Logging, task A5 (P10, P16, design D6):

- **BREAKING — no OTel log handler without a logs exporter.** 0.2.0 always put
  the SDK `LoggingHandler` on root, so with no endpoint (or
  `OTEL_LOGS_EXPORTER=none`) records went nowhere and `logging.basicConfig()`
  did nothing. Now: logs exporter active → the
  `opentelemetry-instrumentation-logging` handler (the SDK one is deprecated) on
  root and on uvicorn's non-propagating `uvicorn`/`uvicorn.error`/`uvicorn.access`
  loggers, each record exported once, kept across `basicConfig`/`dictConfig`/
  `fileConfig` (which otherwise skip or drop it); no exporter → no OTel handler,
  and while the app configures no root handler its records go to stdout as
  single-line JSON (`time`, `level` DEBUG/INFO/WARN/ERROR, `msg`, `logger`, extra
  attributes, `exception`; same shape as Go) at the environment level — a
  console fallback installed as `logging.lastResort`, so a later
  `basicConfig()` simply takes over. `OTEL_LOGS_EXPORTER=otlp,console` → both.
- Helper diagnostics (`staffops_otel_helper.*`, INFO+; that logger is set to INFO when
  left NOTSET) and SDK diagnostics (`opentelemetry.*`, WARNING+) always reach
  stderr, also with a logs exporter active (until the app configures a root
  handler; re-enabled after `dictConfig(disable_existing_loggers=True)`). `opentelemetry.*` records are never exported through OTLP (no
  export-failure feedback loop); `staffops_otel_helper.*` records are also exported.
- A second OpenTelemetry `LoggingHandler` on root (`opentelemetry-instrument`,
  `LoggingInstrumentor().instrument()`) is removed with a warning, so records
  are exported once; set `OTEL_PYTHON_LOG_AUTO_INSTRUMENTATION=false`. It is
  removed before the helper installs its providers.
- **BREAKING — root level.** Set to the environment level (R4: unset/unknown →
  INFO) only when root has no handlers and is at the stdlib default WARNING;
  quieted framework loggers (`urllib3`, `grpc`, …) only when they are NOTSET.
  Shutdown restores what it changed.
- **BREAKING —** `code.file.path`/`code.function.name`/`code.line.number` are no longer added
  by default (0.2.0 always did); set `OTEL_PYTHON_LOG_CODE_ATTRIBUTES=true`
  (upstream variable). Matches Go.
- `set_logger_provider()` is called (once per process), so code using the OTel
  Logs API (`opentelemetry._logs`) exports too.

Health exclusion and instrumentation, task A7-py (R7, P11, P13, design D5):

- **BREAKING — health requests stay in HTTP server metrics.** FastAPI no longer
  gets `excluded_urls`; a sampler wrapping the helper sampler drops SERVER spans
  (no parent or remote parent) whose `url.path`/`http.target` is one of
  `/health /healthz /ready /readyz /live /livez /ping /metrics` (exact,
  case-insensitive, trailing slash/query ignored; 0.2.0: `/ping /health /healthz
  /ready` as regexes, so `/users/ping` was not traced). Children follow under a
  parent-based sampler (not with an explicit `always_on`/`traceidratio`).
  `TelemetryOptions(health_paths=[...])` replaces the HTTP list (`[]` = none).
  User `OTEL_PYTHON_FASTAPI_EXCLUDED_URLS`/`OTEL_PYTHON_EXCLUDED_URLS` keep their
  upstream meaning (span and metric dropped). **Migration:** probes now count in
  `http_server_request_duration_seconds`; SLI/latency queries should exclude
  health routes (`http_route!~"/health|/healthz|/ready.*|/live.*|/ping|/metrics"`).
- **BREAKING — client-side health exclusion is exact.** httpx/requests calls to
  the same health list (incl. `health_paths`) drop the client span and metric
  (upstream `excluded_urls`, anchored patterns merged with the user's
  `OTEL_PYTHON_*_EXCLUDED_URLS`); 0.2.0 matched `/ping` etc. as substrings
  (`/users/ping` dropped) and wrote `OTEL_PYTHON_HTTPX/REQUESTS_EXCLUDED_URLS`
  into the environment — the helper no longer mutates env.
- gRPC servers and clients (sync and aio): the whole `grpc.health.v1.Health`
  service is not traced (native filter). `health_paths` must be ASCII without
  `?`, `#` or `,`. `OTEL_PYTHON_GRPC_EXCLUDED_SERVICES` is folded into
  that filter (upstream would OR it with any filter and re-enable the excluded
  services). Spans a health servicer starts become roots (no server span).
- Removed `otel_helper.tracing.HEALTH_PATHS` / `otel_helper.instrumentation.HEALTH_PATHS`;
  use `staffops_otel_helper.DEFAULT_HEALTH_PATHS`.
- Sync `grpc.server()` / `grpc.insecure_channel()` / `grpc.secure_channel()` are
  instrumented like `grpc.aio` (spans + propagation).
- New `instrument_fastapi(app)` for apps whose `FastAPI` class was imported
  before `setup_telemetry()` (idempotent).

Deprecations and debug marker, task A9 (R8, R9, D8, ADR-006):

- **Deprecated (removed in 1.0), warned once per process** on the `staffops_otel_helper`
  logger, naming the replacement and never the value. Each warns whenever it is
  set, also when a standard variable or a code value wins; behavior is unchanged:
  `SERVICE_NAME` → `OTEL_SERVICE_NAME`; `ENVIRONMENT` →
  `OTEL_RESOURCE_ATTRIBUTES=deployment.environment.name=<value>`;
  `OTEL_HELPER_SAMPLE_RATIO` → `OTEL_TRACES_SAMPLER=parentbased_traceidratio` +
  `OTEL_TRACES_SAMPLER_ARG`; `OTEL_HELPER_DISABLED_SIGNALS` →
  `OTEL_{TRACES,METRICS,LOGS}_EXPORTER=none`; `OTEL_HELPER_EXTRA_INSTRUMENTATION`
  → the explicit `staffops_otel_helper.ext.{sql,redis,aws}.instrument_*()` calls. An empty
  `OTEL_HELPER_EXTRA_INSTRUMENTATION` warns in Python (it still turns the `SQL`
  default off) but not in Go (no effect there): intentional. Schemeless
  http/protobuf endpoints (warned since A3) now warn once per process with Go's
  wording ("schemeless endpoint is deprecated for http/protobuf and will be
  removed in 1.0").
- The implicit `SQL` default is kept (SQLAlchemy is still instrumented globally
  when the `[sql]` extra and SQLAlchemy are installed), but it now logs one
  warning, as do debug-mode-enabled extension instrumentors. **Migration:** call
  `staffops_otel_helper.ext.sql.instrument_sql()` (or `instrument_redis()` / `instrument_aws()`)
  **before** `setup_telemetry()`. Setup then finds the instrumentor active and
  logs nothing.
- **Debug marker on entry spans:** with debug mode on, `debug=true` is set on
  spans with no parent **or a remote parent** (0.2.0: parentless spans only, so
  a service called by an instrumented client never marked its server spans).
  Spans with a local parent are not marked. Debug mode comes from local config
  only (`OTEL_HELPER_DEBUG_LEVEL` / `debug_level`): inbound tracestate or baggage
  never sets the marker, and the helper writes no tracestate.

Unsupported instrumentation versions, task A10 (P18):

- Re-validated 2026-10-28 on contrib 0.63b0 and 0.66b1: with SQLAlchemy 2.1.x
  (2.1.6), `SQLAlchemyInstrumentor` (`sqlalchemy >= 1.0.0, < 2.1.0`) instruments
  nothing. Upstream logs one ERROR record that does not name the helper and,
  on 0.63b0, does not say that nothing was instrumented. SQLAlchemy 2.0.55 works.
  The redis (`redis >= 2.6`) and botocore (`botocore~=1.0`) instrumentors accept
  redis 8.1.0 and botocore 1.43.120 on both legs.
- When an instrumentor (auto-instrumented by `setup_telemetry()`, or through
  `staffops_otel_helper.ext`) refuses an **installed** library version, the helper now
  logs one warning per process naming the instrumentor, the contrib version, the
  installed version and the supported range. A library that is not installed
  stays silent.
- `staffops_otel_helper.ext.instrument_sql()` / `instrument_redis()` / `instrument_aws()`
  now return `True` when the instrumentor is active and `False` when it refused
  (0.2.0: `None`).
- **Pin SQLAlchemy `<2.1` until a contrib release supports 2.1** (none within
  the current `<0.67b0` cap).
- **Database semconv (R10):** the default opt-in now includes `database/dup`,
  so SQLAlchemy spans carry `db.system.name` **and** `db.system` (0.2.0:
  `db.system` only), matching Go's `ext/otelsql`. Verified on contrib 0.63b0 and
  0.66b1 with SQLAlchemy 2.0.55. A user value wins (`database` → new keys only).
  **Known gap:** contrib copies the dialect name unmapped (SQL Server → `mssql`;
  Go → `microsoft.sql_server`).

### Changed

- **Node.js: a process that ends on its own now flushes its telemetry (Phase
  B, B12 fix round, 2026-11-03; not released).** The first successful
  `setup()` of the main thread registers one `process.once('beforeExit')`
  listener that calls `shutdown()` when the app has not. The natural exit then
  **waits for the flush**: up to the export timeout per signal
  (`OTEL_EXPORTER_OTLP_TIMEOUT`, 10 s by default) when the Collector is
  unreachable, bounded by the 30 s `shutdown()` deadline; call
  `await shutdown(5000)` yourself to bound it (the listener then does
  nothing). There is no opt-out. `process.exit()` and signals skip it:
  `await shutdown()` before `process.exit()` and keep the SIGTERM handler.
  Telemetry your own `beforeExit` listener emits may be dropped; a worker
  thread is not flushed when it ends (design D-JS10, `nodejs/HOW-TO.md` §4).
- **Go docs: the `CLIENT MAINT_NOTIFICATIONS` ERROR span.** On Redis < 8 it is
  recorded on the **first connection of each client**, in the trace of the
  request that opened it — not harmless; `go/HOW-TO.md` §12 shows the
  `ModeDisabled` fix.

- **BREAKING — Go: dependency floor raised to Go 1.26 (0.3.0 task A2, ADR-005).**
  `go.mod` is `go 1.26.0`; otel / sdk / trace / metric / log / sdk/log and the
  OTLP trace + metric exporters v1.47.0 (logs API now stable), OTLP log
  exporters v0.23.0, `exporters/prometheus` v0.69.0, contrib `otelhttp` /
  `otelgrpc` / `runtime` v0.72.0, `otelslog` v0.21.0, gRPC v1.83.2,
  `golang.org/x/net` v0.62.0 (GO-2026-6611/-6612/-6617/-6860/-6866); `ext/otelredis` go-redis v9.7.3 (GO-2025-3540). The `ext/*` modules
  (contrib `otelaws` v0.72.0) and examples follow. CI tests Go 1.26.x and
  1.27.x and runs `govulncheck`.
- **Go: `NewHTTPHandler`'s `operation` argument is ignored** (kept for source
  compatibility): spans are named after the route (table above).
- **Python: lockstep dependency bounds (design D9.a).** api / sdk / OTLP
  exporters `>=1.42,<1.46`; every contrib package (core and the `aws` /
  `redis` / `sql` extras) `>=0.63b0,<0.67b0`. Contrib betas pin
  semconv/instrumentation exactly, which couples them to one SDK minor, so
  unbounded ranges pulled in untested SDKs. New core dependency
  `opentelemetry-instrumentation-logging` (same bounds; Apache-2.0), the log
  handler since A5.
- **Go examples:** Dockerfiles build with context `go/` (the old per-example
  context could not see the `replace ../../` helper source) and run as
  uid 65534.

### Removed

- **BREAKING — Go: `UnaryServerInterceptor`, `StreamServerInterceptor`,
  `UnaryClientInterceptor`, `StreamClientInterceptor`** (otelgrpc removed
  interceptors in v0.61.0). Use the stats-handler API:
  `grpc.NewServer(otelhelper.GRPCServerOption())`,
  `grpc.NewClient(target, otelhelper.GRPCDialOption())`; `GRPCServerHandler()`
  / `GRPCClientHandler()` return the `stats.Handler` itself.

### Fixed

- **Python: `OTEL_PROPAGATORS=none` failed every server request (task A34,
  owner decision 2026-11-10 to fix ahead of the queue).** The helper installed
  an empty `CompositePropagator`, whose `extract()` returns the `context`
  argument, `None` when the caller passes none. The ASGI (FastAPI)
  instrumentation attached that `None` and the SDK then raised
  `AttributeError: 'NoneType' object has no attribute 'get'`, so every request
  answered 500, with or without headers, also with `OTEL_TRACES_EXPORTER=none`
  and with traces disabled through `OTEL_HELPER_DISABLED_SIGNALS=traces`
  (measured on the unfixed code). `OTEL_SDK_DISABLED=true` installs no
  instrumentation and is believed unaffected (not measured). The helper's
  propagator (`HelperCompositePropagator`) now always returns a `Context` from
  `extract()`: the passed one, or a fresh empty `Context` when none is given.
  Under `none` a server starts a new trace, inbound `traceparent` and `baggage`
  are ignored, and nothing is injected. Callers that pass `extract(carrier)`
  as a span parent (Celery/Kafka consumers, gRPC servers, custom code) now
  start a new root under `none` instead of continuing the current context.
  `tracecontext`-only, `baggage`-only and lists with skipped tokens were not
  affected. WSGI servers attach the extracted context the same way and are
  covered by the same fix (inferred; no WSGI test, Flask is not a test
  dependency). **Affected release: `0.3.0rc3`** (earlier release candidates
  not checked). Workaround on `0.3.0rc3`: leave `OTEL_PROPAGATORS` unset or set
  `tracecontext` / `baggage` instead of `none`. Tests:
  `tests/test_a34_propagators_none.py`; the test suite's global textmap is now
  reset around each test. Parity case
  `propagation.none_server_ignores_inbound_traceparent` passes for Python.
- **Python: a client key without a client certificate (or the reverse, or an
  unreadable one) aborted the process** in grpc's C core
  (`grpc_tls_certificate_distributor.cc` CHECK, SIGABRT). The helper now
  validates the pair first; an incomplete or rejected pair warns and the
  signal uses server authentication (task A18).
- **Python: an export batch size above the queue size failed setup**
  (`OTEL_BSP_MAX_EXPORT_BATCH_SIZE` > `OTEL_BSP_MAX_QUEUE_SIZE`, or the
  `OTEL_BLRP_*` pair: the SDK raised `ValueError`). The helper now warns
  naming both variables and passes the queue size (task A18).
- **Python: `-bin` gRPC header keys and non-ASCII header values failed every
  export**; the former are now sent as bytes, the latter skipped with a
  warning (A18).
- **Security — Go: OTLP/gRPC logs ignored `OTEL_EXPORTER_OTLP_CERTIFICATE`
  under `OTEL_EXPORTER_OTLP_INSECURE=true` and were sent in plaintext**
  (otlploggrpc v0.7.0). v0.23.0 uses TLS with the env CA, as traces and
  metrics already did.
- **Go: IPv6 literal collector endpoints.** `http://[::1]:4318` became
  `::1:4318`, which the v1.47 OTLP/HTTP exporters reject at setup and OTLP/gRPC
  could not dial; the host is now re-bracketed (`[::1]:4318`).

### Examples and documentation

- **New custom-signals examples:** `go/example/go-custom-signals` and
  `python/example/python-custom-signals` create the same telemetry side by
  side — a batch root span with one child span per order, a named error type
  recorded on the failing span, the four metric instruments (`order.processed`,
  `order.duration`, `order.active`, `order.queue.depth`) with `order.type` /
  `error.type` / `queue.name` attributes, and logs correlated with the active
  span. Both run against a Collector or, with no endpoint, on the stdout
  fallback; their READMEs record the output.
- **New `go/TESTS.md`** (and its MkDocs page): how to run the Go suite, the
  parity fixture and coverage in Docker.
- **go-api:** `uptime_seconds` was always 0 and now reports the process
  uptime; the outbound client span is parented to the `detect.status` span;
  failed spans call `SetStatus(codes.Error, …)` after `RecordError`.
- **Go examples' shutdown budget:** go-api, go-backend and go-process drain
  for at most 4 s and flush for at most 5 s, inside `docker stop`'s default
  10 s grace period (go-api used to allow 10 s + 5 s).
- **go-process:** sets the environment with
  `OTEL_RESOURCE_ATTRIBUTES=deployment.environment.name=…` instead of the
  deprecated `ENVIRONMENT`.
- **python-api:** instruments the app with `instrument_fastapi(app)` (the
  `FastAPI` class is imported before setup) and flushes telemetry (≤ 5 s) in
  the lifespan shutdown.
- **python-backend:** flushes telemetry on SIGTERM and SIGINT (handlers
  registered before `server.start()`; grace 4 s + flush 5 s, inside
  `docker stop`'s 10 s, was 5 s + 10 s), and its histograms use second-based
  bucket boundaries.
- **python-process:** stops on SIGTERM/SIGINT through
  `loop.add_signal_handler` and an `asyncio.Event` instead of `sys.exit` from a
  signal handler, so the workers finish and telemetry is flushed.
- **Example READMEs:** the legacy metric names of the api/backend/process apps
  (`*_total`, `*_ms`, no unit, `process.*`) are marked as kept for the shared
  dashboards, not as a model; build/run commands work from the repo root in
  bash and zsh.
- **Node.js examples run the published helper (task B14, 2026-11-05).**
  `node-api`, `node-backend`, `node-process` and `node-custom-signals` now
  depend on `@staffops/otel-helper` `0.3.0-rc.1` (exact, from npmjs), and
  their lockfiles lock the helper's dependency tree with the registry's
  integrity hashes, so `npm ci` installs the same bytes every time. The
  default `Dockerfile` build installs that; `--build-arg
  HELPER_SOURCE=workspace` builds the core from the local tree and installs
  it over the published one (a commit under development). The CI images
  (`Dockerfile.demo`) keep installing the commit's own tarball, now over the
  locked install. The workspace build creates its `/tarballs` directory
  first: `npm pack --pack-destination` of npm 11 does not, and that build
  failed. `ci/version.mjs set` leaves the examples' pin alone (a version that
  is not published cannot be locked); the new `ci/version.mjs examples
  <version>` moves it after the publish, and `check` rejects a range, a
  drifted lockfile, a missing integrity or any package locked from another
  registry than npmjs. GitHub Packages
  returns `dist.integrity` for `0.3.0-rc.1`, identical to npmjs, so a re-run
  of `publish-nodejs-ghp` skips the packages it already published.

### Internal

- **A30 owner rulings of 2026-11-06 and 2026-11-08.** Node.js: `httpOptInMetrics` / `OTEL_HELPER_HTTP_OPT_IN_METRICS`
  accepted and resolved (`Resolved.httpOptInMetrics`) with no effect —
  instrumentation-http 0.300 has no opt-in instruments
  (`skip:no-upstream-node-http-opt-in-metrics`; one INFO line when it is on);
  `-aws` names every SDK call span `<Service>.<Operation>` (core
  `dbTracerProvider` option `spanKind`; kinds PRODUCER on SQS/SNS send and
  publish, CONSUMER on SQS receive, CLIENT otherwise, owner amendment
  2026-11-08), adds `messaging.operation.name` on SQS and SNS spans, and maps the RPC
  keys by `OTEL_SEMCONV_STABILITY_OPT_IN` (`rpcMode`); `server.address`/`port`
  stay absent (a documented gap, owner Q4 (a)); `-sql` writes the legacy
  `db.statement` in the legacy-keys mode, also when pg sets `db.query.text`
  after start (coordinator decision Q5). **Node.js upgrade note:** AWS span
  names `<queue> send` / `<queue> receive` / `<topic> send` / `<function>
  Invoke` become `<Service>.<Operation>` (kinds unchanged). **Trace-derived
  telemetry (Node.js):** spanmetrics `span_name` series change with the AWS
  and gRPC names (`grpc.pkg.Svc/Method` → `pkg.Svc/Method`); Collector `filter`
  / tail-sampling rules and TraceQL on those names (`grpc.grpc.health…`, `<queue>
  send`) stop matching; DynamoDB `db.system.name` becomes `aws.dynamodb`, and the
  service-graph node does not change in the default dup mode: Tempo and the
  Collector take the first of `db.namespace` (the table name, when the call has
  one), `db.name`, `db.system`, `db.system.name`. It moves from `dynamodb` to
  `aws.dynamodb` only in the stable `database` mode for calls without a table;
  kinds, and so edge types, stay as they were.
  New snapshot fields `resolve.http_opt_in_metrics` (code key
  `http_opt_in_metrics`) and `grpc_server.client_span_name`, and new
  `http.opt_in_metrics_*` and `grpc.*` cases (.NET `xfail:C11`, body size
  `skip:no-upstream-dotnet-http-body-size`; Go
  `skip:no-upstream-go-otelhttp-active-requests` for active requests).

- **A30 instrumentation parity backlog (contract task A30, 2026-11-05).** Node.js: instrumentation-grpc is
  bound to `grpcNamingTracerProvider`, so its spans start as
  `<service>/<method>` (was `grpc.<service>/<method>`) and the health sampler
  matches `grpc.health.v1.Health/` as in Go and Python; `-redis` writes the
  legacy `db.statement` (= `db.query.text`) in the legacy-keys mode (core
  `dbTracerProvider` option `legacyStatement`); `-aws` maps DynamoDB
  `db.system.name` `dynamodb` → `aws.dynamodb` (the legacy `db.system` stays
  `dynamodb`); `REDACTED_QUERY_PARAMS` gains `access_token`,
  `X-Goog-Credential` and `GoogleAccessId` and is passed to
  instrumentation-http as `redactedQueryParams` / `redactedQueryParamsServer`.
  **Node.js upgrade note:** queries on `grpc.pkg.Svc/Method` span names and on
  `db.system.name="dynamodb"` must use `pkg.Svc/Method` and `aws.dynamodb`.
  New parity snapshot field `server_span_name` (Go, Python and Node.js
  drivers) and case `grpc.server_span_name_semconv` (358 in total; .NET
  `xfail:C11`). The owner questions of this round (HTTP opt-in metrics, AWS
  RPC keys, AWS messaging span kinds and names, Node AWS
  `server.address`/`server.port`) were ruled on 2026-11-06 and 2026-11-08 (entry
  above).

- **A29 final round (2026-11-05, after two confirmation passes).** In Go,
  Python and Node.js the masked header values are the union of the values
  captured when setup builds the exporters and the variables' current values
  (deleting or rotating `*_HEADERS` after setup no longer unmasks what the
  exporters still send); masking marks every occurrence of every form,
  overlapping ones included, and replaces each marked run by one `<redacted>`;
  a leading auth-scheme name (`Bearer`, `Basic`, `Digest`, `Negotiate`) is not
  masked on its own. Node.js caches the forms per header set. Go: an error
  whose gRPC status carries details is always rewrapped; the chain walk visits
  each pointer error once (a self-referencing chain no longer runs forever);
  `errors.As` fails for every typed error of the sanitized chain, documented.
  Fixture: `metrics.port_prometheus_port_both_invalid` counts the echoed
  values, not the fallback wording.
- **A29 fix round (2026-11-05; observability, code-review and dev reviews,
  no BLOCKING).** 15 more parity cases (357 in total): the token-only and
  overlapping-value echo cases (`export.http_error_reason_token_header_values_masked`,
  `export.grpc_error_token_header_values_masked`,
  `export.grpc_error_overlapping_header_values_masked`), three gRPC path cases
  (schemeless, skipped invalid value, http/protobuf not warned) and nine
  `metrics.port_prometheus_*` cases; the two error-text cases gain a
  body-only marker and positive controls, the path cases match the variable on
  the warning line. fakeotlp: `x-parity-echo-error=token` echoes only the
  credential (HTTP reason phrase, gRPC message), and the 400 body carries
  `parity-body-marker`. New fixture `code` key `prometheus_metrics_port`.
  Node.js (unreleased since `0.3.0-rc.1`): `maskLiterals` replaces every form
  in one pass, longest first (insertion order let `team` leave
  `<redacted>-secret-token`), adds the token parts, the form-encoded and the
  HTML-escaped forms, and trims header values with the contract set; the
  bind-failure record and the worker/cluster notices name the option or
  variable that set the port (and `OTEL_EXPORTER_PROMETHEUS_HOST`).
- **A29 parity cases and Node.js alignment (contract task A29, 2026-11-05;
  not yet independently reviewed).** 18 parity cases (342 in total):
  `export.http_error_body_header_values_not_logged`,
  `export.grpc_error_details_header_values_masked`,
  `export.grpc_endpoint_path_ignored_warned`,
  `export.grpc_per_signal_endpoint_path_ignored_warned`,
  `export.grpc_code_endpoint_path_ignored_warned`,
  `export.grpc_endpoint_root_path_not_warned`, eleven
  `metrics.port_prometheus_*` and `metrics.listener_prometheus_host_loopback`
  (.NET `xfail:C8`, `xfail:C7b`, `xfail:C12`). The `resolve` snapshot gains
  `prometheus_metrics_host` in the three runners. fakeotlp answers a request
  carrying `x-parity-echo-error` with an error echoing its `x-parity-*`
  headers (HTTP 400 JSON body, gRPC `INVALID_ARGUMENT` message). Node.js
  (unreleased since `0.3.0-rc.1`, a behavior change for rc.1 users): the same gRPC path warning (`otlp.ts`), `OTEL_EXPORTER_PROMETHEUS_PORT`
  / `_HOST` (`metrics-endpoint.ts`, new `Resolved.prometheusMetricsHost`),
  SDK error text unchanged (B14 already drops bodies and masks the header
  values). Specs: three Deferred rows of `specs/config-contract-v0.3/tasks.md`
  promoted to A29; the .NET Phase C decision O10 (a) superseded (C12 honors the
  Prometheus variables).
  Measured 2026-11-05: the 18 cases pass in Go, Python (3.13 latest; 3.10 and
  3.13 lower) and Node.js (24, 22.15.0, 26); with Go's sanitizer disabled, or
  Python's filter not installed (sdk 1.45), the error-text cases fail. Existing
  tests updated to the new rule: Go `TestResolveEndpoint`,
  `TestResolveEndpointA17Rulings` and
  `TestSetup_SDKDoesNotEchoMalformedResourceItems`, Python `test_shims` logger
  scan, the Node.js `Resolved` key list.
- **Node.js workspace scaffolding (Phase B, tasks B1a–B1c; not released).**
  `nodejs/` is an npm workspace (private root, committed `package-lock.json`,
  `npm ci --ignore-scripts`, engines `>=22.15.0`) with placeholder packages
  `@staffops/otel-helper` (subpaths `/hooks`, `/register`, `/testing`) and
  `@staffops/otel-helper-{aws,redis,sql,pino,winston}` (factories throw
  "not implemented yet (task B10x)"). TypeScript 6.0 compiles once to CJS; a
  hand-written ESM wrapper re-exports it, so `require` and `import` share one
  state on `Symbol.for('staffops.otel-helper.state.v1')` (a test asserts it).
  `/hooks` installs import-in-the-middle (`^3.5.3`) sync hooks where
  supported, else `module.register`; it skips Node's internal hooks thread,
  and under `--eval`/`--print` (iitm sync hooks crash there) it uses the async
  loader, or on Node >= 26 no ESM hook plus one diagnostic line. The shared
  state never throws: an unreadable, foreign or other-version slot makes the
  copy inactive with one diagnostic line. Dependencies: OTel JS `~3.0.0` /
  `~0.300.0`, `@opentelemetry/api` peer `>=1.10.0 <1.11.0` (what the SDK
  declares); the install test asserts one copy of api, core, instrumentation,
  otlp-grpc-exporter-base, grpc-js and import-in-the-middle. Checks: eslint + typescript-eslint, `tsc --noEmit`,
  publint, `@arethetypeswrong/cli`, `node:test` + c8 (≥ 90% lines per
  package). Compose `fakeotlp-node` / `node-tests`, `testing/verify.sh` step
  `node`, CI jobs `node-test` (22.15.0, 22, 24, 26) and `node-api-range`. The
  B1c go/no-go spike and its findings: `specs/nodejs-otel-helper/spike-B1c.md`.
- **Node.js parity runner (Phase B, task B2; not released).**
  `nodejs/test/parity/` ports the Python runner to `node:test`
  (`runner.test.cjs` + `harness.cjs`: env reset, placeholders, `_dest_ips`,
  matcher, well-formedness, strict xfail, `PARITY_REQUIRE_FAKEOTLP`,
  `PARITY_RELEASE`) and adds `driver.cjs`, one child process per case with every
  kind wired against the planned helper API (`fork` via `cluster.fork()`); a
  kind whose helper API does not exist yet reports `adapter_pending` naming its
  task. It runs inside the core package's `npm test`, so its children count
  towards the c8 gate. Every `nodejs` status in `parity/cases.json` moved from
  `skip:phase-b` to `skip:B<n>-<reason>` (the task that will flip it), except
  the two gRPC RPC-metrics cases: `skip:no-upstream-rpc-metrics`, as Python.
  The diagnostics sink now receives `(line, {origin: 'helper'|'diag', level})`
  (design D-JS5): `helper_log` is origin `helper` only, `diagnostics` warn and
  above plus process warnings. Root devDependencies `express` and
  `@grpc/grpc-js` (test-only, owner-approved). Text matchers apply to strings
  only and regexes compile with the `u` flag in the Node runner (Go/Python
  follow-up in the config-contract tasks).
  `tools/trace_matrix.py` reads `JS<n>` ids and Node test files
  (`nodejs/**/test/**`, `test(`/`it(`/`describe(`); `JS2`–`JS10` are
  allow-listed with their owner task. Node 26 refuses an IP literal as TLS
  ServerName (spike F5), so the Node runner reaches the fake receiver by DNS name
  (`fakeotlp` / `fakeotlp-tls`, new certificate SAN, `extra_hosts` in compose,
  `/etc/hosts` in CI); Go and Python keep the IP addresses (design D-JS14).
- **Node.js resolver, resource and diagnostics (Phase B, task B3; not
  released).** `@staffops/otel-helper` exports `setup(options?)` (synchronous,
  idempotent: first call wins, returns a frozen `Resolved` with `serviceName`,
  `environment`, `serviceInstanceId`, `debug`, `logLevel`), `shutdown(timeoutMs?)`
  (one ref'd deadline, clamped to 2^31-1 ms, cleared when providers finish;
  cached promise; a synchronous provider throw is reported as an
  `AggregateError`; the next `setup()` rebuilds) and `defaultLogLevel(env, debug)`;
  `/testing` adds `getResource()` and a `reset()` that unregisters what the
  helper registered. Identity follows the 0.3 contract and the D1 rulings dated
  2026-10-29: `service.name` code option > code `resourceAttributes` >
  `OTEL_SERVICE_NAME` > `OTEL_RESOURCE_ATTRIBUTES` > `SERVICE_NAME` > SDK
  `unknown_service:<argv0>` + warning (no warning for a code-only attribute);
  `deployment.environment.name` code > `OTEL_RESOURCE_ATTRIBUTES` >
  `ENVIRONMENT` > absent + warning (ADR-003, unknown values verbatim);
  `service.instance.id` per-process UUID unless set (D4); blank identity values
  count as not provided; trim set ` \t\n\r\v\f`. The helper parses
  `OTEL_RESOURCE_ATTRIBUTES` itself (blank items ignored, trimmed after
  decoding, malformed, empty-key or badly encoded items warned by position,
  never by value); the resource is `defaultResource()` plus those attributes,
  no detectors (D-JS4). Log level per R4 (`minimumLogLevel` option wins).
  Deprecated `SERVICE_NAME`, `ENVIRONMENT`, `OTEL_HELPER_SAMPLE_RATIO`,
  `OTEL_HELPER_DISABLED_SIGNALS` warn once per process;
  `OTEL_HELPER_EXTRA_INSTRUMENTATION` is accepted with no effect and a warning
  (as Go G9). Invalid code options throw a `TypeError` naming the option
  (unknown keys included; `undefined` = not set); options are read once into a
  frozen snapshot; environment problems only warn, echoed values cut to 64
  characters. A stderr `DiagLogger` forwards OpenTelemetry SDK messages to the
  helper's sink (`origin: 'diag'`, one bounded, escaped record each) at WARN, or
  `OTEL_LOG_LEVEL` when set (unknown values warn and use WARN);
  `installDiagLogger: false` opts out (and removes a logger an earlier setup
  installed). `setup()` registers the `AsyncLocalStorageContextManager` and a
  `TracerProvider` with the resource and the optional `spanProcessors`, no
  exporters yet (B4); a foreign global TracerProvider or context manager is
  detected first and kept, with one warning each. An inactive copy (another
  helper version owns the process) resolves without the per-setup warnings and
  installs nothing. The 27 identity, environment, log-level and deprecation
  cases of `parity/cases.json` now pass for `nodejs`; 17 new cases (204 in
  total) cover the D1 identity/`OTEL_RESOURCE_ATTRIBUTES` rulings — `nodejs`
  pass, Go/Python `xfail:A16` except the ones they already pass (Go 3,
  Python 4). New D-JS14 rows: per-setup identity warnings, `telemetry.sdk.*`
  override direction and schema URL, `unknown_service` suffix. `JS3`/`JS4` left
  `tools/trace-allow.txt`.
- **Identity/resource rulings in Go, Python and Node.js (task A16).** No
  fixture status says `xfail:A16` any more: Go 13 of the 14 Go cases pass
  (`identity.blank_env_environment_absent` is
  `skip:no-upstream-go-resource-env-merge`), Python all 13. Six new cases
  (210 in total): `resource_attributes.telemetry_sdk_{env,code}_ignored`
  (all three keys), `resource_attributes.unicode_space_key_verbatim` (Go
  skip, same reason), `resource_attributes.code_empty_key_fails`,
  `resource.detector_var_ignored_warned` (Go `skip:no-detector-variable`)
  and `resource_attributes.proto_key_kept` (Node
  `skip:no-upstream-resource-proto-key`: `@opentelemetry/resources` 3.0
  drops a `__proto__` key in `Resource.attributes`; new D-JS14 row). Node.js
  follows the same rules: `telemetry.sdk.*` always from the SDK (code value
  warned once), schema URL `https://opentelemetry.io/schemas/1.43.0`,
  `OTEL_NODE_RESOURCE_DETECTORS` beyond `env`/`serviceinstance`/`none`
  ignored and warned once, identity and parse warnings once per process, and
  `__proto__` kept as an ordinary key up to the SDK resource. Two D-JS14 rows
  removed (`telemetry.sdk.*` direction, per-setup warning cadence).
- **Node.js endpoint rulings of the A17 refutation (not released).** The host
  is checked by the contract's one host rule instead of being compared with
  WHATWG `URL` output: RFC 3986 reg-name bytes (`a{b`, `"h"` invalid), a host
  ending in a number must be a canonical dotted quad (`127.1` no longer becomes
  `127.0.0.1`; it is invalid), IPv6 kept as written and lowercased
  (`[::ffff:127.0.0.1]` no longer becomes `[::ffff:7f00:1]`); WHATWG
  `domainToASCII` is used for non-ASCII hosts only. An invalid `%` escape in an
  http/protobuf path is invalid (`checkCodeOtlp` now uses the resolved
  protocol). An opaque value renders as `unix:` only; `%40` in a path counts as
  `@`. D-JS14 host-normalization row superseded. Final review round: `domainToASCII` runs on the
  host plus a guard label, so its WHATWG IPv4 parsing no longer turns fullwidth
  `１２７.１` (or `２１３０７０６４３３`, `０１.２.３.４`) into a valid address — the
  one host rule rejects them as in Go and Python. Final review round: with an `@` (or `%40`) in the path or query every
  diagnostic shows only `<scheme>://` (`<schemeless endpoint>` for a schemeless
  value) — `http://user:1234/x@host` used to show `http://user:1234`; a
  non-ASCII host with an empty label or over 253 characters after punycode is
  invalid; a dotted quad with one trailing `.` (`10.0.0.1.`) is accepted; an
  http/protobuf value with an invalid path escape renders as
  `<unparseable URL>`.
- **Node.js OTLP endpoint, protocol and TLS-setting resolution (Phase B, task
  B4a; not released).** `setup()` accepts `otelEndpoint`, `tracesEndpoint`,
  `metricsEndpoint`, `logsEndpoint`, `otlpProtocol` and `insecure`, and
  `Resolved` gains `otlp` (per signal: `protocol`, `url`, `grpcTarget`,
  `insecure`, `source`, `ignoredTlsVariables`; `undefined` = no OTLP exporter).
  Resolution only — exporters are built from it in B4b. The contract D1 rows
  apply (Go's `go/endpoint.go` is the closest reference; the D1 rows dated
  2026-10-29 record where Go and Python still differ, task A17). Endpoints are
  split by RFC 3986, then checked against WHATWG `URL`, which must agree on
  host and port: a backslash, an empty authority, a schemeless value starting
  with `/`, a percent-encoded byte, space or zone id in the host, or a port
  outside 1–65535 make the value invalid; leading-zero ports compare
  numerically; path escapes are kept verbatim; an empty `?`/`#` is removed
  with the userinfo/query warning (once per variable and value, not per
  signal); an `@` in the path or query warns naming the variable only. OTLP is
  configured per signal (code per-signal > code
  generic > env per-signal > env generic; an unparseable env value warns and
  falls back to the next level, an unsupported or opaque scheme leaves the
  signal unconfigured); schemeless `host:port` → `https://` (`http://` with
  `INSECURE=true`) + default port, deprecated for `http/protobuf` (once per
  process); portless gRPC URL → `:4317` + warning; an explicit scheme-default
  port is kept (`http://h:80` → gRPC target `h:80`, which WHATWG `URL.host`
  would drop and grpc-js would dial on 443); `http/protobuf` generic endpoints get
  `/v1/<signal>`, per-signal ones keep their path; userinfo, query and
  fragment are removed and never echoed; a control character makes the value
  `<unparseable URL>`; IPv6 brackets are kept. WHATWG host normalization
  remains (lowercase, punycode, IPv4 shorthand, IPv4-mapped IPv6; D-JS14). Protocol: code > per-signal env >
  generic env > port-4318 inference (INFO once per setup) > `grpc`;
  `http/json` and unknown env values warn and are ignored. `INSECURE` uses the
  boolean grammar (garbage warns, reads false); contradicting a URL scheme
  warns and the scheme wins; TLS file variables with a plaintext endpoint are
  warned by name only. Code values fail fast with a `TypeError` naming the
  option (unusable endpoint, unknown `otlpProtocol`, `insecure` contradicting
  a code URL); an empty string is not set. A global API registry whose
  `version` differs from the exact version of the `@opentelemetry/api` copy the
  helper loads (the API registers only into a registry of its own exact
  version) is now reported with the required version and left alone; a
  registration the API refuses anyway is reported too. The 12 `nodejs: skip:B4a-…` cases
  pass on Node 22.15.0/22/24/26; new case `export.grpc_http_explicit_port_80`
  (Go and Python pass, Node `skip:B4b-otlp-endpoint` until the exporters
  exist). Five D1 rulings from the B4a review (2026-10-29, task A17 for
  Go/Python): an unparseable `INSECURE` warns without an endpoint; a control
  character beats an opaque scheme (invalid, fall back); an empty code
  `otlp_protocol`/`compression` is not set; a port outside 1–65535 is invalid
  (Node now also rejects port 0, which WHATWG URL accepts); an unparseable
  endpoint is always shown as `<unparseable URL>` (Node: also schemeless
  values such as `collector:abc`). Twelve cases cover them: Node `pass`
  (`config.code_empty_compression_not_set`: `skip:B4b-otlp-compression`),
  Go/Python `xfail:A17` where they diverge. Twenty-one more cover the
  refutation rulings (RFC 3986 parsing, leading-zero ports, host characters,
  empty query/fragment, `@` in the path, warning dedupe): Node `pass`, Go
  `xfail:A17` ×7, Python `xfail:A17` ×15. Six more from the re-review (250 in
  total): `redaction.credentials_in_path_never_echoed` (Go `xfail:A17`) —
  diagnostics drop an endpoint's path when the path or query holds an `@`,
  and an opaque value is cut before its last `@`; a backslash in the path
  (env + code) and IDN rulings — hosts punycoded, a host failing IDNA invalid
  (`endpoint.idn_host_punycoded`, `endpoint.idna_failure_invalid` + code
  variant) — Go and Python `xfail:A17`.
- **Endpoint rulings in Go and Python (task A17).** No fixture status says
  `xfail:A17` any more: 48 statuses flipped to `pass` on 35 cases (Go 19,
  Python 29). New case `export.http_path_escape_verbatim`: the generic
  endpoint path `/a%2Fb` must reach the receiver as `/a%2Fb/v1/<signal>` —
  Python `pass`, Go `skip:no-upstream-go-http-raw-path` (exporter decodes the
  path), Node `skip:B4b-otlp-endpoint`. Nine more from the refutation round
  (260 in total), `pass` in Go, Python and Node: `endpoint.port_long_digits`,
  `endpoint.idn_deviation_char`, `endpoint.idn_zwj_invalid`,
  `endpoint.http_path_invalid_escape` + `config.code_http_path_invalid_escape_fails`,
  `endpoint.ipv4_shorthand_invalid`, `endpoint.ipv4_octet_out_of_range_invalid`,
  `endpoint.host_non_regname_invalid`, `redaction.opaque_scheme_renders_scheme_only`. Final review round (266 in
  total): `endpoint.ipv4_fullwidth_shorthand_invalid` and
  `redaction.at_in_path_numeric_password_prefix` (`pass` everywhere), and the
  wire-level twins `export.ipv4_shorthand_falls_back`,
  `export.ipv4_octet_out_of_range_falls_back`,
  `export.host_non_regname_falls_back`,
  `export.ipv4_fullwidth_shorthand_falls_back` (Go/Python `pass`, Node
  `skip:B4b-otlp-endpoint`), which assert that traces reach the generic
  endpoint. `testing/fakeotlp` records the
  path as sent (`raw_path`, `net/url` `EscapedPath`), and the three parity
  runners expose it as a record field.
- **Node.js OTLP exporters for traces, metrics and logs (Phase B, task B4b;
  not released).** `setup()` now builds the OTLP exporters (gRPC and
  http/protobuf) of every configured signal, a `BatchSpanProcessor`, a
  `PeriodicExportingMetricReader` (interval 30 000 ms until B4c) and a
  `BatchLogRecordProcessor`, and registers the global `MeterProvider` and
  `LoggerProvider` (a foreign one is kept and reported); `shutdown()` flushes
  the three providers. Every helper-owned value is passed explicitly — URL or
  gRPC `host:port` target, headers, timeout, compression and, for gRPC,
  channel credentials — and the helper-owned
  `OTEL_EXPORTER_OTLP[_<SIGNAL>]_{ENDPOINT,HEADERS,TIMEOUT,COMPRESSION,PROTOCOL,INSECURE}`
  variables are hidden from `process.env` while the exporters are built
  (restored in `finally`, also when construction throws); the certificate and
  client key/certificate variables stay visible. New options
  `exportTimeoutMs` (integer ms in [1, 86 400 000]; larger values fail fast —
  Go and Python accept them, D-JS14) and `compression` (`gzip`/`none`, empty =
  not set); `Resolved.otlp[signal]` gains `timeoutMs` and `compression`
  (headers stay out of `Resolved`). Headers, timeout and compression follow the
  contract D1 rows as in Go (key grammar, last wins, whole-value decoding,
  control characters skipped, warnings by position only; plain-decimal ms
  clamped to [1 ms, 24 h]; case-insensitive `gzip`/`none`, default `gzip`). A
  header value gRPC metadata or Node's HTTP layer cannot carry is skipped with
  a warning naming the variable and position. The batch processors use an
  explicitly set `OTEL_BSP_EXPORT_TIMEOUT`/`OTEL_BLRP_EXPORT_TIMEOUT`, else the
  resolved OTLP timeout; the metric reader's is capped at the interval with a
  warning. `OTEL_BSP_*`, `OTEL_BLRP_*` and the attribute/span/log-record limit
  variables (which SDK 3.0 no longer reads) are read by the helper with the
  specification defaults; invalid values warn. gRPC credentials are built by
  the helper (no lazy env TLS read): an unreadable TLS file is ignored with a
  warning naming the variable (never the path), a CA file without a parseable
  certificate falls back to the system roots, only one of client key/certificate
  gives server authentication only, credentials the TLS layer rejects fall
  back to the system roots, and a TLS IP-literal target is warned (Node 26
  refuses an IP as TLS server name). A failing exporter construction is
  reported and that signal is not exported; `setup()` does not throw. The 84
  `nodejs: skip:B4b-…` cases pass on Node 22.15.0/24/26 (including the A17
  export twins and `export.http_path_escape_verbatim`). New cases (268 in
  total) `export.grpc_certificate_unreadable_falls_back` and
  `export.grpc_certificate_not_pem_falls_back` pass in Go, Python and Node. A
  key-without-certificate case was not added: Go ignores the incomplete pair
  silently while Python's grpc aborts the process — open, needs a ruling.
  **Fix round (B4b review, contract task A18):** the TLS files are read and
  validated by the helper for both protocols (per-signal then generic, the
  pair checked with `tls.createSecureContext`, a pair gRPC still rejects
  retried with the CA only, then the system roots; http/protobuf gets them
  through `httpAgentOptions`) and every TLS variable is masked too; the
  resolved OTLP timeout is the deadline of BSP, BLRP and reader
  (`OTEL_BSP/BLRP/METRIC_EXPORT_TIMEOUT` warn and are ignored); the
  metric-reader cap warning names the timeout's source; `exportTimeoutMs`
  accepts finite values in [1, 86 400 000] (decimals allowed);
  `Resolved.otlp[signal].timeoutSource` is new; decoded header values must be
  printable ASCII (warned as `item #N`, the env position); a value-length
  limit of 0 and a batch size above the queue size warn. The gRPC exporter
  packages are loaded only when a gRPC signal is configured, so
  `require('@staffops/otel-helper')` no longer loads `@grpc/grpc-js`; each
  signal's exporter and processor/reader are built under one `tryBuild`
  (a built exporter is shut down if its processor fails); exporter
  diagnostics are written after the environment is restored; HTTP headers
  use a null-prototype object; the `shutdown()` `AggregateError` names the
  providers that failed and the first reason. Ten new cases (278 in total)
  pass in Go, Python and Node. Final round: the batch-size warning names both
  variables (contract row "Batch size above queue size"); Python keeps a
  non-zero per-signal value-length limit when only the generic one is 0; Go
  reads any spelling of 0 (`00`) for the log limit. Three more cases (281 in
  total): `config.attribute_value_length_limit_zero_signal_wins`,
  `config.bsp_batch_size_above_queue_clamped`,
  `config.blrp_batch_size_above_queue_clamped`.
- **Node.js exporter lists, console exporters, `OTEL_SDK_DISABLED`,
  `OTEL_CONFIG_FILE` and the metric export interval (Phase B, task B4c; not
  released).** `OTEL_TRACES_EXPORTER`, `OTEL_METRICS_EXPORTER` and
  `OTEL_LOGS_EXPORTER` are parsed as in Go and Python: comma-separated,
  trimmed, case-insensitive, duplicates dropped; `logging` is the deprecated
  spelling of `console` (warned); unknown values warn and are dropped; `none`
  with other values warns and disables the signal; nothing valid left = the
  default (`otlp` for traces and logs; for metrics `otlp` with a metrics
  endpoint, else `prometheus` — its `/metrics` reader is task B7). An env
  `otlp` metric exporter without an endpoint warns and is dropped; the new
  code option `metricExporters` fails fast on `otlp` without an endpoint, an
  unknown value (`logging` included) or `none` with other values. `console`
  uses the SDK console exporters (stdout) behind a batch processor or a
  periodic reader. `OTEL_SDK_DISABLED=true` builds no provider, exporter or
  resource (the API no-op providers stay) and still installs the context
  manager; an unparseable value warns and reads as `false`.
  `OTEL_HELPER_DISABLED_SIGNALS` (deprecated) maps to `none` for each listed
  signal unless that signal's `OTEL_<SIGNAL>_EXPORTER` is set; unknown entries
  warn; new code option `disabledSignals` wins over it (`[]` included).
  `OTEL_METRIC_EXPORT_INTERVAL` (positive integer ms, default 30 000; garbage
  and 0 warn and use the default; above 2^31-1 ms clamped with a warning) and
  the new option `exportIntervalMs` (integer in [1, 2^31-1]) set the interval
  of every periodic reader. The OTLP metric reader's timeout stays the OTLP
  timeout capped at the interval; the warning now names the source of the cap
  (the interval's when only the interval is short, else the timeout's plus
  `interval=<source>`). `OTEL_CONFIG_FILE` is warned once per process (name
  only) and ignored. `Resolved` gains `sdkDisabled`, `tracesExporters`,
  `metricExporters`, `logsExporters` and `metricExportIntervalMs`. The 23
  `nodejs: skip:B4c-…` cases pass on Node 22.15.0/24/26. Its open items became
  contract task A19 (design D1 rows of 2026-10-29: the standard variable wins
  over `OTEL_HELPER_DISABLED_SIGNALS`, `logging` fails in code, the interval
  grammar `^\+?[0-9]+$` with the 2 147 483 647 ms clamp and code bound,
  `OTEL_CONFIG_FILE` warned in every language), implemented in Go and Python
  in the same change; the seven A19 cases pass in Node too. **Fix round:** a
  standard variable counts as set only with a valid entry and code
  `metricExporters` beats `OTEL_HELPER_DISABLED_SIGNALS` for metrics;
  `OTEL_EXPERIMENTAL_CONFIG_FILE` is warned like `OTEL_CONFIG_FILE`;
  `OTEL_SDK_DISABLED=true` logs the contract INFO line; a resolved
  `prometheus` metric exporter is reported once at INFO ("not implemented
  yet in Node", until B7); the interval clamp warning reads "above 2147483647
  ms; clamped"; a reader timeout capped below 1000 ms warns; the parity
  driver maps `disabled_signals`. The six fix-round cases that Node can run
  pass; `propagation.sdk_disabled_propagators_kept` is `skip:B5-propagators`.
- **Node.js sampler, health sampler, propagators and debug marker (Phase B,
  task B5; not released).** The helper parses `OTEL_TRACES_SAMPLER` /
  `OTEL_TRACES_SAMPLER_ARG` itself, as Go and Python do: the six spec names
  (trimmed, case-insensitive), default `parentbased_always_on`; an unknown
  name, or `jaeger_remote`/`parentbased_jaeger_remote`/`xray` ("unsupported by
  this helper"), warns and uses the default; `_ARG` is a plain decimal in
  [0, 1] (no sign, exponent, hex or NaN), else warned and 1.0. The deprecated
  `OTEL_HELPER_SAMPLE_RATIO` selects `parentbased_traceidratio` only while
  `OTEL_TRACES_SAMPLER` is unset; an invalid value warns and is ignored. New
  code option `sampleRatio` (a number in [0, 1], any value — 1 included —
  wins over both variables; anything else throws). A health sampler wraps the
  whole resolved sampler, outside ParentBased (design D-JS7, as Go): SERVER
  spans with no or a remote parent whose `url.path` (else `http.target`) is a
  health path — exact, case-insensitive, trailing slash and query ignored — or
  whose span name is the instrumentation's name of a `grpc.health.v1.Health`
  method (see the fix round below), and CLIENT spans of that service with any
  parent, are not recorded, keeping the parent's tracestate. It decides from
  the span name and start attributes only, so task B6 just feeds it. New
  option `healthPaths` (ASCII, starts with `/`, no query, fragment or comma;
  `[]` traces every HTTP path). `OTEL_PROPAGATORS`: `tracecontext`,
  `baggage`, `none` (trimmed, case-insensitive, empty tokens and duplicates
  dropped); `b3`/`b3multi`/`jaeger`/`xray`/`ottrace` and unknown values warn
  and are skipped; nothing supported left = the W3C default; `none` with other
  values warns and disables propagation. The propagator is installed also
  under `OTEL_SDK_DISABLED=true` (whose INFO line loses its "until task B5"
  note); a propagator registered by someone else is kept and reported. Debug
  mode (`debug` / `OTEL_HELPER_DEBUG_LEVEL`) adds `debug="true"` to spans with
  no or a remote parent, never from inbound tracestate or baggage. `Resolved`
  gains `sampler`, `propagators` and `healthPaths`. The 26 `nodejs:
  skip:B5-…` cases pass; `config.code_sample_ratio_out_of_range_fails` now
  matches the option name by regex (`[Ss]ample_?[Rr]atio`: Go/Python
  `sample_ratio`, Node `sampleRatio`); new case
  `propagation.env_none_disables` (296 in total) passes in Go, Python and
  Node. The HTTP/gRPC health cases stay `skip:B6-…` (they need the
  instrumentations). **Fix round (2026-10-30):** gRPC health spans are matched
  by span name only, in the instrumentation's form
  `grpc.grpc.health.v1.Health/<method>` — instrumentation-grpc sets `rpc.*`
  after start, so the `rpc.method` branch is gone, and a user service in
  package `health.v1` (`grpc.health.v1.Health/<method>`) is kept;
  `isGrpcHealthMethod` handles full-method values for B6. A propagator
  registered before `setup()` is kept and reported as `...; the helper keeps
  it and OTEL_PROPAGATORS is not applied` (Go and Python replace it: design
  D-JS14); re-setup releases only the helper's own propagator instance; under
  `OTEL_SDK_DISABLED=true` the sampler variables are not read and the INFO
  line says `the helper installed no propagator` when none was installed.
  `healthPaths` errors name `item #N`. Shared `PLAIN_DECIMAL`, `typeName`,
  `asciiLower`/`asciiUpper` in `env.ts` (also used by health-path
  normalization). Contract task A20 (same change, Go
  and Python too): per-token propagator warnings with `using` = the
  effective list, `none` with other values warned, one ignored-alias message
  for `OTEL_HELPER_SAMPLE_RATIO`, ASCII-only case folding of env enum values;
  `config.code_sample_ratio_out_of_range_fails` now expects
  `(sample_ratio|sampleRatio).*1\.5`; five new cases (301 in total) pass in
  Go, Python and Node: `propagation.a20_none_combined_warns_each_token`,
  `propagation.a20_per_token_warnings`,
  `propagation.a20_warning_names_effective_list`,
  `sampler.a20_sample_ratio_alias_out_of_range_ignored`,
  `sampler.a20_non_ascii_name_falls_back`.
- **Node.js Prometheus `/metrics` (Phase B, task B7; not released).** With no
  OTLP metrics endpoint, or `prometheus` in `OTEL_METRICS_EXPORTER` /
  `metricExporters`, metrics are served for scraping (ADR-007): a Prometheus
  reader (`@opentelemetry/exporter-prometheus`, its own server never started)
  and a helper listener on `OTEL_HELPER_METRICS_PORT` (default 9464, `0` = no
  listener; new option `prometheusMetricsPort`, an integer in [0, 65535] that
  wins over the variable; an invalid variable warns and uses 9464), host
  omitted (`::` dual-stack, else `0.0.0.0`), serving only `/metrics`,
  `unref()`'d so it never keeps the process alive, closed by `shutdown()`
  before the MeterProvider flushes. A busy port does not fail the synchronous
  `setup()`: it is one ERROR record on the diagnostics sink naming the port
  and the mount alternative (Go fails `Setup`; design D-JS14). A cluster
  worker with a non-zero port is warned (each scrape reaches one worker). New
  export `metricsHandler()` — `(req, res)` for `node:http` and express — with
  the contract status table: 503 before setup, after a failed setup, after
  `shutdown()` and when the global MeterProvider is not the helper's; 200 with
  an empty body when Prometheus is deliberately off (`OTEL_SDK_DISABLED=true`,
  `OTEL_METRICS_EXPORTER` without `prometheus`); otherwise the exposition text
  (`text/plain; version=0.0.4; charset=utf-8`), only OTel series. Metric names
  match Go and Python: unit suffixes (`s` → `_seconds`, `ms` →
  `_milliseconds`, `By` → `_bytes`, `a/b` → `_a_per_b`, `{…}` dropped, `1` →
  `_ratio` on gauges, as Go), the unit before `_total`, `target_info` for the
  resource and an `otel_scope_name` label on every series. `Resolved` gains
  `prometheusMetricsPort`. `OTEL_HELPER_DISABLED_METRICS` is **not
  implemented in Node yet** (Go uses `path.Match`, Python `fnmatch`; no
  contract ruling, and the SDK View wildcard matches neither): warned once per
  process and ignored (*superseded the same day by A21, below*). The B4c INFO
  report "prometheus metric exporter not implemented yet in Node" is gone. Parity: the three `nodejs:
  skip:B7-metrics-endpoint` cases pass on Node 22.15.0/24/26; the
  `metrics_scrape` drivers (Go, Python, Node) also increment `parity.payload`
  (unit `By`) and the `http_server` snapshot gains `scrape`; three new cases
  (304 in total) — `metrics.scrape_counter_unit_suffix`,
  `metrics.scrape_target_info_and_scope_label` (pass in Go, Python and Node)
  and `metrics.http_server_duration_histogram_name` (pass in Go and Python;
  Node `skip:B6-http-instrumentation`). **A21 (same day, contract design D1
  rows of 2026-11-02, Go and Python too):** `OTEL_HELPER_DISABLED_METRICS` and
  the new option `disabledMetrics` (array of strings; `[]` wins) are
  implemented — case-sensitive globs (`*` any run, `?` one character,
  everything else literal), items trimmed, blanks ignored — through a dropping
  wrapper around every push exporter (OTLP, console) and the same filter on
  the scrape (SDK Views cannot express the glob); `Resolved.disabledMetrics`.
  `1` → `_ratio` on Gauges only. `OTEL_HELPER_METRICS_PORT`: `^\+?[0-9]+$`
  (`-0` now invalid). The listener answers 405 (`Allow: GET, HEAD`) to other
  methods on `/metrics`. Eleven new parity cases (315 in total) pass in Go,
  Python and Node: `metrics.disabled_metrics_glob_star_dot`,
  `_question_mark`, `_bracket_literal`, `_case_sensitive`,
  `metrics.scrape_gauge_unit_one_ratio`, `metrics.port_env_underscore_invalid`,
  `_out_of_range_invalid`, `_long_digits_invalid`, `_leading_zeros_valid`,
  `metrics.listener_other_path_404`, `metrics.listener_get_head_only`; new
  fixture kind `metrics_listener` and `resolve` field
  `prometheus_metrics_port`. **Fix round (2026-11-02, after observability,
  dev and code-review):** the listener `unref()`s accepted connections and
  checks its timeouts every second, so a stalled or keep-alive client no
  longer keeps the process alive (it took about 30 s to exit); 404/405 carry
  text bodies; colliding label names are merged as Go does (they were
  written twice, which Prometheus rejects); the scope labels are written by
  the helper, empty values included. New D1 rows "Colliding label names",
  "Suffix semantics", "Exposition details"; "Unit 1", "Metrics listener" and
  "Disabled-metric patterns" amended. Five new cases (320 in total):
  `metrics.scrape_colliding_label_names_merged` (pass everywhere),
  `metrics.scrape_unit_word_inside_name`, `metrics.scrape_total_inside_name`,
  `metrics.scrape_ratio_inside_name` (pass in Python and Node, Go
  `skip:no-upstream-go-otlptranslator-tokens`),
  `metrics.disabled_metrics_mixed_case_instrument` (pass in Go and Node,
  Python `skip:no-upstream-python-case-preserving-names`); the listener cases
  probe TRACE and PROPFIND; `metrics.scrape_gauge_unit_one_ratio` asserts the
  values; `metrics.scrape_target_info_and_scope_label` runs on Python from
  sdk 1.43; the runners require a non-empty `input.requests` for
  `metrics_listener`. **Final round (2026-11-02):** merged label values are
  sorted as strings before joining (Go sorts the values, not the keys;
  `a.b="y"`, `a_b="x"` → `a_b="x;y"`), in Node and Python, on data points and
  `target_info`; Python renders colliding non-strings as Go and Node
  (`true`, `5`, `["a","b"]`) and copies nothing when no name collides; Python
  binds no listener when a foreign MeterProvider is already installed (a busy
  port no longer fails `setup_telemetry()` then); the 500 log and the
  upper-case pattern warning are truncated (the pattern is now named). New
  case `metrics.scrape_colliding_label_values_sorted` (321 in total);
  `export.code_export_timeout_wins_grpc` allows one `grpc-timeout` encoding
  unit (`lte` 2100).
- **Node.js instrumentation (Phase B, task B6; not released).** `setup()`
  instruments `node:http`/`node:https` (servers and clients,
  `@opentelemetry/instrumentation-http`), `fetch`/undici
  (`-undici`), express (`-express`, middleware layers ignored) and
  `@grpc/grpc-js` (`-grpc`) — all already dependencies since B1a; no new
  dependency. Stable HTTP semconv only (`http.server.request.duration` and
  `http.client.request.duration` in s, scraped as
  `http_server_request_duration_seconds`; server metrics carry
  `http.request.method`, `http.response.status_code`, `http.route`,
  `url.scheme`). Health checks follow contract D5: server spans are dropped by
  the B5 health sampler (HTTP by `url.path` at span start, gRPC by the span
  name `grpc.grpc.health.v1.Health/<method>`, CLIENT spans of that service
  too), never by an instrumentation filter, so health requests stay counted
  in the HTTP server metrics; HTTP client requests (`node:http` and `fetch`)
  to a health path get no span and no client metric, as Go
  `NewHTTPTransport` and Python's `excluded_urls`. Also excluded: the helper's
  `/metrics` listener (neither traced nor counted, as Go and Python) and the
  helper's OTLP/HTTP export requests (the SDK suppresses their spans, but
  instrumentation-http 0.300 still recorded `http.client.request.duration`
  for each export). The instrumentations are built once per process after
  the providers are registered, bound to the helper's providers (or to a
  foreign global one), disabled by `shutdown()` and enabled and re-bound by
  the next `setup()`; not built under `OTEL_SDK_DISABLED=true` (as Python).
  gRPC spans carry the legacy `rpc.*` attributes and no RPC metrics are
  emitted (upstream instrumentation-grpc 0.300; Python has the same two gaps,
  but gRPC span names differ in all three languages — README gaps). Parity: the 15
  `nodejs: skip:B6-…` cases pass on Node 22.15.0/24/26 (the `http_server` and
  `grpc_server` kinds, `metrics.http_server_duration_histogram_name`); no new
  case (321 before the fix round). New `test/instrumentation.test.cjs` (url.path at start,
  the gRPC span-name format, two scrapes of the listener leave the HTTP server
  count unchanged, client exclusions, re-binding, the app's grpc-js patched
  when the gRPC exporter loaded it first). **Fix round (2026-11-02, after
  observability, dev and code-review; Node unreleased, so no BREAKING
  marker):** express and grpc-js required before `setup()` in CommonJS are now
  instrumented (the first enable re-requires their cached entry; one warning
  per package for CJS cache hits — *reworded by the dev re-probe, same day:*
  `<module> was loaded before setup(); apps, routers and clients created
  before setup() may lack spans or have an incomplete http.route — call
  setup() in a preload (e.g. --import ./otel.mjs that imports the hooks
  entry and calls setup()) before the app loads`; not reported for an ESM
  import with the loader hooks installed), and ESM named imports of `node:http` see
  the patched functions (`module.syncBuiltinESMExports()`); gRPC SERVER spans
  are ERROR only for the six semconv codes (NOT_FOUND etc. → UNSET, as Go and
  Python); `fetch`/undici spans redact userinfo and signature query values in
  `url.full` and carry no `url.query` (the `node:http` client and Go write
  none on client spans; *corrected by the dev re-probe, same day*);
  express `router` layer spans are gone; client health matching reads the
  pathname of absolute-form (proxied) targets; health paths are
  percent-decoded (A22); a failed instrumentation build is retried by the
  next setup; `/testing` `reset()` always disables the instrumentations;
  `isGrpcHealthMethod` (unused) removed. New D-JS14 rows (HTTP/2, express
  span shape, HTTP metrics, `error.type`, `http.route`, URL redaction,
  foreign TracerProvider) and README gaps; `parity/README.md` lists the R10
  extra attributes. **A22 (contract D1 rows "Percent-encoded health paths"
  and "gRPC server span status", 2026-11-02):** the Go, Python and Node
  `grpc_server` drivers serve `/parity.v1.Echo/NotFound` (NOT_FOUND) and
  report `server_span_status`; two new cases (323 in total),
  `http.health_percent_encoded_path` and `grpc.server_not_found_status_unset`,
  `pass` in Go, Python and Node (Go and Python: driver changes only, no
  library change — both already decode health paths and apply the
  six-code server rule). **Final review (2026-11-02):** the undici query is
  kept as written unless a redacted name is present (no `flag` → `flag=`,
  `%20` → `+`); the gRPC status processor never throws; a package loaded
  only through a deep path (`express/lib/express.js`) is reported and its
  main entry re-required. **Dev re-probe (2026-11-02):** the preload warning
  no longer names `/register` (it does not call `setup()` before B9) and is
  skipped for ESM imports under the loader hooks; undici client spans drop
  `url.query`; new D-JS14 rows and README gaps: routers created before
  `setup()` get a truncated `http.route`, and a gRPC handler that throws
  synchronously leaves its SERVER span unended (upstream).
- **Node.js logging (Phase B, task B8; not released).** New
  `createLogger({name, level?})` (spec JS6): a frozen logger with
  `debug`/`info`/`warn`/`error(message, attributes?)` that never throws;
  invalid options throw a `TypeError` naming the option. Per record it writes
  through the OpenTelemetry Logs API of the active setup while that setup has
  a logs exporter (OTLP and/or `OTEL_LOGS_EXPORTER=console`, the SDK console
  exporter — a debugging format, not line-parseable), else one JSON line on
  stdout — never both (design D6). Level (R4): the logger's `level`, else
  the level the last successful `setup()` resolved (`minimumLogLevel`, else
  `defaultLogLevel(environment, debug)`; `info` before the first setup),
  read per record, kept after `shutdown()`, applied on both paths. Stdout
  line, as Go and Python (contract A23 rulings): `time` (RFC 3339, ms, UTC
  `Z`), `level`, `msg`, `logger`, the attributes, `exception` (stack of the
  first `Error`, also under a reserved key); attributes named `time`,
  `level`, `msg`, `logger` are skipped; no trace ids; compact JSON, always
  valid — NaN/±Infinity, bigint and symbols as text, functions as
  `[Function: name]`, `Map` as an object, `Set` as an array, cycles as
  `[Circular]`, nesting past 100 as `<max-depth-exceeded>`, a throwing
  getter or Proxy trap as `[unreadable …]` (a revoked Proxy or a throwing
  `getPrototypeOf` as `[unreadable object]`). OTel path: the logger name is
  the instrumentation scope (as Python), the first `Error` becomes the
  record exception, the active span correlates the record, and objects,
  `Map`s, arrays and `Set`s are sent as OTLP maps and arrays (as Go and
  Python; `Date` and bigint as text; the exception is passed as a plain
  `{name, message, stack, code}` read through guarded reads). A record whose attributes cannot even
  be listed is dropped and reported once (`a helper log record could not be
  written and was dropped`, reason only). With a foreign global
  LoggerProvider the helper's loggers keep exporting through the helper's
  own provider, as in Go and Python (both logs registration reports say
  so). Parity: `logging.console_when_no_exporter`,
  `logging.console_when_logs_exporter_none` and
  `logging.stdout_time_utc_under_tz` pass for Node (324 cases). New
  `test/logging.test.cjs`. The rulings' history is in contract design D1
  ("Stdout fallback time zone", "Stdout fallback fields", "OTel log scope
  name") and D-JS6/D-JS14.
- **Node.js lifecycle (Phase B, task B9; not released).** The helper now
  registers one **forwarding** TracerProvider, MeterProvider, LoggerProvider
  and propagator per process (design D-JS10) and points them at the SDK
  providers of each `setup()`: tracers, meters, instruments (observable and
  batch callbacks included) and Logs API loggers obtained before a
  `shutdown()` → `setup()` keep working with the new providers (the `todo`
  test "re-setup keeps tracers obtained earlier working" now passes).
  `shutdown()` keeps each provider as their delegate until that provider's
  own shutdown settles (its final export still runs the observable and batch
  callbacks), then points them at the API no-ops; the propagator stays; the
  context manager is registered once. **The delegate of the API's global
  TracerProvider proxy, and the global MeterProvider and LoggerProvider
  themselves, are now the helper's forwarders, not the SDK providers**: code
  that reached SDK internals through `trace.getTracerProvider().getDelegate()`
  must use the forwarders' own `forceFlush()` (new) or `/testing`
  `getProviders()`. A re-setup no longer
  unregisters any global: the helper owns a global while the registry still
  holds the exact entry it registered, and one someone replaced is reported
  as `no longer the helper's` and kept. **Foreign globals (spec JS7):** kept
  and warned as before, and the new `Resolved.inactiveSignals` lists
  `traces` / `metrics` whose global provider is not the helper's (all three
  under another `@opentelemetry/api` version or helper version; a foreign
  LoggerProvider lists nothing). New option **`replaceGlobals`**: replaces a
  foreign TracerProvider, MeterProvider, LoggerProvider and propagator (one
  INFO line each), never the context manager; the instrumentations then
  bind to the helper's providers. **Rollback (Go G1):** a `setup()` that
  fails after resolution undoes every step it took (DiagLogger, context
  manager, propagator, providers and their forwarding delegates,
  instrumentations, `/metrics` listener), shuts down what it built, leaves
  the previous state in place (`metricsHandler()` answers 503, loggers write
  stdout) and rethrows; the next call starts over. **`/register`** now calls
  `setup()` from the environment (a failure is reported, never thrown), and
  the "loaded before setup()" report names
  `--import @staffops/otel-helper/register` as the cure again. **ESM without
  hooks (spec JS5):** `setup()` in an ES module entry (`.mjs`, `"type":
  "module"`, `--input-type=module`) without the loader hooks warns once that
  later imports are not instrumented. `/testing` gains `getProviders()` (the
  active SDK providers) and `reset()` discards the forwarders, so objects and
  callbacks of one test never reach the next. No signal handler is
  installed (the SIGTERM pattern goes to the HOW-TO, B12). New D-JS14 rows:
  re-setup vs Go/Python, foreign globals and `replaceGlobals`, the thrown
  rollback, ESM entry detection limits, observable callbacks across
  re-setups. New `test/forwarding.test.cjs`; no `skip:B9-*` parity case
  existed (324 cases unchanged). **Fix round (2026-11-02, after observability,
  dev and code-review):** the forwarders no longer drop the observable and
  batch callbacks before the final collection of `shutdown()` (H1, found by
  all three reviews); `forceFlush()` on the three forwarders; tracers, meters
  and loggers cached by name, version and schema URL, and callback-less
  observables no longer retained; a batch callback re-homes only its own
  meter's observables; without a delegate a local no-op tracer replaces the
  deprecated `ProxyTracerProvider`; a failed setup undoes the `trace`/`logs`
  registrations through the registry, so tracers and Logs API loggers
  obtained before it join the app's own SDK, records each undo step before
  touching the registry, shuts down what it built at every step (the
  pipelines alone when a provider constructor throws), and redacts its
  report; `/register` calls `setup()` only in the main thread (a worker gets
  the hooks only) and a worker thread's `setup()` starts no
  `/metrics` listener; code options passed after a `/register` setup are
  warned; the MeterProvider gets the shutdown deadline; `getProviders()` is
  frozen and `undefined` after shutdown; an explicit package `type` wins over
  `--experimental-default-type`; `Resolved.inactiveSignals` documented as Node
  only, global ownership only. **Final round (2026-11-02):** on the shutdown
  deadline every provider, a hung one included, stops being a delegate before
  the rejection; `redactErrorText` matches credential names generically
  (quoted too), strips schemeless `user:pw@`, keeps the host of
  `dns:///user:pw@host`, and leaves prose such as "basic auth failed" alone;
  the shutdown `AggregateError` message is redacted; a synchronous throw of
  a delegate's `forceFlush()` rejects; `/register` is silent in worker
  threads; `/testing` `getResource()` is `undefined` after shutdown, as
  `getProviders()`.
- **Node.js prefork model (Phase B, task B9b; not released).** The parity
  `fork` kind runs `cluster.fork()` twice, each worker calling `setup()`
  (design D-JS13); `fork.instance_ids_distinct_post_fork` and
  `fork.env_instance_id_warns_and_kept` now pass for Node. Each worker is a
  fresh process, so the helper's per-process `service.instance.id` is
  regenerated in every worker and never inherited from the primary. New: a
  `service.instance.id` set explicitly (code or `OTEL_RESOURCE_ATTRIBUTES`)
  in a `cluster` worker is kept and **warned once per worker**, as a
  conditional with the same wording as Python except the worker kind
  (`forked`/`cluster`) (`service.instance.id is set
  explicitly in a cluster worker: unless each worker sets a different value,
  every worker reports the same id; …`; contract design D4, task A24). Node detects every cluster
  worker (`cluster.isWorker`); Python warns only
  when it was imported before the fork (D-JS14). The `/metrics` listener
  under `cluster` is unchanged and recorded: workers on one port share it
  round-robin (warned), a primary that also ran `setup()` owns the port and
  the workers report `EADDRINUSE` (warned + error), per-worker ports work
  when they come from a stable slot owned by the primary
  (`cluster.fork({ OTEL_HELPER_METRICS_PORT: String(base + slot) })`, re-forked
  on the same slot; `cluster.worker.id` is never reused, so `base +
  cluster.worker.id` would lose a restarted worker's metrics). The
  cluster-worker port warning is reworded as a conditional ("if several
  workers use this port, each scrape reaches one of them at random; give each
  worker its own port (e.g. base + a per-worker slot) or use OTLP only"),
  still WARN and once per worker. Tests wait for real events under a 20 s
  deadline and cover shared-port round-robin, a worker restarted on its slot
  and a `child_process.fork()` child (not a cluster worker, not warned).
  New `test/cluster.test.cjs`; HOW-TO items for cluster/pm2 added to B12.
- **Node.js SQL instrumentation (Phase B, task B10a; not released).** New
  core option `setup({ instrumentations: [...] })` takes opt-in
  `Instrumentation` objects. Each setup binds them to the helper's providers
  (or to a foreign global provider, as the core instrumentations) and enables
  them; `shutdown()`, a failed setup's rollback and `/testing` `reset()`
  disable them. Under `OTEL_SDK_DISABLED=true` or another API version's
  registry they stay disabled. One that throws while being enabled is
  reported and left out; `setup()` carries on. `@staffops/otel-helper-sql`
  implements `sqlInstrumentation({ pg?, mysql2? })` over the official
  `@opentelemetry/instrumentation-pg` 0.77 and `-mysql2` 0.70 (dependencies;
  `pg`/`mysql2` stay the app's). Each library is instrumented when it
  resolves from the app. One named `true`, or the only one left, that is
  missing throws the D14-worded error, and so does the absence of both.
  Spans and synchronous measurements (`db.client.operation.duration`) carry
  `db.system.name` and, until 1.0, the legacy `db.system`, unless
  `OTEL_SEMCONV_STABILITY_OPT_IN` holds `database` without `database/dup`
  (Go's rule; design D-JS9). The helper wraps the tracer and meter for this,
  not a span processor. Never recorded: bound values (pg
  `enhancedDatabaseReporting` off; mysql2 records the statement as written,
  where upstream would format its values into `db.query.text`), connection
  strings and users. A library loaded before `setup()` is still patched. The
  core now warns once about an `OTEL_SEMCONV_STABILITY_OPT_IN` value without
  `http` or with `http/dup` (Node HTTP is stable-only), and the
  `OTEL_HELPER_EXTRA_INSTRUMENTATION` warning names `sqlInstrumentation()`,
  `redisInstrumentation()`, `awsInstrumentation()` and the option. Pool
  metrics: Node emits what upstream emits (pg `db.client.connection.*`;
  mysql2 none). Go and Python use other names; recorded with the other
  divergences in design D-JS14 (rows of 2026-11-02). Tests run against real
  PostgreSQL and MySQL: new compose services `sql-postgres`
  (postgres:17.11-alpine) and `sql-mysql` (mysql:8.4.11), data on tmpfs, used
  by `node-tests`. They include the D-JS11 ESM custom-preload test (hooks
  plus `setup({ instrumentations })`, a pg span). `ci/api-range-install.sh`
  also installs `-sql` and checks the shared packages are single copies.
  Test-only devDependencies: `pg`, `mysql2`.
- **Node.js SQL instrumentation fix round (task B10a, 2026-11-03; not
  released).** Failed DB spans keep status ERROR with no description, since
  the database's error text can quote a bound value (contract task A25). The
  `-sql` tracer wraps `setStatus`; the `exception` event is kept. mysql2 span
  names now come from the statement: the first word, uppercased, after
  whitespace and comments. Library detection: every library whose contrib
  module loads is instrumented. One the app does not resolve is reported once
  and still patched when loaded, and `{ pg: true }` no longer throws on
  resolution; only a contrib module that does not load throws. The pool max
  now survives a re-setup (a generation guard on the meter wrapper). The
  database mode is read once per process, and the shared contrib
  instrumentations count enables. Core: `setup({ instrumentations })` rejects,
  by name, two instrumentations for one library and those the core installs
  (http, undici, express, grpc), so a raw `MySQL2Instrumentation` next to
  `sqlInstrumentation()` is refused. A rollback at any step, an inactive copy
  and an ignored `setup()` call turn off the opt-in objects they did not
  enable, and the ignored call names them. Enable errors are redacted. The
  `OTEL_HELPER_EXTRA_INSTRUMENTATION` warning names only
  `sqlInstrumentation()` until B10b/B10c. Tests: R1 error cases, pool
  re-setup and two-pool numbers, a stub-`pg` preload, nested `pg`, a mysql2
  preload. `SQL_TEST_REQUIRE` defaults to 1, and `ci/api-range-install.sh`
  now installs pg and calls `sqlInstrumentation()`.
- **CI: database services for the Node.js `node-test` job (task B10a,
  2026-11-03).** The job now runs `postgres:17.11-alpine` and `mysql:8.4.11`
  service containers, with the compose credentials and health checks, and
  sets `SQL_TEST_POSTGRES`, `SQL_TEST_MYSQL` and `SQL_TEST_REQUIRE=1`. The
  `@staffops/otel-helper-sql` database tests therefore run in CI instead of
  being skipped. In default mode, a library the app does not resolve is
  reported at `diag` debug level; only one named `true` gets the WARN.
- **Node.js `-sql` second fix round (2026-11-03; not released).** A span
  whose ERROR description is dropped now gets `error.type=_OTHER` when it
  carries no `error.type`. The WARN for an unresolved library named `true`
  names only the entry directory and the cwd; the full lookup chain goes on a
  debug line. A meter bind that throws restores the previous generation.
  `ci/api-range-install.sh` now proves that the installed `-sql` instruments
  the installed `pg`.
- **Node.js pino destination and winston transport (Phase B, task B10d; not
  released).** `@staffops/otel-helper-pino` exports
  `createPinoDestination({name?, minimumLogLevel?})`, passed by the app to
  `pino(options, destination)` or as one stream of `pino.multistream`;
  `@staffops/otel-helper-winston` exports `OtelHelperTransport` (winston's
  transport options plus `name`, `minimumLogLevel`). Both apply the per-record
  rule of `createLogger` (design D6): the OpenTelemetry Logs API while the
  helper's logs exporter is active, else one stdout JSON line of exactly the
  core shape (`time`, `level`, `msg`, `logger`, the fields, `exception`) —
  never both, and the app's other destinations/transports are never touched.
  Levels: pino 10/20/30/40/50/60 and winston's npm, syslog and cli levels map
  to TRACE 1, DEBUG 5, INFO 9, WARN 13, ERROR 17, FATAL 21 (custom levels by
  their bucket); `severityText` is the source level name upper-cased; the
  stdout `level` is one of DEBUG/INFO/WARN/ERROR (trace below debug, fatal
  as ERROR, as Python); trace-level records are always dropped and the
  helper's level (fixed, or the installed one, read per record) applies on
  top of the library's own (R4, as Go `NewSlogHandler`). The pino
  destination reads the serialized line (redaction, serializers and
  formatters apply) plus pino's destination metadata (level, logger,
  `messageKey`, `errorKey`); pino's `err` becomes the record exception (its
  other keys, e.g. `statusCode`, stay a map under the error key), `level` and
  `time` are not fields, nor `pid`/`hostname` when they are this process's.
  `pino(createPinoDestination())` with one argument works (`writable: true`).
  winston: `logger.error(err)`, `logger.error(msg, err)`, an `Error` field,
  `format.errors({ stack: true })` output and the `handleExceptions` /
  `handleRejections` info become the exception. Trace correlation: pino
  writes inside the logging call (the active context); while the winston
  transport is attached, the logger's `write` records each call's context
  and time for the info object in a process-wide `WeakMap` (nothing is added
  to the info; the logger's own `write` is restored when the last helper
  transport is removed), so a record winston buffers under backpressure keeps
  its caller's trace; an unstamped record (e.g. one written before the
  transport was attached) is uncorrelated (root context), never on the span active at
  delivery. A non-string message is a map body on OTel and its JSON text on
  stdout. `logger.flush(cb)` (pino), `close()` and `logger.end()` (winston)
  flush the logs pipeline; overlapping flushes are serialized and
  `shutdown()` waits for them (sdk-logs 3.0 returns early from a flush that
  overlaps another). `pino.transport({ target
  })` is not supported (its default export throws saying so). No contrib
  instrumentation is used. Peer dependencies: the core at the same version,
  `pino` `^10.0.0` / `winston` `^3.10.0`; a missing library throws an error
  naming it (contract D14 wording). The packages write through a new
  **non-public** core subpath, `@staffops/otel-helper/internal/bridge`
  (design D-JS6, 2026-11-02); the root `lint` script now builds the core
  first (type-aware lint of packages that import it). Test-only
  devDependencies `pino` ~10.4.0, `winston` ~3.19.0 (owner-approved list);
  the OTLP path is asserted on records decoded from a local receiver
  (`nodejs/test/otlp-logs.cjs`); package coverage gates at 99%;
  `ci/api-range-install.sh` also installs the packed bridges next to their
  peer floors, pino 10.0.0 and winston 3.10.0. Fix round 2026-11-03 (three
  reviews, no BLOCKING): the winston backpressure trace misattribution and
  the one-argument pino form (the two HIGHs above), plus the MEDIUM/LOW
  items recorded in design D-JS6 and D-JS14 (rows of 2026-11-03). Fix round
  2 (2026-11-03, re-reviews): the winston stamp moved off the info object
  (it leaked into other transports' output and went stale on reused
  objects), and bridge flushes are serialized with `shutdown()` waiting for
  them; plus pino overhead (cached bound name, reviver and split only when
  needed), the winston exception-handler shape (first-line body; no `date`,
  `process`, `os`) and the LOW items in design D-JS6 "Fix round 2". Final
  round (2026-11-03, re-reviews clean): the logger's `_transform` is wrapped
  too, so a logger-level `format.errors()` keeps the call's trace; bridge
  flushes are serialized per LoggerProvider (a new setup's flush never waits
  behind an old, hung one; `shutdown()` waits only for its own provider's
  pending flushes); flush failures are reported redacted.
- **Node.js Redis instrumentation (Phase B, task B10b; not released).**
  `@staffops/otel-helper-redis` implements `redisInstrumentation({ redis?, ioredis? })`
  over the official `@opentelemetry/instrumentation-redis` and
  `-ioredis` 0.72 (dependencies; `redis`/`ioredis` stay the app's),
  with the `-sql` model: one shared contrib instance per process, counted
  enables, cached files re-required so a library loaded before `setup()` is
  patched, `coveredInstrumentations` (a raw `IORedisInstrumentation` next to
  it is refused), the D14 rule (only a contrib module that does not load
  throws; an unresolved library is reported, WARN when named `true`). Spans
  carry `db.system.name=redis`, the legacy `db.system` (same opt-in rule as
  `-sql`), `db.operation.name`, `server.address`/`server.port`, and
  `db.query.text` as the command name and one `?` per argument (`SET ? ?`,
  Python's format): upstream's default serializer records keys and, for many
  commands (`GET`, `EVAL`, `SISMEMBER`, `LREM`, ...), every argument. ioredis
  spans are created without a parent too (upstream defaults
  `requireParentSpan` to true), as Go, Python and node-redis. A failed span
  keeps ERROR without description and gets `error.type=_OTHER` (A25 parity
  with `-sql`); Redis errors can quote arguments (`ERR unknown command 'X',
  with args beginning with: '<arg>'`). The exception event is kept (the A25
  residual). The tracer and meter wrappers and the database opt-in rule moved
  from `-sql` into a new internal core entry,
  `@staffops/otel-helper/internal/db` (not public API, as `internal/bridge`),
  shared by both packages. The database mode is now read once per process by
  the first `sqlInstrumentation()` or `redisInstrumentation()`. The
  `OTEL_HELPER_EXTRA_INSTRUMENTATION` warning also names
  `redisInstrumentation()`. Divergences from Go and Python (span names,
  `db.operation.name`, no Redis metrics in Node, ioredis handshake spans) are
  in design D-JS14 (rows of 2026-11-03). Tests: an in-process RESP fake
  (RESP2 and RESP3) and a real Redis, new compose service `redis-test`
  (redis:7.4.12-alpine, throwaway password, tmpfs, `REDIS_TEST_URL`,
  `REDIS_TEST_REQUIRE=1`), also a `node-test` CI service. They cover promise
  and callback forms, MULTI, pipeline, pub/sub, AUTH, WRONGTYPE, NOAUTH,
  WRONGPASS, connection refused, no argument, password or URL in any span,
  a preloaded library, a re-setup, the database mode and an ESM custom preload
  (without the hooks, ioredis imported from ESM is not instrumented).
  `ci/api-range-install.sh` installs `-redis` with redis 6 and ioredis 6 and
  checks both are patched. Test-only devDependencies: `redis`, `ioredis`.
  *Rulings 2026-11-03 (B10b Q1-Q4, contract task A26):* span names are the
  command uppercased (`SET`, as Python and semconv `{db.operation.name}`;
  connection spans `connect`; one span per MULTI/pipeline command, so not
  Python's `SET GET`); the contribs' `redis-SET` / `set` names are renamed.
  Go and Python now drop the Redis ERROR description too (A26). The
  exception event stays the accepted residual. In dup mode Node copies only
  `db.system` (Python also writes `db.statement`). Note: the database semconv
  mode is read once per process by the first `sqlInstrumentation()` or
  `redisInstrumentation()`, for both packages.
  *A26 fix round (2026-11-03):* a failed Redis span carries the reply's code
  as `db.response.status_code` and `error.type` (`_OTHER` without one; `-sql`
  unchanged); the statement serializer handles an empty name, typed-array
  names, huge argument counts and code points; `setConfig()` on `-redis` and
  `-sql` warns once that it is ignored; the JS5 "without the module hooks"
  warning names ioredis when `redisInstrumentation()` is passed. ioredis
  traces each connect/reconnect handshake as root spans (recorded in design
  D-JS14 with a mitigation). Span-name uppercasing is documented as parity
  with Python, not semconv.
  *A26 final round (2026-11-03):* only allowlisted Redis reply codes become
  `error.type` (a Lua `error_reply` word is `_OTHER`); a `setConfig()` call
  is warned on the next enable (once the DiagLogger is installed).
- **Node.js AWS SDK instrumentation (Phase B, task B10c; not released).**
  `@staffops/otel-helper-aws` implements `awsInstrumentation()` (no options
  yet; any key is a TypeError) over the official
  `@opentelemetry/instrumentation-aws-sdk` `~0.79.0` (a dependency, matched to
  the core's `@opentelemetry/instrumentation` `~0.300.0`); the AWS SDK v3
  clients (`@aws-sdk/client-*`) stay the app's. One span per `send()`,
  retries included, with the contrib's attributes (`rpc.system=aws-api`,
  `rpc.service`, `rpc.method`, `cloud.region`, `http.response.status_code`,
  S3 bucket, DynamoDB `db.system.name=dynamodb` and table names, SQS/SNS
  messaging attributes with trace context in the message attributes, Lambda
  `faas.*`) plus `aws.request_id` and `aws.region` (Go and Python keys; the
  contrib's `aws.request.id` is kept) and, on DynamoDB, the legacy
  `db.system` (same opt-in rule as `-sql`/`-redis`). The HTTP requests the
  SDK makes are not traced again under its span
  (`suppressInternalInstrumentation`, as Go and Python). Never recorded:
  credentials (access key, secret, session token, signature), request and
  response bodies (no DynamoDB statement serializer, no SQS payload parsing),
  DynamoDB `aws.dynamodb.item_collection_metrics` (it holds the partition-key
  value; dropped), an SNS phone number (`phone_number:**`, as Python). A
  failed call keeps ERROR without description (the A25 rule) and gets
  `error.type` = the AWS error code (`NoSuchKey`, `ThrottlingException`; only
  PascalCase codes `^[A-Z][A-Za-z0-9]{1,63}$`, else `_OTHER`); the exception
  event is kept (the A25 residual). Same model as `-sql`/`-redis`: one shared
  contrib instance per process, a client built before `setup()` is
  instrumented, `coveredInstrumentations` (a raw `AwsInstrumentation` next to
  it is refused), the D14 rule (only a contrib module that does not load
  throws). Divergences from Go and Python are in design D-JS14 (AWS rows of
  2026-11-03); Go and Python still keep the AWS ERROR description and Python
  records `item_collection_metrics` (contract rulings requested). The
  `OTEL_HELPER_EXTRA_INSTRUMENTATION` warning also names
  `awsInstrumentation()`. **Core internal:** the contrib-instance machinery
  of `-sql` and `-redis` (shared instances, counted enables, tracer/meter
  binding with the generation guard, the re-require of preloaded files, the
  one-time reports, D14 detection and load errors) moved to a new non-public
  entry, `@staffops/otel-helper/internal/contrib`, used by all three packages
  (no behavior change for `-sql` and `-redis`); `internal/db`
  `dbTracerProvider` gained an optional attribute sanitizer. Tests: an
  in-process fake AWS endpoint (S3, DynamoDB, SQS, SNS, Lambda wire formats,
  service errors, throttling, fake credentials only) with the real clients
  (`@aws-sdk/client-{s3,dynamodb,sqs,sns,lambda}` `~3.1153.0`, test-only
  devDependencies): every service, a secrets sweep (credentials, signatures,
  presigned query values, bodies), a re-setup, a preloaded client, an ESM
  custom preload with and without the loader hooks, the database mode;
  `ci/api-range-install.sh` installs `-aws` with the newest
  `@aws-sdk/client-sqs` and `client-s3` and checks one SDK span per `send()`.
  *Rulings 2026-11-03 (B10c Q1-Q3, contract task A27):* Go and Python now
  drop the AWS ERROR description and write the AWS error code as
  `error.type` too; Python drops `item_collection_metrics`; the RPC key split
  (Node.js and Python legacy, Go current) and Node's missing
  `server.address`/`server.port` are a recorded parity gap. Dependencies
  owner-approved.
  *Code-review round (2026-11-03):* a traced call made in an S3
  `GetObject` body listener records nothing (the body keeps the SDK's
  suppressed HTTP context; documented with a `context.with` workaround); the SNS phone number is redacted in attributes and the span name
  (an `InvalidParameter` exception event quotes it, the A25 residual); the
  secrets sweep also covers the diagnostics and `/metrics`; a transport
  error's `error.type` is its Node `code` (`ECONNREFUSED`); the request id of
  a failed call is written as `aws.request_id`; a Lambda `Unhandled`
  function error is not an ERROR span.
  *A27/A28 fix round (2026-11-03):* the S3 host id is also written as the
  semconv `aws.extended_request_id`; `getSignedUrl` records a phantom SDK
  span (D-JS14); HEAD errors without a body map their status code through
  the shared table (`403` → `Forbidden`).
  *A28:* Go and Python now redact the same URL query parameters as Node.js.
- **Node.js e2e smoke against a real Collector (Phase B, task B11a; not
  released).** `testing/e2e/smoke.sh` now also runs the `node-smoke` compose
  service (`node:${NODE_VERSION:-24}-slim`, no image build: the workspace is
  copied out of the read-only mount, `npm ci --ignore-scripts`, core built in
  the container) and `testing/e2e/node-smoke/smoke.cjs`, which emits the Go
  smoke markers (span `e2e-smoke-span`, counter `e2e.smoke.runs`, WARN record
  `e2e-smoke-log`) once per combination of `testing/e2e/combos.sh` — gRPC,
  http/protobuf with an `/otlp` path prefix, a per-signal traces endpoint, and
  TLS with the generated CA (`OTEL_EXPORTER_OTLP_CERTIFICATE`). Node has the
  same expectation rows as Go and Python. *Review LOWs (2026-11-03), all three
  languages:* two leak rows (per-signal metrics and logs must not reach the
  per-signal traces receiver) and a `logs-correlated` row (every marker log
  record carries the Trace ID of a marker span of its service, in every
  combination; Python checks "Failed to dispatch" inside
  `process.queue-consume`) — 16 rows per language, 48 in all, all pass (no
  `XFAIL`). Each Go and Node smoke run is bounded by `timeout 60`, the CI job
  by `timeout-minutes: 20`; `verify.sh`'s `node` step runs `node --check` on
  the smoke program. The CI job `e2e-smoke` is renamed "e2e smoke
  (Collector, Go + Python + Node, grpc/http-prefix/per-signal/tls)". The
  whole Node workspace is installed (the core build needs the root
  `typescript`/`@types/node`). No library code changed.
- **Node.js example apps (Phase B, task B11b; not released).**
  `nodejs/example/node-api` (express, `:8000`: `POST /notify`,
  `GET /notify/:notificationId`, `GET`/`POST /templates`, `GET /metrics/summary`,
  `GET /health`; JSON 422/400/404/500 errors; a 5 s deadline on every gRPC
  call), `node-backend` (gRPC `:50051`, `notification.NotificationService`
  from `nodejs/example/protos/notification.proto` loaded at runtime with
  `@grpc/proto-loader`, plus `grpc.health.v1` via `grpc-health-check`, set to
  NOT_SERVING when shutdown starts; a failing handler answers `UNKNOWN`) and
  `node-process` (four workers, one `startRootSpan()` per cycle, `fetch` to the
  API with a 5 s timeout and the shutdown signal, `/health` and `/healthz` on
  `:8080`) — the Node twins of the Python notification service: same routes,
  env vars (`BACKEND_URL`, `API_URL`; `NOTIFICATION_PROTO` added), ports, custom
  span names, span attributes, resource attribute `app.component` and legacy
  metric names. Each app handles SIGTERM with a bounded drain (4 s) and a 5 s
  flush, inside `docker stop`'s 10 s. Each has a `Dockerfile` (the helper built
  and `npm pack`ed from the workspace source in a first stage) and a
  `Dockerfile.demo` (installs a prebuilt tarball
  `nodejs/tarballs/staffops-otel-helper-${HELPER_VERSION}.tgz`), both on
  `node:24.22.2-slim`; the app dependencies are pinned by each example's
  `package-lock.json`, the helper's own dependencies resolve within its declared
  ranges at install time (as Python's wheel install), and the helper is not
  named in the examples' `package.json` (the `@staffops` npm scope is not
  claimed yet). All six Dockerfiles pass `docker build --check`; none was
  built locally. New `nodejs/.dockerignore`; the examples are linted with the
  workspace (`eslint.config.mjs`); the CI `example-images` job builds the three
  `Dockerfile`s and now waits for `node-test` (the `Dockerfile.demo` wiring
  needs the Node `npm pack` in `build-lib`, task B13; the matrix already has an
  `artifact_path` key for it). Verified against
  `otel/opentelemetry-collector-contrib` 0.160.0: one trace
  `process.queue-consume` → `POST /notify` (node-api) →
  `grpc.notification.NotificationService/SendNotification` (node-backend), the
  `Notification sent` log record correlated with `api.send-notification`, no
  `/health` or gRPC health span, and `docker stop` of node-process with the API
  paused exits within the budget with its final log flushed. Found on the way
  (design D-JS14): a hand-injected `traceparent` on an instrumented `fetch` is
  sent twice and breaks the trace, so `node-process` does not inject it.
- **Node.js custom-signals example and `EXAMPLES.md` (Phase B, task B11c; not
  released).** `nodejs/example/node-custom-signals/custom-signals.js` is the
  Node twin of `go-custom-signals` and `python-custom-signals`: one batch of
  three orders (`ord-3` fails), root span `orders.process-batch`
  (`startRootSpan`, `batch.size`, ERROR `1 of 3 orders failed`), child spans
  `order.process` (`order.id`, `order.type`, event `order.validated` with
  `order.amount`, exception event + ERROR `payment declined`), the instruments
  `order.processed` (Counter), `order.duration` (Histogram, `s`, buckets 5 ms –
  1 s), `order.active` (UpDownCounter) and `order.queue.depth` (observable
  gauge, `queue.name`) on scope `orders`, with `error.type` on failures only,
  and the `order processed` / `order failed` / `batch finished` log records
  correlated with the active span. The counts use `valueType: ValueType.INT`
  (integers, as Go and Python; the JS default is a double). `Dockerfile` and
  `Dockerfile.demo` as the other Node examples. `nodejs/example/EXAMPLES.md`
  mirrors `python/example/EXAMPLES.md` (custom signals, the notification
  service per app, cross-service trace, PromQL/TraceQL) and adds "Differences
  from Python and Go": `exception.type` (`PaymentDeclinedError`,
  `__main__.PaymentDeclinedError`, `*main.PaymentDeclinedError`), log scopes,
  no exemplars in Node, express `http.route` syntax, the extra express and
  ASGI spans, gRPC span names, error response shapes vs FastAPI, the Node-only
  log records, the instrumented probe server, and the backend that finishes a
  call after the client's deadline. Verified against collector-contrib 0.160.0
  side by side with the Go and Python twins: the same span, metric and log
  names, attribute keys, units and instrument kinds. No library code changed.
  *Fix round 2026-11-03:* the 7 counters and up-down counters of node-api,
  node-backend and node-process also pass `valueType: ValueType.INT` (integer
  points, as Python; design D-JS14 rows "Integer instruments need
  `ValueType.INT`" and "Log instrumentation scope per language"); `ord-3`
  costs `10.5` in the Go, Python and Node twins (was `10`, an `Int` in Python
  and Node but a `Double` in Go); the Go and Python READMEs, Python
  `EXAMPLES.md` and the twin comments name `node-custom-signals`; links to the
  `.js` file are absolute GitHub URLs (MkDocs-safe).
- **Node.js documentation (Phase B, task B12; not released).** New
  `nodejs/README.md` (requirements, packages, installation from `npm pack`
  tarballs until B13, quick start, the Node.js environment-variable tables —
  `OTEL_LOG_LEVEL`, `OTEL_NODE_RESOURCE_DETECTORS`, no semconv default,
  `OTEL_HELPER_EXTRA_INSTRUMENTATION` without effect —, endpoint and TLS
  rules, public API, known differences), `nodejs/HOW-TO.md` (preloads and ES
  modules, express and outbound HTTP, gRPC, workers with `startRootSpan`,
  `createLogger` and the pino/winston packages with their caveats and
  overhead, custom signals with `ValueType.INT`, sampling, debug mode, the
  `-sql`/`-redis`/`-aws` packages — including a Collector filter for ioredis
  handshake root spans —, `/metrics`, `SetupOptions`/`Resolved`, the
  lifecycle: SIGTERM pattern, re-setup, foreign globals and `replaceGlobals`,
  `cluster`/pm2 per-worker ports, worker threads; testing your app,
  troubleshooting, FAQ) and `nodejs/TESTS.md`; a `README.md` in every package
  (npm packs it; `nodejs/ci/check-pack.mjs` now requires it). MkDocs: a
  Node.js section (`docs/site/nodejs/` symlinks), `docs.yml` path triggers,
  `site_description`. Root `README.md`: Node.js in the library matrix,
  architecture, quick start, guides, environment heading ("Node.js notes"),
  opt-in table and four gap rows (Redis metrics, ioredis handshake spans, AWS
  `server.address`, runtime metrics and exemplars); `docs/site/index.md`
  regenerated. `CONSUMING.md`: "Node.js (npm packages, planned)" — the
  unclaimed `@staffops` npm scope (do not install by name from npmjs), the
  `.npmrc` scope mapping for GitHub Packages and npmjs, peer dependencies.
  `AGENTS.md`, the `release` and `parity-case` skills, and a Go note
  (`go/HOW-TO.md` §12: the harmless `client` ERROR span per connection on
  Redis before 8). No library behavior changed.
  *Corrected 2026-11-03 (B12 fix round):* "No library behavior changed" no
  longer holds — the fix round added the Node.js `beforeExit` flush (see
  Changed); and the Go `client` ERROR span is recorded on the **first
  connection of each client** (Redis < 8), not "harmless … per connection"
  (see Changed).
  Dated spec corrections (B12, 2026-11-03): JS5 (modules loaded before
  `setup()` are re-patched and reported), D-JS3 (the helper reads and masks
  the TLS files for both protocols, A18), D-JS11 (`--require …/register`
  instruments ES module apps too), D-JS13 (a worker the primary disconnects
  gets no SIGTERM: flush on `process.once('disconnect', …)`; new test in
  `nodejs/packages/otel-helper/test/cluster.test.cjs`). The root README
  heading "Installing (private packages)" is now "Installing" (no link
  targeted the old anchor).
  *Fix round 2026-11-03 (three reviews; one HIGH):* **a process that ends on
  its own now flushes** — the first successful `setup()` of the main thread
  registers one `process.once('beforeExit')` listener that calls `shutdown()`
  when the app has not (Python's SDK does the same from `atexit`); not after
  a failed setup, not in a worker thread, never stacked by a re-setup,
  removed by `/testing` `reset()`; `process.exit()` and signals still need
  `await shutdown()` / the SIGTERM handler (`test/exit.test.cjs`; design
  D-JS10, JS7, D-JS14). Docs: short-lived jobs (HOW-TO §4, §13, quick
  starts), `import { shutdown }` in ES module quick starts with the preload
  and code `setup()` as alternatives (README, package README, CONSUMING),
  worker-thread preloads (file workers run `--import` and `--require`,
  `eval` workers only `--require`; guard custom preloads with
  `isMainThread`), cluster workers also flush on `SIGTERM`/`SIGINT`, pm2
  `kill_timeout`, `cluster.setupPrimary({ execArgv })`, per-library DB
  exception events and `error.type`, gRPC endpoints use only
  `scheme://host:port`, install order of the tarballs and the `.npmrc`
  mapping now, the Go `CLIENT MAINT_NOTIFICATIONS` span (first connection
  per client, with the `ModeDisabled` fix), two troubleshooting rows, and the
  LOW items. `nodejs/ci/check-pack.mjs` reports "required files present".
- **Node.js release CI (Phase B, task B13; nothing published yet).** A
  `nodejs/vX.Y.Z[-rc.N]` tag now runs `publish-nodejs-npmjs` and then
  `publish-nodejs-ghp` (`.github/workflows/ci.yml`): after `node-test`,
  `node-api-range`, the new `e2e-smoke-node` (Node 24 and the 22.15.0
  floor; `e2e-smoke` keeps Go and Python) and the new `build-lib-node` job,
  each checks that the tarballs are byte for byte the six `build-lib-node`
  packed (`sha256sum`, recorded as a job output) and that the tag matches
  every version location and every tarball manifest, then publishes them,
  core first — to npmjs through trusted publishing (OIDC, `--provenance`,
  environment `nodejs-release`, no token in the repository or in CI
  secrets), and in a separate job without `id-token` to GitHub Packages
  with `GITHUB_TOKEN`. Release candidates get the dist-tag `next`. A version
  already on a registry is skipped only when its `dist.integrity` equals the
  tarball's sha512 (so a re-run completes a partial publish); other bytes,
  or an unreadable registry answer, fail the job. `build-lib-node` packs
  the six packages once, runs `npm publish --dry-run` on every pull request
  and push for versions not yet on npmjs, and feeds the Node example images,
  which now build their `Dockerfile.demo` from those tarballs
  (`HELPER_VERSION` from the core's `package.json`). `testing/e2e/smoke.sh`
  gains a `SMOKE_LANGS` filter. New `nodejs/ci/version.mjs` (`check`, `set`,
  `dist-tag`, `tarballs`, `verify-sums`, `registry-state`) keeps the six
  `package.json` versions, the exact core peer pins, the `VERSION`
  constants, the lockfile entries, the `Dockerfile.demo` defaults and the
  README version mentions identical, with tests in
  `nodejs/ci/version.test.mjs`. The Python and .NET release jobs now fail
  when the tag does not match `pyproject.toml` (after PEP 440
  normalization) or every project `<Version>`; tag names and tokens reach
  their scripts through `env:` only. Every action in the publish jobs and
  in `build-lib-node` is pinned to a full commit SHA (`# vX.Y.Z` comment),
  kept current by the new `.github/dependabot.yml` (GitHub Actions,
  weekly). The `release` skill documents the Node publish, the required
  `nodejs-release` environment protection (tags `nodejs/v*` only, the owner
  as reviewer) and how to check it, the one-time first-publish bootstrap
  the owner runs (a throwaway `0.0.0-bootstrap.0`, deprecated at once: npm
  accepts a trusted publisher only for an existing package), the Node
  after-tagging checks and an SDK-bump checklist. `CONSUMING.md`: the
  `@staffops` npm scope is claimed (2026-11-04); until the first stable
  release `latest` is the deprecated bootstrap version, so always install
  an exact version. Both Node publish jobs pin Node 24.22.2 (its bundled npm
  11.19.1 publishes; no npm self-upgrade in the OIDC job), the
  `node-packages` artifact is kept 30 days on a `nodejs/v*` tag run, and
  Dependabot waits 7 days (`cooldown`) before proposing an action release.
  No library behavior changed.
- **`nodejs/ci/version.mjs` follows the release-candidate wording (task
  B14, 2026-11-04).** `set` now also rewrites, and `check` warns about,
  "Release candidate `<v>`", `@staffops/otel-helper[-x]@<v>` install specs
  and the tag `` `nodejs/v<v>` `` when they carry the previous core version,
  in `nodejs/README.md`, the package READMEs and now `nodejs/HOW-TO.md`;
  `check` also warns when a stable version is still described as a
  "release candidate" (by hand). The repository-root `README.md` and
  `CONSUMING.md` stay by-hand items (release skill, "Before tagging" step 3).
  Tests in `nodejs/ci/version.test.mjs`.
- **Node.js whole-package review fixes (Phase B, task B14; not released,
  2026-11-04).** `@staffops/otel-helper`: the warnings of the opt-in
  instrumentation packages (`setConfig()` ignored, a library that does not
  load or resolve) and the `/metrics` collection errors are now helper
  records on the diagnostics sink (`origin: 'helper'`), no longer SDK `diag`
  messages: the debug-level ones appear only with `OTEL_LOG_LEVEL=debug`, a
  collection error is one ERROR record per scrape with the count and the
  first error redacted, and a custom sink or the parity `helper_log` now
  sees them. The SDK `diag` messages the helper forwards are redacted
  (URL userinfo/query, tokens, credential-like `key=value` pairs) as its own
  error attributes are, and the text of a `/metrics` 500 response is
  redacted too. `@staffops/otel-helper-pino`: `flush(cb)` calls `cb` on its
  own tick, so a throwing callback is an uncaught exception, not an
  unhandled rejection. `@staffops/otel-helper-winston`: a foreign or
  unreadable value at its shared-registry key is left alone (the copy keeps
  a registry of its own) instead of being used as the registry. The source
  maps no longer embed the TypeScript sources (`inlineSources` dropped);
  the `.d.ts` module docs are user-facing (the root entry links to the
  README and HOW-TO). `nodejs/README.md` gains "API stability (0.x)": the
  root exports, `/register`, `/hooks` and the `SetupOptions`/`Resolved`
  fields are the contract; `/testing` and `internal/*` are not
  (`getState()`, `STATE_KEY` and `HelperState` are marked `@internal`,
  test-only); `spanProcessors` and `instrumentations` take SDK types and the
  `@opentelemetry/api` peer stays `<1.11`. CI sets `PARITY_RELEASE=1` on
  release tags (`go-test` and `python-test` on `v*`, `node-test` on
  `nodejs/v*`), so a parity case still skipped pending a task fails the
  release run; `testing/verify.sh --release` does the same locally.
  `tools/trace_matrix.py` no longer reads test files under `.claude/` (agent
  worktrees) and counts `nodejs/**/*.test.*` files such as
  `nodejs/ci/version.test.mjs`. Tests: the metrics listener tests wait for
  `listening`/`error` instead of a fixed delay and retry once on
  `EADDRINUSE`; every package's c8 gate states its thresholds.
- **Node.js B14 re-probe fixes (Phase B, task B14; not released,
  2026-11-04).** Correction to the entry above: the forwarded SDK `diag`
  text was not free of header values. With `http/protobuf`,
  `OTEL_LOG_LEVEL=all` and an endpoint answering 400/500 with the request
  headers echoed in its body, the `OTLPExporterError` response body reached
  stderr. `@staffops/otel-helper` now (1) drops the response body of an
  exporter error before forwarding (`<response body omitted: N
  characters>`; also a raw response buffer, and the JSON string the SDK's
  global error handler logs), (2) masks the values of
  `OTEL_EXPORTER_OTLP_HEADERS` and `OTEL_EXPORTER_OTLP_<SIGNAL>_HEADERS`
  (4 characters or more) wherever they appear, and (3) redacts more
  credential forms: escaped JSON (`\"api-key\":\"…\"`), util.inspect
  (`{ 'x-api-key': '…' }`), `credential`/`session`/`signature` names,
  `&`-joined query pairs, whole `Cookie`/`Set-Cookie` values and `Digest`
  parameters ("basic auth failed" stays). Layer 3 is pattern-based (HOW-TO
  §14). `@staffops/otel-helper-redis`: an ioredis command issued before the
  connection is `ready` (offline queue) or resent after a reconnect is one
  span, not two; the span starts when the command is issued, so it includes
  the wait. `db.operation.name` keeps the client's casing (node-redis
  `SET`, ioredis `set`), as semconv recommends; now documented. Packages:
  no source maps are built or published (they pointed at the unpublished
  `src/`; `ci/check-pack.mjs` rejects `*.map`); `@staffops/otel-helper-pino`
  and `-winston` export `VERSION` (kept by `ci/version.mjs`); each opt-in
  README and "API stability (0.x)" list the root exports (the `-pino`
  default export, the `pino.transport` target that only throws, is not
  contract). `testing/verify.sh --release` sets `PARITY_RELEASE=1` for
  every step it runs — scope it with `--only` (release skill); the skill's
  bootstrap clones the branch or commit that will be tagged. Go has the
  same response-body exposure through the SDK default error handler
  (backlog, `specs/config-contract-v0.3/tasks.md` Deferred); Python logs
  the status code and reason phrase only.
- **Node.js B14 third-round fixes (Phase B, task B14; not released,
  2026-11-04).** Correction to the entry above: the exporter-body leak did
  not need `OTEL_LOG_LEVEL=all` — the SDK logs exporter errors at ERROR, so it
  also happened at the default WARN. `@staffops/otel-helper`: the forwarding
  `DiagLogger` never throws into the SDK (a message it cannot render — a
  throwing getter or `inspect.custom`, a revoked Proxy — is one record with
  `msg="<diag message could not be rendered>"`); forwarded text is redacted
  before the 2048-character cut; the configured header values are also
  masked in their util.inspect-escaped form; `pwd` and `jwt` count as
  credential names, while status words (`16 UNAUTHENTICATED: denied`,
  `unauthorized: …`) are no longer redacted; a `Cookie` value with a quoted
  pair (`a="b"; sid=…`) is redacted whole; redaction stays linear on long
  backslash runs; a `/metrics` collection error whose text cannot be read is
  reported as `<unprintable error>` instead of failing the scrape's error
  path. The opt-in packages' library reports have a constant message with
  the details as attributes: `<package>: a library is not instrumented
  library=<name> error="…"` and `<package>: a library could not be resolved
  from the app directories; instrumented if it loads library=<name>
  dirs="…"` (match on the attributes, not the old wording). CI and the
  release skill: the Go gate comment names the joint `v*` tag (a `go/v*`
  module tag runs no CI); the Node bootstrap clones `$BRANCH`, the branch to
  be tagged.
- **Node.js B14 fourth-round fixes (Phase B, task B14; not released,
  2026-11-04).** Correction to the third-round entry above: its status-word
  rule and its backslash bound let some credentials through. Now a status
  word (`(un)authenticated`, `(un)authorized`) is exempt only unquoted and
  before `:`, and the pairs inside its value are still redacted
  (`unauthorized: token=<redacted>`); `Authentication: …`,
  `authentication=…`, `"authenticated":"…"`, `unauthorized=…` and
  `authentication_failed: …` are redacted. JSON escaped any number of times
  and long backslash runs inside quoted values are redacted again, in linear
  time; a quoted value cut open by the end of the scanned text is redacted to
  the end, and forwarded diag records are cut only from redacted text. The
  opt-in packages' `error` attribute is the load error's first line,
  redacted, at most 512 characters. Release skill: the bootstrap check stops
  (`false`) when the clone has no `nodejs/`.
- **Node.js B14 fifth-round fix (Phase B, task B14; not released,
  2026-11-04).** Correction to the fourth-round entry above: a status word
  followed by a space swallowed the next `name: secret` (`401 Unauthorized:
  x-api-key: …` kept the key), and so did any credential name
  (`Authorization: x-api-key: …`). Status words are no longer pair names at
  all, and a credential name followed by `: ` is never read as a value, so
  each such name now has its own value redacted. HOW-TO §14 lists the known
  pattern limits.
- **Python example: python-process no longer injects `traceparent` by hand.**
  The injection was a no-op (the httpx instrumentation replaces the header);
  `_propagation_headers()` is removed from
  `python/example/python-process/workers.py`. Emitted telemetry is unchanged.
- **Node.js `getTracer()`, `getMeter()` and `startRootSpan()` (Phase B, task
  B11b; not released).** `@staffops/otel-helper` now exports the Go
  `GetTracer`/`GetMeter`/`StartRootSpan` and Python
  `get_tracer`/`get_meter`/`start_root_span` counterparts from the design's API
  table (owner ruling 2026-11-03). `getTracer(name?, version?)` and
  `getMeter(name?, version?)` read the global providers, scope `otel-helper`
  when the name is omitted or empty. `startRootSpan(name, fn, { tracer?,
  attributes?, kind?, links? })` runs `fn(span)` in a new trace whatever span
  is active (baggage kept, the sampler sees no parent, kind INTERNAL by
  default, attributes set at start). A sync `fn` ends the span on return; a
  thenable result gives a real `Promise` (typed `Promise<T>`) that settles once
  and ends the span then. A throw, a rejection or a throwing `then` records an
  exception event (`exception.type` is the error's name, not its `code`) and
  ERROR `"<error name>: <message>"`, then the original value is rethrown;
  recording never throws. A non-string name or non-function `fn` throws
  `TypeError`. A promise that never settles leaves the span open, as in Go and
  Python. The examples use them.
  The return type is `RootSpanResult<T>` (`T extends PromiseLike<infer U> ?
  Promise<U> : T`, exported); a failure always sets ERROR, even when reading
  the error's `name` or `message` throws.
- **Go test hygiene:** `captureDiag` (`go/config_contract_test.go`) no longer
  prints slog timestamps, which made `TestDeprecatedEnvWarnsNamingReplacement`
  (`go/deprecation_test.go`) flaky.
- **License: Apache-2.0** (`LICENSE`, the official text; same as
  OpenTelemetry). Declared in `python/pyproject.toml` (`license`) and the
  NuGet packages (`PackageLicenseExpression`). Before this the repository had
  no license, so reuse was not legally granted.
- **Spec-driven tooling (`specs/repo-tooling/`).** `testing/verify.sh` runs
  the CI risk matrix locally (Go + ext, Python 3.10/lower, 3.13/lower,
  3.13/latest, e2e, `mkdocs --strict`, spec checks; `--quick`, `--only`);
  `tools/spec_lint.py` enforces the `specs/README.md` rules (ADR index,
  reversal actions, open `xfail:<task>` targets, no TBD, dated corrections);
  `tools/trace_matrix.py` builds the requirement → test matrix and fails on an
  uncovered SHALL requirement outside `tools/trace-allow.txt` (only citations
  inside a test definition count, not file headers);
  `tools/regen_index.py` regenerates `docs/site/index.md` from `README.md`
  (and refuses README constructs it cannot rewrite);
  CI job `spec-checks` runs them on Python 3.10 and 3.12, with the hook tests
  required (`REQUIRE_HOOK_TESTS=1`). `testing/e2e/smoke.sh` and `verify.sh`
  stop only the Collector/fake receivers they started. Claude Code project config: hooks
  (`.claude/hooks/`) — the commit reminder now fires only on `git commit`
  (it fired on most Bash calls), a guard that asks before Docker
  prune/rm/`down -v` and force-push/ref deletion/history rewrite, and a
  session-start summary of open spec tasks; skills `change-loop`, `release`,
  `parity-case`; agent `spec-refuter`.
- **Soak simulation spec (`specs/soak-simulation/`)** replaces D10's
  "soaked in DEV/HML" (no such environment); Kubernetes checks deferred as
  its Phase K.
- **Consumption docs: the repository is public.** `CONSUMING.md` drops the
  private-repo instructions — no `GOPRIVATE` (it silently disables Go
  checksum verification), anonymous wheel download; a token is needed only
  for the .NET packages (GitHub Packages). New: a `nuget.config` with
  `<packageSourceMapping>` pinning `OtelHelper*` to the GitHub source (the IDs
  are not registered on nuget.org), and `#sha256=` / `--require-hashes`
  guidance for the Python URL pin. Example `requirements.txt` files no
  longer name `staffops-otel-helper` (not registered on PyPI: a bare name could resolve
  to an unrelated package).
- **CI fixes found by the `v0.3.0-rc.1` tag run.** SQLAlchemy test floor
  `>=2.0.31` (2.0.0 does not import on Python 3.13; `python/ci/lower.txt`), and
  the e2e Go smoke builds with `-buildvcs=false` (the read-only repo mount owned
  by another uid made VCS stamping fail). The built smoke binary is no longer
  tracked.
- **Cross-language parity harness and CI matrix (0.3.0 task A1, ADR-008).**
  `parity/cases.json` (135 hand-maintained cases at A1, 184 at the end of
  Phase A — Go 181 pass / 3 skip, Python 182 pass / 2 skip at the end of
  Phase A; 187 after A13 — Go 184 pass / 3 skip, Python 185 pass / 2 skip — .NET `xfail:phase-c`,
  Node.js `skip:phase-b`) started from the 0.2.0 behavior of Go and Python with
  every 0.3.0 target as a strict `xfail:<task>`; it runs in
  both suites (`go/parity_test.go`, `python/tests/test_parity.py`, one
  process per case). Wire-level `export` cases run against a fake OTLP
  receiver (`testing/fakeotlp/`); `testing/docker-compose.yml` runs both
  suites and an e2e smoke against a real Collector (`testing/e2e/smoke.sh`).
  CI: Python now runs py 3.10/3.11/3.13 × lower-bound/latest dependencies, Go
  gets a ≥ 90% coverage gate (parity child processes included), both jobs run
  the export cases, the e2e smoke is its own job, and the workflow token is
  read-only except on the publish jobs.
  `tests/test_propagation.py` is a real test module; tests no longer read the
  private `MeterProvider._metric_readers`. No library behavior changed.
  Test-only dev extras: `fastapi`, `httpx`, `grpcio`.

## [.NET 0.2.1] - 2026-11-04

.NET-only patch (tag `dotnet/v0.2.1`, branch `release/dotnet-0.2` cut from
`v0.2.0`) for the .NET 8 end of support on 2026-11-10. Go and Python are not
part of this release.

### Changed

- **.NET: every package targets `net8.0` and `net10.0`.** `OtelHelper`,
  `OtelHelper.AWS`, `OtelHelper.Redis`, `OtelHelper.Sql` and
  `OtelHelper.Profiling` ship `lib/net8.0` and `lib/net10.0`; `net10.0` apps
  no longer need `DOTNET_ROLL_FORWARD`. Same OpenTelemetry versions (1.15.x),
  same code, same defaults: on a given runtime both assets emit the same
  telemetry. Building from source now needs the .NET 10 SDK.
- **.NET: package metadata.** `PackageLicenseExpression` `Apache-2.0` (with
  `LICENSE` at the repository root) and `RepositoryUrl`
  `https://github.com/StaffOps/otel-libs` on all five packages.

### Notes — moving an app to the .NET 9+/10 runtime

These follow the runtime and the upstream instrumentation, not the helper or
the target framework, so they were already true for `0.2.0` running on .NET 10
under roll-forward:

- Runtime metrics come from the built-in `System.Runtime` meter: `dotnet.*`
  instead of `process.runtime.dotnet.*`. Update dashboards and alerts.
- HttpClient `url.full`: query redaction changes from per-value
  `?key=Redacted` to `?*` (the runtime's own redaction); userinfo was already
  removed on .NET 8.
- ASP.NET Core 10 adds the `Microsoft.AspNetCore.Authorization`,
  `.Authentication`, `.Identity` and `.MemoryPool` meters and the Blazor
  Components meters, which the ASP.NET Core instrumentation subscribes to.

### Known issues

- `OtelHelper.Profiling` is not validated on .NET 10: it loads the native
  Pyroscope profiler, which the test suite does not exercise.
- `AddOtelHelperRedis()` without arguments (the overload that resolves
  `IConnectionMultiplexer` from DI) throws `NotSupportedException` when the
  tracer provider is built. Workaround: pass the multiplexer explicitly,
  `services.AddOtelHelperRedis(multiplexer)`. Unchanged from `0.2.0`; the fix
  is planned for the next .NET minor.

No other behavior change.

## [Go 0.2.1] - 2026-07-16

Go-only patch (tag `go/v0.2.1`). This entry sat under Unreleased until the
0.3 docs pass; the A5 per-record log handler (Go section above) supersedes the
mechanism described here.

### Fixed

- **Go: `NewLogger` silently discarded every log record when no OTLP endpoint
  was configured.** `NewLogger` always bridged `slog` through the OTel Logs
  API (`otelslog.NewHandler`) into the process's `LoggerProvider`. When
  `Setup()` runs without an OTLP endpoint, `configureLogging` builds that
  `LoggerProvider` with **no processor attached** (there is nothing to
  export to) — routing records through it in that case dropped every one of
  them, with no error and no output anywhere. Found in a consuming service:
  a full day of `alert_fired`/`anomaly_detected` structured logs never
  reached stdout or Loki, breaking dry-run alert auditing. `NewLogger` now
  checks whether `configureLogging` actually attached a processor; without
  one, it falls back to a plain `slog.NewJSONHandler` on stdout (same flat
  `msg`/`level`/`time` shape consuming apps already parse) instead of the
  OTel bridge.

## [0.2.0] - 2026-07-15

### Added

- **OTLP/HTTP protocol support** (all languages) — the SDK can now export via
  `http/protobuf` in addition to the existing `grpc` transport. Selection
  follows the standard `OTEL_EXPORTER_OTLP_PROTOCOL` env var; when unset, the
  endpoint port infers the protocol (`4318` → `http/protobuf`, else `grpc`),
  following a port-based convention (4318 → http/protobuf). Explicit code config wins
  over the env var, which wins over port inference. `http/json` is a valid
  OTel spec value but has no exporter implementation in any of the three
  SDKs — it fails validation instead of silently falling back. Programmatic
  override: `TelemetryOptions(otlp_protocol=...)` (Python),
  `WithOtlpProtocol(...)` (Go), `TelemetryOptions.OtlpProtocol` (.NET). Python
  and .NET manually append `/v1/{signal}` to the endpoint for the HTTP
  exporters (the Go SDK does this automatically).

## [0.1.0] - 2026-07-14

First stable release. All items tracked in `ANALISE-PROBLEMAS.md` (P1–P9)
are resolved; behavior is unchanged from `0.1.0-rc.1` for anything not
listed below — this release folds in everything shipped since the rc.

### Added

- **`OTEL_METRICS_EXPORTER` contract** (all languages) — selects the active
  metric exporter(s): `otlp`, `prometheus`, `otlp,prometheus`, or `none`.
  Replaces the previous either-or fallback (OTLP if an endpoint is set, else
  Prometheus) with reader accumulation on a single `MeterProvider`, so OTLP
  push and the `/metrics` scrape endpoint can now run **simultaneously**
  without double-counting. Unset behavior is unchanged (fully backwards
  compatible). Equivalent programmatic option:
  `TelemetryOptions(metric_exporters=[...])` (Python),
  `WithMetricExporters(...)` (Go), `TelemetryOptions.MetricExporters` (.NET).
- **Mountable `/metrics` handlers** for multi-worker deployments — Python
  `otel_helper.metrics_app()` (ASGI, mount on FastAPI/Starlette), Go
  `otelhelper.MetricsHandler()` (dedicated `prometheus.Registry`, not the
  `client_golang` global one). .NET's ASP.NET Core path
  (`OpenTelemetry.Exporter.Prometheus.AspNetCore` +
  `MapPrometheusScrapingEndpoint()`) is documented in `dotnet/HOW-TO.md`. All
  three support disabling the standalone listener (port `0`) while keeping
  the reader active for the mounted handler.
- **Standard OTel env var precedence** (all languages) — `OTEL_TRACES_SAMPLER`
  and `OTEL_METRIC_EXPORT_INTERVAL` now take priority over the proprietary
  `OTEL_HELPER_SAMPLE_RATIO` and the previously-hardcoded 30s interval,
  respectively. `OTEL_HELPER_*` vars keep working when the standard var is
  absent. Precedence everywhere: explicit code config > standard OTel env var
  > `OTEL_HELPER_*` env var > library default.

### Fixed

- **Cross-language `deployment.environment.name` resource attribute** (P8) —
  only Go emitted this attribute, and with the legacy semconv key
  (`deployment.environment`). All three languages now emit
  `deployment.environment.name` (semconv >= v1.27) identically, so the shared
  dashboards in `dashboards/` filter/group consistently regardless of which
  language emitted the telemetry.
- **.NET: options no longer resolved through two independent code paths**
  (P9) — `AddOtelHelper()` used to build a hand-rolled copy of the resolved
  `TelemetryOptions` (a duplicate `Configure` + `PostConfigure` call) separate
  from the one DI's `IOptions<TelemetryOptions>` pipeline would produce, so
  the two could silently drift apart. `ActivitySource`/`Meter` are now
  registered as DI factories resolved lazily from the real options pipeline;
  the resource/tracing/metrics/logging setup resolves through that same
  pipeline via a bootstrap `ServiceProvider`, removing the duplicate
  implementation. Side effect: invalid configuration now fails fast inside
  `AddOtelHelper()` itself instead of only surfacing later when something
  resolves `IOptions<TelemetryOptions>.Value` (e.g. `ValidateOnStart` at
  real app startup).

- **Go: `/metrics` listener robustness** — `ListenAndServe` errors were
  silently swallowed (a busy port meant no metrics and no warning — the exact
  "silent telemetry loss" class of bug this project targets). The listener
  now binds synchronously so a busy port fails `Setup` immediately, has a
  `ReadHeaderTimeout`, and its `Shutdown` joins the composite shutdown chain
  returned by `Setup`.

- **Python: `[aws]`, `[redis]`, `[sql]` extras are now real** — the SQLAlchemy,
  Redis, and botocore instrumentations were incorrectly bundled in the core
  package, making the extras no-ops and the core heavier than documented. They
  now install only via their extras (new `[all]` meta-extra added). The
  `otel_helper.ext` helpers raise an actionable `ImportError`
  (`pip install otel-helper[aws]`) when the extra is missing.
- **Python: library-style dependency ranges** — OTel dependencies were pinned
  with `==`, causing pip resolution conflicts for any app depending on a
  different OTel SDK version. Now `>=1.42,<2` (stable) / `>=0.63b0`
  (instrumentations).
- **Python: custom OTLP endpoint port no longer discarded** —
  `OTEL_EXPORTER_OTLP_ENDPOINT=https://gateway:14317` was silently rewritten to
  port 4317. The port is now preserved; 4317 applies only when absent (parity
  with Go/.NET).

## [0.1.0-rc.1] - 2026-07-02

First published pre-release. Consumed and validated end-to-end from the
registries (see [CONSUMING.md](CONSUMING.md)).

### Added

- **Opt-in instrumentation subpackages/extensions** (all languages). The core
  package stays lightweight; add only what you need.
  - .NET: `OtelHelper.AWS`, `OtelHelper.Redis`, `OtelHelper.Sql`,
    `OtelHelper.Profiling` (Pyroscope) — `services.AddOtelHelperAws()` etc.
  - Python: `otel_helper.ext.{aws,redis,sql}` with `instrument_aws()` /
    `instrument_redis()` / `instrument_sql()`; install via
    `pip install otel-helper[aws,redis,sql]`.
  - Go: separate modules `ext/otelaws`, `ext/otelredis`, `ext/otelsql`.
- **Prometheus `/metrics` fallback** (all languages). When
  `OTEL_EXPORTER_OTLP_ENDPOINT` is not set, metrics are exposed via a
  Prometheus HTTP endpoint on port 9464 (configurable via
  `OTEL_HELPER_METRICS_PORT`) instead of OTLP push. Traces run in-process only
  and logs go to stdout — the standard Kubernetes scrape pattern.
- **TLS OTLP export** (all languages). Transport is derived from the endpoint
  scheme, secure by default:
  - `https://host:4317` → gRPC over TLS (system CA trust store)
  - `http://host:4317` → plaintext
  - `host:4317` (no scheme) → TLS (secure default)
  - Override with the standard `OTEL_EXPORTER_OTLP_INSECURE` env var; explicit
    code config wins over env/scheme.
- **30-second metric export interval** (all languages), replacing the SDK
  default of 60s.
- `CONSUMING.md` — validated per-language install guide (auth, registry
  sources, CI variants, gotchas) plus a `read:packages` token guide.

### Fixed

- **Python/Go — schemeless `OTEL_EXPORTER_OTLP_ENDPOINT` silently dropped
  telemetry.** An endpoint like `collector.svc:4317` (no scheme) resolved to an
  invalid `scheme://None`, so the exporter failed quietly. Endpoint resolution
  now prepends a scheme and preserves the host.
- **CI published only the core .NET package.** `build-lib`, `publish-dotnet`
  (dev) and `publish-dotnet-release` (tags) now pack and publish all five
  packages (core + AWS/Redis/Sql/Profiling).
- **CI demo image glob matched subpackages.** `OtelHelper.*.nupkg` matched
  `OtelHelper.AWS.nupkg` etc., breaking version extraction; now
  `OtelHelper.[0-9]*.nupkg` isolates the core version.

### Changed

- **Namespace/module migration to the `StaffOps` org.** NuGet source and
  repository URLs use `github.com/StaffOps`; Go modules, imports and examples
  use `github.com/staffops/staffops-otel-libs/go`.
- **Tests hardened with in-memory exporters and behavioral assertions** across
  all three languages (signal disabling actually drops data, sampler type
  verified, endpoint/TLS resolution covered, validation throws). Coverage:
  .NET ~94%, Python ~90%, Go ~94% — all above the 90% CI gate.

### Notes

- Pre-release only: not yet run in production. Per the versioning policy, a
  stable `0.1.0` is cut once the library is validated in a real workload.
- For a TLS gateway with a valid cert, consume with
  `OTEL_EXPORTER_OTLP_ENDPOINT=https://<gateway>:4317` — no extra flags.
