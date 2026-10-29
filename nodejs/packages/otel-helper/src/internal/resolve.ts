/**
 * The resolver (design D-JS3): code options and the environment become one
 * frozen {@link Resolved} plus the resource attributes. Explicit code > standard
 * `OTEL_*` env > legacy / `OTEL_HELPER_*` env > default (contract D1). Invalid
 * code values throw before anything is built; environment problems only warn
 * (R8). This module covers the identity rows (task B3) and the OTLP endpoint,
 * protocol and TLS-setting rows (task B4a, `otlp.ts`); later tasks add theirs.
 */
import { randomUUID } from 'node:crypto';
import { report, reportOnce, type DiagnosticValue } from './diagnostics';
import { envBool, envValue, trimWS, type Warn } from './env';
import { checkCodeOtlp, resolveOtlp, type OtlpPlan, type Reporter } from './otlp';
import { defaultLogLevel, knownEnvironment, LOG_LEVELS, type LogLevel } from './log-level';
import { parseResourceAttributes } from './resource-attributes';
import type { SpanProcessor } from '@opentelemetry/sdk-trace';

export const ENV_OTEL_SERVICE_NAME = 'OTEL_SERVICE_NAME';
export const ENV_RESOURCE_ATTRIBUTES = 'OTEL_RESOURCE_ATTRIBUTES';
export const ENV_SERVICE_NAME = 'SERVICE_NAME';
export const ENV_ENVIRONMENT = 'ENVIRONMENT';
export const ENV_DEBUG_LEVEL = 'OTEL_HELPER_DEBUG_LEVEL';
export const ENV_SAMPLE_RATIO = 'OTEL_HELPER_SAMPLE_RATIO';
export const ENV_DISABLED_SIGNALS = 'OTEL_HELPER_DISABLED_SIGNALS';
export const ENV_EXTRA_INSTRUMENTATION = 'OTEL_HELPER_EXTRA_INSTRUMENTATION';
export const ENV_NODE_RESOURCE_DETECTORS = 'OTEL_NODE_RESOURCE_DETECTORS';

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
 * `insecure` options; the other documented options land with their tasks and
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
  /** Debug mode (`OTEL_HELPER_DEBUG_LEVEL`): log level debug. */
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
  /** OTLP endpoint per signal (R1, R2, D1); `undefined` = no OTLP exporter for that signal. */
  readonly otlp: OtlpPlan;
}

/** Resolver output: the public view plus what the providers are built from. */
export interface Plan {
  readonly resolved: Resolved;
  /** Attributes layered over the SDK `defaultResource()` (env, helper fallbacks, code). */
  readonly resourceAttributes: Readonly<Record<string, string>>;
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
  'spanProcessors',
]);

function typeName(value: unknown): string {
  if (value === null) return 'null';
  if (Array.isArray(value)) return 'array';
  return typeof value;
}

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
  for (const option of ['otelEndpoint', 'tracesEndpoint', 'metricsEndpoint', 'logsEndpoint', 'otlpProtocol']) checkString(option, o[option]);
  checkBoolean('insecure', o.insecure);
  checkCodeOtlp(o);
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
  if (envValue(ENV_EXTRA_INSTRUMENTATION) !== undefined) {
    reportOnce(
      `unused:${ENV_EXTRA_INSTRUMENTATION}`,
      `${ENV_EXTRA_INSTRUMENTATION} has no effect in Node.js and is ignored; database, Redis and AWS instrumentation come from the opt-in @staffops/otel-helper-sql, -redis and -aws packages`,
      { variable: ENV_EXTRA_INSTRUMENTATION },
    );
  }
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
 * once-per-process deprecations.
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
  const serviceInstanceId = nonBlank(code[ATTR_INSTANCE_ID]) ?? nonBlank(env[ATTR_INSTANCE_ID]) ?? randomUUID();

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

  const otlp = resolveOtlp(options, quiet ? SILENT_REPORTER : newReporter());

  const resolved: Resolved = Object.freeze({ serviceName, environment, serviceInstanceId, debug, logLevel, otlp });
  return { resolved, resourceAttributes: Object.freeze(layer) };
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
