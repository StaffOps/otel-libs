/**
 * The response status code of a failed SQL call (contract design D16,
 * requirement R12, task A44): `db.response.status_code` and `error.type` on
 * the span, through the core `internal/db` hooks `exceptionStatusCode` (pg)
 * and `pendingStatusCode` (mysql2). Only bounded shapes are accepted; message
 * text never reaches an attribute (ADR-014, contract D1 row "DB error
 * status"). Anything else gives the core's `error.type=_OTHER`.
 *
 * - pg: instrumentation-pg 0.77 records the sanitized message
 *   `PostgreSQL error of type '<name>' occurred (code: <code>)` as the
 *   exception, always before `setStatus`; {@link pgStatusCode} reads the code
 *   from it. A socket code (`EPIPE`, `ECONNREFUSED`) or the upstream
 *   placeholder `UNKNOWN` is not a SQLSTATE.
 * - mysql2: instrumentation-mysql2 0.70 never records the error object, so
 *   {@link holdMysql2Errors} overrides the instance's private
 *   `_patchCallbackQuery` (owner decision 3) to hold it while upstream ends
 *   the span; {@link mysql2PendingStatusCode} reads it in `setStatus`. The
 *   streaming form (`connection.query(sql)` without a callback) ends its span
 *   from the `error` event, not through that method: `_OTHER` (documented).
 *
 * Both seams are on the Node.js SDK-bump checklist of the `release` skill and
 * fail a seam test in `test/sql.test.cjs` on a bump that changes them.
 */
import { reportOnce } from '@staffops/otel-helper/internal/contrib';

/** A SQLSTATE: five of `0-9A-Z`; {@link sqlState} also requires one digit. */
const SQLSTATE = /^[0-9A-Z]{5}$/;
const DIGIT = /[0-9]/;

/**
 * `value` when it is a PostgreSQL SQLSTATE: `^[0-9A-Z]{5}$` with at least one
 * digit (every PostgreSQL 17 code has one; this rejects `EPIPE`). Else undefined.
 * A shape check only: a five-character Node error code with a digit
 * (`E2BIG`) would be accepted as a SQLSTATE; it stays bounded (one value).
 */
export function sqlState(value: unknown): string | undefined {
  return typeof value === 'string' && SQLSTATE.test(value) && DIGIT.test(value) ? value : undefined;
}

/** instrumentation-pg's sanitized exception message (`utils.cjs` `sanitizedErrorMessage`). */
const PG_SANITIZED = /^PostgreSQL error of type '[\s\S]*' occurred \(code: ([^()]*)\)$/;

/**
 * The SQLSTATE in instrumentation-pg's sanitized exception message, or
 * undefined (another shape, a socket code, `UNKNOWN`). Only a string is read:
 * an `Error` exception would carry the server's text.
 */
export function pgStatusCode(exception: unknown): string | undefined {
  if (typeof exception !== 'string') return undefined;
  return sqlState(PG_SANITIZED.exec(exception)?.[1]);
}

/**
 * The MySQL / MariaDB server error number of `err` as a decimal string
 * (`1062`), or undefined. Accepted: an integer `errno` 1–65535 outside the
 * client range 2000–2999 (`CR_*`: 2003 connection refused, 2013 server
 * lost), with a string `sqlState` (mysql2 sets both from a server error
 * packet). A Node system error (negative `errno`, a `syscall`, no
 * `sqlState`) is never read. Never throws.
 */
export function mysqlStatusCode(err: unknown): string | undefined {
  if (typeof err !== 'object' || err === null) return undefined;
  try {
    const e = err as { readonly errno?: unknown; readonly sqlState?: unknown; readonly syscall?: unknown };
    const { errno } = e;
    if (typeof e.sqlState !== 'string' || typeof e.syscall === 'string') return undefined;
    if (typeof errno !== 'number' || !Number.isInteger(errno) || errno < 1 || errno > 65535) return undefined;
    return errno >= 2000 && errno <= 2999 ? undefined : String(errno);
  } catch {
    // A getter that throws: no code.
    return undefined;
  }
}

/** The failure mysql2 is ending a span with right now (set only inside upstream's endSpan). */
let held: { error: unknown } | undefined;

/**
 * The code of the mysql2 failure being recorded (read once by the core's
 * `setStatus(ERROR)`; the slot is cleared by the read), or undefined.
 */
export function mysql2PendingStatusCode(): string | undefined {
  const current = held;
  held = undefined;
  return current === undefined ? undefined : mysqlStatusCode(current.error);
}

/**
 * The error object of the mysql2 failure being recorded (the A36 record's
 * detail), or undefined. Read before {@link mysql2PendingStatusCode}, which
 * clears the slot; this read does not.
 */
export function mysql2PendingError(): unknown {
  return held?.error;
}

/** The name in instrumentation-pg's sanitized text (no quote in it). */
const PG_SANITIZED_NAME = /^PostgreSQL error of type '([^']*)' occurred \(code: [^()]*\)$/;

/**
 * The `exception.type` of instrumentation-pg's recorded exception (A36): the
 * name in its sanitized text `PostgreSQL error of type '<name>' occurred
 * (code: <code>)`. pg-protocol names a server error `error` (the protocol
 * message name, `DatabaseError.name`); that is reported as `DatabaseError`,
 * the class, which says what it is. Any other name as it is (`Error` for a
 * socket error); undefined for another shape (the D15 rule then applies).
 */
export function pgExceptionType(exception: unknown): string | undefined {
  if (typeof exception !== 'string') return undefined;
  const name = PG_SANITIZED_NAME.exec(exception)?.[1];
  if (name === undefined || name === '') return undefined;
  return name === 'error' ? 'DatabaseError' : name;
}

type EndSpan = (...args: unknown[]) => unknown;
type PatchCallbackQuery = (this: unknown, endSpan: EndSpan) => unknown;

/** The once-per-process key of the missing-seam warning. */
export const MISSING_SEAM_KEY = '@staffops/otel-helper-sql:mysql2-patch-callback-query';

/**
 * Overrides `instance._patchCallbackQuery` (instrumentation-mysql2's private
 * method that wraps the query callback with `endSpan`) so the error passed to
 * `endSpan` is held while it runs: upstream's `endSpan` calls
 * `span.setStatus(ERROR)` and `span.end()` synchronously, and the core reads
 * the slot there. Returns whether the seam was found; when it is not, nothing
 * changes (failed mysql2 spans keep `error.type=_OTHER`) and one WARN line is
 * written once per process (principle 8).
 */
export function holdMysql2Errors(instance: object): boolean {
  const target = instance as { _patchCallbackQuery?: unknown };
  const original = target._patchCallbackQuery;
  if (typeof original !== 'function') {
    reportOnce(
      MISSING_SEAM_KEY,
      '@staffops/otel-helper-sql: @opentelemetry/instrumentation-mysql2 has no _patchCallbackQuery; failed mysql2 spans carry error.type=_OTHER without db.response.status_code',
    );
    return false;
  }
  const patch = original as PatchCallbackQuery;
  target._patchCallbackQuery = function patchCallbackQuery(this: unknown, endSpan: EndSpan): unknown {
    if (typeof endSpan !== 'function') return patch.call(this, endSpan);
    const holding: EndSpan = (...args: unknown[]) => {
      const previous = held;
      held = args[0] === undefined || args[0] === null ? undefined : { error: args[0] };
      try {
        return endSpan(...args);
      } finally {
        held = previous;
      }
    };
    return patch.call(this, holding);
  };
  return true;
}
