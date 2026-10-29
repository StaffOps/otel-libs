/**
 * Process-wide slot of the helper state (spec JS5, design D-JS10). `Symbol.for`
 * makes every copy of the package loaded in this process (through `require`,
 * through `import`, or from a second install) see the same slot. This module
 * has no imports so diagnostics and state can both use it without a cycle.
 */
export const STATE_KEY: unique symbol = Symbol.for('staffops.otel-helper.state.v1');

/** How the module-loading hooks were installed, if they were. */
export type HooksMode = 'sync' | 'async' | 'cjs-only';

/** Receives one fully formatted diagnostic line (without the trailing newline). */
export type DiagnosticSink = (line: string) => void;

/** Helper state shared by every load of the same version of the package. */
export interface HelperState {
  /** Package version that created the state (non-writable). */
  readonly version: string;
  /** Set once by `@staffops/otel-helper/hooks`. */
  hooks: HooksMode | undefined;
  /** Diagnostics sink installed by tests or the parity runner; `undefined` = stderr. */
  sink: DiagnosticSink | undefined;
}

/** Result of reading the slot: a getter or proxy on `globalThis` may throw. */
export type SlotRead = { readonly ok: true; readonly value: unknown } | { readonly ok: false };

export function readSlot(): SlotRead {
  try {
    return { ok: true, value: (globalThis as Record<symbol, unknown>)[STATE_KEY] };
  } catch {
    return { ok: false };
  }
}

/** Returns the slot's version when it holds a state-shaped object, else `undefined`. Never throws. */
export function stateVersion(value: unknown): string | undefined {
  if (typeof value !== 'object' || value === null) return undefined;
  try {
    const version: unknown = (value as { version?: unknown }).version;
    return typeof version === 'string' ? version : undefined;
  } catch {
    return undefined;
  }
}
