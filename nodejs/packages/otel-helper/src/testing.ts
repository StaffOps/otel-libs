/**
 * `@staffops/otel-helper/testing` — test-only utilities. Not for production
 * code: `reset()` discards helper state.
 */
import { getResource, resetForTests } from './internal/setup';
import { getState, setDiagnosticSink } from './internal/state';

export type { DiagnosticLevel, DiagnosticMeta, DiagnosticOrigin, DiagnosticSink, HelperState, HooksMode } from './internal/state';
export { STATE_KEY } from './internal/state';
export { getResource, getState, setDiagnosticSink };

/**
 * Restores helper state to its defaults: forgets the active setup (the
 * globals and DiagLogger the helper registered are unregistered), the
 * "warned once" diagnostics and a custom diagnostics sink.
 */
export function reset(): void {
  resetForTests();
}
