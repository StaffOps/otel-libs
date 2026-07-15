# Tests — OtelHelper (.NET)

164 unit tests (xUnit), 0 failed, 0 warnings.

```bash
docker run --rm -v "$(pwd):/src" -w /src mcr.microsoft.com/dotnet/sdk:8.0 dotnet test OtelHelper.Tests
```

---

## OptionsTests

Validate defaults and env var resolution.

| Test | Description |
|-------|-----------|
| `Default_ServiceName_Is_MyService` | Default ServiceName is "my-service" |
| `Default_CollectorEndpoint_Is_Empty_When_No_EnvVar` | Default endpoint stays empty (Prometheus fallback) |
| `Default_DebugLevel_Is_False` | Debug disabled by default |
| `ResolveEnvironment_Parses_Correctly` × 6 | LOCAL, DEV, HML, PRD, prd, dev |
| `ResolveEnvironment_Invalid_Falls_Back_To_LOCAL` × 3 | Invalid values → LOCAL |

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

### Extra Instrumentation

| Test | Description |
|-------|-----------|
| `ExtraInstrumentation_Default_Has_SQL` | SQL enabled, AWS disabled by default |
| `ExtraInstrumentation_Multiple_Values` | SQL,AWS enables both |
| `ExtraInstrumentation_Case_Insensitive` | sql,aws works |
| `ExtraInstrumentation_Empty_Disables_All` | Empty string disables all |
| `DebugLevel_Enables_All_Extra_Instrumentation` | Debug mode enables all |

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

### DebugTraceStateProcessor

| Test | Description |
|-------|-----------|
| `DebugMode_Sets_TraceState_And_Attribute_On_Root_Span` | Sets tracestate + attribute debug=true |
| `DebugMode_Does_Not_Set_On_Child_Span` | Does not set on child spans |

### OTEL_HELPER_SAMPLE_RATIO

| Test | Description |
|-------|-----------|
| `SampleRatio_EnvVar_Sets_TraceIdRatioBasedSampler` | 0.5 → TraceIdRatioBased |
| `SampleRatio_Default_Keeps_AlwaysOn` | No env var → AlwaysOn |
| `SampleRatio_Invalid_EnvVar_Keeps_AlwaysOn` | Invalid value → AlwaysOn |

### Double Registration

| Test | Description |
|-------|-----------|
| `Double_AddOtelHelper_Does_Not_Duplicate_Registration` | Second call is no-op |

### PostConfigure

| Test | Description |
|-------|-----------|
| `PostConfigure_Resolves_Environment_From_EnvVar` | ENVIRONMENT=PRD → DeploymentEnvironment.PRD |
| `PostConfigure_Resolves_DisabledSignals_From_EnvVar` | OTEL_HELPER_DISABLED_SIGNALS resolves the list |
| `PostConfigure_Resolves_DisabledMetrics_From_EnvVar` | OTEL_HELPER_DISABLED_METRICS resolves wildcard patterns |
| `PostConfigure_DisabledMetrics_Stays_Empty_When_EnvVar_Not_Set` | Stays empty when unset |

### Validator

| Test | Description |
|-------|-----------|
| `Validator_Fails_On_Empty_ServiceName` | Empty ServiceName fails |
| `Validator_Succeeds_On_Empty_Endpoint_Prometheus_Mode` | Empty endpoint is valid (Prometheus fallback mode) |
| `Validator_Fails_On_Invalid_URI` | Invalid URI fails |
| `Validator_Fails_On_Zero_Timeout` | ExportTimeoutMs ≤ 0 fails |

### IsSignalEnabled

| Test | Description |
|-------|-----------|
| `IsSignalEnabled_Returns_True_When_DisabledSignals_Empty` | All signals enabled when DisabledSignals is empty |
| `IsSignalEnabled_Returns_False_When_Signal_In_DisabledSignals` | Signal in DisabledSignals → disabled |
| `IsSignalEnabled_Is_Case_Insensitive` | Case-insensitive matching |

### Disabled Signals — Full Pipeline

| Test | Description |
|-------|-----------|
| `Pipeline_Registers_When_Metrics_Disabled` | Metrics disabled, pipeline still registers |
| `Pipeline_Registers_With_DisabledMetrics_Pattern` | Wildcard drop pattern accepted |
| `Pipeline_Registers_When_Traces_Disabled` | Traces disabled, pipeline still registers |
| `Pipeline_Registers_When_Logs_Disabled` | Logs disabled, pipeline still registers |
| `Pipeline_Registers_When_All_Signals_Disabled` | All three disabled at once |

### HasInstrumentation("REDIS")

| Test | Description |
|-------|-----------|
| `HasInstrumentation_Redis_Works_Like_SQL_AWS` | REDIS enables like SQL/AWS |
| `HasInstrumentation_Redis_Case_Insensitive` | "redis" (lowercase) works |

### Opt-in Extra Instrumentation — Full Pipeline

| Test | Description |
|-------|-----------|
| `Pipeline_Registers_With_AWS_Instrumentation` | ExtraInstrumentation=SQL,AWS registers cleanly |
| `Pipeline_Registers_With_Redis_Instrumentation` | ExtraInstrumentation=SQL,REDIS registers cleanly |

## TracingAndOptionsHelperTests

Tracing helpers (`StartRootActivity`, `DebugTraceStateProcessor`) and `TelemetryOptions` helper methods, tested directly (not through `AddOtelHelper`).

### StartRootActivity

