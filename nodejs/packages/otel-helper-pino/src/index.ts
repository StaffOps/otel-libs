/**
 * `@staffops/otel-helper-pino` — a pino destination that writes each record
 * by the helper's per-record rule (spec JS6, design D-JS6, contract D6):
 * through the OpenTelemetry Logs API while the helper's logs exporter is
 * active, otherwise as one stdout JSON line of the core shape (`time`,
 * `level`, `msg`, `logger`, the fields, `exception`) — never both. The app
 * passes it explicitly (`pino(options, createPinoDestination())`,
 * `pino(createPinoDestination())`, or as one stream of `pino.multistream`);
 * the helper never rewrites the app's other destinations, so there is no
 * double ingestion unless the app configures it (do not put it in a
 * multistream next to `process.stdout`: before `setup()` both write stdout).
 *
 * The destination reads the line pino serialized (so redaction, serializers
 * and formatters apply) plus the metadata pino hands a destination
 * (`Symbol.for('pino.metadata')`: the numeric level and the logger). Trace
 * correlation comes from the active context: pino writes to its destination
 * synchronously inside the logging call. It cannot run inside
 * `pino.transport()` (a worker thread has no helper setup).
 */
import { hostname } from 'node:os';
import type { DestinationStream, Logger } from 'pino';
import type { LogLevel } from '@staffops/otel-helper';
import { emitBridgeRecord, flushBridgeRecords, LOG_LEVELS, recordEnabled, type RecordLevel } from '@staffops/otel-helper/internal/bridge';

export { VERSION } from './version';

/** Options of {@link createPinoDestination}. */
export interface PinoDestinationOptions {
  /**
   * Logger name (stdout `logger`, OTel instrumentation scope) for loggers
   * without a pino `name`. Default `pino`.
   */
  readonly name?: string | undefined;
  /**
   * Minimum level, applied on top of pino's own. Default: the level the last
   * successful `setup()` resolved (`minimumLogLevel`, else by environment),
   * read per record, as `createLogger`.
   */
  readonly minimumLogLevel?: LogLevel | undefined;
}

/** The destination pino writes to (pino's `DestinationStream`, with metadata). */
export interface PinoDestination extends DestinationStream {
  write(chunk: string): boolean;
  /** Recognized by `pino(destination)` as a stream (pino `tools.js`). */
  readonly writable: true;
  /** Called by `logger.flush(cb)`: flushes the helper's logs pipeline, then `cb`. */
  flush(cb?: (err?: Error) => void): void;
}

const PACKAGE = '@staffops/otel-helper-pino';
const DEFAULT_NAME = 'pino';
/** pino's built-in level numbers (`pino.levels.values`). */
const FATAL = 60;
const ERROR = 50;
const WARN = 40;
const INFO = 30;
const DEBUG = 20;
/**
 * Fields of pino's line that are not attributes: `level` becomes the record
 * severity; `time` is replaced by the record time (now: pino writes inside the
 * logging call; its time formats differ, `unixTime` is in seconds). `pid` and
 * `hostname` are left out only when they are pino's default `base` (this
 * process's pid and host name; the contrib pino bridge drops them too).
 */
const DROPPED = new Set(['level', 'time']);
/** Keys of pino's serialized error that the record exception carries. */
const ERROR_KEYS = new Set(['type', 'message', 'stack', 'name']);
const HOSTNAME = hostname();

interface Metadata {
  lastLevel?: unknown;
  lastLogger?: unknown;
}

/** The logger's keys, found once per logger. */
interface Keys {
  readonly messageKey: string;
  readonly errorKey: string;
  /** pino's `chindings` symbol (the serialized bindings): the cache key of the bound name. */
  readonly chindings: symbol | undefined;
}

/** A logger's bound name, valid while its serialized bindings are `chindings` (`setBindings` changes them). */
interface BoundName {
  readonly chindings: unknown;
  readonly name: string | undefined;
}

const nameCache = new WeakMap<object, BoundName>();

let loaded = false;
const keysCache = new WeakMap<object, Keys>();

function fail(option: string, why: string): never {
  throw new TypeError(`createPinoDestination(): ${option} ${why}`);
}

function typeName(value: unknown): string {
  if (value === null) return 'null';
  return Array.isArray(value) ? 'array' : typeof value;
}

