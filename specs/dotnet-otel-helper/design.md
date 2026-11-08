# .NET OTel helper (Phase C) — design

**Status**: Reviewed r3 (2026-11-04): r1 reviewed in C1, r2 re-reviewed in
C1b, r3 confirmed in C1c (`dev`, `observability`, `spec-refuter`; no
BLOCKING/HIGH/MEDIUM) — C1 closed 2026-11-04 (tasks.md "r3 changes", "C1c
changes"). Spec:
[requirements](requirements.md) · [tasks](tasks.md). Contract:
[config-contract-v0.3 design](../config-contract-v0.3/design.md) (D1 table and
its dated rows are normative; this file only says how .NET meets them).
Resolution model: [ADR-013](../decisions/ADR-013-dotnet-helper-owns-env-resolution.md).

Conventions: "today" = branch `feat/v0.3-config-contract` at `c966d58`; paths
are relative to `dotnet/` unless they start with a top-level directory.
"Measured" = reproduced in Docker (`mcr.microsoft.com/dotnet/sdk:8.0` unless
stated) by this spec's author or the C1 `dev` review (probe sources kept in the
session scratchpad, not in the repo). `[TO VERIFY Cn]` marks a claim that task
Cn settles before it implements anything that depends on it.

Upstream citations: OpenTelemetry .NET core packages at tag `core-1.20.1`;
the Prometheus exporter at `coreunstable-1.20.1-beta.1` (its 1.20.1-beta.1
entries are still under "Unreleased" at `core-1.20.1`); contrib packages at
their own tags `Instrumentation.<Package>-<version>` (e.g.
`Instrumentation.AspNetCore-1.20.1`, `Instrumentation.AWS-1.20.3`,
`Instrumentation.StackExchangeRedis-1.20.1-beta.1`); `dotnet/aspnetcore` and
`dotnet/runtime` at `release/8.0` / `release/10.0`.

## Layout and public API (reference)

Projects after N13 (task C2): `StaffOps.OtelHelper` (core),
`StaffOps.OtelHelper.{AWS,Redis,Sql,Profiling}`, `StaffOps.OtelHelper.Tests`,
`StaffOps.OtelHelper.ParityDriver` (new, not packed), examples
`dotnet-api`, `dotnet-backend`, `dotnet-process`. Entry points stay
`services.AddOtelHelper()` / `AddOtelHelper(Action<TelemetryOptions>)` and
`AddOtelHelper{Aws,Redis,Sql,Profiling}()`.

> **Note 2026-11-04 (C2).** File and line references to `dotnet/OtelHelper*/…`
> in this spec set, the ADRs and older specs point at the pre-rename tree
> (commit `9c93bea`). After C2 the files live under `dotnet/StaffOps.OtelHelper*/`,
> and the new XML doc comments shifted the lines of `Models/TelemetryOptions.cs`,
> `Models/TelemetryOptionsValidator.cs`, `Models/DeploymentEnvironment.cs` and
> `TelemetryExtensions.cs` (for example `ServiceName` `:37` → `:57`). The
> references stay as recorded evidence.

`TelemetryOptions` today (`OtelHelper/Models/TelemetryOptions.cs`) and in 0.3
(all new members are added in C5, so the `skip:C5-no-api` cases can be mapped
from C5 on):

| Member today | 0.3 | Why |
|---|---|---|
| `string ServiceName = "my-service"` (`:37`), `[Required]` | `string? ServiceName` (null = not set) | ADR-003; "set in code wins" (DN4) |
| `DeploymentEnvironment Environment = LOCAL` (`:39`), enum `LOCAL/DEV/HML/PRD` (`Models/DeploymentEnvironment.cs`) | `string? Environment`; enum removed, `DeploymentEnvironments` constants `LOCAL`, `DEV`, `HML`, `PRD` | R4: unknown values verbatim, unset = absent |
| `string OtelCollectorEndpoint = ""` (`:41`) | `string? OtelCollectorEndpoint` + new `TracesEndpoint`, `MetricsEndpoint`, `LogsEndpoint` | R1/R2 (fixture code keys `traces_endpoint`…) |
| `bool DebugLevel` (`:46`) | `bool? DebugLevel` | DN4 |
| `string ExtraInstrumentation = "SQL"` (`:53`) | `[Obsolete]`, no effect, warned; repo tests/examples suppress CS0618 locally | no production caller (`:188` is called only by `TracingAndOptionsHelperTests.cs`, `TelemetryPipelineTests.cs`); Go G9 |
| `int ExportTimeoutMs = 10_000` (`:58`) | `int? ExportTimeoutMs`, code bound [1, 86 400 000] | G10 analog, A18 |
| `Sampler Sampler = new AlwaysOnSampler()` (`:64`) | `Sampler? Sampler` + new `double? SampleRatio` | R6 (fixture `sample_ratio`) |
| `LogLevel? MinimumLogLevel` (`:70`) | unchanged | |
| `Dictionary<string, object> ResourceAttributes` (`:76`) | unchanged type; empty key fails at start | A16 |
| `List<string> AdditionalActivitySources` (`:82`) | unchanged | |
| — | new `List<string> AdditionalMeters` | N5 |
| `string DisabledSignals = ""` (`:87`) | `string? DisabledSignals` (code value kept, not deprecated in code — as Go/Python) | A19 |
| `string DisabledMetrics = ""` (`:92`) | `string? DisabledMetrics`, contract glob (case-sensitive) | A21 |
| `string MetricExporters = ""` (`:99`) | `string? MetricExporters` | |
| `int? ExportIntervalMs` (`:105`) | `long? ExportIntervalMs`, code bound [1, 2 147 483 647] (an `int` cannot hold the out-of-range value `config.code_interval_above_max_fails` sets, C1b refuter H1; `int` assignments still compile) | A19 |
| `int PrometheusMetricsPort = 9464` (`:113`) | `int? PrometheusMetricsPort` | N8 |
| `string? OtlpProtocol` (`:120`) | unchanged (empty = not set, A17) | |
| — | new `bool? Insecure`, `string? Compression`, `List<string>? HealthPaths`, `bool ForwardSdkDiagnostics = true` | fixture code keys; DN3 |
| `IsSignalEnabled`, `ResolvedMetricExporters()`, `ResolvedOtlpProtocol()` (public, `:125-186`) | removed from the public type; resolution lives in an internal resolver | they answer from code-only state (D-NET4) |
| `static GetDefaultLogLevel(DeploymentEnvironment, bool)` (`:133`) | `GetDefaultLogLevel(string? environment, bool debug = false)` | R4 |
| `const …EnvVar` names (`:13-27`) | kept, plus the standard names the helper reads | |
| `TelemetryOptionsValidator` (public) | internal | validates code values only (D-NET6) |
| DI: unkeyed `ActivitySource`, `Meter` singletons (`TelemetryExtensions.cs:47-50`) | keyed singletons, key `OtelHelperKeys.Instrumentation`; unkeyed `ActivitySource` then resolves to `Microsoft.AspNetCore` in web hosts (silently), null in workers; unkeyed `Meter` null (measured, C1b dev M2) | N1, DN5 |

## Gap inventory (reference)

Each row: the contract rule, what .NET does today with evidence, the parity
cases that cover it (area prefixes from `parity/cases.json`, counts at
`c966d58`), and the task that closes it. The real per-case outcome is what
the C4 runner records.

### Identity and resource — R4, R5, D4, A16, A24 (46 cases → C6)

| Contract | .NET today | Cases |
|---|---|---|
| `service.name`: code > `OTEL_SERVICE_NAME` > `OTEL_RESOURCE_ATTRIBUTES` > `SERVICE_NAME` (deprecated, warned) > SDK `unknown_service*` + warning | `SERVICE_NAME` > `OTEL_SERVICE_NAME` > `"my-service"` (`Models/TelemetryOptionsPostConfigure.cs:15-21`); a code value equal to `"my-service"` counts as unset (`:15`; measured, C1 dev P9); the resolved name is added with `AddService` after the SDK env detector, so it **overrides** `service.name` from `OTEL_RESOURCE_ATTRIBUTES` (`TelemetryExtensions.cs:78`; later resource wins, `Resource.cs:88-108`; detector order `ResourceBuilder.cs:34-38`; measured, C1 dev P8). No warning anywhere. | `svc.*` (8), `identity.*` service-name rows |
| `deployment.environment.name`: code > `OTEL_RESOURCE_ATTRIBUTES` > `ENVIRONMENT` (deprecated) > absent + warning; unknown verbatim | Enum, default `LOCAL` (`Models/TelemetryOptions.cs:39`); unknown → `LOCAL`, `-` → `_` (`TelemetryOptionsPostConfigure.cs:24-33`); always emitted, overriding the env attribute (`TelemetryExtensions.cs:82-85`; measured `staging` → `LOCAL`) | `env.*` (6), `resource.env_attribute_not_overwritten`, `identity.*` environment rows |
| Log level: only `LOCAL/DEV/HML/PRD` switch; unset/unknown → INFO | unset/unknown resolve to `LOCAL` first, so → Debug (`TelemetryOptions.cs:137-144`; measured) | `log_level.*` (3) |
| `service.instance.id`: code > `OTEL_RESOURCE_ATTRIBUTES` > per-process id | SDK `AddService(..., autoGenerateServiceInstanceId: true)` adds one static GUID per process (`ResourceBuilderExtensions.cs:15, 38-71`) and **replaces** an env-provided id (measured) | `resource.instance_id_*` (2), `identity.blank_*_instance_id_generated` (2) |
| Prefork worker ids (D4, A24) | no prefork model in .NET | `fork.*` (2) → `skip:no-prefork-model` (as Go) |
| Helper-owned `OTEL_RESOURCE_ATTRIBUTES` parsing, no raw echo, blank values = not provided, `telemetry.sdk.*` not overridable, code empty key fails | SDK env detector parses it; code `ResourceAttributes` merged last (`TelemetryExtensions.cs:85-86`); **both** code and env override `telemetry.sdk.*` (measured: code `telemetry.sdk.name=hacked`, env `telemetry.sdk.language=x`, C1 dev P8/M2); no validation | `resource_attributes.*` (12), `resource.code_beats_resource_attributes_per_key` |
| Detector-selection variables ignored with a warning | no detector-selection variable in the SDK (`OpenTelemetry.dll` 1.20.1 strings: only the auto-instrumentation name `OTEL_DOTNET_AUTO_RESOURCE_DETECTOR_ENABLED`, C1 dev) | `resource.detector_var_ignored_warned` → `skip:no-detector-variable` (as Go) |

### Endpoints, protocol, TLS — R1, R2, D1.a–c, A17 (C7a: 59 resolve + 1 export case; C7b: 48 export cases)

