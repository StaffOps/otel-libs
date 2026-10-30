/**
 * `setup()` / `shutdown()` (spec JS2, JS5, JS7; design D-JS1, D-JS10). Task B3
 * builds the resource, the context manager and the DiagLogger; task B4b the
 * OTLP exporters and the three providers (batch span processor, periodic
 * metric reader, batch log record processor); task B4c the exporter lists
 * (console exporters), `OTEL_SDK_DISABLED` and the export interval. The full lifecycle — rollback, foreign globals,
 * re-binding instrumentations, the stable forwarding providers of D-JS10 — is
 * task B9; the shape below (one runtime record on the shared state, rebuilt
 * after shutdown) is what it extends.
 */
import { context, diag, logs, metrics, trace, type Attributes } from '@opentelemetry/api';
import { AsyncLocalStorageContextManager } from '@opentelemetry/context-async-hooks';
import { defaultResource, resourceFromAttributes, type Resource } from '@opentelemetry/resources';
import { BatchLogRecordProcessor, ConsoleLogRecordExporter, LoggerProvider, type LogRecordProcessor } from '@opentelemetry/sdk-logs';
import { ConsoleMetricExporter, MeterProvider, PeriodicExportingMetricReader, type IMetricReader } from '@opentelemetry/sdk-metrics';
import { BatchSpanProcessor, ConsoleSpanExporter, TracerProvider, type SpanProcessor } from '@opentelemetry/sdk-trace';
import { clearReportedOnce, report, reportOnce, truncateValue } from './diagnostics';
import { installDiagLogger } from './diag-logger';
import { logExporter, metricExporter, spanExporter, tlsMaterial, withMaskedEnv, type ExporterInput, type FileCache } from './exporters';
import { foreignRegistry, registeredGlobal } from './globals';
import type { OtlpEndpoint } from './otlp';
import type { Signal } from './otlp-settings';
import { ATTR_SERVICE_NAME, TELEMETRY_SDK_KEYS, resolvePlan, validateOptions, type Plan, type Resolved, type SetupOptions } from './resolve';
import { getState, setDiagnosticSink, type HelperState } from './state';

/** What one successful `setup()` built. */
interface Active {
  readonly resolved: Resolved;
  /** `undefined` under `OTEL_SDK_DISABLED=true` (no providers are built). */
  readonly resource: Resource | undefined;
  readonly tracerProvider: TracerProvider | undefined;
  readonly meterProvider: MeterProvider | undefined;
  readonly loggerProvider: LoggerProvider | undefined;
  /** Whether the global TracerProvider is the helper's (false: a foreign one was registered first). */
  readonly ownsTracerProvider: boolean;
  readonly ownsMeterProvider: boolean;
  readonly ownsLoggerProvider: boolean;
  readonly contextManager: AsyncLocalStorageContextManager | undefined;
  /** Cached result of the first `shutdown()`. */
  shutdown: Promise<void> | undefined;
}

/** Helper runtime kept on the shared state (one per process for this version). */
interface Runtime {
  active: Active | undefined;
  /** The `diag` registry entry the helper's DiagLogger produced; `undefined` = not installed. */
  diagLogger: unknown;
}

const DEFAULT_SHUTDOWN_TIMEOUT_MS = 30_000;
/**
 * Export timeout of the console metric reader: the SDK reader default (Go and
 * Python leave the console reader at their SDK default), capped at the interval.
 */
const CONSOLE_READER_TIMEOUT_MS = 30_000;
/** Below this, a capped reader timeout is warned (D-JS14). */
const SHORT_READER_TIMEOUT_MS = 1000;
/** Largest delay `setTimeout` honors (larger values fire after 1 ms). */
const MAX_TIMER_MS = 2 ** 31 - 1;

function runtimeOf(state: HelperState): Runtime {
  state.runtime ??= { active: undefined, diagLogger: undefined } satisfies Runtime;
  return state.runtime as Runtime;
}

/**
 * The semantic-conventions schema the helper's resource declares: the semconv
 * version the helper targets, the same literal in every language (contract
 * design D1 row "telemetry.sdk.* and the resource schema URL", 2026-10-29).
 * Bumping it is a contract change.
 */
