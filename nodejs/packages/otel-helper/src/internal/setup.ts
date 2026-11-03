/**
 * `setup()` / `shutdown()` (spec JS2, JS5, JS7; design D-JS1, D-JS10). Task B3
 * builds the resource, the context manager and the DiagLogger; task B4b the
 * OTLP exporters and the three providers (batch span processor, periodic
 * metric reader, batch log record processor); task B4c the exporter lists
 * (console exporters), `OTEL_SDK_DISABLED` and the export interval; task B5
 * the sampler (with the health sampler around it), the propagators and the
 * debug marker; task B7 the Prometheus reader, the `/metrics` listener and
 * `metricsHandler()`; task B6 the core instrumentations (http, undici,
 * express, gRPC); task B9 the lifecycle: the stable forwarding providers and
 * propagator of D-JS10 (registered once per process, their delegates swapped
 * by every setup and shutdown), foreign globals and `replaceGlobals`, and the
 * rollback of a failed setup (Go G1); task B10a the opt-in `instrumentations`.
 */
import cluster from 'node:cluster';
import type * as http from 'node:http';
import { isMainThread } from 'node:worker_threads';
import { context, diag, DiagLogLevel, logs, metrics, propagation, trace, type Attributes, type DiagLogger, type MeterProvider as MeterProviderApi, type TextMapPropagator, type TracerProvider as TracerProviderApi } from '@opentelemetry/api';
import type { PrometheusExporter } from '@opentelemetry/exporter-prometheus';
import { AsyncLocalStorageContextManager } from '@opentelemetry/context-async-hooks';
import { defaultResource, resourceFromAttributes, type Resource } from '@opentelemetry/resources';
import { BatchLogRecordProcessor, ConsoleLogRecordExporter, LoggerProvider, type LogRecordProcessor } from '@opentelemetry/sdk-logs';
import { ConsoleMetricExporter, MeterProvider, PeriodicExportingMetricReader, type IMetricReader } from '@opentelemetry/sdk-metrics';
import { BatchSpanProcessor, ConsoleSpanExporter, TracerProvider, type SpanProcessor } from '@opentelemetry/sdk-trace';
import { clearReportedOnce, redactErrorText, report, reportOnce, truncateValue } from './diagnostics';
import { installDiagLogger } from './diag-logger';
import { logExporter, metricExporter, spanExporter, tlsMaterial, withMaskedEnv, type ExporterInput, type FileCache } from './exporters';
import { createForwarders, type Forwarders } from './forwarding';
import { foreignRegistry, registeredGlobal, removeGlobal, type ApiGlobal } from './globals';
import { entryIsEsm, hooksMode } from './hooks';
import type { LogLevel } from './log-level';
import type { Instrumentation } from '@opentelemetry/instrumentation';
import { bindProviders, clientExclusions, createInstrumentations, disableInstrumentations, disableOptIn, enableInstrumentations, enableOptIn, loadedPackages, otlpTargets, type HelperInstrumentations } from './instrumentation';
import { closeListener, createPrometheusReader, dropPredicate, droppingExporter, ENV_METRICS_PORT, serveMetrics, startListener, type DropPredicate, type ScrapeRequest, type ScrapeResponse, type ScrapeTarget } from './metrics-endpoint';
import type { OtlpEndpoint } from './otlp';
import type { Signal } from './otlp-settings';
import { ATTR_SERVICE_NAME, TELEMETRY_SDK_KEYS, resolvePlan, validateOptions, type Plan, type Resolved, type SetupOptions } from './resolve';
import { getState, setDiagnosticSink, type HelperState } from './state';
import { buildPropagator, buildSampler, DebugMarkerProcessor, GrpcServerStatusProcessor, HealthCheckSampler } from './tracing';

/** What one successful `setup()` built. */
interface Active {
  readonly resolved: Resolved;
  /** `undefined` under `OTEL_SDK_DISABLED=true` (no providers are built). */
  readonly resource: Resource | undefined;
  readonly tracerProvider: TracerProvider | undefined;
  readonly meterProvider: MeterProvider | undefined;
  readonly loggerProvider: LoggerProvider | undefined;
  /**
   * Whether the global TracerProvider is the helper's forwarding provider
   * (false: a foreign one was registered first, or the API refused it); while
   * true it forwards to `tracerProvider`.
   */
  readonly ownsTracerProvider: boolean;
  readonly ownsMeterProvider: boolean;
  readonly ownsLoggerProvider: boolean;
  /**
   * Whether the helper's LoggerProvider has an export processor (OTLP and/or
   * console) and is usable: the helper's loggers write through it, else to
   * stdout (design D6). Independent of who owns the global LoggerProvider
   * (Go and Python: the helper's handler keeps exporting through its own).
   */
  readonly logsExported: boolean;
  /** The Prometheus reader; `undefined` when `prometheus` is not an effective metric exporter or it failed to build. */
  readonly prometheus: PrometheusExporter | undefined;
  /** Prometheus deliberately off (`OTEL_SDK_DISABLED`, or no `prometheus` metric exporter): `/metrics` answers 200, empty. */
  readonly prometheusInactive: boolean;
  /** The helper's `/metrics` listener; `undefined` with port 0 or without a scrapable reader. */
  readonly listener: http.Server | undefined;
  /** Disabled metrics (A21); `undefined` = none. */
  readonly drop: DropPredicate | undefined;
  /** Whether this setup enabled the instrumentations (disabled again by `shutdown()`). */
  readonly instrumented: boolean;
  /** The opt-in instrumentations this setup enabled (`setup({ instrumentations })`; disabled by `shutdown()`). */
  readonly optIn: readonly Instrumentation[];
  /** `register`: built by `@staffops/otel-helper/register`; later code options are then warned about. */
  readonly origin: SetupOrigin;
  /** Cached result of the first `shutdown()`. */
  shutdown: Promise<void> | undefined;
}

/** The API globals the helper registers. */
type OwnedGlobal = Exclude<ApiGlobal, 'diag'>;

/** Who called `setup()`: the app, or the `/register` preload. */
type SetupOrigin = 'code' | 'register';

