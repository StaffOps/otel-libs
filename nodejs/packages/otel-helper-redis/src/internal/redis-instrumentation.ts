/**
 * The `Instrumentation` returned by `redisInstrumentation()` and the
 * per-process state it shares (task B10b; the `-sql` model of B10a).
 *
 * One per process: the contrib instrumentations are built once and shared by
 * every result, and so are their enable state and bindings. Several results
 * enable a contrib instrumentation once (a count per child); the last one
 * disabled turns it off. Pass one `redisInstrumentation()` to each `setup()`.
 */
import { createRequire } from 'node:module';
import * as path from 'node:path';
import { diag, type LoggerProvider, type MeterProvider, type TracerProvider } from '@opentelemetry/api';
import type { Instrumentation, InstrumentationConfig } from '@opentelemetry/instrumentation';
import type * as IORedis from '@opentelemetry/instrumentation-ioredis';
import type * as Redis from '@opentelemetry/instrumentation-redis';
import { dbMeterProvider, dbTracerProvider, legacyKeysMode } from '@staffops/otel-helper/internal/db';
import { instrumentationLoadError, type Library } from './libraries';
import { commandSpanName, redisResponseCode, sanitizedStatement } from './statement';

export const NAME = '@staffops/otel-helper-redis';
/** Version of this package; a test keeps it equal to package.json `version`. */
export const VERSION = '0.3.0-dev.0';

/** The contrib module and its `instrumentationName` per library. */
export const CONTRIB: Readonly<Record<Library, string>> = Object.freeze({
  redis: '@opentelemetry/instrumentation-redis',
  ioredis: '@opentelemetry/instrumentation-ioredis',
});

/* eslint-disable @typescript-eslint/no-require-imports -- loaded on demand, so a failure is reported in D14 words */
const LOADERS: Readonly<Record<Library, () => unknown>> = {
  redis: () => require('@opentelemetry/instrumentation-redis') as typeof Redis,
  ioredis: () => require('@opentelemetry/instrumentation-ioredis') as typeof IORedis,
};
/* eslint-enable @typescript-eslint/no-require-imports */

const NODE_REDIS_FILES = ['commander.js', 'client/index.js', 'client/multi-command.js', 'cluster/index.js', 'cluster/multi-command.js'].map((f) => path.join('dist', 'lib', ...f.split('/')));

/**
 * Files each library's instrumentation patches, required again on the first
 * enable when they are cached (see `patchPreloaded`): [package, file inside
 * it, '' = its main entry]. node-redis 4+ is patched in `@redis/client`
 * (`@node-redis/client` for 4.0 pre-releases), node-redis 2–3 in `redis`.
 */
const PATCHED_FILES: Readonly<Record<Library, readonly (readonly [string, string])[]>> = {
  redis: [...['@redis/client', '@node-redis/client'].flatMap((pkg) => NODE_REDIS_FILES.map((file) => [pkg, file] as const)), ['redis', '']],
  ioredis: [['ioredis', '']],
};

// ── per-process state ─────────────────────────────────────────────────

const built = new Map<Library, Instrumentation>();
const enables = new Map<Instrumentation, number>();
const preloadChecked = new Set<Library>();
const reportedUnresolved = new Set<string>();
/** The TracerProvider each child was last bound to (the input, before wrapping): the same again is not re-bound. */
const boundTracer = new WeakMap<Instrumentation, TracerProvider>();
/** The current meter bind of each child: instruments of an older one record nothing (core `internal/db`). */
const meterGeneration = new WeakMap<Instrumentation, object>();

/** A `setConfig()` call is warned on the next enable, once the helper's DiagLogger is installed. */
let setConfigCalled = false;
let warnedSetConfig = false;

export { legacyKeysMode };

function build(library: Library): Instrumentation {
  // Not `exports`: in the CommonJS output that would shadow the module's own.
  let loaded: unknown;
  try {
    loaded = LOADERS[library]();
  } catch (err) {
    throw instrumentationLoadError(CONTRIB[library], err);
  }
  // Both contribs: arguments never recorded (`sanitizedStatement`), and a
  // span without a parent too, as Go and Python (ioredis defaults
  // `requireParentSpan` to true, instrumentation-redis to false). No request
  // or response hook: nothing reads command values.
  if (library === 'redis') {
    const { RedisInstrumentation } = loaded as typeof Redis;
    return new RedisInstrumentation({ requireParentSpan: false, dbStatementSerializer: sanitizedStatement });
  }
  const { IORedisInstrumentation } = loaded as typeof IORedis;
  return new IORedisInstrumentation({ requireParentSpan: false, dbStatementSerializer: sanitizedStatement });
}

/**
 * The shared contrib instrumentation of `library`, built on first use: a
 * second instance would register its require hook after the library was
 * handed to require-in-the-middle, which never calls it for that copy. Throws
 * the D14 load error when the contrib module does not load.
 */
export function shared(library: Library): Instrumentation {
  let child = built.get(library);
  if (child === undefined) {
    child = build(library);
    // Off until setup() enables it: InstrumentationBase enables itself in its
    // constructor, bound to the API's global providers.
    child.disable();
    built.set(library, child);
  }
  return child;
}