export const SCHEMA_URL = 'https://opentelemetry.io/schemas/1.43.0';


/**
 * The resolved attributes with the SDK's `telemetry.sdk.*` on top: env and
 * code values for those keys are ignored (same D1 row).
 */
function buildResource(base: Resource, attributes: Readonly<Record<string, string>>): Resource {
  // Null prototype and own data properties: a `__proto__` key stays an ordinary attribute.
  const merged = Object.create(null) as Attributes;
  const define = (key: string, value: Attributes[string]): void => {
    Object.defineProperty(merged, key, { value, enumerable: true, writable: true, configurable: true });
  };
  for (const key of Object.keys(attributes)) define(key, attributes[key]);
  for (const key of TELEMETRY_SDK_KEYS) {
    const value = base.attributes[key];
    if (value === undefined) Reflect.deleteProperty(merged, key);
    else define(key, value);
  }
  return resourceFromAttributes(merged, { schemaUrl: SCHEMA_URL });
}

function defaultServiceName(base: Resource): string {
  const name = base.attributes[ATTR_SERVICE_NAME];
  return typeof name === 'string' ? name : 'unknown_service';
}

function helperOwnsDiag(rt: Runtime): boolean {
  return rt.diagLogger !== undefined && registeredGlobal('diag') === rt.diagLogger;
}

/** The processors and the reader built from the OTLP plan. */
interface Pipelines {
  readonly spanProcessors: SpanProcessor[];
  readonly metricReaders: IMetricReader[];
  readonly logProcessors: LogRecordProcessor[];
}

/** A diagnostic held back until the masked environment is restored. */
export type Pending = readonly [
  string,
  Readonly<Record<string, string | number | boolean>>,
  /** `'info'`: reported once per process at INFO; omitted: a warning. */
  level?: 'info',
];

/**
 * Builds one exporter's pipeline (exporter, then its processor or reader). A
 * failure (a bug or an SDK change: every value was validated) is reported and
 * that exporter is not used, never thrown from `setup()` (R8); an exporter
 * already built is shut down. `name` names the exporter in the report.
 */
export function tryBuild<E extends { shutdown(): Promise<void> }, P>(
  signal: Signal,
  exporter: () => E,
  pipeline: (e: E) => P,
  pending: Pending[],
  name = 'OTLP',
): P | undefined {
  let built: E | undefined;
  try {
    built = exporter();
    return pipeline(built);
  } catch (err) {
    if (built !== undefined) void built.shutdown().catch(() => undefined);
    pending.push([
      `the ${name} exporter could not be built; this signal is not exported${name === 'OTLP' ? '' : ' to it'}`,
      { signal, error: truncateValue(err instanceof Error ? err.message : String(err)) },
    ]);
    return undefined;
  }
}

/**
 * The exporters, processors and metric readers of every signal, from its
 * exporter list (task B4c): `otlp` (only while the signal has an endpoint),
 * `console` (the SDK console exporters, printing to stdout, as Go and Python
 * do); the `prometheus` metric reader is task B7. The OTLP exporters are built
 * with the helper-owned OTLP variables hidden (spec JS2). The resolved OTLP
 * timeout of a signal is its single export deadline (contract D1 ruling
 * "Export deadline", A18): the batch processors get it; the metric reader
 * gets it capped at the export interval (the SDK requires timeout <=
 * interval), with a warning naming where the timeout and the interval came
 * from. The diagnostics are written after the environment is restored, so a
 * custom sink never runs while it is masked.
 */