/** Helper runtime kept on the shared state (one per process for this version). */
interface Runtime {
  active: Active | undefined;
  /** The forwarding providers and propagator, built on first use and kept for the process (design D-JS10). */
  forwarders: Forwarders | undefined;
  /**
   * The registry entry the helper registered for each global (for `trace`, the
   * API's proxy holding the forwarding provider): the global is the helper's
   * while the registry still holds this exact entry. Kept across `shutdown()`.
   */
  registered: Partial<Record<OwnedGlobal, unknown>>;
  /** The `diag` registry entry the helper's DiagLogger produced; `undefined` = not installed. */
  diagLogger: unknown;
  /**
   * The core instrumentations, built by the first setup that instruments and
   * kept for the process: every later setup re-binds them (design D-JS7).
   * `undefined` = not built yet, or the last build failed (retried by the
   * next setup; each failure is reported).
   */
  instrumentations: HelperInstrumentations | undefined;
  /** The opt-in instrumentations enabled now (for `/testing` `reset()`, which disables them all); `shutdown()` removes its own. */
  optIn: Set<Instrumentation>;
  /**
   * The log level the last successful setup resolved (R4); `undefined` =
   * none yet (`info`). Kept after `shutdown()`, as Go's installed level.
   */
  logLevel: LogLevel | undefined;
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
  state.runtime ??= { active: undefined, forwarders: undefined, registered: {}, diagLogger: undefined, instrumentations: undefined, optIn: new Set(), logLevel: undefined } satisfies Runtime;
  return state.runtime as Runtime;
}

/**
 * The process's instrumentations, built on first use. A build failure (a bug,
 * an incompatible upstream change, a broken API registry) is reported and
 * this setup runs uninstrumented, never thrown from `setup()` (R8); the next
 * setup tries again (and reports again if it fails).
 */
function instrumentationsOf(rt: Runtime): HelperInstrumentations | undefined {
  if (rt.instrumentations !== undefined) return rt.instrumentations;
  try {
    rt.instrumentations = createInstrumentations();
  } catch (err) {
    report('the instrumentations could not be built; http, fetch, express and gRPC are not instrumented', {
      error: truncateValue(err instanceof Error ? err.message : String(err)),
    });
  }
  return rt.instrumentations;
}

/**
 * Binds the instrumentations to these providers and enables them with this
 * plan's client exclusions (its health paths and its OTLP/HTTP endpoints).
 * Returns whether they are enabled: a failure is reported, the modules are
 * unpatched again and the setup carries on (R8).
 */
function tryEnableInstrumentations(
  instrumentations: HelperInstrumentations,
  plan: Plan,
  providers: { readonly tracerProvider: TracerProviderApi; readonly meterProvider: MeterProviderApi },
  loadedBeforeSetup: ReadonlySet<string>,
): boolean {
  const { tracesExporters, metricExporters, logsExporters, otlp, healthPaths } = plan.resolved;
  const targets = otlpTargets(otlp, {
    traces: tracesExporters.includes('otlp'),
    metrics: metricExporters.includes('otlp'),
    logs: logsExporters.includes('otlp'),
  });
  try {
    bindProviders(instrumentations, providers.tracerProvider, providers.meterProvider);
    enableInstrumentations(instrumentations, clientExclusions(healthPaths, targets), loadedBeforeSetup);
    return true;
  } catch (err) {
    disableInstrumentations(instrumentations);
    report('the instrumentations could not be enabled; http, fetch, express and gRPC are not instrumented', {
      error: truncateValue(err instanceof Error ? err.message : String(err)),
    });
    return false;
  }
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
  /** The Prometheus reader, also in `metricReaders`. */
  prometheus: PrometheusExporter | undefined;
}

/** A diagnostic held back until the masked environment is restored. */
export type Pending = readonly [string, Readonly<Record<string, string | number | boolean>>];

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
 * do) and `prometheus` (the reader `/metrics` collects, design D-JS8). The OTLP exporters are built
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
  // Disabled metrics leave every push exporter through this wrapper (A21); the scrape filters itself.
  const drop = dropPredicate(plan.resolved.disabledMetrics);
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
  const out: Pipelines = { spanProcessors: [], metricReaders: [], logProcessors: [], prometheus: undefined };
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
        () => droppingExporter(metricExporter(metricsIn), drop),
        (exporter) => new PeriodicExportingMetricReader({ exporter, exportIntervalMillis: interval, exportTimeoutMillis: timeout }),
        pending,
      ));
    }
    if (metricExporters.includes('console')) {
      add(out.metricReaders, tryBuild(
        'metrics',
        () => droppingExporter(new ConsoleMetricExporter(), drop),
        (exporter) =>
          new PeriodicExportingMetricReader({ exporter, exportIntervalMillis: interval, exportTimeoutMillis: Math.min(CONSOLE_READER_TIMEOUT_MS, interval) }),
        pending,
        'console',
      ));
    }
    if (metricExporters.includes('prometheus')) {
      out.prometheus = tryBuild('metrics', createPrometheusReader, (reader) => reader, pending, 'prometheus');
      add(out.metricReaders, out.prometheus);
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
  for (const [message, attrs] of pending) report(message, attrs);
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
  /** Whether the LoggerProvider has at least one export processor. */
  readonly logsExported: boolean;
  readonly prometheus: PrometheusExporter | undefined;
}

function buildProviders(plan: Plan, base: Resource, options: SetupOptions): Providers {
  const resource = buildResource(base, plan.resourceAttributes);
  // The gRPC exporters load @grpc/grpc-js only when a gRPC signal is
  // configured, before the instrumentations are enabled; an app loading
  // grpc-js later is still patched by the require hooks (spike B1c F6,
  // asserted in test/instrumentation.test.cjs).
  const pipelines = buildPipelines(plan);
  try {
    step('pipelines');
    return providersFrom(plan, options, resource, pipelines);
  } catch (err) {
    // A provider constructor threw: the pipelines are nobody's yet (B9 fix round).
    for (const part of [...pipelines.spanProcessors, ...pipelines.metricReaders, ...pipelines.logProcessors]) shutDownQuietly(part);
    throw err;
  }
}

function providersFrom(plan: Plan, options: SetupOptions, resource: Resource, pipelines: Pipelines): Providers {
  return {
    resource,
    tracerProvider: new TracerProvider({
      resource,
      // Health-check spans are dropped by a wrapper around the whole resolved
      // sampler, outside ParentBased (contract D5, design D-JS7).
      sampler: new HealthCheckSampler(buildSampler(plan.resolved.sampler), plan.resolved.healthPaths),
      spanLimits: { ...plan.sdk.spanLimits },
      // The debug marker first, so later processors see the attribute at start (R9).
      // The gRPC status rule before everything else, so later processors see it (A22).
      spanProcessors: [
        new GrpcServerStatusProcessor(),
        ...(plan.resolved.debug ? [new DebugMarkerProcessor()] : []),
        ...pipelines.spanProcessors,
        ...(options.spanProcessors ?? []),
      ],
    }),
    meterProvider: new MeterProvider({ resource, readers: pipelines.metricReaders }),
    loggerProvider: new LoggerProvider({
      resource,
      logRecordLimits: { ...plan.sdk.logRecordLimits },
      processors: pipelines.logProcessors,
    }),
    logsExported: pipelines.logProcessors.length > 0,
    prometheus: pipelines.prometheus,
  };
}

