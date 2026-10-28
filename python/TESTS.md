# Tests — otel-helper (Python)

Unit tests (pytest) plus the shared parity fixture (`tests/test_parity.py`,
see [`parity/README.md`](../parity/README.md)). Coverage: ≥90% line (CI gate),
including the parity driver subprocesses (`PARITY_COVERAGE_DATA`).

```bash
# from the repo root — includes the fake OTLP receiver the parity export cases need
docker compose -f testing/docker-compose.yml run --rm python-tests
```

> Full per-test inventory below predates the 0.3.0 harness; refreshed in task A13.

---

## test_config.py (0.3.0 resolution contract)

Pure resolution, no exporters: service-name precedence (R5), environment
precedence / verbatim / warn-once (R4), log level per environment, env values
that must warn and fall back instead of raising (R8), `OTEL_{TRACES,LOGS}_EXPORTER`
(`none`, `console`, `logging` alias, `none` in a list), per-signal "OTLP
configured" (R2), protocol precedence incl. per-signal and port-4318 inference
on the signal's own endpoint, code-value validation (raises naming the field),
URL redaction, `has_instrumentation`.

## test_shims.py (0.3.0 helper-owned OTLP shims)

What the helper passes to the SDK exporters (design D1, ADR-001): nothing for
absolute env URLs; code generic endpoint + `/v1/<signal>` vs per-signal verbatim;
schemeless endpoints (D1 table), portless gRPC → 4317 + warning (D1.b), TLS
conflicts (D1.c), certificate with plaintext, gRPC userinfo/query stripping,
timeout ms → s incl. the "looks like seconds" warning and invalid values (D1.e),
gzip default and env `none` for both protocols (D1.d), stable HTTP semconv default
and its warnings (D12), `OTEL_PROPAGATORS`, `service.instance.id` (D4) and a source
scan enforcing the helper-diagnostics channel (`otel_helper*` loggers only).

## test_setup.py (9 tests)

### TestSetupTelemetry

| Test | Description |
|------|-------------|
| `test_returns_resolved_options` | Returns resolved options |
| `test_double_init_guard` | Second call is a no-op returning the first resolved options |
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
| `test_ratio_below_1_uses_trace_id_sampler` | Ratio < 1.0 → `ParentBased(TraceIdRatioBased)` |
| `test_ratio_1_uses_always_on` | Ratio 1.0 → `ParentBased(AlwaysOn)` |

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

### TestExporterResolution (18 tests)

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
| `test_unknown_env_value_warns_and_uses_inference` | Unknown env value → warning, legacy inference (R8) |
| `test_unknown_code_value_fails_validation` | Unknown code value → ValueError listing valid values |
| `test_otlp_env_without_endpoint_warns_and_falls_back` | Env `otlp` without a metrics endpoint → warning, `["prometheus"]` (R2) |
| `test_otlp_env_without_endpoint_keeps_other_exporters` | Env `otlp,console` without endpoint → `["console"]` |
| `test_otlp_code_without_endpoint_fails_validation` | Code `["otlp"]` without an endpoint → ValueError |
| `test_otlp_per_signal_metrics_endpoint_counts_as_configured` | `OTEL_EXPORTER_OTLP_METRICS_ENDPOINT` satisfies `otlp` |
| `test_none_combined_env_warns_and_is_none` | Env `none,prometheus` → warning, `[]` |
| `test_none_combined_code_fails_validation` | Code `["none", "prometheus"]` → ValueError |
| `test_logging_alias_is_console` | Env `logging` → `["console"]` + deprecation warning |

### TestExportInterval (7 tests)

| Test | Description |
|------|-------------|
| `test_default_is_30s` | Default export_interval_ms is 30000 |
| `test_env_var_honored` | OTEL_METRIC_EXPORT_INTERVAL resolves the interval |
| `test_explicit_beats_env` | Explicit `export_interval_ms` wins over the env var |
| `test_invalid_env_falls_back_to_default` | Non-numeric env value → warning, 30000 |
| `test_non_positive_env_warns_and_falls_back` | Env `0` / `-5` → warning, 30000 (R8) |
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

## test_otlp_protocol.py (8 tests)

OTLP wire protocol selection (grpc vs http/protobuf) — tracing, metrics, logging. Verifies exporter selection via mocking the exporter constructors; the underlying SDK path-append behavior was confirmed empirically against the real installed SDK before writing this file. Cross-language parity with `go/otlp_protocol_test.go` and `dotnet/OtelHelper.Tests/OtlpProtocolTests.cs`.

### TestTracingProtocolSelection (4 tests)

| Test | Description |
|------|-------------|
| `test_grpc_endpoint_uses_grpc_exporter` | Port 4317 → grpc exporter, http exporter not called |
| `test_http_endpoint_uses_http_exporter_with_path_appended` | Port 4318 → http exporter, `/v1/traces` appended, no `insecure` kwarg |
| `test_http_endpoint_with_trailing_slash_no_double_path` | Trailing slash on the endpoint doesn't produce `//v1/traces` |
| `test_explicit_protocol_overrides_port_inference` | `otlp_protocol="http/protobuf"` overrides a :4317 endpoint that would infer grpc |

### TestMetricsProtocolSelection (2 tests)

| Test | Description |
|------|-------------|
| `test_grpc_endpoint_uses_grpc_exporter` | Port 4317 → grpc exporter |
| `test_http_endpoint_uses_http_exporter_with_path_appended` | Port 4318 → http exporter, `/v1/metrics` appended |

### TestLoggingProtocolSelection (2 tests)

| Test | Description |
|------|-------------|
| `test_grpc_endpoint_uses_grpc_exporter` | Port 4317 → grpc exporter |
| `test_http_endpoint_uses_http_exporter_with_path_appended` | Port 4318 → http exporter, `/v1/logs` appended |

---

## test_sampler_precedence.py (4 tests)

`OTEL_TRACES_SAMPLER` (standard SDK var) precedence over `OTEL_HELPER_SAMPLE_RATIO` (US-2).

### TestSamplerPrecedence

| Test | Description |
|------|-------------|
| `test_helper_var_only_applies_parentbased_ratio` | Only `OTEL_HELPER_SAMPLE_RATIO` set → `ParentBased(TraceIdRatioBased)` |
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
| `test_ratio_0_5_uses_trace_id_ratio` | `sample_ratio=0.5` → `ParentBased(TraceIdRatioBased)` |
| `test_ratio_0_01_uses_trace_id_ratio` | `sample_ratio=0.01` → `ParentBased(TraceIdRatioBased)` |
| `test_ratio_1_uses_always_on` | `sample_ratio=1.0` → `ParentBased(AlwaysOn)` |

### TestStartRootSpanCreatesIndependentTrace (4 tests)

| Test | Description |
|------|-------------|
| `test_root_span_has_no_parent` | `start_root_span` detaches from the current parent |
| `test_root_span_has_different_trace_id` | New root span gets a different trace ID from the outer one |
| `test_root_span_attributes_exported` | Attributes set on the root span are exported |
| `test_context_restored_after_root_span` | Original context is restored after the `with` block exits |
