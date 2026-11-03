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

### Go — detailed changes

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

### Internal

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
