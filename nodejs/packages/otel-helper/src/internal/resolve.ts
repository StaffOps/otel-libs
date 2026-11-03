/**
 * The resolver (design D-JS3): code options and the environment become one
 * frozen {@link Resolved} plus the resource attributes. Explicit code > standard
 * `OTEL_*` env > legacy / `OTEL_HELPER_*` env > default (contract D1). Invalid
 * code values throw before anything is built; environment problems only warn
 * (R8). This module covers the identity rows (task B3), the OTLP endpoint,
 * protocol and TLS-setting rows (task B4a, `otlp.ts`), the OTLP headers,
 * timeout and compression rows and the batch/limit settings (task B4b,
 * `otlp-settings.ts`, `sdk-settings.ts`), the exporter lists,
 * `OTEL_SDK_DISABLED` and the metric export interval (task B4c,
 * `signals.ts`), the sampler, propagators and health paths (task B5,
 * `tracing.ts`), the `/metrics` port (task B7, `metrics-endpoint.ts`), the
 * opt-in `instrumentations` option and the `OTEL_SEMCONV_STABILITY_OPT_IN` HTTP
 * warning (task B10a, design D-JS9); later tasks add theirs.
 */
import cluster from 'node:cluster';
import { randomUUID } from 'node:crypto';
import { report, reportOnce, type DiagnosticValue } from './diagnostics';
import { asciiLower, envBool, envValue, trimWS, typeName, type Warn } from './env';
import { checkCodeOtlp, resolveOtlp, type OtlpHeaders, type OtlpPlan, type Reporter } from './otlp';
import type { Signal } from './otlp-settings';
import { resolveSdkSettings, type SdkSettings } from './sdk-settings';
import { checkCodeSignals, resolveSignals, type MetricExporterName, type SpanLogExporterName } from './signals';
import { checkCodeDisabledMetrics, checkCodeMetricsPort, resolveDisabledMetrics, resolveMetricsPort } from './metrics-endpoint';
import { defaultLogLevel, knownEnvironment, LOG_LEVELS, type LogLevel } from './log-level';
import { parseResourceAttributes } from './resource-attributes';
import { checkCodeTracing, DEFAULT_HEALTH_PATHS, DEFAULT_SAMPLER, ENV_SAMPLE_RATIO, resolvePropagators, resolveSampler, type PropagatorName, type SamplerConfig } from './tracing';
import type { Instrumentation } from '@opentelemetry/instrumentation';
import type { SpanProcessor } from '@opentelemetry/sdk-trace';

export const ENV_OTEL_SERVICE_NAME = 'OTEL_SERVICE_NAME';
export const ENV_RESOURCE_ATTRIBUTES = 'OTEL_RESOURCE_ATTRIBUTES';
export const ENV_SERVICE_NAME = 'SERVICE_NAME';
export const ENV_ENVIRONMENT = 'ENVIRONMENT';
export const ENV_DEBUG_LEVEL = 'OTEL_HELPER_DEBUG_LEVEL';
export const ENV_DISABLED_SIGNALS = 'OTEL_HELPER_DISABLED_SIGNALS';
export const ENV_EXTRA_INSTRUMENTATION = 'OTEL_HELPER_EXTRA_INSTRUMENTATION';
export const ENV_NODE_RESOURCE_DETECTORS = 'OTEL_NODE_RESOURCE_DETECTORS';
export const ENV_CONFIG_FILE = 'OTEL_CONFIG_FILE';
/** Deprecated name of `OTEL_CONFIG_FILE`, warned the same way (contract D1 row "OTEL_CONFIG_FILE"). */
export const ENV_EXPERIMENTAL_CONFIG_FILE = 'OTEL_EXPERIMENTAL_CONFIG_FILE';
/** Semantic-convention migration switch (design D-JS9); its database tokens are read by the opt-in packages. */
export const ENV_SEMCONV_OPT_IN = 'OTEL_SEMCONV_STABILITY_OPT_IN';

export const ATTR_SERVICE_NAME = 'service.name';
export const ATTR_ENVIRONMENT = 'deployment.environment.name';
export const ATTR_INSTANCE_ID = 'service.instance.id';

/** `telemetry.sdk.*` keys the SDK sets; env and code values for them are ignored (contract D1). */
export const TELEMETRY_SDK_KEYS: readonly string[] = ['telemetry.sdk.name', 'telemetry.sdk.language', 'telemetry.sdk.version'];

// Detector names that only name what the helper already does (env parse, instance id) or none.
const BUILTIN_DETECTORS: ReadonlySet<string> = new Set(['env', 'serviceinstance', 'none']);