/** Display names of the providers the helper registers. */
const PROVIDER_NAMES = { trace: 'TracerProvider', metrics: 'MeterProvider', logs: 'LoggerProvider' } as const;

/** Display names of every global the helper registers. */
const GLOBAL_NAMES: Readonly<Record<OwnedGlobal, string>> = { ...PROVIDER_NAMES, context: 'context manager', propagation: 'propagator' };

/** The signal a provider global carries (reported as `signal=`); none for the context manager and the propagator. */
const GLOBAL_SIGNALS: Readonly<Partial<Record<OwnedGlobal, Signal>>> = { trace: 'traces', metrics: 'metrics', logs: 'logs' };

/**
 * What keeping someone else's global means. The helper's loggers keep their
 * own provider (Go, Python): with a foreign LoggerProvider only Logs API
 * callers miss the helper's.
 */
const KEPT: Readonly<Record<OwnedGlobal, string>> = {
  trace: 'the helper does not replace it and its traces are inactive',
  metrics: 'the helper does not replace it and its metrics are inactive',
  logs: "the helper does not replace it: OpenTelemetry Logs API callers use it, the helper's loggers (createLogger) still export through the helper's provider",
  context: 'the helper keeps it',
  propagation: 'the helper keeps it and OTEL_PROPAGATORS is not applied',
};

/** What a refused registration means. */
const REFUSED: Readonly<Record<OwnedGlobal, string>> = {
  trace: "the helper's traces are inactive",
  metrics: "the helper's metrics are inactive",
  logs: "OpenTelemetry Logs API callers get no logs, the helper's loggers (createLogger) still export through the helper's provider",
  context: 'in-process context is not propagated',
  propagation: 'context is not propagated across processes',
};

const DISABLE: Readonly<Record<OwnedGlobal, () => void>> = {
  trace: () => {
    trace.disable();
  },
  metrics: () => {
    metrics.disable();
  },
  logs: () => {
    logs.disable();
  },
  context: () => {
    context.disable();
  },
  propagation: () => {
    propagation.disable();
  },
};

/**
 * Empties the `type` global after a failed setup registered it (no previous
 * entry). For `trace` and `logs` the registry entry is removed directly and
 * the API's own proxy is pointed at nothing, instead of `disable()`, which
 * replaces that proxy: tracers and Logs API loggers the app obtained before
 * `setup()` hold it, and must join the provider the app registers next (B9
 * fix round M3). Falls back to `disable()` when the API's shape differs
 * (re-check on API bumps, B13).
 */
function releaseGlobal(type: OwnedGlobal): void {
  if (type === 'trace') {
    const proxy = registeredGlobal('trace') as { setDelegate?: unknown } | undefined;
    if (typeof proxy?.setDelegate === 'function' && removeGlobal('trace')) {
      Reflect.apply(proxy.setDelegate, proxy, [undefined]);
      return;
    }
  } else if (type === 'logs') {
    const proxy = (logs as unknown as { _proxyLoggerProvider?: { _setDelegate?: unknown } })._proxyLoggerProvider;
    if (typeof proxy?._setDelegate === 'function' && removeGlobal('logs')) {
      Reflect.apply(proxy._setDelegate, proxy, [undefined]);
      return;
    }
  }
  DISABLE[type]();
}

/**
 * Puts `previous` (a registry entry) back as the `type` global, or empties
 * it ({@link releaseGlobal}) when `previous` is `undefined` (always for
 * `context`, never replaced). For `trace` the registry holds the API's
 * proxy: its delegate is registered again (a new proxy, the same provider).
 */
function restoreGlobal(type: OwnedGlobal, previous: unknown): void {
  if (previous === undefined) {
    if (registeredGlobal(type) !== undefined) releaseGlobal(type);
    return;
  }
  if (registeredGlobal(type) !== undefined) DISABLE[type]();
  switch (type) {
    case 'trace': {
      const proxy = previous as { getDelegate?: () => TracerProviderApi };
      trace.setGlobalTracerProvider(typeof proxy.getDelegate === 'function' ? proxy.getDelegate() : (previous as TracerProviderApi));
      break;
    }
    case 'metrics':
      metrics.setGlobalMeterProvider(previous as MeterProviderApi);
      break;
    case 'logs':
      logs.setGlobalLoggerProvider(previous as Parameters<typeof logs.setGlobalLoggerProvider>[0]);
      break;
    // No 'context': a foreign context manager is never replaced, so there is none to put back.
    case 'propagation':
      propagation.setGlobalPropagator(previous as TextMapPropagator);
      break;
  }
}

/** One `setup()` call in progress: what it built, and how to undo what it installed (G1). */
interface Attempt {
  /** `replaceGlobals`: replace foreign providers and the propagator. */
  readonly replace: boolean;
  /** Undo steps, in install order (run in reverse on failure). */
  readonly undo: (() => void)[];
  built: Providers | undefined;
  listener: http.Server | undefined;
  instrumented: boolean;
  optIn: Instrumentation[];
}

/**
 * Makes the helper's instance the `type` global (`register` registers it and
 * returns whether the API accepted it). A global the helper registered
 * earlier is kept as is; someone else's is reported and kept, or replaced
 * under `replaceGlobals` (never the context manager: the app's asynchronous
 * context depends on it, design D-JS14). Returns whether the global is now
 * the helper's. Every change is undone by `attempt.undo` on failure.
 */
