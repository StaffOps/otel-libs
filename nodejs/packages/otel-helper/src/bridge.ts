/**
 * `@staffops/otel-helper/internal/bridge` — NOT public API (design D-JS6,
 * task B10d). The entry the opt-in log bridges `@staffops/otel-helper-pino`
 * and `@staffops/otel-helper-winston` write their records through, so they
 * apply the core's per-record rule (D6) and stdout shape instead of a copy
 * of them. Those packages peer-depend on the core at the same version; this
 * entry may change in any release, so applications must not import it.
 *
 * @internal
 * @packageDocumentation
 */
import { context, ROOT_CONTEXT, type Context } from '@opentelemetry/api';

export { LOG_LEVELS } from './internal/log-level';
export { emitBridgeRecord, recordEnabled } from './internal/logging';
export { flushBridgeRecords } from './internal/setup';
export type { BridgeRecord, LogAttributes, RecordLevel } from './internal/logging';

/**
 * The root context (no span): a bridged record delivered without its call's
 * context is written uncorrelated with it.
 *
 * @internal
 */
export { ROOT_CONTEXT };

/**
 * The context active now, captured by a bridge at the logging call (the
 * bridges do not depend on `@opentelemetry/api` themselves).
 *
 * @internal
 */
export function activeContext(): Context {
  return context.active();
}
