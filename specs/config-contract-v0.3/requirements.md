# Requirements — Config Contract & Parity (`0.3.0`)

> **Corrected 2026-10-27 after adversarial review** (observability fact check,
> dev code/rigour check, consistency check — 47 defects raised, 6 BLOCKING).
> Main corrections: gRPC endpoints without a port go to **443** in both SDKs,
> not 80 (R1 rewritten, design D1.b reversed); Python `excluded_urls` drops
> health requests from metrics as well, so the R7 mechanism changed; deferring
> schemeless endpoints and TLS-conflict cases to the SDK breaks them or makes
> the two languages disagree (R1 and R2 now carry explicit helper-owned
> exceptions); "OTLP configured" is per signal; `unknown_service:*` differs
> per SDK; the Go-only R3 moved to G8. A second same-day pass
> (best-practice research + prototypes) made schemeless gRPC supported,
> made the helper normalize TLS conflicts, and left `service.instance.id` to
> the Python SDK where it already handles forks. The first version of this spec stated
> each of these confidently and wrongly. It is the defect class this round
> exists to catch.

## Context

Second audit round (2026-10-27, `main` @ `3f1dc35`). Each finding was produced
by a language auditor and independently validated by an `observability` agent
(spec / semconv / SDK source / backend behavior) and a `dev` agent (reproduced
with throwaway tests on an isolated copy). Contested points went through a
reconciliation round, and the spec set itself through an adversarial review.
Single-source findings are marked ⚠️ and must be re-validated before
implementation.

Headline: most bugs are **the same bug in every language** — each lib
re-implements OTLP env handling the SDK already does correctly
([ADR-001](../decisions/ADR-001-defer-otlp-env-to-sdk.md)).

Previous round: `ANALISE-PROBLEMAS.md` P1–P9 (resolved in 0.1.0/0.2.0).

### Finding index (F = audit findings, evidence below)

| ID | Finding | Requirement |
|----|---------|-------------|
| F1 | Endpoint normalization forces :4317, drops path, breaks IPv6 / code URLs | R1 |
| F2 | Per-signal OTLP vars ignored; per-signal-only → no export, no error | R2 |
| F3 | Custom CA / mTLS env discarded (Go) | G8 |
| F4 | Unset/unknown environment → `LOCAL` | R4 |
| F5 | Proprietary vars beat standard ones; helper defaults overwrite `OTEL_RESOURCE_ATTRIBUTES` | R5 |
| F6 | Sampler not ParentBased in Python/.NET | R6 |
| F7 | Health exclusion: same paths, three behaviors | R7 |
| F8 | Dead/duplicated knobs; env parse errors crash setup | R8 |
| F9 | Docs contradict code (8 items) | A13 |

## Scope

| Phase | Languages | In scope |
|-------|-----------|----------|
| **A** (now) | Go + Python | all R-*, G-*, P-* |
| **B** | Node.js (new lib) | all R-* from day one ([ADR-004](../decisions/ADR-004-phased-rollout-go-python-first.md)) |
| **C** | .NET | all R-* + .NET backlog (tasks.md Phase C) |

### Out of scope
- Fixing the upstream SDK TLS-precedence bugs (issues to be filed; the helper
  shim covers them, design.md D1.c).
- Moving Prometheus out of core ([ADR-007](../decisions/ADR-007-keep-prometheus-in-core.md)).
- SDK-level profiling for Go/Python (cluster eBPF profiling is the baseline).
- Opt-in to ignore remote unsampled parents ([ADR-002](../decisions/ADR-002-parentbased-sampler.md) reversal action only).
- Collector/Helm changes — prerequisites (design.md D11), owned outside this repo.

---

## Cross-language requirements (R)

### R1 — Endpoint resolution follows the OTLP exporter spec
Evidence (F1): `https://otlp.vendor.io` + `http/protobuf` → `:4317/v1/traces`
(Go `go/config.go:87-88`, Python `python/otel_helper/config.py:74`); path prefix
dropped on the wire in Python (`HITS ['/v1/traces']` for `.../otlp/`); IPv6
`[::1]:4317` → `::1:4317`; Go `WithEndpoint("http://collector:4318")` →
`too many colons` (`go/options.go:57`).

