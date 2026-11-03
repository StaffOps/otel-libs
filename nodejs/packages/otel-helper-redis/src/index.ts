/**
 * `@staffops/otel-helper-redis` — Redis instrumentation for
 * `@staffops/otel-helper` (opt-in; task B10b, spec JS8, design D-JS9). Go
 * `ext/otelredis`, Python `staffops_otel_helper.ext.redis`.
 *
 * ```js
 * const { setup } = require('@staffops/otel-helper');
 * const { redisInstrumentation } = require('@staffops/otel-helper-redis');
 * setup({ instrumentations: [redisInstrumentation()] });
 * const { createClient } = require('redis'); // after setup(): commands produce spans
 * ```
 *
 * Span names are the command, uppercased (`SET`; connection spans `connect`).
 *
 * Wraps the official contrib instrumentations `@opentelemetry/instrumentation-redis`
 * (node-redis: `redis` 2–3, `@redis/client` 1/5/6 — that is `redis` 4, 5 and
 * 6) and `@opentelemetry/instrumentation-ioredis` (`ioredis` 2–6). Spans carry
 * `db.system.name=redis` and, until 1.0, the legacy `db.system` (see
 * `OTEL_SEMCONV_STABILITY_OPT_IN`, design D-JS9), `db.operation.name`,
 * `server.address`/`server.port`, and `db.query.text` as the command name and
 * one `?` per argument (`SET ? ?`, as Python). Never recorded: command
 * arguments (keys, values, `AUTH` passwords), the server's error text in the
 * span status (contract D1 row "DB error status", A25), and connection
 * strings or credentials (only host and port).
 *
 * One per process: every result shares the same contrib instrumentations,
 * their enable state and bindings (`internal/redis-instrumentation.ts`). Pass
 * one `redisInstrumentation()` to each `setup()`; do not also pass a raw
 * `RedisInstrumentation` or `IORedisInstrumentation` (`setup()` rejects it:
 * it would patch twice and record arguments).
 */
import type { Instrumentation } from '@opentelemetry/instrumentation';
import { LIBRARIES, type Library } from './internal/libraries';
import { legacyKeysMode, registry } from './internal/redis-instrumentation';

export { VERSION } from './internal/redis-instrumentation';

/** Options of {@link redisInstrumentation}. */
export interface RedisInstrumentationOptions {
  /**
   * Instrument node-redis (`redis`, `@redis/client`). Default: yes; when
   * neither resolves from the app it is still instrumented if the app loads
   * it (a `diag` debug line says so). `true`: yes, and a one-time WARN when
   * it does not resolve; `false`: no.
   */
  readonly redis?: boolean | undefined;
  /** Instrument `ioredis`. Values as {@link redis}. */
  readonly ioredis?: boolean | undefined;
}

/** What {@link redisInstrumentation} returns: one `Instrumentation` covering every selected library. */
export interface RedisInstrumentation extends Instrumentation {
  /** The libraries this instance instruments (`redis`, `ioredis`). */
  readonly libraries: readonly Library[];
  /** The contrib instrumentation names it covers (`setup()` rejects another instrumentation for them). */
  readonly coveredInstrumentations: readonly string[];
}

function typeName(value: unknown): string {
  if (value === null) return 'null';
  if (Array.isArray(value)) return 'array';
  return typeof value;
}

function checkOptions(options: unknown): RedisInstrumentationOptions {
  if (options === undefined) return {};
  const proto: unknown = typeof options === 'object' && options !== null && !Array.isArray(options) ? Object.getPrototypeOf(options) : undefined;
  if (proto !== Object.prototype && proto !== null) throw new TypeError(`redisInstrumentation(): options must be an object, got ${typeName(options)}`);
  const o = options as Record<string, unknown>;
  const extra = Object.keys(o).filter((k) => o[k] !== undefined && !(LIBRARIES as readonly string[]).includes(k));
  if (extra.length > 0) throw new TypeError(`redisInstrumentation(): options has unknown key(s) ${extra.sort().join(', ')}`);
  for (const library of LIBRARIES) {
    const value = o[library];
    if (value !== undefined && typeof value !== 'boolean') throw new TypeError(`redisInstrumentation(): ${library} must be a boolean, got ${typeName(value)}`);
  }
  return { redis: o.redis as boolean | undefined, ioredis: o.ioredis as boolean | undefined };
}

/**
 * Redis instrumentation for `setup({ instrumentations: [redisInstrumentation()] })`.
 * Call it — and `setup()` — before the app loads `redis` or `ioredis` (a
 * library loaded earlier is still patched on the first enable). The first
 * call of this or `sqlInstrumentation()` reads `OTEL_SEMCONV_STABILITY_OPT_IN`
 * for the whole process.
 *
 * @throws TypeError on invalid options; Error (D14 wording) when the contrib
 *   instrumentation of a library named `true` does not load, or when none of
 *   the selected libraries' contrib instrumentations loads.
 */
export function redisInstrumentation(options?: RedisInstrumentationOptions): RedisInstrumentation {
  const o = checkOptions(options);
  if (o.redis === false && o.ioredis === false) throw new TypeError('redisInstrumentation(): redis and ioredis are both false; nothing to instrument');
  legacyKeysMode();
  return registry.instrument(o);
}