| Contract | .NET today | Cases |
|---|---|---|
| Absolute URL kept (path, scheme default port for HTTP; gRPC portless → 4317 + warning) | `ResolveEndpoint` rebuilds `scheme://host:port`: path dropped, default port forced to 4317 for http and https alike (`TelemetryOptionsPostConfigure.cs:140-145`; measured: `https://otlp.example.com` → `https://otlp.example.com:4317`, `http://collector:4318/otlp` → `http://collector:4318`); HTTP appends `/v1/<signal>` to that (`TracerSetup.cs:58`, `MetricsSetup.cs:35`, `LoggingSetup.cs:33`) | `export.http_path_prefix`, `export.http_https_url_without_port_443`, `export.http_http_url_without_port_80`, `endpoint.grpc_url_without_port_warns`, … |
| Schemeless `host:port` → `https://` (`http://` with `INSECURE=true`), port default per protocol; HTTP deprecation warning | rewritten with the scheme from `INSECURE`, port 4317 for both protocols (`:147-163`); no warning | `export.schemeless_*`, `endpoint.schemeless_*` |
| IPv6 literal unchanged; userinfo/query stripped **with** a redacted warning | schemeless `[::1]:4317` → `https://[::1]:4317` (kept); userinfo and query dropped **without** a warning (`http://user:pw@collector:4317/?q=1` → `http://collector:4317`) — both measured | `export.ipv6_literal_*` (2), `redaction.endpoint_userinfo_query` |
| Per-signal env endpoints, protocol, insecure; "OTLP configured" per signal | not read; one endpoint for all signals; upstream `AddOtlpExporter` does not read per-signal variables (OTLP README lines 494-498, 532-536, 556-560) | `export.per_signal_*`, `export.code_per_signal_endpoint_verbatim` |
| TLS conflict: scheme wins + warning; `INSECURE` only for schemeless | scheme wins silently (`:141-145`); `INSECURE` parsed only as `"true"`, other values silently false (`:124-128`) | `tls.*` (4 in C7a; `tls.certificate_with_plaintext_warns` is C8), `export.tls_conflict_*` |
| RFC 3986 parsing rulings (A17): userinfo/query strip, backslash, zone ids, IDN punycode, leading-zero ports, port range, `<unparseable URL>` text | `Uri.TryCreate` only (`Models/TelemetryOptionsValidator.cs:17-19`); code `OtelCollectorEndpoint="collector:4317"` passes validation (measured) as a URI with scheme `collector` (N10); the failure message echoes the raw code value (`:19`) — an R8 gap when it holds userinfo | `endpoint.*` (27), `redaction.*` endpoint rows, `config.code_*` endpoint rows |
| Protocol: code > per-signal > generic env > port-4318 inference (INFO) > grpc; `http/json` env → warned, ignored | generic env and code only (`:116-121`); `http/json` or any unknown env value throws `OptionsValidationException` **inside `AddOtelHelper()`** (bootstrap provider, `TelemetryExtensions.cs:70-73`; validator `:56-63`; measured, C1 dev P1) | `protocol.*` (2), `export.http_port_4318_inferred`, `export.per_signal_endpoint_drives_4318_inference` |
| Endpoint path escapes kept verbatim | `%2F` kept on the wire with 1.20.1 (`RawUrl=/a%2Fb/v1/traces`, measured, C1 dev) | `export.http_path_escape_verbatim` — no skip needed |

### Exporter settings — R2, R8, G10, A18 (52 cases → C8)

| Contract | .NET today | Cases |
|---|---|---|
| Headers: helper parsing rules (malformed items skipped, encoded keys skipped, last wins, printable ASCII values, `-bin` keys as bytes), per-signal replaces generic, never logged | the helper sets no `Headers`; the SDK reads `OTEL_EXPORTER_OTLP_HEADERS` (generic only), unescapes the **whole** string before splitting (`OtlpExporterOptionsExtensions.cs:37`) and throws `ArgumentException("Headers provided in an invalid format.")` on an item without `=` (`:58`) and on a duplicate key — **the provider build fails** (measured on 1.15.3 and 1.20.1, C1 dev H4). Wire results (1.20.1): `k=%3D` → `=`; `k=%20v%2C1` rejected; `a=b=c` → `b=c`; `k= v ` → `v`. No header value is echoed in messages or `OpenTelemetry*` `EventSource` events (measured; 1.20.0-rc.1 stopped logging malformed headers, OTLP CHANGELOG lines 119-120) | `export.headers_*`, `export.per_signal_headers*`, `redaction.headers_*` |
| Timeout: ms, code > per-signal > generic > 10 s, clamped; the resolved value also bounds BSP/BLRP/reader | always `TimeoutMilliseconds = ExportTimeoutMs` = 10 000 unless code changes it (`TelemetryOptions.cs:58`, `TracerSetup.cs:65`, `MetricsSetup.cs:42`, `LoggingSetup.cs:40`), masking `OTEL_EXPORTER_OTLP_TIMEOUT` | `export.timeout_*`, `export.per_signal_timeout_*`, `export.batch_export_timeout_env_ignored_grpc`, `config.code_export_timeout_above_24h_fails` |
| Compression: code > env > helper default `gzip` | never set; 1.15.3 has no compression (added in 1.16.0, OTLP CHANGELOG lines 318-321); 1.20.1 sends no `Content-Encoding` unless set (measured) | `export.compression_*`, `export.code_compression_*` |
| TLS files read and validated by the helper; unreadable / non-PEM falls back; per-signal beats generic; dropped with a warning on plaintext | the SDK reads the generic certificate variables into the **internal** `MtlsOptions` (`OtlpExporterOptions.cs:309, 484-508`; mTLS since 1.15.0, ".NET 8.0+ only"); no per-signal handling, no warning on plaintext | `export.grpc_certificate_*`, `export.grpc_client_*`, `export.*certificate_with_plaintext*`, `tls.certificate_with_plaintext_warns`, `redaction.client_key_with_plaintext` |
| Value-length limit 0 invalid (warned, ignored); batch size above queue clamped | `OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT` is applied by the OTLP exporter at serialization, read from env even for directly constructed exporters: limit 3 sends `abc`, limit 0 sends an **empty** value (measured on the wire, C1 dev M6); the in-memory exporter shows the full value; the SDK reads `OTEL_BSP_*` / `OTEL_BLRP_*` (README lines 483-486, 545-548) | `config.attribute_value_length_*` (2), `config.bsp_*`, `config.blrp_*` |

### Signals, exporters, interval — R2, R8, A19 (36 cases → C9)

| Contract | .NET today | Cases |
|---|---|---|
| `OTEL_{TRACES,METRICS,LOGS}_EXPORTER`: `otlp`, `console`, `none` (+ `prometheus`), `logging` → `console` warned, unknown env warned, code unknown fails, `none` combined warned | only `OTEL_METRICS_EXPORTER` (`TelemetryOptionsPostConfigure.cs:64-70`); `console`, `logging` and any unknown value throw inside `AddOtelHelper()` (`TelemetryOptionsValidator.cs:31-47`; measured `Unknown metric exporter(s) 'console' in OTEL_METRICS_EXPORTER`); `OTEL_TRACES_EXPORTER` is a string in `OpenTelemetry.dll` 1.20.1 but `console` had no effect (measured, C1 dev) | `metrics.exporter_*` (5), `export.*_exporter_*` (9), `config.code_exporter_logging_fails` |
| Env `otlp` without a metrics endpoint warns; code fails | throws inside `AddOtelHelper()` (`TelemetryOptionsValidator.cs:49-52`) | `metrics.otlp_*_without_endpoint_*` (2) |
| Deprecated `OTEL_HELPER_DISABLED_SIGNALS`, standard var wins, unknown entries warn | supported, not deprecated, no precedence rule (`TelemetryOptionsPostConfigure.cs:98-103`, `TelemetryOptions.cs:125-127`) | `config.disabled_signals_*` (4), `config.code_disabled_signals_trimmed` |
| Interval `^\+?[0-9]+$`, > 0, clamped at 2 147 483 647, garbage warns | `int.TryParse` (measured: accepts `" 9464"` and `"+9464"`, rejects `9_464`), garbage ignored silently (`TelemetryOptionsPostConfigure.cs:73-80`) | `metrics.interval_*` (7), `config.code_interval_above_max_fails` |
| `OTEL_SDK_DISABLED`: no-op providers, propagators kept, one INFO | the SDK honors it from `IConfiguration`: `NoopTracerProvider`, `StartActivity` returns null, the default propagator stays the TraceContext + Baggage composite (measured, C1 dev); no INFO line | `config.sdk_disabled_*` (3), `export.sdk_disabled`, `propagation.sdk_disabled_propagators_kept` (C10) |
| `OTEL_CONFIG_FILE`, `OTEL_EXPERIMENTAL_CONFIG_FILE` warned once | strings present in `OpenTelemetry.dll` 1.20.1; a YAML resource file had no effect (measured) — re-check on every bump | `config.*config_file_warns` (2) |
| Exemplars: `OTEL_METRICS_EXEMPLAR_FILTER`, spec default `trace_based` | `SetExemplarFilter(ExemplarFilterType.TraceBased)` in code (`MetricsSetup.cs:21`) masks the variable; the .NET SDK default is `AlwaysOff` (SDK `docs/metrics/customizing-the-sdk/README.md:505-511`), so dropping the call would turn exemplars off | no case; unit test in C9 |

### Sampler, propagators, debug — R6, R9, A20, ADR-006 (32 cases → C10)

| Contract | .NET today | Cases |
|---|---|---|
| Default `parentbased_always_on`; `OTEL_TRACES_SAMPLER`/`_ARG` helper-parsed, unknown warns | default bare `AlwaysOnSampler` (`TelemetryOptions.cs:64`); the SDK reads the variable only when the code sampler is still an `AlwaysOnSampler` (`TracerSetup.cs:40-43`) | `sampler.*` (13) |
| `OTEL_HELPER_SAMPLE_RATIO` → `parentbased_traceidratio`, deprecated, invalid → warned and ignored | bare `TraceIdRatioBasedSampler`, value clamped silently, ignored at `1.0` (`TelemetryOptionsPostConfigure.cs:85-95`) | `sampler.ratio_alias_*`, `sampler.a20_*`, `config.code_sample_ratio_*` (4) |
| `OTEL_PROPAGATORS`: `tracecontext`, `baggage`, `none`; others warned per token | not read by the helper; string present in `OpenTelemetry.dll` 1.20.1 but `b3` had no effect (measured); SDK default composite TraceContext + Baggage (`Sdk.cs:24-27`) | `propagation.*` (10) |
| Debug: `debug=true` attribute on spans with no or a remote parent; no tracestate | attribute **and** tracestate `debug=true` prepended when `Activity.Parent == null` (`Tracing/DebugTraceStateProcessor.cs:17-23`): remote parents give `debug=true,debug=true` (measured, C1 dev P6, N2); in-process children started from an explicit `ActivityContext` (Redis, `RedisProfilerEntryToActivityConverter.cs:155-160`) also have `Parent == null` and are over-marked | `debug.*` (5) |
| `StartRootActivity` restores context | clears `Activity.Current`, never restores (`ActivitySourceExtensions.cs:16`; measured, N3) | no case; unit test in C10 |