- WHEN the endpoint is a URL with a scheme and no port, protocol `http/protobuf`
  THEN the exporter SHALL use the scheme's default port (443 / 80).
- WHEN the endpoint is a URL with a scheme and no port, protocol `grpc`
  THEN the helper SHALL use port **4317** and log a warning recommending an
  explicit port (design.md D1.b — both SDKs would otherwise dial 443).
- WHEN the generic endpoint has a path prefix and the protocol is `http/protobuf`
  THEN requests SHALL go to `<prefix>/v1/<signal>`.
- WHEN the endpoint is an IPv6 literal THEN it SHALL be exported to unchanged.
- WHEN a **generic** endpoint URL is set in code THEN the helper SHALL append
  `/v1/<signal>` for `http/protobuf` (the SDKs take code URLs verbatim); WHEN
  a **per-signal** endpoint is set in code THEN it SHALL be used verbatim.
- WHEN a schemeless `host:port` is given (env or code) THEN the helper SHALL
  rewrite it to `https://host:port` (`http://` only when `OTEL_EXPORTER_OTLP_INSECURE=true`)
  and default the port to the protocol port when absent. For `grpc` this is a
  supported form (legal per spec); for `http/protobuf` a deprecation warning
  SHALL be logged. (Deferring it to the SDK fails: Go `url.Parse("10.0.0.5:4317")`
  errors and the exporter silently falls back to localhost; Python HTTP sends a
  scheme-less URL to `requests`.)

### R2 — Per-signal OTLP variables are honored
Evidence (F2): only `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` set → `PROCS []` (no
exporter, no error) in Go and Python; Go generic + per-signal → host A + path
`/custom/traces`.

- WHEN `OTEL_EXPORTER_OTLP[_<SIGNAL>]_INSECURE` contradicts the endpoint scheme
  THEN the scheme SHALL win (spec) in every language and a warning SHALL be
  logged; WHEN `OTEL_EXPORTER_OTLP_CERTIFICATE` is set with a plaintext
  endpoint THEN a warning SHALL be logged.
- WHEN `OTEL_EXPORTER_OTLP_<SIGNAL>_ENDPOINT` is set THEN that signal SHALL be
  exported to it with its path verbatim (no `/v1/<signal>` appended; the value
  is still trimmed and userinfo/query are stripped per design D1 rulings), with or without the
  generic endpoint.
- OTLP is configured **per signal**: code endpoint ∨ generic env ∨ that
  signal's env endpoint. WHEN only `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` is set
  THEN traces SHALL be exported and metrics/logs SHALL NOT get an OTLP exporter
  (no silent export to `localhost:4317`).
- WHEN `OTEL_METRICS_EXPORTER=otlp` comes from env and no metrics endpoint is
  configured THEN a warning SHALL be logged and the legacy inference used;
  from code THEN setup SHALL fail.
- WHEN `OTEL_EXPORTER_OTLP_<SIGNAL>_PROTOCOL`, `_HEADERS`, `_TIMEOUT`,
  `_COMPRESSION` or the generic equivalents are set and no code value
  overrides them THEN the exporter SHALL apply them.
- WHEN neither env nor code sets compression THEN the helper default SHALL be
  `gzip` in every language (design.md D1.d).
- WHEN `OTEL_EXPORTER_OTLP[_<SIGNAL>]_TIMEOUT` is set THEN it SHALL be
  interpreted as milliseconds in every language (spec; Python SDK reads
  seconds — helper shim, design.md D1.e).

### R4 — Missing environment never means LOCAL
Evidence (F4): Go `config.go:53-66`, Python `config.py:44-49` map unset/unknown → `LOCAL`
([ADR-003](../decisions/ADR-003-environment-unset-not-local.md)).

