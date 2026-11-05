/**
 * Which instrumented libraries the app has, and the load errors (contract
 * design D14, "Python ext import failures": a cause is named only when it is
 * certain). Fix round 2026-11-03 (R3): resolution never decides alone — an app
 * can load a library from a directory the helper cannot see (a nested,
 * non-hoisted `node_modules`) — so an unresolved library is reported, not an
 * error; only a contrib instrumentation that does not load is.
 */
import {
  appDirectories as coreAppDirectories,
  contribLoadError,
  resolvesFromApp as coreResolvesFromApp,
} from '@staffops/otel-helper/internal/contrib';

/** The libraries this package instruments, in the order they are reported. */
export const LIBRARIES = ['pg', 'mysql2'] as const;
export type Library = (typeof LIBRARIES)[number];

/**
 * Directories the app resolves packages from: its entry script's, the
 * working directory, then the main module's `node_modules` lookup paths
 * (core `internal/contrib`).
 */
export const appDirectories: typeof coreAppDirectories = coreAppDirectories;

/**
 * Whether `name` resolves from the app (never loaded: requiring the library
 * before its instrumentation is enabled would leave the app's copy unpatched).
 */
export function resolvesFromApp(name: string, dirs: readonly string[] = appDirectories()): boolean {
  return coreResolvesFromApp([name], dirs);
}

/**
 * The error for a failed load of a contrib instrumentation module (`module`,
 * e.g. `@opentelemetry/instrumentation-pg`): "not installed" only when the
 * module itself is the one not found, else the load error verbatim (first
 * line: the require stack lists local paths). The returned error's `cause` is
 * the original load error, for debugging (never printed by the helper).
 */
export function instrumentationLoadError(module: string, err: unknown): Error {
  return contribLoadError('SQL', '@staffops/otel-helper-sql', module, err);
}
