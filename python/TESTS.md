# Tests — otel-helper (Python)

133 unit tests (pytest), `--ignore=tests/test_propagation.py`. Coverage: ≥90% line (CI gate).

```bash
docker run --rm -v $(pwd):/app -w /app python:3.11-slim sh -c "pip install -e '.[dev]' grpcio -q && pytest tests/ --ignore=tests/test_propagation.py -v"
```

---

## test_config.py (47 tests)

### TestDefaults (5 tests)

| Test | Description |
|------|-------------|
| `test_default_service_name` | Default ServiceName is "my-service" |
| `test_default_environment` | Default Environment is LOCAL |
| `test_default_debug_level` | Debug disabled by default |
| `test_default_extra_instrumentation` | Default extra instrumentation is "SQL" |
| `test_default_export_timeout` | Default export timeout is 10000ms |

### TestEnvResolution (14 tests)

| Test | Description |
|------|-------------|
| `test_service_name_from_env` | SERVICE_NAME resolves service_name |
| `test_otel_service_name_fallback` | OTEL_SERVICE_NAME as fallback |
| `test_service_name_priority` | SERVICE_NAME has priority over OTEL_SERVICE_NAME |
| `test_environment_from_env` | ENVIRONMENT=PRD resolves correctly |
| `test_environment_invalid_falls_back_to_local` | Invalid value → LOCAL |
| `test_collector_endpoint_from_env` | OTEL_EXPORTER_OTLP_ENDPOINT resolves endpoint |
| `test_collector_endpoint_default_empty` | No env var → endpoint stays empty (Prometheus fallback) |
| `test_collector_endpoint_preserves_custom_port` | `https://gw:14317` keeps port 14317 (P3) |
| `test_collector_endpoint_defaults_port_when_absent` | `https://gw` (no port) → defaults to :4317 |
| `test_collector_endpoint_schemeless_preserves_port` | `gw:14317` (no scheme) keeps the port |
| `test_collector_endpoint_strips_path` | Scheme+host+port kept; path suffix dropped |
| `test_debug_level_from_env` | OTEL_HELPER_DEBUG_LEVEL=true resolves |
| `test_extra_instrumentation_from_env` | OTEL_HELPER_EXTRA_INSTRUMENTATION resolves |
| `test_explicit_value_overrides_env` | Explicit value has priority over env var |

### TestValidation (5 tests)

| Test | Description |
|------|-------------|
| `test_valid_options` | Valid options pass |
| `test_empty_service_name_fails` | Empty ServiceName → ValueError |
| `test_empty_endpoint_is_valid` | Empty endpoint is valid (Prometheus fallback mode, not an error) |
| `test_invalid_endpoint_fails` | Invalid URI → ValueError |
| `test_zero_timeout_fails` | Timeout ≤ 0 → ValueError |

### TestHasInstrumentation (4 tests)

| Test | Description |
|------|-------------|
| `test_sql_enabled_by_default` | SQL enabled by default |
| `test_aws_not_enabled_by_default` | AWS disabled by default |
| `test_debug_enables_all` | Debug mode enables all |
| `test_case_insensitive` | Case insensitive (sql = SQL) |

### TestLogLevel (5 tests)

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

### TestResolveInsecure (11 tests)

TLS auto-detection via `resolve_insecure()`.

| Test | Description |
|------|-------------|
| `test_https_endpoint_is_secure` | `https://` endpoint → secure (insecure=False) |
| `test_http_endpoint_is_insecure` | `http://` endpoint → insecure=True |
| `test_empty_endpoint_is_insecure` | No endpoint → insecure=True (irrelevant, Prometheus mode) |
| `test_env_override_false_with_http_endpoint` | OTEL_EXPORTER_OTLP_INSECURE=false wins over http:// scheme |
| `test_env_override_true_with_https_endpoint` | OTEL_EXPORTER_OTLP_INSECURE=true wins over https:// scheme |
| `test_env_override_case_insensitive` | "False" (mixed case) parsed correctly |
| `test_env_override_with_whitespace` | "  true  " (whitespace) parsed correctly |
| `test_explicit_insecure_false_overrides_scheme` | Explicit `insecure=False` wins over http:// scheme |
| `test_explicit_insecure_true_overrides_scheme` | Explicit `insecure=True` wins over https:// scheme |
| `test_explicit_overrides_env` | Explicit value wins over env var |
| `test_default_insecure_is_none` | `insecure` field defaults to None (unset) |

---

## test_setup.py (9 tests)

### TestSetupTelemetry