- WHEN neither code, `OTEL_RESOURCE_ATTRIBUTES` nor `ENVIRONMENT` provide an
  environment THEN `deployment.environment.name` SHALL be absent and a warning
  SHALL be logged once.
- WHEN `ENVIRONMENT` holds a value outside `LOCAL/DEV/HML/PRD` THEN it SHALL be
  emitted verbatim.
- WHEN `OTEL_RESOURCE_ATTRIBUTES` contains `deployment.environment.name`
  THEN it SHALL win over `ENVIRONMENT`.
- WHEN the environment is unset or unknown THEN helper-created log handlers
  SHALL use level INFO (LOCAL keeps DEBUG).

### R5 — Standard variables win
Evidence (F5): `SERVICE_NAME` beats `OTEL_SERVICE_NAME` (Go `config.go:98-104`,
Python `config.py:175-176`); `OTEL_RESOURCE_ATTRIBUTES=deployment.environment.name=PRD`
overwritten by `LOCAL`.

- WHEN both `OTEL_SERVICE_NAME` and `SERVICE_NAME` are set THEN `service.name`
  SHALL be `OTEL_SERVICE_NAME`.
- WHEN no service name is configured anywhere THEN the helper SHALL set none,
  `service.name` SHALL be the SDK default — prefix `unknown_service`; the
  `:<exe>` suffix is SDK-version dependent (Go: binary name; Python ≥ 1.45:
  interpreter name; Python 1.42: no suffix, the process detector is not a
  default there) — and a warning SHALL be logged.
- WHEN `OTEL_RESOURCE_ATTRIBUTES` sets a key THEN no helper default SHALL overwrite it.
- WHEN `service.instance.id` is not provided THEN every process SHALL get a
  unique one (Go and Python 1.42–1.44: helper UUID; Python ≥ 1.45: SDK
  detector); WHEN setup runs in each pre-fork worker THEN ids SHALL differ per
  worker; WHEN an env/code id is used in forked workers THEN a warning SHALL be
  logged and the helper SHALL NOT rewrite it (design.md D4). Documented
  limitation: Python SDK ≥ 1.45 itself regenerates the id after fork
  (`sdk/resources/__init__.py:640-648`).

### R6 — Sampler is ParentBased
Evidence (F6): remote `sampled=0` parent → Python 1000/1000 children sampled
(Go 0/1000) ([ADR-002](../decisions/ADR-002-parentbased-sampler.md)).

With no sampler configured:
- WHEN a span has no parent THEN it SHALL be sampled.
- WHEN a span has a remote parent with `sampled=1` THEN it SHALL be sampled;
  with `sampled=0` THEN it SHALL NOT be sampled.
- WHEN a span has a local parent THEN it SHALL inherit the parent's decision.

And:
- WHEN `OTEL_HELPER_SAMPLE_RATIO=x` (deprecated) is set and `OTEL_TRACES_SAMPLER`
  is not THEN the sampler SHALL be `parentbased_traceidratio(x)`.
- WHEN `OTEL_TRACES_SAMPLER` holds an unknown value THEN a warning SHALL be
  logged and the default used.

### R7 — One health-exclusion semantics
Evidence (F7), reproduced 2026-10-27 (span created? per server instrumentation):

| path | Go `NewHTTPHandler` | Python FastAPI | .NET ASP.NET Core |
|---|---|---|---|
| `/Health` | yes | yes | no |
| `/health/live` | yes | **no** | yes |
| `/healthz` | no | no | no |
| `/users/ping` | yes | **no** | yes |

- WHEN a server request path equals a health path (case-insensitive, trailing
  slash and query ignored) THEN no server span SHALL be recorded — whether the
  request has no parent or a remote parent — and no child span of it either.
- WHEN a path merely contains a health path (`/users/ping`, `/healthcare`)
  THEN a server span SHALL be recorded.
- WHEN a health request is served THEN it SHALL still be counted in HTTP server
  metrics. (Python's `excluded_urls` skips the whole ASGI middleware —
  `instrumentation/asgi/__init__.py` — so it SHALL NOT be used for health
  paths; design.md D5.)