function buildPipelines(plan: Plan): Pipelines {
  const { otlp, tracesExporters, metricExporters, logsExporters, metricExportIntervalMs: interval } = plan.resolved;
  const cache: FileCache = new Map();
  const pending: Pending[] = [];
  const seen = new Set<string>();
  const warnOnce: ExporterInput['warn'] = (message, attrs) => {
    const key = `${message}\0${String(attrs.variable)}\0${String(attrs.value)}`;
    if (seen.has(key)) return;
    seen.add(key);
    pending.push([message, attrs]);
  };
  const input = (signal: Signal, wanted: boolean): ExporterInput | undefined => {
    const endpoint = otlp[signal];
    if (!wanted || endpoint === undefined) return undefined;
    // TLS files are read before masking: their variables are masked too.
    const tls = endpoint.insecure ? undefined : tlsMaterial(signal, cache, warnOnce);
    return { signal, endpoint, headers: plan.otlpHeaders[signal], tls, warn: warnOnce };
  };
  const inputs = {
    traces: input('traces', tracesExporters.includes('otlp')),
    metrics: input('metrics', metricExporters.includes('otlp')),
    logs: input('logs', logsExporters.includes('otlp')),
  };
  const out: Pipelines = { spanProcessors: [], metricReaders: [], logProcessors: [] };
  const add = <T>(list: T[], built: T | undefined): void => {
    if (built !== undefined) list.push(built);
  };
  const { bsp, blrp } = plan.sdk;
  withMaskedEnv(() => {
    const traces = inputs.traces;
    if (traces !== undefined) {
      add(out.spanProcessors, tryBuild(
        'traces',
        () => spanExporter(traces),
        (exporter) => new BatchSpanProcessor({ exporter, ...bsp, exportTimeoutMillis: traces.endpoint.timeoutMs }),
        pending,
      ));
    }
    if (tracesExporters.includes('console')) {
      add(out.spanProcessors, tryBuild('traces', () => new ConsoleSpanExporter(), (exporter) => new BatchSpanProcessor({ exporter, ...bsp }), pending, 'console'));
    }
    const metricsIn = inputs.metrics;
    if (metricsIn !== undefined) {
      const timeout = metricTimeout(metricsIn.endpoint, interval, plan.intervalSource, warnOnce);
      add(out.metricReaders, tryBuild(
        'metrics',
        () => metricExporter(metricsIn),
        (exporter) => new PeriodicExportingMetricReader({ exporter, exportIntervalMillis: interval, exportTimeoutMillis: timeout }),
        pending,
      ));
    }
    if (metricExporters.includes('console')) {
      add(out.metricReaders, tryBuild(
        'metrics',
        () => new ConsoleMetricExporter(),
        (exporter) =>
          new PeriodicExportingMetricReader({ exporter, exportIntervalMillis: interval, exportTimeoutMillis: Math.min(CONSOLE_READER_TIMEOUT_MS, interval) }),
        pending,
        'console',
      ));
    }
    // `prometheus` (the /metrics reader and listener) is task B7; until then
    // the choice is reported (remove this with B7).
    if (metricExporters.includes('prometheus')) {
      // INFO, not WARN: a temporary development state of the unreleased package, not a configuration problem.
      pending.push(['prometheus metric exporter not implemented yet in Node; metrics are not exposed on /metrics', { signal: 'metrics' }, 'info']);
    }
    const logsIn = inputs.logs;
    if (logsIn !== undefined) {
      add(out.logProcessors, tryBuild(
        'logs',
        () => logExporter(logsIn),
        (exporter) => new BatchLogRecordProcessor({ exporter, ...blrp, exportTimeoutMillis: logsIn.endpoint.timeoutMs }),
        pending,
      ));
    }
    if (logsExporters.includes('console')) {
      add(out.logProcessors, tryBuild('logs', () => new ConsoleLogRecordExporter(), (exporter) => new BatchLogRecordProcessor({ exporter, ...blrp }), pending, 'console'));
    }
  });
  for (const [message, attrs, level] of pending) {
    if (level === 'info') reportOnce(`pending:${message}`, message, attrs, { origin: 'helper', level: 'info' });
    else report(message, attrs);
  }
  return out;
}

/**
 * The OTLP metric reader's export timeout: the resolved OTLP timeout, capped
 * at the export interval (D-JS14). The warning names the source of the value
 * that caused the cap: the timeout's variable or option when one was set,
 * else the interval's (a short interval below the 10 000 ms default timeout).
 */
