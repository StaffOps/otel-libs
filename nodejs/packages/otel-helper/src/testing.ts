/**
 * `@staffops/otel-helper/testing` — test-only utilities. Not for production
 * code: `reset()` discards helper state.
 */
import { getProviders, getResource, resetForTests } from './internal/setup';
import { getState, setDiagnosticSink } from './internal/state';

export type { DiagnosticLevel, DiagnosticMeta, DiagnosticOrigin, DiagnosticSink, HelperState, HooksMode } from './internal/state';
export { STATE_KEY } from './internal/state';
export { getProviders, getResource, getState, setDiagnosticSink };

/**
 * Restores helper state to its defaults: forgets the active setup (the
 * globals and DiagLogger the helper still owns are unregistered, its
 * forwarding providers are discarded: what was obtained from them stays a
 * no-op), the "warned once" diagnostics and a custom diagnostics sink.
 */
export function reset(): void {
  resetForTests();
}
