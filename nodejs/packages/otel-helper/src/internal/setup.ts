/**
 * `setup()` / `shutdown()` (spec JS5, JS7; design D-JS1, D-JS10). Task B3
 * builds the resource, the TracerProvider (no exporters yet: B4), the context
 * manager and the DiagLogger. The full lifecycle — rollback, foreign globals,
 * re-binding instrumentations, the stable forwarding providers of D-JS10 — is
 * task B9; the shape below (one runtime record on the shared state, rebuilt
 * after shutdown) is what it extends.
 */
import { context, diag, trace } from '@opentelemetry/api';
import { AsyncLocalStorageContextManager } from '@opentelemetry/context-async-hooks';
import { defaultResource, resourceFromAttributes, type Resource } from '@opentelemetry/resources';
import { TracerProvider } from '@opentelemetry/sdk-trace';
import { clearReportedOnce, report } from './diagnostics';
import { installDiagLogger } from './diag-logger';
import { registeredGlobal } from './globals';
import { ATTR_SERVICE_NAME, resolvePlan, validateOptions, type Resolved, type SetupOptions } from './resolve';
import { getState, setDiagnosticSink, type HelperState } from './state';

/** What one successful `setup()` built. */
interface Active {
  readonly resolved: Resolved;
  readonly resource: Resource;
  readonly tracerProvider: TracerProvider;
  /** Whether the global TracerProvider is the helper's (false: a foreign one was registered first). */
  readonly ownsTracerProvider: boolean;
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
/** Largest delay `setTimeout` honors (larger values fire after 1 ms). */
const MAX_TIMEOUT_MS = 2 ** 31 - 1;

function runtimeOf(state: HelperState): Runtime {
  state.runtime ??= { active: undefined, diagLogger: undefined } satisfies Runtime;
  return state.runtime as Runtime;
}

function defaultServiceName(base: Resource): string {
  const name = base.attributes[ATTR_SERVICE_NAME];
  return typeof name === 'string' ? name : 'unknown_service';
}

function helperOwnsDiag(rt: Runtime): boolean {
  return rt.diagLogger !== undefined && registeredGlobal('diag') === rt.diagLogger;
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
  // Before resolution, so SDK messages from everything built below are captured.
  const ownsDiag = helperOwnsDiag(rt);
  if (validated.installDiagLogger === false) {
    // Opt-out on a re-setup: remove the logger an earlier setup installed.
    if (ownsDiag) diag.disable();
    rt.diagLogger = undefined;
  } else {
    installDiagLogger(ownsDiag);
    rt.diagLogger = registeredGlobal('diag');
  }
  const plan = resolvePlan(validated, defaultServiceName(base));
  const resource = base.merge(resourceFromAttributes(plan.resourceAttributes));
  const tracerProvider = new TracerProvider({ resource, spanProcessors: [...(validated.spanProcessors ?? [])] });
  if (rt.active !== undefined) releaseGlobals(rt.active);
  // A global registered by someone else is kept; checking first avoids the
  // API's duplicate-registration error (with a stack trace) on `diag`.
  let contextManager: AsyncLocalStorageContextManager | undefined;
  if (registeredGlobal('context') === undefined) {
    contextManager = new AsyncLocalStorageContextManager().enable();
    context.setGlobalContextManager(contextManager);
  } else {
    report('a global context manager is already registered; the helper keeps it');
  }
  let ownsTracerProvider = false;
  if (registeredGlobal('trace') === undefined) {
    ownsTracerProvider = trace.setGlobalTracerProvider(tracerProvider);
  } else {
    report('a global TracerProvider is already registered; the helper does not replace it and its traces are inactive', {
      signal: 'traces',
    });
  }
  rt.active = { resolved: plan.resolved, resource, tracerProvider, ownsTracerProvider, contextManager, shutdown: undefined };
  return plan.resolved;
}

// Unregisters what an earlier setup registered, so the next one can.
function releaseGlobals(active: Active): void {
  if (active.ownsTracerProvider) trace.disable();
  if (active.contextManager !== undefined) context.disable();
}

function validTimeout(timeoutMs: unknown): timeoutMs is number {
  return typeof timeoutMs === 'number' && Number.isFinite(timeoutMs) && timeoutMs > 0;
}

/**
 * Flushes and shuts down what `setup()` built, under one deadline (default
 * 30 000 ms, clamped to 2^31-1 ms). The first call's promise is cached: later
 * calls return it. It rejects with an `AggregateError` when a provider failed
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
  active.shutdown ??= runShutdown(active, Math.min(timeoutMs, MAX_TIMEOUT_MS));
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
  const providers = [active.tracerProvider].map((p) => Promise.resolve().then(() => p.shutdown()));
  try {
    const outcome = await Promise.race([Promise.allSettled(providers), deadline]);
    if (outcome === 'timeout') throw new Error(`shutdown(): providers did not finish within ${String(timeoutMs)} ms`);
    const failures = outcome.filter((r): r is PromiseRejectedResult => r.status === 'rejected').map((r) => r.reason as unknown);
    if (failures.length > 0) throw new AggregateError(failures, 'shutdown(): a provider failed to shut down');
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
      if (active.shutdown === undefined) void Promise.resolve().then(() => active.tracerProvider.shutdown()).catch(() => undefined);
      rt.active = undefined;
    }
    if (helperOwnsDiag(rt)) diag.disable();
    rt.diagLogger = undefined;
  }
  clearReportedOnce();
  setDiagnosticSink(undefined);
}