function claimGlobal(rt: Runtime, type: OwnedGlobal, register: () => boolean, attempt: Attempt): boolean {
  const name = GLOBAL_NAMES[type];
  const signal = GLOBAL_SIGNALS[type];
  const attrs = signal === undefined ? undefined : { signal };
  const entry = registeredGlobal(type);
  const mine = rt.registered[type];
  if (entry !== undefined && entry === mine) return true;
  // Registered by the helper once, replaced (or removed) by someone since.
  const lost = mine !== undefined;
  rt.registered[type] = undefined;
  let previous: unknown;
  if (entry !== undefined) {
    if (!attempt.replace || type === 'context') {
      const message = `${lost ? `the global ${name} is no longer the helper's` : `a global ${name} is already registered`}; ${KEPT[type]}`;
      // Under replaceGlobals the kept context manager is worth saying once, not on every setup.
      if (attempt.replace) reportOnce('replace-globals:context', `${message}; replaceGlobals never replaces the context manager`);
      else report(message, attrs);
      return false;
    }
    previous = entry;
    report(`a global ${name} is already registered; replaceGlobals is set: the helper replaces it`, attrs, { origin: 'helper', level: 'info' });
  }
  // Set once the registration succeeded; read by the undo step below.
  const ours: { entry: unknown } = { entry: undefined };
  // Pushed before anything changes (B9 fix round M4): a throw from disable()
  // or the registration is undone too. It puts `previous` back unless the
  // global already is `previous` again or someone else's.
  attempt.undo.push(() => {
    const now = registeredGlobal(type);
    if (now !== previous && (now === undefined || now === ours.entry)) restoreGlobal(type, previous);
    rt.registered[type] = undefined;
  });
  if (previous !== undefined) DISABLE[type]();
  if (!register()) {
    if (previous !== undefined) restoreGlobal(type, previous);
    report(`the OpenTelemetry API refused the global ${name}; ${REFUSED[type]}`, attrs);
    return false;
  }
  ours.entry = registeredGlobal(type);
  rt.registered[type] = ours.entry;
  return true;
}

/** A forwarder's delegate slot. */
interface DelegateSlot<P> {
  getDelegate(): P | undefined;
  setDelegate(next: P | undefined): void;
}

/** Points a forwarder at `next`; the previous delegate comes back on failure. */
function forwardTo<P>(slot: DelegateSlot<P>, next: P | undefined, attempt: Attempt): void {
  const previous = slot.getDelegate();
  slot.setDelegate(next);
  attempt.undo.push(() => {
    slot.setDelegate(previous);
  });
}

/** Puts the DiagLogger registered before a failed setup back. */
function restoreDiag(previous: unknown): void {
  if (registeredGlobal('diag') === previous) return;
  if (previous === undefined) diag.disable();
  // The registry holds a level-filtered logger: ALL keeps its own filter.
  else diag.setLogger(previous as DiagLogger, { logLevel: DiagLogLevel.ALL, suppressOverrideMessage: true });
}

/**
 * @internal Rollback test seam (test/setup.test.cjs): called after each
 * install step with its name; a throw fails the setup at that step. Not part
 * of the API (only reachable through `dist/internal/setup.js`).
 */
export const setupFault: { at: ((step: string) => void) | undefined } = { at: undefined };

function step(name: string): void {
  setupFault.at?.(name);
}

const ALL_SIGNALS: readonly Signal[] = Object.freeze(['traces', 'metrics', 'logs']);

/** `resolved` with the signals the helper could not activate (JS7). */
function withInactiveSignals(resolved: Resolved, inactiveSignals: readonly Signal[]): Resolved {
  return Object.freeze({ ...resolved, inactiveSignals: Object.freeze([...inactiveSignals]) });
}

/**
 * Opt-in contrib instrumentations that patch a package's entry file, which an
 * ES module `import` loads without require(): without the loader hooks they
 * miss it, like express and grpc-js (B10b fix round, 2026-11-03).
 */
const ESM_ENTRY_PATCHED: Readonly<Record<string, string>> = Object.freeze({
  '@opentelemetry/instrumentation-ioredis': 'ioredis',
});

/**
 * The JS5 warning, naming the packages imported after setup() that are not
 * instrumented: the core's, plus those of the opt-in instrumentations passed
 * (their own name or `coveredInstrumentations`).
 *
 * @internal
 */
export function esmWithoutHooksMessage(optIn: readonly Instrumentation[]): string {
  const packages = ['express', '@grpc/grpc-js'];
  for (const instrumentation of optIn) {
    const covered: unknown = (instrumentation as { coveredInstrumentations?: unknown }).coveredInstrumentations;
    const names: unknown[] = [instrumentation.instrumentationName, ...(Array.isArray(covered) ? (covered as unknown[]) : [])];
    for (const name of names) {
      const pkg = typeof name === 'string' ? ESM_ENTRY_PATCHED[name] : undefined;
      if (pkg !== undefined && !packages.includes(pkg)) packages.push(pkg);
    }
  }
  return `setup() runs in an ES module process without the module hooks; packages imported after setup() (${packages.join(', ')}) are not instrumented — start the process with --import @staffops/otel-helper/register, or import @staffops/otel-helper/hooks first in the preload that calls setup()`;
}

/**
 * Configures OpenTelemetry for this process and returns what it resolved,
 * frozen. Synchronous, so it works from a `--require` preload. Idempotent:
 * while a setup is active, later calls return its result unchanged (first call
 * wins); after `shutdown()` the next call builds everything again and points
 * the helper's globals at it (design D-JS10), so tracers, meters and loggers
 * obtained earlier keep working.
 *
 * Invalid options throw a `TypeError` naming the option before anything is
 * installed; problems in the environment only warn on the diagnostics sink.
 * A global registered by someone else is kept, reported, and its signal
 * listed in `Resolved.inactiveSignals`, unless `replaceGlobals` is set (JS7).
 * Any other failure (a bug, an SDK change) releases everything this call
 * built, leaves the globals as they were and is thrown (Go G1); a later call
 * can try again. When another version of the helper owns the process (design
 * D-JS10), the options are still resolved and returned but nothing is
 * installed, and the per-setup identity warnings are not repeated.
 */
export function setup(options?: SetupOptions): Resolved {
  return setupFrom('code', options);
}

/** @internal `setup()` from the environment, for `@staffops/otel-helper/register`. */
export function setupFromRegister(): Resolved {
  return setupFrom('register', undefined);
}

