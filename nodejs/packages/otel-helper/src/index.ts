/**
 * `@staffops/otel-helper` — OpenTelemetry helper for Node.js at parity with
 * the Go and Python helpers (specs/nodejs-otel-helper). Implemented so far:
 * `setup()` identity/resource/log-level resolution and diagnostics (task B3),
 * OTLP endpoint/protocol/TLS-setting resolution (B4a), OTLP exporters for
 * traces, metrics and logs (B4b); exporter lists, sampler, instrumentation and
 * logging land in B4c–B9.
 */
export { VERSION } from './version';
export { setup, shutdown } from './internal/setup';
export { defaultLogLevel } from './internal/log-level';
export type { DeploymentEnvironment, LogLevel } from './internal/log-level';
export type { Resolved, SetupOptions } from './internal/resolve';
export type { OtlpEndpoint, OtlpPlan, OtlpProtocol, Signal } from './internal/otlp';
export type { OtlpCompression } from './internal/otlp-settings';
