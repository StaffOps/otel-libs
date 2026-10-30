# Design — Config Contract & Parity (`0.3.0`)

> **Corrected 2026-10-27 after adversarial review.** D1.b originally sent
> portless gRPC URLs to the scheme default "80"; both SDKs actually dial
> **443** (grpc-go dns resolver `defaultPort = "443"`), so the decision was
> reversed: the helper keeps 4317 for gRPC. D5's Python mechanism
> (`excluded_urls`) would have dropped health requests from metrics too; it is
> replaced by a sampler. D2's snapshot asserted resolved URLs the helper no
> longer holds (contradicting ADR-008); URLs and TLS are now asserted only on
> the wire. D4's reliance on the Python SDK fork detector was wrong (it
> overwrites env-provided ids and doesn't exist at the 1.42 floor).
>
> **Second correction, same day — best-practice validation** (observability
> research against spec/docs/distros + dev prototypes on sdk 1.42/1.45 and
> otel-go v1.47): schemeless gRPC endpoints are legal per spec and are no
> longer deprecated; TLS conflicts are now normalized by the helper (scheme
> wins, per spec) instead of only warned, because both SDKs violate the spec
> rule; the Python SDK ≥ 1.45 generates `service.instance.id` itself, so the
> helper only fills the gap on 1.42–1.44; the `-<pid>` suffix was an
> invention and is dropped.

Guiding rule ([ADR-001](../decisions/ADR-001-defer-otlp-env-to-sdk.md)):
**if the SDK already implements a spec behavior correctly and identically in
our languages, the helper must not re-implement or override it.** Where an
SDK breaks a supported input or the SDKs disagree, the helper owns that case
through a small, fixture-covered shim.