### HTTP/gRPC instrumentation and health — R7, R10, D5, A22, N1 (19 cases → C11)

| Contract | .NET today | Cases |
|---|---|---|
| Health list `/health /healthz /ready /readyz /live /livez /ping /metrics`; exact, case-insensitive, trailing slash/query ignored, percent-decoded; overridable | `{"/ping","/health","/healthz","/ready"}` with `HashSet<string>(OrdinalIgnoreCase).Contains(Request.Path)` (`TracerSetup.cs:15-23`): no trailing-slash handling, four paths missing, no code option | `http.health_*` (10), `http.health_paths_override` (`skip:phase-c` → `skip:C5-no-api`) |
| Children of a dropped health span dropped; still counted in metrics | a `Filter` returning false calls `DisableActivity`, which clears `IsAllDataRequested` and the `Recorded` flag (`Implementation/HttpInListener.cs:257-261`), so ParentBased children are not sampled; `Filter` runs after the sampler (AspNetCore README lines 298-301); the request metric comes from the framework meter (README "Metrics": native since .NET 8) | `http.health_child_span_dropped`, `http.health_counted_in_metrics` |
| gRPC server: span per call, `grpc.health.v1.Health/*` no span, status per A22 | no gRPC server tracing configured: AspNetCore gRPC support is behind `OTEL_DOTNET_EXPERIMENTAL_ASPNETCORE_ENABLE_GRPC_INSTRUMENTATION`, read from the app `IConfiguration` (measured: in-memory key → enabled); the options constructor and the property setter are non-public, so no `IServiceCollection` extension can switch it on (C1 dev H3) — owner decision O7 (b), 2026-11-04: the app sets the key (D-NET8). With it on, AspNetCore emits RPC semconv 1.42 (span name `pkg.Svc/Method`, `rpc.system.name`, `rpc.method`, `rpc.response.status_code`, `error.type`) and ERROR only for the six A22 codes (`HttpInListener.cs:490-538`, `Shared/GrpcTagHelper.cs:143-160`) | `grpc.*` (7) |
| RPC server metrics count health calls | no RPC metric in the AspNetCore, GrpcNetClient and Http 1.20.x assemblies (string check: only `rpc.*` attribute keys, C1 dev); gRPC calls are counted in `http.server.request.duration` with status 200 even when the RPC fails | `grpc.health_counted_in_metrics`, `grpc.health_watch_counted_in_metrics` → `skip:no-upstream-rpc-metrics` |
| One CLIENT span per gRPC call | `AddHttpClientInstrumentation` and `AddGrpcClientInstrumentation` without `SuppressDownstreamInstrumentation` (`TracerSetup.cs:26-32`) → two CLIENT spans; since GrpcNetClient 1.20.0 the HttpClient span is parented to the gRPC span (CHANGELOG line 16) — a leaf CLIENT edge in the service graph and duplicate `http.client.*` series | no case; C11 test |
| Server spans under the instrumentation scope `Microsoft.AspNetCore` | the helper registers `new ActivitySource(ServiceName)` as an unkeyed DI singleton (`TelemetryExtensions.cs:47-48`); ASP.NET Core registers its source with `TryAddSingleton` (`GenericWebHostBuilder.cs:76`) and resolves the last registration — reproduced (C1 dev P5: `svc-a|Microsoft.AspNetCore.Hosting.HttpRequestIn|Server`) | no case; C11 test |
| Stable HTTP semconv metric and attributes | AspNetCore and Http instrumentations emit stable conventions only (`OTEL_SEMCONV_STABILITY_OPT_IN` support removed upstream) | `metrics.http_server_duration_histogram_name`, `http.non_health_traced_and_counted` |

### Prometheus fallback — ADR-007, A21, N7, N8 (22 cases → C12)

| Contract | .NET today | Cases |
|---|---|---|
| Listener `GET`/`HEAD /metrics` only, other paths 404, other methods 405 | 1.9.0-beta.2 on `UriPrefixes http://*:<port>/` (`MetricsSetup.cs:53-56`) answers 200 to `POST`/`PUT` (measured); 1.20.1-beta.1 with the `+` prefix answers 200 to `POST`, `PUT`, `DELETE`, `HEAD`, `GET` on `/metrics`, `/metrics/` and `/metrics/foo`, 404 on `/`, `/foo`, `/METRICS` (measured, C1 dev H2); no public collection API exists to serve `/metrics` differently (exported types: options, translation-strategy enum, builder extension) — owner decision O9 (a), 2026-11-04: both cases `skip:no-upstream-dotnet-prom-listener` | `metrics.listener_*` (2) |
| Port `^\+?[0-9]+$` in [0, 65535], code wins | env port parsed with `int.TryParse` and applied over code (`TelemetryOptionsPostConfigure.cs:47-49`; measured code 1234 + env `" 9999"` → 9999, N8) | `metrics.port_*` (4) |
| Bind all interfaces | 0.2.x binds `http://*:<port>/`; 1.20.1-beta.1 defaults `Host` to `localhost` (Prometheus README line 61) and **aborts** the MeterProvider build for `Host` `*`, `+` or `0.0.0.0` (`UriFormatException`, measured, C1 dev H1); working pattern: `ConfigureHttpListener` with `Prefixes.Clear()` + `http://+:{port}/metrics/` (README lines 180-213); `UriPrefixes` removed in 1.17.0-beta.1, `Host`/`Port` added in 1.16.0-beta.1 | e2e scrape from another container (C17; landed in C3, see the D-NET11 correction of 2026-11-05) |
| `OTEL_EXPORTER_PROMETHEUS_HOST` / `_PORT` | read by the 1.20.1-beta.1 listener options from env/`IConfiguration` (README lines 105-106); not ruled by the contract — owner decision O10 (a), 2026-11-04: ignored with one warning each (D-NET11); *superseded 2026-11-05 (contract A29): honored, C12* | no case |
| Disabled-metric glob: `*`, `?`, rest literal, **case-sensitive** | `Regex.Escape` + `*` → `.*`, `IgnoreCase` (`MetricsSetup.cs:62-65`); `?` literal | `metrics.disabled_metrics_*` (5) |
| Suffixes (`_total`, unit, Gauge unit `1` → `_ratio`), `target_info`, `otel_scope_name`, colliding labels | measured diff 1.9.0-beta.2 → 1.20.1-beta.1 (C1 dev M7): `target_info` added; timestamps dropped; `# UNIT` lines and `# EOF` dropped; a Gauge in unit `1` gets **no** `_ratio` suffix in either version. CHANGELOG 1.10.0–1.19.0: meter tags as labels (1.10.0), `otel_scope_*` labels on every series, invalid characters → `_`, `le` canonicalization; scrape responses cached 300 ms by default (`ScrapeResponseCacheDurationMilliseconds`, README line 114) | `metrics.scrape_*` (11) |

### Logging — D6, R4, A23, N9 (3 cases → C13)

| Contract | .NET today | Cases |
|---|---|---|
| No logs exporter → helper JSON line on stdout (UTC `Z`, reserved keys dropped, A23 level names); `console` → SDK console exporter | without an endpoint the OTel logger provider is added with no exporter (`LoggingSetup.cs:17-43`); stdout is whatever console provider the host has, not the contract line; `OTEL_LOGS_EXPORTER` unread | `logging.*` (3) |
| Framework categories untouched | `AddFilter("Microsoft", Error)` and `("System.Net.Http", Error)` on every provider above Debug (`LoggingSetup.cs:51-55`, N9) | no case; unit test in C13 |

### Deprecations — R8, D8 (6 cases → C16)

`SERVICE_NAME`, `ENVIRONMENT`, `OTEL_HELPER_SAMPLE_RATIO`,
`OTEL_HELPER_DISABLED_SIGNALS`, `OTEL_HELPER_EXTRA_INSTRUMENTATION`: read
without any warning (rows above). Cases `deprecation.*` (6).

### Opt-in packages — A25–A28, R10 database (no fixture kind → integration tests in C14)