- WHEN a gRPC call targets `grpc.health.v1.Health/*` (Check or Watch) THEN no
  span SHALL be recorded; WHERE the instrumentation emits RPC server metrics
  (Go otelgrpc `rpc.server.call.duration`) the call SHALL still be counted.
  Python contrib `opentelemetry-instrumentation-grpc` emits no RPC metrics at
  all (0.63b0–0.66b1, no meter in the package); the helper SHALL NOT add them
  (ADR-001) — a documented parity gap, not a task.

### R8 — Env parsing never crashes the app; secrets never logged
Evidence (F8): `OTEL_METRICS_EXPORTER=console` raises in Python; `HasInstrumentation`
never called in Go (`options.go:128`).

- WHEN an env var holds an unknown or unparseable value THEN a warning SHALL be
  logged naming the variable and the default used (no exception).
- WHEN the same invalid value is passed in code THEN setup SHALL fail with an
  error naming it.
- WHEN `OTEL_{TRACES,METRICS,LOGS}_EXPORTER` is `none` THEN that signal SHALL
  have no exporter; `console` SHALL be accepted; the deprecated `logging`
  value SHALL be treated as `console` with a warning; a list containing `none`
  together with another value SHALL warn and be treated as `none`.
- WHEN `OTEL_PROPAGATORS` is set THEN it SHALL be honored.
- WHEN a deprecated variable (`SERVICE_NAME`, `ENVIRONMENT`,
  `OTEL_HELPER_SAMPLE_RATIO`, `OTEL_HELPER_DISABLED_SIGNALS`, Python
  `OTEL_HELPER_EXTRA_INSTRUMENTATION`) is used THEN a deprecation warning SHALL
  be logged once.
- WHEN the helper logs a warning or error about configuration THEN it SHALL
  NOT print values of `*_HEADERS`, `*_CLIENT_KEY`, or URL userinfo/query
  strings; a fixture case SHALL assert the redaction **on helper diagnostics**.
  Scope: messages emitted by the SDKs themselves (e.g. Go `parse headers`
  `input`, Python invalid-header warning echo the raw value) are upstream
  behavior and out of the helper's control (G7 forbids masking via env).
- WHEN any endpoint (env or code, either protocol) contains URL userinfo or a
  query string THEN the helper SHALL strip them before the SDK sees the URL and
  warn (redacted) — auth belongs in `OTEL_EXPORTER_OTLP_HEADERS` *(amended
  2026-10-28: first version said gRPC only)*. Original note: (Go: the SDK already dials only the
  host for env URLs; the helper strips it from URLs it passes explicitly) (meaningless in a gRPC target; the
  Python exporter otherwise prints it in export-failure warnings).