| Test | Description |
|------|-------------|
| `test_returns_resolved_options` | Returns resolved options |
| `test_double_init_guard` | Second call is no-op |
| `test_sets_tracer_provider` | Global TracerProvider configured |
| `test_resource_emits_deployment_environment_name` | Resource carries `deployment.environment.name` (semconv ≥ v1.27), not the legacy key (P8) |
| `test_sets_meter_provider` | Global MeterProvider configured |
| `test_env_var_resolution` | Env vars resolved in setup |
| `test_validation_fails_on_bad_config` | Invalid config → ValueError |
| `test_resource_attributes` | Resource attributes accepted |
| `test_debug_mode` | Debug mode activates correctly |

---

## test_features.py (28 tests)

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

### TestDisabledSignals (8 tests)

| Test | Description |
|------|-------------|
| `test_is_signal_enabled_true_when_empty` | All signals enabled when disabled_signals is empty |
| `test_is_signal_enabled_false_when_in_list` | Signal in disabled_signals → disabled |
| `test_is_signal_enabled_case_insensitive` | Case-insensitive matching ("Metrics", "TRACES") |
| `test_env_var_resolves_disabled_signals` | OTEL_HELPER_DISABLED_SIGNALS resolves the list |
| `test_env_var_resolves_disabled_metrics` | OTEL_HELPER_DISABLED_METRICS resolves wildcard patterns |
| `test_disabled_metrics_empty_when_env_not_set` | Empty list when env var unset |
| `test_setup_with_disabled_signals_does_not_raise` | Full setup with a disabled signal doesn't raise |
| `test_disabled_metrics_pattern_does_not_raise` | Full setup with a metric-drop pattern doesn't raise |

---

## test_ext.py (7 tests)

Opt-in instrumentation extensions (`otel_helper.ext`) — the optional packages are NOT installed in the test env, so the installed-package path is exercised via fake modules injected into `sys.modules`.

### TestExtraNotInstalled (3 tests)

| Test | Description |
|------|-------------|
| `test_aws_raises_actionable_import_error` | Missing `[aws]` extra → ImportError naming `pip install otel-helper[aws]` |
| `test_redis_raises_actionable_import_error` | Missing `[redis]` extra → actionable ImportError |
| `test_sql_raises_actionable_import_error` | Missing `[sql]` extra → actionable ImportError |

### TestExtraInstalled (4 tests)

| Test | Description |
|------|-------------|
| `test_aws_instruments` | BotocoreInstrumentor.instrument() called when the module is present |
| `test_redis_instruments` | RedisInstrumentor.instrument() called when the module is present |
| `test_sql_instruments_globally` | SQLAlchemyInstrumentor.instrument() with no engine (global) |
| `test_sql_instruments_specific_engine` | SQLAlchemyInstrumentor.instrument(engine=...) with an explicit engine |

---

## test_metrics_contract.py (23 tests)

`OTEL_METRICS_EXPORTER` / `OTEL_METRIC_EXPORT_INTERVAL` contract (US-1, US-2, US-6 of the metrics-exporter-contract spec). Cross-language parity with `go/metrics_contract_test.go` and `dotnet/OtelHelper.Tests/MetricsContractTests.cs`.

### TestExporterResolution (11 tests)

| Test | Description |
|------|-------------|
| `test_unset_with_endpoint_is_otlp` | Unset + endpoint set → `["otlp"]` (legacy inference) |
| `test_unset_without_endpoint_is_prometheus` | Unset + no endpoint → `["prometheus"]` |
| `test_otlp_only` | `OTEL_METRICS_EXPORTER=otlp` → OTLP only |
| `test_prometheus_only_even_with_endpoint` | `prometheus` value wins even with an endpoint set |
| `test_dual_mode` | `otlp,prometheus` → both active |
| `test_none_disables_metrics` | `none` → empty list (metrics disabled) |
| `test_case_and_whitespace_tolerant` | " OTLP , Prometheus " parses correctly |
| `test_explicit_option_beats_env` | `metric_exporters=[...]` option wins over the env var |
| `test_unknown_value_fails_validation` | Unknown value → ValueError listing valid values |
| `test_otlp_without_endpoint_fails_validation` | `otlp` without an endpoint → ValueError |
| `test_none_combined_with_other_fails_validation` | `none,prometheus` → ValueError |

### TestExportInterval (5 tests)

