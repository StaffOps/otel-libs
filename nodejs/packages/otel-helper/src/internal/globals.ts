/**
 * Read-only view of the OpenTelemetry API's global registry, so the helper can
 * tell whether a global is already registered (and whether it is the helper's)
 * **before** calling `set*Global*`: a refused duplicate registration makes the
 * API write an error with a stack trace to `diag`.
 */

// The API keeps its globals under `Symbol.for('opentelemetry.js.api.<major>')`
// (api `internal/global-utils`); the peer range (>=1.10.0 <1.11.0) fixes major 1.
const API_GLOBAL_KEY = Symbol.for('opentelemetry.js.api.1');

/** Names of the API globals the helper registers. */
export type ApiGlobal = 'trace' | 'context' | 'diag' | 'metrics' | 'propagation';

/** The registered instance for `type`, or `undefined`. Never throws. */
export function registeredGlobal(type: ApiGlobal): unknown {
  try {
    const registry = (globalThis as Record<symbol, unknown>)[API_GLOBAL_KEY];
    if (typeof registry !== 'object' || registry === null) return undefined;
    return (registry as Record<string, unknown>)[type];
  } catch {
    return undefined;
  }
}
