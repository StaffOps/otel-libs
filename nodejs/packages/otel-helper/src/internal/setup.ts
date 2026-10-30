/**
 * `setup()` / `shutdown()` (spec JS2, JS5, JS7; design D-JS1, D-JS10). Task B3
 * builds the resource, the context manager and the DiagLogger; task B4b the
 * OTLP exporters and the three providers (batch span processor, periodic
 * metric reader, batch log record processor). The full lifecycle — rollback, foreign globals,
 * re-binding instrumentations, the stable forwarding providers of D-JS10 — is
 * task B9; the shape below (one runtime record on the shared state, rebuilt
 * after shutdown) is what it extends.
 */
import { context, diag, logs, metrics, trace, type Attributes } from '@opentelemetry/api';
import { AsyncLocalStorageContextManager } from '@opentelemetry/context-async-hooks';
import { defaultResource, resourceFromAttributes, type Resource } from '@opentelemetry/resources';
import { BatchLogRecordProcessor, LoggerProvider, type LogRecordProcessor } from '@opentelemetry/sdk-logs';
import { MeterProvider, PeriodicExportingMetricReader, type IMetricReader } from '@opentelemetry/sdk-metrics';
import { BatchSpanProcessor, TracerProvider, type SpanProcessor } from '@opentelemetry/sdk-trace';
import { clearReportedOnce, report, truncateValue } from './diagnostics';
import { installDiagLogger } from './diag-logger';
import { logExporter, metricExporter, spanExporter, tlsMaterial, withMaskedEnv, type ExporterInput, type FileCache } from './exporters';
import { foreignRegistry, registeredGlobal } from './globals';
import type { Signal } from './otlp-settings';
import { ATTR_SERVICE_NAME, TELEMETRY_SDK_KEYS, resolvePlan, validateOptions, type Plan, type Resolved, type SetupOptions } from './resolve';
import { getState, setDiagnosticSink, type HelperState } from './state';