function setupFrom(origin: SetupOrigin, options: SetupOptions | undefined): Resolved {
  const validated = validateOptions(options);
  const state = getState();
  const base = defaultResource();
  if (state === null) {
    // Inactive copy (getState() already reported why): resolve, install nothing;
    // an opt-in instrumentation its constructor enabled is turned off.
    disableOptIn(validated.instrumentations ?? []);
    return withInactiveSignals(resolvePlan(validated, defaultServiceName(base), true).resolved, ALL_SIGNALS);
  }
  const rt = runtimeOf(state);
  if (rt.active !== undefined && rt.active.shutdown === undefined) {
    // Opt-in instrumentations of this ignored call that the active setup does
    // not use are turned off (their constructor may have enabled them) and named.
    const activeOptIn = rt.active.optIn;
    const ignored = (validated.instrumentations ?? []).filter((i) => !activeOptIn.includes(i));
    disableOptIn(ignored);
    const ignoredAttrs = ignored.length > 0 ? { instrumentations: truncateValue(ignored.map((i) => i.instrumentationName).join(',')) } : undefined;
    if (Object.keys(validated).length > 0) {
      if (rt.active.origin === 'register') {
        // The app expects its options to apply: they silently would not (B9 fix round).
        report('setup() already ran from @staffops/otel-helper/register, so these options are ignored; to pass options in code, preload your own file (import @staffops/otel-helper/hooks, then setup({...})) instead of /register', ignoredAttrs);
      } else {
        report('setup() already ran; these options are ignored and the first result is returned', ignoredAttrs, {
          origin: 'helper',
          level: 'info',
        });
      }
    }
    return rt.active.resolved;
  }
  // Resolution first: it can still throw for an invalid code option (code
  // `metricExporters` with `otlp` and no endpoint), before anything is installed.
  const plan = resolvePlan(validated, defaultServiceName(base));
  const attempt: Attempt = { replace: validated.replaceGlobals === true, undo: [], built: undefined, listener: undefined, instrumented: false, optIn: [] };
  let active: Active;
  try {
    active = install(rt, plan, validated, base, attempt, origin);
  } catch (err) {
    rollback(rt, attempt, validated.instrumentations ?? []);
    throw err;
  }
  rt.active = active;
  rt.logLevel = plan.resolved.logLevel;
  return active.resolved;
}

/** Releases what a failed setup built and installed (Go G1); `rt.active` was never replaced. */
function rollback(rt: Runtime, attempt: Attempt, optIn: readonly Instrumentation[]): void {
  if (attempt.instrumented) disableInstrumentations(rt.instrumentations);
  // Every opt-in one of this call, also when the failure came before the
  // instrumentations step (a constructor may have enabled it).
  disableOptIn(optIn);
  for (const instrumentation of optIn) rt.optIn.delete(instrumentation);
  void closeListener(attempt.listener);
  for (const undo of attempt.undo.reverse()) {
    try {
      undo();
    } catch (err) {
      report('setup() rollback: a step could not be undone', { error: redactErrorText(err instanceof Error ? err.message : String(err)) });
    }
  }
  const built = attempt.built;
  if (built !== undefined) {
    for (const provider of [built.tracerProvider, built.meterProvider, built.loggerProvider]) shutDownQuietly(provider);
  }
}

/** Shuts `target` down in the background, ignoring its errors (rollback, reset). */
function shutDownQuietly(target: { shutdown(): Promise<void> }): void {
  void Promise.resolve().then(() => target.shutdown()).catch(() => undefined);
}

