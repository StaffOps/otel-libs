/**
 * The TracerProvider and MeterProvider `@staffops/otel-helper-sql` binds each
 * contrib instrumentation to.
 *
 * The wrappers themselves live in the core's internal entry
 * `@staffops/otel-helper/internal/db` (shared with `-redis`): an ERROR status keeps its code and drops the description
 * (contract D1 row "DB error status": the database's error text can quote
 * a statement value; Go XSAM otelsql sets an empty description, Python's
 * `ext.sql` drops it the same way; the `exception` event is semconv-defined
 * and kept), `error.type` `_OTHER` when absent, the legacy `db.system`
 * (design D-JS9), and the meter generation guard (an instrument created
 * before the latest bind records nothing: instrumentation-pg re-creates its
 * instruments on every `setMeterProvider` and moves the pool `max` between
 * them, which through the helper's forwarding MeterProvider read 0 after a
 * re-setup). This module adds the mysql2 span name (`statementSpanName`)
 * and the response status codes of failed queries (`status-codes.ts`, design
 * D16, task A44: pg through the recorded exception, mysql2 through the held
 * error).
 */
import type { TracerProvider } from '@opentelemetry/api';
import { ATTR_DB_SYSTEM_NAME, dbTracerProvider as coreDbTracerProvider } from '@staffops/otel-helper/internal/db';
import { mysql2PendingStatusCode, pgStatusCode } from './status-codes';

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
  /**
   * Where a failed query's response status code is read (D16, A44): `pg` from
   * the recorded exception, `mysql2` from the held error. Unset: none
   * (`error.type=_OTHER`).
   */
  readonly statusCodes?: 'pg' | 'mysql2' | undefined;
}

/** A TracerProvider whose tracers apply {@link DbTracerOptions} and drop ERROR descriptions (core `internal/db`). */
export function dbTracerProvider(provider: TracerProvider, opts: DbTracerOptions): TracerProvider {
  return coreDbTracerProvider(provider, {
    legacyKeys: opts.legacyKeys,
    // The legacy db.statement next to db.query.text in the legacy-keys mode (A30, ruling Q5, 2026-11-06).
    legacyStatement: true,
    spanName: opts.normalizeSpanName
      ? (requested, attrs) => {
          const system = attrs?.[ATTR_DB_SYSTEM_NAME];
          return statementSpanName(attrs?.['db.query.text'], typeof system === 'string' ? system : requested);
        }
      : undefined,
    exceptionStatusCode: opts.statusCodes === 'pg' ? pgStatusCode : undefined,
    pendingStatusCode: opts.statusCodes === 'mysql2' ? mysql2PendingStatusCode : undefined,
    // A db.response.status_code already on the span becomes its error.type (D16, as Python).
    copyPresetStatusCode: true,
  });
}