export function metricTimeout(
  endpoint: Pick<OtlpEndpoint, 'timeoutMs' | 'timeoutSource'>,
  interval: number,
  intervalSource: string,
  warn: ExporterInput['warn'],
): number {
  const { timeoutMs, timeoutSource } = endpoint;
  if (timeoutMs <= interval) return timeoutMs;
  if (interval < SHORT_READER_TIMEOUT_MS) {
    // D-JS14: an export rarely completes in under a second over the network.
    warn('the metric export timeout capped at a very short interval is below 1000 ms; OTLP metric exports will likely time out', {
      variable: intervalSource,
      value: String(interval),
      using: String(interval),
    });
  }
  if (timeoutSource === 'default') {
    warn('metric export interval is shorter than the default OTLP export timeout; the metric export timeout is capped at the interval (the SDK requires timeout <= interval)', {
      variable: intervalSource,
      value: String(interval),
      timeout: String(timeoutMs),
      using: String(interval),
    });
  } else {
    warn('metric export timeout is capped at the export interval (the SDK requires timeout <= interval)', {
      variable: timeoutSource,
      value: String(timeoutMs),
      using: String(interval),
      interval: intervalSource,
    });
  }
  return interval;
}

/** The resource and the three providers of one setup. */
interface Providers {
  readonly resource: Resource;
  readonly tracerProvider: TracerProvider;
  readonly meterProvider: MeterProvider;
  readonly loggerProvider: LoggerProvider;
}

function buildProviders(plan: Plan, base: Resource, options: SetupOptions): Providers {
  const resource = buildResource(base, plan.resourceAttributes);
  // Instrumentations will be registered before this point (task B6, design
  // D-JS1). The gRPC exporters load @grpc/grpc-js only when a gRPC signal is
  // configured; an app loading grpc-js later is patched by the require hooks.
  const pipelines = buildPipelines(plan);
  return {
    resource,
    tracerProvider: new TracerProvider({
      resource,
      spanLimits: { ...plan.sdk.spanLimits },
      spanProcessors: [...pipelines.spanProcessors, ...(options.spanProcessors ?? [])],
    }),
    meterProvider: new MeterProvider({ resource, readers: pipelines.metricReaders }),
    loggerProvider: new LoggerProvider({
      resource,
      logRecordLimits: { ...plan.sdk.logRecordLimits },
      processors: pipelines.logProcessors,
    }),
  };
}

/** Display names of the providers the helper registers. */
const PROVIDER_NAMES = { trace: 'TracerProvider', metrics: 'MeterProvider', logs: 'LoggerProvider' } as const;

/**
 * Registers a provider as the `type` global unless one is registered already
 * (kept, reported) or the API refuses it (reported). Returns whether the
 * global is the helper's.
 */
function registerProvider(type: keyof typeof PROVIDER_NAMES, signal: Signal, register: () => boolean): boolean {
  const name = PROVIDER_NAMES[type];
  if (registeredGlobal(type) !== undefined) {
    report(`a global ${name} is already registered; the helper does not replace it and its ${signal} are inactive`, { signal });
    return false;
  }
  if (register()) return true;
  report(`the OpenTelemetry API refused the global ${name}; the helper's ${signal} are inactive`, { signal });
  return false;
}

/**
 * Configures OpenTelemetry for this process and returns what it resolved,
 * frozen. Synchronous, so it works from a `--require` preload. Idempotent:
 * while a setup is active, later calls return its result unchanged (first call
 * wins); after `shutdown()` the next call builds everything again.
 *
 * Invalid options throw a `TypeError` naming the option before anything is
 * installed; problems in the environment only warn on the diagnostics sink.
 * When another version of the helper owns the process (design D-JS10), the
 * options are still resolved and returned but nothing is installed, and the
 * per-setup identity warnings are not repeated.
 */
