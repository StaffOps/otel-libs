/**
 * The `db.query.text`, span name and error code the helper records for a
 * Redis command (JS8 "never records command values"; contract D14
 * `otelredis` bullet, D1 "DB error status").
 *
 * Upstream's default serializer (`@opentelemetry/redis-common`
 * `defaultDbStatementSerializer`) keeps arguments for many commands: every
 * argument of `GET`, `DEL`, `EVAL` (script and values), `SISMEMBER`, `LREM`,
 * `ZSCORE`, ...; the key of `SET`, `HSET`, `PUBLISH`. The helper replaces it:
 * the command name, uppercased (ASCII), then one `?` per argument — Python's
 * contrib format (`SET ? ?`, `_format_command_args`), which D14 names as the
 * equivalent of Go's `WithDBStatement(false)`. Keys are not recorded either
 * (Go and Python record none). Cut at {@link MAX_STATEMENT} code points with
 * `...`, as Python.
 */

/** Python contrib's `cmd_max_len`. */
export const MAX_STATEMENT = 1000;
const TOO_LONG = '...';
/** db.system.name: the name used when a command has none. */
const SYSTEM = 'redis';

const upper = (s: string): string => s.replace(/[a-z]+/g, (m) => m.toUpperCase());

/** The command name as text: a Buffer or any other typed array is decoded, not printed as `1,2,3`. */
function nameText(commandName: unknown): string {
  if (ArrayBuffer.isView(commandName)) return Buffer.from(commandName.buffer, commandName.byteOffset, commandName.byteLength).toString();
  return String(commandName);
}

/** `SET ? ?` for `SET k v`: the command name and one `?` per argument, never their values. */
export function sanitizedStatement(commandName: unknown, commandArgs: unknown): string {
  const name = upper(nameText(commandName)) || SYSTEM;
  // No more `?` than fit: the count can be huge (MSET of a large object).
  const count = Math.min(Array.isArray(commandArgs) ? commandArgs.length : 0, MAX_STATEMENT);
  const text = `${name}${' ?'.repeat(count)}`;
  // Fast path: code units >= code points, so a short text needs no cut.
  if (text.length <= MAX_STATEMENT) return text;
  const points = Array.from(text);
  return points.length > MAX_STATEMENT ? points.slice(0, MAX_STATEMENT - TOO_LONG.length).join('') + TOO_LONG : text;
}

/**
 * The span name for a contrib span: the command uppercased (ASCII) — a **parity choice with
 * Python**, not semconv (semconv: `{db.operation.name} {target}`, with
 * `db.operation.name` as provided, so ioredis `set`; the helper keeps
 * `db.operation.name` as provided and has no `{target}`). node-redis names its
 * spans `redis-<COMMAND>`, ioredis the lower-case command. Connection spans
 * (node-redis `redis-connect`, ioredis `connect`) are named `connect`. MULTI
 * and pipeline commands keep one span per command (both contribs), so each is
 * named by its own command (Python names the one pipeline span by all of
 * them: `SET GET`; design D-JS14). An empty name falls back to `redis`.
 */
export function commandSpanName(requested: string): string {
  const name = requested.startsWith('redis-') ? requested.slice('redis-'.length) : requested;
  if (name === 'connect') return name;
  return upper(name) || SYSTEM;
}

/**
 * Known Redis reply codes (contract D1 "DB error status"): the error codes Redis 7.4 sends (`src/*.c`, e.g. server.c:1875
 * NOSCRIPT, cluster.c:1186 CROSSSLOT, cluster.c:1205 MOVED/ASK) plus redis-py's
 * `BaseParser.EXCEPTION_CLASSES` keys. The same set in Go and Python.
 */
export const REPLY_CODES: ReadonlySet<string> = new Set([
  'ASK', 'BUSY', 'BUSYGROUP', 'BUSYKEY', 'CLUSTERDOWN', 'CROSSSLOT', 'DENIED', 'ERR', 'EXECABORT', 'INPROG', 'INVALIDOBJ',
  'IOERR', 'LOADING', 'MASTERDOWN', 'MISCONF', 'MOVED', 'NOAUTH', 'NOGOODSLAVE', 'NOGROUP', 'NOMASTERLINK', 'NOPERM',
  'NOPROTO', 'NOQUORUM', 'NOREPLICAS', 'NOSCRIPT', 'NOTBUSY', 'OOM', 'READONLY', 'TRYAGAIN', 'UNBLOCKED', 'UNKILLABLE',
  'WRONGPASS', 'WRONGTYPE',
]);

/**
 * The reply's leading code in a Redis error message when it is a known one
 * ({@link REPLY_CODES}): `db.response.status_code` and `error.type`.
 * Anything else — a Lua `error_reply` whose first word is data, a client-side
 * error (`connect ECONNREFUSED ...`) — gives none (`_OTHER`).
 */
export function redisResponseCode(message: string): string | undefined {
  const token = /^([A-Z][A-Z0-9_]{0,31}) /.exec(message)?.[1];
  return token !== undefined && REPLY_CODES.has(token) ? token : undefined;
}
