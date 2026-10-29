/**
 * `@staffops/otel-helper` — OpenTelemetry helper for Node.js at parity with
 * the Go and Python helpers (specs/nodejs-otel-helper). Implemented so far:
 * `setup()` identity/resource/log-level resolution and diagnostics (task B3);
 * exporters, sampler, instrumentation and logging land in B4–B9.
 */
export { VERSION } from './version';
export { setup, shutdown } from './internal/setup';
export { defaultLogLevel } from './internal/log-level';
export type { DeploymentEnvironment, LogLevel } from './internal/log-level';
export type { Resolved, SetupOptions } from './internal/resolve';