| Test | Description |
|------|-------------|
| `test_default_is_30s` | Default export_interval_ms is 30000 |
| `test_env_var_honored` | OTEL_METRIC_EXPORT_INTERVAL resolves the interval |
| `test_explicit_beats_env` | Explicit `export_interval_ms` wins over the env var |
| `test_invalid_env_falls_back_to_default` | Non-numeric env value → falls back to 30000 |
| `test_non_positive_fails_validation` | `export_interval_ms=0` → ValueError |

### TestDualModePipeline (4 tests)

| Test | Description |
|------|-------------|
| `test_dual_mode_has_both_readers` | `otlp,prometheus` → single `MeterProvider`, 2 readers; same counter visible via both (no double counting) |
| `test_prometheus_only_with_endpoint_has_no_otlp_reader` | `prometheus` mode with an endpoint set → only 1 reader (no OTLP) |
| `test_otlp_reader_uses_resolved_interval` | OTLP reader honors the resolved `export_interval_ms` |
| `test_none_skips_metrics_pipeline` | `none` → `resolved_metric_exporters()` is empty after `setup_telemetry()` |

### TestMountableHandler (1 test)

| Test | Description |
|------|-------------|
| `test_metrics_app_serves_prometheus_text` | `metrics_app()` ASGI app serves Prometheus text format with the recorded counter |

### TestListenerRobustness (2 tests)

| Test | Description |
|------|-------------|
| `test_port_zero_suppresses_listener` | `prometheus_metrics_port=0` → no listener bound, no error |
| `test_busy_port_raises_actionable_error` | Port already in use → RuntimeError naming `metrics_app()` as the fix |

---

## test_sampler_precedence.py (4 tests)

`OTEL_TRACES_SAMPLER` (standard SDK var) precedence over `OTEL_HELPER_SAMPLE_RATIO` (US-2).

### TestSamplerPrecedence

| Test | Description |
|------|-------------|
| `test_helper_var_only_applies_ratio` | Only `OTEL_HELPER_SAMPLE_RATIO` set → ratio applied, `TraceIdRatioBased` sampler |
| `test_standard_var_only_wins` | Only `OTEL_TRACES_SAMPLER=always_off` set → SDK's own env handling wins |
| `test_standard_var_beats_helper_var` | Both set → standard var wins, helper ratio ignored entirely |
| `test_explicit_ratio_beats_standard_var` | Explicit `sample_ratio` in code wins over the standard env var |

---

## test_signal_integration.py (18 tests)

Integration tests using `InMemorySpanExporter` to verify spans are actually created, exported, and configured correctly (no network I/O).

### TestGetTracerCreatesExportedSpans (2 tests)

| Test | Description |
|------|-------------|
| `test_span_is_exported` | A span created via the configured tracer is exported |
| `test_multiple_spans_exported` | Parent + child spans both exported |

### TestGetTracerUsesConfiguredResource (2 tests)

| Test | Description |
|------|-------------|
| `test_resource_has_service_name` | Exported span's resource carries `service.name` |
| `test_custom_resource_attributes_propagated` | Custom resource attributes propagate to the exported span |

### TestDebugProcessorSetsAttributeOnRootSpan (2 tests)

| Test | Description |
|------|-------------|
| `test_root_span_has_debug_attribute` | DebugProcessor sets `debug=true` on a root span |
| `test_debug_processor_added_when_debug_level_enabled` | `debug_level=True` in options wires up the processor end-to-end |

### TestDebugProcessorDoesNotSetOnChild (2 tests)

| Test | Description |
|------|-------------|
| `test_child_span_no_debug_attribute` | Child span does not get `debug=true` |
| `test_deeply_nested_child_no_debug` | Only the true root gets the attribute in a 3-level span tree |

### TestSampleRatioBelowOneUsesTraceIdSampler (3 tests)

| Test | Description |
|------|-------------|
| `test_ratio_0_5_uses_trace_id_ratio` | `sample_ratio=0.5` → `TraceIdRatioBased` sampler |
| `test_ratio_0_01_uses_trace_id_ratio` | `sample_ratio=0.01` → `TraceIdRatioBased` sampler |
| `test_ratio_1_uses_always_on` | `sample_ratio=1.0` → `ALWAYS_ON` sampler |

### TestStartRootSpanCreatesIndependentTrace (4 tests)

| Test | Description |
|------|-------------|
| `test_root_span_has_no_parent` | `start_root_span` detaches from the current parent |
| `test_root_span_has_different_trace_id` | New root span gets a different trace ID from the outer one |
| `test_root_span_attributes_exported` | Attributes set on the root span are exported |
| `test_context_restored_after_root_span` | Original context is restored after the `with` block exits |