/** The body of `setup()`: builds, then installs, recording every change in `attempt`. */
function install(rt: Runtime, plan: Plan, options: SetupOptions, base: Resource, attempt: Attempt, origin: SetupOrigin): Active {
  // A registry of another API version refuses every registration (the API
  // compares versions), so the helper installs no global into it.
  const foreignApi = foreignRegistry();
  // Before anything is built, so SDK messages from it are captured.
  const ownsDiag = helperOwnsDiag(rt);
  const diagBefore = registeredGlobal('diag');
  const diagLoggerBefore = rt.diagLogger;
  attempt.undo.push(() => {
    restoreDiag(diagBefore);
    rt.diagLogger = diagLoggerBefore === diagBefore ? registeredGlobal('diag') : diagLoggerBefore;
  });
  if (foreignApi !== undefined) {
    report('the OpenTelemetry API globals belong to another @opentelemetry/api version; the helper registers no global and its telemetry is inactive', {
      version: truncateValue(foreignApi.version),
      required: foreignApi.required,
    });
    rt.diagLogger = undefined;
  } else if (options.installDiagLogger === false) {
    // Opt-out on a re-setup: remove the logger an earlier setup installed.
    if (ownsDiag) diag.disable();
    rt.diagLogger = undefined;
  } else {
    installDiagLogger(ownsDiag);
    rt.diagLogger = registeredGlobal('diag');
  }
  step('diag');
  const { sdkDisabled } = plan.resolved;
  // Before the exporters load grpc-js: what the app had loaded itself.
  const loadedBeforeSetup = sdkDisabled ? new Set<string>() : loadedPackages();
  const built = sdkDisabled ? undefined : buildProviders(plan, base, options);
  attempt.built = built;
  step('built');
  const fw = (rt.forwarders ??= createForwarders());
  // The context manager and the propagator are installed under
  // OTEL_SDK_DISABLED=true too (spec JS1; contract D1 row "OTEL_SDK_DISABLED",
  // case propagation.sdk_disabled_propagators_kept). A global registered by
  // someone else is checked first, which avoids the API's duplicate-
  // registration error (with a stack trace) on `diag`.
  let ownsPropagator = false;
  if (foreignApi === undefined) {
    claimGlobal(rt, 'context', () => {
      const manager = new AsyncLocalStorageContextManager().enable();
      if (context.setGlobalContextManager(manager)) return true;
      manager.disable();
      return false;
    }, attempt);
    step('context');
    // A propagator registered first is kept unless replaceGlobals: the 3.0
    // API refuses a second registration (Go and Python replace it; D-JS14).
    ownsPropagator = claimGlobal(rt, 'propagation', () => propagation.setGlobalPropagator(fw.propagator), attempt);
    if (ownsPropagator) forwardTo(fw.propagator, buildPropagator(plan.resolved.propagators), attempt);
    step('propagator');
  }
  if (sdkDisabled) {
    // The same INFO line in every language (contract D1 row "OTEL_SDK_DISABLED INFO", A19),
    // only when the propagator was installed; Node also keeps its context manager (spec JS1).
    const message = ownsPropagator
      ? 'OTEL_SDK_DISABLED=true: telemetry disabled, only propagators installed'
      : 'OTEL_SDK_DISABLED=true: telemetry disabled; the helper installed no propagator';
    report(message, undefined, { origin: 'helper', level: 'info' });
  }
  let ownsTracerProvider = false;
  let ownsMeterProvider = false;
  let ownsLoggerProvider = false;
  // OTEL_SDK_DISABLED=true: no provider is registered and the forwarders keep
  // no delegate, so every global stays a no-op; the context manager and the
  // propagators above still propagate context (spec JS1).
  if (foreignApi === undefined && built !== undefined) {
    ownsTracerProvider = claimGlobal(rt, 'trace', () => trace.setGlobalTracerProvider(fw.tracerProvider), attempt);
    ownsMeterProvider = claimGlobal(rt, 'metrics', () => metrics.setGlobalMeterProvider(fw.meterProvider), attempt);
    ownsLoggerProvider = claimGlobal(rt, 'logs', () => logs.setGlobalLoggerProvider(fw.loggerProvider), attempt);
    // A forwarder forwards only while it is the global: a foreign provider's signal is inactive.
    forwardTo(fw.tracerProvider, ownsTracerProvider ? built.tracerProvider : undefined, attempt);
    forwardTo(fw.meterProvider, ownsMeterProvider ? built.meterProvider : undefined, attempt);
    forwardTo(fw.loggerProvider, ownsLoggerProvider ? built.loggerProvider : undefined, attempt);
  }
  step('providers');
  // The instrumentations, after the providers are registered: their
  // constructors read the API globals (design D-JS1, amended by B6). Not
  // under OTEL_SDK_DISABLED=true (Python: no instrumentation) nor into a
  // foreign API registry. Bound to the forwarding providers when they are the
  // globals (so a re-setup needs no re-bind), else to the global ones (a
  // foreign provider), so instrumentation spans join the app's own; every
  // setup binds them again, so a later `replaceGlobals` setup moves them to
  // the helper's (task B9).
  const instrumenting = foreignApi === undefined && built !== undefined;
  const bound = instrumenting
    ? {
        tracerProvider: ownsTracerProvider ? fw.tracerProvider : trace.getTracerProvider(),
        meterProvider: ownsMeterProvider ? fw.meterProvider : metrics.getMeterProvider(),
        loggerProvider: ownsLoggerProvider ? fw.loggerProvider : logs.getLoggerProvider(),
      }
    : undefined;
  const instrumentations = bound !== undefined ? instrumentationsOf(rt) : undefined;
  if (bound !== undefined && instrumentations !== undefined) {
    attempt.instrumented = tryEnableInstrumentations(instrumentations, plan, bound, loadedBeforeSetup);
    // Spec JS5: in an ES module process, import-in-the-middle only sees later
    // imports through the loader hooks (require-in-the-middle never does).
    if (attempt.instrumented && hooksMode() === undefined && entryIsEsm()) reportOnce('esm-without-hooks', esmWithoutHooksMessage(options.instrumentations ?? []));
  }
  // The opt-in instrumentations (task B10a), on the same providers and under
  // the same conditions; otherwise left disabled (one built enabled by its
  // constructor is turned off: no instrumentation under OTEL_SDK_DISABLED=true).
  const optIn = options.instrumentations ?? [];
  if (bound !== undefined) {
    attempt.optIn = enableOptIn(optIn, bound);
    for (const instrumentation of attempt.optIn) rt.optIn.add(instrumentation);
  } else {
    disableOptIn(optIn);
  }
  step('instrumentations');
  // The listener serves only a reader whose MeterProvider is the global one
  // (a foreign provider's instruments would never reach it: 503 instead).
  const prometheus = built?.prometheus;
  const port = plan.resolved.prometheusMetricsPort;
  if (prometheus !== undefined && ownsMeterProvider && port > 0 && !isMainThread) {
    // A worker thread shares the process (and the port) with the main thread: no listener (B9 fix round M2).
    reportOnce(
      'worker:metrics-port',
      `worker thread: the helper's /metrics listener (${ENV_METRICS_PORT}=${String(port)}) is not started outside the main thread; mount metricsHandler() on a server of this thread, or export metrics over OTLP`,
      { variable: ENV_METRICS_PORT },
    );
  } else if (prometheus !== undefined && ownsMeterProvider && port > 0) {
    if (cluster.isWorker) {
      reportOnce(
        'cluster:metrics-port',
        `cluster worker serving /metrics on port ${String(port)}: if several workers use this port, each scrape reaches one of them at random; give each worker its own port (e.g. base + a per-worker slot) or use OTLP only`,
        { variable: ENV_METRICS_PORT },
      );
    }
    attempt.listener = startListener(port, metricsHandler());
  }
  step('listener');
  const inactiveSignals: Signal[] = [];
  if (foreignApi !== undefined) inactiveSignals.push(...ALL_SIGNALS);
  else if (built !== undefined) {
    if (!ownsTracerProvider) inactiveSignals.push('traces');
    if (!ownsMeterProvider) inactiveSignals.push('metrics');
  }
  return {
    resolved: withInactiveSignals(plan.resolved, inactiveSignals),
    resource: built?.resource,
    tracerProvider: built?.tracerProvider,
    meterProvider: built?.meterProvider,
    loggerProvider: built?.loggerProvider,
    ownsTracerProvider,
    ownsMeterProvider,
    ownsLoggerProvider,
    // Not into a foreign API registry: its telemetry is inactive (reported above).
    logsExported: foreignApi === undefined && built?.logsExported === true,
    prometheus,
    prometheusInactive: !plan.resolved.metricExporters.includes('prometheus'),
    listener: attempt.listener,
    drop: dropPredicate(plan.resolved.disabledMetrics),
    instrumented: attempt.instrumented,
    optIn: attempt.optIn,
    origin,
    shutdown: undefined,
  };
}

/** Where the helper's loggers write a record now, and the installed level (design D6, R4). */
export interface LogDestination {
  /** The active setup's LoggerProvider while it has an export processor; `undefined` = stdout. */
  readonly provider: LoggerProvider | undefined;
  /** The level the last successful setup resolved; `info` before the first. */
  readonly level: LogLevel;
}

/**
 * Read per record by the helper's loggers: the provider only while a setup
 * is active (not shut down) and has a logs exporter, so a logger created
 * before `setup()` switches to it and one outliving `shutdown()` goes back to
 * stdout instead of a dead provider (Go `activeLogBridge`).
 */
export function logDestination(): LogDestination {
  const state = getState();
  if (state === null) return { provider: undefined, level: 'info' };
  const rt = runtimeOf(state);
  const active = rt.active;
  const exporting = active !== undefined && active.shutdown === undefined && active.logsExported;
  return { provider: exporting ? active.loggerProvider : undefined, level: rt.logLevel ?? 'info' };
}

