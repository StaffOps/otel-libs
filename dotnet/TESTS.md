# Tests — OtelHelper (.NET)

Unit tests (xUnit).

```bash
docker run --rm -v "$(pwd):/src" -w /src mcr.microsoft.com/dotnet/sdk:8.0 dotnet test OtelHelper.Tests
```

---

## OptionsTests

Validate defaults and env var resolution.

| Test | Description |
|-------|-----------|
| `Default_ServiceName_Is_MyService` | Default ServiceName is "my-service" |
| `Default_CollectorEndpoint_Has_Port_4317` | Default endpoint ends with :4317 |
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
| `Default_Endpoint_Uses_Localhost` | Default contains localhost |
| `Endpoint_Extracts_Host_And_Appends_Port` | Extracts host and adds :4317 |
| `Endpoint_With_Trailing_Slash_Is_Cleaned` | Removes trailing slash |

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

### Validator

| Test | Description |
|-------|-----------|
| `Validator_Fails_On_Empty_ServiceName` | Empty ServiceName fails |
| `Validator_Fails_On_Empty_Endpoint` | Empty endpoint fails |
| `Validator_Fails_On_Invalid_URI` | Invalid URI fails |
| `Validator_Fails_On_Zero_Timeout` | ExportTimeoutMs ≤ 0 fails |