/**
 * `setup()` options set in code (design API table). A key left `undefined` is
 * not set and reads the environment. Task B3 implements the identity,
 * log-level and diagnostics options, task B4a the OTLP endpoint, protocol and
 * `insecure` options, task B4b `exportTimeoutMs` and `compression`, task B4c
 * `metricExporters`, `exportIntervalMs` and `disabledSignals`, task B5
 * `sampleRatio` and `healthPaths`, task B7 `prometheusMetricsPort`, A21
 * `disabledMetrics`, task B10a `instrumentations`; the other documented options land with their tasks and
 * are rejected as unknown until then. Pass a plain object (or
 * a null-prototype one); class instances are rejected. `setup()` reads each
 * property once.
 */
export interface SetupOptions {
  /** `service.name`; wins over `OTEL_SERVICE_NAME` and `OTEL_RESOURCE_ATTRIBUTES`. Must not be blank. */
  readonly serviceName?: string | undefined;
  /** `deployment.environment.name`, emitted verbatim; `LOCAL`/`DEV`/`HML`/`PRD` switch the log level. Must not be blank. */
  readonly environment?: string | undefined;
  /** Resource attributes; each key wins over the same key in `OTEL_RESOURCE_ATTRIBUTES`. */
  readonly resourceAttributes?: Readonly<Record<string, string>> | undefined;
  /**
   * Debug mode (`OTEL_HELPER_DEBUG_LEVEL`): log level debug, and `debug="true"`
   * on the service's entry spans (no parent or a remote one; ADR-006).
   */
  readonly debug?: boolean | undefined;
  /** Log level of the helper's logger; default `defaultLogLevel(environment, debug)`. */
  readonly minimumLogLevel?: LogLevel | undefined;
  /**
   * Install the helper's stderr `DiagLogger` for OpenTelemetry SDK messages
   * (default true: level `OTEL_LOG_LEVEL`, else WARN). `false` leaves `diag` alone.
   */
  readonly installDiagLogger?: boolean | undefined;
  /**
   * Generic OTLP endpoint (all signals): a URL (`http://`, `https://`) or a
   * schemeless `host:port`. `http/protobuf` gets `/v1/<signal>` appended. Wins
   * over every endpoint environment variable; an empty string is not set.
   */
  readonly otelEndpoint?: string | undefined;
  /** Traces endpoint, used verbatim (path kept); wins over `otelEndpoint`. */
  readonly tracesEndpoint?: string | undefined;
  /** Metrics endpoint, used verbatim (path kept); wins over `otelEndpoint`. */
  readonly metricsEndpoint?: string | undefined;
  /** Logs endpoint, used verbatim (path kept); wins over `otelEndpoint`. */
  readonly logsEndpoint?: string | undefined;
  /** OTLP protocol of every signal: `grpc` or `http/protobuf` (case-insensitive). */
  readonly otlpProtocol?: string | undefined;
  /**
   * Plaintext for a schemeless endpoint. With a URL the scheme decides: a code
   * value contradicting a code URL's scheme throws; contradicting an env URL warns.
   */
  readonly insecure?: boolean | undefined;
  /**
   * OTLP export timeout of every signal, in milliseconds (an integer in
   * [1, 86 400 000]); wins over `OTEL_EXPORTER_OTLP[_<SIGNAL>]_TIMEOUT`.
   */
  readonly exportTimeoutMs?: number | undefined;
  /** OTLP compression of every signal: `gzip` or `none` (case-insensitive); an empty string is not set. */
  readonly compression?: string | undefined;
  /**
   * Metric exporters: `otlp`, `prometheus`, `console` (case-insensitive); `[]`
   * or `['none']` disables metrics. Wins over `OTEL_METRICS_EXPORTER`. `otlp`
   * without a metrics endpoint, an unknown value or `none` with other values throw.
   */
  readonly metricExporters?: readonly string[] | undefined;
  /** Metric export interval in milliseconds (an integer in [1, 2^31-1]); wins over `OTEL_METRIC_EXPORT_INTERVAL`. */
  readonly exportIntervalMs?: number | undefined;
  /**
   * Port of the helper's `/metrics` listener (an integer in [0, 65535]; `0` =
   * no listener, mount `metricsHandler()` instead); wins over
   * `OTEL_HELPER_METRICS_PORT`, whatever the value (9464 included).
   */
  readonly prometheusMetricsPort?: number | undefined;
  /**
   * Instrument-name globs to drop from every exporter (case-sensitive; `*` any
   * run of characters, `?` one, everything else literal); wins over
   * `OTEL_HELPER_DISABLED_METRICS`, `[]` included. Items are trimmed, blank
   * ones ignored.
   */
  readonly disabledMetrics?: readonly string[] | undefined;
  /**
   * Signals to disable (`traces`, `metrics`, `logs`); wins over the deprecated
   * `OTEL_HELPER_DISABLED_SIGNALS`, `[]` included. Prefer `OTEL_<SIGNAL>_EXPORTER=none`.
   */
  readonly disabledSignals?: readonly string[] | undefined;
  /**
   * Head-sampling ratio in [0, 1] for root spans:
   * `ParentBased(TraceIdRatioBased(sampleRatio))`. Wins over
   * `OTEL_TRACES_SAMPLER` and the deprecated `OTEL_HELPER_SAMPLE_RATIO`,
   * whatever the value (1 included).
   */
  readonly sampleRatio?: number | undefined;
  /**
   * HTTP paths whose server spans are not recorded (the requests still count
   * in the HTTP server metrics); replaces the default list
   * (`/health /healthz /ready /readyz /live /livez /ping /metrics`). Matching is
   * exact and case-insensitive, ignoring a trailing slash and the query. Each
   * path must be ASCII, start with `/` and carry no query, fragment or comma;
   * `[]` traces every path. `grpc.health.v1.Health` calls are dropped regardless.
   */
  readonly healthPaths?: readonly string[] | undefined;
  /**
   * Replace a TracerProvider, MeterProvider, LoggerProvider or propagator that
   * someone else registered before `setup()` (default false: keep it, warn,
   * and list its signal in `Resolved.inactiveSignals`). A foreign context
   * manager is always kept. Tracers the app obtained from the replaced
   * provider keep using it.
   */
  readonly replaceGlobals?: boolean | undefined;
  /**
   * Opt-in instrumentations, e.g. `sqlInstrumentation()` from
   * `@staffops/otel-helper-sql`, or any `@opentelemetry/instrumentation`
   * `Instrumentation`. Each setup binds them to the helper's providers and
   * enables them; `shutdown()` disables them. Not enabled under
   * `OTEL_SDK_DISABLED=true` or into another API version's registry. Create
   * them before the app loads the libraries they instrument.
   */
  readonly instrumentations?: readonly Instrumentation[] | undefined;
  /** Test/advanced: extra span processors, added after the helper's own (design D-JS7). */
  readonly spanProcessors?: readonly SpanProcessor[] | undefined;
}

