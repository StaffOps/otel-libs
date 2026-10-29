import { report } from './diagnostics';
import { readSlot, stateVersion, STATE_KEY, type DiagnosticSink, type HelperState } from './key';
import { VERSION } from '../version';

export { STATE_KEY };
export type { DiagnosticSink, HelperState, HooksMode } from './key';

// Resolved once per module instance: `null` = this copy is inactive.
let resolved: HelperState | null | undefined;

function inactive(message: string, attrs?: Readonly<Record<string, string>>): null {
  report(`${message}; this copy of @staffops/otel-helper stays inactive`, attrs);
  resolved = null;
  return null;
}

/**
 * Returns the shared state, creating it on first access. Never throws (a
 * preload must not crash the app): when the slot cannot be read, holds
 * something that is not a helper state, or holds the state of another
 * version, this copy reports once on the diagnostics sink and stays inactive
 * (`null`), so two versions never drive the same globals. The slot is
 * non-configurable and `version` non-writable, so neither can be replaced.
 */
export function getState(): HelperState | null {
  if (resolved !== undefined) return resolved;
  const slot = readSlot();
  if (!slot.ok) return inactive('the shared state slot cannot be read');
  const existing = slot.value;
  if (existing === undefined) {
    const created = Object.defineProperty({ hooks: undefined, sink: undefined }, 'version', {
      value: VERSION,
      enumerable: true,
      writable: false,
      configurable: false,
    }) as HelperState;
    try {
      Object.defineProperty(globalThis, STATE_KEY, { value: created, configurable: false, enumerable: false, writable: false });
    } catch {
      return inactive('the shared state slot cannot be created');
    }
    resolved = created;
    return created;
  }
  const version = stateVersion(existing);
  if (version === undefined) return inactive('the shared state slot holds an unexpected value');
  if (version !== VERSION) return inactive('another version is already loaded in this process', { loaded: version, inactive: VERSION });
  resolved = existing as HelperState;
  return resolved;
}

/**
 * Replaces the diagnostics sink of the shared state (parity runner, tests);
 * `undefined` restores stderr. No effect while this copy is inactive.
 */
export function setDiagnosticSink(next: DiagnosticSink | undefined): void {
  const state = getState();
  if (state === null) return;
  try {
    state.sink = next;
  } catch {
    report('the diagnostics sink cannot be replaced: the shared state is read-only');
  }
}
