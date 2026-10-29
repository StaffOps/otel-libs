/**
 * `@staffops/otel-helper-sql` — SQL (pg, mysql2) instrumentation for @staffops/otel-helper (opt-in).
 * Scaffolding (task B1a): the implementation lands in task B10a.
 */

/** Options accepted by {@link sqlInstrumentation}; defined in task B10a. */
export type SqlInstrumentationOptions = Readonly<Record<string, never>>;

/**
 * Placeholder for the factory the app passes to the helper. Throws until task
 * B10a so a premature use is loud instead of silently doing nothing.
 */
export function sqlInstrumentation(_options?: SqlInstrumentationOptions): never {
  throw new Error('@staffops/otel-helper-sql: sqlInstrumentation() is not implemented yet (task B10a of specs/nodejs-otel-helper)');
}