/** What one successful `setup()` built. */
interface Active {
  readonly resolved: Resolved;
  readonly resource: Resource;
  readonly tracerProvider: TracerProvider;
  readonly meterProvider: MeterProvider;
  readonly loggerProvider: LoggerProvider;
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
/** Metric export interval until task B4c resolves `OTEL_METRIC_EXPORT_INTERVAL` (contract default). */
const DEFAULT_EXPORT_INTERVAL_MS = 30_000;
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
export type Pending = readonly [string, Readonly<Record<string, string | number | boolean>>];

/**
 * Builds one signal's pipeline (exporter, then its processor or reader). A
 * failure (a bug or an SDK change: every value was validated) is reported and
 * the signal is not exported, never thrown from `setup()` (R8); an exporter
 * already built is shut down.
 */
export function tryBuild<E extends { shutdown(): Promise<void> }, P>(
  signal: Signal,
  exporter: () => E,
  pipeline: (e: E) => P,
  pending: Pending[],
): P | undefined {
  let built: E | undefined;
  try {
    built = exporter();
    return pipeline(built);
  } catch (err) {
    if (built !== undefined) void built.shutdown().catch(() => undefined);
    pending.push([
      'the OTLP exporter could not be built; this signal is not exported',
      { signal, error: truncateValue(err instanceof Error ? err.message : String(err)) },
    ]);
    return undefined;
  }
}

/**
 * The OTLP exporters, processors and metric reader of every configured
 * signal, built with the helper-owned OTLP variables hidden (spec JS2). The
 * resolved OTLP timeout of a signal is its single export deadline (contract
 * D1 ruling "Export deadline", A18): the batch processors get it; the metric
 * reader gets it capped at the export interval (the SDK requires timeout <=
 * interval), with a warning naming where the timeout came from. The
 * diagnostics are written after the environment is restored, so a custom
 * sink never runs while it is masked.
 */
function buildPipelines(plan: Plan): Pipelines {
  const { otlp } = plan.resolved;
  const cache: FileCache = new Map();
  const pending: Pending[] = [];
  const seen = new Set<string>();
  const warnOnce: ExporterInput['warn'] = (message, attrs) => {
    const key = `${message}\0${String(attrs.variable)}\0${String(attrs.value)}`;
    if (seen.has(key)) return;
    seen.add(key);
    pending.push([message, attrs]);
  };
  const input = (signal: Signal): ExporterInput | undefined => {
    const endpoint = otlp[signal];
    if (endpoint === undefined) return undefined;
    // TLS files are read before masking: their variables are masked too.
    const tls = endpoint.insecure ? undefined : tlsMaterial(signal, cache, warnOnce);
    return { signal, endpoint, headers: plan.otlpHeaders[signal], tls, warn: warnOnce };
  };
  const inputs = { traces: input('traces'), metrics: input('metrics'), logs: input('logs') };
  const out: Pipelines = { spanProcessors: [], metricReaders: [], logProcessors: [] };
  withMaskedEnv(() => {
    const traces = inputs.traces;
    if (traces !== undefined) {
      const { bsp } = plan.sdk;
      const processor = tryBuild(
        'traces',
        () => spanExporter(traces),
        (exporter) => new BatchSpanProcessor({ exporter, ...bsp, exportTimeoutMillis: traces.endpoint.timeoutMs }),
        pending,
      );
      if (processor !== undefined) out.spanProcessors.push(processor);
    }
    const metricsIn = inputs.metrics;
    if (metricsIn !== undefined) {
      const interval = DEFAULT_EXPORT_INTERVAL_MS;
      const { timeoutMs, timeoutSource } = metricsIn.endpoint;
      const timeout = Math.min(timeoutMs, interval);
      if (timeout < timeoutMs) {
        warnOnce('metric export timeout is capped at the export interval (the SDK requires timeout <= interval)', {
          variable: timeoutSource,
          value: String(timeoutMs),
          using: String(timeout),
        });
      }
      const reader = tryBuild(
        'metrics',
        () => metricExporter(metricsIn),
        (exporter) => new PeriodicExportingMetricReader({ exporter, exportIntervalMillis: interval, exportTimeoutMillis: timeout }),
        pending,
      );
      if (reader !== undefined) out.metricReaders.push(reader);
    }
    const logsIn = inputs.logs;
    if (logsIn !== undefined) {
      const { blrp } = plan.sdk;
      const processor = tryBuild(
        'logs',
        () => logExporter(logsIn),
        (exporter) => new BatchLogRecordProcessor({ exporter, ...blrp, exportTimeoutMillis: logsIn.endpoint.timeoutMs }),
        pending,
      );
      if (processor !== undefined) out.logProcessors.push(processor);
    }
  });
  for (const [message, attrs] of pending) report(message, attrs);
  return out;
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
  // A registry of another API version refuses every registration (the API
  // compares versions), so the helper installs no global into it.
  const foreignApi = foreignRegistry();
  // Before resolution, so SDK messages from everything built below are captured.
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
  const plan = resolvePlan(validated, defaultServiceName(base));
  const resource = buildResource(base, plan.resourceAttributes);
  // Instrumentations will be registered before this point (task B6, design
  // D-JS1). The gRPC exporters load @grpc/grpc-js only when a gRPC signal is
  // configured; an app loading grpc-js later is patched by the require hooks.
  const pipelines = buildPipelines(plan);
  const tracerProvider = new TracerProvider({
    resource,
    spanLimits: { ...plan.sdk.spanLimits },
    spanProcessors: [...pipelines.spanProcessors, ...(validated.spanProcessors ?? [])],
  });
  const meterProvider = new MeterProvider({ resource, readers: pipelines.metricReaders });
  const loggerProvider = new LoggerProvider({
    resource,
    logRecordLimits: { ...plan.sdk.logRecordLimits },
    processors: pipelines.logProcessors,
  });
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
  if (foreignApi === undefined) {
    ownsTracerProvider = registerProvider('trace', 'traces', () => trace.setGlobalTracerProvider(tracerProvider));
    ownsMeterProvider = registerProvider('metrics', 'metrics', () => metrics.setGlobalMeterProvider(meterProvider));
    ownsLoggerProvider = registerProvider('logs', 'logs', () => logs.setGlobalLoggerProvider(loggerProvider));
  }
  rt.active = {
    resolved: plan.resolved,
    resource,
    tracerProvider,
    meterProvider,
    loggerProvider,
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

function providersOf(active: Active): readonly { shutdown(): Promise<void> }[] {
  return [active.tracerProvider, active.meterProvider, active.loggerProvider];
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
  const providers = providersOf(active).map((p) => Promise.resolve().then(() => p.shutdown()));
  try {
    const outcome = await Promise.race([Promise.allSettled(providers), deadline]);
    if (outcome === 'timeout') throw new Error(`shutdown(): providers did not finish within ${String(timeoutMs)} ms`);
    const names = [PROVIDER_NAMES.trace, PROVIDER_NAMES.metrics, PROVIDER_NAMES.logs];
    const failed = outcome.flatMap((r, i) => (r.status === 'rejected' ? [{ name: names[i] ?? 'provider', reason: r.reason as unknown }] : []));
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

/** The resource of the installed TracerProvider (tests only, through `/testing`). */
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
        for (const p of providersOf(active)) void Promise.resolve().then(() => p.shutdown()).catch(() => undefined);
      }
      rt.active = undefined;
    }
    if (helperOwnsDiag(rt)) diag.disable();
    rt.diagLogger = undefined;
  }
  clearReportedOnce();
  setDiagnosticSink(undefined);
}