export function setup(options?: SetupOptions): Resolved {
  const validated = validateOptions(options);
  const state = getState();
  const base = defaultResource();
  if (state === null) {
    // Inactive copy (getState() already reported why): resolve, install nothing.
    return resolvePlan(validated, defaultServiceName(base), true).resolved;
  }
  const rt = runtimeOf(state);
  if (rt.active !== undefined && rt.active.shutdown === undefined) {
    if (Object.keys(validated).length > 0) {
      report('setup() already ran; these options are ignored and the first result is returned', undefined, {
        origin: 'helper',
        level: 'info',
      });
    }
    return rt.active.resolved;
  }
  // Resolution first: it can still throw for an invalid code option (code
  // `metricExporters` with `otlp` and no endpoint), before anything is installed.
  const plan = resolvePlan(validated, defaultServiceName(base));
  // A registry of another API version refuses every registration (the API
  // compares versions), so the helper installs no global into it.
  const foreignApi = foreignRegistry();
  // Before anything is built, so SDK messages from it are captured.
  const ownsDiag = helperOwnsDiag(rt);
  if (foreignApi !== undefined) {
    report('the OpenTelemetry API globals belong to another @opentelemetry/api version; the helper registers no global and its telemetry is inactive', {
      version: truncateValue(foreignApi.version),
      required: foreignApi.required,
    });
    rt.diagLogger = undefined;
  } else if (validated.installDiagLogger === false) {
    // Opt-out on a re-setup: remove the logger an earlier setup installed.
    if (ownsDiag) diag.disable();
    rt.diagLogger = undefined;
  } else {
    installDiagLogger(ownsDiag);
    rt.diagLogger = registeredGlobal('diag');
  }
  if (plan.resolved.sdkDisabled) {
    // The same INFO line in every language (contract D1 row "OTEL_SDK_DISABLED INFO", A19);
    // the propagators are task B5, so only the context manager is installed until then.
    report('OTEL_SDK_DISABLED=true: telemetry disabled, only propagators installed (Node: the context manager until task B5)', undefined, {
      origin: 'helper',
      level: 'info',
    });
  }
  const built = plan.resolved.sdkDisabled ? undefined : buildProviders(plan, base, validated);
  if (rt.active !== undefined) releaseGlobals(rt.active);
  // A global registered by someone else is kept; checking first avoids the
  // API's duplicate-registration error (with a stack trace) on `diag`.
  let contextManager: AsyncLocalStorageContextManager | undefined;
  if (foreignApi !== undefined) {
    // Reported above.
  } else if (registeredGlobal('context') === undefined) {
    contextManager = new AsyncLocalStorageContextManager().enable();
    if (!context.setGlobalContextManager(contextManager)) {
      contextManager.disable();
      contextManager = undefined;
      report('the OpenTelemetry API refused the global context manager; in-process context is not propagated');
    }
  } else {
    report('a global context manager is already registered; the helper keeps it');
  }
  let ownsTracerProvider = false;
  let ownsMeterProvider = false;
  let ownsLoggerProvider = false;
  // OTEL_SDK_DISABLED=true: no provider is registered, so the API's no-op
  // providers stay; the context manager above still propagates in-process
  // context (spec JS1; the propagators are task B5).
  if (foreignApi === undefined && built !== undefined) {
    const { tracerProvider, meterProvider, loggerProvider } = built;
    ownsTracerProvider = registerProvider('trace', 'traces', () => trace.setGlobalTracerProvider(tracerProvider));
    ownsMeterProvider = registerProvider('metrics', 'metrics', () => metrics.setGlobalMeterProvider(meterProvider));
    ownsLoggerProvider = registerProvider('logs', 'logs', () => logs.setGlobalLoggerProvider(loggerProvider));
  }
  rt.active = {
    resolved: plan.resolved,
    resource: built?.resource,
    tracerProvider: built?.tracerProvider,
    meterProvider: built?.meterProvider,
    loggerProvider: built?.loggerProvider,
    ownsTracerProvider,
    ownsMeterProvider,
    ownsLoggerProvider,
    contextManager,
    shutdown: undefined,
  };
  return plan.resolved;
}

// Unregisters what an earlier setup registered, so the next one can.
function releaseGlobals(active: Active): void {
  if (active.ownsTracerProvider) trace.disable();
  if (active.ownsMeterProvider) metrics.disable();
  if (active.ownsLoggerProvider) logs.disable();
  if (active.contextManager !== undefined) context.disable();
}

interface NamedProvider {
  readonly name: string;
  readonly provider: { shutdown(): Promise<void> };
}