/**
 * What `setup()` resolved, frozen. Every field is the effective value, whatever
 * set it (code, environment or default).
 */
export interface Resolved {
  /** Effective `service.name` (the SDK default `unknown_service:<argv0>` when none was configured). */
  readonly serviceName: string;
  /** Emitted `deployment.environment.name`, verbatim; `undefined` when absent (ADR-003). */
  readonly environment: string | undefined;
  /** Effective `service.instance.id` (per-process UUID unless code or `OTEL_RESOURCE_ATTRIBUTES` set one, D4). */
  readonly serviceInstanceId: string;
  /** `debug` option, else `OTEL_HELPER_DEBUG_LEVEL`. */
  readonly debug: boolean;
  /** `minimumLogLevel` option, else `defaultLogLevel(environment, debug)` (R4). */
  readonly logLevel: LogLevel;
  /**
   * OTLP endpoint per signal (R1, R2, D1); `undefined` = no OTLP exporter for
   * that signal. Used only by a signal whose exporter list holds `otlp`.
   */
  readonly otlp: OtlpPlan;
  /** `OTEL_SDK_DISABLED=true`: no providers and no exporters; the context manager is still installed. */
  readonly sdkDisabled: boolean;
  /** Effective trace exporters (`OTEL_TRACES_EXPORTER`, default `otlp`); `[]` = none. */
  readonly tracesExporters: readonly SpanLogExporterName[];
  /**
   * Effective metric exporters (`metricExporters` > `OTEL_METRICS_EXPORTER` >
   * `otlp` with a metrics endpoint, else `prometheus`); `[]` = none.
   */
  readonly metricExporters: readonly MetricExporterName[];
  /** Effective log exporters (`OTEL_LOGS_EXPORTER`, default `otlp`); `[]` = none. */
  readonly logsExporters: readonly SpanLogExporterName[];
  /** Metric export interval in milliseconds (`exportIntervalMs` > `OTEL_METRIC_EXPORT_INTERVAL` > 30 000). */
  readonly metricExportIntervalMs: number;
  /**
   * Port of the `/metrics` listener (`prometheusMetricsPort` >
   * `OTEL_HELPER_METRICS_PORT` > 9464; `0` = none). Used only while
   * `metricExporters` holds `prometheus`.
   */
  readonly prometheusMetricsPort: number;
  /** Disabled-metric globs (`disabledMetrics` > `OTEL_HELPER_DISABLED_METRICS`), trimmed, blanks dropped. */
  readonly disabledMetrics: readonly string[];
  /**
   * The sampler (`sampleRatio` > `OTEL_TRACES_SAMPLER`/`_ARG` >
   * `OTEL_HELPER_SAMPLE_RATIO` > `parentbased_always_on`), wrapped by the
   * health sampler. Under `OTEL_SDK_DISABLED=true` the sampler variables are
   * not read and this is the default (no TracerProvider is built).
   */
  readonly sampler: SamplerConfig;
  /**
   * Propagators (`OTEL_PROPAGATORS` > `tracecontext`, `baggage`); `[]` = `none`:
   * what the helper installs when it owns the global propagator (also under
   * `OTEL_SDK_DISABLED=true`). A propagator registered before `setup()` is kept
   * and this list is not applied (design D-JS14).
   */
  readonly propagators: readonly PropagatorName[];
  /** HTTP health paths whose server spans are dropped (`healthPaths` > the default list). */
  readonly healthPaths: readonly string[];
  /**
   * Node only (Go and Python have no such field). Signals whose **global
   * provider** is not the helper's (spec JS7): `traces` / `metrics` when
   * someone else's global provider was kept (or the API refused the
   * helper's); all three when the API globals belong to another
   * `@opentelemetry/api` version or another helper version owns the process.
   * A foreign LoggerProvider does not list `logs`: the helper's loggers still
   * export through the helper's provider (as Python). It covers global
   * ownership only: a signal whose exporter failed to build (reported on the
   * diagnostics sink) is not listed. `[]` otherwise, also under
   * `OTEL_SDK_DISABLED=true`.
   */
  readonly inactiveSignals: readonly Signal[];
}

