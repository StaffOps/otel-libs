/**
 * `@staffops/otel-helper-redis` — Redis (redis, ioredis) instrumentation for @staffops/otel-helper (opt-in).
 * Scaffolding (task B1a): the implementation lands in task B10b.
 */

/** Options accepted by {@link redisInstrumentation}; defined in task B10b. */
export type RedisInstrumentationOptions = Readonly<Record<string, never>>;

/**
 * Placeholder for the factory the app passes to the helper. Throws until task
 * B10b so a premature use is loud instead of silently doing nothing.
 */
export function redisInstrumentation(_options?: RedisInstrumentationOptions): never {
  throw new Error('@staffops/otel-helper-redis: redisInstrumentation() is not implemented yet (task B10b of specs/nodejs-otel-helper)');
}
