/**
 * The `Instrumentation` returned by `sqlInstrumentation()` and the per-process
 * state it shares (on the core `internal/contrib` registry).
 *
 * One per process: the contrib instrumentations are built once and shared by
 * every result, and so are their enable state and bindings. Several results
 * enable a contrib instrumentation once (a count per child); the last one
 * disabled turns it off. Pass one `sqlInstrumentation()` to each `setup()`.
 */
import * as path from 'node:path';
import type { Instrumentation } from '@opentelemetry/instrumentation';
import type * as Mysql2 from '@opentelemetry/instrumentation-mysql2';
import type * as Pg from '@opentelemetry/instrumentation-pg';
import { contribRegistry } from '@staffops/otel-helper/internal/contrib';
import { legacyKeysMode } from '@staffops/otel-helper/internal/db';
import { instrumentationLoadError, LIBRARIES, type Library } from './libraries';
import { dbMeterProvider, dbTracerProvider } from './providers';

export const NAME = '@staffops/otel-helper-sql';
/** Version of this package; a test keeps it equal to package.json `version`. */
export const VERSION = '0.3.0-dev.0';

/** The contrib module and its `instrumentationName` per library. */
export const CONTRIB: Readonly<Record<Library, string>> = Object.freeze({
  pg: '@opentelemetry/instrumentation-pg',
  mysql2: '@opentelemetry/instrumentation-mysql2',
});

/* eslint-disable @typescript-eslint/no-require-imports -- loaded on demand, so a failure is reported in D14 words */
const LOADERS: Readonly<Record<Library, () => unknown>> = {
  pg: () => require('@opentelemetry/instrumentation-pg') as typeof Pg,
  mysql2: () => require('@opentelemetry/instrumentation-mysql2') as typeof Mysql2,
};
/* eslint-enable @typescript-eslint/no-require-imports */

/**
 * Files each library's instrumentation patches, required again on the first
 * enable when they are cached (core `internal/contrib`): [package, file inside
 * it, '' = its main entry].
 */
const PATCHED_FILES: Readonly<Record<Library, readonly (readonly [string, string])[]>> = {
  pg: [['pg', ''], ['pg', path.join('lib', 'client.js')], ['pg', path.join('lib', 'native', 'client.js')], ['pg-pool', '']],
  mysql2: [['mysql2', ''], ['mysql2', 'promise.js'], ['mysql2', path.join('lib', 'connection.js')]],
};

/**
 * `OTEL_SEMCONV_STABILITY_OPT_IN` database mode, read once per process by the
 * first `sqlInstrumentation()` or `redisInstrumentation()` (as Go's first
 * `Open`): the core's `internal/db` keeps it.
 */
export { legacyKeysMode };

function build(library: Library): Instrumentation {
  // Not `exports`: in the CommonJS output that would shadow the module's own.
  let loaded: unknown;
  try {
    loaded = LOADERS[library]();
  } catch (err) {
    throw instrumentationLoadError(CONTRIB[library], err);
  }
  if (library === 'pg') {
    const { PgInstrumentation } = loaded as typeof Pg;
    // Bound values (`db.postgresql.values`) are never recorded; nothing is added to the SQL text.
    return new PgInstrumentation({ enhancedDatabaseReporting: false, addSqlCommenterCommentToQueries: false });
  }
  const { MySQL2Instrumentation } = loaded as typeof Mysql2;
  // Upstream formats the bound values into `db.query.text` unless masking is
  // on; the identity mask records the statement as written (placeholders
  // kept), as Go, Python and instrumentation-pg do.
  return new MySQL2Instrumentation({ maskStatement: true, maskStatementHook: (query: string) => query, addSqlCommenterCommentToQueries: false });
}

/**
 * The per-process state: shared contrib instances, counted enables, bindings,
 * the re-require of preloaded files and the one-time reports (core
 * `internal/contrib`).
 */
export const registry = contribRegistry<Library>({
  name: NAME,
  version: VERSION,
  libraries: LIBRARIES,
  contrib: CONTRIB,
  packages: { pg: ['pg'], mysql2: ['mysql2'] },
  build,
  patchedFiles: PATCHED_FILES,
  tracerProvider: (library, provider) => dbTracerProvider(provider, { legacyKeys: legacyKeysMode(), normalizeSpanName: library === 'mysql2' }),
  // Always a new bind (core): instrumentation-pg re-creates its instruments
  // and moves the pool max onto them; the older instruments go quiet.
  meterProvider: (_library, provider, current) => dbMeterProvider(provider, { legacyKeys: legacyKeysMode(), current }),
});

/** Test seam: the contrib instrumentations of a result (never part of the API). */
export const childrenForTests = (instrumentation: unknown): readonly Instrumentation[] => registry.children(instrumentation);
