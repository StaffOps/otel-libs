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
 */
import type { Attributes } from '@opentelemetry/api';

export const ENV_SEMCONV_OPT_IN = 'OTEL_SEMCONV_STABILITY_OPT_IN';
export const ATTR_DB_SYSTEM_NAME = 'db.system.name';
export const ATTR_DB_SYSTEM = 'db.system';

/**
 * `db.system.name` (v1.43) → legacy `db.system` (v1.26) for the systems this
 * package instruments. Both values are identical for PostgreSQL and MySQL
 * (Go maps the same pairs, `ext/otelsql` `systemAttributes`); an unknown value
 * is copied as it is.
 */
export const LEGACY_DB_SYSTEM: Readonly<Record<string, string>> = Object.freeze({
  postgresql: 'postgresql',
  mysql: 'mysql',
});

// The contract trim set (design D1 "Trimmed character set") and ASCII-only
// case folding (D1 "ASCII case folding"), as the core resolver.
const LEADING = /^[ \t\n\r\v\f]+/;
const TRAILING = /[ \t\n\r\v\f]+$/;

function token(raw: string): string {
  return raw.replace(LEADING, '').replace(TRAILING, '').replace(/[A-Z]+/g, (m) => m.toLowerCase());
}

/**
 * Whether the legacy `db.system` key is emitted: yes unless the value holds
 * `database` without `database/dup` (Go `legacyKeysEnabled`). Unset, empty or
 * without database tokens: yes (the helper default is `http,database/dup`).
 */
export function legacyKeysEnabled(value: string | undefined = process.env[ENV_SEMCONV_OPT_IN]): boolean {
  if (value === undefined) return true;
  const tokens = new Set(value.split(',').map(token));
  return tokens.has('database/dup') || !tokens.has('database');
}

/** `attributes` plus the legacy `db.system` when they carry `db.system.name` and no `db.system`. */
export function withLegacyKey<A extends Attributes | undefined>(attributes: A): A;
export function withLegacyKey(attributes: Attributes | undefined): Attributes | undefined {
  if (attributes === undefined) return undefined;
  const name = attributes[ATTR_DB_SYSTEM_NAME];
  if (typeof name !== 'string' || attributes[ATTR_DB_SYSTEM] !== undefined) return attributes;
  return { ...attributes, [ATTR_DB_SYSTEM]: LEGACY_DB_SYSTEM[name] ?? name };
}
