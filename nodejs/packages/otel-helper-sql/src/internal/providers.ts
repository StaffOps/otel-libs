/**
 * The TracerProvider and MeterProvider `@staffops/otel-helper-sql` binds each
 * contrib instrumentation to (task B10a, fix round 2026-11-03).
 *
 * The wrappers themselves live in the core's internal entry
 * `@staffops/otel-helper/internal/db` (shared with `-redis` since B10b,
 * 2026-11-03): an ERROR status keeps its code and drops the description
 * (contract D1 row "DB error status", A25: the database's error text can quote
 * a statement value; Go XSAM otelsql sets an empty description, Python's
 * `ext.sql` drops it the same way; the `exception` event is semconv-defined
 * and kept), `error.type` `_OTHER` when absent, the legacy `db.system`
 * (design D-JS9), and the meter generation guard (an instrument created
 * before the latest bind records nothing: instrumentation-pg re-creates its
 * instruments on every `setMeterProvider` and moves the pool `max` between
 * them, which through the helper's forwarding MeterProvider read 0 after a
 * re-setup). This module adds the mysql2 span name (`statementSpanName`).
 */
import type { TracerProvider } from '@opentelemetry/api';
import { ATTR_DB_SYSTEM_NAME, dbTracerProvider as coreDbTracerProvider } from '@staffops/otel-helper/internal/db';

export { dbMeterProvider, type DbMeterOptions } from '@staffops/otel-helper/internal/db';

export const MAX_SPAN_NAME = 32;

/**
 * mysql2 span name from the statement: upstream takes the text before its
 * first space, so `\n  SELECT 1` gives an empty name, `/* c *\/ SELECT 1` the
 * comment, and `SELECT 1;` with a tab a whole statement. Leading whitespace
 * and comments (`--`, `#`, `/* *\/`) are skipped; the first word (letters,
 * digits, `_`) is uppercased and cut at {@link MAX_SPAN_NAME}; with none, the
 * `db.system.name` value (`mysql`). pg already names spans from the operation.
 */
export function statementSpanName(text: unknown, fallback: string): string {
  if (typeof text !== 'string') return fallback;
  let rest = text;
  for (;;) {
    rest = rest.replace(/^\s+/, '');
    if (rest.startsWith('--') || rest.startsWith('#')) {
      const nl = rest.indexOf('\n');
      rest = nl < 0 ? '' : rest.slice(nl + 1);
    } else if (rest.startsWith('/*')) {
      const end = rest.indexOf('*/', 2);
      rest = end < 0 ? '' : rest.slice(end + 2);
    } else {
      break;
    }
  }
  const word = /^[A-Za-z0-9_]+/.exec(rest)?.[0];
  if (word === undefined) return fallback;
  return word.slice(0, MAX_SPAN_NAME).replace(/[a-z]+/g, (m) => m.toUpperCase());
}

/** Options of {@link dbTracerProvider}. */
export interface DbTracerOptions {
  /** Copy `db.system.name` into the legacy `db.system` at span start. */
  readonly legacyKeys: boolean;
  /** Rename spans with {@link statementSpanName} (mysql2). */
  readonly normalizeSpanName: boolean;
}

/** A TracerProvider whose tracers apply {@link DbTracerOptions} and drop ERROR descriptions (core `internal/db`). */
export function dbTracerProvider(provider: TracerProvider, opts: DbTracerOptions): TracerProvider {
  return coreDbTracerProvider(provider, {
    legacyKeys: opts.legacyKeys,
    spanName: opts.normalizeSpanName
      ? (requested, attrs) => {
          const system = attrs?.[ATTR_DB_SYSTEM_NAME];
          return statementSpanName(attrs?.['db.query.text'], typeof system === 'string' ? system : requested);
        }
      : undefined,
  });
}