/** Resolver output: the public view plus what the providers are built from. */
export interface Plan {
  readonly resolved: Resolved;
  /** Attributes layered over the SDK `defaultResource()` (env, helper fallbacks, code). */
  readonly resourceAttributes: Readonly<Record<string, string>>;
  /** Parsed OTLP headers per configured signal (secrets: kept out of `Resolved`). */
  readonly otlpHeaders: OtlpHeaders;
  /** Batch processor settings and limits the SDK 3.0 no longer reads (spec JS2). */
  readonly sdk: SdkSettings;
  /** Where {@link Resolved.metricExportIntervalMs} came from (option, env variable or `default`). */
  readonly intervalSource: string;
}

const OPTION_KEYS: ReadonlySet<string> = new Set([
  'serviceName',
  'environment',
  'resourceAttributes',
  'debug',
  'minimumLogLevel',
  'installDiagLogger',
  'otelEndpoint',
  'tracesEndpoint',
  'metricsEndpoint',
  'logsEndpoint',
  'otlpProtocol',
  'insecure',
  'exportTimeoutMs',
  'compression',
  'metricExporters',
  'exportIntervalMs',
  'prometheusMetricsPort',
  'disabledMetrics',
  'disabledSignals',
  'sampleRatio',
  'healthPaths',
  'replaceGlobals',
  'instrumentations',
  'spanProcessors',
]);

function fail(option: string, why: string): never {
  throw new TypeError(`setup(): ${option} ${why}`);
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return false;
  const proto: unknown = Object.getPrototypeOf(value);
  return proto === Object.prototype || proto === null;
}

function checkNonBlankString(option: string, value: unknown): void {
  if (value === undefined) return;
  if (typeof value !== 'string') fail(option, `must be a string, got ${typeName(value)}`);
  if (trimWS(value) === '') fail(option, 'must not be empty');
}

function checkString(option: string, value: unknown): void {
  if (value !== undefined && typeof value !== 'string') fail(option, `must be a string, got ${typeName(value)}`);
}

function checkBoolean(option: string, value: unknown): void {
  if (value !== undefined && typeof value !== 'boolean') fail(option, `must be a boolean, got ${typeName(value)}`);
}

const SPAN_PROCESSOR_METHODS = ['onStart', 'onEnd', 'forceFlush', 'shutdown'] as const;
const INSTRUMENTATION_METHODS = ['enable', 'disable', 'setTracerProvider', 'setMeterProvider'] as const;

function isInstrumentation(value: unknown): value is Instrumentation {
  if (typeof value !== 'object' || value === null) return false;
  const o = value as Record<string, unknown>;
  return typeof o.instrumentationName === 'string' && INSTRUMENTATION_METHODS.every((m) => typeof o[m] === 'function');
}

/** Instrumentation names the core installs itself (`instrumentation.ts`): an opt-in one with these would patch twice. */
export const CORE_INSTRUMENTATION_NAMES: ReadonlySet<string> = new Set([
  '@opentelemetry/instrumentation-http',
  '@opentelemetry/instrumentation-undici',
  '@opentelemetry/instrumentation-express',
  '@opentelemetry/instrumentation-grpc',
]);

/**
 * The instrumentation names one opt-in object covers: its own and, for a
 * composite such as `sqlInstrumentation()`, the `coveredInstrumentations` it
 * wraps (a string array; anything else is rejected).
 */