/**
 * Checks that the app's pino (peer dependency) loads from this package. A
 * missing pino names the library (contract design D14 wording); any other
 * load failure is reported by its first line (the rest is a require stack of
 * local paths), the original kept as `cause`.
 */
function loadPino(): void {
  if (loaded) return;
  try {
    // eslint-disable-next-line @typescript-eslint/no-require-imports -- the peer dependency is loaded on first use, so a missing pino is a call-time error
    require('pino');
    loaded = true;
  } catch (err) {
    const code = (err as { code?: unknown } | null)?.code;
    const first = (err instanceof Error ? err.message : String(err)).replace(/\n[\s\S]*$/, '');
    if (code === 'MODULE_NOT_FOUND' && first.startsWith("Cannot find module 'pino'")) {
      throw new Error(`${PACKAGE} is installed but the library 'pino' is not. Install it (e.g. npm install pino)`, { cause: err });
    }
    throw new Error(`${PACKAGE}: failed to load pino: ${first}`, { cause: err });
  }
}

/**
 * Creates the destination. Invalid options throw a `TypeError` naming the
 * option (R8); a missing pino throws an `Error` naming it. The destination
 * can be created before `setup()`: destination and default level are read
 * per record. Its `write` never throws.
 */
export function createPinoDestination(options?: PinoDestinationOptions): PinoDestination {
  // Typed for callers, checked as unknown (JavaScript callers pass anything; null is not "no options").
  let o: unknown = options;
  if (o === undefined) o = {};
  if (typeof o !== 'object' || o === null || Array.isArray(o)) fail('options', `must be an object, got ${typeName(o)}`);
  const { name, minimumLogLevel } = o as { name?: unknown; minimumLogLevel?: unknown };
  if (name !== undefined && (typeof name !== 'string' || name.trim() === '')) {
    fail('name', `must be a non-empty string, got ${typeof name === 'string' ? JSON.stringify(name) : typeName(name)}`);
  }
  if (minimumLogLevel !== undefined && !LOG_LEVELS.includes(minimumLogLevel as LogLevel)) {
    fail('minimumLogLevel', `must be one of ${LOG_LEVELS.join(', ')}, got ${typeof minimumLogLevel === 'string' ? JSON.stringify(minimumLogLevel) : typeName(minimumLogLevel)}`);
  }
  loadPino();
  const fallbackName = name ?? DEFAULT_NAME;
  const minimum = minimumLogLevel as LogLevel | undefined;
  const destination: PinoDestination & Metadata = {
    [Symbol.for('pino.metadata')]: true,
    writable: true,
    write(chunk: string): boolean {
      try {
        writeChunk(destination, chunk, fallbackName, minimum);
        /* c8 ignore next 3 -- defensive: writeChunk reads guarded, emitBridgeRecord never throws */
      } catch {
        // A logging call must not break the caller.
      }
      return true;
    },
    flush(cb?: (err?: Error) => void): void {
      void flushBridgeRecords().then(() => {
        // On the next tick, outside the promise chain: a throwing callback is
        // an uncaught exception, as from any Node callback, not an unhandled rejection.
        if (typeof cb === 'function') process.nextTick(cb);
      });
    },
  };
  return destination;
}

/**
 * The default export pino's `transport({ target })` would call in its worker
 * thread: unsupported (the worker has no helper setup), so it fails clearly.
 */
export default function pinoTransportTarget(): never {
  throw new Error(`${PACKAGE}: pino.transport({ target: '${PACKAGE}' }) is not supported (a worker thread has no helper setup); pass createPinoDestination() to pino() instead`);
}

