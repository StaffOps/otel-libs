# Tests — otel-helper (Python)

63 unit tests (pytest). Coverage: 94% line.

```bash
docker run --rm -v $(pwd):/app -w /app python:3.11-slim sh -c "pip install -e '.[dev]' grpcio -q && pytest tests/ --ignore=tests/test_propagation.py -v"
```

---

## test_config.py (35 tests)

### TestDefaults (5 tests)

| Test | Description |
|------|-------------|
| `test_default_service_name` | Default ServiceName is "my-service" |
| `test_default_environment` | Default Environment is LOCAL |
| `test_default_debug_level` | Debug disabled by default |
| `test_default_extra_instrumentation` | Default extra instrumentation is "SQL" |
| `test_default_export_timeout` | Default export timeout is 10000ms |

### TestEnvResolution (11 tests)

| Test | Description |
|------|-------------|
| `test_service_name_from_env` | SERVICE_NAME resolves service_name |
| `test_otel_service_name_fallback` | OTEL_SERVICE_NAME as fallback |
| `test_service_name_priority` | SERVICE_NAME has priority over OTEL_SERVICE_NAME |
| `test_environment_from_env` | ENVIRONMENT=PRD resolves correctly |
| `test_environment_invalid_falls_back_to_local` | Invalid value → LOCAL |
| `test_collector_endpoint_from_env` | OTEL_EXPORTER_OTLP_ENDPOINT resolves endpoint |
| `test_collector_endpoint_default` | Default uses localhost:4317 |
| `test_debug_level_from_env` | OTEL_HELPER_DEBUG_LEVEL=true resolves |
| `test_extra_instrumentation_from_env` | OTEL_HELPER_EXTRA_INSTRUMENTATION resolves |
| `test_explicit_value_overrides_env` | Explicit value has priority over env var |

### TestValidation (5 tests)

| Test | Description |
|------|-------------|
| `test_valid_options` | Valid options pass |
| `test_empty_service_name_fails` | Empty ServiceName → ValueError |
| `test_empty_endpoint_fails` | Empty endpoint → ValueError |
| `test_invalid_endpoint_fails` | Invalid URI → ValueError |
| `test_zero_timeout_fails` | Timeout ≤ 0 → ValueError |

### TestHasInstrumentation (4 tests)

| Test | Description |
|------|-------------|
| `test_sql_enabled_by_default` | SQL enabled by default |
| `test_aws_not_enabled_by_default` | AWS disabled by default |
| `test_debug_enables_all` | Debug mode enables all |
| `test_case_insensitive` | Case insensitive (sql = SQL) |

### TestLogLevel (6 tests)

| Test | Description |
|------|-------------|
| `test_local_debug` | LOCAL → DEBUG |
| `test_dev_info` | DEV → INFO |
| `test_hml_info` | HML → INFO |
| `test_prd_warning` | PRD → WARNING |
| `test_debug_override` | Debug mode → DEBUG in any environment |

### TestEnvironmentParsing (3 tests)

| Test | Description |
|------|-------------|
| `test_valid_values` | LOCAL, DEV, HML, PRD parse correctly |
| `test_case_insensitive` | prd, dev work |
| `test_invalid_falls_back` | Invalid value → LOCAL |

---

## test_setup.py (8 tests)

### TestSetupTelemetry

| Test | Description |
|------|-------------|
| `test_returns_resolved_options` | Returns resolved options |
| `test_double_init_guard` | Second call is no-op |
| `test_sets_tracer_provider` | Global TracerProvider configured |
| `test_sets_meter_provider` | Global MeterProvider configured |
| `test_env_var_resolution` | Env vars resolved in setup |
| `test_validation_fails_on_bad_config` | Invalid config → ValueError |
| `test_resource_attributes` | Resource attributes accepted |
| `test_debug_mode` | Debug mode activates correctly |

---

## test_features.py (20 tests)

### TestSampleRatio (8 tests)

| Test | Description |
|------|-------------|
| `test_default_is_always_on` | Ratio 1.0 by default |
| `test_env_var_sets_ratio` | OTEL_HELPER_SAMPLE_RATIO=0.5 works |
| `test_env_var_clamped_to_0_1` | Value > 1.0 clamped to 1.0 |
| `test_env_var_negative_clamped` | Value < 0.0 clamped to 0.0 |
| `test_invalid_env_var_ignored` | Non-numeric value ignored |
| `test_explicit_value_overrides_env` | Explicit value has priority |
| `test_ratio_below_1_uses_trace_id_sampler` | Ratio < 1.0 → TraceIdRatioBased |
| `test_ratio_1_uses_always_on` | Ratio 1.0 → not TraceIdRatioBased |

### TestDebugProcessor (2 tests)

| Test | Description |
|------|-------------|
| `test_sets_debug_attribute_on_root_span` | Sets debug=true on root spans |
| `test_does_not_set_on_child_span` | Does not set on child spans |

### TestGrpcAutoInstrumentation (2 tests)

| Test | Description |
|------|-------------|
| `test_grpc_aio_client_instrumentor_patches` | Monkey-patch grpc.aio.insecure_channel |
| `test_grpc_aio_server_instrumentor_patches` | Monkey-patch grpc.aio.server |

### TestHelpers (4 tests)

| Test | Description |
|------|-------------|
| `test_get_tracer` | get_tracer() returns Tracer |
| `test_get_meter` | get_meter() returns Meter |
| `test_get_tracer_default_name` | get_tracer() without args works |
| `test_get_meter_default_name` | get_meter() without args works |

### TestStartRootSpan (2 tests)

| Test | Description |
|------|-------------|
| `test_creates_independent_trace` | Creates trace without parent (independent) |
| `test_yields_span` | Returns recording span |

### TestDebugProcessorLifecycle (2 tests)

| Test | Description |
|------|-------------|
| `test_shutdown` | shutdown() does not fail |
| `test_force_flush` | force_flush() returns True |