function coveredNames(instrumentation: Instrumentation): string[] {
  const covered: unknown = (instrumentation as { coveredInstrumentations?: unknown }).coveredInstrumentations;
  if (covered === undefined) return [instrumentation.instrumentationName];
  if (!Array.isArray(covered) || !covered.every((n) => typeof n === 'string')) {
    fail('instrumentations', `has ${JSON.stringify(instrumentation.instrumentationName)} with coveredInstrumentations that is not an array of strings`);
  }
  return [instrumentation.instrumentationName, ...(covered)];
}

/**
 * Two instrumentations for the same library would patch it twice (and a raw
 * contrib one next to its helper wrapper would undo the wrapper's settings,
 * e.g. the mysql2 statement mask): duplicates are found by name, as are the
 * names the core owns.
 */
function checkInstrumentationNames(list: readonly Instrumentation[]): void {
  const seen = new Map<string, string>();
  for (const instrumentation of list) {
    for (const name of coveredNames(instrumentation)) {
      if (CORE_INSTRUMENTATION_NAMES.has(name)) {
        fail('instrumentations', `has ${JSON.stringify(name)}, which the helper installs itself; remove it`);
      }
      const owner = seen.get(name);
      if (owner !== undefined) {
        fail('instrumentations', `has two instrumentations for ${JSON.stringify(name)} (${JSON.stringify(owner)} and ${JSON.stringify(instrumentation.instrumentationName)}); pass one`);
      }
      seen.set(name, instrumentation.instrumentationName);
    }
  }
}

/**
 * Rejects invalid code options (R8: fail fast, naming the option) and returns
 * a snapshot: each own enumerable property is read once, so a getter cannot
 * return one value to validation and another to resolution. A key set to
 * `undefined` counts as not set (it is dropped, and is never "unknown").
 * Other unknown keys are rejected: a typo must not silently fall back to the
 * environment. Only plain objects (or null-prototype objects) are accepted.
 */
export function validateOptions(options: unknown): SetupOptions {
  if (options === undefined) return Object.freeze({});
  if (!isPlainObject(options)) fail('options', `must be an object, got ${typeName(options)}`);
  const o: Record<string, unknown> = {};
  for (const key of Object.keys(options)) {
    const value = options[key];
    if (value !== undefined) o[key] = value;
  }
  const unknown = Object.keys(o).filter((k) => !OPTION_KEYS.has(k));
  if (unknown.length > 0) fail('options', `has unknown key(s) ${unknown.sort().join(', ')}`);
  checkNonBlankString('serviceName', o.serviceName);
  checkNonBlankString('environment', o.environment);
  if (o.resourceAttributes !== undefined) {
    const attrs = o.resourceAttributes;
    if (!isPlainObject(attrs)) fail('resourceAttributes', `must be an object of strings, got ${typeName(attrs)}`);
    const copy = Object.create(null) as Record<string, string>;
    for (const key of Object.keys(attrs)) {
      const value = attrs[key];
      if (trimWS(key) === '') fail('resourceAttributes', 'has an empty key');
      if (typeof value !== 'string') fail('resourceAttributes', `value of ${JSON.stringify(key)} must be a string, got ${typeName(value)}`);
      copy[key] = value;
    }
    o.resourceAttributes = Object.freeze(copy);
  }
  checkBoolean('debug', o.debug);
  checkBoolean('installDiagLogger', o.installDiagLogger);
  checkBoolean('replaceGlobals', o.replaceGlobals);
  for (const option of ['otelEndpoint', 'tracesEndpoint', 'metricsEndpoint', 'logsEndpoint', 'otlpProtocol', 'compression']) {
    checkString(option, o[option]);
  }
  checkBoolean('insecure', o.insecure);
  if (o.exportTimeoutMs !== undefined && typeof o.exportTimeoutMs !== 'number') {
    fail('exportTimeoutMs', `must be a number, got ${typeName(o.exportTimeoutMs)}`);
  }
  checkCodeOtlp(o);
  Object.assign(o, checkCodeSignals(o), checkCodeTracing(o));
  if (o.prometheusMetricsPort !== undefined) o.prometheusMetricsPort = checkCodeMetricsPort(o.prometheusMetricsPort);
  if (o.disabledMetrics !== undefined) o.disabledMetrics = checkCodeDisabledMetrics(o.disabledMetrics);
  const level = o.minimumLogLevel;
  if (level !== undefined && !LOG_LEVELS.includes(level as LogLevel)) {
    const shown = typeof level === 'string' ? JSON.stringify(level) : typeName(level);
    fail('minimumLogLevel', `must be one of ${LOG_LEVELS.join(', ')}, got ${shown}`);
  }
  if (o.spanProcessors !== undefined) {
    if (!Array.isArray(o.spanProcessors)) fail('spanProcessors', `must be an array, got ${typeName(o.spanProcessors)}`);
    const processors = [...(o.spanProcessors as unknown[])];
    for (const p of processors) {
      const ok = typeof p === 'object' && p !== null && SPAN_PROCESSOR_METHODS.every((m) => typeof (p as Record<string, unknown>)[m] === 'function');
      if (!ok) fail('spanProcessors', 'must contain SpanProcessor objects (onStart, onEnd, forceFlush, shutdown)');
    }
    o.spanProcessors = Object.freeze(processors);
  }
  if (o.instrumentations !== undefined) {
    if (!Array.isArray(o.instrumentations)) fail('instrumentations', `must be an array, got ${typeName(o.instrumentations)}`);
    // The same object twice is enabled once; two objects for one library are rejected below.
    const list = [...new Set(o.instrumentations as unknown[])];
    if (!list.every(isInstrumentation)) {
      fail('instrumentations', 'must contain Instrumentation objects (instrumentationName, enable, disable, setTracerProvider, setMeterProvider)');
    }
    checkInstrumentationNames(list);
    o.instrumentations = Object.freeze(list);
  }
  return Object.freeze(o);
}