/** The record level of a pino level number (custom levels fall in the bucket below them). */
function recordLevel(level: number): RecordLevel {
  if (level >= FATAL) return 'fatal';
  if (level >= ERROR) return 'error';
  if (level >= WARN) return 'warn';
  if (level >= INFO) return 'info';
  if (level >= DEBUG) return 'debug';
  return 'trace';
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function own(holder: object, key: PropertyKey): unknown {
  try {
    return (holder as Record<PropertyKey, unknown>)[key];
  } catch {
    return undefined;
  }
}

/** A chunk that is not a string (a Buffer from a stream wrapper) as text; never throws. */
function chunkText(chunk: unknown): string {
  if (typeof chunk === 'string') return chunk;
  try {
    return String(chunk);
  } catch {
    return '[unreadable line]';
  }
}

/** A JSON integer beyond Number's safe range keeps its digits (as text) instead of being rounded. */
function reviver(this: unknown, _key: string, value: unknown, context?: { source?: unknown }): unknown {
  if (typeof value === 'number' && !Number.isSafeInteger(value) && Number.isInteger(value)) {
    const source = context?.source;
    if (typeof source === 'string' && /^-?\d+$/.test(source)) return source;
  }
  return value;
}

/** A JSON integer literal long enough to be past `Number.MAX_SAFE_INTEGER` (16 digits): only then is the reviver needed. */
const LONG_INTEGER = /\d{16,}/;

/**
 * One record per line. pino writes one line per call (a trailing `\n`), so
 * the split runs only for a chunk with an inner newline (a wrapper batching
 * lines, or a direct non-JSON write with newlines: several records).
 */
function writeChunk(meta: Metadata, chunk: unknown, fallbackName: string, minimum: LogLevel | undefined): void {
  const text = chunkText(chunk);
  const body = text.endsWith('\n') ? text.slice(0, -1) : text;
  if (!body.includes('\n')) {
    if (body.trim() !== '') writeLine(meta, body, true, fallbackName, minimum);
    return;
  }
  // Several lines: each line's own level (one line: pino's metadata level).
  for (const line of body.split('\n')) if (line.trim() !== '') writeLine(meta, line, false, fallbackName, minimum);
}

function writeLine(meta: Metadata, text: string, single: boolean, fallbackName: string, minimum: LogLevel | undefined): void {
  const metaLevel = typeof meta.lastLevel === 'number' ? meta.lastLevel : undefined;
  // pino's own write: the level is known before parsing, so a dropped record is never parsed.
  if (single && metaLevel !== undefined && !recordEnabled(minimum, recordLevel(metaLevel))) return;
  let parsed: unknown;
  try {
    parsed = LONG_INTEGER.test(text) ? JSON.parse(text, reviver) : JSON.parse(text);
  } catch {
    parsed = undefined;
  }
  const fields = isRecord(parsed) ? parsed : undefined;
  const lineLevel = fields !== undefined && typeof fields.level === 'number' ? fields.level : undefined;
  const numeric = (single ? (metaLevel ?? lineLevel) : (lineLevel ?? metaLevel)) ?? INFO;
  const level = recordLevel(numeric);
  if (!recordEnabled(minimum, level)) return;
  const logger = typeof meta.lastLogger === 'object' && meta.lastLogger !== null ? (meta.lastLogger as Logger) : null;
  const { messageKey, errorKey, chindings } = keysOf(logger);
  const severityText = levelLabel(logger, numeric);
  const boundName = bindingsName(logger, chindings);
  const loggerName = boundName ?? fallbackName;
  if (fields === undefined) {
    // Not a JSON object (a custom serializer of the whole line): the line is the body.
    emitBridgeRecord({ logger: loggerName, minimum, level, severityText, message: text.trimEnd() });
    return;
  }
  const errorValue = fields[errorKey];
  const error = errorOf(errorValue);
  const attributes: Record<string, unknown> = {};
  for (const key of Object.keys(fields)) {
    if (DROPPED.has(key) || key === messageKey) continue;
    if (key === 'pid' && fields.pid === process.pid) continue;
    if (key === 'hostname' && fields.hostname === HOSTNAME) continue;
    // The logger's own `name` binding is the logger name; a different per-call `name` stays a field.
    if (key === 'name' && boundName !== undefined && fields.name === boundName) continue;
    let value = fields[key];
    if (key === errorKey && error !== undefined) {
      const extras = errorExtras(errorValue as Record<string, unknown>);
      if (extras === undefined) continue;
      value = extras;
    }
    if (key === '__proto__') Object.defineProperty(attributes, key, { value, enumerable: true, writable: true, configurable: true });
    else attributes[key] = value;
  }
  emitBridgeRecord({ logger: loggerName, minimum, level, severityText, message: fields[messageKey], attributes, error });
}

/**
 * The logger's `messageKey`/`errorKey`, found by their symbols' descriptions
 * (`pino.messageKey`, `pino.errorKey`) up the logger's prototype chain, so a
 * logger made by another copy of pino (other Symbol instances) is read too.
 */
function keysOf(logger: Logger | null): Keys {
  const defaults: Keys = { messageKey: 'msg', errorKey: 'err', chindings: undefined };
  if (logger === null) return defaults;
  const cached = keysCache.get(logger);
  if (cached !== undefined) return cached;
  const found: { messageKey?: string; errorKey?: string; chindings?: symbol } = {};
  try {
    for (let o: object | null = logger; o !== null; o = Object.getPrototypeOf(o) as object | null) {
      for (const sym of Object.getOwnPropertySymbols(o)) {
        if (sym.description === 'pino.chindings') found.chindings ??= sym;
        const which = sym.description === 'pino.messageKey' ? 'messageKey' : sym.description === 'pino.errorKey' ? 'errorKey' : undefined;
        if (which === undefined || found[which] !== undefined) continue;
        const value = own(o, sym);
        if (typeof value === 'string' && value !== '') found[which] = value;
      }
    }
  } catch {
    // A hostile logger (a Proxy): the defaults.
  }
  const keys: Keys = { messageKey: found.messageKey ?? defaults.messageKey, errorKey: found.errorKey ?? defaults.errorKey, chindings: found.chindings };
  keysCache.set(logger, keys);
  return keys;
}

/**
 * The logger's `name` binding (`pino({ name })`, inherited by children), from
 * `logger.bindings()` (which parses the bindings: cached per logger while its
 * serialized bindings are unchanged, so `setBindings` is seen).
 */
function bindingsName(logger: Logger | null, chindings: symbol | undefined): string | undefined {
  if (logger === null) return undefined;
  const current = chindings === undefined ? undefined : own(logger, chindings);
  const cached = nameCache.get(logger);
  if (cached !== undefined && current !== undefined && cached.chindings === current) return cached.name;
  let name: string | undefined;
  try {
    const bindings: unknown = logger.bindings();
    const value = isRecord(bindings) ? bindings.name : undefined;
    name = typeof value === 'string' && value.trim() !== '' ? value : undefined;
  } catch {
    name = undefined;
  }
  if (current !== undefined) nameCache.set(logger, { chindings: current, name });
  return name;
}

/** The logger's label of a level number, upper-cased (OTel `severityText`); `undefined` = the bucket's own text. */
function levelLabel(logger: Logger | null, level: number): string | undefined {
  if (logger === null) return undefined;
  const levels = own(logger, 'levels');
  const labels = isRecord(levels) ? own(levels, 'labels') : undefined;
  const label = isRecord(labels) ? own(labels, String(level)) : undefined;
  return typeof label === 'string' && label !== '' ? label.toUpperCase() : undefined;
}

/**
 * The record's error: pino's error serializer writes `{type, message, stack,
 * …}` under the error key (its message and stack include the causes);
 * rebuilt as an `Error` (its stack kept, no new one) so the core writes it as
 * the `exception`. The type is a string `name` when there is one, else `type`.
 */
function errorOf(value: unknown): Error | undefined {
  if (!isRecord(value)) return undefined;
  const { type, name, message, stack, code } = value;
  if (typeof message !== 'string' && typeof stack !== 'string') return undefined;
  const err = new Error(typeof message === 'string' ? message : '');
  const errName = typeof name === 'string' && name !== '' ? name : typeof type === 'string' && type !== '' ? type : 'Error';
  const props: PropertyDescriptorMap = {
    name: { value: errName, configurable: true, writable: true },
    stack: { value: typeof stack === 'string' ? stack : undefined, configurable: true, writable: true },
  };
  if (typeof code === 'string' || typeof code === 'number') props.code = { value: code, configurable: true, writable: true };
  Object.defineProperties(err, props);
  return err;
}

/** The serialized error's other keys (`statusCode`, `details`, `code`…), kept as a map under the error key. */
function errorExtras(value: Record<string, unknown>): Record<string, unknown> | undefined {
  const extras: Record<string, unknown> = {};
  let any = false;
  for (const key of Object.keys(value)) {
    if (ERROR_KEYS.has(key)) continue;
    Object.defineProperty(extras, key, { value: value[key], enumerable: true, writable: true, configurable: true });
    any = true;
  }
  return any ? extras : undefined;
}