### R9 — Debug marker is a local span attribute
([ADR-006](../decisions/ADR-006-debug-span-attribute.md))
- WHEN debug mode is on THEN every span whose parent is absent **or remote**
  (the service's entry spans) SHALL carry `debug=true`.
- WHEN a span has a local parent THEN the helper SHALL NOT add `debug`.
- WHEN an inbound request carries debug tracestate or baggage and debug mode
  is off THEN no `debug` attribute SHALL be set (negative fixture case).

### R10 — Same HTTP semantic-convention generation everywhere
Evidence (A2 validation, 2026-10-28): otelhttp v0.72 emits only stable HTTP
semconv (`http.server.request.duration` in s, `http.request.method`, …) with no
way back; Python contrib 0.63b0–0.66b1 defaults to the old generation
(`http.server.duration` in ms, `http.method`, `http.target`) unless
`OTEL_SEMCONV_STABILITY_OPT_IN=http`. Prototype on both Python legs: helper
default before instrumenting → stable; set after an instrumentor ran → stays
old, silently.

- All languages SHALL emit the stable HTTP semantic conventions for HTTP
  metrics and spans. Metric names `http.server.request.duration` and
  `http.client.request.duration` (unit `s`) and attributes
  `http.request.method`, `http.response.status_code`, `http.route` (server,
  when known) and `url.scheme` (server) SHALL match; language-specific extra
  attributes are allowed and listed in the parity fixture notes.
- WHEN `OTEL_SEMCONV_STABILITY_OPT_IN` is unset THEN the Python helper SHALL
  default it to `http` before any instrumentor is initialized (including the
  `ext.*` entry points); a user-provided value always wins (incl. `http/dup`).
- WHEN the effective HTTP mode is not stable (user value without `http`, or
  stability already initialized before the helper ran) THEN one warning SHALL
  be logged.
- Database semconv (A10 validation, 2026-10-28): WHEN `OTEL_SEMCONV_STABILITY_OPT_IN`
  is unset THEN both languages SHALL emit **both** the new (`db.system.name`, semconv
  v1.43) and legacy (`db.system`) keys (Python defaults the variable to
  `http,database/dup`; Go's ext/otelsql emits both); WHEN it contains `database`
  but not `database/dup` THEN only the new keys SHALL be emitted (Go too).
  Known gap: Python contrib copies the raw vendor into `db.system.name`
  (SQL Server → `mssql`, Go → `microsoft.sql_server`) — documented.
- RPC metrics follow each upstream (Python grpc contrib emits none). Runtime
  metrics are language-specific namespaces (`go.*` vs `process.runtime.cpython.*`)
  and out of parity scope.

## Go-only (G)

- **G1** Evidence: failed Setup → `global TP sdk=true span.IsRecording=true`,
  `MetricsHandler after failed Setup: 200` (`go/otelhelper.go:67-85`).
  WHEN `Setup` returns an error THEN every provider, processor, exporter and
  listener built SHALL be released (no leaked goroutines), globals SHALL be
  **left as they were before the call** (never clobbering providers the app
  installed) and `MetricsHandler()` SHALL return 503. *(Amended 2026-10-28: first
  version said "globals SHALL be no-op".)*
- **G2** Evidence: `shutdown#2 err=reader is shutdown`; `Setup after shutdown sameFn=true`.
  WHEN `Shutdown` is called twice THEN the second call SHALL return the first
  result; WHEN `Setup` is called after `Shutdown` THEN a fresh pipeline SHALL be built.
- **G3** Evidence: `NewSlogHandler Enabled(Info)=false` with no endpoint.
  WHEN no logs exporter is active THEN records from `NewLogger` **and**
  `NewSlogHandler` SHALL reach stdout, including loggers created before `Setup`.
- **G4** WHEN the module is built with otel v1.47 / contrib v0.72 THEN it SHALL
  compile, and gRPC instrumentation SHALL be provided via stats handlers
  ([ADR-005](../decisions/ADR-005-go-1-26-floor-grpc-stats-handlers.md)).
- **G5** WHEN an HTTP route is served via `NewHTTPHandler` on a `ServeMux`
  pattern THEN the span name SHALL include the route (`http.route`).
- **G6** Evidence: `go/ext/otelsql/sql.go:15-30` sets `semconv/v1.26.0`
  `DBSystemKey.String(driverName)` → raw `pgx`/`sqlite3`.
  WHEN `ext/otelsql` instruments a driver THEN the driver name SHALL be mapped
  (`pgx`/`postgres` → `postgresql`, `sqlite3` → `sqlite`, `mysql` → `mysql`,
  `sqlserver`/`mssql` → `microsoft.sql_server`, else `other_sql`) and emitted as
  `db.system.name` plus the legacy `db.system` until 1.0 (exact semconv version
  pinned in A10); WHEN `ext/otelredis` instruments a client THEN pool metrics
  SHALL be emitted. Each ext module SHALL have tests.
- **G7** The helper SHALL NOT call `os.Setenv`.
- **G8** Evidence: `OTEL_EXPORTER_OTLP_CERTIFICATE` → `x509: certificate signed
  by unknown authority` (empty `tls.Config{}` on all six exporters).
  WHEN `OTEL_EXPORTER_OTLP_CERTIFICATE` (or `_CLIENT_CERTIFICATE`/`_CLIENT_KEY`)
  is set THEN the TLS handshake SHALL use it.
- **G9** WHEN `OTEL_HELPER_EXTRA_INSTRUMENTATION` is set THEN a warning SHALL
  say it has no effect in Go (option removed).
- **G10** WHEN no timeout/compression/headers are set in code THEN the helper
  SHALL resolve them from env per design D1 rulings (milliseconds, gzip default,
  header parsing) and pass the resolved values explicitly — never a hard-coded
  value that masks env (today `ExportTimeoutMs: 10_000` at `go/options.go:145`
  is indistinguishable from a code value). *(Amended 2026-10-28: the first
  version said "none SHALL be passed"; A3 validation showed the SDKs disagree,
  so the helper owns these inputs — ADR-001 corrections.)*

## Python-only (P)

- **P10** Evidence: root handlers `['LoggingHandler']`, `HELLO` never printed;
  `LEVEL 30` under PRD (`python/otel_helper/logging_setup.py:43-45`).
  - WHEN no logs exporter is active THEN no OTel handler SHALL be attached and
    `logging.basicConfig()` called afterwards SHALL work.
  - The helper SHALL set the root level only when, at setup, root has no
    handlers and `logging.root.level == logging.WARNING` (the stdlib default);
    otherwise it SHALL leave root untouched.
- **P11** Evidence: class stays `FastAPI`, `/health` produces a SERVER span.
  WHEN `FastAPI` was imported before `setup_telemetry()` THEN the app SHALL
  still be instrumented (public `instrument_fastapi(app)` helper), and the
  HOW-TO SHALL show a working order.
- **P12** WHEN `setup_telemetry()` is called twice THEN it SHALL return the
  first resolved options; a public `shutdown_telemetry()` SHALL flush and shut
  down all providers.
- **P13** WHEN a sync `grpc.server()`/channel is used THEN spans and
  propagation SHALL work as for `grpc.aio`.
- **P14** WHEN `/metrics` is scraped THEN only OTel-produced series SHALL be
  present (dedicated `CollectorRegistry`, no `process_*`/`python_gc_*`).
- **P15** WHEN the suite runs on the declared lower bounds (sdk 1.42 / contrib
  0.63b0) THEN it SHALL pass; dependency ranges SHALL be lockstep-bounded.
- **P16** uvicorn access/error loggers (`propagate=False`) SHALL reach the OTel
  handler when a logs exporter is active.
- **P17** The package SHALL ship `py.typed` and SHALL NOT import from any
  `opentelemetry.*._internal` module; `tests/test_propagation.py` SHALL contain
  real tests and no import-time setup.
- **P18** ⚠️ (dev-only finding) WHEN SQLAlchemy ≥ 2.1 is installed THEN SQL
  instrumentation SHALL either work or warn that the installed contrib
  doesn't support it (today: silent no-op — "only instruments <2.1.0").

## Acceptance criteria (Phase A)

- [ ] `parity/cases.json` runs in Go and Python CI; Node.js/.NET entries carry
      `skip:phase-b` / `xfail:phase-c`; zero `xfail` left for Go/Python
      ([ADR-008](../decisions/ADR-008-shared-parity-fixture.md)).
- [ ] Every R-* statement has ≥ 1 fixture case passing in Go and Python.
- [ ] Every G-*/P-* has a regression test that **failed before** the fix
      (CI log or PR note shows the failing run).
- [ ] End-to-end: Go and Python examples export traces/metrics/logs to a real
      Collector via `docker compose` over grpc and http/protobuf, with a path
      prefix, a per-signal endpoint and a custom CA; the Collector debug
      exporter shows every signal.
- [ ] Coverage ≥ 90% in both languages.
- [ ] Each task PR links its validator reports (observability, dev,
      code-review) with **zero open BLOCKING/HIGH** findings.
- [ ] A13 checklist complete (docs, MkDocs, custom-signal examples).
