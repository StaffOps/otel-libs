/**
 * `@staffops/otel-helper-sql` — SQL instrumentation for `@staffops/otel-helper`
 * (opt-in; task B10a, spec JS8, design D-JS9). Go `ext/otelsql`, Python
 * `staffops_otel_helper.ext.sql`.
 *
 * ```js
 * const { setup } = require('@staffops/otel-helper');
 * const { sqlInstrumentation } = require('@staffops/otel-helper-sql');
 * setup({ instrumentations: [sqlInstrumentation()] });
 * const pg = require('pg'); // after setup(): queries produce spans and metrics
 * ```
 *
 * Wraps the official contrib instrumentations `@opentelemetry/instrumentation-pg`
 * (`pg`, `pg-pool`) and `@opentelemetry/instrumentation-mysql2` (`mysql2`).
 * Spans and measurements carry `db.system.name` and, until 1.0, the legacy
 * `db.system` key (see `OTEL_SEMCONV_STABILITY_OPT_IN`, design D-JS9). Never
 * recorded: bound values (pg `enhancedDatabaseReporting` off; mysql2 records
 * the statement as written, never formatted with its values), the database's
 * error text in the span status (contract D1 row "DB error status", A25), and
 * connection strings or credentials (only host, port and database name).
 *
 * One per process: every result shares the same contrib instrumentations,
 * their enable state and bindings (`internal/sql-instrumentation.ts`). Pass
 * one `sqlInstrumentation()` to each `setup()`; do not also pass a raw
 * `PgInstrumentation` or `MySQL2Instrumentation` (`setup()` rejects it: it
 * would patch twice and drop the mysql2 statement mask).
 */
import type { Instrumentation } from '@opentelemetry/instrumentation';
import { LIBRARIES, type Library } from './internal/libraries';
import { legacyKeysMode, registry } from './internal/sql-instrumentation';

export { VERSION } from './internal/sql-instrumentation';

/** Options of {@link sqlInstrumentation}. */
export interface SqlInstrumentationOptions {
  /**
   * Instrument `pg` (node-postgres, `pg-pool` included). Default: yes; when
   * `pg` does not resolve from the app it is still instrumented if the app
   * loads it (a `diag` debug line says so). `true`: yes, and a one-time WARN
   * when it does not resolve; `false`: no.
   */
  readonly pg?: boolean | undefined;
  /** Instrument `mysql2` (callback and promise APIs). Values as {@link pg}. */
  readonly mysql2?: boolean | undefined;
}

/** What {@link sqlInstrumentation} returns: one `Instrumentation` covering every selected library. */
export interface SqlInstrumentation extends Instrumentation {
  /** The libraries this instance instruments (`pg`, `mysql2`). */
  readonly libraries: readonly Library[];
  /** The contrib instrumentation names it covers (`setup()` rejects another instrumentation for them). */
  readonly coveredInstrumentations: readonly string[];
}

function typeName(value: unknown): string {
  if (value === null) return 'null';
  if (Array.isArray(value)) return 'array';
  return typeof value;
}

function checkOptions(options: unknown): SqlInstrumentationOptions {
  if (options === undefined) return {};
  const proto: unknown = typeof options === 'object' && options !== null && !Array.isArray(options) ? Object.getPrototypeOf(options) : undefined;
  if (proto !== Object.prototype && proto !== null) throw new TypeError(`sqlInstrumentation(): options must be an object, got ${typeName(options)}`);
  const o = options as Record<string, unknown>;
  const extra = Object.keys(o).filter((k) => o[k] !== undefined && !(LIBRARIES as readonly string[]).includes(k));
  if (extra.length > 0) throw new TypeError(`sqlInstrumentation(): options has unknown key(s) ${extra.sort().join(', ')}`);
  for (const library of LIBRARIES) {
    const value = o[library];
    if (value !== undefined && typeof value !== 'boolean') throw new TypeError(`sqlInstrumentation(): ${library} must be a boolean, got ${typeName(value)}`);
  }
  return { pg: o.pg as boolean | undefined, mysql2: o.mysql2 as boolean | undefined };
}

/**
 * SQL instrumentation for `setup({ instrumentations: [sqlInstrumentation()] })`.
 * Call it — and `setup()` — before the app loads `pg` or `mysql2` (a library
 * loaded earlier is still patched on the first enable). The first call of
 * this or `redisInstrumentation()` reads `OTEL_SEMCONV_STABILITY_OPT_IN` for
 * the whole process (core `internal/db`, since B10b).
 *
 * @throws TypeError on invalid options; Error (D14 wording) when the contrib
 *   instrumentation of a library named `true` does not load, or when none of
 *   the selected libraries' contrib instrumentations loads.
 */
export function sqlInstrumentation(options?: SqlInstrumentationOptions): SqlInstrumentation {
  const o = checkOptions(options);
  if (o.pg === false && o.mysql2 === false) throw new TypeError('sqlInstrumentation(): pg and mysql2 are both false; nothing to instrument');
  legacyKeysMode();
  return registry.instrument(o);
}
