# Tests — StaffOps.OtelHelper (.NET)

1840 tests (xUnit) with the parity runner, 0 failed, 0 skipped, 0 warnings (compose `dotnet-tests` on `sdk:8.0` with `sql-postgres` and `sql-mysql`, and on `sdk:10.0` (10.0.403) with `DOTNET_ROLL_FORWARD=Major` on the same network, 2026-11-11, task C14a round 4); coverlet total `| Total | 98.52% | 96.81% | 99.41% |` on `sdk:8.0`, `| Total | 98.49% | 96.77% | 99.41% |` on SDK 10. Go, Python and Node.js re-run green (Node.js on the second run: the first timed out in the unrelated cluster test `D4 A24 an env-provided id …`, 20 s deadline). The round-3 line below is the previous run.

1840 tests (xUnit) with the parity runner, 0 failed, 0 skipped, 0 warnings (compose `dotnet-tests` on `sdk:8.0` with `sql-postgres` and `sql-mysql`, and on `sdk:10.0` (10.0.403) with `DOTNET_ROLL_FORWARD=Major` on the same network, 2026-11-11, task C14a round 3); coverlet total `| Total | 98.54% | 96.81% | 99.41% |` (line, branch, method) on both. Go, Python (1546 passed, 3 xfailed, 97.43%) and Node.js suites re-run green with the new parity case. The round-2 line below is the previous run.

1823 tests (xUnit) with the parity runner, 0 failed, 0 skipped, 0 warnings (compose `dotnet-tests` on `sdk:8.0` with `sql-postgres` and `sql-mysql`, and on `sdk:10.0` (10.0.403) with `DOTNET_ROLL_FORWARD=Major` on the same network, 2026-11-11, task C14a round 2); coverlet total `| Total | 98.58% | 96.79% | 99.4% |` (line, branch, method) on both. The round-1 line below is the previous run.

1810 tests (xUnit) with the parity runner, 0 failed, 0 skipped, 0 warnings (compose `dotnet-tests` on `sdk:8.0` with `sql-postgres` and `sql-mysql`, and on `sdk:10.0` (10.0.403) with `DOTNET_ROLL_FORWARD=Major` on the same network, 2026-11-11, task C14a fix round 1); coverlet total `| Total | 98.58% | 96.87% | 99.4% |` (line, branch, method) on both. Before the fix round: 1781 tests. The C13 line below is the previous task's run.

1689 tests (xUnit) with the parity runner, 0 failed, 0 warnings (compose `dotnet-tests` on `sdk:8.0`, and on `sdk:10.0` (10.0.403) with `DOTNET_ROLL_FORWARD=Major`, 2026-11-11, task C13 fix round 1); coverlet total `| Total | 98.44% | 96.48% | 99.35% |` (line, branch, method) on both. Before the fix round: 1671 tests, 98.45% / 98.52%. The C12 line below is the previous task's run.

1615 tests (xUnit) with the parity runner, 0 failed, 0 warnings (compose `dotnet-tests` on `sdk:8.0`, and on `sdk:10.0` (10.0.403) with `DOTNET_ROLL_FORWARD=Major`, 2026-11-11, task C12 fix round 1); coverlet total `| Total | 98.47% | 96.37% | 99.3% |` (line, branch, method) on `sdk:8.0`, `| Total | 98.4% | 96.32% | 99.3% |` on SDK 10. Before the fix round (C12): 1598 tests, 98.41% / 98.44%. The C11 line below is the previous task's run.

1502 tests (xUnit) with the parity runner, 0 failed, 0 warnings (compose `dotnet-tests` on `sdk:8.0`, and on `sdk:10.0` (10.0.403) with `DOTNET_ROLL_FORWARD=Major`, 2026-11-10, task C11 fix round 2); coverlet total `| Total | 98.39% | 96.37% | 99.27% |` (line, branch, method) on `sdk:8.0`, `| Total | 98.31% | 96.32% | 99.27% |` on SDK 10. The total varies by about ±0.1 point between runs; the cause was not investigated.

From `dotnet/`; the repository root is mounted because the parity runner reads
`parity/cases.json` (and `specs/dotnet-otel-helper/tasks.md`) from it:

```bash
docker run --rm -v "$(git rev-parse --show-toplevel):/src" -w /src/dotnet mcr.microsoft.com/dotnet/sdk:8.0 dotnet test StaffOps.OtelHelper.Tests
```

Every project builds with warnings as errors (`Directory.Build.props`), so a
new compiler warning or a public member without an XML doc comment (`CS1591`)
fails the build. NuGet warnings that depend on the network or on advisory data
stay warnings, so the tree also builds offline from a filled package cache:
`NU1801`, `NU1900`–`NU1905`, and `NU5104` (prerelease dependencies, DN1).

With coverage and the 90% gate, as CI runs it, on a container-local copy (the
source mount stays read-only):

```bash
docker run --rm -e VSTEST_TESTHOST_SHUTDOWN_TIMEOUT=10000 -v "$(git rev-parse --show-toplevel):/src:ro" mcr.microsoft.com/dotnet/sdk:8.0 sh -c '
  mkdir /w && cd /src && tar --exclude=bin --exclude=obj --exclude=coverage --exclude=node_modules -cf - dotnet parity specs | tar -xf - -C /w
  cd /w/dotnet && dotnet test StaffOps.OtelHelper.Tests -c Release \
    /p:CollectCoverage=true /p:CoverletOutputFormat=cobertura /p:CoverletOutput=./coverage/ \
    /p:Threshold=90 /p:ThresholdType=line /p:ThresholdStat=total /p:Exclude="[xunit*]*%2c[StaffOps.OtelHelper.ParityDriver]*"'
```

