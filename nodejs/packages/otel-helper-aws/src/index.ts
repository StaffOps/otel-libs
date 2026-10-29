/**
 * `@staffops/otel-helper-aws` — AWS SDK v3 instrumentation for @staffops/otel-helper (opt-in).
 * Scaffolding (task B1a): the implementation lands in task B10c.
 */

/** Options accepted by {@link awsInstrumentation}; defined in task B10c. */
export type AwsInstrumentationOptions = Readonly<Record<string, never>>;

/**
 * Placeholder for the factory the app passes to the helper. Throws until task
 * B10c so a premature use is loud instead of silently doing nothing.
 */
export function awsInstrumentation(_options?: AwsInstrumentationOptions): never {
  throw new Error('@staffops/otel-helper-aws: awsInstrumentation() is not implemented yet (task B10c of specs/nodejs-otel-helper)');
}
