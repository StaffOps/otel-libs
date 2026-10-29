/**
 * Read-only view of the OpenTelemetry API's global registry, so the helper can
 * tell whether a global is already registered (and whether it is the helper's)
 * **before** calling `set*Global*`: a refused duplicate registration makes the
 * API write an error with a stack trace to `diag`.
 */

// The API keeps its globals under `Symbol.for('opentelemetry.js.api.<major>')`
// (api `internal/global-utils`); the peer range (>=1.10.0 <1.11.0) fixes major 1.
import * as fs from 'node:fs';
import * as path from 'node:path';

const API_GLOBAL_KEY = Symbol.for('opentelemetry.js.api.1');

// The API refuses to register into a registry created by another API version:
// `registerGlobal` requires `registry.version` to equal its own VERSION
// exactly. A registry whose version differs from the API copy the helper loaded
// is foreign: the helper registers nothing into it.
const SUPPORTED_RANGE = /^1\.10\.[0-9]+$/;

let loaded: string | null | undefined;

/**
 * Version of the `@opentelemetry/api` copy this helper loads, read once from
 * its package.json (not exported by the package, so found next to its entry
 * point); `undefined` when it cannot be read.
 */
export function loadedApiVersion(): string | undefined {
  if (loaded === undefined) {
    loaded = null;
    try {
      let dir = path.dirname(require.resolve('@opentelemetry/api'));
      for (let i = 0; i < 6 && loaded === null; i++) {
        const file = path.join(dir, 'package.json');
        if (fs.existsSync(file)) {
          const pkg = JSON.parse(fs.readFileSync(file, 'utf8')) as { name?: unknown; version?: unknown };
          if (pkg.name === '@opentelemetry/api' && typeof pkg.version === 'string') loaded = pkg.version;
        }
        dir = path.dirname(dir);
      }
    } catch {
      loaded = null;
    }
  }
  return loaded ?? undefined;
}

/** Names of the API globals the helper registers. */
export type ApiGlobal = 'trace' | 'context' | 'diag' | 'metrics' | 'propagation';

function registry(): Record<string, unknown> | undefined {
  const value = (globalThis as Record<symbol, unknown>)[API_GLOBAL_KEY];
  return typeof value === 'object' && value !== null ? (value as Record<string, unknown>) : undefined;
}

/** A registry created by another API version: what it says and what the helper needs. */
export interface ForeignRegistry {
  /** Its `version` (`'unknown'` when unreadable or not a string). */
  readonly version: string;
  /** The version the helper's API copy registers with (`1.10.x` when unknown). */
  readonly required: string;
}

/**
 * The registry, when it was created by an `@opentelemetry/api` other than the
 * copy the helper loaded (exact version; the peer range `1.10.x` when that
 * version cannot be read); `undefined` when there is no registry yet or it
 * matches. Never throws.
 */
export function foreignRegistry(): ForeignRegistry | undefined {
  const own = loadedApiVersion();
  const required = own ?? '1.10.x';
  try {
    const reg = registry();
    if (reg === undefined) return undefined;
    const version = reg.version;
    if (typeof version !== 'string') return { version: 'unknown', required };
    const matches = own === undefined ? SUPPORTED_RANGE.test(version) : version === own;
    return matches ? undefined : { version, required };
  } catch {
    return { version: 'unknown', required };
  }
}

/** The registered instance for `type`, or `undefined`. Never throws. */
export function registeredGlobal(type: ApiGlobal): unknown {
  try {
    return registry()?.[type];
  } catch {
    return undefined;
  }
}