`VSTEST_TESTHOST_SHUTDOWN_TIMEOUT=10000` is required for a stable number:
vstest kills the test host about 100 ms after the last test, often before
`coverlet.msbuild` writes its hit files, and the total then drops (81.86%, or
0%) and fails the gate while every test passed. CI's `dotnet-test` job sets
the same variable. Long-term option: `coverlet.collector` (a data collector
inside vstest's own shutdown sequence) instead of `coverlet.msbuild`; it needs
a different threshold step, because the collector has no `Threshold`
property.

---

## ParityTests (shared fixture)

`ParityTests` runs every case of `parity/cases.json` in its own process
(`StaffOps.OtelHelper.ParityDriver`), against the fake OTLP receiver, with the
`dotnet` status of each case (strict xfail); `ParityHarnessTests` cover the
matcher and the schema. Rules, environment variables and adapter seams:
[`parity/README.md`](../parity/README.md#net-runner). Run it with the receiver:

```bash
docker compose -f testing/docker-compose.yml run --rm dotnet-tests
```

Without the receiver (the plain `dotnet test` above), every case with a
`{receiver…}` placeholder, and every export case, is reported as passed with a
"not run" line. Without the repository root mounted, `Fixture_is_found` fails
with "parity/cases.json not found; mount the repo root or set PARITY_CASES".

---

## OptionsTests

Validate defaults and env var resolution.

| Test | Description |
|-------|-----------|
| `Default_ServiceName_Is_The_Sdk_Unknown_Service` | No service name anywhere → `unknown_service:<process>`, not configured (task C6) |
| `Default_CollectorEndpoint_Is_Empty_When_No_EnvVar` | Default endpoint stays empty (Prometheus fallback) |
| `Default_DebugLevel_Is_False` | Debug disabled by default |
| `ResolveEnvironment_Parses_Correctly` × 6 | LOCAL, DEV, HML, PRD, prd, dev |
| `ResolveEnvironment_Unknown_Is_Verbatim_And_Blank_Is_Absent` × 5 | Unknown `ENVIRONMENT` values kept as written (trimmed), blank → absent (task C6; 0.2.x: `LOCAL`) |

## RegistrationTests

| Test | Description |
|-------|-----------|
| `AddOtelHelper_Parameterless_Uses_Defaults` | Parameterless registration works |
| `AddOtelHelper_Registers_Options` | IOptions<TelemetryOptions> registered |
| `AddOtelHelper_Registers_Validator` | TelemetryOptionsValidator registered |

## TelemetryPipelineTests

### Environments

| Test | Description |
|-------|-----------|
| `All_Environments_Register_Pipeline_Without_Error` × 4 | LOCAL, DEV, HML, PRD register pipeline |

### Log Level

| Test | Description |
|-------|-----------|
| `LogLevel_Matches_Environment` × 4 | LOCAL=Debug, DEV/HML=Info, PRD=Warning |
| `DebugLevel_Forces_Debug_LogLevel` | Debug mode forces LogLevel.Debug in PRD |

### Endpoint Resolution

| Test | Description |
|-------|-----------|
| `Default_Endpoint_Is_Empty_Without_EnvVar` | No env var → endpoint stays empty |
| `Endpoint_Extracts_Host_And_Appends_Port` | Extracts host and adds :4317 |
| `Endpoint_With_Trailing_Slash_Is_Cleaned` | Removes trailing slash |
| `PostConfigure_Endpoint_NonURI_Uses_RawValue` | Non-URI value (e.g. "just-a-hostname") falls back to raw value |

### Full Pipeline

| Test | Description |
|-------|-----------|
| `Full_Pipeline_Registers_All_Signals` | Logging + Options registered |
| `Custom_Sampler_Is_Accepted` | TraceIdRatioBasedSampler accepted |
| `Custom_MinimumLogLevel_Overrides_Environment` | MinimumLogLevel override works |
| `ResourceAttributes_Default_Is_Empty` | Empty by default |
| `ResourceAttributes_Accepted_In_Pipeline` | Custom attributes accepted |

### ActivitySources

| Test | Description |
|-------|-----------|
| `AdditionalActivitySources_Default_Is_Empty` | Empty list by default |
| `AdditionalActivitySources_Accepted_In_Pipeline` | Additional sources accepted |

### DI Registration

| Test | Description |
|-------|-----------|
| `ActivitySource_Registered_Via_DI` | ActivitySource singleton with ServiceName |
| `Meter_Registered_Via_DI` | Meter singleton with ServiceName |

### GetDefaultLogLevel

| Test | Description |
|-------|-----------|
| `GetDefaultLogLevel_Returns_Correct_Values` | LOCAL=Debug, DEV=Info, PRD=Warning, debug override |

### StartRootActivity

| Test | Description |
|-------|-----------|
| `StartRootActivity_Creates_Root_Span_Without_Parent` | Clears parent, creates root |
| `StartRootActivity_Generates_New_TraceId_Each_Call` | Each call generates new traceId |

### OTEL_HELPER_SAMPLE_RATIO

| Test | Description |
|-------|-----------|
| `SampleRatio_EnvVar_Sets_TraceIdRatioBasedSampler` | 0.5 → `ParentBased(TraceIdRatioBased)` (task C10; the name is kept) |
| `SampleRatio_Default_Is_ParentBased_AlwaysOn` | No env var → `parentbased_always_on` |
| `SampleRatio_Invalid_EnvVar_Keeps_The_Default` | Invalid value → `parentbased_always_on` (warned; see the C10 section) |

### Double Registration

| Test | Description |
|-------|-----------|
| `Double_AddOtelHelper_Does_Not_Duplicate_Registration` | Second call is no-op |

### Environment resolution (`TelemetryResolver`)

| Test | Description |
|-------|-----------|
| `PostConfigure_Resolves_Environment_From_EnvVar` | ENVIRONMENT=PRD → resolved `PRD` (`DeploymentEnvironments.PRD`) |
| `PostConfigure_Resolves_DisabledSignals_From_EnvVar` | OTEL_HELPER_DISABLED_SIGNALS resolves the list |
| `PostConfigure_Resolves_DisabledMetrics_From_EnvVar` | OTEL_HELPER_DISABLED_METRICS resolves wildcard patterns |
| `PostConfigure_DisabledMetrics_Stays_Empty_When_EnvVar_Not_Set` | Stays empty when unset |

### IsSignalEnabled

| Test | Description |
|-------|-----------|
| `IsSignalEnabled_Returns_True_When_DisabledSignals_Empty` | All signals enabled when DisabledSignals is empty |
| `IsSignalEnabled_Is_Case_Insensitive` | Case-insensitive matching |

### Disabled Signals — Full Pipeline

| Test | Description |
|-------|-----------|
| `Pipeline_Registers_When_Metrics_Disabled` | Metrics disabled, pipeline still registers |
| `Pipeline_Registers_With_DisabledMetrics_Pattern` | Wildcard drop pattern accepted |
| `Pipeline_Registers_When_Traces_Disabled` | Traces disabled, pipeline still registers |
| `Pipeline_Registers_When_Logs_Disabled` | Logs disabled, pipeline still registers |
| `Pipeline_Registers_When_All_Signals_Disabled` | All three disabled at once |

## TracingAndOptionsHelperTests

Tracing helpers (`StartRootActivity`) and `TelemetryOptions` helper methods, tested directly (not through `AddOtelHelper`). The debug marker tests moved to the C10 section (`SamplerPropagatorDebugTests`).

### StartRootActivity

| Test | Description |
|-------|-----------|
| `StartRootActivity_Creates_Root_Span_Without_Parent` | Clears parent, creates root |
| `StartRootActivity_Generates_New_TraceId_Each_Call` | Each call generates a new traceId |

### IsSignalEnabled

| Test | Description |
|-------|-----------|
| `IsSignalEnabled_Returns_True_When_DisabledSignals_Empty` | All signals enabled when empty |
| `IsSignalEnabled_Returns_False_When_Signal_Disabled` | Disabled signal reports false |
| `IsSignalEnabled_Is_Case_Insensitive` | Case-insensitive matching |

### GetDefaultLogLevel

| Test | Description |
|-------|-----------|
| `GetDefaultLogLevel_Returns_Correct_Values` | LOCAL=Debug, DEV/HML=Info, PRD=Warning, debug override |

## SignalIntegrationTests

Integration tests verifying signals are actually collected or dropped via in-memory exporters. Uses `[Collection("EnvVarTests")]` to prevent parallel execution (env var conflicts).

| Test | Description |
|-------|-----------|
| `Meter_Name_Matches_ServiceName` | DI-resolved Meter name equals ServiceName |
| `ActivitySource_Name_Matches_ServiceName` | DI-resolved ActivitySource name equals ServiceName |
| `Custom_Metrics_Via_DI_Meter_Are_Collected` | A counter created via the DI Meter is exported |
| `Traces_Via_DI_ActivitySource_Are_Collected` | A span created via the DI ActivitySource is exported with its tags |
| `LogLevel_Above_Minimum_Are_Enabled` × 3 | LOCAL/DEV/PRD minimum log level enabled, level below disabled |
| `Full_Pipeline_Registers_All_Signals` | Logging, Options, ActivitySource, Meter all resolve from the built provider |
| `DebugLevel_Forces_Debug_LogLevel` | Debug mode forces Debug level even in PRD |
| `Custom_MinimumLogLevel_Overrides_Environment` | Explicit MinimumLogLevel overrides the env-based default |
| `DisabledMetrics_Pattern_Drops_Matching_Metric` | A metric matching the drop pattern is excluded; a non-matching one is kept |

## SubpackageExtensionsTests

Opt-in subpackage extensions (AWS, SQL, Redis) — each verifies the fluent `IServiceCollection` return and that `TracerProvider` resolves after registration.

| Test | Description |
|-------|-----------|
| `AddOtelHelperAws_Registers_And_Returns_Services` | AWS extension registers, TracerProvider resolves |
| `AddOtelHelperSql_Registers_And_Returns_Services` | SQL extension registers, TracerProvider resolves |
| `AddOtelHelperRedis_Explicit_Connection_Registers` | Redis extension with an explicit `IConnectionMultiplexer` |
| `AddOtelHelperRedis_FromDI_Registers_And_Returns_Services` | Redis extension resolving the connection from DI |

## TlsEndpointResolutionTests

`TelemetryResolver`'s TLS scheme resolution — secure by default, `OTEL_EXPORTER_OTLP_INSECURE` override, explicit scheme always wins. Uses `[Collection("EnvVarTests")]`.

| Test | Description |
|-------|-----------|
| `HttpsScheme_Preserved_When_Explicit` | `https://gw:4317` kept as-is |
| `HttpScheme_Preserved_When_Explicit` | `http://gw:4317` kept as-is (plaintext) |
| `Schemeless_Defaults_To_Https_SecureByDefault` | `gw:4317` (no scheme) → `https://gw:4317` |
| `Schemeless_With_Insecure_True_Uses_Http` | Schemeless + INSECURE=true → `http://` |
| `Schemeless_With_Insecure_False_Uses_Https` | Schemeless + INSECURE=false → `https://` |
| `Schemeless_HostOnly_Defaults_Port_4317_And_Https` | Host-only (no port) → `https://host:4317` |
| `HttpsScheme_Preserves_Custom_Port` | `https://gw:4318` keeps the custom port |
| `Schemeless_With_Custom_Port_And_Insecure` | `gw:4318` + INSECURE=true → `http://gw:4318` |
| `ConsumerSet_Endpoint_Not_Overridden` | Endpoint set in code is not overwritten by the environment |
| `Insecure_EnvVar_Ignored_When_Scheme_Explicit_Https` | Explicit `https://` scheme wins over INSECURE=true |
| `HttpsScheme_DefaultPort_Gets_4317` | `https://gw` (no port, gRPC) dials :4317 (portless gRPC rule, warned since task C7a) |
| `EndpointResolver_Unit_Grpc` × 4 | `EndpointResolver.Resolve` (task C7a, replaces the 0.2 `ResolveEndpoint` helper): schemeless `host:port` and `host`, schemeless + insecure, explicit `http://` |
| `Http_Code_Endpoint_Keeps_Its_Path_For_Http_Protobuf` | Task C7a: an HTTP URL keeps its path (`/v1/<signal>` appended for the generic value); gRPC dials `host:4317` |

## ProfilingTests

`StaffOps.OtelHelper.Profiling`'s `ProfilingPrerequisites` and extension registration. Uses `[Collection("EnvVarTests")]`.

| Test | Description |
|-------|-----------|
| `ProfilingPrerequisites_FromEnvironment_AllSet_IsSatisfied` | All required env vars set → satisfied |
| `ProfilingPrerequisites_Missing_ServerAddress_NotSatisfied` | Missing server address → not satisfied |
| `ProfilingPrerequisites_Missing_ClrProfiler_NotSatisfied` | Missing CLR profiler env var → not satisfied |
| `ProfilingPrerequisites_Missing_ProfilingEnabled_NotSatisfied` | Missing "profiling enabled" var → not satisfied |
| `ProfilingPrerequisites_DescribeMissing_ListsMissingVars` | `DescribeMissing()` lists all missing vars when nothing is set |
| `ProfilingPrerequisites_DescribeMissing_OnlyListsMissing` | `DescribeMissing()` omits vars that are already set |
| `ProfilingPrerequisites_DescribeMissing_ReturnsNull_When_Satisfied` | `DescribeMissing()` returns null when fully satisfied |
| `ProfilingPrerequisites_Truthy_AcceptsBothOneAndTrue` × 4 | "1", "true", "True", "TRUE" all count as enabled |
| `ProfilingPrerequisites_NotTruthy_Rejects_InvalidValues` × 5 | "0", "false", "", "yes", "on" are all rejected |
| `ProfilingPrerequisites_ApplicationName_IsRead` | ApplicationName read from its env var |
| `ProfilingPrerequisites_IsSatisfied_Without_ApplicationName` | ApplicationName is optional — not required for `IsSatisfied` |
| `AddOtelHelperProfiling_Registers_HostedService_When_Prerequisites_Missing` | Missing prerequisites → `ProfilingPrerequisiteWarning` hosted service registered |
| `ProfilingPrerequisiteWarning_LogsWarning_OnStart` | The warning hosted service starts/stops without exception |
| `AddOtelHelperProfiling_Registers_IProfilingProvider` | `IProfilingProvider` resolves from DI after registration |

## MetricsContractTests

`OTEL_METRICS_EXPORTER` contract (US-1, US-2, US-6). Cross-language parity with `python/tests/test_metrics_contract.py` and `go/metrics_contract_test.go`. Uses `[Collection("EnvVarTests")]`.

### Exporter resolution

| Test | Description |
|-------|-----------|
| `Unset_WithEndpoint_IsOtlp` | Unset + endpoint set → otlp (legacy inference) |
| `Unset_WithoutEndpoint_IsPrometheus` | Unset + no endpoint → prometheus |
| `PrometheusOnly_EvenWithEndpoint` | `prometheus` wins even with an endpoint set |
| `DualMode_BothExporters` | `otlp,prometheus` → both active |
| `None_DisablesMetrics` | `none` → empty resolved list |
| `CaseAndWhitespace_Tolerated` | " OTLP , Prometheus " parses correctly |
| `ExplicitOption_BeatsEnv` | `MetricExporters` property wins over the env var |

### Export interval

| Test | Description |
|-------|-----------|
| `Interval_DefaultIs30s` | Default is 30000ms |
| `Interval_EnvHonored` | OTEL_METRIC_EXPORT_INTERVAL resolves the interval |
| `Interval_ExplicitBeatsEnv` | Explicit `ExportIntervalMs` wins over the env var |
| `Interval_InvalidEnvFallsBack` | Non-numeric env value falls back to 30000 |
| `Interval_NonPositive_FailsValidation` | `ExportIntervalMs = 0` → validation error |

### Listener / port

| Test | Description |
|-------|-----------|
| `PortZero_IsValid` | `PrometheusMetricsPort = 0` passes validation (listener disabled) |

### Dual-mode pipeline

| Test | Description |
|-------|-----------|
| `DualMode_CounterVisible_InInMemoryAndPrometheusReaders` | Single provider, two readers — same counter visible via an in-memory exporter AND a live Prometheus HTTP scrape, no double counting |
| `ConfigureMetrics_PrometheusOnly_WithEndpoint_Builds` | `prometheus`-only with an endpoint set builds cleanly and serves `/metrics` |
| `ConfigureMetrics_PortZero_SkipsListener` | Port 0 builds without binding any listener |

## PrometheusListenerTests

The Prometheus fallback on `OpenTelemetry.Exporter.Prometheus.HttpListener`
1.20.x keeps the 0.2.x listener behavior (task C3, design D-NET11). Uses
`[Collection("EnvVarTests")]`. *Corrected 2026-11-11 (C12 fix round 1):* these
tests need no network, but two `PrometheusFallbackC12Tests` facts
(`[NonLoopbackFact]`) need a non-loopback IPv4 address of the container and
are skipped, with the reason, without one (`--network none`). Reachability
from another container, in the text and OpenMetrics formats, and the
loopback-bind refusal are checked by `testing/e2e/dotnet-prom-scrape.sh`.

| Test | Description |
|-------|-----------|
| `ListenerPrefix_IsWildcardHostMetricsPath` | The bind prefix is `http://+:<port>/metrics/` |
| `ConfigurePrometheusListener_SetsHostPortCacheAndPrefixDelegate` | `Host` reset to `localhost`, helper port, response cache 0, and a delegate that replaces the listener prefixes with the `+` prefix only |
| `Scrape_IsFresh_NoResponseCache` | Two scrapes back to back see the new value (upstream caches 300 ms by default) |
| `PrometheusEnvVars_HaveNoEffect_HelperPortServed` | `OTEL_EXPORTER_PROMETHEUS_HOST=*` does not abort the build and `OTEL_EXPORTER_PROMETHEUS_PORT` does not move the listener: `OTEL_HELPER_METRICS_PORT` is served |

*Changed 2026-11-11 (task C12):* `ListenerPrefix_IsWildcardHostMetricsPath`
became `ListenerPrefix_FollowsResolvedHost` × 4 (`""` and `0.0.0.0` → `+`, an
IPv4 literal, `::1` → `[::1]`); `ConfigurePrometheusListener_Host_ReplacesThePrefix`
is new; `PrometheusEnvVars_HaveNoEffect_HelperPortServed` became
`PrometheusPortEnvVar_BeatsHelperPort_AndHostStarDoesNotAbort` (the standard
port is served, the helper port is not, `*` warns once); the inactive-prefix
test is gone (no listener reader is registered then, see below).

## SamplerPrecedenceTests

`OTEL_TRACES_SAMPLER` (standard SDK var) precedence over `OTEL_HELPER_SAMPLE_RATIO` (US-2). Since task C10 the helper parses the variable and always sets the sampler; the SDK never reads it. Uses `[Collection("EnvVarTests")]`.

| Test | Description |
|-------|-----------|
| `HelperVarOnly_AppliesRatio` | Only `OTEL_HELPER_SAMPLE_RATIO` set → ratio applied, `ParentBasedSampler` (`parentbased_traceidratio`) |
| `StandardVarOnly_Wins` | Only `OTEL_TRACES_SAMPLER=always_off` set → `AlwaysOffSampler`, span not sampled |
| `StandardVarBeatsHelperVar` | Both set → standard var wins, helper ratio ignored |
| `ExplicitRatioBeatsStandardVar` | Explicit `Sampler` in code wins over the standard env var |
| `NoEnvNoOverride_DefaultsToParentBasedAlwaysOn` | Neither set → `ParentBasedSampler(AlwaysOnSampler)` (ADR-002), span sampled |

## OtlpProtocolTests

OTLP wire protocol selection (grpc vs http/protobuf) — tracing and metrics. Cross-language parity with `python/tests/test_otlp_protocol.py` and `go/otlp_protocol_test.go`. Uses `[Collection("EnvVarTests")]`.

### ResolvedOtlpProtocol precedence

| Test | Description |
|-------|-----------|
| `DefaultIsGrpc` | Endpoint on port 4317, no env var → grpc |
| `NoEndpoint_DefaultsToGrpc` | No endpoint at all → grpc |
| `Port4318_InfersHttp` | Endpoint on port 4318 → http/protobuf |
| `Port4317_InfersGrpc` | Endpoint on port 4317 → grpc |
| `OtherPort_InfersGrpc` | Endpoint on an unrelated port → grpc |
| `EnvVar_BeatsPortInference` | `OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf` wins over a :4317 endpoint that would infer grpc |
| `EnvVarGrpc_BeatsHttpPortInference` | `OTEL_EXPORTER_OTLP_PROTOCOL=grpc` wins over a :4318 endpoint that would infer http |
| `ExplicitOption_BeatsEnv` | `TelemetryOptions.OtlpProtocol` set in code wins over the env var |
| `EnvVar_CaseAndWhitespaceTolerant` | `"  HTTP/PROTOBUF  "` normalizes correctly |

### Validation

| Test | Description |
|-------|-----------|
| `GrpcAndHttpProtobuf_PassValidation` × 2 | `grpc` and `http/protobuf` both pass validation |

### Live integration — HTTP protocol delivers to /v1/{signal}

| Test | Description |
|-------|-----------|
| `Tracing_Http_DeliversToV1Traces` | `AddOtelHelper` with http/protobuf sends the request to `/v1/traces` on a real `HttpListener` |
| `Metrics_Http_DeliversToV1Metrics` | `AddOtelHelper` with http/protobuf sends the request to `/v1/metrics` on a real `HttpListener` |

## Resolver core (task C5)

`DiagnosticsTests`, `ResolverCoreTests`, `ResolverCoreFixRoundTests` (helpers in
`TestDiagnostics.cs`: a sink of its own per test, never `Console.SetError`;
`TestEventSources.cs`). They cover the diagnostics sink (record format,
escaping of control, separator and bidi characters, 64-code-point cut, once
per sink, URL rendering, free-text redaction), the SDK `EventSource`
forwarding (one listener per process, the latest sink, masked code and env
secrets, response bodies dropped by payload position, noise rules), the
`IConfiguration` neutralization of the exporter options (DN2), bound options
as code (DN4), keyed `ActivitySource`/`Meter` (DN5), env-warns / code-fails
validation naming the option, `OTEL_SDK_DISABLED` from `IConfiguration`, the
config-file warnings, the metric temporality, header parsing (malformed and
control-character items skipped; warned by position since task C8), TLS files and a custom root
with an intermediate, client disposal, the SDK `User-Agent`, the drop-all view
when no metric exporter is active, and an invalid code value reported once
when a provider is built before start.

## Identity and resource (task C6)

`IdentityResourceTests` (contract R4, R5, D4, A16, A24; design D-NET7):

| Area | Tests |
|---|---|
| `OTEL_RESOURCE_ATTRIBUTES` parser | `Parser_Blank_Items_Are_Ignored_Without_A_Position`, `Parser_Skips_Items_Without_Equals_Or_With_An_Empty_Key_By_Position`, `Parser_Decodes_Strictly_Then_Trims_With_The_Contract_Set` × 8 (`+` stays `+`, `=` in a value, UTF-8 escapes, a leading U+00A0 kept), `Parser_Keeps_An_Undecodable_Value_Literal_By_Position` × 6, `Parser_Unpaired_Surrogates_Make_A_Key_Malformed_And_A_Value_Replaced`, `Parser_Keeps_A_Unicode_Space_In_The_Key_And_A_Later_Duplicate_Wins` |
| Diagnostics of the parse | `Env_Resource_Attribute_Problems_Warn_Once_By_Position_Never_The_Value`, `Blank_Env_Resource_Attributes_Warn_Nothing` |
| `service.name` | `ServiceName_Otel_Variable_Beats_The_Deprecated_One`, `ServiceName_Resource_Attributes_Beat_The_Deprecated_Variable_And_Lose_To_The_Otel_One`, `ServiceName_Code_Option_Beats_Code_Attribute_Which_Beats_Env`, `ServiceName_Blank_Resource_Attribute_Falls_Back_To_The_Deprecated_Variable`, `ServiceName_Unset_Is_The_Sdk_Default_With_One_Warning`, `ServiceName_From_A_Code_Attribute_Only_Warns_Nothing` |
| Environment and log level | `Environment_Unset_Is_Absent_Info_And_Warned_Once`, `Environment_Alias_Canonicalizes_Known_Names_And_Keeps_Others` × 3, `Environment_Resource_Attributes_Beat_The_Alias_And_Are_Verbatim`, `Environment_Code_Option_Beats_Code_Attribute_Which_Beats_Env`, `Environment_Blank_In_Resource_Attributes_Is_Absent_And_Warned`, `LogLevel_Code_Environment_Matches_Case_Insensitively`; `OptionsTests.GetDefaultLogLevel_By_Environment_Name` (ASCII case, trim, U+0130 and U+00A0 not folded) |
| `service.instance.id` | `InstanceId_Code_Beats_Env_Which_Beats_The_Process_Id`, `InstanceId_Is_The_One_The_Sdk_AddService_Generates_In_This_Process` |
| Rebuilt resource | `Resource_Carries_The_Resolved_Identity_On_Every_Provider_With_The_Contract_Schema_Url`, `Resource_Code_Attributes_Beat_Env_Per_Key`, `Resource_Drops_The_Sdk_Env_Detector_Parse`, `Resource_Ignores_Otel_Service_Name_Set_Only_In_IConfiguration` (a guard), `Resource_Ignores_Otel_Resource_Attributes_Set_Only_In_IConfiguration`, `Resource_Blank_Env_Service_Name_Is_The_Explicit_Sdk_Default`, `Resource_Telemetry_Sdk_Comes_From_The_Sdk_Env_Silently_Code_Warned_Once_Per_Key`, `Resource_Keeps_Earlier_App_Attributes_Between_Env_And_Code`, `Resource_App_AddService_Name_Is_Replaced_With_One_Warning_Its_Instance_Id_Agrees`, `Resource_App_Environment_Stays_When_None_Is_Resolved`, `Resource_An_Earlier_Factory_Detector_Is_Kept_Without_Clearing_And_Warned_Once`, `Resource_App_Attributes_Equal_To_The_Sdk_Defaults_Are_Not_Kept` |
| Application resource configuration (fix round 1) | `Later_ConfigureResource_Is_Reported_At_Start_For_Every_Signal_With_The_Schema_Conflict_As_Helper_Text` (tracer, meter, logger resources), `Later_SetResourceBuilder_On_Tracing_Only_Is_Reported_Naming_Traces`, `Earlier_ConfigureResource_Is_Not_Reported_As_A_Later_Change`, `Each_Earlier_Detector_Runs_Exactly_Once_Per_Provider`, `Each_Later_Detector_Runs_Exactly_Once_Per_Provider`, `Sdk_Disabled_Skips_The_Later_Change_Check`, `Host_App_Environment_On_Tracing_Only_Names_The_Lacking_Signals_Without_Not_Set`, `Host_Without_Any_Environment_Warns_Not_Set_Exactly_Once`, `App_Environment_Is_Used_When_None_Is_Resolved_And_Picks_The_Log_Level`, `App_Identity_Values_Replaced_By_Resolved_Ones_Are_Warned_Once_Per_Key`, `App_Explicit_Instance_Id_Is_Kept_When_The_Helper_Has_Only_The_Process_Id`, `Factory_Detector_Path_Names_An_Unresolved_Environment_Whose_Blank_Sdk_Value_Stays` |
| Code validation | `TracingAndOptionsHelperTests.Validator_Blank_Identity_Options_Fail_Naming_The_Option` × 2, `SignalIntegrationTests.Blank_Code_ServiceName_Fails_Naming_The_Option`, `Validator_Empty_Code_Key_Fails_At_Start` × 2, `Validator_Unicode_Space_Key_Is_Valid`, `Validator_Unsupported_Values_Fail_Naming_The_Key`, `Validator_Null_Resource_Attributes_Fail` |

Tests about another row set a service name and an environment in code
(`TestDiagnostics.Identified`), so they see only their own diagnostics.

## Endpoint parsing (task C7a)

`EndpointGrammarTests` (the grammar and one value, no environment) and
`EndpointResolutionTests` (`[Collection("EnvVarTests")]`: environment, code,
the validator) cover contract R1, R2, D1.a–c and the D1 edge-case rulings of
A17:

| Area | Tests |
|---|---|
| RFC 3986 split and ports | `Split_Follows_Rfc3986` × 9, `Split_Rejects_A_Malformed_Authority` × 8, `Ports_Compare_Numerically` × 11, `A_Port_Of_Thousands_Of_Digits_Never_Throws`, `Endpoint_Port_Is_Read_As_The_Resolver_Reads_It` × 7 |
| Host rules and IDN | `Valid_Hosts_Normalize` × 11 (lowercase, IPv6, trailing dot, punycode, deviation character, ideographic full stop), `Invalid_Hosts_Are_Rejected` × 25 (space, `%`, reg-name, one host rule, IPv6 literal, U+FFFD, ZWJ, fullwidth digits, empty label; hosts `System.Uri` refuses: `a!b`, `a$b`, `a~b`, `a=b`, `a..b`, `1.2.3.4..`), `An_Unescaped_Zone_Id_Is_Reported_As_Such`, `A_Non_Ascii_Host_Longer_Than_253_After_Punycode_Is_Invalid`, `Without_Uts46_A_Non_Ascii_Host_Is_Invalid_And_Says_Why` (globalization-invariant mode fails closed), `Invalid_Escapes_Are_Found` × 5 |
| Rendering | `Render_Matches_The_Resolver` × 11; `DiagnosticsTests.Endpoints_Render_Without_Credentials` (unchanged, now through the grammar) |
| One value | `Unparseable_Env_Values_Warn_The_Constant_And_Fall_Back` × 18, `Unparseable_Code_Values_Fail_Naming_The_Option` × 5, `Unsupported_Schemes_Leave_The_Signal_Unconfigured_Rendered_Redacted` × 3, `An_Invalid_Http_Path_Escape_Is_Invalid_For_Http_Only`, `Schemeless_Values_Are_Rewritten` × 7, `A_Schemeless_Value_With_An_At_In_The_Path_Renders_The_Constant`, `Portless_Grpc_Urls_Get_4317_With_A_Warning` × 3, `Http_Urls_Keep_Their_Path_And_Default_Port` × 4, `Grpc_Urls_Drop_The_Path`, `Userinfo_Query_And_Fragment_Are_Removed_With_A_Warning` × 5, `An_At_In_The_Path_Warns_Naming_The_Variable_Only`, `Env_Failures_Use_Gos_Wording_And_Warn_Once_Per_Raw_Value`, `The_Userinfo_Using_Is_The_Url_The_Signal_Uses`, `An_Env_Insecure_Contradicting_The_Scheme_Warns_And_The_Scheme_Wins` × 2, `A_Code_Insecure_Contradicting_A_Code_Url_Fails_Only_When_Both_Are_Code` |
| Resolution order and fallback | `An_Unparseable_Per_Signal_Value_Falls_Back_To_The_Generic_One`, `An_Unsupported_Per_Signal_Scheme_Leaves_Only_That_Signal_Unconfigured`, `A_Per_Signal_Env_Value_Is_Used_As_Written_And_Drives_The_Inference`, `Code_Code_Per_Signal_Env_Generic_Env_Is_The_Order`, `Generic_Endpoint_Reports_Empty_When_The_Env_Value_Is_Unusable`, `An_Idn_Host_Reaches_The_Exporter_Punycoded` |
| INSECURE | `An_Unparseable_Insecure_Warns_Without_An_Endpoint` × 2, `A_Shadowed_Insecure_Stays_Silent`, `The_Per_Signal_Insecure_Beats_The_Generic_One_For_Schemeless_Values`, `An_Unparseable_Insecure_Means_False` |
| Protocol | `Per_Signal_Protocol_Beats_The_Generic_One_And_Bad_Values_Fall_Through`, `A_Bad_Generic_Protocol_Is_Warned_Once_And_Grpc_Is_Used` × 2, `A_Code_Protocol_Beats_Every_Variable_And_The_Inference`, `The_4318_Inference_Is_Logged_Once_For_Three_Signals` |
| gRPC path warning (task C7b, A29) | `A_Grpc_Path_Warns_Naming_The_Variable_Only` × 7 (URL, schemeless, `//`; not `/`, empty or query-only), `An_Http_Path_Is_Not_Warned_And_The_Grpc_Path_Key_Ignores_The_Value`, `The_Grpc_Path_Warning_Is_Written_Once_Per_Variable` × 2, `A_Skipped_Per_Signal_Value_With_A_Path_Is_Not_Warned`, `A_Code_Grpc_Path_Warns_And_Does_Not_Fail_Start` |
| Exporter targets (task C7b audit) | `Exporter_Options_Carry_Each_Signals_Target` × 3 (per-signal HTTP for every signal, used as written also without a path; the others generic + `/v1/<signal>`), `A_Signal_Without_A_Target_Gets_No_Exporter_Options` |
| Warning cadence | `The_Userinfo_Warning_Is_Written_Once_Per_Variable_And_Value`, `A_Generic_Http_Query_Is_Stripped_Before_The_Signal_Path` (also on `OtlpExporters.OptionsFor`), `Code_Warnings_Reach_The_Sink_At_Start` |
| Code validation | `Invalid_Code_Endpoints_Fail_At_Start` × 5, `A_Code_Http_Path_Escape_Fails_Only_For_Http_Protobuf`, `A_Code_Insecure_Contradicting_A_Code_Url_Fails`, `The_Same_Error_Is_Listed_Once_And_A_Shadowed_Generic_Value_Is_Not_Checked`, `Schemeless_And_Credential_Bearing_Code_Values_Are_Accepted` × 3; `TracingAndOptionsHelperTests.Validator_Fails_On_Unsupported_Endpoint_Redacted` and `Validator_Per_Signal_Endpoints_Need_An_Http_Url` (schemeless per-signal code values are accepted since C7a, as in Go and Python) |

The IDN tests need ICU (the SDK images have it). Under
`DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1` the tests that expect a punycode
conversion fail by design (the helper rejects non-ASCII hosts there) and the
invalid-host tests still pass; measured 2026-11-10: 10 of 283 endpoint and
diagnostics tests fail in that mode, all of them IDN conversions or their
reason text.

`DiagnosticsTests.Sdk_Text_Is_Redacted` also covers the exception-message
exemption (fix round 2): a pair is kept only when the name is a type name
(`^[A-Z][A-Za-z0-9]*(Exception|Error)$`), the separator is `:` plus whitespace
and the value starts like prose (`AuthenticationException: Cannot …`);
`TokenError: eyJ…`, `AuthError=x`, `X-Auth-Error: x` and `session_token_error: x`
are masked.

Endpoint warnings use Go's wording (`<why>; ignored`, the variable as an
attribute); the other environment warnings keep the .NET form
`invalid <VAR>: …` (task C5).

## Exporter settings (task C8)

`ExporterSettingsTests` (`[Collection("EnvVarTests")]`; contract A18, R2, R8,
G10; ADR-013) — each rule was checked to fail its test when the rule is
removed (mutation run, 2026-11-10):

| Area | Tests |
|---|---|
| Headers | `Header_Grammar` × 17 (key set, keys not decoded, whole-value literal on an invalid escape or non-UTF-8 bytes, non-ASCII and control characters skipped, trim after decoding, last key wins, empty items), `Header_Warnings_Name_The_Variable_And_The_Position_Never_A_Value`, `Per_Signal_Headers_Replace_The_Generic_Ones_And_An_Unusable_Per_Signal_Value_Falls_Back`, `Bin_Header_Values_Are_Base64_On_Grpc_Only` × 3, `Content_Header_Rejected_By_The_Client_Is_Warned_By_Key` |
| Timeout | `Timeout_Grammar` × 18 (decimal grammar, 0, sign, exponent, bare dot, hex, clamps at 1 and 86 400 000), `Per_Signal_Timeout_Falls_Back_To_The_Generic_One_And_Code_Wins`, `Timeout_Bounds_The_Exporter_The_Client_The_Batch_Processors_And_The_Reader` (also the three ignored `*_EXPORT_TIMEOUT` warnings), `A_Hanging_Collector_Ends_The_Export_At_The_Deadline`, `Grpc_Timeout_Header_Encoding` × 6, `Handler_Sends_Grpc_Timeout_On_Grpc_Only_And_Forwards_Send_And_SendAsync` |
| Compression | `Compression_Defaults_To_Gzip_Per_Signal_With_Fallback_And_Code_Wins` |
| TLS files | `Plaintext_Target_Ignores_The_Tls_Files_With_One_Warning_Naming_Them`, `Ca_Per_Signal_Unreadable_Or_Not_Pem_Falls_Back_To_Generic_Then_The_System_Roots`, `Client_Pair_Incomplete_Or_Rejected_Uses_Server_Authentication_And_A_Good_Pair_Is_Presented`; `ResolverCoreTests.Tls_Files_From_The_Environment_Are_Loaded_Or_Warned` |
| Client duties | `One_Client_Per_Exporter_Instance_With_The_Sdk_Response_Bound_And_No_Redirects`, `A_Redirect_Is_Refused_And_Reported_Naming_The_Signal_And_The_Status` × 12 (traces, metrics, logs × 301, 302, 307, 308 on a local `HttpListener`: one ERROR naming signal and status, the `Location` never requested nor printed) |
| Fix round 1 | `Application_Set_Export_Timeouts_Are_Warned_Naming_The_Option`, `Untouched_Sdk_Options_Or_A_Set_Variable_Give_No_Option_Warning`, `Histogram_Path_Deadline_Beats_A_Later_Named_Configure`, `Oversized_Or_Device_Tls_Files_Are_Refused_As_Unreadable`; `ResolverCoreFixRoundTests.Only_The_Resolved_Otlp_Keys_Are_Dropped` (per-signal keys) |
| Batch size, value length | `Batch_Size_Above_The_Queue_Size_Warns_Naming_Both` × 6, `Batch_Size_Above_The_Queue_Size_In_The_Options_Is_Clamped`, `Env_Batch_Size_Above_The_Queue_Size_Builds_And_The_Processors_Use_The_Queue_Size`, `Zero_Value_Length_Limit_Warns_Naming_Only_That_Variable` × 5 |

The wire side is the parity fixture: every C8 case runs through the helper
client on both protocols (`export.*headers*`, `export.*timeout*` via the
receiver's `grpc-timeout`, `export.*compression*`, and the gRPC TLS cases on
the receiver's TLS port 14317 — `export.grpc_certificate_*`,
`export.grpc_client_*` — so gRPC TLS through the helper handler is measured,
no `skip:no-upstream-dotnet-grpc-tls-files` is needed). The processor and
reader timeouts are read from SDK internals by reflection (test only).

## Signals and exporters (task C9)

`SignalsExportersTests` (`[Collection("EnvVarTests")]`; contract A19, R2, R8;
ADR-013) — each rule was checked to fail its test when the rule is removed
(13 mutants, 2026-11-10; fix round 1: 6 more, every one caught by a unit test
— the `otlp`-guard and logs-`none` mutants, parity-only in the first round, now
fail `Otlp_Is_Exported_Only_When_Listed` too, without the fake receiver):

| Area | Tests |
|---|---|
| Traces and logs exporter lists | `Trace_And_Log_Exporters_Default_To_Otlp` × 2, `Traces_Exporter_List_Is_Parsed` × 5 (case, trim, de-duplication, `none`), `Logging_Is_A_Warned_Alias_Of_Console` × 3 (traces, logs, metrics), `Logging_And_Console_Together_Are_One_Console_Exporter`, `Unknown_Trace_Exporter_Warns_And_Only_Unknown_Uses_The_Default`, `Prometheus_Is_Not_A_Logs_Exporter`, `None_Combined_Warns_And_Wins` × 2, `Blank_Or_Separator_Only_Exporter_Variable_Is_Unset`, `Sdk_Disabled_Leaves_No_Signal_Exporter` |
| Metric exporters | `Console_Metric_Exporter_Is_Accepted_From_Env_And_Code`, `Code_Logging_Metric_Exporter_Fails_Naming_It`, `Env_Otlp_Alone_Without_Endpoint_Warns_And_Falls_Back_To_Prometheus`, `Env_Otlp_And_Console_Without_Endpoint_Keeps_Console`, `Env_Otlp_With_A_Metrics_Endpoint_Is_Kept` |
| Deprecated disabled signals | `A_Set_Standard_Exporter_Variable_Wins_Over_Disabled_Signals` × 4, `Standard_None_Counts_As_Set_And_Turns_The_Signal_Off`, `Standard_Variable_With_Only_Unknown_Entries_Counts_As_Unset`, `Env_Otlp_Dropped_For_No_Endpoint_Still_Counts_As_Set`, `Code_Metric_Exporters_Win_Over_Disabled_Signals_For_Metrics_Only`, `Code_Disabled_Signals_Win_Over_A_Set_Standard_Variable`, `Unknown_Disabled_Signal_Entry_Warns_Once_Echoing_It`, `Unknown_Code_Disabled_Signal_Fails_Naming_The_Option` |
| Export interval | `Interval_Valid_Values_Are_Used_Without_A_Warning` × 6, `Interval_Invalid_Values_Warn_And_Use_30000` × 12 (`0`, `+0`, `5_000`, `-5000`, `++5000`, `5e3`, non-ASCII digits, …), `Interval_Above_The_Maximum_Is_Clamped_With_A_Warning` × 4, `Interval_Of_Thousands_Of_Digits_Is_Clamped_And_Echoed_Truncated`, `Interval_Code_Value_Beats_An_Invalid_Env_Value_Silently`, `Interval_Only_In_IConfiguration_Is_Not_Read_And_Warns_Once` × 2 (round 2), `Interval_In_The_Environment_Seen_Through_IConfiguration_Gives_No_Not_Read_Line`, `Interval_In_Code_Gives_No_Not_Read_Line` |
| `OTEL_SDK_DISABLED` | `Sdk_Disabled_Unparseable_Warns_Naming_The_Variable_And_Means_False` × 4, `Sdk_Disabled_Booleans_Do_Not_Warn` × 3, `Sdk_Disabled_Unparseable_In_IConfiguration_Warns_Too` |
| Exemplar filter | `Exemplar_Filter_Is_Parsed` × 4, `Exemplar_Filter_Unknown_Warns_And_Is_Trace_Based`, `ExemplarFilter_DefaultsToTraceBased_AndHonorsEnv` × 4 (the test ADR-013 names: exemplars counted by a reader on the built provider, under and without a sampled span), `An_Application_SetExemplarFilter_Registered_Later_Wins` |
| Built pipeline | `Traces_None_Leaves_The_Service_Source_Without_A_Listener`, `Console_Exporters_Write_Spans_Logs_And_Metrics_To_Standard_Output` (redirects `Console.Out` for the test; the parity export cases check the same output per process), `Otlp_Is_Exported_Only_When_Listed` × 4 (a local `TcpListener` as the OTLP endpoint: `console` traces and `none` logs open no connection, `otlp` does) |
| Fix round 1 | `Code_Empty_Disabled_Signals_Are_Set_And_Win_Over_The_Env` × 2, `Code_Empty_Metric_Exporters_Are_Set_Mean_None_And_Win` × 2, `Code_Empty_Metric_Exporters_Say_So_Once_At_Info` (round 2), `Unknown_Entry_And_None_Combined_Each_Get_Their_Own_Line`, `Plus_Prefixed_Interval_Gives_No_Sdk_Line`, `Unparseable_Sdk_Disabled_Is_Warned_Once_By_The_Helper_Only`, `Sdk_Parse_Warnings_Of_Helper_Owned_Rows_Are_Noise` × 3; `DiagnosticsTests.Known_Noise_Is_Not_Forwarded` now treats the SDK's `OTEL_SDK_DISABLED` parse line as noise |

The wire side is the parity fixture: `export.{traces,logs}_exporter_{none,console,logging_alias,none_list}`,
`metrics.exporter_*`, `metrics.interval_*`, `config.disabled_signals_*` and
`config.sdk_disabled_*`, `config.code_disabled_signals_empty_wins` and
`metrics.exemplar_filter_*` pass; since fix round 1
`config.disabled_signals_standard_var_wins` asserts only the precedence
(`pass`) and `deprecation.disabled_signals_standard_var_set_still_warns` the
deprecation (`xfail:C16`). The driver reads `exemplar_filter` from the built
`MeterProviderSdk` by reflection (driver only).

---

## Sampler, propagators and debug (task C10)

`SamplerPropagatorDebugTests` (`[Collection("EnvVarTests")]`; contract R6, R8,
R9, A20; ADR-002, ADR-006; design D-NET9, N2, N3). Each rule was checked to
fail its test when the rule is removed (16 mutants, 2026-11-10: bare
`AlwaysOn` default, bare ratio alias, alias clamped, unknown name or argument
silent, runtime propagator not replaced or not undone, an application
propagator replaced, `Activity.Parent == null` as the debug test, no restore
in `StartRootActivity`, sampler set only when traces are on, a single
propagator wrapped in a composite (caught by the type test only: the wire is
the same), no install at resolution, `none` not winning, unsupported tokens
reported as unknown, the `IConfiguration` sampler warning removed). The
`IConfiguration` propagator warning was not mutation-checked (two mutant forms
did not compile); `Value_Only_In_IConfiguration_Is_Not_Read_And_Warns_Once`
covers it. Fix round 1 (2026-11-10): 9 more mutants, each failing a test — the container's propagator not replaced, `Current` not set, no pass-through for disabled traces, no continuation listener, the disabled-traces sampler, no OpenTelemetry pass-through wrapper, extraction ignoring the settings, Activity baggage injected unfiltered (caught after `Runtime_Propagator_Fields_And_Extraction_Follow_The_Settings` gained an injection check).

| Area | Tests |
|---|---|
| Sampler names | `Each_Spec_Name_Builds_Its_Sampler` × 9 (the six names, ARG 0 and 1, case and trim, each decided for a root, a sampled and an unsampled remote parent), `Default_Is_ParentBased_Always_On`, `Unknown_Or_Unsupported_Name_Warns_Once_And_Uses_The_Default` × 5 (unknown, U+0130, the three unsupported spec names) |
| Sampler argument | `Invalid_Sampler_Argument_Warns_And_Uses_One` × 9 (`2`, `-0.5`, `+0.5`, `1e-1`, `0x1`, `NaN`, `.5`, `0,5`, Arabic-Indic digit), `Sampler_Argument_Is_A_Plain_Decimal` × 3, `Argument_Of_A_Non_Ratio_Sampler_Is_Ignored_Silently` |
| Ratio alias | `Ratio_Alias_Is_ParentBased`, `Invalid_Ratio_Alias_Warns_And_Is_Ignored_Not_Clamped` × 5, `Ratio_Alias_Is_Not_Read_While_The_Standard_Variable_Is_Set`, `Code_Sampler_And_Ratio_Win_Over_Every_Variable_Without_A_Line` |
| `IConfiguration` only | `Value_Only_In_IConfiguration_Is_Not_Read_And_Warns_Once` × 3 (`OTEL_TRACES_SAMPLER`, `_ARG`, `OTEL_PROPAGATORS`), `Values_In_The_Environment_Seen_Through_IConfiguration_Give_No_Not_Read_Line`, `Sampler_In_IConfiguration_With_A_Code_Sampler_Gives_No_Line`, `The_Sdk_Does_Not_Apply_A_Sampler_Set_Only_In_IConfiguration` (a host with `always_off` in `IConfiguration` still samples) |
| Propagator parsing | `Propagators_Are_Parsed_Per_Token` × 15 (exact diagnostic lines: per-token "not supported"/"unknown", `none` combined, `using=` the effective list, case, empty tokens, duplicates, U+0130) |
| Propagator install | `The_Default_TextMap_Propagator_Is_The_Resolved_List` × 4, `A_Lone_TraceContext_Is_The_Sdk_TraceContext_Propagator`, `Propagators_Are_Installed_Under_Sdk_Disabled` × 2, `Hostless_Sdk_Disabled_Installs_Once_The_Helper_Settings_Resolve` |
| `HttpClient` on the wire | `HttpClient_Sends_Only_The_Configured_Context` × 4 (default, `tracecontext`, `baggage`, `none`; one `traceparent` at most), `HttpClient_Follows_The_List_Under_Sdk_Disabled` × 2 (a root sends nothing, a continued trace its inbound `traceparent`), `The_Runtime_Propagator_Follows_The_Last_Container`, `An_Application_Runtime_Propagator_Is_Kept_And_Warned_When_It_Matters` × 2 (a local `TcpListener` records the request headers) |
| Web host, both directions (fix round 1) | `Web_Host_Extracts_And_Injects_Only_The_Configured_Context` × 4 (inbound `traceparent …-01` + `baggage: user.id=42`; the server span's trace id and parent, the headers its `HttpClient` call sends, for `none`, `baggage`, `tracecontext` and the default), `The_Helper_Runtime_Propagator_Wins_In_Every_Host_Registration_Order` (`WebApplication`, `HostBuilder` with `AddOtelHelper` before and after `ConfigureWebHostDefaults`), `An_Application_Registered_Runtime_Propagator_Service_Is_Kept` × 2 (warned when the settings are not the default; fix round 2), `Runtime_Propagator_Fields_And_Extraction_Follow_The_Settings` × 4 (fields, extraction, injection of Activity baggage) |
| Early handlers (fix round 2) | `HttpClient_Created_Before_Build_Follows_The_List` × 3 (`none`, `tracecontext` with Activity baggage, default), `A_Current_Set_By_The_Application_After_AddOtelHelper_Is_Kept_And_Warned`, `Invalid_Context_Injection_Is_Noise_Only_When_Expected` × 2; `Disabled_Traces_Root_With_Default_Logging_Sends_No_Context` also checks that the SDK's "invalid context" line is not forwarded, and `Pass_Through_Injection_Uses_The_Local_Root_Remote_Parent` that a local explicit-context child carries nothing |
| Disabled traces (fix round 1) | `Disabled_Traces_Propagate_As_A_No_Op_Tracer` × 8 (env and code disabled signals, `OTEL_SDK_DISABLED` without logging providers, `none`: a root sends nothing, a continued trace its inbound `traceparent` and `tracestate` unchanged, `none` starts a sampled trace), `Disabled_Traces_Root_With_Default_Logging_Sends_No_Context`, `Disabled_Traces_Sampler_Does_Not_Sample_Roots` × 2, `Pass_Through_Injection_Uses_The_Local_Root_Remote_Parent` |
| Traces off | `Traces_Off_Server_Passes_The_Inbound_Trace_On` × 3: an ASP.NET Core app with `OTEL_TRACES_EXPORTER=none` forwards the inbound trace id and flag (`01` → `01`, `00` → `00`); with an `AlwaysOffSampler` it would forward `00` (the rejected alternative) |
| Debug marker (N2) | `Debug_Marks_A_Root_Without_Touching_The_TraceState`, `Debug_Marks_A_Remote_Parent_Entry_Span_And_Keeps_Its_TraceState_Once`, `Debug_Does_Not_Mark_An_In_Process_Child`, `Debug_Does_Not_Mark_A_Child_Started_From_An_Explicit_Local_Context`, `Debug_Is_Installed_Only_In_Debug_Mode` (also: never from an inbound `debug=true` tracestate) |
| `StartRootActivity` (N3) | `StartRootActivity_Restores_The_Previous_Activity_When_Disposed`, `StartRootActivity_Without_A_Previous_Activity_Leaves_None`, `StartRootActivity_That_Creates_Nothing_Leaves_The_Current_Activity`, `StartRootActivity_Restore_Needs_The_Runtime_Field` |

The wire side is the parity fixture: the 37 cases of the C10 group (five added in fix round 1, with the `http_server` field `server_continues_inbound`)
(`sampler.*`, `debug.*`, `propagation.*`, `config.code_sample_ratio_*`) pass.
The ParentBased default also makes `http.health_child_span_dropped` (C11 group)
pass: a child of a dropped health span now follows the drop.

## HTTP/gRPC instrumentation and health (task C11)

`HttpGrpcInstrumentationTests` (`[Collection("EnvVarTests")]`; contract R7,
R10, D5, A22, A30; requirements DN5, DN7; design D-NET8, N1, N5), 131 cases.
The web-host tests run Kestrel on a loopback port (HTTP/1.1, or HTTP/2 for
gRPC with raw-byte methods, no protoc) and select spans by `server.port`,
because every ASP.NET Core host in the process reports to the same
instrumentation; raw HTTP requests go over a socket so the target reaches
Kestrel as written. Each rule was checked to fail a test when removed (11
mutants, 2026-11-10: no `SuppressDownstreamInstrumentation`, no gRPC client
filter, HttpClient filter without the gRPC health service, no percent-decoding,
no HTTP opt-in view, the gRPC check on `ApplicationStopped` instead of
`ApplicationStarted`, the key ignored, the invalid-boolean warning removed,
the `HealthPaths` validation removed, the 0.2.x server filter on
`HttpRequest.Path` without the gRPC health service; the last one is caught
only by the uninstrumented-client tests `The_Server_Filter_Drops_Grpc_Health_Calls_Of_An_Uninstrumented_Client`
and `Grpc_Health_Service_Is_Dropped_Whatever_The_Health_List` — with an
instrumented client the filtered client span sends an unsampled parent, which
ParentBased drops on its own; since fix round 1 also by the parity cases
`grpc.health_check_no_span` and `grpc.health_watch_no_span`).
Fix round 1 (2026-11-10): 3 more mutants, each failing a test — no header
removal in the HttpClient health filter, no health check in the runtime
propagator (both needed: the instrumentation injects with the OpenTelemetry
propagator before its filter, the runtime injects through
`DistributedContextPropagator.Current`), the absolute-form fallback decoded twice. Fix round 2 (2026-11-10): 3 more mutants, each failing a test — every
context header removed (the application's own lost), no header removal, no
health check in the pass-through propagator. Not mutation-checked: the runtime
propagator keeping baggage on a health request (no test sets Activity baggage
there).

| Area | Tests |
|---|---|
| Normalization | `Normalize_Follows_The_Contract` × 15 (query, fragment, trailing slashes, `%68`, `%2f`, a decoded `?`/`#`, malformed escapes, non-UTF-8 bytes, UTF-8, ASCII-only folding), `The_Grpc_Health_Service_Is_Matched_Whole_And_Case_Sensitive` × 9, `The_Default_List_Is_The_Contract_List` × 11, `Default_List_Matches_Go` |
| `HealthPaths` validation | `Invalid_Code_Health_Path_Fails_Start` × 6, `Null_Code_Health_Path_Fails_Start`, `Valid_Code_Health_Paths_Pass` |
| Server | `Server_Health_Requests_Get_No_Span_And_Are_Counted` × 9 (still counted in `http.server.request.duration`), `Server_Paths_That_Only_Contain_A_Health_Path_Are_Traced` × 4, `Code_Health_Paths_Replace_The_Default_List`, `An_Empty_Health_List_Traces_Every_Path`, `Children_Of_A_Health_Request_Are_Dropped_And_Of_Others_Kept` |
| HttpClient | `HttpClient_Health_Requests_Get_No_Client_Span` (also `/grpc.health.v1.Health/Check`), `HttpClient_Follows_Code_Health_Paths`, `HttpClient_Health_Requests_Send_No_Context` × 12 (fix rounds 1 and 2: a local `TcpListener` records the headers; health paths get no `traceparent` or `tracestate` inside a sampled request and as a root, with the default list and a lone `tracecontext`; an application's own `baggage` and `tracestate` survive; with traces disabled a health call carries nothing and `/orders` the inbound `traceparent`; `/orders` keeps the context) |
| Server (fix round 1) | `Absolute_Form_Targets_Are_Decoded_Once` × 3 (`GET http://host/%2568ealth` is traced), `An_Application_Filter_Registered_After_AddOtelHelper_Replaces_The_Health_Filter` × 2 |
| gRPC | `One_Client_Span_Per_Grpc_Call_And_The_Server_Continues_It` (the only CLIENT span is GrpcNetClient's `parity.v1.Echo/Ping`, the parent of the server span `parity.v1.Echo/Ping` under scope `Microsoft.AspNetCore` with `rpc.system.name=grpc`), `Grpc_Not_Found_Is_Unset_On_The_Server`, `Grpc_Health_Calls_Get_No_Server_Or_Client_Span` × 2 (Check, Watch), `The_Server_Filter_Drops_Grpc_Health_Calls_Of_An_Uninstrumented_Client` × 2 (with and without the key), `Grpc_Health_Service_Is_Dropped_Whatever_The_Health_List`, `Without_The_Key_Grpc_Server_Spans_Are_Http_Shaped` (`POST …`, status 200, UNSET for NOT_FOUND) |
| O7 (b) warning | `Mapped_Grpc_Endpoints_Without_The_Key_Warn_Once` × 8 (environment and `IConfiguration` values, `bool.TryParse` reading, exact line), `No_Grpc_Endpoint_No_Warning`, `No_Warning_When_Traces_Are_Off` × 3, `The_Grpc_Metadata_Type_Name_Is_The_Pinned_One` (the D-NET8 TO VERIFY at Grpc.AspNetCore.Server 2.62.0) |
| Keyed DI (N1) | `In_A_Web_Host_Unkeyed_ActivitySource_Is_AspNetCore_And_Server_Spans_Keep_Its_Scope`, `In_A_Worker_Unkeyed_Instrumentation_Is_Null` |
| Meters (N5) | `Service_Meter_Children_And_Additional_Meters_Are_Exported` (`svc`, `svc.x`, `svc.a.b`, `svc.` and `AdditionalMeters`; not `svcx`, a child of an additional meter, or an unrelated meter), `Idle_Metrics_Drop_The_Service_Meter_Children_Too`, `Quiet_Meter_Notices_Cover_The_Service_Meter_Children` × 4 |
| HTTP opt-in metrics (A30) | `Http_Opt_In_Metrics_From_The_Environment` × 7, `Http_Opt_In_Metrics_Code_Wins_And_The_Variable_Is_Not_Read` × 3, `The_Opt_In_View_Is_Scoped_To_The_Framework_Http_Meters` × 9, `Http_Opt_In_Metrics_Switch_Controls_The_Framework_Series` × 2 (with the INFO line), `An_Application_Instrument_With_An_Opt_In_Name_Is_Kept`, `Disabled_Metrics_Still_Drop_A_Switched_On_Series` |

The wire side is the parity fixture: the C11 group (`http.*`, `grpc.*`,
`metrics.http_server_duration_histogram_name`, 30 cases) has 25 `pass`; the
two `grpc.health_*_counted_in_metrics` (`skip:no-upstream-rpc-metrics`) and
three body-size cases (`skip:no-upstream-dotnet-http-body-size`) stay skipped.

## Prometheus fallback (task C12)

`PrometheusFallbackC12Tests` (`[Collection("EnvVarTests")]`; contract A21,
A29, N8; design D-NET11, D-NET3), 104 cases with `PrometheusListenerTests` (120 after fix round 1).
The bind tests need the container's own non-loopback IPv4 address (compose
`dotnet-tests` has one, `eth0`); since fix round 1 `LoopbackHost_RefusesANonLoopbackConnect`
and `UnspecifiedIpv4Host_BindsEveryIpv4Interface` are skipped with a reason
without it, and the loopback tests check the refusal only when one exists. Each rule was checked to fail a test when
broken (7 mutants, 2026-11-11: glob case-insensitive again, the bind ignoring
the host, the listener reader registered with OTLP too, the two port
variables swapped, IPv4 leading zeros accepted, the push readers not tracked
for `ForceFlushOtelHelper`, the invalid-host warning removed).

| Area | Tests |
|---|---|
| Port grammar (A21) | `PortGrammar_Valid` × 6, `PortGrammar_Invalid` × 12, `UnparseableHelperPort_Warns_And_Uses9464` (silent before C12), `HelperPort_IsTrimmedWithTheContractSet` (U+00A0 kept) |
| Port precedence (A29) | `StandardPort_BeatsHelperPort_WithoutWarning`, `InvalidStandardPort_Warns_And_FallsToHelperPort`, `BothPortsInvalid_EachWarnsOnce_9464`, `StandardPortZero_IsNoListener_NotTheNextLevel`, `EmptyStandardPort_IsUnset`, `CodePort_Beats_BothVariables_AndThey_AreNotRead`, `InvalidPortEcho_LosesUserinfoAndQuery` |
| Host grammar (A29) | `HostGrammar_Valid` × 15, `HostGrammar_Invalid` × 19, `InvalidHost_WarnsOnce_FailsOpen_And_RedactsTheEcho`, `Host_IsTrimmed_AndBlankIsUnset`, `Host_IsParsedAndWarned_EvenWhenTheListenerDoesNotRun` |
| Bind | `LoopbackHost_RefusesANonLoopbackConnect` (the negative control: with `127.0.0.1`, a connect to the container's `eth0` address is refused; unset, the same connect gets 200), `SpecificHost_AnswersOnlyRequestsNamingIt` (`Host: localhost` → 404 on a `127.0.0.1` bind), `UnspecifiedIpv4Host_BindsEveryIpv4Interface`, `Ipv6Host_FailsTheBuild_WithOneHelperError`, `Ipv6Host_WithoutListener_IsNotAnError`, `UnownedHost_FailsTheBuild_LikeABusyPort` (TEST-NET-1 `192.0.2.1`, and a busy port) |
| Reader registration | `ListenerReader_IsRegistered_OnlyWhenItBinds` × 5, `ApplicationListener_KeepsItsOptions_WhenTheHelperListenerIsInactive` |
| Flush | `OtlpOnly_SdkForceFlushAndShutdown_ReportTheRealResult` (both `true`; `false` before C12), `DualMode_SdkForceFlushIsFalse_HelperFlushIsTrue`, `HelperFlush_IsFalse_WhenTheOtlpExportFails` (receiver answering 500), `HelperFlush_FlushesTracesAndLogs` (a 500 still gives `true` there: batch-processor semantics), `HelperFlush_HistogramPath_UsesTheMeterProvider`, `HelperFlush_ArgumentsAndNoHelper`, `HelperFlush_SdkDisabled_IsTrue` |
| Disabled-metric glob (A21) | `DisabledMetricGlob` × 11, `DisabledMetricGlob_ItemsTrimmedWithTheContractSet`, `DisabledMetrics_CaseSensitive_OnTheScrape` |

*Fix round 1 (2026-11-11):* `PrometheusFallbackC12Tests` has 112 cases (120 with `PrometheusListenerTests`); 7 more mutants each fail a test (no `localhost` prefix, no `::1` mapping, other IPv6 widened, default-named options, no SDK noise entry, no bind-failure ERROR, `$` instead of `\z`).
`SpecificHost_AnswersOnlyRequestsNamingIt` became
`LoopbackHost_AnswersLocalhostAnd127_StillLoopbackOnly` × 3 and
`OtherIpv4Host_RoutesByHostHeader` (127.0.0.2: 404 for `Host: localhost`); the
IPv6 tests became `Ipv6LoopbackHost_Binds127001_WithOneWarning` × 3,
`OtherIpv6Host_NoListener_OneError_StartSucceeds` × 2,
`UnspecifiedIpv6Host_EveryIpv4Interface_OneWarning` × 2 and
`Ipv6Host_WithoutListener_IsNotReported`; the bind failures
`UnownedHost_FailsTheBuild_AfterOneHelperError` and
`BusyPort_FailsTheBuild_AfterOneHelperError_NamingThePortSource`; new
`HelperAndApplicationListeners_BothActive_OnTheirOwnPorts` (named options),
`PrometheusPortVariable_IsWarnedByTheHelperOnly` × 2 (SDK noise),
`HelperFlush_DisposedProvider_Throws`,
`OtlpOnly_SdkResults_AfterA500_OnlyTheHelperFlushIsFalse`, and a `\z` glob row.

A test that builds a provider expecting the build to fail goes through
`BuildMetricsOnly`, which disposes the container: a `TracerProvider` left
alive by a failed build keeps sampling other tests' activities (it made two
`SamplerPropagatorDebugTests` cases fail in a full run).

The wire side is the parity fixture: of the 26 `xfail:C12` cases 23 pass;
`metrics.scrape_gauge_unit_one_ratio` and `metrics.scrape_ratio_inside_name`
are `skip:no-upstream-dotnet-prom-ratio`, `metrics.scrape_colliding_label_values_sorted`
`skip:no-upstream-dotnet-prom-label-order`. Across containers:
`testing/e2e/dotnet-prom-scrape.sh` (the second app, bound to `127.0.0.1`,
refuses the curl container).

## Logging (task C13)

`StdoutLoggingTests` (`[Collection("EnvVarTests")]`; contract D6, R4, A23;
design D-NET10, requirement DN8, N9). The stdout provider's `Output` and
`Clock` are internal seams: the container tests point `Output` at a
`StringWriter`, so no test redirects the process-wide `Console.Out`. Parity
cases: `logging.*` (6; `logging.stdout_line_shape` and
`logging.stdout_reserved_keys_dropped` added with this task, with the
`logging` kind input `attributes`; `logging.stdout_logger_field` in fix round 1).

| Area | Tests |
|---|---|
| Line shape (A23) | `Line_Has_The_Contract_Fields_In_Order_Compact_And_One_Line`, `Time_Is_Utc_With_Z_And_Truncated_Milliseconds_Whatever_The_Offset`, `Level_Uses_The_Four_Contract_Words` × 6, `Control_Characters_Are_Escaped_Non_Ascii_And_Html_Characters_Are_Not` |
| Fields and values | `Reserved_Keys_Are_Dropped_And_Other_Attributes_Follow_Logger`, `Reserved_Keys_Are_Case_Sensitive`, `Template_Values_Become_Attributes_Without_The_Original_Format_And_Duplicates_Keep_The_First`, `Exception_Is_The_Last_Field_And_Replaces_An_Attribute_Of_That_Name`, `An_Exception_Attribute_Is_Kept_When_The_Record_Has_None`, `Values_Keep_Their_Json_Type_And_The_Rest_Becomes_Text`, `Hostile_Values_Never_Lose_The_Record`, `A_State_That_Is_Not_Pairs_Gives_Only_The_Message_And_A_Null_Formatter_Uses_Its_Text` |
| Provider | `Logger_Writes_Through_Output_Only_When_Active_And_Never_Throws`, `Default_Output_Is_The_Current_Console_Out`, `A_Second_AddOtelHelper_Registers_No_Second_Stdout_Provider`, `Host_Writes_The_Line_And_Keeps_The_App_Console_Provider` |
| When it writes (D6) | `Fallback_Writes_Exactly_When_No_Logs_Exporter_Is_Active` × 9 (no endpoint, endpoint, `none`, `console`, `otlp,console`, `otlp` without endpoint, logs or traces disabled; the idle provider held at `None`), `Sdk_Disabled_Writes_The_Fallback`, `Unusable_Endpoint_Writes_The_Fallback`, `Fallback_Line_Written_Through_The_Container_Has_The_Category_And_The_State` |
| Level (R4) | `Stdout_Level_Follows_R4_And_Code_Wins` × 8, `Debug_Mode_Gives_Debug_And_Keeps_Framework_Categories`, `Level_Reapplies_When_The_App_Environment_Arrives` |
| N9 (helper's providers only) | `App_Provider_Keeps_Its_Level_And_Framework_Warnings_While_The_Helper_Holds_Its_Own`, `App_Category_Rules_Without_Provider_Still_Apply_To_The_Helper_Providers_Also_When_Registered_Later`, `App_Rule_For_A_Framework_Category_Replaces_The_Helper_Error_Rule`, `Longer_App_Category_Beats_The_Helper_Framework_Rule`, `App_Filter_Without_Level_Leaves_R4_And_Still_Runs`, `Provider_Specific_App_Configuration_Wins_For_That_Provider` × 3 (a lower-case alias is not applied, as in the framework), `Otel_Provider_Alias_Is_Read_From_The_Sdk`, `Apply_Helper_Rules_Without_App_Rules`, `Apply_Helper_Rules_Leaves_Slots_The_App_Set_By_Alias` |
| Profiling category (kept, 2026-11-11) | `Profiling_Prerequisite_Warning_Keeps_The_OtelHelper_Profiling_Category` |

The parity driver's `log_level` now reads the lowest level a `parity` logger
enables (only the helper's providers remain after `ClearProviders()`), not
`LoggerFilterOptions.MinLevel`, which the helper no longer sets; the C6
`IdentityResourceTests` that asserted `MinLevel` read `LoggingSetup.HelperLevel`.

*Fix round 1 (2026-11-11, owner decision: the application's explicit level wins):*
`Template_Appsettings_In_Prd_Exports_Information_The_App_Default_Wins` (the
ASP.NET Core template `appsettings.json` in PRD exports Information, framework
Information still dropped), `App_Default_Through_AddFilter_Wins_Over_R4`,
`App_SetMinimumLevel_Wins_Over_R4` × 2, `App_SetMinimumLevel_Is_Detected`,
`Code_MinimumLogLevel_Wins_Over_The_App_Default_And_The_Provider_Slot`,
`Apply_Helper_Rules_App_MinLevel_Gives_No_Default_Rule`,
`Host_Console_Provider_With_The_Fallback_Warns_Once` × 3,
`Lone_Surrogates_In_Message_Value_And_Key_Become_Replacement_Characters`,
`Characters_Outside_The_Bmp_Are_Written_Raw_And_A_Literal_Backslash_U_Stays_Escaped`,
`Collections_Are_Capped`, `A_Failure_Outside_Format_Guards_Writes_The_Minimal_Line`.
8 more mutants: 7 each fail a test (app default ignored, `SetMinimumLevel`
ignored, code not winning over the provider slot, no duplicate-console warning,
no raw supplementary characters, cap doubled, no minimal line); dropping
`Clean` on the message survives, because `Utf8JsonWriter` already writes U+FFFD
for a lone surrogate (an equivalent mutant, measured).

## Sql and the database sanitizer (task C14a)

Contract A25, R10; design D-NET14, DN9, O5. No parity case: the fixture has
no database kind. `DbSpanSanitizerTests` and `SqlDriverIntegrationTests` are
in `[Collection("EnvVarTests")]` (driver-named test sources, one test swaps
`Console.Out`).

| Area | Tests |
|---|---|
| Literal sanitizer, both dialects (`DbQuerySanitizerTests`) | `String_Literals_Become_Placeholders_In_Both_Dialects` × 3, `Numbers_Become_Placeholders` × 5, `Comments_Are_Dropped_In_Both_Dialects` × 5, `Parameters_Keywords_And_Identifiers_With_Digits_Are_Kept` × 2, `Empty_And_Literal_Free_Text_Is_Unchanged`, `Non_Ascii_Identifiers_Are_Kept`, `Identifier_Adjacent_To_A_Quote_Is_Not_A_Prefix_Unless_Listed` |
| PostgreSQL rules | `PostgreSql_Escape_String_Honors_Backslash` × 2, `PostgreSql_Double_Quoted_Text_Is_An_Identifier_And_Kept`, `PostgreSql_Prefixed_Strings_Become_Placeholders` × 2, `PostgreSql_Unicode_Identifier_Is_Kept`, `PostgreSql_Dollar_Quoted_Strings_Become_Placeholders` × 3, `PostgreSql_Parameters_And_Dollar_In_Identifiers_Are_Kept` × 4, `PostgreSql_Block_Comments_Nest`, `Backtick_Is_Ordinary_Text_In_PostgreSql`, `Underscore_Introducer_Is_MySql_Only`, `PostgreSql_Unterminated_Text_Drops_The_Rest` × 6 |
| MySQL rules | `MySql_Backslash_Escapes_In_Every_Quoted_String`, `MySql_Double_Quoted_Text_Is_A_String`, `MySql_Backtick_Identifier_Is_Kept_Even_With_A_Quote_Inside`, `MySql_Prefixed_Strings_And_Introducers_Become_Placeholders` × 2, `E_Prefix_Is_PostgreSql_Only`, `Dollar_Is_Not_A_Quote_In_MySql`, `MySql_Block_Comments_Do_Not_Nest`, `Hash_Starts_A_Comment_In_MySql_Only`, `MySql_Unterminated_Text_Drops_The_Rest` × 4 |
| Span rules (`DbSpanSanitizerTests`) | `Failed_Db_Span_Keeps_Error_Without_The_Description` × 3, `Other_Sources_Keep_Their_Status_And_Attributes`, `Unset_Or_Ok_Status_Is_Left_Alone`, `Npgsql_Query_Text_Uses_The_PostgreSql_Rules`, `MySqlConnector_Query_Text_Uses_The_MySql_Rules`, `SqlClient_Query_Text_Is_Left_To_Upstream`, `Non_String_Query_Text_Is_Left_Alone`, `Connection_String_Attribute_Is_Removed` × 3, `Npgsql_Data_Source_Password_Keys_Are_Redacted` × 4, `Npgsql_Data_Source_Without_Password_Is_Unchanged` × 2, `Npgsql_Data_Source_That_Does_Not_Parse_Is_Removed`, `Data_Source_Rule_Is_Npgsql_Only` |
| Processor and exporters | `Spans_Without_Recorded_Data_Are_Not_Touched`, `A_Failing_Rule_Strips_The_Sensitive_Attributes_And_Warns_Once`, `A_Failing_Rule_Leaves_An_Unfailed_Status_Alone`, `The_Helper_Console_Exporter_Sees_The_Sanitized_Span`, `Sanitize_Reads_The_Batch_Once_And_Returns_Every_Span_Sanitized`, `Sanitized_Exporter_Behind_A_Batch_Processor_Exports_Every_Span`, `Helper_Otlp_Exporter_Sanitizes_A_Span_Changed_After_It_Ended`, `Helper_Console_Exporter_Sanitizes_A_Span_Changed_After_It_Ended`, `Sanitizer_Runs_When_The_Helper_Exports_No_Traces`, `Rules_Apply_To_A_Driver_Source_The_Application_Subscribed_Without_The_Sql_Package` |
| `AddOtelHelperSql()` | `Sql_Package_Subscribes_The_Driver_Sources`, `Sql_Package_Keeps_Its_Names_Over_A_Later_Configure_And_Adds_No_Duplicate`, `Driver_Sources_Are_Not_Subscribed_While_The_Helper_Exports_No_Traces`, `Sql_Package_Exports_The_Driver_Meters_And_The_SqlClient_Meter`, `Driver_Meters_Are_Dropped_While_The_Helper_Exports_No_Metrics` |
| Real drivers (`SqlDriverIntegrationTests`) | `Npgsql_Spans_Are_Sanitized_And_Metrics_Exported` (Npgsql 10.0.3 on `sql-postgres`: literal, comment, `CAST` error text, `SSL Password` and the password absent from every attribute and the status; `22P02` as `error.type`; `db.system` absent; `Npgsql` `db.client.operation.duration` exported), `MySqlConnector_Spans_Are_Sanitized_And_Metrics_Exported` (MySqlConnector 2.6.2 on `sql-mysql`: single-, double-quoted and comment literals, `Certificate Password` and the password absent; the late status seen by a processor, removed by the helper's OTLP exporter at shutdown; legacy keys only) |

The integration tests need `SQL_TEST_POSTGRES` / `SQL_TEST_MYSQL`
(`postgres://` / `mysql://` URLs, as the Node.js suite); compose
`dotnet-tests` sets them and `SQL_TEST_REQUIRE=1`, so a missing database fails
the run. Without the variables they are skipped. Stop the databases afterwards:
`docker compose -f testing/docker-compose.yml stop sql-postgres sql-mysql`.

Mutation check (2026-11-11, scripted, one mutant per run on a fresh copy, the
three C14a classes as the filter): 45 mutants — 23 of the span rules,
processor, exporters and `AddOtelHelperSql()`, 22 of the sanitizer's lexical
rules — 44 each fail at least one test; the survivor is equivalent (an
unterminated literal that sets the index to the end instead of leaving the
loop). Two first-round survivors were fixed: a batch read twice (test
`Sanitized_Exporter_Behind_A_Batch_Processor_Exports_Every_Span` added) and
`$` inside an identifier (case `SELECT x$tag$y FROM t` added); a dead
key-compaction step in the password-key match was removed.

*Fix round 1 (2026-11-11):* `PostgreSql_Standard_String_With_A_Backslash_Drops_The_Rest`
× 4 (`'C:\'`, `'it\'s secret'`, `N'it\'s secret'`, `'C:\\'`) and
`PostgreSql_Standard_String_Without_A_Backslash_Keeps_The_Rest` replace the
standard-string backslash test; `Legacy_Keys_Follow_The_Opt_In_As_Go` × 11,
`Stable_Only_Drivers_Get_The_Legacy_System_By_Default` × 2,
`MySqlConnector_Legacy_Span_Gets_The_New_Keys_And_Keeps_The_Legacy_By_Default`,
`MySqlConnector_Legacy_Span_Keeps_Only_The_New_Keys_In_The_Stable_Mode`,
`Existing_Query_Text_Is_Not_Replaced_By_The_Statement`,
`Spans_Without_A_System_Key_Get_None`,
`Processor_Reads_The_Opt_In_When_The_Provider_Is_Built`,
`Default_Pipeline_Adds_The_Legacy_System`, `Query_Parameters_Are_Removed` × 2,
`SqlClient_Exception_Events_Can_Be_Turned_Off_After_AddOtelHelperSql`,
`Driver_Meters_Lose_The_Pool_Name_And_Keep_The_Other_Keys`,
`Driver_Meters_Subscribed_By_The_Application_Lose_The_Pool_Name_Too`,
`Disabled_Metric_Patterns_Still_Drop_A_Driver_Instrument`. The integration
tests also assert that no Npgsql or MySqlConnector metric point carries the
pool name, the `SSL Password` / `Certificate Password` value or the password,
and the helper-added `db.system` / `db.system.name` / `db.query.text`. 20 new
mutants (backslash drop in both string branches, opt-in parse — dup, folding,
trimming —, each key addition and removal, the SqlClient legacy value,
parameter removal, the meter views, the helper's drop rules inside the view,
the env read) each fail a test. Writing a tag only when it changes has no
observable effect to test (an equivalent mutant by construction).

Quick run without the databases (fakeotlp only; the two integration tests skip):

```bash
docker compose -f testing/docker-compose.yml up -d --wait fakeotlp-dotnet
docker compose -f testing/docker-compose.yml run --rm --no-deps \
  -e SQL_TEST_POSTGRES= -e SQL_TEST_MYSQL= -e SQL_TEST_REQUIRE=0 dotnet-tests
docker compose -f testing/docker-compose.yml stop fakeotlp-dotnet
```

CI's `dotnet-test` job now has `postgres` and `mysql` services and
`SQL_TEST_REQUIRE: '1'` (fix round 1), so the integration tests run there too.

*Round 2 (2026-11-11, ADR-014: exception detail to a correlated log):*
`Activity_Still_Has_The_Private_Event_List_The_Rewrite_Uses` (canary for
`Activity._events`), `Exception_Event_Keeps_Only_The_Type_And_The_Detail_Goes_To_One_Correlated_Log`
× 3 (SqlClient, Npgsql, MySqlConnector; processor and exporter pass, one
record), `An_Exception_Event_Added_After_The_Span_Ended_Is_Handled_By_The_Helper_Exporter`
(MySqlConnector's late event), `Other_Sources_Keep_Their_Exception_Events`,
`Without_The_Private_Event_List_Nothing_Changes_And_One_Warning_Is_Written`,
`A_Failing_Rule_Still_Removes_The_Exception_Detail`,
`No_Record_When_The_Logger_Is_Off_Or_Absent_And_The_Detail_Still_Leaves_The_Span`,
`Otlp_Log_Record_Carries_The_Span_Context_From_Any_Thread`,
`Correlation_Processor_Leaves_Other_Records_Alone`,
`Stdout_Fallback_Line_Has_The_Detail_And_The_Ids`, and
`On_The_Wire_The_Span_Has_No_Detail_And_The_Log_Has_The_Span_Ids` (an in-test
OTLP/HTTP receiver: `/v1/traces` without the literal or `exception.stacktrace`,
`/v1/logs` with the literal and the span's trace and span id bytes). The
integration tests use `pgcastsecret` (Npgsql `CAST`) and `mydupsecret`
(MySqlConnector duplicate key): the literal is in no span attribute or event,
and one record per failed span has the span's ids, the message and the stack
trace. 16 mutants: 15 fail a test; the survivor (lifting
`SuppressInstrumentation` around the write) was dead code — the helper's
exporters sanitize before `OtlpTraceExporter.Export` enters its suppression
scope — and was removed.

*Round 3 (2026-11-11):* `Two_Exporter_Passes_On_Two_Threads_Log_Once_And_Keep_Every_Event`
(50 barrier rounds), `Ids_Are_Lower_Case_Non_Zero_Hex` × 7,
`Invalid_Ids_Leave_The_Record_Unchanged_And_Do_Not_Throw`,
`Otlp_Record_Timestamp_Is_The_Exception_Event_Time`,
`Late_Event_Record_Reaches_The_Log_Pipeline_Through_The_Helper_Exporter` (pins
that the record is not written under `SuppressInstrumentation`),
`Stdout_Line_Has_The_Current_Span_Ids_After_Logger_And_No_Duplicate`,
`Stdout_Line_Without_A_Span_Has_No_Ids_And_Keeps_An_App_Trace_Id`,
`Stdout_Database_Record_Uses_Its_Span_And_Event_Time_Not_The_Current_Span`,
`MySqlConnector_Legacy_Name_And_Peer_Keys_Get_The_Current_Names`,
`Exception_Logger_Is_Null_When_The_Container_Is_Disposed`; the MySqlConnector
integration test also asserts `db.namespace`, `server.address` and no
`db.user`. Parity: new case `logging.stdout_span_context` (`logging` input
`in_span`), `pass` on .NET.

*Round 4 (2026-11-11):* `A_Blocked_Log_Sink_Stalls_No_Other_Span_And_A_Concurrent_Pass_Logs_Nothing`
replaces the 50-round barrier test: the first pass blocks in the logger (inside
`Handle`, after the lock), and while it is blocked a second pass over the same
span and the end of another failed DB span must finish within 10 s; then one
record for the late span, one for the other, events intact. With the logger
called under the lock (the round-3 code) it times out.
