/**
 * `@staffops/otel-helper/testing` — test-only utilities. Not for production
 * code (`reset()` discards helper state) and not part of the API contract:
 * this entry may change in any 0.x release (see "API stability (0.x)" in the
 * Node.js README).
 *
 * - {@link reset}, {@link setDiagnosticSink}, {@link getProviders},
 *   {@link getResource}: for an app's own tests.
 * - {@link getState}, {@link STATE_KEY}, `HelperState`: `@internal`, used by
 *   the helper's own test suites only (the opt-in packages' fixtures read the
 *   hooks mode from them); their shape changes without notice.
 *
 * @packageDocumentation
 */
import { getProviders, getResource, resetForTests } from './internal/setup';
import { getState, setDiagnosticSink } from './internal/state';

export type { DiagnosticLevel, DiagnosticMeta, DiagnosticOrigin, DiagnosticSink, HooksMode } from './internal/state';
/**
 * The process-wide helper state. Test-only, for the helper's own suites; its
 * shape changes without notice.
 *
 * @internal
 */
export type { HelperState } from './internal/state';
/**
 * The `Symbol.for` key of the shared state slot. Test-only, for the helper's
 * own suites.
 *
 * @internal
 */
export { STATE_KEY } from './internal/state';
export { getProviders, getResource, setDiagnosticSink };
/**
 * The shared helper state (`null` when this copy is inactive). Test-only, for
 * the helper's own suites; its shape changes without notice.
 *
 * @internal
 */
export { getState };

/**
 * Restores helper state to its defaults: forgets the active setup (the
 * globals and DiagLogger the helper still owns are unregistered, its
 * forwarding providers are discarded: what was obtained from them stays a
 * no-op), the "warned once" diagnostics and a custom diagnostics sink.
 */
export function reset(): void {
  resetForTests();
}