// Deprecated or dead variables (design D8, Go G9), warned once per process
// whenever they are set, also when a standard variable or code wins. Only the
// variable name is printed, never its value.
const DEPRECATED_ENV: readonly (readonly [string, string])[] = [
  [ENV_SERVICE_NAME, ENV_OTEL_SERVICE_NAME],
  [ENV_ENVIRONMENT, `${ENV_RESOURCE_ATTRIBUTES} (deployment.environment.name=<value>)`],
  [ENV_SAMPLE_RATIO, 'OTEL_TRACES_SAMPLER=parentbased_traceidratio with OTEL_TRACES_SAMPLER_ARG'],
  [ENV_DISABLED_SIGNALS, 'OTEL_TRACES_EXPORTER/OTEL_METRICS_EXPORTER/OTEL_LOGS_EXPORTER=none'],
];

function warnDeprecatedEnv(): void {
  for (const [variable, replacement] of DEPRECATED_ENV) {
    if (envValue(variable) !== undefined) {
      reportOnce(`deprecated:${variable}`, `${variable} is deprecated and will be removed in 1.0; use ${replacement}`, {
        variable,
        replacement,
      });
    }
  }
  // Detector-selection rule (contract design D1, 2026-10-29): the helper builds
  // the resource itself, so only the variable name is reported.
  const detectors = (envValue(ENV_NODE_RESOURCE_DETECTORS) ?? '').split(',').map((d) => trimWS(d)).filter((d) => d !== '');
  if (detectors.some((d) => !BUILTIN_DETECTORS.has(d))) {
    reportOnce(
      `unused:${ENV_NODE_RESOURCE_DETECTORS}`,
      `${ENV_NODE_RESOURCE_DETECTORS} is ignored: the helper builds the resource itself; add host/process/cloud attributes in the Collector (resourcedetection, k8sattributes processors)`,
      { variable: ENV_NODE_RESOURCE_DETECTORS },
    );
  }
  for (const variable of [ENV_CONFIG_FILE, ENV_EXPERIMENTAL_CONFIG_FILE]) {
    if (envValue(variable) === undefined) continue;
    // Contract D1 row "OTEL_CONFIG_FILE" (A19): not supported; the name only, never the path.
    reportOnce(
      `unused:${variable}`,
      `${variable} is not supported by the Node.js helper and is ignored; configure it with environment variables or setup() options`,
      { variable },
    );
  }
  if (envValue(ENV_EXTRA_INSTRUMENTATION) !== undefined) {
    reportOnce(
      `unused:${ENV_EXTRA_INSTRUMENTATION}`,
      // B10b/B10c add redisInstrumentation() and awsInstrumentation() when they exist.
      `${ENV_EXTRA_INSTRUMENTATION} has no effect in Node.js and is ignored; pass sqlInstrumentation() (@staffops/otel-helper-sql) in setup({ instrumentations })`,
      { variable: ENV_EXTRA_INSTRUMENTATION },
    );
  }
  warnSemconvOptIn();
}

/**
 * `OTEL_SEMCONV_STABILITY_OPT_IN` (design D-JS9): the Node.js HTTP
 * instrumentations emit only the stable HTTP conventions (instrumentation-http
 * 0.300 has no old generation), so a value asking for the old ones — no
 * `http` token, or `http/dup` — is warned once and has no HTTP effect. Tokens
 * are trimmed and ASCII case-folded (contract D1 row "ASCII case folding").
 * The database tokens are read by the opt-in packages (`-sql`).
 */
