/**
 * Which instrumented libraries the app has, and the load errors (contract
 * design D14, "Python ext import failures" and the Node `-sql` paragraph: a
 * cause is named only when it is certain). As `-sql` (B10a fix round R3):
 * resolution never decides alone — an app can load a library from a
 * directory the helper cannot see (a nested, non-hoisted `node_modules`) — so
 * an unresolved library is reported, not an error; only a contrib
 * instrumentation that does not load is.
 */
import * as path from 'node:path';

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
 * working directory, then the main module's `node_modules` lookup paths.
 */
export function appDirectories(
  argv1: string | undefined = process.argv[1],
  cwd: string = process.cwd(),
  mainPaths: readonly string[] = require.main?.paths ?? [],
): string[] {
  const dirs: string[] = [];
  if (argv1 !== undefined && argv1 !== '') dirs.push(path.dirname(path.resolve(cwd, argv1)));
  dirs.push(cwd, ...mainPaths);
  return [...new Set(dirs)];
}

/**
 * Whether one of the library's packages resolves from the app (never loaded:
 * requiring the library before its instrumentation is enabled would leave
 * the app's copy unpatched).
 */
export function resolvesFromApp(library: Library, dirs: readonly string[] = appDirectories()): boolean {
  return PACKAGES[library].some((name) => {
    try {
      require.resolve(name, { paths: [...dirs] });
      return true;
    } catch {
      return false;
    }
  });
}

/**
 * The report for a library the app dirs do not resolve; it is still
 * instrumented. WARN when the library was named `true`, `diag` debug in
 * default mode (an absent library is the common case).
 */
export function unresolvedMessage(library: Library, dirs: readonly string[]): string {
  return `@staffops/otel-helper-redis: '${library}' could not be resolved from ${dirs.join(', ')}; instrumented if it loads`;
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
  const message = err instanceof Error ? err.message : String(err);
  const first = message.split('\n', 1)[0] ?? '';
  const code = typeof err === 'object' && err !== null ? (err as { code?: unknown }).code : undefined;
  if (code === 'MODULE_NOT_FOUND' && first.includes(`'${module}'`)) {
    return new Error(`Redis instrumentation not installed: ${module} is missing. Reinstall @staffops/otel-helper-redis (it depends on ${module}).`, { cause: err });
  }
  return new Error(`failed to import the Redis instrumentation: ${first}`, { cause: err });
}