/** Cached files of `pkg` (every copy in `require.cache`): `file` inside it, or its main entry when that is cached. */
function cachedFiles(pkg: string, file: string): string[] {
  const marker = `${path.sep}node_modules${path.sep}${pkg.split('/').join(path.sep)}${path.sep}`;
  const roots = new Set<string>();
  for (const loaded of Object.keys(require.cache)) {
    const at = loaded.lastIndexOf(marker);
    if (at >= 0) roots.add(loaded.slice(0, at + marker.length));
  }
  const files: string[] = [];
  for (const root of roots) {
    let target: string;
    if (file !== '') {
      target = path.join(root, file);
    } else {
      try {
        target = createRequire(path.join(root, 'package.json')).resolve(root);
      } catch {
        continue; // a copy whose main entry does not resolve was never required as the package
      }
    }
    if (require.cache[target] !== undefined) files.push(target);
  }
  return files;
}

/**
 * Libraries the app loaded before the instrumentation's require hooks existed
 * are unknown to require-in-the-middle, which patches on `require`. Requiring
 * the cached files again by path hands them to the hooks, which patch their
 * prototypes in place, so the app's copy (and clients it already built) is
 * instrumented — as `-sql` and the core (express, grpc-js) do.
 */
function patchPreloaded(libraries: readonly Library[]): void {
  for (const library of libraries) {
    if (preloadChecked.has(library)) continue;
    preloadChecked.add(library);
    for (const [pkg, file] of PATCHED_FILES[library]) {
      for (const cached of cachedFiles(pkg, file)) {
        // eslint-disable-next-line @typescript-eslint/no-require-imports -- re-require so the require hook patches it
        require(cached);
      }
    }
  }
}

function warnSetConfig(): void {
  if (warnedSetConfig) return;
  warnedSetConfig = true;
  diag.warn(`${NAME}: setConfig() is ignored; the configuration is fixed and enabled follows setup()/shutdown()`);
}

/** How a library report is emitted on the first enable (through the helper's DiagLogger). */
export type ReportLevel = 'warn' | 'debug';

/** Test seam: the contrib instrumentations of a result (never part of the API). */
export let childrenForTests: (instrumentation: Instrumentation) => readonly Instrumentation[];

export class RedisInstrumentationImpl implements Instrumentation {
  readonly instrumentationName = NAME;
  readonly instrumentationVersion = VERSION;
  /** The libraries this result instruments. */
  readonly libraries: readonly Library[];
  /** The contrib instrumentation names it covers: the core rejects another instrumentation for them. */
  readonly coveredInstrumentations: readonly string[];
  readonly #children: readonly (readonly [Library, Instrumentation])[];
  readonly #unresolved: readonly (readonly [Library, string, ReportLevel])[];
  #enabled = false;

  static {
    childrenForTests = (i) => (i instanceof RedisInstrumentationImpl ? i.#children.map(([, c]) => c) : []);
  }

  /** `unresolved`: libraries the app dirs did not resolve or whose contrib module failed, with their report (emitted on the first enable). */
  constructor(children: readonly (readonly [Library, Instrumentation])[], unresolved: readonly (readonly [Library, string, ReportLevel])[]) {
    this.#children = children;
    this.#unresolved = unresolved;
    this.libraries = Object.freeze(children.map(([library]) => library));
    this.coveredInstrumentations = Object.freeze(this.libraries.map((library) => CONTRIB[library]));
  }

  enable(): void {
    if (this.#enabled) return;
    this.#enabled = true;
    for (const [, child] of this.#children) {
      const count = enables.get(child) ?? 0;
      enables.set(child, count + 1);
      if (count === 0) child.enable();
    }
    // Reported once the helper's DiagLogger is installed (setup() enables after it).
    if (setConfigCalled) warnSetConfig();
    for (const [, message, level] of this.#unresolved) {
      const key = `${level}:${message}`;
      if (reportedUnresolved.has(key)) continue;
      reportedUnresolved.add(key);
      if (level === 'warn') diag.warn(message);
      else diag.debug(message);
    }
    patchPreloaded(this.libraries);
  }

  disable(): void {
    if (!this.#enabled) return;
    this.#enabled = false;
    for (const [, child] of this.#children) {
      const count = (enables.get(child) ?? 1) - 1;
      enables.set(child, count);
      if (count === 0) child.disable();
    }
  }

  setTracerProvider(tracerProvider: TracerProvider): void {
    for (const [, child] of this.#children) {
      if (boundTracer.get(child) === tracerProvider) continue;
      boundTracer.set(child, tracerProvider);
      child.setTracerProvider(dbTracerProvider(tracerProvider, { legacyKeys: legacyKeysMode(), spanName: commandSpanName, responseStatusCode: redisResponseCode }));
    }
  }

  setMeterProvider(meterProvider: MeterProvider): void {
    // Neither contrib records metrics today (design D-JS14); bound through the
    // same wrapper as `-sql` so a contrib release that adds them gets the
    // legacy key and the generation guard without a change here.
    for (const [, child] of this.#children) {
      const previous = meterGeneration.get(child);
      const generation = {};
      meterGeneration.set(child, generation);
      try {
        child.setMeterProvider(dbMeterProvider(meterProvider, { legacyKeys: legacyKeysMode(), current: () => meterGeneration.get(child) === generation }));
      } catch (err) {
        if (previous === undefined) meterGeneration.delete(child);
        else meterGeneration.set(child, previous);
        throw err;
      }
    }
  }

  setLoggerProvider(loggerProvider: LoggerProvider): void {
    for (const [, child] of this.#children) child.setLoggerProvider?.(loggerProvider);
  }

  /** Ignored (warned once per process, on the next enable): the contrib configurations are fixed by this package and `enabled` follows `enable()`/`disable()`. */
  setConfig(_config: InstrumentationConfig): void {
    setConfigCalled = true;
    if (this.#enabled) warnSetConfig();
  }

  /** `{ enabled }`: the real state of this result. */
  getConfig(): InstrumentationConfig {
    return { enabled: this.#enabled };
  }
}