/** The providers a setup built (none under `OTEL_SDK_DISABLED=true`). */
function providersOf(active: Active): readonly NamedProvider[] {
  const all = [
    { name: PROVIDER_NAMES.trace, provider: active.tracerProvider },
    { name: PROVIDER_NAMES.metrics, provider: active.meterProvider },
    { name: PROVIDER_NAMES.logs, provider: active.loggerProvider },
  ];
  return all.flatMap(({ name, provider }) => (provider === undefined ? [] : [{ name, provider }]));
}

function validTimeout(timeoutMs: unknown): timeoutMs is number {
  return typeof timeoutMs === 'number' && Number.isFinite(timeoutMs) && timeoutMs > 0;
}

/**
 * Flushes and shuts down what `setup()` built, under one deadline (default
 * 30 000 ms, clamped to 2^31-1 ms). The first call's promise is cached: later
 * calls return it. It rejects with an `AggregateError` naming the providers
 * that failed and the first reason when a provider failed
 * (also synchronously) and with an `Error` when the deadline passed. The
 * deadline timer keeps the process alive until it settles, so a hung export
 * is reported instead of the process exiting silently. Without an active
 * setup it resolves immediately.
 */
export function shutdown(timeoutMs: number = DEFAULT_SHUTDOWN_TIMEOUT_MS): Promise<void> {
  if (!validTimeout(timeoutMs)) {
    return Promise.reject(new TypeError(`shutdown(): timeoutMs must be a positive number, got ${String(timeoutMs)}`));
  }
  const state = getState();
  const active = state === null ? undefined : runtimeOf(state).active;
  if (active === undefined) return Promise.resolve();
  active.shutdown ??= runShutdown(active, Math.min(timeoutMs, MAX_TIMER_MS));
  return active.shutdown;
}

async function runShutdown(active: Active, timeoutMs: number): Promise<void> {
  let timer: NodeJS.Timeout | undefined;
  // Ref'd on purpose (amended D-JS10): an unref'd timer lets a process whose
  // only pending work is a hung provider exit without the rejection.
  const deadline = new Promise<'timeout'>((resolve) => {
    timer = setTimeout(() => {
      resolve('timeout');
    }, timeoutMs);
  });
  // Promise.resolve().then(): a provider that throws synchronously still lands in allSettled.
  const named = providersOf(active);
  const providers = named.map(({ provider }) => Promise.resolve().then(() => provider.shutdown()));
  try {
    const outcome = await Promise.race([Promise.allSettled(providers), deadline]);
    if (outcome === 'timeout') throw new Error(`shutdown(): providers did not finish within ${String(timeoutMs)} ms`);
    const failed = outcome.flatMap((r, i) => (r.status === 'rejected' ? [{ name: named[i]?.name ?? 'provider', reason: r.reason as unknown }] : []));
    const first = failed[0];
    if (first !== undefined) {
      // Exporter errors carry the redacted endpoint at most; cut anyway.
      const why = truncateValue(first.reason instanceof Error ? first.reason.message : String(first.reason));
      throw new AggregateError(
        failed.map((f) => f.reason),
        `shutdown(): ${failed.map((f) => f.name).join(', ')} failed to shut down (first: ${why})`,
      );
    }
  } finally {
    clearTimeout(timer);
  }
}

/** The resource of the installed TracerProvider (tests only, through `/testing`); `undefined` under `OTEL_SDK_DISABLED=true`. */
export function getResource(): Resource | undefined {
  const state = getState();
  return state === null ? undefined : runtimeOf(state).active?.resource;
}

/**
 * Test-only: forgets every setup, unregisters what the helper registered
 * (globals it owns, its DiagLogger), clears the "once" diagnostics and
 * restores the stderr sink. Providers not shut down yet are shut down in the
 * background.
 */
export function resetForTests(): void {
  const state = getState();
  if (state !== null) {
    const rt = runtimeOf(state);
    const active = rt.active;
    if (active !== undefined) {
      releaseGlobals(active);
      if (active.shutdown === undefined) {
        for (const { provider } of providersOf(active)) void Promise.resolve().then(() => provider.shutdown()).catch(() => undefined);
      }
      rt.active = undefined;
    }
    if (helperOwnsDiag(rt)) diag.disable();
    rt.diagLogger = undefined;
  }
  clearReportedOnce();
  setDiagnosticSink(undefined);
}