References: [SDK env vars](https://opentelemetry.io/docs/specs/otel/configuration/sdk-environment-variables/),
[OTLP exporter](https://opentelemetry.io/docs/specs/otel/protocol/exporter/),
resource semconv (`service.*`, `deployment.*`, `db.*`), W3C Trace Context.

---

## D1 — Resolution contract (the table every language implements)

Precedence: **explicit code > standard `OTEL_*` env > legacy / `OTEL_HELPER_*` env > helper default**.

| Setting | Precedence (high → low) | Owner |
|---|---|---|
| `service.name` | code > `OTEL_SERVICE_NAME` > `OTEL_RESOURCE_ATTRIBUTES` > `SERVICE_NAME` (deprecated) > SDK default `unknown_service:*` + warning | helper sets only what it resolved ([ADR-003](../decisions/ADR-003-environment-unset-not-local.md)) |
| `deployment.environment.name` | code > `OTEL_RESOURCE_ATTRIBUTES` > `ENVIRONMENT` (deprecated alias) > unset + warning | helper (ADR-003) |
| Env-driven behavior (log level) | only `LOCAL/DEV/HML/PRD` switch behavior; unset/unknown → INFO; unknown values emitted verbatim | helper |
| `service.instance.id` | code > `OTEL_RESOURCE_ATTRIBUTES` > SDK detector (Python ≥ 1.45) > helper UUID per process (Go; Python 1.42–1.44) (D4) | SDK / helper |
| Other resource attrs | code per key > `OTEL_RESOURCE_ATTRIBUTES`; never overwritten by a helper default | SDK + helper merge |
| "OTLP configured" | **per signal**: code endpoint ∨ generic env ∨ that signal's env endpoint | helper |
| Endpoint (absolute `http(s)://` URL in env) | `_<SIGNAL>_ENDPOINT` verbatim > generic (`/v1/<signal>` appended for HTTP) | **SDK** — helper passes nothing (except D1.b) |
| Endpoint in code | generic: helper appends `/v1/<signal>` for HTTP, then passes the URL (Go `WithEndpointURL`); per-signal: verbatim | helper |
| Schemeless `host:port` (env or code) | rewritten to `https://` (`http://` only if `INSECURE=true`), port defaulted 4317/4318, passed explicitly. gRPC: supported (legal per spec). HTTP: deprecation warning | helper shim |
| Port, URL with scheme and none | `http/protobuf`: scheme default 443/80 (SDK). `grpc`: **4317** + warning (D1.b) | SDK / helper |
| Protocol | code > `_<SIGNAL>_PROTOCOL` > `OTEL_EXPORTER_OTLP_PROTOCOL` > port-4318 inference (INFO once; uses the endpoint that signal actually resolves to — per-signal if set, else generic) > `grpc` | helper picks exporter package |
| TLS | scheme decides; `INSECURE` only for schemeless (spec). On conflict the helper normalizes — scheme wins — and warns (D1.c) | SDK / helper on conflict |
| Certificates, headers | `OTEL_EXPORTER_OTLP_*` incl. per-signal | **SDK** — passed only when set in code |
| Timeout | code > `OTEL_EXPORTER_OTLP_<SIGNAL>_TIMEOUT` > `OTEL_EXPORTER_OTLP_TIMEOUT` (**milliseconds**, spec) > SDK default 10 s | Helper-parsed in both (Go passes the resolved value to the exporter and batch processors; Python shim D1.e). *Corrected 2026-10-28 (A13): earlier text said "Go: SDK", superseded by the edge-case rulings below* |
| Compression | code > env > helper default `gzip` (D1.d) | helper default |
| Sampler | code > `OTEL_TRACES_SAMPLER`/`_ARG` > `OTEL_HELPER_SAMPLE_RATIO` → `parentbased_traceidratio` (deprecated) > SDK default `parentbased_always_on`; health drop wrapper (D5) on top | SDK + helper ([ADR-002](../decisions/ADR-002-parentbased-sampler.md)) |
| Signal on/off | `OTEL_SDK_DISABLED`, `OTEL_{TRACES,METRICS,LOGS}_EXPORTER=none` > `OTEL_HELPER_DISABLED_SIGNALS` (deprecated) | helper |
| Metrics exporters / interval / exemplars | `OTEL_METRICS_EXPORTER` (existing contract + `console`; env `otlp` without endpoint warns — R2), `OTEL_METRIC_EXPORT_INTERVAL` (default 30000), `OTEL_METRICS_EXEMPLAR_FILTER` (never set in code) | helper / SDK |
| Propagators | `OTEL_PROPAGATORS` > `tracecontext,baggage`. Supported values in every language: `tracecontext`, `baggage`, `none`; any other value (`b3`, `jaeger`, `xray`, `ottrace`, …) warns and is skipped — no extra propagator dependencies (decided 2026-10-28, A3) | helper |

**Parsing rule:** unknown/unparseable **env** values warn and fall back to the
default; invalid **code** values fail fast. Warnings name the variable and
never print `*_HEADERS`, `*_CLIENT_KEY` or URL userinfo/query values.

**When wrong** (added 2026-10-28, spec lint): an OTel SDK starts honoring a
row itself (ADR-001's trigger) or a consumer needs env to override code.
**Reversal action**: move that row's owner to the SDK (ADR-001) and drop the
helper parsing in Go and Python together, with a parity case flip; for an
env-over-code need, add an explicit opt-in option rather than inverting the
order.

### D1 edge-case rulings (A3 validation, 2026-10-28)

The A3 review (observability + dev + two code-reviews) found inputs the table
didn't pin down, where Go and Python diverged. One rule each, both languages:

| Input | Rule | Why |
|---|---|---|
| Boolean env value that isn't `true`/`false` (`INSECURE=yes`, `OTEL_SDK_DISABLED=maybe`) | Case-insensitive `true` → true; **anything else → false**, with a warning. Never "ignored / fall through" | Spec boolean parsing; Python's "ignore" made `http://` + `INSECURE=yes` dial TLS (observability H3) |
| `OTEL_PROPAGATORS` casing / empty tokens (`TraceContext`, `tracecontext,,baggage`, trailing comma, `""`) | Tokens are trimmed and lowercased; empty tokens dropped; Python hides the raw value from the API whenever it differs from the canonical form (not only when something was skipped) | Spec enum values are case-insensitive; Python crashed at import (observability H1, code-review H2) |
| gRPC + `http://` + `OTEL_EXPORTER_OTLP[_<SIGNAL>]_CERTIFICATE`/`CLIENT_*` | Plaintext (scheme wins). Go passes `WithTLSCredentials(insecure.NewCredentials())` for that signal; warn | Go SDK prioritizes env credentials over `Insecure` (observability H2, Go code-review H1) |
| Env compression | Helper resolves it in both languages: case-insensitive `gzip` / `none`; any other value warns and the default (`gzip`) is used. `deflate` is not supported | Spec defines `gzip`/`none`; Go deferred to the SDK (case-sensitive), Python accepted `deflate` (observability M1) |
| `OTEL_SDK_DISABLED=true` | Short-circuit setup in both: no-op providers, no exporters, no Prometheus listener, no log handlers; propagators still installed | Python kept side effects (observability M2) |
| Invalid per-signal env value (endpoint, timeout) with a valid generic one | Warn and fall back to the **generic** value, then the default | "Warn and fall back" means the next level, as the SDK does for timeouts (observability L4, Go code-review M3) |
| Env endpoint with an unsupported scheme (`unix://`, `ftp://`) | Warn; the signal is **not configured** (no exporter) — also when it is a per-signal var and a valid generic endpoint exists (an explicit per-signal choice is not overridden; unparseable per-signal URLs, by contrast, fall back) | Python behavior; consistent with code values being rejected (Go code-review M2) |
| URL userinfo or query in any endpoint (env or code, both protocols) | Not supported: the helper strips them before the SDK sees the URL and warns (redacted). Auth belongs in `OTEL_EXPORTER_OTLP_HEADERS` | Go SDK drops them anyway; same input must not behave differently depending on whether a shim fires (Python code-review M2, observability L1) |
| Code `insecure` contradicting a code URL's scheme | Fail fast in both languages, error names the option | Parsing rule (code-invalid fails fast); Go already did (Python code-review M1) |
| Code values out of range (`sample_ratio` ∉ [0,1] or NaN, metrics port ∉ [0,65535], unknown disabled-signal names, wrong types) | Fail fast with an error naming the option | Parsing rule (both code-reviews M3/M4) |
| Code timeout + garbage per-signal env timeout (Python) | Mask the garbage per-signal var during exporter construction regardless of where the timeout came from | Python SDK `float()`s it unconditionally (Python code-review H1) |
| Env timeout garbage / `0` / negative (generic or per-signal) | Helper parses it in both languages (milliseconds): warn and fall back (per-signal → generic → 10 000 ms) | Go passed it to the SDK: garbage → only an SDK error; `0`/negative → ~30 s BatchSpanProcessor deadline, no warning (dev M4) |
| `OTEL_EXPORTER_OTLP[_<SIGNAL>]_HEADERS` with a malformed item (`a=1,broken`, empty item) | Helper parses headers in both languages (W3C-baggage-style, percent-decoded): malformed items are skipped with a warning naming the **key position only, never values**; valid pairs are passed explicitly to every signal | Go log SDK drops *all* headers on one bad item (auth header lost for logs only) while traces/metrics keep them (dev L7) |
| Header parsing details (round 2, amended after dev round 2) | Keys must match `^[A-Za-z0-9._-]+$` — the intersection of HTTP tokens and gRPC metadata keys (others: warn by position, skip); keys compared case-insensitively, **last one wins** (one value per key on both protocols); keys are NOT percent-decoded, values are; an invalid `%` escape anywhere in a value — or a decode that isn't valid UTF-8 — keeps the **whole value literal** (no partial decoding) **and warns** (position only); a decoded value containing a control character (byte < 0x20 or 0x7F, e.g. `%0D%0A`) is **skipped** with a warning (it would fail every export); trim after decoding; when no valid item remains, Python still masks the env var during exporter construction so the SDK never re-parses and logs it | Go/Python diverged on all five points; Go dropped an auth value containing `%` (A3 round-2 observability M2) |
| Fallback order for every per-signal value (endpoint, timeout, compression, headers) | Invalid per-signal → warn → **generic** → default; when a per-signal env endpoint is skipped, the helper passes the generic URL explicitly (the SDK would otherwise still read the bad per-signal var) | Go returned the default on the first invalid compression and let the SDK read a rejected `http://:4317` (round-2 H2, M1) |
| Opaque-scheme values without `//` (`unix:/tmp/x`, `ftp:/x`, `collector:/v1/traces`) | A raw value with no `://` matching `^[A-Za-z][A-Za-z0-9+.-]*:/` is an **unsupported scheme** (warn, signal not configured) in both languages — schemeless endpoints must be `host[:port]` with a numeric port, no path | Python parsed `unix:/tmp/x` as host `unix` and dialed `https://unix:4317`; Go rejected it (A3 final code-review P2-a) |
| Trimmed character set | Exactly ` \t\n\r\v\f` in both languages (not Unicode whitespace / `\x1c`–`\x1f`) | Python `str.strip()` and Go `TrimSpace` differ (P3-a) |
| Effective export deadline | The resolved timeout must actually bound the export: Go also passes it to the batch span processor / log batch processor / periodic metric reader (their own export timeouts otherwise cap it at 30 s); Python honours it already (A3 round-3 dev D4) *Superseded 2026-10-29 (A18) by the row "Export deadline": the timeout is always passed, also by Python, and the processor/reader timeout variables are not honored* |
| IPv6 zone IDs over `http/protobuf` in Python | Documented limitation: urllib3 cannot resolve `::1%25lo`; the helper warns when an http/protobuf endpoint carries a zone id (A3 round-3 dev D3) |
| Timeout range / syntax | Plain decimal milliseconds only, matching `^[0-9]+(\.[0-9]+)?$` after trimming (no sign, exponent, hex, underscores or bare dot); anything else warns and falls back; valid values clamp to [1 ms, 24 h] with a warning. Fixture cases assert the **resolved value**, not only the warning | `1e30` overflowed Go's Duration (negative on amd64); `0x1p10` accepted by Go only (round-2 L1/L2) |
| Unparseable endpoint in diagnostics | Print a constant (`<unparseable URL>`), never the raw value or the parser's exception text | Python printed the password when it contained `/`, `?` or `#` (round-2 H1) |
| Endpoint env value with surrounding whitespace / control characters | Surrounding whitespace is trimmed and the trimmed URL is passed explicitly (never left for the SDK to re-read raw). A control character **inside** the value (byte < 0x20 or 0x7F after trimming) makes it unparseable: warn `<unparseable URL>`, fall back per the order rule (Python's `urlsplit` silently strips `\t\r\n` — the helper must check first; A3 round-3 dev D2) | Go lost all logs on a trailing `\n` (dev round-2 H) |
| Go endpoint ownership (round 2) | For every configured signal the Go helper passes the fully resolved URL explicitly (`WithEndpointURL`) — never leaves an env endpoint for the SDK to re-read | Every round-2 Go HIGH (rejected per-signal fallback, per-signal schemeless regression, trailing newline) had the same root cause: the SDK re-reads raw env and disagrees with the helper (ADR-001 second correction) |
| SDK's own log output of env values (Go `global.Error` for headers/URLs) | Out of the helper's control in Go (G7 forbids masking env); documented limitation in HOW-TO and CHANGELOG. Python masks the affected vars during exporter construction | Go/Python code-review round 2 *Extended 2026-10-29 (A18): the Go SDK also reads the TLS file variables at exporter `New()` and prints the **file path** in its own errors (`otlp*/internal/envconfig/envconfig.go:140`, `read tls ca cert file … file=<path>`; the log exporter: `failed to load TLS: open <path>`), even though the helper then passes its own TLS configuration — a documented Go limitation. Python and Node mask the TLS variables while the exporters are built, so their SDKs never read or print them* |
| Whitespace-only `OTEL_SERVICE_NAME` / `SERVICE_NAME` | Trimmed; whitespace-only = unset | Python used `"  "` as the name (dev L8) |
| Pre-fork setup on Python 1.42–1.44 (`--preload`) | `register_at_fork` child hook only **sets a flag** (never logs inside the hook — the OTel log pipeline may hold the parent's locks); the warning is emitted on the child's next helper call | Shared helper UUID would go unnoticed (observability M3) |
| Code option equal to the helper default (A13 validation, 2026-10-28): `WithSampleRatio(1.0)` / `sample_ratio=1.0`, `WithPrometheusMetricsPort(9464)` / `prometheus_metrics_port=9464`, `WithDisabledMetrics([])` / `disabled_metrics=[]`, `disabled_signals=[]`, `debug_level=False`, `extra_instrumentation="SQL"`, `minimum_log_level=0` (Python) | **Set in code wins, whatever the value.** "Was it set" is tracked explicitly — Go: an option flag set by the `With*` option (`WithoutMetricsListener` counts as setting the port; a **nil** slice passed to `WithDisabledMetrics`/`WithDisabledSignals` means "not set", like Python's `None`); Python: the field defaults to `None` and **only ever holds the code value** — resolution stores the effective value privately (`TelemetryOptions.resolved`), so a value assigned after construction counts as code, `dataclasses.replace()` of a resolved object and a second `setup_telemetry()` with the same object re-read the environment for every field code left unset, except `service_name` and `environment`, which are still written back in place (pre-existing; moving them to `resolved` is a follow-up API change) (A13 review H1/M1) — never by comparing the value to the default. Python code types (A13 review M3): `disabled_signals`/`disabled_metrics` accept a list or tuple of strings (normalized to a list), `debug_level` a bool or int (truthiness, as in 0.2); any other type fails fast naming the option. A code sample ratio (any value, 1.0 included) selects `ParentBased(TraceIDRatioBased(ratio))` and beats `OTEL_TRACES_SAMPLER` and `OTEL_HELPER_SAMPLE_RATIO`. An explicit empty list (`disabled_metrics`, `disabled_signals`) means "none" and beats the env list. Empty strings stay "not set" for string options (endpoints, protocol, compression): an empty endpoint is not a usable value, as already documented on each option. Go has no code-side "debug off" (`WithDebug()` only enables), so the `debug_level=False` row is Python-only API, not a parity gap | D1 "explicit code > env"; a value-equals-default check silently handed the decision back to the env (`go/config.go`, `go/tracing.go` buildSampler `< 1.0`, `python/otel_helper/config.py` resolve_from_env) |
| Code identity option vs the same key in code `resource_attributes` (Node B3 review, 2026-10-29): `service_name='a'` + `resource_attributes={'service.name':'b'}`; same for `environment` / `deployment.environment.name` | **The dedicated option wins**, in the resolved value **and** in the emitted resource (`a`) | Go and Python reported `a` but emitted `b` (Go `otelhelper.go` appends the option before the attribute loop; Python `setup.py` `update()`s attributes over the override) — the resolved value and the wire disagreed. Fix in Go/Python: task A16 |
| Blank identity value inside `OTEL_RESOURCE_ATTRIBUTES` or code `resource_attributes` (`service.name=`, `deployment.environment.name=`, `service.instance.id=` — empty or contract-trim-set only) (Node B3 review, 2026-10-29) | **Counts as not provided**: the next precedence level applies (`service.name` → … → SDK default + warning; environment → absent + ADR-003 warning; instance id → generated) | Go emitted `service.name=""` and warned `using=""`; Go/Python emitted `deployment.environment.name=""` and kept a blank instance id. A16 |
| Code-only `resource_attributes['service.name']` (no option, no env) (Node B3 review, 2026-10-29) | Resolves to that value, **no** "no service name configured" warning | Go resolved `unknown_service`, warned, and emitted the code value. A16 |
| `OTEL_RESOURCE_ATTRIBUTES` parsing (Node B3 review, 2026-10-29) | **Helper-owned in every language** (not the SDK detector): split on `,`; blank items ignored; per item split at the first `=`; key and value trimmed with the contract trim set **after** percent-decoding; an item without `=`, with an empty key or with a key that is not valid UTF-8 is skipped with a warning naming the **item position only**; an invalid percent sequence or invalid UTF-8 keeps that value literal (still trimmed with the contract set) with a positional warning; a blank item is ignored **without** a warning; values are never echoed (R8) | The SDK parsers echo raw invalid items (Go `otel.Handle`, Python SDK warning — an R8 leak when values carry secrets), use Unicode trimming, and disagree on bad `%` and empty keys. The Node **helper** already implements this rule (the Node SDK's env detector instead discards the whole variable); Go/Python: A16 (parity cases cover each sub-rule). *Rationale, 2026-10-29 (A16):* the OTel SDK environment-variable spec says an SDK SHOULD discard the whole variable when it cannot be parsed; the helper deliberately keeps the valid items, because dropping them silently loses `service.name` and `deployment.environment.name` — wrong service and wrong log level in production — while one malformed item is already reported loudly. Raw invalid bytes are replaced with U+FFFD one per byte (Go and Python agree; Node sees an already-decoded environment, so a non-UTF-8 key cannot be detected there). *Reverse when* the spec makes the discard a MUST, or backends start relying on all-or-nothing resource parsing |
| `telemetry.sdk.*` and the resource schema URL (A16, 2026-10-29) | **`telemetry.sdk.name`/`.language`/`.version` always come from the SDK**: a value for them in `OTEL_RESOURCE_ATTRIBUTES` (silently) or code `resource_attributes` (one warning per key, once per process, naming the key only) is ignored; other `telemetry.*` keys, e.g. `telemetry.distro.*`, are kept. The resource **declares the schema URL of the semconv version the helper targets**, one literal in every language: `https://opentelemetry.io/schemas/1.43.0` (Go `semconv/v1.43.0`, a test asserts the package matches; Python asserts it against `Schemas.V1_43_0` when the installed semconv package has it — on SDK 1.42–1.44 the literal is newer than that package, by design); bumping it is a contract change. Layering, lowest to highest: env parse < code attributes < resolved identity < `telemetry.sdk.*`. Parity cases `resource_attributes.telemetry_sdk_{env,code}_ignored`; the schema URL is asserted by unit tests (the fixture's `resolve` snapshot carries attributes only) | Go: the SDK won (telemetry detector last) and Go declared 1.43.0; Python and Node let env/code override them and declared no schema. Chosen direction: the SDK identifies itself — an app overriding `telemetry.sdk.*` would make backend SDK-version views lie, and Go already behaved this way, so only two languages move. Node D-JS14 row removed |
| Go: SDK providers re-read `OTEL_RESOURCE_ATTRIBUTES` (A16, 2026-10-29) | `sdktrace`/`sdkmetric`/`sdklog.WithResource` do `resource.Merge(resource.Environment(), r)`: the SDK parses the variable again under the helper's resource. **Echo: fixed by the helper.** When its own parse found something the SDK parser reports (an item without `=`, a blank item included, or a bad escape), Setup installs a scoped `otel.ErrorHandler` filter while it builds the three providers: it drops only `errors.Is(err, resource.ErrPartialResource)` and a bare `url.EscapeError` (type assertion — an exporter's wrapping `*url.Error` passes) and forwards everything else to the handler installed before (or, when that is the API's default delegator or a filter, to `log.Print`, avoiding the delegator recursion); afterwards the previous handler is restored unless the application installed another meanwhile. Race window (microseconds): another goroutine's partial-resource error is dropped too. **Comeback: documented Go limitation.** A key the helper leaves absent comes back from the SDK parse (blank `deployment.environment.name=` → `""`), and a key with surrounding Unicode whitespace appears twice (helper: verbatim key; SDK: `strings.TrimSpace`). Only masking the env (G7 forbids it) or an exporter wrapper (rejected: rewrites every signal's resource) would fix it. Go `skip:no-upstream-go-resource-env-merge` on `identity.blank_env_environment_absent` and `resource_attributes.unicode_space_key_verbatim`; `resource_attributes.values_never_echoed` passes | `sdk/trace/provider.go:390`, `sdk/metric/config.go:115`, `sdk/log/provider.go:301` (v1.47.0). *Upstream issue draft:* "`WithResource` merges `resource.Environment()` under a caller-supplied resource, so a caller that builds its resource from `OTEL_RESOURCE_ATTRIBUTES` itself cannot drop or normalize keys, and malformed items are reported through `otel.Handle` with their raw text. Proposal: an option (e.g. `WithResource(r, resource.WithoutEnvironment())`) or not merging the env when the caller passes a resource." |
| Python: SDK parse echo, resource detectors, after-fork refresh (A16, 2026-10-29) | The helper builds the resource itself (`Resource(attrs, schema_url)`, never `Resource.create()`). **SDK echo:** `opentelemetry.sdk._logs` calls `Resource.create({})` at import (a log-record class default), which runs the SDK env detector; a process-local `logging.Filter` on the `opentelemetry.sdk.resources` logger drops only its "Invalid key value resource attribute pair" record while the helper imports that module and while it sets up — the environment is never mutated. **Detectors:** see the detector-selection row. **Default service name:** helper-computed `unknown_service:<basename(sys.executable)>` (the ≥ 1.45 spelling) on every SDK version. **After fork (sdk ≥ 1.45):** the SDK's fork hook merges its process-dependent detectors on top of the resource, which replaced an explicit `service.instance.id` (pre-A16) and could re-add detector attributes; the helper registers its own `after_in_child` hook after the providers' (hooks run in registration order) that puts the helper's resource back, with the SDK's fresh per-process id unless code or env set one (private `_resource` / `_sdk_config.resource` + `_update_resource`, guarded by `hasattr`; the hook never logs or raises) | `opentelemetry/sdk/_logs/_internal/__init__.py:262` (1.42) / `:281` (1.45); `sdk/trace/__init__.py` `_handle_fork` (1.45). An app that imports the SDK log module before the helper still gets the SDK warning (out of the helper's control) |
| Detector-selection variables (A16, 2026-10-29) | **Not honored in any language**: the helper builds the resource itself (principle 4: host/process/cloud attributes come from the Collector). A warning, once per process and naming only the variable, when it lists anything beyond the env and service-instance equivalents — Python `OTEL_EXPERIMENTAL_RESOURCE_DETECTORS` beyond `otel`/`service_instance`; Node `OTEL_NODE_RESOURCE_DETECTORS` beyond `env`/`serviceinstance`/`none`. Go: no warning (`OTEL_GO_X_RESOURCE` only gates the SDK's instance-id detector, and the helper generates the id itself). Parity case `resource.detector_var_ignored_warned` (Go `skip:no-detector-variable`) | Python honored the variable through `Resource.create()` before A16 (breaking, migration item 19); Node never ran detectors (D-JS4) |
| Identity and resource-attribute warning cadence (A16, 2026-10-29) | **Once per process in every language**: "no service name configured", the ADR-003 environment warning, the two `OTEL_RESOURCE_ATTRIBUTES` parse warnings and the code `telemetry.sdk.*` warning — like the deprecations (D8). A later setup with the same problem stays silent | Node and Go wrote them on every setup, Python once per options object (Node D-JS14 row, removed) |
| Invalid code `resource_attributes` (A16, 2026-10-29) | An empty or contract-trim-set-only key fails setup in every language (Go: `Setup` error naming `WithResourceAttributes`; Python `ValueError`; Node `TypeError`). Python only: a non-string value for `service.name`, `deployment.environment.name` or `service.instance.id` fails with `ValueError` naming the key (Go's map is typed; Node already rejects non-strings). Other non-string Python values (int, float, bool, sequences) are passed to the SDK as before — a deliberate Python-only API extension: Go's option is `map[string]string` and Node's `resourceAttributes` accepts strings only (A16 review, 2026-10-29; typed attribute values in Go/Node would be a separate, additive API change). Parity case `resource_attributes.code_empty_key_fails` | R8 fail fast on code values |
| Unparseable `OTEL_EXPORTER_OTLP[_<SIGNAL>]_INSECURE` with no endpoint configured (Node B4a, 2026-10-29) | **Warn when the INSECURE variable that would be consulted for a signal is set and unparseable**, whether or not an endpoint is configured (env parse errors are loud, principle 8). "Would be consulted" = per-signal, else generic; none when code `insecure` is set (a shadowed variable stays silent — Go and Node agree). *Clarified 2026-10-29 (B4a code-review L5)* | Go warned (it resolves every signal); Python read INSECURE only for a chosen endpoint and stayed silent. A17 |
| Control character inside an opaque-scheme endpoint (`unix:/x\x7f`) (Node B4a, 2026-10-29) | The control-character ruling applies first: the value is **invalid** (warn `<unparseable URL>`) and resolution falls back to the next level — not "unsupported scheme" | Go checked control characters first; Python classified the opaque scheme first and left the signal unconfigured. A17 |
| Empty code `otlp_protocol` / `compression` (`""`) (Node B4a, 2026-10-29) | **Not set** (the A13 "empty strings stay not set" ruling) — env, then default apply; no error | Go and Node follow the ruling; Python raised `ValueError`. A17 |
| Endpoint port outside 1–65535 (`http://h:70000`) (Node B4a, 2026-10-29) | **Invalid**: warn `<unparseable URL>`, fall back to the next level (env) or fail fast (code) | WHATWG URL (Node) and Python reject it; Go's `url.Parse` accepted it. A17 *Corrected 2026-10-29 (A17 refutation):* the port is compared after stripping leading zeros; nothing left or more than five digits remaining is invalid, so a port of any length is parsed without converting it whole (Python `int()` refuses more than 4300 digits and raised `ValueError` out of the resolver). Parity case `endpoint.port_long_digits` |
| Text used for an endpoint that can't be parsed (`http://:4317`, control characters, bad port) (Node B4a, 2026-10-29) | Always the constant `<unparseable URL>` in every language (never a partial or host-less rendering) | Go printed the host-less URL. A17 |
| Endpoint parsing follows RFC 3986, not WHATWG (Node B4a refutation, 2026-10-29) | The authority ends only at `/`, `?` or `#`; a **backslash** anywhere in the endpoint makes it **invalid** (`<unparseable URL>`, fall back / fail fast); a URL scheme must be followed by `//` and a **non-empty host**; a schemeless value starting with `/` or `\` is invalid; userinfo is cut at the **last** `@` of the RFC authority | Node's WHATWG parser treated `\` as an authority end: `http://user:12\pw@collector` leaked the password to diagnostics and onto the wire as a path (BLOCKING); `http:///x` and `//h:4317` resolved silently. Go already invalid; Python accepted some forms. A17 (Python), Node B4a |
| Leading-zero ports (`http://h:04318`) (Node B4a refutation, 2026-10-29) | Compared **numerically** (`04318` = 4318): port-4318 inference applies; the rendered URL uses the numeric port | Go compared the string and dialed gRPC. A17 (Go) |
| Percent-encoding in the endpoint path (`/%2F`, `/%zz`) (Node B4a refutation, 2026-10-29) | The path is kept **verbatim** (escaped form), never decoded; *(withdrawn 2026-10-29)* ~~an invalid escape in the path is kept as written, not an error~~ | Go decoded `%2F` (losing the segment) and rejected `%zz`. A17 (Go) *Corrected 2026-10-29 (A17 refutation):* "an invalid escape in the path is kept as written" is **withdrawn**. An invalid `%` escape (a `%` not followed by two hex digits) in an **http/protobuf** path is **invalid in every language**: env → warn `<unparseable URL>`, fall back; code → fail fast (gRPC drops the path, so it stays irrelevant there). Evidence: the Go OTLP/HTTP exporters `url.Parse` the endpoint in `WithEndpointURL` and on error `global.Error` the full URL and keep their **default endpoint** (otlptracehttp v1.47.0 `internal/otlpconfig/options.go:296-305`); Python `requests.utils.requote_uri` falls back to `quote(uri, safe=…without %)` once one escape is invalid, double-encoding **every** escape of the URL. Parity cases `endpoint.http_path_invalid_escape`, `config.code_http_path_invalid_escape_fails`. Go: see the row "Go: path escapes decoded" *Amended 2026-10-29 (A17 final review):* diagnostics render such an http/protobuf value as `<unparseable URL>` too (`redactURL`/`redact_url` take the protocol; without one, no escape check). |
| Host characters (Node B4a refutation, 2026-10-29) | An unescaped IPv6 zone id, a space, or a percent-encoded byte in the **host** makes the endpoint **invalid** | Python accepted zone ids and spaces; Node decoded `%68` to `h`; Python passed `%68` raw. A17 (Python), Node B4a |
| Empty query or fragment (`http://h:4317/?`, `#`) (Node B4a refutation, 2026-10-29) | Treated like any query/fragment: removed, with the userinfo/query warning (once per variable) | Python left `?` to the SDK (`…/?/v1/traces`). A17 (Python) |
| `@` in the endpoint path or query (`http://user:4317/secret@collector`) (Node B4a refutation, 2026-10-29) | **Warn** ("endpoint path contains '@' — misplaced credentials?") naming the variable only, never the value; the endpoint is used as parsed | All three silently sent `…/secret@collector…` as the request path. A17 (Go, Python), Node B4a *Amended 2026-10-29 (A17 refutation):* a percent-encoded `%40` counts as `@`, for the warning and for diagnostics (the path is not rendered) |
| Userinfo/query warning dedupe (Node B4a refutation, 2026-10-29) | Once per **variable and value** per setup (not per signal) | Node warned once per signal for a generic http/protobuf endpoint |
| Internationalized (IDN) hosts (`http://bücher.de:4317`) (Node B4a re-refutation, 2026-10-29) | The host is converted to **punycode** (IDNA/UTS-46, `xn--bcher-kva.de`) before dialing and in every rendering; a host that fails IDNA conversion is **invalid** | Node (WHATWG) already punycodes; Go rendered `b%C3%BCcher.de`, Python passed raw unicode. A17 (Go, Python) *Corrected 2026-10-29 (A17 refutation):* "IDNA/UTS-46" means **UTS #46 non-transitional** in every language (Go: `x/net/idna` profile with WHATWG options; Node: `url.domainToASCII`; Python: the `idna` package, `idna.encode(host, uts46=True, transitional=False)`, a core dependency `idna>=3.7,<4` — 3.7 fixes CVE-2024-3651; the stdlib codec is IDNA 2003 and mapped `ß` to `ss`). Deviation characters are kept (`straße.de` → `xn--strae-oqa.de`); a ZWJ/ZWNJ outside its CONTEXTJ context is invalid. IDNA runs on non-ASCII hosts only. Parity cases `endpoint.idn_deviation_char`, `endpoint.idn_zwj_invalid` |
| IDN hosts: Python residual divergence (A17 refutation, 2026-10-29) | **Recorded, not fixed.** The `idna` package validates labels by IDNA 2008 after the UTS #46 mapping, so it rejects some hosts WHATWG (Node) and the Go profile accept: emoji and other non-PVALID code points, `_` in a label that also holds non-ASCII text, labels longer than 63 characters, hyphens in the third and fourth position of a non-`xn--` label. Python treats them as invalid (warn `<unparseable URL>`, fall back / fail fast). No parity case on these hosts (they would fix the divergence in the fixture) | `idna` has no WHATWG profile (no `CheckHyphens=false`, no IDNA 2008 relaxation). *Reverse when* `idna` gains a WHATWG mode, or Go/Node move to IDNA 2008 validation *Corrected 2026-10-29 (A17 final review):* an **empty label** in a non-ASCII host and a non-ASCII host **longer than 253 characters** after punycode (one trailing `.` ignored) are now invalid in Go and Node too, matching Python's `idna`. What remains Python-only: emoji and other non-PVALID code points, `_` in a label that also holds non-ASCII text, labels over 63 characters, hyphens in the third and fourth position (recorded in the README gap list). |
| One host rule (A17 refutation, 2026-10-29) | After the host-characters and IDN rows, the (ASCII) host must be an **IPv6 literal** (`[…]`, `net.ParseIP` / `ipaddress` / `net.isIPv6`) or an **RFC 3986 reg-name** (unreserved / sub-delims bytes only, so `a{b`, `"h"`, `h{` are invalid). A host that **ends in a number** (WHATWG term: its last label, a trailing empty label ignored, is decimal digits or `0x` + hex) must be a **canonical dotted-quad IPv4 address**, each octet 0–255 without leading zeros; otherwise it is invalid: `127.1`, `0x7f.0.0.1`, `2130706433`, `1.2.3.256`, `1.2.3.4.5`, `01.2.3.4` (`1.2.3.4.` was listed here; accepted since the correction below). Hosts are lowercased (IPv6 too, kept as written otherwise). Node implements the same rule itself instead of comparing with WHATWG `URL` output | Node normalized `127.1` to `127.0.0.1` (an address) while Go/Python passed it to the resolver as a DNS name, and Node accepted `{` and `"`, which Go/Python rejected — the same endpoint dialed different targets. Parity cases `endpoint.ipv4_shorthand_invalid`, `endpoint.ipv4_octet_out_of_range_invalid`, `endpoint.host_non_regname_invalid` *Corrected 2026-10-29 (A17 final review):* **one** trailing `.` is dropped before the canonical-quad check, so `10.0.0.1.` (and `1.2.3.4.`) is accepted, rendered as written; `1.2.3.4..` does not end in a number (WHATWG: last label empty after dropping one) and is a reg-name. Node runs `domainToASCII` on the host plus a guard label (`.x`, stripped afterwards): WHATWG `domainToASCII` also runs IPv4 parsing on its result, which turned fullwidth `１２７.１`, `２１３０７０６４３３` and `０１.２.３.４` into valid addresses in Node only; with the guard the rule sees `127.1` as Go and Python do. Parity cases `endpoint.ipv4_fullwidth_shorthand_invalid` and the export twins `export.{ipv4_shorthand,ipv4_octet_out_of_range,host_non_regname,ipv4_fullwidth_shorthand}_falls_back` (assert the fallback traces endpoint; Node `skip:B4b-otlp-endpoint`). Kubernetes: an all-digit namespace makes `collector.2024` end in a number — write the FQDN or the `.svc` form. |
| Opaque-scheme rendering (A17 refutation, 2026-10-29) | An opaque value (`unix:/…`, `collector:/…`) renders as **`<scheme>:` only** (lowercased) in every diagnostic and error; the text after the scheme is never shown (it may hold credentials, and cutting at the last `@` still showed the rest). A schemeless value with a later `://` keeps its current handling | Parity case `redaction.opaque_scheme_renders_scheme_only` |
| Rendering when the path or query holds `@` (A17 final review, 2026-10-29) | **Every diagnostic and error renders only `<scheme>://`** — the userinfo/query warning (`value` and `using`), the portless-gRPC warning, an unsupported-scheme value; a schemeless value renders as the constant **`<schemeless endpoint>`** (also the schemeless deprecation `value`), in every language. `%40` counts as `@` | `http://admin:1234/rest@collector?x` rendered `http://admin:1234` — the user name and the start of the password (R8). Parity case `redaction.at_in_path_numeric_password_prefix` |
| Go: path escapes decoded (A17 refutation, 2026-10-29) | **Documented Go limitation.** The helper keeps the path verbatim in the resolved URL, but the Go OTLP/HTTP exporters send `url.URL.Path`, the decoded path: `/a%2Fb` reaches the Collector as `/a/b` (an escape of a character a path needs escaped, e.g. `%20`, survives the re-encoding). The helper warns, once per variable and naming only the variable, when the http/protobuf path has an escape the exporter will change ("endpoint path has percent-escapes the Go OTLP/HTTP exporter sends decoded"). `export.http_path_escape_verbatim` is Go `skip:no-upstream-go-http-raw-path` | otlptracehttp v1.47.0 `internal/otlpconfig/options.go:296-305` (`WithEndpointURL`: `cfg.Traces.URLPath = u.Path`) and `client.go:292` (`url.URL{Scheme, Host, Path: c.cfg.URLPath}`); otlpmetrichttp v1.47.0 `internal/oconf/options.go:310-313` and `client.go:88-92`; otlploghttp v0.23.0 `config.go:195-198` and `client.go:83-86`. `WithURLPath` does not help (the `%` is re-escaped to `%25`). `WithHTTPClient` is **rejected**: it replaces the exporter's TLS (`OTEL_EXPORTER_OTLP_*CERTIFICATE`/`CLIENT_KEY`), proxy and timeout handling, which the helper would have to re-implement (ADR-001). *Upstream issue text (not filed):* "**`WithEndpointURL` drops `url.URL.RawPath`.** `otlptracehttp`/`otlpmetrichttp`/`otlploghttp` `WithEndpointURL(u)` stores `u.Path` and the client rebuilds the request URL from it, so an endpoint whose path contains percent-escapes of characters that do not need escaping (e.g. `https://h/tenant%2Fa/v1/traces`) is sent decoded (`/tenant/a/v1/traces`). Expected: the escaped path is sent as configured. Proposal: keep `u.EscapedPath()` (or `RawPath`) in the config and build the request URL with `RawPath` set." *Reverse when* upstream keeps the raw path |
| TLS files (Node B4b review, 2026-10-29; task A18) | Applies to gRPC and http/protobuf with a TLS endpoint. `OTEL_EXPORTER_OTLP[_<SIGNAL>]_CERTIFICATE`, `_CLIENT_KEY` and `_CLIENT_CERTIFICATE` each resolve **per-signal, then generic**; a per-signal file that cannot be read (or, for the CA, holds no PEM certificate) warns and falls back to the generic file, then to the default (system roots / no client certificate). The client key and certificate are **one pair**: when only one resolves, either cannot be read, or the TLS layer rejects them (bad PEM, mismatch, encrypted key), the helper never fails setup and never aborts the process — it warns naming **the variables only** (never the path, the file content or key material) and that signal uses **server authentication with the resolved CA**. A CA that cannot be read or has no PEM certificate warns and the system roots are used. The helper resolves the files itself and passes the result explicitly: Go a `tls.Config` (`WithTLSCredentials` / `WithTLSClientConfig`), Python gRPC channel credentials or the HTTP file paths (the TLS variables masked while the exporters are built), Node gRPC channel credentials or HTTPS agent options (`httpAgentOptions`; variables masked) | Python aborted the process on an incomplete pair (grpc C core CHECK, `grpc_tls_certificate_distributor.cc:344/345`); Go ignored it silently; the Go log exporter dropped the whole TLS config (CA included) on one bad file; Python ignored the generic CA when a per-signal one was set. Cases `export.grpc_certificate_unreadable_falls_back`, `export.grpc_certificate_not_pem_falls_back` (helper warning added), `export.grpc_client_key_without_certificate_server_auth`, `export.grpc_client_pair_mismatch_server_auth`, `export.grpc_certificate_per_signal_bad_generic_good`. Go limitation: SDK path printing (row "SDK's own log output"). *Python limitation (A18 final round, 2026-10-29): the pair is validated with CPython `ssl` (OpenSSL) while grpc uses BoringSSL; a pair one accepts and the other rejects fails every handshake (no abort, no export) instead of falling back to server authentication* |
| Export deadline (Node B4b review, 2026-10-29; task A18) | The resolved OTLP timeout of a signal (code > `_<SIGNAL>_TIMEOUT` > `OTEL_EXPORTER_OTLP_TIMEOUT` > 10 000 ms) is its **single export deadline**: the helper always passes it to the exporter, the batch span processor, the batch log record processor and the periodic metric reader (the default 10 000 ms included). `OTEL_BSP_EXPORT_TIMEOUT`, `OTEL_BLRP_EXPORT_TIMEOUT` and `OTEL_METRIC_EXPORT_TIMEOUT` are **not honored**; when one is set the helper warns once pointing to `OTEL_EXPORTER_OTLP[_<SIGNAL>]_TIMEOUT`. Node caps the reader timeout at the export interval with a warning naming the timeout's source (the SDK requires timeout ≤ interval; Node design D-JS14) | Supersedes the 2026-10-29 contract follow-up (tasks.md). Its premise "Python lets the SDK read those variables" was wrong: the Python SDK 1.4x batch processors accept `export_timeout_millis` but do not use it (`_shared_internal/__init__.py`: "Not used. No way currently to pass timeout to export"), and only the periodic reader honored `OTEL_METRIC_EXPORT_TIMEOUT`; Go passed the timeout only when one was set, so the SDKs' own env reading (30 s) applied otherwise. Cases `export.batch_export_timeout_env_ignored_grpc`, `export.code_export_timeout_wins_grpc` |
| Code export timeout bound (Node B4b review, 2026-10-29; task A18) | A code export timeout outside **[1, 86 400 000] ms** (the env clamp range) fails fast in every language. Go `WithExportTimeout` and Python `export_timeout_ms` take integers; Node `exportTimeoutMs` accepts any finite number in the range (decimals, as the env grammar) | Go and Python accepted any positive value; Node timers overflow above 2^31-1 ms. Case `config.code_export_timeout_above_24h_fails` |
| Header bytes (Node B4b review, 2026-10-29; task A18) | A decoded header value must be **printable ASCII (0x20–0x7E)**; any other value (a control character, a decoded `%C3%A9`, a raw non-ASCII character) warns naming the variable and the item position in the env value, and the item is skipped — both protocols, every language. A `-bin` gRPC metadata key gets no special handling beyond that: the ASCII value is sent as its bytes (gRPC base64-encodes binary metadata on the wire). Go already did; Python now passes `bytes` for `-bin` keys (grpc requires them); Node passes a `Buffer` | grpc-go and Python grpc failed the **whole export** on a non-ASCII metadata value; Python failed it on a `str` value for a `-bin` key. Cases `export.headers_value_non_ascii_skipped`, `export.headers_bin_key_sent_as_bytes`; `export.per_signal_headers_replace_generic` pins the existing per-signal-replaces-generic rule |
| Value-length limits (Node B4b review, 2026-10-29; task A18) | A value-length limit of **0 is invalid** (`OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT`, `OTEL_SPAN_ATTRIBUTE_VALUE_LENGTH_LIMIT`, `OTEL_LOGRECORD_ATTRIBUTE_VALUE_LENGTH_LIMIT`): it warns once and the next level applies (per-signal → generic → no limit). The helper passes the limit explicitly only when a 0 is set (Go `WithRawSpanLimits` / log `WithAttributeValueLengthLimit`, Python `SpanLimits` / `LogRecordLimits`, Node the provider limits). *Batch size above queue size: ruled 2026-10-29, see the next row* | Before: Go and Python truncated every value to `""`; the JS SDK ignored 0 with a `diag.warn` per attribute. Go limitation: the Go log SDK reads only `OTEL_LOGRECORD_ATTRIBUTE_VALUE_LENGTH_LIMIT` (no generic fallback), so the generic variable affects Go logs only through the 0 rule. Python limitation: the SDK 1.42 `LoggerProvider` takes no limits (1.45 does: `log_record_limits`), so a 0 still empties log attribute values there. *Corrected 2026-10-29 (A18 final round): a non-zero per-signal value is kept when only the generic variable is 0 (Python passed the generic fallback over it).* Cases `config.attribute_value_length_limit_zero_warns`, `config.attribute_value_length_limit_zero_signal_wins` |
| Batch size above queue size (A18 final round, 2026-10-29) | When `OTEL_BSP_MAX_EXPORT_BATCH_SIZE` is larger than `OTEL_BSP_MAX_QUEUE_SIZE` (likewise `OTEL_BLRP_*`; defaults 512 / 2048), the helper warns **once naming both variables** and uses the **queue size** as the batch size, passing it explicitly (Go `WithMaxExportBatchSize` / `WithExportMaxBatchSize`, Python `max_export_batch_size`, Node the processor options). Setup never fails. Unparseable values are left to the SDKs | Python's SDK raised `ValueError` at processor construction and failed setup (`sdk/trace/export/__init__.py:288`); the Go span processor clamped silently to min(512, queue), the Go log processor to the queue; the JS SDK clamped with a `diag.warn`. Cases `config.bsp_batch_size_above_queue_clamped`, `config.blrp_batch_size_above_queue_clamped` |

### Decision D1.a: keep the port-4318 → `http/protobuf` inference

**Choice**: with no protocol configured and endpoint port exactly 4318, select
`http/protobuf` and log it once at INFO.

**Justification, strongest first**:
1. 4318 is the IANA-registered OTLP/HTTP port; gRPC on 4318 always fails, so
   the inference cannot pick a working config over a broken one.
2. Shipped in 0.2.0; consumers already rely on it.

**Trade-offs accepted**:
| Cost | Why acceptable |
|---|---|
| Not in the OTel spec | Only fires on an unambiguous port; logged |

**When this decision would be wrong**: a consumer runs gRPC on 4318 behind a proxy.
**Reversal action**: document `OTEL_EXPORTER_OTLP_PROTOCOL=grpc` (already wins).
**Alternatives rejected**: drop inference (breaks 0.2.0 users); default
`http/protobuf` as the spec suggests (breaks every in-cluster gRPC collector on
4317). Note: the Go SDK has no protocol default (chosen by package) and Python's
`otlp` exporter entry point maps to gRPC — the helper's `grpc` default matches
the current behavior of both, not "most SDKs" (unmeasured beyond these two).

### Decision D1.b: portless gRPC URLs keep 4317 (reversed)

> **Reversed 2026-10-27.** First version: "scheme default in 0.3.0, `http://otel-collector` → 80".
> Wrong on the fact (gRPC dials 443) and on the policy (silent breakage).

**Choice**: `http/protobuf` + URL without port → scheme default (443/80) via
the SDK. `grpc` + URL without port → the helper adds **:4317** and warns
"set an explicit port".

**Justification, strongest first**:
1. gRPC has no scheme-default port: both SDKs dial 443 regardless of scheme, so
   `http://otel-collector` (common in-cluster) would go to **443 plaintext** and
   fail — a silent loss of telemetry (principle 8).
2. 4317 is the IANA OTLP/gRPC port and today's behavior: no consumer breaks.
3. For HTTP the scheme default is the SDK's spec behavior and fixes the real
   bug (`https://vendor` → `:4317`).

**Trade-offs accepted**:
| Cost | Why acceptable |
|---|---|
| Another helper-owned shim against ADR-001's guiding rule | One line, fixture-covered, warned |
| gRPC behind a 443 ingress needs `:443` explicitly | Warning says so; explicit is correct |

**When this decision would be wrong**: consumers commonly point gRPC at
`https://<ingress>` expecting 443.
**Reversal action**: drop the shim (SDK behavior = 443), keep the warning.
**Alternatives rejected**: scheme default for gRPC (443 plaintext breakage);
warn in 0.3 / flip in 0.4 (no flip is planned now).

### Decision D1.c: TLS conflicts are normalized — scheme wins (revised)

> **Revised 2026-10-27.** First version only warned and accepted per-language
> SDK behavior. Validation showed both SDKs violate the spec rule, in
> different directions, so "accept" meant shipping a parity break.

**Choice**: when `OTEL_EXPORTER_OTLP[_<SIGNAL>]_INSECURE` contradicts the URL
scheme, the helper resolves that signal per spec (the scheme wins), passes
explicit consistent options for **that signal only** (Go: `WithEndpointURL`
+ `WithInsecure`/TLS default; Python: `endpoint=` + `insecure=`), and warns.
Non-conflicting configs stay with the SDK (ADR-001). Also warn when
`OTEL_EXPORTER_OTLP_CERTIFICATE` is set with a plaintext endpoint.

**Justification, strongest first**:
1. Spec: `http`/`https` schemes "take precedence over the insecure
   configuration setting"; `INSECURE` applies only to schemeless gRPC
   endpoints ([exporter.md](https://github.com/open-telemetry/opentelemetry-specification/blob/main/specification/protocol/exporter.md)).
2. Measured (dev prototype, otel-go v1.47, Python 1.42/1.45):

   | Combination | Go gRPC | Go HTTP | Py gRPC | Py HTTP |
   |---|---|---|---|---|
   | `https://` + `INSECURE=true` | plaintext ✗ | plaintext ✗ | TLS | TLS |
   | `http://` + `INSECURE=false` | TLS ✗ | TLS ✗ | TLS ✗ | plaintext |

   With scheme and `INSECURE` consistent, all four exporters agree.
3. Detection is a URL parse plus a comparison; the override is per signal, so
   per-signal env handling for other signals is untouched.

**Trade-offs accepted**:
| Cost | Why acceptable |
|---|---|
| A fourth helper-owned shim | Only on conflicting (misconfigured) input; warned; fixture-covered by `export` TLS cases |
| Go: `CERTIFICATE` + `http://` fails at exporter `New()` (`insecure HTTP endpoint cannot use TLS client configuration`) | Warned before construction; the config is contradictory |

Corollary (A3, 2026-10-28): `OTEL_EXPORTER_OTLP_CERTIFICATE` with a plaintext
`http://` endpoint under `http/protobuf` makes the Go SDK's `New()` fail
(`insecure HTTP endpoint cannot use TLS client configuration`). The helper
drops the TLS client config for that signal and warns, so env-only config
never fails setup (R8) — Python's HTTP exporter already ignores the cert there.

**When this decision would be wrong**: upstream fixes the SDKs (file issues:
opentelemetry-go, opentelemetry-python). **Reversal action**: delete the shim
once both SDK versions in range comply; keep the fixture cases.
**Alternatives rejected**: warn only (parity break); helper parses every
endpoint/TLS env var (re-implements the SDK, against ADR-001).

### Decision D1.d: helper default compression `gzip`

**Choice**: when neither code nor `OTEL_EXPORTER_OTLP[_<SIGNAL>]_COMPRESSION`
set compression, pass `gzip` — in Go and Python alike.
**Justification**: Go forces gzip today (`tracing.go:49,61`, `metrics.go:92`);
deferring to the SDK default (`none`) would silently raise egress; Python
never set it, so this also closes a parity gap. Same pattern as the helper's
30 s export interval.
**Trade-offs**: CPU cost of gzip on the app — negligible relative to egress.
**When wrong**: a Collector receiver without gzip support. **Reversal action**:
`OTEL_EXPORTER_OTLP_COMPRESSION=none`.
**Alternatives rejected**: SDK default `none` (egress regression for Go users).

Python corollary (A3, 2026-10-28): the Python gRPC exporters (1.42 and 1.45)
raise `InvalidCompressionValueException` on the spec value
`OTEL_EXPORTER_OTLP_COMPRESSION=none` (their map only has `gzip`), so the
Python helper always resolves compression itself and passes `compression=`
explicitly. The Go trace/metric gRPC exporters reject a `"none"` compressor
via `otel.Handle`, so the Go helper translates `none` into "no compressor option".

### Decision D1.e: Python timeout env is converted from milliseconds

**Choice**: in Python, when `OTEL_EXPORTER_OTLP[_<SIGNAL>]_TIMEOUT` is set and
no code value overrides it, the helper reads it as **milliseconds** (spec),
converts to seconds and passes `timeout=` explicitly for that signal.

**Justification, strongest first**:
1. Spec: the OTLP timeout env vars are in milliseconds (default `10000`).
   Python sdk 1.42/1.45 parse them as seconds —
   `float(environ.get(OTEL_EXPORTER_OTLP_TIMEOUT, 10))`
   (`exporter/otlp/proto/grpc/exporter.py:354` in 1.42, `:325` in 1.45; per
   signal `trace_exporter/__init__.py:113-115`) — so `500` becomes 500 s.
2. Same pattern as D1.c: where an SDK violates the spec, the helper owns that
   one input; Go already behaves per spec once G10 lands.

**Trade-offs accepted**:
| Cost | Why acceptable |
|---|---|
| Behavior differs from a bare Python SDK with the same env | The bare SDK is the non-compliant one; documented |
| Consumers who set seconds intentionally (e.g. `10`) get 10 ms | Migration note; values < 100 log a warning ("looks like seconds") |

**When this decision would be wrong**: upstream changes the unit.
**Reversal action**: drop the shim once every SDK version in range parses
milliseconds (file an opentelemetry-python issue; keep the fixture cases).
**Alternatives rejected**: document as a parity gap (silent 1000× timeout
difference across languages); follow the SDK (contradicts the spec).

Corollary (A3, 2026-10-28): the Python gRPC exporters parse
`OTEL_EXPORTER_OTLP_<SIGNAL>_TIMEOUT` with an unconditional `float()` even when
`timeout=` is passed, so a garbage value crashes construction. The helper hides
that variable from `os.environ` only while constructing the exporter and
restores it immediately (warned as an unparseable env value, R8).

---

## D2 — Parity fixture ([ADR-008](../decisions/ADR-008-shared-parity-fixture.md))

```json
{
  "cases": [
    {
      "id": "endpoint.https_no_port_http",
      "kind": "export",
      "env": {"OTEL_EXPORTER_OTLP_ENDPOINT": "https://{receiver}",
              "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
              "OTEL_EXPORTER_OTLP_CERTIFICATE": "{receiver_ca}"},
      "code": {},
      "expect": {"received": {"traces": {"port": 443, "path": "/v1/traces"}}},
      "status": {"go": "xfail:A3", "python": "xfail:A3",
                 "dotnet": "xfail:phase-c", "nodejs": "skip:phase-b"}
    }
  ]
}
```

- `kind`:
  - `resolve` — snapshot of **decisions the helper owns** only:
    `service_name`, `environment`, `service_instance_id_source`,
    `otlp_signals[]`, `protocol{signal}`, `endpoint_source{signal: code|env|none}`,
    `code_endpoint_url{signal}` (only URLs the helper itself passed),
    `sampler{type,ratio}`, `metric_exporters`, `compression`, `log_level`,
    `warnings[]`.
  - `export` — fake OTLP receiver asserts proto/TLS/port/path of what was
    actually received. **Final URLs and TLS are asserted only here.** Receiver:
    one small Go binary (`testing/fakeotlp/`, run as a `docker compose`
    service) — every port accepts gRPC and HTTP (dispatch on
    `content-type: application/grpc`, h2c for plaintext), generates a test CA
    at startup (`/out/ca.pem`), binds 80/443 as non-root (Docker
    `ip_unprivileged_port_start=0`), and exposes `GET/DELETE :8888/requests`.
    Assert on the recorded port, not `Host` (Python HTTP omits the port).
    Prototyped: 132 lines; Go and Python exporters recorded all four modes.
    Plus unit tests of the helper's pure resolution functions (upstream's
    two-layer pattern: config-struct tests + mock collector).
  - `sampler`, `http_server` / `grpc_server` (health: span + metric
    assertions), `metrics_scrape`, `logging`.
- `code` keys are snake_case; each runner maps them to its API.
- `expect` operators: exact value, `{"prefix": …}`, `{"regex": …}`,
  `{"absent": true}`; `error: "<substring>"` = code-level failure;
  `warning: "<substring>"` = env value warned and ignored.
- `status`: `pass` | `xfail:<task-or-phase>` | `skip:<reason>`; strict — an
  `xfail` that passes fails the build. A1 lands the fixture describing today's
  behavior; target cases carry `xfail:<task>` until that task flips them.
- Runners: Go `go/parity_test.go`; Python `python/tests/test_parity.py` (one
  subprocess per case — OTel globals are set-once). Docker test commands mount
  the repo root.
- Adapters read helper-owned state only; never private SDK fields, never a
  re-derivation of SDK resolution.

---

## D3 — Sampler → [ADR-002](../decisions/ADR-002-parentbased-sampler.md)

Ownership (A4, 2026-10-28; numeric grammar unified after validation): both
helpers parse `OTEL_TRACES_SAMPLER`/`_ARG` themselves; `_ARG` and the deprecated
`OTEL_HELPER_SAMPLE_RATIO` use the same plain-decimal grammar as timeouts
(`^[0-9]+(\.[0-9]+)?$`, no hex/sign/exponent/full-width digits), NaN rejected,
out-of-range values warn; spec-listed but unsupported names (`jaeger_remote`,
`parentbased_jaeger_remote`, `xray`) warn "unsupported by this helper". Both helpers parse (ADR-001 owned-inputs list): six spec names, case-insensitive,
trimmed; unknown name → warn + default; invalid/out-of-range/NaN `_ARG` → warn +
1.0; one `build_sampler`/`buildSampler` function returns the sampler so the D5
health wrapper can wrap it. P17 ("no `_internal` imports") applies to the
package, not to test fixtures.

*(Superseded by the Ownership paragraph above — kept for history:)* Implementation note: the Python SDK already defaults to `parentbased_always_on`
and parses `OTEL_TRACES_SAMPLER` (`sdk/trace/sampling.py`); the Python bug is
the helper passing a bare sampler. Fix = stop passing one unless code or the
deprecated ratio alias sets it (then wrap in `ParentBased`).

---

## D4 — `service.instance.id`

**Choice**: code > `OTEL_RESOURCE_ATTRIBUTES` > SDK > helper.
- Go: the helper generates a per-process UUID (the Go SDK detector is behind
  `OTEL_GO_X_RESOURCE`, `sdk/resource/resource.go:261`).
- Python ≥ 1.45: the SDK's `ServiceInstanceIdResourceDetector` is on by
  default and refreshes the id after fork — the helper adds nothing.
  *Amended 2026-10-29 (A16): the SDK's fork refresh also replaced an
  explicit (code/env) id; the helper now restores its resource after the
  SDK hook, so an explicit id survives the fork (and still collides across
  workers, as warned) — D1 row "Python: SDK parse echo, resource detectors,
  after-fork refresh".*
- Python 1.42–1.44: the helper generates a per-process UUID at setup.
- Documented model for pre-fork servers: call setup in each worker
  (gunicorn `post_fork`, uWSGI `@postfork`), per the OTel Python fork docs.
- An env/code-provided id in a multi-worker server is a collision risk: the
  helper warns when it can detect the fork (it was imported before forking,
  e.g. `--preload`; when first imported inside the child it cannot tell) and
  docs mark it unsupported. The
  value is never rewritten by the helper.
- Pod identity lives in `k8s.pod.uid` (via `k8sattributes`), not in
  `service.instance.id`.

**Justification, strongest first**:
1. Semconv: the id MUST be unique per `service.namespace,service.name`; one id
   for several processes is explicitly not recommended
   ([service registry](https://opentelemetry.io/docs/specs/semconv/registry/attributes/service/)).
   Shared ids make worker counters collide in VictoriaMetrics.
2. Measured (dev, gunicorn 26.2, 2 workers):

   | Case | sdk 1.42 | sdk 1.45 |
   |---|---|---|
   | `post_fork` setup, helper id | unique per worker | unique per worker |
   | `--preload`, setup at import | **same id** in both | SDK replaces the helper's id with a fresh one per worker |
   | `post_fork` + env id | collision | collision |
   | `--preload` + env id | collision | env id **overwritten** per worker |

3. Following the SDK where it already behaves correctly is ADR-001.

**Trade-offs accepted**:
| Cost | Why acceptable |
|---|---|
| VictoriaMetrics promotes all resource attributes to labels → series × workers, churn per restart (and per gunicorn `max_requests` recycle) | Required for correctness; platform drops other volatile attrs with `-opentelemetry.ignoreResourceAttributes` (D11) |
| Behavior depends on SDK version in range | Fixture covers 1.42 and 1.45 legs |

**When this decision would be wrong**: measured VM cardinality from worker
churn becomes a problem. **Reversal action**: recommend one process per pod
(horizontal scaling) for OTLP-push workloads.
**Alternatives rejected**: `-<pid>` suffix on env ids (invented, mutates user
data); raise the floor to 1.45 only for this (collapses the supported range to
one minor); pod-level UUIDv5 recipe (per container — wrong for multi-worker).

---

## D5 — Health-check exclusion

**Choice**: a **sampler wrapper** (both languages), deciding for SERVER spans
with **no local parent** (absent or remote — a probe carrying a sampled
`traceparent` is still dropped); children follow via ParentBased, so a DB call
inside `/ready` is dropped too; returns DROP when the path — `url.path`, falling back to
`http.target` (Python default semconv) — is a health path (exact,
case-insensitive, trailing slash and query ignored), or when the gRPC span name
(leading `/` stripped) is `grpc.health.v1.Health/Check|Watch`. Python gRPC
instead uses the native `filters.negate(filters.health_check())`: that
instrumentation emits no metrics at all (grep for any meter/histogram in
`opentelemetry-instrumentation-grpc` 0.63b0 and 0.66b1 returns nothing), so
there is nothing to keep. The parent's tracestate is kept on DROP. Defaults:
`/health /healthz /ready /readyz /live /livez /ping /metrics`; list overridable
in code. Instrumentation middleware keeps running, so metrics still count.
Clarified 2026-10-28 (A7-go): the overridable list (`WithHealthPaths` /
`health_paths`) covers **HTTP paths only**; the standard
`grpc.health.v1.Health` service is always dropped (it has one well-known name).
Children of a dropped health span are dropped only under a parent-based
sampler (the default and both ratio aliases); with an explicit `always_on` or
`traceidratio` they are sampled again — documented, since a sampler cannot
mark the context.

**Justification, strongest first**:
1. Both instrumentation-level filters drop metrics too: Python
   `instrumentation-asgi` returns before any span **or metric** when
   `excluded_urls` matches; Go otelhttp `WithFilter` (`handler.go:88-90`) and
   otelgrpc `WithFilter` skip metrics as well. Probe failures must stay visible.
2. One mechanism, identical in both languages, sitting next to the sampler the
   helper already builds.
3. Exact matching keeps real routes (`/users/ping`) traced.

**Trade-offs accepted**:
| Cost | Why acceptable |
|---|---|
| Relies on path/method at span start | Verified by dev prototype: Python 1.42/1.45 (`http.target` default, `url.path` with `OTEL_SEMCONV_STABILITY_OPT_IN=http`), Go otelhttp v0.72 (`url.path`, query stripped), otelgrpc v0.72 (span name) — health spans dropped, `http.server.(request.)duration` / `rpc.server.call.duration` still counted |
| Case-insensitive match also drops spans for unserved paths like `/HEALTH` (404) | Harmless; still counted in metrics |
| Market practice mostly drops health from both signals (`excluded_urls`, Collector `filterprocessor`) | Keeping metrics is defensible (probe failures visible); SLI queries must exclude health routes — documented. Precedent for the sampler: java-contrib `RuleBasedRoutingSampler`, spec `ComposableRuleBased` (Development) |
| Dropped health spans still propagate a non-sampled context downstream | Health handlers don't call downstream |
| Users' own `OTEL_PYTHON_*_EXCLUDED_URLS` keep upstream meaning (drop span **and** metric) | That is what those vars mean upstream; documented |

**When this decision would be wrong**: the span-start attributes don't carry
the path in some instrumentation version.
**Reversal action**: drop at a SpanProcessor/exporter stage (span is recorded
but not exported), still keeping metrics.
**Alternatives rejected**: `excluded_urls` / `WithFilter` (drop metrics);
second otelhttp handler with a no-op TracerProvider (Go-only, no Python
equivalent); prefix matching (drops real routes).

---

## D6 — Logging

| Situation | Behavior |
|---|---|
| Logs exporter active | OTLP only |
| No endpoint / `OTEL_LOGS_EXPORTER=none` | stdout only; never an OTel handler without a processor |
| `OTEL_LOGS_EXPORTER=otlp,console` | both |
| Helper self-diagnostics | always stderr |

**Choice**: OTLP-only when an exporter is active; stdout is the opt-in via the
standard `OTEL_LOGS_EXPORTER` list.

**Justification, strongest first**:
1. Fluent Bit/filelog commonly tail stdout — teeing would ingest every log
   twice into Loki.
2. The real `kubectl logs` need (startup failures, collector unreachable) is
   covered by stderr self-diagnostics.

**Trade-offs accepted**:
| Cost | Why acceptable |
|---|---|
| Logs emitted while the collector is down are lost after SDK retries | Same as traces/metrics; `otlp,console` exists |
| New core dependency `opentelemetry-instrumentation-logging` (Apache-2.0) | Replaces the deprecated SDK `LoggingHandler`; same maintainers, lockstep-bounded with contrib (D9.a) — **approved by the owner 2026-10-27** |

**When this decision would be wrong**: incidents where app logs were needed and
the collector was down.
**Reversal action**: default logs to `otlp,console`.
**Alternatives rejected**: always tee (double ingestion); new `OTEL_HELPER_*`
knob (spec already has one).

Python level rule: the helper sets the root level only when, at setup, root
has no handlers and `logging.root.level == WARNING` (stdlib default); helper
handlers use the environment level (R4); the no-exporter stdout fallback is `logging.lastResort` (see A5 rulings below). Go: one handler deciding **per record** (atomic flag),
used by `NewLogger` and `NewSlogHandler`, reset on `Shutdown`.

Helper-diagnostics channel (decided 2026-10-28, A2 review L1): **Go** — one
package-level `*slog.Logger` writing to **stderr** (`slog.NewTextHandler(os.Stderr, …)`),
never `slog.Default()` (an app may have pointed it at the OTel bridge), never
`fmt.Fprint*`; `otel.Handle` stays only for exporter/runtime errors raised by
the SDK pipeline. The logger is a package var so tests can hook it.
**Python** — named loggers under `otel_helper` (stdlib library convention, no
handlers added by the library for its own diagnostics; with no app config,
WARNING+ reaches stderr via `logging.lastResort`). Both are what the parity
`helper_log` field captures; a test enforces it (A3).

> **Amended 2026-10-29 (ADR-011).** The Python package is renamed
> `staffops_otel_helper` (distribution `staffops-otel-helper`), so the
> diagnostics channel is the named loggers under **`staffops_otel_helper`**;
> every `otel_helper.*` logger name in this design (D6 A5 rulings, D14 ext
> hints) reads `staffops_otel_helper.*`. The parity `helper_log` capture
> (`python/tests/parity_driver.py`) keys on the new hierarchy. The
> `otel_helper` compatibility module (0.3.x only, removed in 1.0) re-exports
> the API but does **not** alias logger names: filters on `otel_helper.*`
> must move.
The enforcement test is a **backstop, not a sandbox**: after four review rounds
it catches every realistic accidental pattern; deliberately obfuscated writes
(paths in constants, function values of `os.WriteFile`, `vars(sys)`…) are out
of scope and code review covers them (decided 2026-10-28 — further hardening
has diminishing returns).

A5 validation rulings (2026-10-28):
- **Helper diagnostics always reach stderr** in both languages, INFO and above
  (Go: the stderr diag logger; Python: while a logs exporter is active, a
  root-level filter handler sends `otel_helper.*` and `opentelemetry.*` records
  at INFO+ (`otel_helper.*`; Python sets that logger to INFO when it is NOTSET,
  restored on detach) / WARN+ (`opentelemetry.*`, kept at WARN as noise) to the
  original stderr `lastResort`, removed when the app configures root; loggers
  of both hierarchies are re-enabled after an app `dictConfig`/`fileConfig`
  with `disable_existing_loggers=True`). `opentelemetry.*` SDK/exporter records are **never** exported via OTLP
  (avoids the export-failure feedback loop; Go equivalent: `otel.Handle`);
  `otel_helper.*` records go to stderr and, additionally, to OTLP.
- **Stdout fallback shape is identical**: single-line JSON with `time`
  (RFC 3339, millisecond precision, `Z` for UTC — both languages),
  `level` (`DEBUG/INFO/WARN/ERROR`), `msg`, the logger name (`logger`, Python) and
  extra attributes, plus `exception` for tracebacks (one line); both languages
  apply the environment level to it (R4).
- Python installs the no-exporter fallback as `logging.lastResort` (not a root
  `StreamHandler`) so a later `logging.basicConfig()` still works (P10).
- **Client-side health exclusion**: exact path match only (same normalization
  as D5), in both languages; Python passes anchored patterns via
  `instrument(excluded_urls=…)` for requests; httpx only reads env, so its
  variable is set **only during setup** and restored (does not cover
  `instrument_client()`/transports the user builds later — documented); health
  paths may not contain commas (they would split the upstream list). Client exclusion drops
  the client span and metric (upstream behavior) — documented.
- gRPC health matching is the whole service in both languages: method name
  (leading `/` stripped) starts with `grpc.health.v1.Health/` (covers Check,
  Watch and List).
- Go's stdout fallback renders error values like `slog` does (typed-nil error →
  `<nil>`, a panicking `Error()` → `!PANIC: …`), never crashing the caller.
- **gRPC client** health calls (`grpc.health.v1.Health/*`) are excluded on the
  client side too, in both languages — same rule as HTTP clients: the client
  span **and** the client metric are dropped (the only upstream client-side
  mechanism is a filter); server-side RED metrics still count health calls.
- Go trace↔log correlation needs the `*Context` slog calls (`InfoContext`…);
  Python correlates implicitly. Documented in both HOW-TOs.

A13 validation ruling (2026-10-28):
- **Go `NewSlogHandler` level-filters both paths.** The handler applies the level of the environment the last successful `Setup` resolved (`DefaultLogLevel`; Info before the first `Setup`) to records going through the OTel bridge too, not only to the stdout fallback — the LoggerProvider enables every level, so a `nil` minimum exported DEBUG/INFO in PRD. The level is read per record (a `slog.LevelVar` updated by `Setup`), so a handler created before `Setup` picks up the level `Setup` resolves. This is the Python behavior (`HelperLogHandler(level, …)`) and R4 ("helper-created log handlers SHALL use the environment level"). `NewLogger(env, debug)` keeps its explicit level. Parity fixture: not expressible yet — the `logging` kind is console-only and emits one WARN through `NewLogger`, and the `export` kind emits its log record on the global LoggerProvider, bypassing the handler; covered by Go unit tests until a wire-capturing logging kind with a record-level input exists.

Coupling note: `OTEL_HELPER_DEBUG_LEVEL=true` turns on both DEBUG logs and the
`debug=true` span marker — documented as coupled (ADR-006).

---

## D7 — Debug marker → [ADR-006](../decisions/ADR-006-debug-span-attribute.md)

---

## D8 — Deprecations

**Choice**: warn once from 0.3, remove at 1.0: `SERVICE_NAME`, `ENVIRONMENT`
(alias), `OTEL_HELPER_SAMPLE_RATIO`, `OTEL_HELPER_DISABLED_SIGNALS`, schemeless
endpoints **for `http/protobuf` only** (schemeless gRPC is legal per spec and
supported — D1, R1). `OTEL_HELPER_EXTRA_INSTRUMENTATION`: removed in Go (no effect
today; warns if set — G9), deprecated in Python.

**Justification**: standard vars already win, so duplicates add surface without
capability; a dead knob silently accepted misleads users.

**Trade-offs accepted**:
| Cost | Why acceptable |
|---|---|
| Log noise for configs still on legacy vars | Once per process; the migration note gives the replacement |

**When wrong**: consumers can't migrate before 1.0. **Reversal action**: extend
the window; never drop silently.
**Alternatives rejected**: remove now (no warning period).

> **Amended 2026-10-29 (ADR-011).** The deprecated **import path** `otel_helper`
> (Python, 0.3.x, removed in 1.0) is a code-level deprecation, so it uses the
> stdlib channel for those: one `DeprecationWarning` attributed to the importing
> line (Python ≥ 3.12 via `skip_file_prefixes`). Because the default filters hide
> it outside `__main__` (an app module loaded by uvicorn/gunicorn), the same
> notice is also logged once per process on the `staffops_otel_helper.compat`
> logger, like the env-var deprecations above.

---

## D9 — Dependencies

Go → [ADR-005](../decisions/ADR-005-go-1-26-floor-grpc-stats-handlers.md).

### Decision D9.a: Python lockstep bounds

**Choice**: api/sdk/otlp exporters `>=1.42,<1.46`; all contrib (core, extras,
and `opentelemetry-instrumentation-logging`) `>=0.63b0,<0.67b0`. CI: py
{3.10, 3.13} × {lower-bound constraints file, latest}.

**Justification, strongest first**:
1. Contrib betas pin the SDK exactly (`exporter-prometheus 0.66b1` →
   `opentelemetry-sdk~=1.45.1`, `0.63b0` → `~=1.42.0`; `semantic-conventions
   0.63b0` → `api==1.42.0`), so an unbounded `>=0.63b0` drags the SDK past
   what was tested.
2. Measured (dev prototype): all 4 matrix cells pass 153 tests (suite run with
   `--ignore=tests/test_propagation.py`) at 95–96% once the private
   `_metric_readers` assertion is removed.

**Trade-offs accepted**:
| Cost | Why acceptable |
|---|---|
| Caps must be raised by PR | That PR runs the latest leg — the point of the cap |

**When wrong**: a consumer needs an SDK above the cap urgently.
**Reversal action**: raise the cap in a patch release after the latest leg passes.
**Alternatives rejected**: exact pins (packaging rule); no caps (status quo bug).

---

## D10 — Release

> **Amended 2026-10-28 (owner):** there is no DEV/HML environment; the rc
> soak is the local simulation in [`specs/soak-simulation/`](../soak-simulation/)
> (Docker Compose; Kubernetes deferred as its Phase K). Its acceptance replaces
> "soaked in DEV/HML" below.

**Choice**: one coordinated `0.3.0` for Go + Python, preceded by
`0.3.0-rc.1` soaked in DEV/HML; no legacy-behavior flag; .NET stays on 0.2.x
until Phase C ([ADR-004](../decisions/ADR-004-phased-rollout-go-python-first.md)).

**Justification**: the Go interceptor removal forces a breaking release anyway;
staged flips would make users migrate twice; a legacy flag keeps two code
paths in every language with no removal date.

**Trade-offs accepted**:
| Cost | Why acceptable |
|---|---|
| One large migration for Go/Python consumers | Single migration guide; rc soak first |
| Go/Python and .NET versions diverge (0.3.x vs 0.2.x) | Tracked by fixture xfails; per-language docs |

**When wrong**: rc soak shows widespread breakage from one change.
**Reversal action**: hold that change behind a one-release compat shim, ship the rest.
**Alternatives rejected**: staged 0.3/0.4 flips; `OTEL_HELPER_LEGACY_PRECEDENCE` flag.

**A11 ruling (2026-10-28, owner approved the rc cut).** Release tags per
language, because versions now diverge (ADR-004): `vX.Y.Z[-rc.N]` publishes the
Python wheel/sdist to the GitHub Release (PEP 440 version in `pyproject.toml`,
e.g. `0.3.0rc1`); `go/vX.Y.Z[-rc.N]` plus `go/ext/<module>/vX.Y.Z[-rc.N]` for
each ext module (Go submodule tag rule); `dotnet/vX.Y.Z` publishes NuGet. Before
this ruling every `v*` tag also published .NET under that version, so
`v0.3.0-rc.1` would have shipped the unchanged 0.2.x .NET code as 0.3.0-rc.1.

---

## D12 — Python defaults HTTP semconv to stable

> **Corrected 2026-10-28 (A13).** The default became `http,database/dup` in
> A9/A10 (DB semconv parity with Go's `ext/otelsql`, requirements.md R-DB
> bullet); this section still said `"http"`. The HTTP reasoning below is
> unchanged; `database/dup` emits both `db.system` and `db.system.name`.

**Choice**: `_ensure_semconv_default()` does
`os.environ.setdefault("OTEL_SEMCONV_STABILITY_OPT_IN", "http,database/dup")`; called first
in `setup_telemetry` and at the top of each `ext.*` entry point; detects late
initialization via the private
`_OpenTelemetrySemanticConventionStability._initialized` (guarded by
try/except) and warns once.

**Justification, strongest first**:
1. Go can no longer emit the old generation (otelhttp v0.72), so without this
   the two languages emit different metric names and units — no shared query.
2. Contrib reads the variable once, at first `instrument()`
   (`opentelemetry/instrumentation/_semconv.py`, identical in 0.63b0 and
   0.66b1); there is no public programmatic API, and the helper already uses
   `os.environ.setdefault` for `OTEL_PYTHON_*_EXCLUDED_URLS`.
3. `http` over `http/dup`: dup doubles HTTP histogram series; the semconv HTTP
   migration guide positions dup as a user opt-in transition phase.

**Trade-offs accepted**:
| Cost | Why acceptable |
|---|---|
| Breaking for Python dashboards on `http_server_duration_milliseconds` / `http_target` | Repo `dashboards/` use no HTTP metrics; `python/example/EXAMPLES.md` already uses stable names; escape hatch `http/dup` documented |
| Affects every contrib instrumentor in the process and child processes | Desirable (one generation); documented |
| Relies on a private attribute for the late-init warning | try/except; worst case no warning |

**When this decision would be wrong**: a consumer must keep old names and
cannot set an env var. **Reversal action**: code option to skip the default.
**Alternatives rejected**: `http/dup` default (series ×2); leave Python old
(parity break).

---

## D13 — Lifecycle: setup after shutdown (A6)

**Choice**: Go rebuilds the whole pipeline on `Setup` after `Shutdown` — but only
code that obtains tracers/meters **after** the new `Setup` uses it: the Go global
delegates forward from their first provider once, so tracers, meters,
instruments and otelhttp/otelgrpc handlers created before stay bound to the
shut-down providers. Python rebuilds logs and propagators only; traces and metrics stay off after a
successful shutdown in the same process, with a warning — a **documented parity
gap**.

**Justification, strongest first**:
1. The Python API installs global tracer/meter providers once per process
   (`set_tracer_provider`/`set_meter_provider` are guarded by a set-once flag);
   resetting it needs private API (`_internal` for metrics/logs), which P17
   forbids in the package.
2. The only alternative — helper-owned forwarding providers installed once,
   swapping SDK providers behind them — changes what `get_tracer_provider()`
   returns (breaks `isinstance(..., sdk.TracerProvider)` in user code) and needs
   forwarding instruments for metrics: high risk for a rare use case.
3. Re-setup after shutdown is rare in production (tests, hot reconfiguration);
   failure rollback — the common case — works fully in both languages because
   nothing global is installed until every signal is built.

**Trade-offs accepted**:
| Cost | Why acceptable |
|---|---|
| Python: no traces/metrics after shutdown→setup in one process | Warned at setup; documented in HOW-TO and CHANGELOG |
| Python has one global `shutdown_telemetry()` instead of a per-setup function, so the Go "stale shutdown" guard has no Python equivalent | Shutdown is serialized with setup under a lock |

**When this decision would be wrong**: a consumer needs in-process
reconfiguration of traces/metrics in Python (e.g. long-running workers that
rotate collectors).
**Reversal action**: implement forwarding providers behind an opt-in.
**Alternatives rejected**: forwarding providers by default (isinstance break,
metrics forwarding complexity); private-API reset (violates P17); reviving the
old global Python `TracerProvider` with a new span processor via public
`add_span_processor()` (works for traces — measured — but metrics cannot come
back: a shut-down `MeterProvider` refuses new meters; a traces-only revival would
be its own parity gap). After the downgrade `options.is_signal_enabled("traces")`
/`("metrics")` must report False.

### Shutdown result semantics (A6 validation)
Both languages: the first shutdown call does the work; every later or
concurrent call returns **the same result** (Go: the same `error`; Python:
re-raises the same `RuntimeError`) until the next successful setup. A `Setup`
called while a shutdown is in flight **waits** for it to finish and then builds
a fresh pipeline (never returns the dying one).

### Shutdown concurrency (A6 validation)
Both languages shut down logs, metrics and traces **concurrently** under the
caller's single deadline (Go previously ran them sequentially, so a logs exporter
retrying against a dead collector could consume the whole deadline and traces
would lose their buffer).

### Process termination (A6 validation)
Neither helper installs signal handlers (a library must not take over the app's
signals). Documented in both HOW-TOs: Go — default SIGTERM kills without running
deferred `Shutdown`; use `signal.NotifyContext` and call `Shutdown`. Python —
default SIGTERM skips `atexit`, so buffered spans/logs (up to the batch delay)
and the last metric interval are lost; handle SIGTERM (`sys.exit(0)` or the event
loop's signal handler) or run under gunicorn/uvicorn, which do. The examples
show the pattern (verified on the wire with `docker stop`).

### Mounted `/metrics` handler status (A6/A8 validation)
| State | Status |
|---|---|
| Before setup, or setup failed | 503 (transient — scrape retries) |
| Prometheus deliberately not active (`OTEL_SDK_DISABLED=true`, or `OTEL_METRICS_EXPORTER` without `prometheus`) | **200 with an empty body** — a 503 would mark the target `up=0` and fire TargetDown-style alerts for a configuration choice |
| After shutdown | 503 |
Same in Go `MetricsHandler()` and Python `metrics_app()`; CHANGELOG marks the
change BREAKING (0.2.0 served the global registry, incl. `process_*`/`python_*`).

---

## D14 — Go ext modules API (A10 validation)

- `otelsql.Open(driver, dsn) (db *sql.DB, closeDB func() error, err error)`:
  `closeDB` unregisters the pool-metrics callback and closes the pool (call it
  instead of `db.Close`, mirroring `Setup`'s shutdown func) — otherwise every
  open/close cycle leaks a callback that keeps exporting a closed pool.
- `otelredis.Instrument`: tracing + metrics with
  `redisotel.WithSemConvCompliantMetrics(true)` (metrics are new in 0.3, so no
  consumer depends on the old instrument types); spans also carry
  `db.system.name=redis` and never carry command values or the connection
  string (`WithDBStatement(false)` or a sanitizer equivalent to Python's
  `SET ? ?`). Metric names are `db.client.connections.*` (as emitted
  by redisotel v9.23) — documented with their Prometheus names. Call once per
  client; on a metrics error tracing may already be installed (documented).
- Debug marker limit (ADR-006): under the default ParentBased sampler an entry
  span with an unsampled remote parent is dropped before the marker runs, so
  debug mode cannot force retention of traces an upstream caller didn't sample.

**Reversal action** (added 2026-10-28, spec lint): if `closeDB` proves
unwanted, keep `Open` returning it but make `db.Close` sufficient by
registering the callback on a wrapper driver (one release with both paths);
if semconv redis metrics churn, pin `WithSemConvCompliantMetrics(false)` and
document the old instruments — the ext modules are versioned with the core,
so either ships in the next minor.

Python ext import failures (A13 validation, 2026-10-28): `otel_helper.ext.{aws,redis,sql}` raise an `ImportError` whose message claims a cause only when it is certain:
- `ModuleNotFoundError` for the extra's instrumentor module (`opentelemetry.instrumentation.<lib>`, passed in by the entry point) or one of its parent packages → "instrumentation not installed. Install the staffops-otel-helper wheel with the [<extra>] extra (not on PyPI; see CONSUMING.md)" *(wheel name amended 2026-10-29, ADR-011; was "the otel-helper wheel")*. *(Corrected 2026-10-28, repo-tooling review H2: the first wording, "Run: pip install otel-helper[<extra>]", told users to install by name from PyPI, where `otel-helper` is not registered — a dependency-confusion vector in a public repo. The hint now points to the wheel and CONSUMING.md.)* *(Amended 2026-10-29, ADR-011: the module is `staffops_otel_helper.ext.*` and the hint names the `staffops-otel-helper` wheel; `otel_helper.ext.*` resolves to the same modules through the 0.3.x compatibility module.)*
- `ModuleNotFoundError` whose missing top-level module is the instrumented library of that extra (`botocore` / `redis` / `sqlalchemy`, passed in by the entry point) → "library '<lib>' is not installed" (the extra does not pull it in).
- Anything else — another `opentelemetry.*` module (`opentelemetry.semconv._incubating.attributes`: version skew inside the OTel namespace), a submodule of the instrumentor, no module name (A13 review M2), a plain `ImportError` (version skew: `cannot import name 'X' from 'sqlalchemy'`), a missing transitive dependency (`wrapt`) — → "failed to import the <label> instrumentation: <error>", without guessing a cause. Before this ruling both cases were reported as "library not installed".

---

## D11 — Collector / platform prerequisites (reference checklist)

Owned outside this repo; each item blocks the 0.3.0 **rollout** (not the rc).

1. Tail sampling: default branch for spans **without**
   `deployment.environment.name` at the PRD rate (ADR-003).
2. Tail sampling: the `debug=true` policy SHALL be combined (`and`) with an
   environment allow-list and a `rate_limiting` sub-policy, so a debug-enabled
   PRD service cannot force 100% retention of every trace it touches (ADR-006).
3. Gateway: `loadbalancingexporter` with `routing_key: traceID`;
   `decision_wait` ≥ observed p99 trace duration (unmeasured — measure first);
   *corrected 2026-10-28 (soak S-D8): p99 trace duration alone is too short —
   spans of one trace reach the gateway spread over the SDK batch schedule
   delay (5 s default) and the agent batch timeout; use p99 duration + max
   SDK batch delay + agent batch timeout + export time + margin, and enable
   `decision_cache.sampled_cache_size` > 0 (default 0 = off)*;
   sampled-decision cache enabled for late spans (ADR-006 rationale depends on it).
4. Charts: set `OTEL_SERVICE_NAME`; `OTEL_RESOURCE_ATTRIBUTES` with
   `deployment.environment.name`. Do **not** set `service.instance.id` for
   multi-worker containers (D4); single-process pods may use the semconv K8s
   recipe.
5. Istio: mesh tracing at 100% or disabled (ADR-002).
6. VictoriaMetrics OTLP ingest promotes every resource attribute to a label:
   use `-opentelemetry.ignoreResourceAttributes` for volatile attributes
   (e.g. `process.pid`); keep `service.instance.id` (D4).
7. Collector `filterprocessor` dropping health spans as defense in depth (D5).
8. Consumers using portless gRPC URLs: add an explicit `:4317` (D1.b warning).
