# StaffOps.OtelHelper — OpenTelemetry Helper for .NET

Observability library for .NET 8+ applications. Abstracts OpenTelemetry SDK configuration to provide standardized instrumentation.

> **Version note.** This page documents the `0.3` contract as the .NET helper
> implements it on this branch (Phase C of
> [`specs/dotnet-otel-helper/`](https://github.com/StaffOps/otel-libs/blob/main/specs/dotnet-otel-helper/tasks.md),
> tasks C4–C17; ADR-004), first published as the release candidate
> `0.3.0-rc.1` (tag `dotnet/v0.3.0-rc.1`) after the final validation, task C19.
> The released `0.2.1` keeps the 0.2 behavior. Notes marked "0.3 (this branch,
> task C<n>)" say what changed; "0.2.x:" gives the released behavior. Every
> change, in two parts (API; telemetry): the .NET migration guide in
> [`CHANGELOG.md`](https://github.com/StaffOps/otel-libs/blob/main/CHANGELOG.md).
> The remaining differences from Go, Python and Node.js are parity skips
> (design D-NET15) and the root README's "Known emitted-telemetry gaps" rows.
>
> **Names.** On this branch the packages, assemblies and namespace are
> `StaffOps.OtelHelper*` (ADR-011, task C2 of
> [`specs/dotnet-otel-helper/`](https://github.com/StaffOps/otel-libs/blob/main/specs/dotnet-otel-helper/tasks.md));
> they are first published as `0.3.0-rc.1`. The released `0.2.x` packages
> keep the IDs `OtelHelper*` and `using OtelHelper;` — no compatibility
> package. Environment variable names and the `AddOtelHelper*()` entry points
> do not change.

📖 **[HOW-TO.md](HOW-TO.md)** — Developer guide (logs, traces, metrics, examples)
🧪 **[TESTS.md](TESTS.md)** — Full list of unit tests
🚀 **[example/README.md](example/README.md)** — Sample apps with test endpoints and Grafana queries

## Quick Start

```csharp
// Everything resolved via env vars — zero config in code
services.AddOtelHelper();
```

Optionally, with overrides:

```csharp
services.AddOtelHelper(opts =>
{
    opts.ServiceName = "checkout-api";
});
```

Keep only the helper's stdout JSON line when no OTLP endpoint is set: call `builder.Logging.ClearProviders()` **before** `AddOtelHelper()`. Otherwise every record appears twice on stdout and the helper writes `otelhelper: WARN no logs exporter is active, so each record is written to standard output twice: …` on stderr (HOW-TO §4).

After registration, available via DI:
- `ActivitySource` — for creating manual spans (keyed: `[FromKeyedServices(OtelHelperKeys.Instrumentation)]`)
- `Meter` — for creating custom metrics (keyed: `[FromKeyedServices(OtelHelperKeys.Instrumentation)]`)
- `ActivitySourceExtensions.StartRootActivity()` — for independent traces in workers

---

## Environment Variables

### Required (injected automatically by infrastructure)

| Variable | Source | Description | Default |
|---|---|---|---|
| `OTEL_SERVICE_NAME` | CI/CD Pipeline | Service name, emitted as `service.name`. 0.3 (this branch): wins over `service.name` in `OTEL_RESOURCE_ATTRIBUTES` and over `SERVICE_NAME` | none: `unknown_service:<process name>` + one warning |
| `SERVICE_NAME` | CI/CD Pipeline | Deprecated alias. 0.3 (this branch): read after `OTEL_SERVICE_NAME` and `service.name` in `OTEL_RESOURCE_ATTRIBUTES` (0.2.x: read first); warned once when set, also when another level wins ([Deprecated variables](#deprecated-variables-03-this-branch-task-c16)) | — |
| `OTEL_RESOURCE_ATTRIBUTES` | Helm Chart | `key=value,…` resource attributes, parsed by the helper (0.3, this branch): blank items ignored, an item without `=` or with an empty key skipped, values percent-decoded (`%XX` only; `+` stays `+`) then trimmed of ASCII whitespace, an invalid escape kept literally; problems warned once by item position, never the value. `deployment.environment.name` here wins over `ENVIRONMENT`. `telemetry.sdk.*` keys are ignored | — |
| `ENVIRONMENT` | Helm Chart (`values.yaml`) | Deprecated alias of `deployment.environment.name`. 0.3 (this branch): a known name (`LOCAL`, `DEV`, `HML`, `PRD`, any case) in its canonical spelling, any other value emitted as written; unset everywhere → the attribute is absent, one warning, log level Information (0.2.x: `LOCAL`); warned once as deprecated when set ([Deprecated variables](#deprecated-variables-03-this-branch-task-c16)) | none |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | Helm Chart | Collector endpoint. Unset → no OTLP export, Prometheus `/metrics` fallback. 0.3 (this branch): parsed by the contract rules below ([Endpoint parsing](#endpoint-parsing-03-this-branch)); `OTEL_EXPORTER_OTLP_<SIGNAL>_ENDPOINT` beats it for one signal | _(unset)_ |

> These variables are injected automatically. Application teams **do not need to configure them manually**.

### Optional

| Variable | Description | Default |
|---|---|---|
| `OTEL_HELPER_DEBUG_LEVEL` | Debug mode: forces Debug log level and sets the attribute `debug=true` on entry spans (`true`/`false`) | `false` |
| `OTEL_HELPER_METRICS_PORT` | Prometheus `/metrics` port when no OTLP endpoint is configured. 0.3 (this branch, task C12): `^\+?[0-9]+$` after the contract trim, in [0, 65535], leading zeros allowed, `0` = no listener; anything else warns once and uses 9464 (0.2.x: `int.TryParse`, an unparseable value silently 9464). `OTEL_EXPORTER_PROMETHEUS_PORT` and `TelemetryOptions.PrometheusMetricsPort` win | `9464` |
| `OTEL_EXPORTER_PROMETHEUS_PORT` | 0.3 (this branch, task C12, A29): the listener port, same grammar; beats `OTEL_HELPER_METRICS_PORT` (no warning when both are set); invalid → one warning, the next level applies. 0.2.x: no effect | _(unset)_ |
| `OTEL_EXPORTER_PROMETHEUS_HOST` | 0.3 (this branch, task C12, A29): the listener bind address, see [Prometheus fallback](#prometheus-fallback-03-this-branch). 0.2.x: no effect | _(unset)_: every IPv4 interface |
| `OTEL_HELPER_HTTP_OPT_IN_METRICS` | 0.3 (this branch, task C11, A30): `true` (ASCII case-insensitive) keeps the HTTP metrics semantic conventions make opt-in: `http.server.active_requests`, `http.client.active_requests`, `http.client.open_connections`, `http.client.connection.duration`, `http.client.request.time_in_queue`; another value than `true`/`false` warns and means `false`. `TelemetryOptions.HttpOptInMetrics` wins. 0.2.x exported them always | `false` |
| `OTEL_DOTNET_EXPERIMENTAL_ASPNETCORE_ENABLE_GRPC_INSTRUMENTATION` | Upstream key, read by the ASP.NET Core instrumentation from the **application configuration** (environment or `appsettings.json`), never set by the helper (owner decision O7 (b)): `true` traces gRPC server calls as RPC spans; see [gRPC server spans](#grpc-server-spans) | unset |

### Extra Instrumentation

| Variable | Description | Default |
|---|---|---|
| `OTEL_HELPER_EXTRA_INSTRUMENTATION` | **No effect.** Nothing in core uses it since SQL/AWS moved to opt-in subpackages (the code option `TelemetryOptions.ExtraInstrumentation` is `[Obsolete]`). Use the subpackages below. 0.3 (this branch, task C16): a set value warns once ([Deprecated variables](#deprecated-variables-03-this-branch-task-c16)) | — (no effect) |
| `OTEL_HELPER_SAMPLE_RATIO` | Deprecated alias (0.3, this branch, task C10): a plain decimal in [0, 1] gives `parentbased_traceidratio`; anything else (`2`, `-1`, `abc`, `1e0`) warns `sample ratio is not a number in [0, 1]` and the default applies — not clamped. Ignored while `OTEL_TRACES_SAMPLER` is set or a sampler or ratio is set in code. 0.2.x: clamped, a bare `TraceIdRatioBasedSampler`, invalid values ignored silently. A set value is warned once as deprecated (task C16, [Deprecated variables](#deprecated-variables-03-this-branch-task-c16)). | _(unset)_ |

> Debug mode (`OTEL_HELPER_DEBUG_LEVEL=true`) does **not** enable extra instrumentations; it sets the Debug log level and marks entry spans — spans with no parent or a remote one (`ParentSpanId == default` or `HasRemoteParent`) — with a `debug=true` attribute (see HOW-TO §13). An in-process child started from an explicit context (the Redis instrumentation does this) is not an entry span and is not marked. 0.3 (this branch, task C10, ADR-006): the tracestate is no longer touched; 0.2.x prepended `debug=true` to it, which gave `debug=true,debug=true` under a remote parent. Combine the Collector's `debug=true` policy with an environment allow-list and a rate limit (design D11).

> ⚠️ **`OTEL_HELPER_DEBUG_LEVEL=true` in production causes cost explosion and backend saturation.** Use only for targeted troubleshooting.

### Deprecated variables (0.3, this branch, task C16)

Each deprecated variable that is set writes **one** line on standard error,
once per container (per diagnostics sink: two hosts in one process each write
it; Go, Python and Node.js write it once per process), also when a standard
variable or a code option wins over it. The value is never printed. Empty or
whitespace-only counts as unset: no line. The variable keeps its effect until
1.0, which removes it. 0.2.x wrote nothing.

| Variable | Migrate to | Line on standard error |
|---|---|---|
| `SERVICE_NAME` | `OTEL_SERVICE_NAME` | `otelhelper: WARN SERVICE_NAME is deprecated and will be removed in 1.0; use OTEL_SERVICE_NAME variable=SERVICE_NAME replacement=OTEL_SERVICE_NAME` |
| `ENVIRONMENT` | `OTEL_RESOURCE_ATTRIBUTES=deployment.environment.name=<value>` | `otelhelper: WARN ENVIRONMENT is deprecated and will be removed in 1.0; use OTEL_RESOURCE_ATTRIBUTES (deployment.environment.name=<value>) variable=ENVIRONMENT replacement="OTEL_RESOURCE_ATTRIBUTES (deployment.environment.name=<value>)"` |
| `OTEL_HELPER_SAMPLE_RATIO` | `OTEL_TRACES_SAMPLER=parentbased_traceidratio` + `OTEL_TRACES_SAMPLER_ARG=<ratio>` | `otelhelper: WARN OTEL_HELPER_SAMPLE_RATIO is deprecated and will be removed in 1.0; use OTEL_TRACES_SAMPLER=parentbased_traceidratio with OTEL_TRACES_SAMPLER_ARG variable=OTEL_HELPER_SAMPLE_RATIO replacement="OTEL_TRACES_SAMPLER=parentbased_traceidratio with OTEL_TRACES_SAMPLER_ARG"` |
| `OTEL_HELPER_DISABLED_SIGNALS` | `OTEL_TRACES_EXPORTER` / `OTEL_METRICS_EXPORTER` / `OTEL_LOGS_EXPORTER` `=none` | `otelhelper: WARN OTEL_HELPER_DISABLED_SIGNALS is deprecated and will be removed in 1.0; use OTEL_TRACES_EXPORTER/OTEL_METRICS_EXPORTER/OTEL_LOGS_EXPORTER=none variable=OTEL_HELPER_DISABLED_SIGNALS replacement="OTEL_TRACES_EXPORTER/OTEL_METRICS_EXPORTER/OTEL_LOGS_EXPORTER=none"` |
| `OTEL_HELPER_EXTRA_INSTRUMENTATION` (no effect) | the packages `StaffOps.OtelHelper.Sql`, `.AWS`, `.Redis` | `otelhelper: WARN OTEL_HELPER_EXTRA_INSTRUMENTATION has no effect in .NET and is ignored; add the opt-in packages (StaffOps.OtelHelper.Sql, .AWS, .Redis) instead variable=OTEL_HELPER_EXTRA_INSTRUMENTATION` |

The variable's own warnings stay separate: an `OTEL_HELPER_SAMPLE_RATIO` that is
not a plain decimal in [0, 1] also writes `sample ratio is not a number in
[0, 1]`, and an unknown `OTEL_HELPER_DISABLED_SIGNALS` entry its `invalid
OTEL_HELPER_DISABLED_SIGNALS` line. Examples and the migration steps:
[HOW-TO §10](HOW-TO.md#deprecated-variables-03-this-branch-task-c16).

### Derived Ports

0.2.x rebuilt every endpoint as `scheme://host:port` (path dropped, `:4317`
when no port, also for `http/protobuf`). 0.3 (this branch, task C7a) follows
the contract:

| Endpoint | Protocol | Resulting target |
|---|---|---|
| `http://collector` (URL, no port) | `grpc` | `http://collector:4317`, with a warning to set an explicit port |
| `http://collector` (URL, no port) | `http/protobuf` | `http://collector/v1/traces` (scheme default port, 80 or 443) |
| `https://gw.example.com/otlp` | `http/protobuf` | `https://gw.example.com/otlp/v1/traces` (path kept; a per-signal value is used as written) |
| `https://gw.example.com/otlp` | `grpc` | `https://gw.example.com:4317` (gRPC dials `host:port`; the path is ignored and warned once per variable, task C7b) |
| `collector` (schemeless) | `grpc` / `http/protobuf` | `https://collector:4317` / `https://collector:4318/v1/traces` (deprecated for `http/protobuf`, warned) |

### Endpoint parsing (0.3, this branch)

Every endpoint, from code or the environment, is parsed by RFC 3986, as in Go,
Python and Node.js (contract design D1, A17):

- **Removed with one warning:** URL userinfo, query and fragment (an empty `?`
  or `#` too). Put credentials in `OTEL_EXPORTER_OTLP_HEADERS`. An `@` in the
  path is used as parsed and warned ("misplaced credentials?"), naming the
  variable only.
- **Invalid:** a control character or a backslash anywhere, a scheme without
  `//` and a host (`http:///x`), a schemeless value starting with `/`, a port
  outside 1–65535 (port 0 included; `04318` is 4318), a space, `%` or IPv6
  zone id in the host, a host that is neither an IPv6 literal nor an RFC 3986
  name, a host that ends in a number but is not a canonical dotted quad
  (`127.1`, `1.2.3.256`), a host that fails IDNA, and for `http/protobuf` an
  invalid `%` escape in the path. Also invalid in .NET only: a host `System.Uri`
  refuses (sub-delims other than `-` and `_`, such as `a!b` or `a~b`, and empty
  labels such as `a..b`). From the environment: one warning that shows
  `<unparseable URL>`, and the next value is used
  (`OTEL_EXPORTER_OTLP_<SIGNAL>_ENDPOINT` → `OTEL_EXPORTER_OTLP_ENDPOINT` →
  none). From code: the host fails to start, naming the option.
- **Unsupported scheme** (`ftp://`, `unix:/path`): warned, and the signal has
  no OTLP exporter (an explicit per-signal choice is not overridden).
- **IDN hosts** are converted to punycode (UTS #46 non-transitional, the
  runtime's ICU: `bücher.de` → `xn--bcher-kva.de`); hosts are lowercased.
  Without ICU (globalization-invariant mode, e.g. chiseled or alpine images
  with `DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1`) a non-ASCII host is invalid
  (the warning says it needs ICU): write it in punycode. With ICU, a non-ASCII
  host with a label over 63 characters or starting or ending with `-` is
  invalid, as in Python (Go and Node.js accept it).
- **Paths are normalized by `System.Uri`** before they are sent (Go sends them
  as written): `/a/../v1` → `/v1`, `%7E` → `~`; `%2F` stays. See HOW-TO §17.
- Diagnostics show `scheme://host:port/path` at most, only `scheme://` when
  the path or query holds an `@`, and `<scheme>:` for `unix:/…`.

### TLS / Transport Security

Transport is determined by the endpoint scheme:

| Endpoint | Transport |
|----------|-----------|
| `https://host:4317` | TLS (system CA trust store) |
| `http://host:4317` | Plaintext (insecure) |
| `host:4317` (no scheme) | TLS (secure by default) |

`OTEL_EXPORTER_OTLP_INSECURE` applies **only to schemeless endpoints**: `true` → `http://`, anything else → `https://`. An explicit `http://` or `https://` scheme always wins (env or code). The variable is read by this helper, not by the .NET SDK.

0.3 (this branch, task C7a): `OTEL_EXPORTER_OTLP_<SIGNAL>_INSECURE` beats it
for one signal and `TelemetryOptions.Insecure` beats both. The value is
`true` or `false`, ASCII case-insensitive; anything else is false and warned,
also when no endpoint is configured. A setting that contradicts an explicit
scheme (`https://` with `true`, `http://` with `false`) is warned and the
scheme wins; `TelemetryOptions.Insecure` contradicting a code URL fails at
start. 0.2.x treated any value but `true` as false, silently.

For a local plaintext collector, use `http://` in the endpoint or set `OTEL_EXPORTER_OTLP_INSECURE=true`.

### Behavior when no OTLP endpoint is configured

When `OTEL_EXPORTER_OTLP_ENDPOINT` is **not set**, the library automatically falls back to:

| Signal | Behavior |
|--------|----------|
| Metrics | Exposed via Prometheus HTTP `/metrics` on port 9464 |
| Traces | In-process only (context propagation works, no export) |
| Logs | stdout: one JSON object per line (`time` UTC, `level`, `msg`, `logger`, values; no OTel export) |

The Prometheus metrics port is configurable (default 9464; precedence below).

This enables the standard Kubernetes pattern: deploy without a collector, and let Prometheus/VictoriaMetrics scrape `/metrics` directly from the pod.

The listener binds `http://+:<port>/metrics/`: every IPv4 address (the managed
`HttpListener` maps `+` to `IPAddress.Any`, as it did 0.2.x's `*`), so other
pods and containers can scrape it, and it serves a fresh collection on every
scrape. There is no IPv6 bind: on an IPv6-only cluster a scrape of the pod IP
fails (known limitation, unchanged from 0.2.x).

#### Prometheus fallback (0.3, this branch)

The helper honors the standard variables (task C12, contract A29; 0.2.x:
`OTEL_EXPORTER_PROMETHEUS_HOST` and `_PORT` had no effect). The upstream
exporter's own invalid-value warning for them is not forwarded: only the
helper's line below is written.

- **Port:** `TelemetryOptions.PrometheusMetricsPort` > `OTEL_EXPORTER_PROMETHEUS_PORT` >
  `OTEL_HELPER_METRICS_PORT` > 9464; both variables take `^\+?[0-9]+$` in
  [0, 65535] (`0` = no listener); an invalid value warns once (the echo loses
  URL userinfo and query) and the next level applies.
- **Host:** `OTEL_EXPORTER_PROMETHEUS_HOST` unset, `0.0.0.0`, `::` or `[::]`:
  every IPv4 interface (the `+` prefix). An IPv4 literal (no leading zeros):
  that address only. `localhost` (any case): `127.0.0.1`. An IPv4-mapped IPv6
  address binds its IPv4 address. Any other value (a host name, `*`, a URL, a
  port, a zone id) warns once and binds every IPv4 interface (fails open,
  owner decision). **.NET has no IPv6 bind** (fix round 1, 2026-11-11): the
  managed `HttpListener` rejects every IPv6 literal prefix (measured on .NET
  8.0.33 and 10.0.14). So `::1` / `[::1]` binds `127.0.0.1` with one warning;
  `::` / `[::]` binds every IPv4 interface with one warning; any other IPv6
  address gets **no listener** and one ERROR (never widened to every interface,
  never a failed start).
- **A failed bind** (a busy port, an address the machine does not own): one
  ERROR naming the address, the port and what set them, then the
  `MeterProvider` build fails with the upstream `InvalidOperationException`.
- **Scraping a host-bound listener:** the listener routes a specific-host bind
  by the request's `Host` header. A `127.0.0.1` bind (also `localhost` and
  `::1`) registers a `localhost` prefix too. `http://127.0.0.1:9464/metrics`
  always answers. `http://localhost:9464/metrics` answers on the `localhost`
  prefix, which binds the resolver's first loopback address (`::1` when the
  host has an IPv6 loopback, `127.0.0.1` with IPv6 off). Measured: Go
  (Prometheus, vmagent) and .NET `HttpClient` dial `::1` first and get 200;
  curl and Node.js resolve with `AI_ADDRCONFIG`, so in a container whose only
  IPv6 address is `::1` (Docker networks, many pods) they connect to
  `127.0.0.1` with `Host: localhost` and get 404: point them at
  `http://127.0.0.1:9464/metrics`. Any other address answers only requests
  naming it.
- **An application listener next to the helper's:** the helper uses named
  options (`StaffOps.OtelHelper.Prometheus`), so an application's own
  `AddPrometheusHttpListener()` keeps its options and port.
- **When the listener runs:** the `prometheus` metric exporter is active, the
  port is above 0, metrics and the SDK are enabled. Only then is the listener
  reader added, at provider build (a late `Configure<TelemetryOptions>` still
  decides it). So in an OTLP-only setup `MeterProvider.ForceFlush()` reports the
  OTLP result, but `Shutdown()` returns `true` even after the collector answered
  500 (upstream, measured); in dual mode `otlp,prometheus` both return `false` (a
  pull-only reader fails every collection outside a scrape, upstream). Only
  `services.ForceFlushOtelHelper()` is a reliable success signal (HOW-TO §15).
- **Disabled metrics:** `OTEL_HELPER_DISABLED_METRICS` patterns are
  case-sensitive globs on the instrument name (`*`, `?`, every other character
  literal), dropped from every exporter. 0.2.x matched case-insensitively and
  took `?` literally.
- **Exposition gaps** (upstream exporter, recorded as parity skips): a Gauge in
  unit `1` gets no `_ratio` suffix; colliding label values are joined in
  attribute order (`a_b="y;x"`), not sorted as in Go.
- **Requests the listener answers** (upstream `HttpListener`, parity
  `skip:no-upstream-dotnet-prom-listener`): any HTTP method and any path under
  `/metrics/`; Go, Python and Node.js answer only `GET`/`HEAD /metrics`. With
  Prometheus inactive (`none`, port 0, metrics or the SDK disabled) there is no
  listener and no mounted handler (parity `skip:no-upstream-dotnet-prom-mounted-handler`).
- **The lines** (examples; values are echoed redacted, at most 64 characters):

  ```text
  otelhelper: WARN invalid OTEL_EXPORTER_PROMETHEUS_PORT: invalid port (ASCII digits with an optional +, in [0, 65535]; 0 = no listener), using OTEL_HELPER_METRICS_PORT, else 9464 variable=OTEL_EXPORTER_PROMETHEUS_PORT value=9_100
  otelhelper: WARN invalid OTEL_EXPORTER_PROMETHEUS_HOST: invalid host, not applied: the listener binds every interface, wider than a restriction (use an IP address or localhost) variable=OTEL_EXPORTER_PROMETHEUS_HOST value=myhost
  otelhelper: WARN OTEL_EXPORTER_PROMETHEUS_HOST is the IPv6 loopback, which the .NET HttpListener cannot bind: the listener binds 127.0.0.1 (and answers requests for localhost) instead variable=OTEL_EXPORTER_PROMETHEUS_HOST value=::1
  otelhelper: WARN OTEL_EXPORTER_PROMETHEUS_HOST is the IPv6 unspecified address; .NET has no IPv6 or dual-stack bind: the listener binds every IPv4 interface variable=OTEL_EXPORTER_PROMETHEUS_HOST
  otelhelper: ERROR OTEL_EXPORTER_PROMETHEUS_HOST is an IPv6 address, which the .NET HttpListener cannot bind: no Prometheus listener runs (not widened to every interface); use an IPv4 address or localhost, or leave it unset (every IPv4 interface) variable=OTEL_EXPORTER_PROMETHEUS_HOST value=fd00::1
  ```

  "Every interface" in the host warning is every IPv4 interface on .NET (the
  `+` prefix). A failed bind writes one line of the form `otelhelper: ERROR the
  Prometheus listener cannot bind (port in use, or an address this machine does
  not own); change the port set by <source>; the MeterProvider build fails
  host=<address> port=<port> error=<OS text>` (with the host variable set, the
  hint also names it) and the build then fails; only an `HttpListenerException`
  writes it, any other reader failure propagates without the line.


### Standard OpenTelemetry SDK Variables

How the helper treats the standard variables on this branch. The helper reads its rows from the **process environment**; `OTEL_SDK_DISABLED` and `OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE` are read from `IConfiguration`, as the SDK reads them; the attribute limits and batch sizes stay with the SDK. A key of the helper's only in `appsettings.json`, on the command line or as a `DOTNET_`/`ASPNETCORE_` variable has no effect — the sampler, propagator and interval variables warn once, the OTLP rows are ignored without a warning (0.2.x let the SDK read them; full table: [HOW-TO §2](HOW-TO.md#where-the-helper-reads-its-settings-environment-appsettingsjson-code-03-this-branch-adr-013)). Each row says what 0.2.x did.

#### Signals and exporters (0.3, this branch, task C9)

| Variable | Description | Default |
|---|---|---|
| `OTEL_TRACES_EXPORTER`, `OTEL_LOGS_EXPORTER` | Comma-separated: `otlp` (needs an endpoint), `console` (the OpenTelemetry console exporter, on standard output), `none` (the signal is off). `logging` is a deprecated alias of `console` and warns; an unknown entry warns and is dropped; `none` with other entries warns and wins; only unknown entries count as unset. 0.2.x: not read. | `otlp` |
| `OTEL_METRICS_EXPORTER` | As above plus `prometheus`. `otlp` without a metrics endpoint warns and is dropped; when nothing is left, the default applies (0.2.x failed start). `TelemetryOptions.MetricExporters` wins; there `logging` or an unknown entry fails start. | `otlp` with an endpoint, else `prometheus` |
| `OTEL_HELPER_DISABLED_SIGNALS` | Deprecated. Disables each listed signal unless that signal's `OTEL_<SIGNAL>_EXPORTER` holds a valid entry (`none` included) or, for metrics, `TelemetryOptions.MetricExporters` is set; an unknown entry warns once, echoing it. `TelemetryOptions.DisabledSignals` wins over every variable. A set value is warned once as deprecated, also when a standard variable wins (task C16, [Deprecated variables](#deprecated-variables-03-this-branch-task-c16)). | _(unset)_ |
| `OTEL_METRIC_EXPORT_INTERVAL` | Interval of the OTLP and console metric readers, milliseconds: digits with an optional `+`, any length (`5_000`, `-1`, `0` and non-ASCII digits warn, 30 000 is used); above 2 147 483 647 clamped with a warning. Read from the process environment only: an `IConfiguration`-only value (`appsettings.json`) has no effect and warns once naming the variable. `TelemetryOptions.ExportIntervalMs` wins and fails start outside [1, 2 147 483 647]. 0.2.x: read as an `int`, invalid values ignored silently. | `30000` |
| `OTEL_METRICS_EXEMPLAR_FILTER` | `always_on`, `always_off`, `trace_based`; anything else warns. Read by the helper, which always sets the filter. | `trace_based` |
| `OTEL_SDK_DISABLED` | `true` (any case): no-op providers, no exporters, no Prometheus listener, propagators kept, one INFO line `otelhelper: INFO OTEL_SDK_DISABLED=true: telemetry disabled, only propagators installed`. Read from `IConfiguration` (which includes the environment in a host), as the SDK reads it; a value other than `true`/`false` warns and means `false` (`otelhelper: WARN invalid OTEL_SDK_DISABLED: not a boolean (true or false), using false variable=OTEL_SDK_DISABLED value=yes`). 0.2.x: handled by the SDK alone, no line. | `false` |
| `OTEL_CONFIG_FILE`, `OTEL_EXPERIMENTAL_CONFIG_FILE` | Not supported (contract A19): set, ignored with one warning per diagnostics sink (container) naming the variable, never the path: `otelhelper: WARN OTEL_CONFIG_FILE is not supported by the .NET helper and is ignored; configure it with environment variables or TelemetryOptions variable=OTEL_CONFIG_FILE`. 0.2.x: not read. | _(unset)_ |

#### Sampling

| Variable | Description | Default |
|---|---|---|
| `OTEL_TRACES_SAMPLER` | 0.3 (this branch, task C10): parsed by the helper, which always sets the sampler. The six spec names `always_on`, `always_off`, `traceidratio`, `parentbased_always_on`, `parentbased_always_off`, `parentbased_traceidratio`, ASCII case-insensitive (a non-ASCII spelling, such as U+0130 `İ` for `I`, is unknown); `jaeger_remote`, `parentbased_jaeger_remote`, `xray` and unknown names warn and the default applies. `TelemetryOptions.Sampler` and `SampleRatio` win. Process environment only: a value only in `IConfiguration` (`appsettings.json`) warns once and is not read. 0.2.x: read by the SDK only when no code sampler was set and the variable was in the process environment (an `IConfiguration`-only value was ignored silently); unknown names warned by the SDK only. | `parentbased_always_on` |
| `OTEL_TRACES_SAMPLER_ARG` | Ratio of `traceidratio` and `parentbased_traceidratio`: a plain decimal in [0, 1]; anything else warns and 1.0 is used. Ignored for the other samplers. Process environment only (an `IConfiguration`-only value warns once). | `1.0` |

#### Context Propagation

| Variable | Description | Default |
|---|---|---|
| `OTEL_PROPAGATORS` | 0.3 (this branch, task C10): read by the helper per token — `tracecontext`, `baggage`, `none` (ASCII case-insensitive, empty tokens and duplicates dropped). `b3`, `b3multi`, `jaeger`, `xray`, `ottrace` warn `propagator not supported by the .NET helper, ignored`, other tokens `unknown propagator, ignored`, one line per token naming the effective list (`using=`); nothing supported left → the default; `none` disables propagation and wins over other tokens (warned when combined). Installed process-wide also under `OTEL_SDK_DISABLED=true`, in both directions: the OpenTelemetry propagator, the runtime propagator `HttpClient` injects with (`DistributedContextPropagator.Current`) and the one ASP.NET Core extracts with all carry only the listed tokens — with `none` or `baggage` an inbound `traceparent` starts a new trace and nothing or only `baggage` is sent on; with `tracecontext` no `baggage` is sent (HOW-TO §13). The default list behaves as 0.2.x. A runtime propagator the application set is kept, with one warning. Process environment only (an `IConfiguration`-only value warns once). 0.2.x: not read; W3C TraceContext + Baggage always. | `tracecontext,baggage` |

#### OTLP Exporter

| Variable | Description | Default |
|---|---|---|
| `OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE` | Read by the helper from `IConfiguration`, else the environment (task C5): `cumulative`, `delta`, `lowmemory`, any case; a number or another value warns and gives `cumulative` (`otelhelper: WARN invalid OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE: unsupported temporality, using cumulative; valid values: cumulative, delta, lowmemory variable=OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE value=1`). An application `Configure<MetricReaderOptions>` wins. 0.2.x: read by the SDK, numbers included. | `cumulative` |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | Read by the helper: `grpc` or `http/protobuf` (ASCII case-insensitive). Unset → `http/protobuf` when the port of the endpoint a signal uses is `4318` (compared numerically; logged once at INFO), else `grpc`. `http/json` and unknown values are warned and ignored (0.2.x: `http/json` failed validation). | port inference |
| `OTEL_EXPORTER_OTLP_INSECURE` | Read by the helper, schemeless endpoints only (see TLS above). | _(unset)_ |
| `OTEL_EXPORTER_OTLP_HEADERS` | Additional headers (e.g., auth), `key=value,…`. 0.3 (this branch, task C8): parsed by the helper and sent on its own HTTP client (0.2.x: by the SDK, a malformed item failed start). Keys `[A-Za-z0-9._-]+`, not percent-decoded; values percent-decoded as a whole (an invalid escape or non-UTF-8 bytes keep the whole value literally, warned); a value outside printable ASCII is skipped; the last of a key wins. Warnings name the variable and the item position, never a value. A gRPC key ending in `-bin` is sent base64. | empty |
| `OTEL_EXPORTER_OTLP_TIMEOUT` | 0.3 (this branch, task C8): export timeout in decimal milliseconds (`^[0-9]+(\.[0-9]+)?$`), clamped to [1, 86 400 000] with a warning; `0`, a sign, an exponent or a bare dot warn and the default applies. It is the deadline of the exporter, the HTTP client, the batch processors and the periodic reader; gRPC sends it as `grpc-timeout`. `TelemetryOptions.ExportTimeoutMs` wins. 0.2.x: not honored (fixed 10 s). | `10000` |
| `OTEL_EXPORTER_OTLP_COMPRESSION` | `gzip` or `none` (ASCII case-insensitive); anything else warns and the default applies. 0.3 (this branch, task C8): default `gzip` (0.2.x: none). | `gzip` |
| `OTEL_EXPORTER_OTLP_CERTIFICATE`, `_CLIENT_KEY`, `_CLIENT_CERTIFICATE` | TLS files of an `https://` (or schemeless, secure) endpoint, read and validated by the helper (task C8): an unreadable file or a CA with no PEM certificate warns and the next level applies, then the system roots; the client key and certificate are one pair, and an incomplete or rejected pair warns and server authentication is used. On a plaintext endpoint the files are ignored with one warning naming them and the signals. A file over 1 MiB (or a device) is "cannot be read". Warnings name the variables, never a path or content. | _(unset)_ |
| `OTEL_BSP_EXPORT_TIMEOUT`, `OTEL_BLRP_EXPORT_TIMEOUT`, `OTEL_METRIC_EXPORT_TIMEOUT` | Not honored (task C8): the OTLP export timeout is the deadline of every signal; set, each warns once. | — |
| `OTEL_BSP_MAX_EXPORT_BATCH_SIZE` / `OTEL_BLRP_MAX_EXPORT_BATCH_SIZE` | Read by the SDK; above `OTEL_BSP_MAX_QUEUE_SIZE` / `OTEL_BLRP_MAX_QUEUE_SIZE` (defaults 512 / 2048) the queue size is used and one warning names both variables. | `512` |
| `OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT` (and `OTEL_SPAN_…`, `OTEL_LOGRECORD_…`) | Read by the OTLP exporter. A `0` is warned (contract: invalid), but the .NET exporter has no public override and still sends those values empty: unset it or use a positive limit. | no limit |
| `OTEL_EXPORTER_OTLP_<SIGNAL>_*` | Not used in 0.2.x. 0.3 (this branch): `_ENDPOINT` (used as written, no `/v1/<signal>`), `_PROTOCOL` and `_INSECURE` (task C7a), and `_HEADERS`, `_TIMEOUT`, `_COMPRESSION`, `_CERTIFICATE`, `_CLIENT_KEY`, `_CLIENT_CERTIFICATE` (task C8) are read by the helper and beat the generic variable for that signal; a per-signal `_HEADERS` replaces the generic headers (no merge). | — |

> **Redirects are refused** (0.3, this branch, task C8). The helper's OTLP client never follows a `3xx`: that export is dropped and one ERROR names the signal and the status code (`otelhelper: ERROR the OTLP endpoint for traces answered HTTP 307 (a redirect); redirects are not followed, … signal=traces status=307`), never the `Location` value. 0.2.x followed redirects. Configure the final endpoint URL.

---

## Behavior per Environment

| Environment | Trace Sampling | Log Level |
|---|---|---|
| `LOCAL` | 100% of roots (`parentbased_always_on`) | Debug |
| `DEV` | 100% of roots (`parentbased_always_on`) | Information |
| `HML` | 100% of roots (`parentbased_always_on`) | Information |
| `PRD` | 100% of roots (`parentbased_always_on`) | Warning |
| unset, or any other value | 100% of roots (`parentbased_always_on`) | Information (0.2.x: unset meant `LOCAL`, Debug) |

The name is matched after trimming, ignoring ASCII case only (`prd` = `PRD`; `İ` U+0130 or a no-break space U+00A0 do not fold).

> The SDK sends 100% of traces to the Collector in all environments. **Tail-based sampling is the Collector's responsibility** (Agent → Gateway), which decides what to keep based on errors, latency, and configured rate per environment.

---

## What is Instrumented Automatically

### Traces

| Instrumentation | What it captures |
|---|---|
| ASP.NET Core | Incoming HTTP requests (server spans; instrumentation scope `Microsoft.AspNetCore` — 0.2.x exported them under the service name, so check queries and Collector rules on `otel_scope_name`, task C5) |
| HttpClient | Outgoing HTTP requests (client spans) |
| gRPC Client | Outgoing gRPC calls: one CLIENT span per call, `pkg.Svc/Method` (0.3, this branch, task C11; 0.2.x also had the HttpClient `POST` span under it) |
| gRPC Server | Incoming gRPC calls, through ASP.NET Core: RPC spans only with the upstream key set by the app ([gRPC server spans](#grpc-server-spans)) |
| SqlClient | SQL queries — only with the `StaffOps.OtelHelper.Sql` subpackage (`AddOtelHelperSql()`) |
| AWS SDK | S3, SQS, SNS, DynamoDB calls, etc. — only with the `StaffOps.OtelHelper.AWS` subpackage (`AddOtelHelperAws()`; one CLIENT span per call, [AWS](#aws)) |
| Custom Sources | Spans created via `ActivitySource(serviceName)` |

> Exceptions on spans: database and AWS spans keep only `exception.type` in their `exception` event; the message and stack trace go to an ERROR log record correlated by `trace_id` / `span_id` (ADR-014, [Sql](#sql-databases), [AWS](#aws)). ASP.NET Core (gRPC server calls included) and HttpClient spans are not covered yet: their event still holds the message and stack trace (`RecordException`, HOW-TO §9); their review is contract task A37 (ADR-014 decision 4, all languages). In your own spans, put the type on the span and the detail in a log record written inside the activity (HOW-TO §5).

Sampler (0.3, this branch, task C10): `parentbased_always_on` in all environments (ADR-002, as Go, Python and Node.js): roots are sampled, a span with a parent follows the parent's sampled flag, so an unsampled caller stays unsampled. Configurable with `opts.Sampler`, `opts.SampleRatio` or `OTEL_TRACES_SAMPLER`. The sampler is set also when traces are off (`OTEL_TRACES_EXPORTER=none`, a disabled signal): the instrumentations stay registered, and `HttpClient` passes the inbound trace and its sampled flag on. 0.2.x: a bare `AlwaysOnSampler`, which ignored the parent's flag. Tail-based sampling is done in the Collector.

> Health paths (0.3, this branch, task C11, contract R7/D5, as Go, Python and Node.js): `/health`, `/healthz`, `/ready`, `/readyz`, `/live`, `/livez`, `/ping`, `/metrics` — or `TelemetryOptions.HealthPaths` — get no ASP.NET Core server span and no HttpClient client span. The request path is cut at `?`/`#`, percent-decoded (a malformed escape keeps the raw path), ASCII-lowercased and stripped of trailing slashes, then matched exactly: `/Health/`, `/healthz?probe=1`, `/%68ealth` and `/HEALTH%2f` are health requests, `/users/ping` and `/healthcare` are not. The `grpc.health.v1.Health` service (Check, Watch, List) gets no server or client span, whatever the list. Health requests are still counted in `http.server.request.duration` (the framework meter does not see the filter) and their child spans are dropped under the default sampler; HttpClient health calls sampled by the client are sent without the trace context the propagators write (no `traceparent` or `tracestate`, as Go's transport; C11 fix rounds 1 and 2; the application's own headers and propagated baggage are kept; an unsampled call keeps its `…-00` `traceparent`; HOW-TO §9) and stay in `http.client.request.duration`. 0.2.x: `/ping`, `/health`, `/healthz`, `/ready` only, without the trailing-slash, query or encoding rules.

#### gRPC server spans

Without `OTEL_DOTNET_EXPERIMENTAL_ASPNETCORE_ENABLE_GRPC_INSTRUMENTATION=true` in the application configuration, gRPC server spans are **HTTP-shaped**: named `POST /pkg.Svc/Method`, with the status taken from the HTTP status, which is 200 also when the RPC fails. With the key, ASP.NET Core emits RPC spans (semconv 1.42 in the 1.20.x instrumentation): name `pkg.Svc/Method` (the gRPC span name of every language, contract D1 row "gRPC span names"), `rpc.system.name`, `rpc.method`, `rpc.response.status_code`, and ERROR only for `UNKNOWN`, `DEADLINE_EXCEEDED`, `UNIMPLEMENTED`, `INTERNAL`, `UNAVAILABLE` and `DATA_LOSS` (contract A22). The key is experimental upstream (attribute changes per bump). When gRPC endpoints are mapped, traces are exported and the key is not `true`, the helper writes one warning at start.

gRPC calls are counted in `http.server.request.duration` with `http.response.status_code=200` **even when the RPC fails**: the .NET instrumentations have no RPC metric (`rpc.server.call.duration` exists in Go only), so error rates of gRPC services come from spans, not from this metric.

### Metrics

| Instrumentation | What it captures |
|---|---|
| .NET Runtime | GC, thread pool, JIT, assemblies |
| ASP.NET Core | `http.server.request.duration`; `http.server.active_requests` only with the HTTP opt-in switch (0.3, this branch) |
| HttpClient | `http.client.request.duration`; `http.client.active_requests`, `http.client.open_connections`, `http.client.connection.duration` and `http.client.request.time_in_queue` only with the HTTP opt-in switch |
| Other framework meters | Also exported (measured 2026-11-10): `kestrel.active_connections`, `kestrel.connection.duration`, `kestrel.queued_connections`, `kestrel.queued_requests`, `aspnetcore.routing.match_attempts`, `dns.lookup.duration`; Go, Python and Node.js have no counterpart (root README gap rows). Drop unneeded ones with `OTEL_HELPER_DISABLED_METRICS` |
| gRPC | No `rpc.*` metric: a gRPC client call is counted in `http.client.request.duration` as `POST`, status 200, `network.protocol.version=2`; a server call in `http.server.request.duration` with status 200 even when the RPC fails |
| Custom Meters | Business metrics via the keyed `Meter` (named after the service), any `Meter` named `<serviceName>` or `<serviceName>.<anything>` (0.3, this branch, N5), and the meters in `AdditionalMeters` |

Metrics are exported every **30 seconds** (overrides SDK default of 60s).

Exemplars enabled (`ExemplarFilterType.TraceBased`) — metric → trace correlation in Grafana. 0.3 (this branch, task C9): the helper reads `OTEL_METRICS_EXEMPLAR_FILTER` (`always_on`, `always_off`, `trace_based`; anything else warns) and always sets the filter, default `trace_based` — the .NET SDK's own default is `AlwaysOff`. An application `SetExemplarFilter` registered after `AddOtelHelper()` wins; one registered before it is overridden by the helper's, without a warning. `OTEL_DOTNET_EXPERIMENTAL_METRICS_EXEMPLAR_FILTER_HISTOGRAMS` has no effect, because the helper always sets a filter (it had none in 0.2.x either).

### Logs

| Feature | Description |
|---|---|
| OTLP Export | Logs sent via OTLP to the Collector |
| Trace Correlation | `traceId`/`spanId` automatic via `Activity.Current` |
| Formatted Message | Formatted message included |
| Scopes | ILogger scopes included (OTLP; the stdout line has none) |
| Stdout fallback | No logs exporter active (no endpoint, `OTEL_LOGS_EXPORTER=none`, logs disabled, `OTEL_SDK_DISABLED=true`): one JSON object per line on standard output, the line Go and Python write (0.3, this branch, task C13; HOW-TO §4) |
| Levels and framework filters | Code `MinimumLogLevel`, else your own default (`Logging:LogLevel:Default`, `SetMinimumLevel`), else the environment level (table above); `Microsoft.*` / `System.Net.Http*` at Error above Debug. All on the helper's own providers only, never on the application's (0.3, task C13, N9; 0.2.x set them on every provider; HOW-TO §4) |

> The lib adds to the logging pipeline the exporters `OTEL_LOGS_EXPORTER` selects: OTLP (the default, with an endpoint) and, with `console`, the OpenTelemetry console exporter (0.3, this branch, task C9). With neither active, its stdout provider (alias `OtelHelperStdout`) writes the contract JSON line instead; with one active it writes nothing. The application's own console provider and its filters stay the application's responsibility: keep the host's console provider and every record appears twice on stdout (call `builder.Logging.ClearProviders()` before `AddOtelHelper()` to keep only the JSON line).
> Console output is each SDK's native format, not a shared one: multi-line text in .NET, JSON in Go (one object per line) and Python (indented), `console.dir` objects in Node.js. Use it for local debugging, not as a log pipeline.

---

## Library Options (`TelemetryOptions`)

| Property | Type | Env Var | Default |
|---|---|---|---|
| `ServiceName` | string? | `OTEL_SERVICE_NAME` / `OTEL_RESOURCE_ATTRIBUTES` / `SERVICE_NAME` | `unknown_service:<process name>` + warning |
| `Environment` | string? (`DeploymentEnvironments.*` or any value) | `OTEL_RESOURCE_ATTRIBUTES` / `ENVIRONMENT` | none (attribute absent + warning) |
| `OtelCollectorEndpoint` | string? | `OTEL_EXPORTER_OTLP_ENDPOINT` | none (no OTLP export) |
| `DebugLevel` | bool? | `OTEL_HELPER_DEBUG_LEVEL` | `false` |
| `ExportTimeoutMs` | int? (in [1, 86 400 000]) | `OTEL_EXPORTER_OTLP_<SIGNAL>_TIMEOUT` / `OTEL_EXPORTER_OTLP_TIMEOUT` | `10000` (10s); the deadline of every signal |
| `ExtraInstrumentation` | string?, `[Obsolete]` | `OTEL_HELPER_EXTRA_INSTRUMENTATION` | none (no effect — use subpackages; set, one `otelhelper: WARN TelemetryOptions.ExtraInstrumentation has no effect in .NET; add the opt-in packages (StaffOps.OtelHelper.Sql, .AWS, .Redis) instead`) |
| `Sampler` | Sampler? | `OTEL_TRACES_SAMPLER` / `_ARG` | `ParentBasedSampler(AlwaysOnSampler)` (0.2.x: `AlwaysOnSampler`) |
| `MinimumLogLevel` | LogLevel? | — | `null` (auto per environment) |
| `ResourceAttributes` | Dictionary<string, object> | — | empty |
| `AdditionalActivitySources` | List\<string\> | — | empty |
| `TracesEndpoint`, `MetricsEndpoint`, `LogsEndpoint` | string? | `OTEL_EXPORTER_OTLP_<SIGNAL>_ENDPOINT` | none (per-signal endpoint, used as written; schemeless allowed) |
| `Insecure` | bool? | `OTEL_EXPORTER_OTLP_<SIGNAL>_INSECURE` / `OTEL_EXPORTER_OTLP_INSECURE` | `false` (schemeless endpoint → `https://`) |
| `Compression` | string? (`gzip`/`none`) | `OTEL_EXPORTER_OTLP_<SIGNAL>_COMPRESSION` / `OTEL_EXPORTER_OTLP_COMPRESSION` | `gzip` (0.2.x: `none`) |
| `SampleRatio` | double? (in [0, 1]) | `OTEL_HELPER_SAMPLE_RATIO` (deprecated) | none (`ParentBased(TraceIdRatioBased(ratio))` when set, 1.0 included) |
| `HealthPaths` | List\<string\>? (each ASCII, starting with `/`, no `?`, `#`, `,`) | — | `/health`, `/healthz`, `/ready`, `/readyz`, `/live`, `/livez`, `/ping`, `/metrics` (0.2.x: `/ping`, `/health`, `/healthz`, `/ready`); an empty list traces every HTTP path |
| `AdditionalMeters` | List\<string\> | — | empty (meters `<ServiceName>` and `<ServiceName>.*` are exported without it) |
| `HttpOptInMetrics` | bool? | `OTEL_HELPER_HTTP_OPT_IN_METRICS` | `false` |
| `ForwardSdkDiagnostics` | bool | — | `true` (SDK warnings and errors on stderr, redacted; [Helper diagnostics](#helper-diagnostics-standard-error)) |
| `MetricExporters` | string? (`otlp`, `prometheus`, `console`, `none`) | `OTEL_METRICS_EXPORTER` | `otlp` with an endpoint, else `prometheus` |
| `ExportIntervalMs` | long? (in [1, 2 147 483 647]) | `OTEL_METRIC_EXPORT_INTERVAL` | `30000` |
| `DisabledSignals` | string? (`traces`, `metrics`, `logs`) | `OTEL_HELPER_DISABLED_SIGNALS` (deprecated) | none |

> `ServiceName` resolves (0.3, this branch): `ServiceName` > `ResourceAttributes["service.name"]` > `OTEL_SERVICE_NAME` > `service.name` in `OTEL_RESOURCE_ATTRIBUTES` > `SERVICE_NAME` > `unknown_service:<process name>` with one warning. A blank value counts as not set at every level.
> `Environment` resolves: `Environment` > `ResourceAttributes["deployment.environment.name"]` > `OTEL_RESOURCE_ATTRIBUTES` > `ENVIRONMENT` > absent with one warning; a code or `OTEL_RESOURCE_ATTRIBUTES` value is emitted as written, a known `ENVIRONMENT` name (`LOCAL`, `DEV`, `HML`, `PRD`, any case) in its canonical spelling, any other `ENVIRONMENT` value as written.
> `service.instance.id`: `ResourceAttributes` > `OTEL_RESOURCE_ATTRIBUTES` > one id per process (the one the SDK's `AddService` generates).
> `ResourceAttributes` beat `OTEL_RESOURCE_ATTRIBUTES` per key; an empty key or a value the SDK cannot hold fails host start; `telemetry.sdk.*` always come from the SDK (a code value is ignored with a warning). The resource declares the schema URL `https://opentelemetry.io/schemas/1.43.0`. Attributes added by an earlier `ConfigureResource` are kept (they beat `OTEL_RESOURCE_ATTRIBUTES`, lose to `ResourceAttributes`); their `service.name` is replaced by the resolved one, with a warning; their `deployment.environment.name` and an explicit `service.instance.id` are kept when the helper resolves none of its own (the environment then also picks the log level), else replaced with one warning per key.
> **Register `ConfigureResource` before `AddOtelHelper()`.** The SDK applies resource callbacks in registration order with no later hook, so one registered after `AddOtelHelper()` overrides the helper's resource (an `AddService` there replaces `service.name` and drops the schema URL; a `SetResourceBuilder(...)` there replaces the whole resource of that signal). At host start, after the providers are built, the helper compares each provider's resource with what it set and warns once naming the signals and the changed keys; an app without a host (a bare `ServiceProvider`) gets no such check. An environment set by an earlier `ConfigureResource` applies to the signals whose resource carries it, and picks the log level; one set only by a later callback does not. In a host the "environment not set" warning is written at start, only when no signal's resource carries it (one "differs between signals" line names the signals that lack it otherwise); without a host it is written when a provider builds its resource.
> A blank code `ServiceName` or `Environment` (`""`, whitespace) fails host start; leave it `null` for "not set".
> Identity and `OTEL_RESOURCE_ATTRIBUTES` warnings are written once per service provider (one diagnostics sink per container), not once per process as in Go and Python.
> `MinimumLogLevel` when null: LOCAL=Debug, DEV/HML=Information, PRD=Warning, unset or any other value=Information; the name is matched ignoring ASCII case (`prd` = `PRD`). Use `GetDefaultLogLevel()` to query.
> `Sampler` default is `ParentBasedSampler(AlwaysOnSampler)` — override only if you have a specific reason. Precedence: `Sampler` > `SampleRatio` > `OTEL_TRACES_SAMPLER` > `OTEL_HELPER_SAMPLE_RATIO` > default.
> Options hold only what code sets (`null` = not set); a value set in code (also one bound from `IConfiguration`, [HOW-TO §2](HOW-TO.md#options-bound-from-configuration-count-as-code)) wins over the env var. The full 0.3 option list and the breaking changes: `CHANGELOG.md`, .NET migration guide.
> The identity keys (`service.name`, `deployment.environment.name`, `service.instance.id`) in `ResourceAttributes` must be strings. Under `OTEL_SDK_DISABLED=true` the start-time resource check is skipped. Each resource detector registered in an earlier `ConfigureResource` runs once per signal provider (three times).
> An earlier `ConfigureResource` with a detector factory (`AddDetector(sp => …)`): the helper keeps that builder and adds its attributes last, so the resource keeps the SDK's schema URL (1.44.0) and the SDK's own `OTEL_RESOURCE_ATTRIBUTES` / `OTEL_SERVICE_NAME` parse stays for keys the helper does not set (the SDK reads `+` as a space; one warning, below).
> Known residuals (C6, carried to the A13 docs pass): a host that is built but never started writes no environment warning; in a container without `IServiceProviderIsService`, both the build-time "not set" line and the start-time "differs between signals" line can appear (each once).

### Helper diagnostics (standard error)

The helper writes its own diagnostics to **standard error**, one line per
record, `otelhelper: <INFO|WARN|ERROR> <message> key=value…` (task C5, design
D-NET5). They never go through `ILogger`: collect the container's standard
error. A valid configuration writes no line, except the INFO lines that state a
decision (for example `OTEL_SDK_DISABLED=true`, the HTTP opt-in metrics, the
protocol inferred from port 4318).

- Echoed values are redacted (URL userinfo and query removed, secrets shown as
  `<redacted>`) and cut at 64 characters with `…`; header values and TLS file
  paths are never echoed. Control, line-breaking and bidi characters are
  escaped as `\uXXXX`, so a value cannot forge a second line.
- "Once" means once per diagnostics sink — one per service provider
  (container): two hosts in one process each write their lines.
- SDK warnings and errors (its `EventSource` events, failed exports included)
  are forwarded as `otelhelper: <WARN|ERROR> sdk <EventSource>: <text>`. A
  failed export's response body is replaced by `<response body omitted>`;
  configured header values of 4 characters or more (as in Go, Python and
  Node.js) and endpoint credentials become `<redacted>`; credential-like
  `name: value` / `name=value` pairs are masked, while an exception type
  followed by prose (`AuthenticationException: Cannot determine the frame
  size`) is kept. One listener serves the process; each event goes to the
  most recently started host that forwards. `ForwardSdkDiagnostics = false`
  turns forwarding off.
- Not forwarded: the Prometheus listener's cancel at shutdown, View-drop
  notices, the "not subscribed" notices for the runtime's
  `Private.InternalDiagnostics.*` meters (32 `msquic.*` lines on .NET 10,
  task C14c), and the SDK's own parse warnings for the variables the helper
  resolves and warns about itself (`OTEL_EXPORTER_OTLP_ENDPOINT`, `_PROTOCOL`,
  `_COMPRESSION`, the per-signal keys, `OTEL_METRIC_EXPORT_INTERVAL`,
  `OTEL_SDK_DISABLED`, `OTEL_EXPORTER_PROMETHEUS_*`).

Examples (verbatim from the code; values vary):

```text
otelhelper: INFO OTEL_SDK_DISABLED=true: telemetry disabled, only propagators installed
otelhelper: WARN no service name configured; set OTEL_SERVICE_NAME using=unknown_service:OrdersApi
otelhelper: WARN deployment.environment.name is not set; set it in OTEL_RESOURCE_ATTRIBUTES (e.g. deployment.environment.name=PRD); log level defaults to INFO using=absent
otelhelper: WARN resource attribute items are not key=value and were skipped variable=OTEL_RESOURCE_ATTRIBUTES items=2,4
otelhelper: WARN resource attribute values have an invalid percent-encoding and are kept literally variable=OTEL_RESOURCE_ATTRIBUTES items=3
otelhelper: WARN TelemetryOptions.ResourceAttributes["telemetry.sdk.name"] is ignored; the OpenTelemetry SDK sets telemetry.sdk.name
otelhelper: WARN service.name set by an earlier ConfigureResource is replaced by the helper's resolved value; set it in TelemetryOptions or the environment instead
otelhelper: WARN resource configuration registered after AddOtelHelper() (ConfigureResource or SetResourceBuilder) changed the helper's resource; register it before AddOtelHelper(), or set these values in TelemetryOptions signals=traces,metrics,logs changed=service.name,service.instance.id
otelhelper: WARN deployment.environment.name differs between signals: only an earlier ConfigureResource set it, not for every signal; set it in OTEL_RESOURCE_ATTRIBUTES or TelemetryOptions missing=metrics,logs
otelhelper: WARN resources with different schema URLs were merged, so the resource has none (for example, a resource added after AddOtelHelper(), such as AddService in a later ConfigureResource); register it before AddOtelHelper() old=https://opentelemetry.io/schemas/1.43.0 updating=https://opentelemetry.io/schemas/1.44.0
otelhelper: WARN an earlier ConfigureResource added a resource detector factory (AddDetector(sp => ...)); the helper keeps the builder and adds its attributes last, so the SDK's own OTEL_RESOURCE_ATTRIBUTES and OTEL_SERVICE_NAME handling stays for keys the helper does not set
otelhelper: WARN AddOtelHelper was called more than once; the later calls change nothing (configure options with Configure<TelemetryOptions> instead)
```

An invalid value **in code** fails host start (an invalid environment value
only warns): one ERROR line, then the host throws the
`OptionsValidationException` whose message follows `fails: `, for example

```text
otelhelper: ERROR invalid TelemetryOptions set in code; telemetry is not configured and host start fails: TelemetryOptions.ServiceName must not be empty (null means not set).
```

Every other line is listed next to its setting (endpoints and TLS: HOW-TO §17;
protocol: §18; headers, timeout, compression: §19; signals and exporters: §15;
sampler and propagators: §13; deprecated variables: below; Prometheus: above).

---

## Opt-in Subpackages

AWS, Redis, SQL, and Profiling instrumentations are **no longer bundled in core**. Install the subpackage you need:

| Package | Registration | What it instruments |
|---------|--------------|---------------------|
| `StaffOps.OtelHelper.AWS` | `services.AddOtelHelperAws()` | AWS SDK calls (S3, SQS, DynamoDB, etc.) |
| `StaffOps.OtelHelper.Redis` | `services.AddOtelHelperRedis()` | StackExchange.Redis commands |
| `StaffOps.OtelHelper.Sql` | `services.AddOtelHelperSql()` | SqlClient (spans and `db.client.operation.duration`), Npgsql and MySqlConnector (their own spans and metrics) — see [Sql](#sql-databases) |
| `StaffOps.OtelHelper.Profiling` | `services.AddOtelHelperProfiling()` | Pyroscope continuous profiling |

### Usage

```csharp
services.AddOtelHelper();

// Add only the instrumentations you need:
services.AddOtelHelperAws();
services.AddOtelHelperRedis();
services.AddOtelHelperSql();
```

Redis with explicit connection:

```csharp
services.AddOtelHelperRedis(connectionMultiplexer);
```

### AWS

`AddOtelHelperAws()` (task C14c) instruments the AWS SDK for .NET v4 through
`OpenTelemetry.Instrumentation.AWS` 1.20.3 (verified with `AWSSDK.Core` 4.0.3.3,
S3 4.0.14, DynamoDBv2 4.0.10.1, SNS 4.0.2.7, SQS 4.0.2.5 against a local fake
endpoint, and DynamoDB Local 2.6.1). Examples of a successful call, a failed
call with its correlated log record, the URL redaction and the warnings:
`HOW-TO.md` §14 "AWS".

| Item | .NET |
|---|---|
| Spans | one CLIENT span per call, `<Service>.<Operation>` (`S3.PutObject`, `SQS.SendMessage`), sources `AWSSDK.<Service>`; S3 `HEAD` is `S3.GetObjectMetadata` (the .NET request name) |
| HttpClient span under an AWS call | none (`SuppressDownstreamInstrumentation`, set by the helper; without it the HttpClient span's `url.full` path is the S3 object key) |
| AWS client created before the tracer provider is built | SDK spans without the instrumentation's attributes and without the suppression: the HttpClient child span comes back, but the helper replaces its URL path with `/` (no key; fix round 1). Create clients after the provider to get the instrumentation's attributes |
| Your own `AddAWSInstrumentation()` first | AWSSDK.Core keeps the first registration in the process: your options win; the HttpClient child span, if any, loses its URL path as above |
| Failed call | status `ERROR` without description; the helper owns `error.type` (a value set by `Enrich` or upstream is replaced): the AWS error code (`NoSuchKey`, `ValidationException`), else the modeled exception name (`QueueDoesNotExist` for SQS's dotted code), else the class of a non-SDK error (`HttpRequestException`), else `_OTHER`; `aws.error_code` and `exception.type` kept |
| Removed attributes | `aws.s3.key`, `aws.s3.copy_source`, `aws.s3.delete`, `aws.dynamodb.item_collection_metrics`, and the `exception.message` / `exception.stacktrace` attributes AWSSDK.Core writes on the span |
| `exception` event (ADR-014) | keeps only `exception.type`; message and stack trace in one ERROR log record of category `StaffOps.OtelHelper.Aws` with the span's `trace_id` / `span_id` (DynamoDB quotes request literals in its messages, measured) |
| SNS phone number | never on a span (1.20.3 records no destination for a phone publish). The error text of a failed call, which can include the phone number or item values, goes to the correlated log (or stdout without an exporter): treat the log pipeline as sensitive |
| Keys | the legacy RPC keys `rpc.system=aws-api`, `rpc.service`, `rpc.method=<Operation>`, `aws.s3.bucket`, `aws.dynamodb.table_names`, `db.system=dynamodb`, `aws.queue_url`, `aws.request_id`, `http.status_code` (the instrumentation's `V1_28_0` default); no `server.address` / `server.port`, `messaging.system`, `db.system.name` (root `README.md` gap row) |
| URL query (every HTTP span) | upstream text kept (`Redacted`; .NET 9+ client `?*`); with upstream redaction turned off the ten contract names (`X-Amz-Signature`, `X-Amz-Credential`, `X-Amz-Security-Token`, `access_token`, …) are written `REDACTED` (span URL attributes only, also `Uri` values; not free text) |

Tests: `TESTS.md` "AWS and URL redaction (task C14c)". Gaps: root `README.md`,
"Known emitted-telemetry gaps" (.NET AWS rows).

### Redis

`AddOtelHelperRedis()` (task C14b) instruments StackExchange.Redis through
`OpenTelemetry.Instrumentation.StackExchangeRedis` 1.20.1-beta.1 (verified with
StackExchange.Redis 2.6.122 on `redis:7.4.12-alpine`). Examples of the spans and
of counting failures: `HOW-TO.md` §14 "Redis".

| Item | .NET |
|---|---|
| `AddOtelHelperRedis()` | instruments the **last** registered unkeyed `IConnectionMultiplexer` (registered before or after the call); 0.2.x threw `NotSupportedException` at provider build |
| no multiplexer registered | building the tracer provider fails with `InvalidOperationException`, also with traces off; the check is skipped by a container without `IServiceProviderIsService` |
| keyed or several multiplexers | `AddOtelHelperRedis(connection)` once per multiplexer (a keyed-only registration is not found) |
| the same multiplexer twice | two registrations and two drain threads, still one span per command (the client keeps the last profiler); call once per multiplexer |
| factory registration | the factory runs when the tracer provider is built: the app connects to Redis at start |
| Span name | the command (`SET`, `HGET`; `EVAL` for a prepared `LuaScript`); a transaction is one `EXEC` span; `CreateBatch` commands give **no span** (StackExchange.Redis does not profile batches) |
| Keys, `OTEL_SEMCONV_STABILITY_OPT_IN` unset | `db.system=redis`, `db.system.name=redis`, `db.statement`, `db.operation.name`, `db.query.text`, `db.redis.database_index`, `server.address` / `server.port` (owner decision O8 (b), 2026-11-11: the helper adds `db.system.name`, `db.operation.name`, `db.query.text`) |
| Keys, `database/dup` | the same plus `db.namespace` (written by the instrumentation) |
| Keys, `database` | `db.system.name`, `db.operation.name`, `db.query.text`, `db.namespace`, `server.*`; no `db.system`, `db.statement` or `db.redis.database_index` |
| Statement (`db.statement`, `db.query.text`) | the command plus one `?` when anything follows (`SET ?`); values and `AUTH` arguments never; subcommands not kept (`CONFIG GET x` → `CONFIG ?`); `SetVerboseDatabaseStatements` makes upstream add the key or the Lua script, which the helper removes again |
| Failed command | **not** marked ERROR: no status, `error.type` or `exception` event (spans are built from the client's profiler after the command, without its result); error ratios and the service graph read 0% for .NET Redis — count failures in the application |
| `exception` event from an `Enrich` callback | keeps only `exception.type`; the detail goes to the `StaffOps.OtelHelper.Database` log record (ADR-014) |

Tests: `TESTS.md` "Redis (task C14b)" (the integration tests need compose
`redis-test`). Gaps: root `README.md`, "Known emitted-telemetry gaps" (.NET Redis rows).

### Sql (databases)

`AddOtelHelperSql()` (task C14a) instruments three drivers; the application brings
the driver, the package depends on none of them except through the SqlClient
instrumentation (`OpenTelemetry.Instrumentation.SqlClient` 1.20.2):

| Driver | Spans | Metrics | Verified at |
|---|---|---|---|
| Microsoft.Data.SqlClient / System.Data.SqlClient | `OpenTelemetry.Instrumentation.SqlClient` (exception events recorded: `exception.type` only on the span, the detail in a correlated log, ADR-014) | `db.client.operation.duration` | instrumentation 1.20.2 (no SQL Server in the tests: synthetic spans) |
| Npgsql | source `Npgsql` | meter `Npgsql` (`db.client.operation.*`, `db.client.connection.*`) | Npgsql 10.0.3 on `postgres:17.11-alpine` |
| MySqlConnector | source `MySqlConnector` | meter `MySqlConnector` (`db.client.connections.*`) | MySqlConnector 2.6.2 on `mysql:8.4.11` |

The Npgsql and MySqlConnector names are added to `AdditionalActivitySources` and
`AdditionalMeters` (a post-configure step, so a later `Configure<TelemetryOptions>`
does not drop them): call `AddOtelHelper()` too, and they follow its signal
switches (no source or meter while the helper exports nothing). No
`Npgsql.OpenTelemetry` package is needed: its builder extensions only add the
`Npgsql` source and meter.

The helper's sanitizing processor (core, before every exporter the helper adds;
the helper's exporters apply it again to each batch) handles the spans of these
three sources, also when the application subscribed them itself:

| Rule | Effect |
|---|---|
| Failed span (contract A25) | status `ERROR` kept, description removed (Npgsql writes the SQLSTATE or the exception message, MySqlConnector the error code or the message; SqlClient 1.20 writes none) |
| Query text | Npgsql and MySqlConnector record the command text as written: the literals of `db.query.text` and `db.statement` become `?` and comments are dropped (PostgreSQL rules for Npgsql, MySQL rules for MySqlConnector). SqlClient sanitizes its own |
| `db.connection_string` | removed (MySqlConnector legacy conventions, Npgsql 8 and 9): the connection string without `Password` but with every other key, `Certificate Password` included |
| `db.npgsql.data_source` | the value of every password key (`SSL Password`, …) written as `REDACTED`; a value that does not parse is removed |
| `db.query.parameter.*` | removed (SqlClient records bound values with `OTEL_DOTNET_EXPERIMENTAL_SQLCLIENT_ENABLE_TRACE_DB_QUERY_PARAMETERS`) |
| `db.user`; MySqlConnector `db.name`, `net.peer.name`, `net.peer.port` | `db.user` removed in every mode; the three MySqlConnector legacy keys also give `db.namespace`, `server.address`, `server.port` (removed in the stable mode) |
| `exception` events ([ADR-014](https://github.com/StaffOps/otel-libs/blob/main/specs/decisions/ADR-014-exception-detail-in-correlated-logs.md)) | keep only `exception.type` (and other non-detail attributes); the message and the stack trace go to one ERROR log record, category `StaffOps.OtelHelper.Database`, with the span's `trace_id` / `span_id` (OTLP), through the normal log pipeline and level rules |
| `db.system` / `db.system.name` (R10) | both on every span by default (SqlClient `mssql` / `microsoft.sql_server`, Npgsql and MySqlConnector `postgresql`, `mysql`), only the new key with `OTEL_SEMCONV_STABILITY_OPT_IN=database`; a MySqlConnector `db.statement` also gives `db.query.text`, and is removed in that mode |

On the `Npgsql` and `MySqlConnector` meters a view drops the pool-name attribute
(`db.client.connection.pool.name`, `pool.name`): an unnamed pool is named after
its connection string, `SSL Password` / `Certificate Password` included. The view
keeps an allowlist of semconv keys and merges the pools.

The database's error text is in the correlated log, not on the span (ADR-014;
Go, Python and Node.js follow in contract task A36). Gaps (root `README.md`, "Known emitted-telemetry
gaps"): system keys on driver metrics, the stricter query-text rule, the
MySqlConnector status seen by application processors, and exception events.
Configuration and examples: `HOW-TO.md` §14.

### Profiling (Pyroscope)

```csharp
services.AddOtelHelperProfiling();
```

The runtime loads Pyroscope's native profiler from environment variables it
reads when the process starts, so the helper cannot set them; it checks them.
Set them on the container (the two `.so` files come from the
`pyroscope/pyroscope-dotnet` image, e.g. `0.14.5-glibc-x86_64`):

```yaml
env:
  - name: CORECLR_ENABLE_PROFILING
    value: "1"
  - name: CORECLR_PROFILER
    value: "{BD1A650D-AC5D-4896-B64F-D6FA25D6B26A}"
  - name: CORECLR_PROFILER_PATH
    value: "/opt/pyroscope/Pyroscope.Profiler.Native.so"
  - name: LD_PRELOAD
    value: "/opt/pyroscope/Pyroscope.Linux.ApiWrapper.x64.so"
  - name: PYROSCOPE_PROFILING_ENABLED
    value: "1"
  - name: PYROSCOPE_SERVER_ADDRESS
    value: "http://pyroscope.example.com:4040"
  - name: PYROSCOPE_APPLICATION_NAME   # optional: the profiles' service_name
    value: "orders-api"
```

Behavior (task C15; measured 2026-11-12 with the native profiler 0.14.5 on
`linux/amd64`, details and every check in `HOW-TO.md` §14 "Profiling"):

- Nothing starts at registration. A hosted service checks the variables when
  the host starts; when all are met it switches on Pyroscope's CPU,
  allocation, contention and exception profiling and adds the span processor
  that writes `pyroscope.profile.id` (the span id) on root spans. The helper
  writes nothing on success; the native profiler writes its own
  `[<time> | info | …]` lines on standard error. Profiles go from the native
  profiler straight to `PYROSCOPE_SERVER_ADDRESS`, not through the Collector.
- When a check fails, one line on standard error (the helper's diagnostics
  channel, not `ILogger`), for example with nothing set:

  ```text
  otelhelper: WARN profiling prerequisites not met (CORECLR_ENABLE_PROFILING=1, CORECLR_PROFILER, CORECLR_PROFILER_PATH, LD_PRELOAD: no Pyroscope.Linux.ApiWrapper, PYROSCOPE_PROFILING_ENABLED=1, PYROSCOPE_SERVER_ADDRESS): the helper does not start the Pyroscope profiler and spans get no pyroscope.profile.id; set these variables in the container environment (Helm values or Dockerfile), the runtime reads them when the process starts
  ```

  The profiler is not started and the span processor is not added; traces,
  metrics and logs are unaffected and the host starts. The line names
  variables only, never values (`PYROSCOPE_SERVER_ADDRESS` may carry
  credentials).
- `CORECLR_ENABLE_PROFILING` must be exactly `1` (`true` does not load the
  profiler, measured); `CORECLR_PROFILER` must be the Pyroscope CLSID with
  braces (any case); the profiler path (`CORECLR_PROFILER_PATH_64` on x64 or
  `_ARM64` on Arm64 first, then `CORECLR_PROFILER_PATH`) must name an existing
  file; on Linux `LD_PRELOAD` must hold `Pyroscope.Linux.ApiWrapper`.
- The warning used to be an `ILogger` record of category
  `OtelHelper.Profiling`; that category no longer exists, so
  `Logging:LogLevel:OtelHelper.Profiling` filters have no effect.
- Install `StaffOps.OtelHelper.Profiling` at the same version as
  `StaffOps.OtelHelper` (it uses the core's diagnostics channel).
- Opt-in accessory: core does not depend on it; the native profiler is heavy
  to install and measured only on x64. Arm64, musl and the .NET 10 runtime
  are not verified; `DOTNET_EnableDiagnostics=0` is not detected, and a
  failed `Start()` still leaves `pyroscope.profile.id` on root spans. Open
  items are long-term TODOs (`HOW-TO.md` §14 "Status").

> **Note**: `OTEL_HELPER_EXTRA_INSTRUMENTATION` no longer turns anything on in core — only the subpackages above add SQL/AWS/Redis instrumentation.

---

## Architecture

```
Application (.NET SDK) → OTLP gRPC :4317 → Agent Collector → Gateway Collector → Tempo / VictoriaMetrics / Loki
```

All telemetry goes through the Collector. The SDK does NOT export directly to backends.

---

## Tests

### Run unit tests

From `dotnet/`, with the repository root mounted (the parity runner reads
`parity/cases.json`):

```bash
docker run --rm -v "$(git rev-parse --show-toplevel):/src" -w /src/dotnet mcr.microsoft.com/dotnet/sdk:8.0 dotnet test StaffOps.OtelHelper.Tests
```

Coverage: see [TESTS.md](TESTS.md) for the full list.

### Examples

See [example/README.md](example/README.md) for sample apps with distributed traces
(`dotnet-api` HTTP → `dotnet-backend` gRPC, `dotnet-process` workers) and
`dotnet-custom-signals` (custom spans, metrics and logs with the Go, Python and
Node.js names). Their Dockerfiles pack `StaffOps.OtelHelper` and restore the
`.nupkg` from a local feed, as a consumer does; the backend sets the O7 key
(`OTEL_DOTNET_EXPERIMENTAL_ASPNETCORE_ENABLE_GRPC_INSTRUMENTATION`, RPC-shaped
server spans).

### End-to-end smoke

```bash
SMOKE_LANGS=dotnet testing/e2e/smoke.sh
```

Packs the helper, builds `testing/e2e/dotnet-smoke` against the package and
exports to a real `otel/opentelemetry-collector-contrib` over gRPC,
http/protobuf with an `/otlp` path prefix, a per-signal traces endpoint and TLS
with a custom CA; checks the span `e2e-smoke-span`, the counter
`e2e.smoke.runs` and the WARN record `e2e-smoke-log` (correlated with the span)
at the right receiver, then scrapes the Prometheus fallback from another
container. Rows and failure output: [HOW-TO.md](HOW-TO.md#running-the-examples-and-the-e2e-smoke-task-c17).

---

## Supported Targets

- .NET 8 (LTS)
- .NET 10 (LTS) — via roll-forward

On this branch the libraries target `net8.0` only (the `0.3.0-rc.1` packages
too). The released `0.2.1` has `net8.0` and `net10.0` assets. Task C0, after
the release candidate and the soak, retargets the 0.3 line to `net10.0` only
(ADR-009); the migration guide gets a section for consumers still on .NET 8.

OpenTelemetry .NET 1.20.x (core and contrib, one generation; minimum
versions, NuGet semantics). Three packages exist only as prereleases, so the
helper depends on the newest prerelease of the matching minor: the contrib
instrumentations `OpenTelemetry.Instrumentation.GrpcNetClient` and
`OpenTelemetry.Instrumentation.StackExchangeRedis` (`StaffOps.OtelHelper.Redis`),
and the Prometheus listener `OpenTelemetry.Exporter.Prometheus.HttpListener`,
all `1.20.1-beta.1`. The core package also depends on the stable
`OpenTelemetry.Exporter.Console` 1.20.1 (0.3, this branch, task C9), used only
when an `OTEL_<SIGNAL>_EXPORTER` (or `MetricExporters`) lists `console`.

### .NET 10 Note

The lib is compiled for `net8.0` and runs on .NET 10 via roll-forward. Requirements:

- **Runtime image**: use `aspnet` (not `runtime`) — the lib depends on `Microsoft.AspNetCore.App` for ASP.NET Core instrumentation
- **Roll-forward**: add `DOTNET_ROLL_FORWARD=Major` env var if using a custom image:
  ```dockerfile
  ENV DOTNET_ROLL_FORWARD=Major
  ```

Moving an app to the .NET 9+/10 **runtime** changes three things in the
emitted telemetry; they come from the runtime and the upstream
instrumentation, not from the helper (same as the `0.2.1` release notes):

- **Runtime metrics** come from the built-in `System.Runtime` meter and are
  named `dotnet.*` (for example `dotnet.gc.collections`) instead of
  `process.runtime.dotnet.*`. Update dashboards and alerts that query the old
  names.
- **HttpClient `url.full`**: query redaction changes from per-value
  `?key=Redacted` to `?*` (the runtime's own redaction); userinfo was already
  removed on .NET 8.
- **ASP.NET Core 10** adds meters that the ASP.NET Core instrumentation
  subscribes to: `Microsoft.AspNetCore.Authorization`, `.Authentication`,
  `.Identity` and `.MemoryPool`, and the Blazor Components meters.

---

## Project Structure

```
StaffOps.OtelHelper.sln
├── Directory.Build.props               # Shared build settings: SourceLink, warnings as errors
├── Directory.Build.targets             # Package metadata of the five packed libraries
├── StaffOps.OtelHelper/                # Main library
│   ├── TelemetryExtensions.cs          # Entry point AddOtelHelper()
│   ├── ActivitySourceExtensions.cs     # StartRootActivity() for workers
│   ├── TracerSetup.cs                  # Tracing + health filter + sampler
│   ├── Tracing/
│   │   ├── DebugMarkerProcessor.cs     # debug=true attribute on entry spans
│   │   ├── HealthMatching.cs           # Health list and path normalization (server, HttpClient, gRPC)
│   │   ├── GrpcServerTracingCheck.cs   # One-time warning: gRPC endpoints without the upstream key
│   │   └── PropagatorSetup.cs          # OTEL_PROPAGATORS: OTel default + runtime HttpClient propagator
│   ├── MetricsSetup.cs                 # Metrics with exemplars
│   ├── LoggingSetup.cs                 # Logging via OpenTelemetry SDK; level and filter rules on the helper's providers
│   ├── Logging/JsonLineLoggerProvider.cs # Stdout JSON-line fallback (no logs exporter)
│   ├── Models/
│   │   ├── DeploymentEnvironments.cs   # Environment name constants
│   │   ├── TelemetryOptions.cs         # Code options (nullable)
│   │   └── TelemetryOptionsValidator.cs      # Code-value validation at start
│   ├── Resolution/                     # Env + code resolution (ResolvedTelemetry)
│   ├── Diagnostics/                    # otelhelper: stderr sink, SDK EventSource forwarding
│   ├── Exporters/                      # OTLP exporter options and the helper HttpClient
├── StaffOps.OtelHelper.{AWS,Redis,Sql,Profiling}/  # Opt-in subpackages
├── StaffOps.OtelHelper.Tests/          # Unit tests (xUnit) and the parity runner
├── StaffOps.OtelHelper.ParityDriver/   # Per-case child process of the parity runner (not packed)
└── example/                            # Sample apps
    ├── dotnet-api/        # API frontend
    ├── dotnet-backend/    # Backend with DB + external call
    ├── dotnet-process/    # Background worker
    └── dotnet-custom-signals/ # Custom spans, metrics and logs (cross-language names)
```
