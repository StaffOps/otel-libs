/**
 * The `Instrumentation` returned by `redisInstrumentation()` and the
 * per-process state it shares (the `-sql` model, on the core
 * `internal/contrib` registry).
 *
 * One per process: the contrib instrumentations are built once and shared by
 * every result, and so are their enable state and bindings. Several results
 * enable a contrib instrumentation once (a count per child); the last one
 * disabled turns it off. Pass one `redisInstrumentation()` to each `setup()`.
 */
import { join } from 'node:path';
import type { Instrumentation } from '@opentelemetry/instrumentation';
import type * as IORedis from '@opentelemetry/instrumentation-ioredis';
import type * as Redis from '@opentelemetry/instrumentation-redis';
import { contribRegistry } from '@staffops/otel-helper/internal/contrib';
import { dbMeterProvider, dbTracerProvider, legacyKeysMode } from '@staffops/otel-helper/internal/db';
import { instrumentationLoadError, LIBRARIES, PACKAGES, type Library } from './libraries';
import { commandSpanName, redisResponseCode, sanitizedStatement } from './statement';

export const NAME = '@staffops/otel-helper-redis';
/** Version of this package; a test keeps it equal to package.json `version`. */
export const VERSION = '0.3.0-rc.1';

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

const NODE_REDIS_FILES = ['commander.js', 'client/index.js', 'client/multi-command.js', 'cluster/index.js', 'cluster/multi-command.js'].map((f) => join('dist', 'lib', ...f.split('/')));

/**
 * Files each library's instrumentation patches, required again on the first
 * enable when they are cached (core `internal/contrib`): [package, file inside
 * it, '' = its main entry]. node-redis 4+ is patched in `@redis/client`
 * (`@node-redis/client` for 4.0 pre-releases), node-redis 2–3 in `redis`.
 */
const PATCHED_FILES: Readonly<Record<Library, readonly (readonly [string, string])[]>> = {
  redis: [...['@redis/client', '@node-redis/client'].flatMap((pkg) => NODE_REDIS_FILES.map((file) => [pkg, file] as const)), ['redis', '']],
  ioredis: [['ioredis', '']],
};

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
  return oneSpanPerCommand(new IORedisInstrumentation({ requireParentSpan: false, dbStatementSerializer: sanitizedStatement }));
}

type SendCommand = (this: unknown, ...args: unknown[]) => unknown;
interface TraceSendCommand {
  _traceSendCommand?: (original: SendCommand, moduleVersion: string | undefined) => SendCommand;
}

/**
 * ioredis sends a command a second time through `Redis.prototype.sendCommand`
 * when it replays its offline queue (a command issued before `ready`, or
 * resent after a reconnect): the contrib would start a second span for the
 * same command, both ended by the reply. The first call's span is kept — it
 * covers the whole latency, queueing included — and a command object already
 * seen goes straight to ioredis's own `sendCommand`. Wraps the contrib's
 * `_traceSendCommand` (its patch factory, called each time ioredis is
 * patched); when a contrib release renames it, the instance is returned as is
 * and the redis tests (`one span per command issued before ready`) fail.
 */
export function oneSpanPerCommand<I extends Instrumentation>(instrumentation: I): I {
  const target = instrumentation as I & TraceSendCommand;
  const traceSendCommand = target._traceSendCommand;
  if (typeof traceSendCommand !== 'function') return instrumentation;
  const sent = new WeakSet<object>();
  target._traceSendCommand = function patchFactory(this: unknown, original, moduleVersion) {
    const traced = traceSendCommand.call(target, original, moduleVersion);
    return function sendCommand(this: unknown, ...args: unknown[]): unknown {
      const command = args[0];
      if (command === null || typeof command !== 'object') return traced.apply(this, args);
      if (sent.has(command)) return original.apply(this, args);
      sent.add(command);
      return traced.apply(this, args);
    };
  };
  return instrumentation;
}

/**
 * The per-process state: shared contrib instances, counted enables, bindings,
 * the re-require of preloaded files and the one-time reports (core
 * `internal/contrib`).
 */
export const registry = contribRegistry<Library>({
  name: NAME,
  version: VERSION,
  libraries: LIBRARIES,
  contrib: CONTRIB,
  packages: PACKAGES,
  build,
  patchedFiles: PATCHED_FILES,
  tracerProvider: (_library, provider) =>
    dbTracerProvider(provider, { legacyKeys: legacyKeysMode(), spanName: commandSpanName, responseStatusCode: redisResponseCode }),
  // Neither contrib records metrics today (design D-JS14); bound through the
  // same wrapper as `-sql` so a contrib release that adds them gets the
  // legacy key and the generation guard without a change here.
  meterProvider: (_library, provider, current) => dbMeterProvider(provider, { legacyKeys: legacyKeysMode(), current }),
});

/** Test seam: the contrib instrumentations of a result (never part of the API). */
export const childrenForTests = (instrumentation: unknown): readonly Instrumentation[] => registry.children(instrumentation);
