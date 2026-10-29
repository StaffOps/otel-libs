/**
 * `@staffops/otel-helper-winston` — winston transport that sends records through @staffops/otel-helper (opt-in).
 * Scaffolding (task B1a): the implementation lands in task B10d.
 */

/** Options accepted by {@link OtelHelperTransport}; defined in task B10d. */
export type OtelHelperTransportOptions = Readonly<Record<string, never>>;

/**
 * Placeholder for the transport the app passes to winston. Its constructor
 * throws until task B10d so a premature use is loud.
 */
// eslint-disable-next-line @typescript-eslint/no-extraneous-class -- placeholder; the winston transport methods land in B10d
export class OtelHelperTransport {
  constructor(_options?: OtelHelperTransportOptions) {
    throw new Error('@staffops/otel-helper-winston: OtelHelperTransport is not implemented yet (task B10d of specs/nodejs-otel-helper)');
  }
}
