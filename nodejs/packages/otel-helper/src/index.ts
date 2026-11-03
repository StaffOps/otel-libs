/**
 * `@staffops/otel-helper` — OpenTelemetry helper for Node.js at parity with
 * the Go and Python helpers (specs/nodejs-otel-helper). Implemented so far:
 * `setup()` identity/resource/log-level resolution and diagnostics (task B3),
 * OTLP endpoint/protocol/TLS-setting resolution (B4a), OTLP exporters for
 * traces, metrics and logs (B4b), exporter lists with the console exporters,
 * `OTEL_SDK_DISABLED` and the metric export interval (B4c), the sampler
 * (ParentBased, health sampler around it), propagators and the debug marker
 * (B5), the Prometheus `/metrics` listener and `metricsHandler()` (B7), the
 * http/undici/express/gRPC instrumentation (B6), `createLogger()` with the
 * stdout fallback (B8), the lifecycle (B9: rollback, foreign globals and
 * `replaceGlobals`, forwarding providers across re-setups, the register entry),
 * `getTracer`, `getMeter` and `startRootSpan` (B11b).
 */
export { VERSION } from './version';
export { getMeter, getTracer, startRootSpan } from './internal/api';
export type { RootSpanOptions, RootSpanResult } from './internal/api';
export { metricsHandler, setup, shutdown } from './internal/setup';
export type { ScrapeRequest, ScrapeResponse } from './internal/metrics-endpoint';
export { createLogger } from './internal/logging';
export type { LogAttributes, Logger, LoggerOptions } from './internal/logging';
export { defaultLogLevel } from './internal/log-level';
export type { DeploymentEnvironment, LogLevel } from './internal/log-level';
export type { Resolved, SetupOptions } from './internal/resolve';
export type { OtlpEndpoint, OtlpPlan, OtlpProtocol, Signal } from './internal/otlp';
export type { OtlpCompression } from './internal/otlp-settings';
export type { MetricExporterName, SpanLogExporterName } from './internal/signals';
export type { PropagatorName, SamplerConfig, SamplerName } from './internal/tracing';