/** A provider's bridge flush running now, and the one queued behind it (B10d fix round 2, 2026-11-03). */
interface BridgeFlushes {
  current: Promise<void> | undefined;
  queued: Promise<void> | undefined;
}

/**
 * Per LoggerProvider (final round, 2026-11-03): a flush for a new setup's
 * provider never waits behind one of an older (perhaps hung) provider, and
 * `shutdown()` waits only for its own provider's flushes.
 */
const bridgeFlushes = new Map<LoggerProvider, BridgeFlushes>();

/**
 * Flushes the active setup's logs pipeline (a bridge's `flush(cb)` /
 * `close()`, so `logger.flush(() => process.exit())` delivers its records).
 * Resolves when done, at once when records go to stdout; never rejects (a
 * failure is reported once, with its reason only).
 *
 * Serialized: sdk-logs 3.0 `BatchLogRecordProcessorBase._flushAll` returns at
 * once while another flush runs (`if (this._flushing) return`), so a second
 * concurrent `forceFlush()` would resolve before its records were exported
 * (D-JS14). Per provider, one flush runs at a time; a request that arrives
 * meanwhile waits for it and then runs one more `forceFlush` (requests
 * arriving during the same wait share it).
 *
 * @internal For `@staffops/otel-helper-pino` / `-winston` only (B10d).
 */
export function flushBridgeRecords(): Promise<void> {
  const provider = logDestination().provider;
  return provider === undefined ? Promise.resolve() : flushProvider(provider);
}

function flushProvider(provider: LoggerProvider): Promise<void> {
  let entry = bridgeFlushes.get(provider);
  if (entry === undefined) {
    entry = { current: undefined, queued: undefined };
    bridgeFlushes.set(provider, entry);
  }
  const flushes = entry;
  if (flushes.current === undefined) {
    flushes.current = runBridgeFlush(provider).finally(() => {
      flushes.current = undefined;
      if (flushes.queued === undefined) bridgeFlushes.delete(provider);
    });
    return flushes.current;
  }
  flushes.queued ??= flushes.current.then(() => {
    flushes.queued = undefined;
    return flushProvider(provider);
  });
  return flushes.queued;
}

async function runBridgeFlush(provider: LoggerProvider): Promise<void> {
  try {
    await provider.forceFlush();
  } catch (err) {
    // Redacted (an exporter error can carry an endpoint), read guarded: the flush never rejects.
    reportOnce('logging:flush-failed', 'the helper logs pipeline could not be flushed', { reason: redactErrorText(flushFailureReason(err)) });
  }
}

/** The failure's message when it is a readable string, else its type; never throws. */
function flushFailureReason(err: unknown): string {
  try {
    if (err instanceof Error && typeof err.message === 'string') return err.message;
  } catch {
    // A throwing `message` getter or prototype trap.
  }
  return typeof err;
}

/**
 * Settles once the bridge flushes of `provider` pending **now** have settled
 * (the queued one runs after the current one, so it covers both): `shutdown()`
 * waits for them before shutting the provider down, whose own `_flushAll`
 * would otherwise return early. Flushes requested later are not waited for (no
 * starvation by a steady stream of flushes).
 */
function bridgeFlushesSettled(provider: LoggerProvider): Promise<void> {
  const flushes = bridgeFlushes.get(provider);
  return (flushes?.queued ?? flushes?.current ?? Promise.resolve()).then(() => undefined);
}

const UNAVAILABLE: ScrapeTarget = Object.freeze({ kind: 'unavailable' });
const INACTIVE: ScrapeTarget = Object.freeze({ kind: 'inactive' });

function scrapeTarget(): ScrapeTarget {
  const state = getState();
  const active = state === null ? undefined : runtimeOf(state).active;
  if (active === undefined || active.shutdown !== undefined) return UNAVAILABLE;
  if (active.prometheus !== undefined) return active.ownsMeterProvider ? { kind: 'active', reader: active.prometheus, drop: active.drop } : UNAVAILABLE;
  return active.prometheusInactive ? INACTIVE : UNAVAILABLE;
}

/**
 * A request handler serving the Prometheus scrape of the active setup, for
 * the app's own server (`node:http`, express):
 * `app.get('/metrics', metricsHandler())`. Pair it with
 * `OTEL_HELPER_METRICS_PORT=0` (or `prometheusMetricsPort: 0`) to skip the
 * helper's listener. It can be created before `setup()`; every request reads
 * the current state. Status (design "Mounted /metrics handler status"): 503
 * before setup, after a failed one, after `shutdown()` and when the global
 * MeterProvider is not the helper's; 200 with an empty body when Prometheus
 * is deliberately off (`OTEL_SDK_DISABLED=true`, or `OTEL_METRICS_EXPORTER`
 * without `prometheus`); else 200 with the exposition text (format 0.0.4).
 */
export function metricsHandler(): (req: ScrapeRequest, res: ScrapeResponse) => void {
  return (req, res) => {
    serveMetrics(scrapeTarget, req, res);
  };
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
 * is reported instead of the process exiting silently. The deadline bounds the
 * promise, not the process: a provider still exporting, or any other open
 * handle, can keep the process alive after it settles, so a signal handler
 * should exit explicitly:
 * `process.once('SIGTERM', () => { shutdown().finally(() => process.exit(0)); })`.
 * The global providers keep forwarding to this setup's providers until each
 * one's shutdown settles (their final export still runs the observable
 * callbacks), then forward to nothing. Without an active setup it resolves
 * immediately.
 */
export function shutdown(timeoutMs: number = DEFAULT_SHUTDOWN_TIMEOUT_MS): Promise<void> {
  if (!validTimeout(timeoutMs)) {
    return Promise.reject(new TypeError(`shutdown(): timeoutMs must be a positive number, got ${String(timeoutMs)}`));
  }
  const state = getState();
  if (state === null) return Promise.resolve();
  const rt = runtimeOf(state);
  const active = rt.active;
  if (active === undefined) return Promise.resolve();
  active.shutdown ??= runShutdown(rt, active, Math.min(timeoutMs, MAX_TIMER_MS));
  return active.shutdown;
}

/**
 * Points `slot` at nothing once `provider` settled, if it still forwards to
 * it (a newer setup may have replaced it meanwhile).
 */
function releaseDelegate(slot: DelegateSlot<unknown> | undefined, provider: unknown): void {
  if (slot !== undefined && slot.getDelegate() === provider) slot.setDelegate(undefined);
}

