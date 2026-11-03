/**
 * Database semantic-convention migration (contract R10, Node design D-JS9).
 *
 * The contrib instrumentations emit only the current keys (`db.system.name`,
 * semconv v1.43) and ignore `OTEL_SEMCONV_STABILITY_OPT_IN`. The other
 * helpers also emit the legacy `db.system` key (semconv v1.26) until 1.0, on
 * spans and on measurements, unless the variable holds `database` without
 * `database/dup` (Go `ext/otelsql` `legacyKeysEnabled`; Python normalizes the
 * variable to the same rule since A25). The helper adds the legacy key through
 * the tracer and meter it binds each instrumentation to (`providers.ts`) — not
 * a SpanProcessor, which could not reach the metric attributes.
 *
 * The rule is implemented once, in the core's internal entry
 * `@staffops/otel-helper/internal/db` (shared with `-redis` since B10b,
 * 2026-11-03); re-exported here for this package's modules and tests.
 */
export { ATTR_DB_SYSTEM, ATTR_DB_SYSTEM_NAME, ENV_SEMCONV_OPT_IN, LEGACY_DB_SYSTEM, legacyKeysEnabled, withLegacyKey } from '@staffops/otel-helper/internal/db';
