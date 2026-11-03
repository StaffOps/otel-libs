/**
 * Which instrumented libraries the app has, and the load errors (contract
 * design D14, "Python ext import failures" and the Node `-sql` paragraph: a
 * cause is named only when it is certain). As `-sql` (B10a fix round R3):
 * resolution never decides alone — an app can load a library from a
 * directory the helper cannot see (a nested, non-hoisted `node_modules`) — so
 * an unresolved library is reported, not an error; only a contrib
 * instrumentation that does not load is.
 */
import { appDirectories as coreAppDirectories, contribLoadError, resolvesFromApp as coreResolvesFromApp } from '@staffops/otel-helper/internal/contrib';

/** The libraries this package instruments, in the order they are reported. */
export const LIBRARIES = ['redis', 'ioredis'] as const;
export type Library = (typeof LIBRARIES)[number];

/**
 * Package names that count as the library being present: node-redis is
 * usually installed as `redis` (which depends on `@redis/client`, the package
 * instrumentation-redis patches), sometimes as `@redis/client` alone.
 */
export const PACKAGES: Readonly<Record<Library, readonly string[]>> = Object.freeze({
  redis: Object.freeze(['redis', '@redis/client']),
  ioredis: Object.freeze(['ioredis']),
});

/**
 * Directories the app resolves packages from: its entry script's, the
 * working directory, then the main module's `node_modules` lookup paths
 * (core `internal/contrib` since B10c).
 */
export const appDirectories: typeof coreAppDirectories = coreAppDirectories;

/**
 * Whether one of the library's packages resolves from the app (never loaded:
 * requiring the library before its instrumentation is enabled would leave
 * the app's copy unpatched).
 */
export function resolvesFromApp(library: Library, dirs: readonly string[] = appDirectories()): boolean {
  return coreResolvesFromApp(PACKAGES[library], dirs);
}

/**
 * The error for a failed load of a contrib instrumentation module (`module`,
 * e.g. `@opentelemetry/instrumentation-ioredis`): "not installed" only when
 * the module itself is the one not found, else the load error verbatim
 * (first line: the require stack lists local paths). The returned error's
 * `cause` is the original load error, for debugging (never printed by the
 * helper).
 */
export function instrumentationLoadError(module: string, err: unknown): Error {
  return contribLoadError('Redis', '@staffops/otel-helper-redis', module, err);
}