function warnSemconvOptIn(): void {
  const value = envValue(ENV_SEMCONV_OPT_IN);
  if (value === undefined) return;
  const tokens = new Set(value.split(',').map((t) => asciiLower(trimWS(t))));
  if (tokens.has('http') && !tokens.has('http/dup')) return;
  reportOnce(
    `semconv:${ENV_SEMCONV_OPT_IN}`,
    `${ENV_SEMCONV_OPT_IN} asks for the old HTTP semantic conventions (no 'http' token, or 'http/dup'); the Node.js HTTP instrumentations emit only the stable ones, so HTTP telemetry is unchanged`,
    { variable: ENV_SEMCONV_OPT_IN },
  );
}

function envResourceAttributes(warn: boolean): Readonly<Record<string, string>> {
  const raw = process.env[ENV_RESOURCE_ATTRIBUTES];
  if (raw === undefined || trimWS(raw) === '') return Object.create(null) as Record<string, string>;
  const parsed = parseResourceAttributes(raw);
  // Positions only: a resource attribute value is never printed. Once per process (D1 cadence row).
  if (warn && parsed.malformed.length > 0) {
    reportOnce('resource-attributes:malformed', 'resource attribute items are not key=value and were skipped', {
      variable: ENV_RESOURCE_ATTRIBUTES,
      items: parsed.malformed.join(','),
    });
  }
  if (warn && parsed.undecodable.length > 0) {
    reportOnce('resource-attributes:undecodable', 'resource attribute values have an invalid percent-encoding and are kept literally', {
      variable: ENV_RESOURCE_ATTRIBUTES,
      items: parsed.undecodable.join(','),
    });
  }
  return parsed.attributes;
}

/**
 * Resolves the identity rows (contract D1, D4, ADR-003; spec JS4) from
 * already-validated options. `defaultServiceName` is the SDK default
 * (`unknown_service:<argv0>`). Warnings go to the diagnostics sink; `quiet`
 * (an inactive copy, design D-JS10) drops the per-setup identity and resource
 * warnings — the copy installs nothing they would describe — and keeps the
 * once-per-process deprecations. Throws a `TypeError` when the code
 * `metricExporters` holds `otlp` and metrics have no endpoint (R2: known only
 * once the environment is read).
 */
