/**
 * `@staffops/otel-helper` — OpenTelemetry for Node.js services with the same
 * environment-variable contract as the Go and Python helpers.
 *
 * - {@link setup} reads the standard `OTEL_*` variables (and the helper's
 *   own), builds the trace, metric and log pipelines (OTLP to the Collector;
 *   a Prometheus `/metrics` endpoint when no endpoint is set) and installs
 *   the http, undici, express and gRPC instrumentations; {@link shutdown}
 *   flushes and stops them. Both are safe to call again.
 * - {@link getTracer}, {@link getMeter} and {@link startRootSpan} for custom
 *   spans and metrics; {@link createLogger} for logs correlated with the
 *   active span; {@link metricsHandler} to serve `/metrics` from your own server.
 * - `@staffops/otel-helper/register` sets up from the environment before the
 *   app loads (`node --import @staffops/otel-helper/register app.js`);
 *   `@staffops/otel-helper/hooks` installs only the module hooks, for a
 *   custom preload that calls `setup()` itself.
 *
 * Start with the README (configuration, environment variables, API
 * stability) and HOW-TO (recipes) in `nodejs/` of the repository:
 * https://github.com/StaffOps/otel-libs/blob/main/nodejs/README.md and
 * https://github.com/StaffOps/otel-libs/blob/main/nodejs/HOW-TO.md.
 *
 * @packageDocumentation
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
