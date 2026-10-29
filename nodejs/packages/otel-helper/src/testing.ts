/**
 * `@staffops/otel-helper/testing` — test-only utilities. Not for production
 * code: `reset()` discards helper state.
 */
import { getState, setDiagnosticSink } from './internal/state';

export type { DiagnosticSink, HelperState, HooksMode } from './internal/state';
export { STATE_KEY } from './internal/state';
export { getState, setDiagnosticSink };

/** Restores test-visible helper state to its defaults (the diagnostics sink, for now). */
export function reset(): void {
  setDiagnosticSink(undefined);
}