| Contract (contract design D1 rows) | .NET today / upstream at the bumped version |
|---|---|
| DI overload works | `AddOtelHelperRedis()` (multiplexer from DI) throws `NotSupportedException` at provider build (`OtelHelper.Redis/OtelRedisExtensions.cs:17-21`; measured, C1 dev B1; the test avoids building the provider, `SubpackageExtensionsTests.cs:94-99`) |
| DB span ERROR without description (A25) | SqlClient 1.20.0: "Fixed exception messages being exported as the span status description" (SqlClient CHANGELOG line 33) — met upstream after C3 |
| DB error text in exception events (accepted residual) | `RecordException = true` (`OtelSqlExtensions.cs:17`) records the `SqlException` text in `exception.message` — kept deliberately, as Go (XSAM `RecordError`) |
| `db.system.name` + legacy `db.system` by default (R10) | SqlClient stable-only since 1.14.0-rc.1 (already so in 0.2.x: 1.15.2 — `db.system.name=microsoft.sql_server` is not a 0.2 → 0.3 change); StackExchange.Redis 1.20.1-beta.1 defaults to **legacy only** (`EmitOldAttributes=True`, `EmitNewAttributes=False`), and only the process environment switches it (an in-memory `IConfiguration` key had no effect; measured, C1 dev H6) — owner decision O8 (c), 2026-11-04: documented gap; `database/dup` is purely additive (C1 observability L6) |
| PostgreSQL and MySQL drivers (O5, 2026-11-04) | not instrumented; Npgsql and MySqlConnector emit native `ActivitySource`s `[TO VERIFY C14a: source/meter names and attribute keys at the pinned driver versions]` |
| Redis ERROR without description, reply code (A26) | spans built after the fact from `IProfiledCommand` never get a status, `error.type`, exception or reply code (`Implementation/RedisProfilerEntryToActivityConverter.cs:122-260`) → permanent gap |
| Redis span names: uppercased command (Node/Python) | the command as StackExchange.Redis formats it, e.g. `SET` (`RedisProfilerEntryToActivityConverter.cs:137-140`) — matches Node/Python `[TO VERIFY C14b: MULTI/pipeline names]` |
| Redis `db.operation.name` | only with the new conventions, i.e. when the deployment sets `database` or `database/dup` (O8 (c)) `[TO VERIFY C14b]` |
| AWS ERROR without description, `error.type` = AWS code (A27) | `SetStatus(Error, exception.Message)`, no `error.type`, an `exception` event (`Implementation/Tracing/AWSTraceSpan.cs:63-64`; measured `Connection refused (127.0.0.1:1)`); an `OnEnd` processor can clear the description and set `error.type` (measured) |
| AWS payload attributes (A27) | `aws.s3.key` recorded from the request `Key` (`AWSServiceHelper.cs:25, 35`; since 1.15.1, today's pin); `aws.dynamodb.item_collection_metrics` and the SNS phone number not recorded |
| AWS RPC keys and service attribute values (parity gaps) | default `SemanticConventionVersion.V1_28_0` (AWS README line 56): legacy `rpc.*` keys, as Python and Node `[TO VERIFY C14c: messaging.system and db.system.name values]` |
| URL query redaction, listed names as `REDACTED` (A28) | see D-NET14 (target-framework dependent) |
| Profiling starts with the host; prerequisites complete (N11) | `provider.Start()` at registration (`OtelHelper.Profiling/OtelProfilingExtensions.cs:28-29`); checks `CORECLR_ENABLE_PROFILING`, `PYROSCOPE_*` only (`ProfilingPrerequisites.cs:40-43`) |

### Count check

46 (C6) + 60 (C7a) + 48 (C7b) + 52 (C8) + 36 (C9) + 32 (C10) + 19 (C11) +
22 (C12) + 3 (C13) + 6 (C16) = **324**, every case of the fixture (C1
`spec-refuter` recomputed the same counts). The assignment rule is in
tasks.md "Case ownership"; 13 of these start as `skip:C5-no-api` (D-NET12).

## Upstream packages (reference)

Versions from the nuget.org registration indexes (2026-11-04).

| Package | Today | Target (C3) | net8.0 asset | Notes |
|---|---|---|---|---|
| `OpenTelemetry`, `.Api`, `.Extensions.Hosting`, `.Exporter.OpenTelemetryProtocol` | 1.15.3 | 1.20.1 | yes; net8.0 groups pin `Microsoft.Extensions.*` 8.0.0 | gzip ≥ 1.16.0; core 1.20.x breaking items for the C3 diff: exporter client no longer follows redirects (OTLP CHANGELOG 1.20.1-rc.1, lines 22-27), `byte[]` attributes truncated, event/link attribute-limit fallback, scope attribute limit (OpenTelemetry CHANGELOG 1.20.0) |
| `OpenTelemetry.Exporter.Prometheus.HttpListener` | 1.9.0-beta.2 | 1.20.1-beta.1 | yes | `Host` default `localhost`; `Host`/`Port` ≥ 1.16.0-beta.1; meter tags as labels ≥ 1.10.0 |
| `OpenTelemetry.Instrumentation.AspNetCore` | 1.15.2 | 1.20.1 | yes | gRPC behind an experimental flag; RPC semconv 1.42 (1.16.0) |
| `OpenTelemetry.Instrumentation.Http` | 1.15.1 | 1.20.1 | yes | on the .NET 9+ **runtime** HttpClient spans come from the runtime's `System.Net.Http` source (`IsNet9OrGreater = Environment.Version.Major >= 9`, `HttpHandlerDiagnosticListener.cs:24` at 1.15.1); the contrib "will not add/change/override any attributes" (Http README lines 43-48) |
| `OpenTelemetry.Instrumentation.GrpcNetClient` | 1.15.1-beta.1 | 1.20.1-beta.1 | yes | **breaking** 1.16.0: semconv 1.42, `rpc.grpc.status_code` → `rpc.response.status_code` (CHANGELOG line 66); **breaking** 1.20.0: context propagated regardless of `SuppressDownstreamInstrumentation` (line 16); `Filter` (1.19.1-beta.1) |
| `OpenTelemetry.Instrumentation.Runtime` | 1.15.1 | 1.20.1 | yes | on .NET 9+ subscribes to the built-in `System.Runtime` meter instead of emitting `process.runtime.dotnet.*` (Runtime README lines 52-55) — decided at **run time** (`Environment.Version.Major >= 9`, `MeterProviderBuilderExtensions.cs:14-15, 40` at 1.15.1), so any app on the .NET 9+ runtime sees it, roll-forward included — not a C0 change |
| `OpenTelemetry.Instrumentation.SqlClient` | 1.15.2 | 1.20.1 | yes | stable DB conventions only; no error text in status description (1.20.0) |
| `OpenTelemetry.Instrumentation.StackExchangeRedis` | 1.15.1-beta.1 | 1.20.1-beta.1 | yes | legacy attributes by default; `database`/`database/dup` from the process env |
| `OpenTelemetry.Instrumentation.AWS` | 1.15.1 | 1.20.3 | yes | `AWSSDK.Core [4.0.3.3, 5.0.0)` as in 1.15.1 (no AWSSDK major bump); default semconv `V1_28_0` |
| `Npgsql` (native tracing) / `Npgsql.OpenTelemetry` | — | driver of the app / 10.0.3 if needed | `Npgsql.OpenTelemetry` 10.0.3: `net8.0` group | O5; source names first, the package only if C14a needs its builder extension |
| `MySqlConnector` (native tracing) | — | driver of the app | — | O5; source/meter names only, no dependency |
| `OpenTelemetry.Exporter.InMemory` (tests) | 1.10.0 | 1.20.1 | yes | test only |

Sources: nuget.org registration and flat-container indexes; CHANGELOG, README
and source files at the tags listed under "Upstream citations" (fetched by the
author and by the C1 reviewers); .NET support dates from
`builds.dotnet.microsoft.com/dotnet/release-metadata/releases-index.json`
(8.0 and 9.0: end of support 2026-11-10; 10.0: 2028-11-14).

> **Corrected 2026-11-05 (C3).** (1) The `byte[]` truncation, the event/link
> attribute-limit fallback and the scope attribute limit are in the **OTLP
> exporter** CHANGELOG (`1.20.0-rc.1`, lines 130-145 at `core-1.20.1`), not in
> the `OpenTelemetry` one. (2) "No redirects" (OTLP CHANGELOG `1.20.1-rc.1`,
> lines 22-27) covers only the `HttpClient` the exporter creates itself: with
> `http/protobuf` and an app that registers `IHttpClientFactory`
> (`services.AddHttpClient()`, as the `dotnet-api` and `dotnet-process`
> examples do), the exporter's factory client still follows a `307` — measured
> through `AddOtelHelper` on 1.20.1: without the factory the span is lost at the
> redirect, with it the span arrives. The helper's own client (C8, ADR-013)
> makes it uniform. (3) The 1.9.0-beta.2 listener had no response cache
> (`ScrapeResponseCacheDurationMilliseconds` arrived in 1.16.0-beta.1), so
> D-NET11's cache 0 is the 0.2.x behavior, not a change.

## Migration guide inputs (reference, for C18)

API part — **first item**: unkeyed `ActivitySource` injection keeps compiling and, in web apps, silently resolves to the `Microsoft.AspNetCore` source (custom spans exported under that scope); in workers it is null — switch to `[FromKeyedServices(OtelHelperKeys.Instrumentation)]`. Then: the `TelemetryOptions` table above (incl. `long? ExportIntervalMs`), the package/namespace rename, `appsettings.json` `OTEL_*` keys no longer read for
helper-owned rows, bound options sections count as code, the helper's
`HttpClient` replacing the named `IHttpClientFactory` clients.

Telemetry part (C1 observability checklist; each row: before → after, and
what to check in dashboards/alerts):

1. `deployment.environment.name`: absent when unset (was `LOCAL`); unknown
   values verbatim, `-` kept (`PRD-BATCH` was `PRD_BATCH`).
2. `service.name`: default `unknown_service:<process>` (was `my-service`);
   `OTEL_SERVICE_NAME` beats `SERVICE_NAME`; `OTEL_RESOURCE_ATTRIBUTES`
   `service.name` respected.
3. `service.instance.id` from env kept (VictoriaMetrics `instance` label).
4. Log volume: unset environment → Information (was Debug); framework
   warnings visible (N9).
5. gRPC server spans — **only for apps that set the O7 key**: HTTP-shaped
   (`POST /pkg.Svc/Method`, status from HTTP 200) → RPC spans; apps without the
   key keep HTTP-shaped spans (a fleet can show both); spanmetrics `span_name`
   and `status_code` change (`dashboards/03-traces-reliability.json` uses
   both); error rate rises for the six A22 codes.
6. gRPC client spans: `rpc.system`/`rpc.service`/`rpc.grpc.status_code` →
   `rpc.system.name`/`rpc.response.status_code`; one CLIENT span per call.
7. Health paths `/live`, `/livez`, `/readyz`, `/metrics` and trailing-slash
   variants no longer traced.
8. Sampler `AlwaysOn` → `ParentBased(AlwaysOn)`: a caller sending
   `sampled=0` suppresses the .NET spans.
9. No `tracestate debug=true`: tail-sampling policies keyed on it stop
   matching (ADR-006).
10. Export transport: gzip on; endpoint path kept; HTTPS default port 443
    (was forced to 4317); redirects not followed.
11. Disabled-metric patterns case-sensitive.
12. Prometheus fallback: bind prefix and exposition changes (D-NET11).
13. Opt-in: AWS `aws.s3.key` dropped, `error.type` added, description
    removed; Redis keys unchanged by default (legacy only; new keys when the deployment sets `OTEL_SEMCONV_STABILITY_OPT_IN=database/dup`, O8 (c));
    Npgsql/MySqlConnector spans appear.
14. On the .NET 9+ **runtime** (any helper version, `net8.0` apps under
    roll-forward included — so already true for `0.2.x` on .NET 10, listed in
    the `0.2.1` release notes): runtime metrics `process.runtime.dotnet.*` →
    built-in `dotnet.*`; HttpClient `url.full` query → `?*`, userinfo removed;
    additional ASP.NET Core 10 meters (`Microsoft.AspNetCore.Authorization`,
    `.Authentication`, `.Identity`, `.MemoryPool`, subscribed by AspNetCore
    1.15.2, `AspNetCoreInstrumentationMeterProviderBuilderExtensions.cs:50-62`).
    C0 itself changes these only for consumers who were still on the .NET 8
    runtime.

## Decisions

Each: choice, justification, trade-offs, when wrong, reversal action,
alternatives.

### D-NET0 — `0.2.1`: dual target, no behavior change on the same runtime (owner decision O1, 2026-11-04)
**Choice**: a `.NET`-only `0.2.1` on a maintenance branch `release/dotnet-0.2`
cut from tag `v0.2.0` (`33d66f0`, the commit .NET 0.2.0 was published from;
`main` adds only `.github/FUNDING.yml` and a Go fix since — `git diff --stat
v0.2.0 main -- dotnet .github`). Its content: `<TargetFrameworks>net8.0;net10.0</TargetFrameworks>`
on the five libraries and the test project (test project
`Microsoft.Extensions.DependencyInjection` / `.Hosting` 9.0.0 on `net8.0`, 10.0.0
on `net10.0` — OpenTelemetry 1.15.3's `net10.0` group requires 10.0.0, so 9.0.0
would give `NU1605`); OpenTelemetry stays on 1.15.x; no library code change;
no Redis DI fix; CI and packaging changes listed in tasks.md C-patch. The
branch plan, the forward port and the owner's steps are in tasks.md C-patch.
**Version `0.2.1`** (C1b refuter L4): no .NET `0.2.1` exists (published .NET
versions: `0.2.0` from `v0.2.0`, plus `0.1.0-dev-*` builds); versions diverge
per language (ADR-004), so Go's `0.2.1` is no reason to skip one.
**"No behavior change" means on the same runtime** (C1b observability N-H1):
the helper has no target-framework branches (no `#if`, `Environment.Version`
or `RuntimeInformation` in `dotnet/OtelHelper*`), and NuGet resolves the
OpenTelemetry packages for the consuming app's framework, so on a given
runtime the `net8.0` and `net10.0` assets emit the same telemetry (evidence:
that source check — no CI leg runs the `net8.0` asset on runtime 10). Moving an
app to the .NET 10 runtime changes three things (upstream runtime checks,
already true for 0.2.0 under roll-forward): runtime metrics from `System.Runtime`
(`dotnet.*`), HttpClient `url.full` query `?*` and userinfo removed, extra
ASP.NET Core 10 meters (migration item 14). They go in the `0.2.1` release
notes and the README ".NET 10 Note"; a test (no library change) asserts
`process.runtime.dotnet.*` names when `Environment.Version.Major < 9` and
`dotnet.*` names otherwise, so a leg that silently rolled forward fails. The Profiling package on .NET 10 is not validated (it depends on the
native Pyroscope 1.2.0 profiler, which unit tests do not load): release-note
line.
**Measured 2026-11-04** (author and C1b `dev`, repo copy in containers): SDK
10.0.403 builds both TFMs, 0 errors, no `NU1605`; the 52 warnings are all
`CS1591` in the core (26 members × 2 TFMs, pre-existing); tests 179/179 on
runtime 10.0.14 and 179/179 on runtime 8.0.33 (net8.0 binaries from the SDK
10 build, run on `sdk:8.0`); with the runtime-metric test added and the plan
applied to the `v0.2.0` tree itself, **180/180** on each leg (net8.0 on
runtime 8.0.33, net10.0 on 10.0.14, SDK 10.0.403, per-TFM coverage 95.96%;
C1c dev); `net10.0` line coverage 95.96%; each package
packs exactly `lib/net8.0/` and `lib/net10.0/` with identical dependency
groups (pack adds the pre-existing `NU5104` warnings for prerelease
dependencies); SDK 8 cannot build the dual target even with `-f net8.0`
(`NETSDK1045`), so every CI leg installs the 10.0 SDK.
**Justification**: (1) the owner asked for no behavior change and a release
within days; (2) the 1.20.1 bump changes telemetry (Prometheus bind host,
redirects, gRPC client conventions — D-NET1) and needs C3's diff; (3) the Redis
fix changes a public overload's behavior and needs its own tests (C14b) —
the release notes give the workaround (pass the multiplexer).
**Trade-offs**: a maintenance branch to keep until 0.3.0; the 180/180
prototype of 2026-10-27 used OTel 1.20.1, not 1.15.3 (C1 refuter M1) — the
measurement above replaces it for this shape. **When wrong**: a 1.15.x
package fails on `net10.0` at runtime. **Reversal action**: withdraw `0.2.1`
guidance (consumers stay on 0.2.0 + roll-forward) and ship the 1.20.x bump
after C3's diff. **Alternatives**: tag on `feat/v0.3-config-contract` and
revert in the same PR (rejected: the release would carry 0.3 docs and CI,
and DN1 / the `sdk:8.0` commands would be false between the two commits);
cut from `main` (equivalent code, but `main`'s CHANGELOG `[Unreleased]` holds a
Go entry; rejected only for clarity); bump OTel in the patch (rejected:
behavior changes).

> **Corrected 2026-11-04 (C-patch code-review, PR #12).** (1) URL userinfo is
> not a .NET 9+ change: `OpenTelemetry.Instrumentation.Http` 1.15.1 already
> builds `url.full` from `uri.Authority`, which excludes userinfo, on .NET 8;
> the only HttpClient change on the .NET 9+ runtime is query redaction
> `?key=Redacted` → `?*`. (2) The runtime-metric names alone cannot detect a
> leg that rolled forward (upstream picks the names by the same
> `Environment.Version.Major` check); the test detects it by asserting the
> runtime major equals the test assembly's TFM major, and keeps the name
> assertions to pin upstream behaviour.


### D-NET1 — Version set: OTel 1.20.x on net8.0 first (owner decision O6, 2026-11-04), net10.0 last
**Choice**: C3 moves every `OpenTelemetry*` reference to the 1.20.x
generation (contrib to the matching 1.20.x / 1.20.x-beta) **while still
targeting `net8.0`** (the `0.2.1` dual target is not carried into 0.3 — C0
drops `net8.0` anyway); test deps `Microsoft.Extensions.*` 8.x,
`OpenTelemetry.Exporter.InMemory` 1.20.x. C3 records the before/after
`/metrics` and span diff of the three examples and walks the breaking items in
the package table.
**Justification**: (1) gzip — a contract default — needs exporter ≥ 1.16.0;
(2) every 1.20.x package ships a `net8.0` asset; (3) the runner (C4) records
statuses against the SDK the release ships.
**Trade-offs**: five minors of upstream changes in one task.
**When wrong**: a 1.20.x package drops `net8.0` before C0.
**Reversal action**: pin to the last version with a `net8.0` asset, or move
C0 forward. **Alternatives**: stay on 1.15.x and reimplement gzip in a client
handler (rejected: ADR-001); bump after C0 (rejected: the runner would record
twice).

### D-NET2 — The helper owns resolution and builds its exporters → [ADR-013](../decisions/ADR-013-dotnet-helper-owns-env-resolution.md)
ADR-013 holds the choice, the "neutralizing SDK-read values" table, the rows
left to the SDK and their source, the exemplar-filter exception, the HTTP
client duties and the named-client migration consequence. Measured in C1b
(dev M1): the helper `HttpClientFactory` reaches gRPC and `DefaultRequestHeaders`
arrive intact, comma included — so r2's `%25` re-encoding fallback and the
`skip:no-upstream-dotnet-grpc-header-comma` candidate are dropped. New
constraint: HTTP/protobuf exports call the synchronous `HttpClient.Send`, so
the helper client is built on `SocketsHttpHandler` (or a `DelegatingHandler`
forwarding both `Send` and `SendAsync`), with a test per protocol in C8; one
client per exporter instance. TLS through that handler on gRPC is a C8 wire
test (`export.grpc_certificate_*`, `export.grpc_client_*`); only if it fails
do those cases get `skip:no-upstream-dotnet-grpc-tls-files`.

### D-NET3 — Eager registration, deferred values (N4)
**Choice** (r2 after C1 dev B1; r3 after C1b dev M4): two phases.
- **Eager, inside `AddOtelHelper()`**: every call that registers services —
  `AddAspNetCoreInstrumentation`, `AddHttpClientInstrumentation`,
  `AddGrpcClientInstrumentation`, runtime metrics, the logging provider, and
  the Prometheus HttpListener reader, **always**. An "off" signal is
  "instrumentation registered, no exporter, reader or source".
- **Deferred, at provider build**: values only — `IConfigureOptions<T>` /
  `IPostConfigureOptions<T>` that depend on a `ResolvedTelemetry` singleton
  (instrumentation `Filter`s, Prometheus listener options), and builder calls
  that do not touch services inside sp-aware callbacks: `AddSource`,
  `SetSampler`, `ConfigureResource`, `AddMeter`, `AddView`,
  `SetExemplarFilter`, `AddProcessor`/`AddReader` with exporters the helper
  constructs itself. Prototyped end to end in C1b (dev): all three signals
  exported (gzip), a late `Configure<…>` honored (custom span under the late
  source name), the deferred `Filter` dropped `/healthz`.
- **Prometheus "no listener"**: when the resolved state has no listener (port
  0, `prometheus` not among the metric exporters, metrics off, SDK disabled),
  the deferred `ConfigureHttpListener` clears the prefixes and adds none —
  measured: the provider builds, nothing listens on the port, a scrape is
  refused (C1b dev M4). The cost is an idle reader. So the existence of the
  listener follows the resolved options like every other value, late
  `Configure` and bound options included (DN4); no eager decision, no
  contradicting-value warning.
- The internal `BuildServiceProvider()` (`TelemetryExtensions.cs:70-73`) is
  removed; `ResolvedTelemetry` is computed once, from `IOptions<TelemetryOptions>.Value`.
**Justification**: registration-time service calls throw inside sp-aware
callbacks (measured, C1 dev B1); values-only deferral keeps N4 for everything
else; directly constructed exporters are what ADR-013 needs anyway.
**Trade-offs**: instrumentations (and the idle Prometheus reader) exist for a
signal that is off. **When wrong**: a measured case shows an "off" signal
still exporting or allocating noticeably, or an empty-prefix listener
misbehaving on a platform. **Reversal action**: decide signal on/off eagerly
from env plus the `configure` delegate, documenting that late `Configure`
changes values only. **Alternatives**: keep the bootstrap provider (rejected:
N4, double construction); fully deferred registration (rejected: throws);
eager Prometheus decision (r2; rejected in r3: runs the user delegate twice
and ignores bound options).

### D-NET4 — Options API: nullable code fields, one breaking release
**Choice**: the API table above. Every option with an env counterpart becomes
nullable and holds **only** the code value; the resolver keeps the effective
values privately (Python's model). `Environment` becomes `string?`; the enum is
removed; `ExportIntervalMs` becomes `long?` so a code value above the bound
reaches the validator (C1b refuter H1); `ExtraInstrumentation` stays `[Obsolete]` with no effect (CS0618
suppressed in the repo's own tests/examples). The public `Resolved*` /
`IsSignalEnabled` methods are removed; the parity driver reads
`ResolvedTelemetry` through `InternalsVisibleTo`. Precedence: explicit code >
standard `OTEL_*` env > `OTEL_HELPER_*` / legacy env > helper default
(AGENTS.md principle 6). A section bound from `IConfiguration` into
`TelemetryOptions` is code (it beats env) — tested, documented.
**Justification**: (1) sentinel comparison makes a code value equal to the
default lose to env (measured, C1 dev P9); (2) the rename already breaks every
`using`; (3) the same rule in Go and Python. **Trade-offs**: source-breaking;
binding from `appsettings.json` now beats env, the reverse of the usual .NET
order. **When wrong**: consumers need binary compatibility, or expect env to
beat bound settings. **Reversal action**: `[Obsolete]` shims for the 0.2
members for one minor; for binding, an opt-in that treats bound values as
defaults below env. **Alternatives**: keep the enum and add `EnvironmentName`
(rejected: two properties for one value); `Optional<T>` wrappers (rejected).

### D-NET5 — Diagnostics channel: one stderr line, `otelhelper:` prefix
**Choice**: every helper diagnostic goes through one DI-registered sink
(`IOtelHelperDiagnostics`, injectable `TextWriter`, per-sink "once"
deduplication) with a static fallback writer for paths that run before the
host exists; each record is one line on standard error:
`otelhelper: <LEVEL> <message>`. Values pass one redaction function (R8),
truncation (D1 row "Echoed values are truncated") and escaping of CR, LF,
other C0/C1 controls, DEL, U+2028 and U+2029 (Node D-JS5 rule). An in-process
`EventListener` forwards `OpenTelemetry-*` `EventSource` events at Warning and
above to the same sink with origin `sdk` (redacted; off with
`ForwardSdkDiagnostics = false`); the parity driver records origin `helper`
as `helper_log` and both origins as `diagnostics`. Tests use their own sink
instance, never `Console.SetError` (racy under parallel xUnit).
**Justification**, strongest first: (1) the diagnostics are produced while
providers are built, before an app's logging exists, and possibly by the very
`ILoggerFactory` the helper configures — `ILogger` would route them into the
OTLP pipeline they describe and through N9-style filters and the PRD
`Warning` minimum; (2) parity with Go (package stderr logger plus
`otel.Handle` for SDK errors) and Node (stderr sink plus a `DiagLogger` at
WARN); (3) without forwarding, SDK export failures stay inside `EventSource`
and are invisible (principle 8). **Trade-offs**: not in the app's structured
logs; an `EventListener` costs a little at runtime. **When wrong**: users need
helper warnings in their log backend with trace correlation. **Reversal
action**: an opt-in replay of the same records into an `ILogger` category
`StaffOps.OtelHelper` after host start. **Alternatives**: `ILogger` only
(rejected: reason 1); an `EventSource` `StaffOps-OtelHelper` as a second
channel (not adopted: AGENTS.md asks for one channel; it can be added later as
the reversal of a "no listener" complaint).

### D-NET6 — Parsing rule: env warns and falls back, code fails at start
**Choice**: the resolver parses env values with the contract grammars and
warns (D-NET5) on unknown/unparseable values, using the default; the validator
(`IValidateOptions<TelemetryOptions>`, `ValidateOnStart`) checks **code values
only** and fails host start with a message naming the option (redacted, DN3).
Today's env-driven throws inside `AddOtelHelper()` (`TelemetryOptionsValidator.cs:31-63`
via `TelemetryExtensions.cs:70-73`) become warnings when the value came from
env.
**Justification**: principle 8 and R8. **Trade-offs**: an env typo no longer
stops a deployment. **When wrong**: an owner wants a strict mode.
**Reversal action**: a strict-mode switch would be a contract change (Go +
Python first, ADR-004). **Alternatives**: keep failing on env (rejected: R8).

### D-NET7 — Resource: rebuilt by the helper, earlier app configuration kept
**Choice** (r2 after C1 observability H1, refuter H2, dev M3; r3 after C1b dev
M3, observability LOW-1): in its `ConfigureResource` callback the helper
1. calls `Build()` on the builder it receives to capture what earlier
   callbacks contributed (app attributes, detector packages) — measured to
   work without factory detectors (C1b dev) — and drops from that snapshot a
   key only when its value equals what the SDK defaults or the env detector
   produce (the helper recomputes those with its own A16 parser; an app value
   for the same key differs and is kept, because later resources win);
2. calls `Clear()` (`ResourceBuilder.cs:51-55`) and adds back, in this
   precedence order (later wins): `AddTelemetrySdk()`; the helper's parsed
   `OTEL_RESOURCE_ATTRIBUTES` (A16 rules); the kept earlier app attributes
   (an app's own `ConfigureResource` is **code**, so it beats env); code
   `ResourceAttributes` per key; `service.name` — the resolved name (a name
   from an earlier app `AddService` is replaced by it, with one warning), or
   `unknown_service:<process name>` explicitly with the ADR-003 warning (the
   SDK default lives in the detector list `Clear()` empties,
   `ResourceBuilder.cs:35-38, 135-148`, and `AddService` throws on an empty
   name, `ResourceBuilderExtensions.cs:47`, measured); `deployment.environment.name`
   only when resolved; `service.instance.id` through `AddAttributes` (code >
   env > one per-process GUID);
3. keeps the schema URL the default builder would give the resource (exported
   since OTLP 1.17.0), checked against the contract row "`telemetry.sdk.*` and
   the resource schema URL" in C6.
**When `Build()` throws** (an earlier `AddDetector(sp => …)` factory detector:
`NotSupportedException: IResourceDetector factory pattern is not supported when
calling ResourceBuilder.Build() directly`, measured even inside a callback
registered from a deferred tracer callback, C1b dev M3): the helper does **not**
call `Clear()`; it appends its attributes last (later wins, `Resource.cs:88-108`),
so the app's detectors are kept, and warns once that strict A16 handling is
reduced (blank or invalid env items the SDK detector accepted stay in the
resource unless the helper overrides the same key). `[TO VERIFY C6: which
common detector packages register factory detectors]`. C6 tests the
precedence order and both paths.
**Justification**: the default builder lets later resources win, so the SDK
env detector cannot be ranked below the helper's values except by rebuilding;
a plain `Clear()` loses app attributes and the `unknown_service` default
(measured, C1 dev M3); `Build()` is all-or-nothing, so the factory-detector
path must not clear. **Trade-offs**: an extra `Build()` per provider; reduced
A16 strictness when factory detectors exist. **When wrong**: apps rely on
detector order the helper cannot reconstruct. **Reversal action**: a
`TelemetryOptions.ResourceDetectors` hook run after the helper's attributes.
**Alternatives**: plain `Clear()` (rejected); keep the SDK env detector
(rejected: its parse and ordering are not the contract's).

### D-NET8 — Health: instrumentation `Filter`; one gRPC client span
**Choice**: HTTP and gRPC server health requests are dropped by the ASP.NET
Core instrumentation `Filter` with the contract normalization (percent-decode,
cut query/fragment, ASCII-lowercase, trailing slashes) on the raw request
path, plus `/grpc.health.v1.Health/Check|Watch`; HTTP client spans to health
paths stay excluded with the same normalization. gRPC server tracing (owner
decision O7 (b), 2026-11-04): the **app** sets
`OTEL_DOTNET_EXPERIMENTAL_ASPNETCORE_ENABLE_GRPC_INSTRUMENTATION=true` in its
configuration (env or `appsettings.json` — the instrumentation reads the app
`IConfiguration`); the helper never mutates configuration or uses reflection.
Conditions (C1b observability): (1) the helper detects gRPC endpoints without
the key and warns once — at `ApplicationStarted` it scans `EndpointDataSource`
endpoints for metadata whose type name is `Grpc.AspNetCore.Server.GrpcMethodMetadata`
(by name, so the core takes no `Grpc.AspNetCore` dependency) and reads the key
from `IConfiguration` as a boolean, as the instrumentation does (C1b dev M7)
`[TO VERIFY C11: the metadata type name at the pinned Grpc.AspNetCore]`;
(2) the README says default gRPC server spans are HTTP-shaped (`POST
/pkg.Svc/Method`, status from HTTP 200); (3) migration item 5 is conditional
on the key; (4) the parity driver and the examples set the key, so the
`grpc.*` cases measure the opt-in configuration (D-NET12). gRPC client calls produce one CLIENT span: the HttpClient
instrumentation skips requests the gRPC instrumentation already covers
(`SuppressDownstreamInstrumentation = true`, or an HttpClient `Filter` on
`Content-Type: application/grpc` if 1.20.0's propagation change defeats the
suppression `[TO VERIFY C11]`).
**Justification**, strongest first: (1) the request metric is recorded by the
framework meter, independent of the tracing `Filter`, so R7's "still counted"
holds; (2) the `Filter` path clears `Recorded` (`HttpInListener.cs:257-261`),
so ParentBased children are dropped; (3) on ASP.NET Core 8 the server
activity is created without tags (`HostingApplicationDiagnostics.cs:399-408`,
release/8.0), so a sampler has no path to match until ASP.NET Core 10 with
`SuppressActivityOpenTelemetryData=false` (`:412-414`, release/10.0); (4) Go
emits one client span per call. **Trade-offs**: with an explicit `always_on`
sampler children of a filtered request are sampled again (the D5 limit).
**When wrong**: a measured case shows the filter suppressing
`http.server.request.duration`. **Reversal action**: the D5 sampler wrapper
when the app runs on the ASP.NET Core 10 shared framework (creation tags follow
the runtime's shared framework, not the target framework — C1b observability
LOW-4). **Alternatives**: sampler wrapper now (rejected: no path
on net8).

### D-NET9 — Sampler, propagators and the debug marker
**Choice**: the resolver parses `OTEL_TRACES_SAMPLER`/`_ARG` (the six spec
names, A20 ASCII folding) and always calls `SetSampler` — code sampler, else
the parsed one, else `ParentBasedSampler(AlwaysOnSampler)`; `SampleRatio` and
the deprecated `OTEL_HELPER_SAMPLE_RATIO` become
`ParentBasedSampler(TraceIdRatioBasedSampler(x))`. `OTEL_PROPAGATORS` is
parsed per token and installed with `Sdk.SetDefaultTextMapPropagator`
(`Sdk.cs:57-61`), also under `OTEL_SDK_DISABLED=true`. The debug processor
sets only the attribute, on `ParentSpanId == default || HasRemoteParent`
(C1 observability L1, refuter M6), and never writes `TraceStateString`.
**Justification**: ADR-001's sampler row, ADR-002, ADR-006; `Activity.Parent`
is null for in-process children started from an explicit context.
**Trade-offs**: the helper sets the process-wide default propagator, as Go
and Node replace globals. **When wrong**: an app sets its own default
propagator before `AddOtelHelper`. **Reversal action**: skip the propagator
when it is not the SDK default and warn. **Alternatives**: let the SDK read
the sampler variable (rejected: no ParentBased default, no named warning).

### D-NET10 — Logging: helper JSON-line provider as the stdout fallback
**Choice**: when no logs exporter is active (no endpoint, or
`OTEL_LOGS_EXPORTER=none`) the helper adds no OTLP exporter and adds its own
`ILoggerProvider` writing the contract JSON line to stdout (D1 rows "Stdout
fallback time zone/fields"; `level` uses the four words of the other
languages: `Trace`/`Debug` → `DEBUG`, `Information` → `INFO`, `Warning` →
`WARN`, `Error`/`Critical` → `ERROR` — Python `_slog_level`
(`python/staffops_otel_helper/_console.py:44-53`, "CRITICAL -> ERROR"), Node
`STDOUT_TEXT` (`nodejs/packages/otel-helper/src/internal/logging.ts:67-76`);
OTLP records keep the SDK severity number and text, `TRACE` … `FATAL`); `console` adds the SDK
console exporter. The helper does not remove the app's providers; the HOW-TO
shows `builder.Logging.ClearProviders()` for apps that want only the contract
line. N9: framework-noise filters only on the helper's providers. Scope name:
the category (accepted A23 difference, as Python and Node).
**Justification**: D6/G3; one JSON object per line for Kubernetes pipelines;
a Loki `| json | level="ERROR"` alert must also see .NET `Critical` (C1b observability N-M2).
`[TO VERIFY C13: the built-in JSON console formatter's field names]`
**Trade-offs**: duplicate stdout lines when the app keeps the default console
provider. **When wrong**: users prefer the framework formatter. **Reversal
action**: map the contract fields onto a custom `ConsoleFormatter`.
**Alternatives**: rely on the host console provider (rejected: not the
contract line).

### D-NET11 — Prometheus: upstream listener on an explicit `+` prefix
**Choice** (r2 after C1 dev H1/H2, observability H9/M6/M7, refuter H6; r3
after C1b): C3 moves to 1.20.1-beta.1 and configures the listener with `Port`
= the resolved port and
`ConfigureHttpListener = (o, l) => { l.Prefixes.Clear(); l.Prefixes.Add($"http://+:{o.Port}/metrics/"); }`
(Prometheus README lines 180-213; `Prefixes.Clear()` first, as written there)
— never `Host = "*"` (aborts) and never the `localhost` default (unreachable
from other pods); measured reachable on the container IP (C1b dev). When the
resolved state has no listener, the same delegate clears the prefixes and
adds none (D-NET3). `ScrapeResponseCacheDurationMilliseconds = 0` so a scrape
right after a measurement sees it (measured: cache 0 → fresh, 300 → stale);
every scrape then runs a collection, so two concurrent scrapers (e.g. two
vmagents) each trigger one — acceptable for the fallback path. Method/path
rules: owner decision O9 (a), 2026-11-04 — `metrics.listener_other_path_404`
and `metrics.listener_get_head_only` are `skip:no-upstream-dotnet-prom-listener`
(the data is read-only; 0.2.x is already permissive). `OTEL_EXPORTER_PROMETHEUS_HOST`
/ `_PORT`: owner decision O10 (a), 2026-11-04 — ignored, one warning naming the
variable (value truncated): "`OTEL_EXPORTER_PROMETHEUS_HOST` has no effect: the
helper binds `http://+:<port>/metrics/`; use `OTEL_HELPER_METRICS_PORT`"
(and the equivalent for `_PORT`); the helper's `Port` wins because its
configure delegate runs after the env-reading options factory (C1b dev). The
contract question goes to Go + Python (`config-contract-v0.3/tasks.md`
Deferred row of 2026-11-04). Exposition residuals (no `_ratio` for unit `1`
Gauges) become `skip:no-upstream-dotnet-prom-*` after C12 confirms them; an
e2e scrape from another container (C17) guards the bind.
**Justification**: ADR-007 keeps the fallback in core; the exporter owns the
exposition format. **Trade-offs**: `+` binds every IPv4 interface on Linux
(README lines 191-195), as 0.2.x did; the listener answers any method and
`/metrics/*`. **When wrong**: the prefix is rejected on some platform, or an
IPv6-only cluster cannot reach an IPv4-only bind. **Reversal action**: an
explicit IPv6 prefix (`http://[::]:{port}/metrics/`) or `Host` = the pod IP
from env, documented. **Alternatives**: helper-owned listener delegating to
the exporter (rejected: no public collection API, C1 dev H2); the
`Prometheus.AspNetCore` middleware (rejected with O9: excludes worker
services).

> **Corrected 2026-11-05 (C3).** (1) The helper also sets `Host = "localhost"`
> in its options delegate: the delegate runs after the options factory, and an
> `OTEL_EXPORTER_PROMETHEUS_HOST=*` left in `Host` would abort the provider
> build before `ConfigureHttpListener` replaces the prefix (measured: the test
> `PrometheusEnvVars_HaveNoEffect_HelperPortServed` fails without that line).
> Until C12 adds the O10 warnings, both variables have no effect and print
> nothing — the 0.2.x behavior (1.9.0-beta.2 read neither). (2) The e2e scrape
> from another container landed in C3, not C17: `testing/e2e/dotnet-prom-scrape.sh`
> (`testing/verify.sh --only dotnet-scrape`), CI job `dotnet-prom-scrape`, which
> `publish-dotnet` and `publish-dotnet-release` need; C17 may fold it into the
> `dotnet-smoke` service.

> **Superseded 2026-11-05 (owner decision, contract task A29).** O10 (a) no
> longer holds: the contract now honors both variables (config-contract design
> D1 row "Prometheus exporter variables"): port = code >
> `OTEL_EXPORTER_PROMETHEUS_PORT` > `OTEL_HELPER_METRICS_PORT` > 9464, the A21
> grammar, `0` = no listener; `OTEL_EXPORTER_PROMETHEUS_HOST` = the bind address
> (an IP literal or `localhost` → `127.0.0.1`; unset, `::` or an invalid value =
> every interface, the `+` prefix; invalid values warn once). **C12 must honor
> them** instead of warning that they have no effect: the helper keeps owning
> `Host` (never the exporter's `localhost` default, never `*`) and builds the
> prefix from the resolved host; `PrometheusEnvVars_HaveNoEffect_HelperPortServed`
> is rewritten in C12. Cases `metrics.port_prometheus_*` and
> `metrics.listener_prometheus_host_loopback` are `xfail:C12`.

### D-NET12 — Parity runner: xUnit theory over the fixture, one process per case
**Choice**: a test class `ParityTests` in the test project finds
`parity/cases.json` from a repository-root marker (walking up from the test
assembly to the directory holding `parity/cases.json`, overridable with
`PARITY_CASES`), validates the fixture (schema, matchers, statuses — the Node
`harness.cjs` rules), and runs each case as a child process of a new console
project `StaffOps.OtelHelper.ParityDriver` (`dotnet <driver.dll> <case-json>`),
with env reset (`OTEL_*`, `SERVICE_NAME`, `ENVIRONMENT` removed,
`OTEL_HELPER_METRICS_PORT=0`). The driver builds a host with
`AddOtelHelper(…)` mapping the fixture `code` keys, runs the kind
(`resolve`, `export`, `sampler`, `propagation`, `http_server` and
`grpc_server` on Kestrel with a test gRPC service and the O7 key set — so
the `grpc.*` cases measure the opt-in configuration,
`logging`, `metrics_scrape`, `metrics_listener`; `fork` → recorded skip), and
prints one JSON snapshot; export cases run serially against `fakeotlp`
(compose `fakeotlp-dotnet` + `dotnet-tests` on `mcr.microsoft.com/dotnet/sdk:8.0`
until C0; CI: the existing `./.github/actions/fakeotlp` action in
`dotnet-test`). Strict xfail; `PARITY_REQUIRE_FAKEOTLP`; `PARITY_RELEASE`.
**Status scheme**: C4 writes `pass`, `xfail:C<n>`, a D-NET15 skip, or
`skip:C5-no-api` for the 13 cases whose `code` keys have no 0.2 API
(`traces_endpoint`, `insecure`, `compression`, `sample_ratio`, `health_paths`)
or an out-of-range value the 0.2 type cannot hold (`export_interval_ms`
2 147 483 648 into `int?`, C1b refuter H1): `config.code_interval_above_max_fails`,
`export.code_per_signal_endpoint_verbatim`, `config.code_sample_ratio_out_of_range_fails`,
`config.code_sample_ratio_one_beats_standard_sampler`,
`config.code_sample_ratio_one_beats_alias`,
`config.code_sample_ratio_one_is_parentbased`,
`config.code_insecure_contradicts_scheme_fails`,
`config.code_empty_compression_not_set`,
`export.code_compression_none_beats_env_gzip_grpc`,
`export.code_compression_none_beats_env_gzip_http`,
`config.code_at_in_path_warns`, `config.code_http_path_invalid_escape_fails`,
`http.health_paths_override`); C5 adds the options and moves them to the
owning `xfail:C<n>`. `tools/spec_lint.py` checks only `xfail:` statuses
(`check_xfail_tasks`), so the runner itself fails on a `skip:C<n>-<reason>`
whose task is missing or done (the Node rule), and `PARITY_RELEASE` fails on
any left. A lint extension is a repo-tooling backlog item (tasks.md
promotion triggers), not a Phase C task.
**Coverage**: coverlet.msbuild 6.0.2 merges hits from a child that runs the
instrumented assemblies of the test output folder and exits normally
(measured: 42.85% → 100% on a probe library, C1 dev); the driver always exits
normally, with an in-driver deadline below the runner's kill timeout.
**Cost**: a warm host + provider process takes 261–336 ms (measured, C1 dev
L3), so 324 cases take about 1.5–3 min serially before export waits.
**Justification**: same contract as the Go/Python/Node runners; one process
per case because OTel .NET globals are process-wide. **Trade-offs**: process
start per case. **When wrong**: too slow for CI. **Reversal action**: batch
non-export `resolve` cases per child with a fresh host each.
**Alternatives**: in-process with `AssemblyLoadContext` isolation (rejected:
`Activity` listeners and `EventSource`s are process-wide).

### D-NET13 — Rename first (N13); no compatibility package (owner decision O2, 2026-11-04)
**Choice**: C2 renames packages, assemblies, namespaces and projects to
`StaffOps.OtelHelper*` **before** any behavior task, together with N12.
Migration story: `0.2.x` (incl. `0.2.1`) stays published as `OtelHelper*` on
GitHub Packages; `0.3.0-rc.1` and later exist only as `StaffOps.OtelHelper*`;
the migration guide is a table (package reference, `using StaffOps.OtelHelper;`,
API changes of D-NET4). No `OtelHelper` 0.3 compatibility package (owner,
2026-11-04). nuget.org: owner decision O4 (2026-11-04) — reserve the
`StaffOps.` ID prefix (an open owner action, no deadline, blocking nothing);
publishing stays on GitHub Packages only.
**Justification**: (1) every later task is written once with the final names;
(2) .NET consumers are compiled — the compiler lists every place to change
(C1 dev L4 agrees); (3) a type-forwarding shim cannot change a namespace.
**Trade-offs**: one-step upgrade of package, namespace and API.
**When wrong**: a consumer with many services needs a staged migration.
**Reversal action**: publish `OtelHelper` 0.3.x as a thin `[Obsolete]`
wrapper for one minor. **Alternatives**: rename last (rejected: churn).

### D-NET14 — Opt-in packages: one sanitizer before the exporter; upstream semconv
**Choice**:
- **One sanitizing processor**, owned by core and registered before every
  export processor (the helper builds those, D-NET3), runs the A25–A28 rules;
  opt-in packages register rules with it through DI instead of adding their
  own processors (`ConfigureOpenTelemetryTracerProvider` runs after core
  added the exporter, so an `OnEnd` change there races the batch queue — C1
  observability M4).
- **Sql** (O5, 2026-11-04): SqlClient (stable conventions, A25 met upstream
  in 1.20.0, exception text kept deliberately — `RecordException = true`, as
  Go), Npgsql and MySqlConnector through their native source/meter names (no
  new package unless C14a shows Npgsql's builder extension is needed — then
  `Npgsql.OpenTelemetry` 10.0.3, approved by O5); tests on `sql-postgres` /
  `sql-mysql`; legacy `db.system` absent for SqlClient — recorded gap.
- **Redis**: the DI overload fixed (resolve the multiplexer inside a
  non-registering callback, or upstream's DI-resolving overload
  `[TO VERIFY C14b]`); failed commands not marked ERROR — permanent gap; R10
  dup — owner decision O8 (c), 2026-11-04: documented gap; the HOW-TO says
  "set `OTEL_SEMCONV_STABILITY_OPT_IN=database/dup` in the deployment to get
  `db.system.name` and the other new keys on Redis spans" and the README has a
  gap row. The reviewers were split: observability preferred (b), a sanitizer
  rule deriving `db.system.name=redis`, `db.operation.name`/`db.query.text`
  (from `db.statement`) and `db.namespace` (from the database index) on
  legacy-only spans; dev preferred (c) (principle 7). (b) is the **reversal**
  if `db_system_name="redis"` panels must include .NET without a deployment
  change.
- **AWS**: the sanitizer clears the status description, sets `error.type`
  from the AWS error code (`^[A-Z][A-Za-z0-9]{1,63}$` or `_OTHER`), drops
  `aws.s3.key`, `aws.s3.copy_source`, `aws.s3.delete`,
  `aws.dynamodb.item_collection_metrics`, writes SNS phone numbers as
  `phone_number:**`.
- **URL query redaction** (O3, 2026-11-04 — keep upstream): the gap depends on
  the **runtime** the app runs on (upstream checks `Environment.Version`, not
  the target framework — C1b observability N-M1): server `url.query`
  `?k=Redacted` (contrib) on every runtime (ASP.NET Core 10 native hosting
  emits no `url.query`, `HostingApplicationDiagnostics.cs:458`); client
  `url.full` `?k=Redacted` on the .NET 8 runtime (contrib
  `Shared/RedactionHelper.cs:12`) and `?*` with userinfo removed on the .NET
  9+ runtime (runtime `UriRedactionHelper.cs:34-80`, `release/10.0`, names
  dropped). Security
  argument: upstream hides **every** value, the contract list only seven names
  — aligning would expose values such as `token` or `access_token`. When the
  app disables upstream redaction (contrib flags, `DOTNET_SYSTEM_NET_HTTP_DISABLEURIREDACTION`,
  `System.Net.Http.DisableUriRedaction`), the sanitizer still writes the A28
  names as `REDACTED`.
**Justification**: principle 7 (don't re-implement) where upstream is right;
one ordered sanitizer is the only race-free place for the rest.
**Trade-offs**: .NET DB spans lack `db.system` (SqlClient); query text
differs from the contract (`Redacted`, `?*`). **When wrong**: dashboards keyed
on `db.system` include .NET services. **Reversal action**: a sanitizer rule
copying `db.system.name` into `db.system`. **Alternatives**: per-package
processors (rejected: ordering); align query redaction to the list (rejected
by the owner, O3).

### D-NET15 — Known .NET differences (reference; each with a fixture status)

A `skip:` status is set only after the C4 runner or the named task shows the
failure is upstream.

| Case(s) | Status | Why | Task |
|---|---|---|---|
| `fork.instance_ids_distinct_post_fork`, `fork.env_instance_id_warns_and_kept` | `skip:no-prefork-model` | no prefork server model (as Go) | C4 |
| `resource.detector_var_ignored_warned` | `skip:no-detector-variable` | no SDK detector-selection variable (string check) | C4 |
| `grpc.health_counted_in_metrics`, `grpc.health_watch_counted_in_metrics` | `skip:no-upstream-rpc-metrics` | no RPC metric in the .NET packages (string check) | C4 |
| `metrics.listener_other_path_404`, `metrics.listener_get_head_only` | `skip:no-upstream-dotnet-prom-listener` (O9 (a), 2026-11-04) | any method, `/metrics/*` answered (measured) | C4 |
| `metrics.scrape_gauge_unit_one_ratio` | `skip:no-upstream-dotnet-prom-ratio` | no `_ratio` in either version (measured) | C12 |
| other `metrics.scrape_*` suffix/scope rows | `skip:no-upstream-dotnet-prom-*` | only where the 1.20.x exposition differs | C12 |
| `config.attribute_value_length_limit_zero_warns`, `config.attribute_value_length_limit_zero_signal_wins` | `skip:no-upstream-dotnet-attribute-limit` unless C8 finds a public override | the exporter reads the limit from env and truncates to empty at 0 (measured); the helper can warn but not ignore it | C8 |
| `export.grpc_certificate_*`, `export.grpc_client_*` | `skip:no-upstream-dotnet-grpc-tls-files` only if the handler does not apply to gRPC | ADR-013 | C8 |
| `grpc.*` server cases | none (O7 (b): the driver sets the key) | measured with the opt-in configuration | C11 |
| (no case) Redis failed commands not ERROR, no `error.type` | README gap row | upstream never sets a status | C14b |
| (no case) Redis `db.system.name` absent by default (legacy keys only) | README gap row + HOW-TO line (O8 (c)) | the instrumentation reads `OTEL_SEMCONV_STABILITY_OPT_IN` from the process env only | C14b |
| (no case) SqlClient legacy `db.system` absent | README gap row | stable-only upstream | C14a |
| (no case) URL query redaction text and scope (.NET 8 vs .NET 9+ runtime) | README gap row | O3 | C14c, C-patch notes |
| (no case) gRPC failures counted as HTTP 200 in `http.server.request.duration`; no RPC metrics | README gap row | upstream | C11 |
| (no case) gRPC span names `pkg.Svc/Method` *(amended 2026-11-08, A30: cases `grpc.server_span_name_semconv` and `grpc.client_span_name_semconv` now cover it)* | README row: same as Go | upstream | C11 |
| `http.opt_in_metrics_scrape_body_size_on`, `http.opt_in_metrics_scrape_code_on` *(added 2026-11-08, A30)*: ASP.NET Core and `System.Net.Http` have no HTTP body-size instrument (Microsoft Learn built-in metrics pages read 2026-11-08), so the opt-in switch brings back active requests and connection metrics only | `skip:no-upstream-dotnet-http-body-size` | upstream | C11 |

`skip:dotnet-sdk-logs-env-values` (r1 candidate) is not needed: the SDK
throws on malformed headers but echoes no value (measured, C1 dev H4).

### D-NET16 — C0 last and .NET 8 end of support (owner decision O1, 2026-11-04)
**Choice**: .NET 8 and 9 reach end of support on **2026-11-10**
(releases-index.json). The owner chose (2026-11-04) the `0.2.1` dual-target
patch (D-NET0, task C-patch, ahead of C2) and kept C0 last: the 0.3 rc line
targets `net8.0` until C0, then `net10.0` only. C0 is a **target-framework**
change; the telemetry differences consumers associate with ".NET 10" follow
the **runtime** (C1b observability N-M1) and already apply to any 0.2.x or 0.3
build running on .NET 9+. So C0's before/after diff compares an app on the
**.NET 8 runtime** (last `net8.0` rc) with the same app on the **.NET 10
runtime** (C0 build) — a diff on `sdk:10.0` alone would show nothing — and the
migration guide's C0 section is for consumers still on the .NET 8 runtime.
**Justification**: (1) `0.2.1` gives consumers a library compiled and tested
on a supported runtime within days, without dragging 0.3 behavior changes in;
(2) keeping C0 last avoids a second TFM change mid-Phase C (owner order
2026-10-28); (3) the runtime framing keeps consumers already on .NET 10 from
expecting a change at C0. **Trade-offs**: rc builds keep shipping a `net8.0`
asset after its end of support; two branches carry .NET changes until 0.3.0.
**When wrong**: a consumer needs `net8.0` on the 0.3 line past EOL.
**Reversal action**: ADR-009's reversal — multi-target `net8.0;net10.0` on 0.3.
**Alternatives**: keep the order without a patch (not chosen by the owner);
move C0 first (rejected by the owner, 2026-10-28).

## Risks

| Risk | Effect | Mitigation |
|---|---|---|
| `0.2.1` not out by 2026-11-10 | consumers on an unsupported runtime with a library never compiled for net10 | C-patch is small and first; measured build/test before tagging |
| Five upstream minors in C3 | exposition and attribute changes unrelated to the contract | before/after diff; breaking items listed in the package table |
| Experimental upstream gRPC server flag | attribute churn per bump; apps that forget the key get HTTP-shaped spans | O7 (b) warning; re-check per bump (release skill checklist) |
| Helper `HttpClient` replaces the SDK client | lost named-client customizations; must copy SDK hardening per bump | ADR-013 duties list; C3/C8 checklist |
| `IConfiguration`-only OTEL keys stop working; bound options beat env | silent changes for `appsettings.json` users | migration guide rows; C5 unit test (env plus in-memory `IConfiguration`) |
| Source-breaking API + rename | every consumer edits code | one migration (D-NET4/13) |

## Owner decisions

Decided (2026-11-04):

| ID | Decision | Where |
|---|---|---|
| O1 | Ship `0.2.1` adding `net10.0` alongside `net8.0`, no behavior change on the same runtime; ADR-009 amended (dual targeting on the 0.2.x line only); C0 still last | D-NET0, D-NET16, task C-patch |
| O2 | No `OtelHelper` 0.3 compatibility package | D-NET13 |
| O3 | Keep upstream query redaction; runtime-dependent gap rows; contract names still redacted when upstream redaction is disabled | D-NET14, DN9 |
| O5 | Add Npgsql and MySqlConnector to the Sql package, tested on compose postgres/mysql | D-NET14, C14a |
| O6 | Bump to OTel 1.20.x in C3 | D-NET1 |
| O7 | (b): the app sets `OTEL_DOTNET_EXPERIMENTAL_ASPNETCORE_ENABLE_GRPC_INSTRUMENTATION`; the helper detects gRPC endpoints without it and warns once; README and migration item 5 say so; the driver and examples set it | D-NET8, D-NET12 |
| O8 | (c): documented gap — README gap row and HOW-TO line on `OTEL_SEMCONV_STABILITY_OPT_IN=database/dup`; reviewers split (observability (b), dev (c)); (b) is the reversal | D-NET14, D-NET15 |
| O9 | (a): `metrics.listener_*` (2 cases) `skip:no-upstream-dotnet-prom-listener` | D-NET11, D-NET15 |
| O10 | (a): `OTEL_EXPORTER_PROMETHEUS_HOST`/`_PORT` ignored with one warning each; contract question to Go + Python (Deferred row) *Superseded 2026-11-05 (contract A29): honored per the contract D1 row "Prometheus exporter variables"; C12 implements it (D-NET11 note)* | ADR-013, D-NET11 |
| O11 | (a): `0.2.1` carries `LICENSE` and `<PackageLicenseExpression>Apache-2.0</PackageLicenseExpression>` (metadata only; the repository is Apache-2.0 since `72d82c6`) | tasks.md C-patch plan item 1 |
| O4 | Reserve the `StaffOps.` ID prefix on nuget.org; publish on GitHub Packages only. The reservation is an **open owner action** with no deadline that blocks nothing | D-NET13 |

No owner decision is open. Open owner action: the nuget.org prefix reservation (O4).
