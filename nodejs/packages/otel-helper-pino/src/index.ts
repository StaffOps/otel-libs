/**
 * `@staffops/otel-helper-pino` — pino destination that sends records through @staffops/otel-helper (opt-in).
 * Scaffolding (task B1a): the implementation lands in task B10d.
 */

/** Options accepted by {@link createPinoDestination}; defined in task B10d. */
export type PinoDestinationOptions = Readonly<Record<string, never>>;

/**
 * Placeholder for the factory the app passes to the helper. Throws until task
 * B10d so a premature use is loud instead of silently doing nothing.
 */
export function createPinoDestination(_options?: PinoDestinationOptions): never {
  throw new Error('@staffops/otel-helper-pino: createPinoDestination() is not implemented yet (task B10d of specs/nodejs-otel-helper)');
}