async function runShutdown(rt: Runtime, active: Active, timeoutMs: number): Promise<void> {
  let timer: NodeJS.Timeout | undefined;
  try {
    // Ref'd on purpose (amended D-JS10): an unref'd timer lets a process whose
    // only pending work is a hung provider exit without the rejection.
    const deadline = new Promise<'timeout'>((resolve) => {
      timer = setTimeout(() => {
        resolve('timeout');
      }, timeoutMs);
    });
    // Instrumentations off first (Python uninstruments first): no new spans or
    // measurements from them reach providers that are shutting down.
    if (active.instrumented) disableInstrumentations(rt.instrumentations);
    disableOptIn(active.optIn);
    for (const instrumentation of active.optIn) rt.optIn.delete(instrumentation);
    // The listener first (Go): scrapes stop, then the MeterProvider flushes.
    const listenerClosed = closeListener(active.listener);
    // The forwarders keep each provider as their delegate until its shutdown
    // settles (B9 fix round H1): Go and Python keep their global providers
    // installed through the flush, and the final collection must still call
    // the observable and batch callbacks registered with it. Only the
    // MeterProvider takes the deadline (`timeoutMillis`); the TracerProvider
    // and LoggerProvider of SDK 3.0 take none (D-JS14), the race below bounds them.
    const fw = rt.forwarders;
    const slots: Readonly<Record<string, DelegateSlot<unknown> | undefined>> = {
      [PROVIDER_NAMES.trace]: fw?.tracerProvider,
      [PROVIDER_NAMES.metrics]: fw?.meterProvider,
      [PROVIDER_NAMES.logs]: fw?.loggerProvider,
    };
    const named = providersOf(active);
    // Promise.resolve().then(): a provider that throws synchronously still lands in allSettled.
    const providers = named.map(({ name, provider }) => {
      const run = name === PROVIDER_NAMES.metrics
        ? listenerClosed.then(() => (provider as MeterProvider).shutdown({ timeoutMillis: timeoutMs }))
        : name === PROVIDER_NAMES.logs
          // An in-flight bridge flush first: during it the SDK's shutdown flush would return early (D-JS14).
          ? bridgeFlushesSettled(provider as LoggerProvider).then(() => provider.shutdown())
          : Promise.resolve().then(() => provider.shutdown());
      return run.finally(() => {
        releaseDelegate(slots[name], provider);
      });
    });
    const outcome = await Promise.race([Promise.allSettled(providers), deadline]);
    if (outcome === 'timeout') {
      // A hung provider stops being a delegate now, not whenever it settles (B9 final round).
      for (const { name, provider } of named) releaseDelegate(slots[name], provider);
      throw new Error(`shutdown(): providers did not finish within ${String(timeoutMs)} ms`);
    }
    const failed = outcome.flatMap((r, i) => (r.status === 'rejected' ? [{ name: named[i]?.name ?? 'provider', reason: r.reason as unknown }] : []));
    const first = failed[0];
    if (first !== undefined) {
      // Exporter errors carry the redacted endpoint at most; cut anyway.
      const why = redactErrorText(first.reason instanceof Error ? first.reason.message : String(first.reason));
      throw new AggregateError(
        failed.map((f) => f.reason),
        `shutdown(): ${failed.map((f) => f.name).join(', ')} failed to shut down (first: ${why})`,
      );
    }
  } finally {
    clearTimeout(timer);
  }
}

/** The resource of the active setup's TracerProvider (tests only, through `/testing`); `undefined` without one, after `shutdown()` and under `OTEL_SDK_DISABLED=true`, as `getProviders()`. */
export function getResource(): Resource | undefined {
  const state = getState();
  const active = state === null ? undefined : runtimeOf(state).active;
  return active === undefined || active.shutdown !== undefined ? undefined : active.resource;
}

/** The SDK providers of the active setup, frozen (tests only, through `/testing`); `undefined` without one, after `shutdown()` and under `OTEL_SDK_DISABLED=true`. */
export function getProviders(): { readonly tracerProvider: TracerProvider; readonly meterProvider: MeterProvider; readonly loggerProvider: LoggerProvider } | undefined {
  const state = getState();
  const active = state === null ? undefined : runtimeOf(state).active;
  if (active === undefined || active.shutdown !== undefined) return undefined;
  const { tracerProvider, meterProvider, loggerProvider } = active;
  if (tracerProvider === undefined || meterProvider === undefined || loggerProvider === undefined) return undefined;
  return Object.freeze({ tracerProvider, meterProvider, loggerProvider });
}

/**
 * Test-only: forgets every setup, unregisters what the helper registered
 * (the globals it still owns, its DiagLogger), points the forwarders at
 * nothing and discards them (tracers, meters and loggers obtained before
 * `reset()` stay no-ops), clears the "once" diagnostics and restores the
 * stderr sink. Providers not shut down yet are shut down in the background.
 * The instrumentations are kept, disabled: their require/import hooks stay
 * registered.
 */
export function resetForTests(): void {
  const state = getState();
  if (state !== null) {
    const rt = runtimeOf(state);
    const active = rt.active;
    if (active !== undefined) {
      void closeListener(active.listener);
      if (active.shutdown === undefined) {
        for (const { provider } of providersOf(active)) shutDownQuietly(provider);
      }
      rt.active = undefined;
    }
    const fw = rt.forwarders;
    if (fw !== undefined) {
      fw.tracerProvider.setDelegate(undefined);
      fw.meterProvider.setDelegate(undefined);
      fw.loggerProvider.setDelegate(undefined);
      fw.propagator.setDelegate(undefined);
      // Discarded: objects obtained before reset() stay no-ops, so a test's
      // observable callbacks never reach the next test's setup.
      rt.forwarders = undefined;
    }
    for (const type of Object.keys(rt.registered) as OwnedGlobal[]) {
      // Only the helper's own entries: one registered over them is someone else's.
      if (rt.registered[type] !== undefined && registeredGlobal(type) === rt.registered[type]) DISABLE[type]();
    }
    rt.registered = {};
    // Unconditionally: whatever a setup left enabled (B6 review).
    disableInstrumentations(rt.instrumentations);
    disableOptIn(rt.optIn);
    rt.optIn.clear();
    if (helperOwnsDiag(rt)) diag.disable();
    rt.diagLogger = undefined;
    rt.logLevel = undefined;
  }
  clearReportedOnce();
  setDiagnosticSink(undefined);
}