| Test | Description |
|-------|-----------|
| `StartRootActivity_Creates_Root_Span_Without_Parent` | Clears parent, creates root |
| `StartRootActivity_Generates_New_TraceId_Each_Call` | Each call generates a new traceId |

### DebugTraceStateProcessor

| Test | Description |
|-------|-----------|
| `DebugMode_Sets_TraceState_And_Attribute_On_Root_Span` | Sets tracestate + attribute debug=true |
| `DebugMode_Does_Not_Set_On_Child_Span` | Does not set on child spans |

### IsSignalEnabled

| Test | Description |
|-------|-----------|
| `IsSignalEnabled_Returns_True_When_DisabledSignals_Empty` | All signals enabled when empty |
| `IsSignalEnabled_Returns_False_When_Signal_Disabled` | Disabled signal reports false |
| `IsSignalEnabled_Is_Case_Insensitive` | Case-insensitive matching |

### HasInstrumentation

| Test | Description |
|-------|-----------|
| `HasInstrumentation_Default_Has_SQL` | SQL enabled, AWS/REDIS disabled by default |
| `HasInstrumentation_Multiple_Values` | SQL,AWS,REDIS all enable |
| `HasInstrumentation_Case_Insensitive` | Mixed case works |
| `HasInstrumentation_Empty_Disables_All` | Empty string disables all |
| `DebugLevel_Enables_All_Instrumentation` | Debug mode bypasses the list check entirely |

### GetDefaultLogLevel

| Test | Description |
|-------|-----------|
| `GetDefaultLogLevel_Returns_Correct_Values` | LOCAL=Debug, DEV/HML=Info, PRD=Warning, debug override |

### Validator

| Test | Description |
|-------|-----------|
| `Validator_Fails_On_Empty_ServiceName` | Empty ServiceName fails |
| `Validator_Succeeds_On_Empty_Endpoint_Prometheus_Mode` | Empty endpoint is valid |
| `Validator_Fails_On_Invalid_URI` | Invalid URI fails |
| `Validator_Fails_On_Zero_Timeout` | ExportTimeoutMs ≤ 0 fails |

## SignalIntegrationTests

Integration tests verifying signals are actually collected or dropped via in-memory exporters. Uses `[Collection("EnvVarTests")]` to prevent parallel execution (env var conflicts).

| Test | Description |
|-------|-----------|
| `Meter_Name_Matches_ServiceName` | DI-resolved Meter name equals ServiceName |
| `ActivitySource_Name_Matches_ServiceName` | DI-resolved ActivitySource name equals ServiceName |
| `Custom_Metrics_Via_DI_Meter_Are_Collected` | A counter created via the DI Meter is exported |
| `Traces_Via_DI_ActivitySource_Are_Collected` | A span created via the DI ActivitySource is exported with its tags |
| `ValidateOnStart_Throws_With_Empty_ServiceName` | Invalid ServiceName fails fast inside `AddOtelHelper()` (P9) |
| `ValidateOnStart_Throws_With_Invalid_Endpoint` | Invalid endpoint URI fails fast inside `AddOtelHelper()` (P9) |
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

`TelemetryOptionsPostConfigure`'s TLS scheme resolution — secure by default, `OTEL_EXPORTER_OTLP_INSECURE` override, explicit scheme always wins. Uses `[Collection("EnvVarTests")]`.

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
| `ConsumerSet_Endpoint_Not_Overridden` | Endpoint set in code is not overwritten by PostConfigure |
| `Insecure_EnvVar_Ignored_When_Scheme_Explicit_Https` | Explicit `https://` scheme wins over INSECURE=true |
| `HttpsScheme_DefaultPort_Gets_4317` | `https://gw` (no port) normalizes to :4317 |
| `ResolveEndpoint_Unit_Schemeless_Host_Port` | `ResolveEndpoint` static helper: schemeless host:port |
| `ResolveEndpoint_Unit_Schemeless_Host_Only` | `ResolveEndpoint` static helper: schemeless host only |
| `ResolveEndpoint_Unit_Http_Explicit` | `ResolveEndpoint` static helper: explicit http:// |

## ProfilingTests

`OtelHelper.Profiling`'s `ProfilingPrerequisites` and extension registration. Uses `[Collection("EnvVarTests")]`.

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
| `UnknownValue_FailsValidation` | Unknown value → validation error listing valid values |
| `OtlpWithoutEndpoint_FailsValidation` | `otlp` without an endpoint → validation error |
| `NoneCombined_FailsValidation` | `none,prometheus` → validation error |

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

## SamplerPrecedenceTests

`OTEL_TRACES_SAMPLER` (standard SDK var) precedence over `OTEL_HELPER_SAMPLE_RATIO` (US-2). Verified against the real `Sdk.CreateTracerProviderBuilder()` behavior (empirically confirmed, not assumed — see `ANALISE-PROBLEMAS.md` P9). Uses `[Collection("EnvVarTests")]`.

| Test | Description |
|-------|-----------|
| `HelperVarOnly_AppliesRatio` | Only `OTEL_HELPER_SAMPLE_RATIO` set → ratio applied, `TraceIdRatioBasedSampler` |
| `StandardVarOnly_Wins` | Only `OTEL_TRACES_SAMPLER=always_off` set → span not sampled |
| `StandardVarBeatsHelperVar` | Both set → standard var wins, helper ratio ignored |
| `ExplicitRatioBeatsStandardVar` | Explicit `Sampler` in code wins over the standard env var |
| `NoEnvNoOverride_DefaultsToAlwaysOn` | Neither set → `AlwaysOnSampler`, span sampled |