export function resolvePlan(options: SetupOptions, defaultServiceName: string, quiet = false): Plan {
  warnDeprecatedEnv();
  const code: Readonly<Record<string, string>> = options.resourceAttributes ?? {};
  const env = envResourceAttributes(!quiet);
  const layer: Record<string, string> = Object.create(null) as Record<string, string>;
  for (const [key, value] of Object.entries(env)) defineAttr(layer, key, value);

  // service.name: code > OTEL_SERVICE_NAME > OTEL_RESOURCE_ATTRIBUTES >
  // SERVICE_NAME (deprecated) > SDK default + warning. Whitespace-only = unset.
  const codeName = options.serviceName !== undefined ? trimWS(options.serviceName) : nonBlank(code[ATTR_SERVICE_NAME]);
  const raName = nonBlank(env[ATTR_SERVICE_NAME]);
  let serviceName = codeName ?? envValue(ENV_OTEL_SERVICE_NAME) ?? raName ?? envValue(ENV_SERVICE_NAME);
  if (serviceName === undefined) {
    serviceName = defaultServiceName;
    if (!quiet) reportOnce('identity:service.name', `no service name configured; set ${ENV_OTEL_SERVICE_NAME}`, { using: defaultServiceName });
  }

  // deployment.environment.name: code > OTEL_RESOURCE_ATTRIBUTES > ENVIRONMENT
  // (deprecated alias) > absent + warning. Known values (case-insensitive) of
  // the alias are canonicalized; everything else is emitted verbatim.
  const codeEnv = options.environment !== undefined ? trimWS(options.environment) : nonBlank(code[ATTR_ENVIRONMENT]);
  let environment = codeEnv ?? nonBlank(env[ATTR_ENVIRONMENT]);
  if (environment === undefined) {
    const alias = envValue(ENV_ENVIRONMENT);
    if (alias !== undefined) environment = knownEnvironment(alias) ?? alias;
  }
  if (environment === undefined && !quiet) {
    reportOnce(
      'identity:environment',
      `${ATTR_ENVIRONMENT} is not set; set it in ${ENV_RESOURCE_ATTRIBUTES} (e.g. ${ATTR_ENVIRONMENT}=PRD); log level defaults to INFO`,
      { using: 'absent' },
    );
  }

  // service.instance.id: code > OTEL_RESOURCE_ATTRIBUTES > per-process UUID (D4).
  // randomUUID() runs per resolve, so each process (cluster worker) gets its own id;
  // a forked worker is a fresh process and never inherits the primary's (D-JS13).
  const explicitInstanceId = nonBlank(code[ATTR_INSTANCE_ID]) ?? nonBlank(env[ATTR_INSTANCE_ID]);
  const serviceInstanceId = explicitInstanceId ?? randomUUID();
  if (explicitInstanceId !== undefined && cluster.isWorker && !quiet) {
    // D4: an explicit id in a multi-worker server may collide; kept, never rewritten. Same
    // wording as Python except the worker kind (forked/cluster) (contract D1 "Explicit instance id in a worker", A24).
    reportOnce(
      'cluster:service.instance.id',
      `${ATTR_INSTANCE_ID} is set explicitly in a cluster worker: unless each worker sets a different value, every worker reports the same id; leave it unset so each process gets its own`,
      { key: ATTR_INSTANCE_ID },
    );
  }

  // Own data properties only (never `Object.assign`/`[[Set]]`): `__proto__` is an ordinary key.
  for (const [key, value] of Object.entries(code)) {
    if (TELEMETRY_SDK_KEYS.includes(key)) {
      if (!quiet) reportOnce(`code-telemetry-sdk:${key}`, `${key} in resourceAttributes is ignored; the OpenTelemetry SDK sets it`, { key });
      continue;
    }
    defineAttr(layer, key, value);
  }
  layer[ATTR_SERVICE_NAME] = serviceName;
  layer[ATTR_INSTANCE_ID] = serviceInstanceId;
  if (environment === undefined) Reflect.deleteProperty(layer, ATTR_ENVIRONMENT);
  else layer[ATTR_ENVIRONMENT] = environment;

  // A value set in code wins whatever it is (D1 ruling): only `undefined` reads the env.
  const debug = options.debug ?? envBool(ENV_DEBUG_LEVEL, quiet ? SILENT_REPORTER.warn : undefined) ?? false;
  const logLevel = options.minimumLogLevel ?? defaultLogLevel(environment, debug);

  const reporter = quiet ? SILENT_REPORTER : newReporter();
  const { plan: otlp, headers: otlpHeaders } = resolveOtlp(options, reporter);
  const signals = resolveSignals(options, otlp, reporter.warn);
  const sdk = resolveSdkSettings(reporter.warn);
  // Not read under OTEL_SDK_DISABLED=true (no TracerProvider is built), as in Go and Python.
  const sampler = signals.sdkDisabled ? DEFAULT_SAMPLER : resolveSampler(options, reporter.warn);
  const propagators = resolvePropagators(reporter.warn);
  const prometheusMetricsPort = resolveMetricsPort(options.prometheusMetricsPort, reporter.warn);

  const resolved: Resolved = Object.freeze({
    serviceName,
    environment,
    serviceInstanceId,
    debug,
    logLevel,
    otlp,
    sdkDisabled: signals.sdkDisabled,
    tracesExporters: signals.tracesExporters,
    metricExporters: signals.metricExporters,
    logsExporters: signals.logsExporters,
    metricExportIntervalMs: signals.metricExportIntervalMs,
    prometheusMetricsPort,
    disabledMetrics: resolveDisabledMetrics(options.disabledMetrics),
    sampler,
    propagators,
    healthPaths: options.healthPaths ?? DEFAULT_HEALTH_PATHS,
    // Set by setup() once it knows which globals it owns.
    inactiveSignals: Object.freeze([]),
  });
  return { resolved, resourceAttributes: Object.freeze(layer), otlpHeaders, sdk, intervalSource: signals.intervalSource };
}

const SILENT_REPORTER: Reporter = Object.freeze({ warn: () => undefined, info: () => undefined });

/**
 * A reporter that writes each configuration warning once per message, variable
 * and value per resolution (Go `warnOnce`: a generic setting shared by three
 * signals warns once), and each INFO once per key.
 */
function newReporter(): Reporter {
  const seen = new Set<string>();
  const warn: Warn = (message, attrs) => {
    const key = `${message}\0${String(attrs.variable)}\0${String(attrs.value)}`;
    if (seen.has(key)) return;
    seen.add(key);
    report(message, attrs);
  };
  const info = (key: string, message: string, attrs: Readonly<Record<string, DiagnosticValue>>): void => {
    if (seen.has(`info:${key}`)) return;
    seen.add(`info:${key}`);
    report(message, attrs, { origin: 'helper', level: 'info' });
  };
  return Object.freeze({ warn, info });
}

function defineAttr(target: Record<string, string>, key: string, value: string): void {
  Object.defineProperty(target, key, { value, enumerable: true, writable: true, configurable: true });
}

function nonBlank(value: string | undefined): string | undefined {
  if (value === undefined) return undefined;
  const trimmed = trimWS(value);
  return trimmed === '' ? undefined : trimmed;
}
