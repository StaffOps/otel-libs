/**
 * The helper's logger (spec JS6, design D-JS6; contract R4 and design D6).
 *
 * Per record, a logger created by {@link createLogger} writes through the
 * OpenTelemetry Logs API of the active setup while it has a logs exporter
 * (OTLP and/or `OTEL_LOGS_EXPORTER=console`, the SDK console exporter), and
 * otherwise — before `setup()`, after `shutdown()`, without an endpoint,
 * with `OTEL_LOGS_EXPORTER=none`, under `OTEL_SDK_DISABLED=true` — writes one
 * JSON line to stdout, never both (D6: no double ingestion). Records are
 * never handed to a provider without a processor, which would drop them.
 *
 * The stdout line has the shape of the Go and Python fallbacks: `time` (RFC
 * 3339, milliseconds, UTC `Z`), `level` (`DEBUG`/`INFO`/`WARN`/`ERROR`),
 * `msg`, `logger` (the name, as Python), the record attributes, and
 * `exception` for the first `Error`-valued attribute. No trace ids (as
 * Python). Both paths drop records below the level (R4).
 */
import { SeverityNumber, type AnyValue, type Attributes } from '@opentelemetry/api';
import { reportOnce, truncateValue } from './diagnostics';
import { typeName } from './env';
import { LOG_LEVELS, type LogLevel } from './log-level';
import { logDestination } from './setup';

/** Attribute values a record may carry; anything else is rendered as text. */
export type LogAttributes = Readonly<Record<string, unknown>>;

/** Options of {@link createLogger}. */
export interface LoggerOptions {
  /** Logger name: the `logger` field on stdout and the instrumentation scope on the OTel path. */
  readonly name: string;
  /**
   * Minimum level. Default: the level the last successful `setup()` resolved
   * (`minimumLogLevel`, else `defaultLogLevel(environment, debug)`; `info`
   * before the first setup), read per record.
   */
  readonly level?: LogLevel | undefined;
}

/** The helper's logger (spec JS6). Its methods never throw. */
export interface Logger {
  readonly name: string;
  debug(message: string, attributes?: LogAttributes): void;
  info(message: string, attributes?: LogAttributes): void;
  warn(message: string, attributes?: LogAttributes): void;
  error(message: string, attributes?: LogAttributes): void;
}

const SEVERITY: Readonly<Record<LogLevel, { readonly number: SeverityNumber; readonly text: string }>> = {
  debug: { number: SeverityNumber.DEBUG, text: 'DEBUG' },
  info: { number: SeverityNumber.INFO, text: 'INFO' },
  warn: { number: SeverityNumber.WARN, text: 'WARN' },
  error: { number: SeverityNumber.ERROR, text: 'ERROR' },
};

/** Keys of the stdout line that record attributes cannot replace (Python: skipped). */
const RESERVED = new Set(['time', 'level', 'msg', 'logger']);
const EXCEPTION = 'exception';

function fail(option: string, why: string): never {
  throw new TypeError(`createLogger(): ${option} ${why}`);
}

/**
 * Creates a logger. Invalid options throw a `TypeError` naming the option
 * (R8: code values fail fast). The logger can be created before `setup()`:
 * destination and default level are read per record.
 */
export function createLogger(options: LoggerOptions): Logger {
  // Typed for callers, checked as unknown (JavaScript callers pass anything).
  const o: unknown = options;
  if (typeof o !== 'object' || o === null) fail('options', `must be an object, got ${typeName(o)}`);
  const { name, level } = o as { name?: unknown; level?: unknown };
  if (typeof name !== 'string' || name.trim() === '') {
    fail('name', `must be a non-empty string, got ${typeof name === 'string' ? JSON.stringify(name) : typeName(name)}`);
  }
  if (level !== undefined && !LOG_LEVELS.includes(level as LogLevel)) {
    fail('level', `must be one of ${LOG_LEVELS.join(', ')}, got ${typeof level === 'string' ? JSON.stringify(level) : typeName(level)}`);
  }
  const fixed = level as LogLevel | undefined;
  const log = (recordLevel: LogLevel) => (message: string, attributes?: LogAttributes): void => {
    emitRecord(name, fixed, recordLevel, message, attributes);
  };
  return Object.freeze({ name, debug: log('debug'), info: log('info'), warn: log('warn'), error: log('error') });
}

function enabled(min: LogLevel, level: LogLevel): boolean {
  return LOG_LEVELS.indexOf(level) >= LOG_LEVELS.indexOf(min);
}

/**
 * One record, by the per-record rule (D6). `fixed` is the logger's own level;
 * `undefined` = the installed one. Never throws: a logging call must not
 * break the caller.
 */
export function emitRecord(name: string, fixed: LogLevel | undefined, level: LogLevel, message: unknown, attributes?: LogAttributes | null): void {
  try {
    const destination = logDestination();
    if (!enabled(fixed ?? destination.level, level)) return;
    const text = typeof message === 'string' ? message : safeText(message);
    if (destination.provider !== undefined) {
      const { attrs, exception } = otelAttributes(attributes);
      let plain: ReturnType<typeof plainException> | undefined;
      try {
        plain = exception === undefined ? undefined : plainException(exception);
      } catch {
        // Unreachable in practice (plainException reads through read()); stdout keeps the record.
        process.stdout.write(`${stdoutLine(name, level, text, attributes, new Date())}\n`);
        return;
      }
      const severity = SEVERITY[level];
      destination.provider.getLogger(name).emit({
        severityNumber: severity.number,
        severityText: severity.text,
        body: text,
        attributes: attrs,
        ...(plain === undefined ? {} : { exception: plain }),
      });
      return;
    }
    process.stdout.write(`${stdoutLine(name, level, text, attributes, new Date())}\n`);
  } catch (err) {
    // Never into the caller: an attribute object that cannot even be listed (a
    // Proxy whose ownKeys trap throws), an SDK error, a synchronous write error.
    // Reported once with the reason only (R8: never the values).
    reportOnce('logging:emit-failed', 'a helper log record could not be written and was dropped', {
      reason: truncateValue(errorReason(err)),
    });
  }
}

/** `typeName(value)`, `object` when even that throws (`Array.isArray` of a revoked Proxy). */
function safeTypeName(value: unknown): string {
  try {
    return typeName(value);
  } catch {
    return 'object';
  }
}

/** The reason of an emit failure: its message when it is a readable string, else its type. */
function errorReason(err: unknown): string {
  if (isError(err)) {
    const message = read(err, 'message');
    if (typeof message === 'string') return message;
  }
  return safeTypeName(err);
}

/** `value instanceof Error`, false when the prototype lookup throws (a Proxy trap, a revoked Proxy). */
function isError(value: unknown): value is Error {
  try {
    return value instanceof Error;
  } catch {
    return false;
  }
}

/**
 * The record exception handed to the SDK: a plain `{name, message, stack}`
 * (plus a string or number `code`, which the SDK uses as `exception.type`)
 * read through {@link read}, so a hostile getter on the app's Error never
 * runs inside the SDK (its message would reach the emit-failed report).
 */
function plainException(err: Error): { name?: string; message?: string; stack?: string; code?: string | number } {
  const out: { name?: string; message?: string; stack?: string; code?: string | number } = {};
  for (const key of ['name', 'message', 'stack'] as const) {
    const value = read(err, key);
    if (typeof value === 'string') out[key] = value;
  }
  const code = read(err, 'code');
  if (typeof code === 'string' || typeof code === 'number') out.code = code;
  return out;
}

/** `String(value)` that never throws. */
function safeText(value: unknown): string {
  try {
    return String(value);
  } catch {
    return `[${safeTypeName(value)}]`;
  }
}

/** The stdout `exception` text: the stack (Python writes the traceback), else `Name: message`. */
export function errorText(err: Error): string {
  try {
    if (typeof err.stack === 'string' && err.stack !== '') return err.stack;
    return `${safeText(err.name)}: ${safeText(err.message)}`;
  } catch {
    return safeText(err);
  }
}

/** `[Function: name]` (`[Function: anonymous]` without a name), as `util.inspect` writes it. */
function functionText(fn: (...args: never[]) => unknown): string {
  let name = '';
  try {
    name = typeof fn.name === 'string' ? fn.name : '';
  } catch {
    // An exotic name accessor.
  }
  return `[Function: ${name === '' ? 'anonymous' : name}]`;
}

/** Marker for a value whose read or conversion threw. */
function unreadable(kind: string): string {
  return `[unreadable ${kind}]`;
}

/** Returned by {@link read} when the read threw. */
const UNREADABLE: unique symbol = Symbol('unreadable');
const UNREADABLE_ACCESSOR = '[unreadable accessor]';

/** `holder[key]`, or {@link UNREADABLE} when a getter or Proxy trap throws. */
function read(holder: object, key: PropertyKey): unknown {
  try {
    return (holder as Record<PropertyKey, unknown>)[key];
  } catch {
    return UNREADABLE;
  }
}

/** Own enumerable string keys, or `undefined` when listing them throws. */
function keysOf(value: object): string[] | undefined {
  try {
    return Object.keys(value);
  } catch {
    return undefined;
  }
}

/** Nesting limit of a converted attribute value, and its marker (as Go otelslog `convert.go`). */
const MAX_DEPTH = 100;
const MAX_DEPTH_EXCEEDED = '<max-depth-exceeded>';
const CIRCULAR = '[Circular]';

function isPlainObject(value: object): boolean {
  const proto: unknown = Object.getPrototypeOf(value);
  return proto === Object.prototype || proto === null;
}

/** Sets an own data property; `__proto__` is defined, never sent to the prototype setter. */
function define(target: Record<string, unknown>, key: string, value: unknown): void {
  if (key === '__proto__') Object.defineProperty(target, key, { value, enumerable: true, writable: true, configurable: true });
  else target[key] = value;
}

type Mode = 'json' | 'otel';

/**
 * Converts one attribute value for the stdout JSON line (`json`) or an OTLP
 * attribute (`otel`). Shared rules (contract D1 row "Stdout fallback fields"
 * and D-JS14 row "Helper log attribute values", 2026-11-02): an `Error` as its
 * stack, a function as `[Function: name]`, bigint and symbols as text, a
 * `Map` as an object/map with string keys, a `Set` as an array, a cycle as
 * `[Circular]`, nesting deeper than 100 as `<max-depth-exceeded>`, a value
 * whose read throws as `[unreadable …]`. `json`: NaN and ±Infinity as text,
 * other objects by their `toJSON` or own enumerable properties (what
 * `JSON.stringify` writes). `otel`: numbers as they are, `Uint8Array` as
 * bytes, `Date` as ISO 8601 text, plain objects and arrays as maps and
 * arrays, other objects as their JSON text. `ancestors` is the path from the
 * root (push/pop), so a repeated sibling reference is not a cycle.
 */
function convert(value: unknown, mode: Mode, ancestors: object[]): unknown {
  switch (typeof value) {
    case 'string':
    case 'boolean':
      return value;
    case 'number':
      return mode === 'json' && !Number.isFinite(value) ? String(value) : value;
    case 'undefined':
      return null;
    case 'bigint':
    case 'symbol':
      return safeText(value);
    case 'function':
      return functionText(value as (...args: never[]) => unknown);
    default:
      break;
  }
  if (value === null) return null;
  const obj = value as object;
  try {
    if (isError(obj)) return errorText(obj);
    if (mode === 'otel') {
      if (obj instanceof Uint8Array) return obj;
      if (obj instanceof Date) return Number.isNaN(obj.getTime()) ? safeText(obj) : obj.toISOString();
    }
    if (ancestors.includes(obj)) return CIRCULAR;
    if (ancestors.length >= MAX_DEPTH) return MAX_DEPTH_EXCEEDED;
    ancestors.push(obj);
    try {
      return convertObject(obj, mode, ancestors);
    } finally {
      ancestors.pop();
    }
  } catch {
    return unreadable(safeTypeName(value));
  }
}

function convertObject(obj: object, mode: Mode, ancestors: object[]): unknown {
  if (Array.isArray(obj) || obj instanceof Set) {
    const items: unknown[] = Array.isArray(obj) ? (obj as unknown[]) : [...(obj as Set<unknown>)];
    const out: unknown[] = [];
    for (let i = 0; i < items.length; i++) {
      const item = read(items, i);
      out.push(item === UNREADABLE ? UNREADABLE_ACCESSOR : convert(item, mode, ancestors));
    }
    return out;
  }
  const map: Record<string, unknown> = {};
  if (obj instanceof Map) {
    for (const [k, v] of obj as Map<unknown, unknown>) define(map, typeof k === 'string' ? k : safeText(k), convert(v, mode, ancestors));
    return map;
  }
  if (mode === 'otel' && !isPlainObject(obj)) {
    // Go: a struct as `%+v` text; Python: `str()`. Node: its JSON text.
    const json = convert(obj, 'json', ancestors.slice(0, -1));
    return typeof json === 'string' ? json : JSON.stringify(json);
  }
  if (mode === 'json') {
    const toJSON = read(obj, 'toJSON');
    if (toJSON === UNREADABLE) return UNREADABLE_ACCESSOR;
    if (typeof toJSON === 'function') {
      // As JSON.stringify: the toJSON result replaces the value (a throw: caught by convert);
      // a toJSON returning the object itself lists its own keys below.
      const replaced = (toJSON as (key: string) => unknown).call(obj, '');
      if (replaced !== obj) return convert(replaced, mode, ancestors);
    }
  }
  const keys = keysOf(obj);
  if (keys === undefined) return unreadable(safeTypeName(obj));
  for (const key of keys) {
    const item = read(obj, key);
    if (item === undefined && mode === 'json') continue; // JSON.stringify leaves undefined members out
    define(map, key, item === UNREADABLE ? UNREADABLE_ACCESSOR : convert(item, mode, ancestors));
  }
  return map;
}

/** Finite numbers, strings, booleans and null as they are; everything else through {@link convert}. */
function jsonValue(value: unknown): unknown {
  if (typeof value === 'string' || typeof value === 'boolean' || value === null) return value;
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  return convert(value, 'json', []);
}

/** The stdout JSON line of one record (design D6 "Stdout fallback shape"). */
export function stdoutLine(name: string, level: LogLevel, message: string, attributes: LogAttributes | null | undefined, now: Date): string {
  // `define` keeps a `__proto__` attribute an ordinary field.
  const entry: Record<string, unknown> = { time: now.toISOString(), level: SEVERITY[level].text, msg: message, logger: name };
  let exception: string | undefined;
  if (attributes !== undefined && attributes !== null) {
    const keys = keysOf(attributes);
    if (keys === undefined) throw new TypeError('the attributes cannot be listed');
    // Go: an attribute already named `exception` is kept and no error is renamed.
    const renameError = !keys.includes(EXCEPTION);
    for (const key of keys) {
      const value = read(attributes, key);
      // The first Error is renamed even under a reserved key (A23 fix round, as Go).
      if (renameError && exception === undefined && isError(value)) {
        exception = errorText(value);
        continue;
      }
      if (RESERVED.has(key)) continue;
      define(entry, key, value === UNREADABLE ? UNREADABLE_ACCESSOR : jsonValue(value));
    }
  }
  if (exception !== undefined) define(entry, EXCEPTION, exception);
  return JSON.stringify(entry);
}

/** An OTel attribute value (D-JS14 row "Helper log attribute values", ruled 2026-11-02). */
function otelValue(value: unknown): AnyValue {
  if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') return value;
  return convert(value, 'otel', []);
}

/**
 * The record attributes for the Logs API: the first `Error`-valued attribute
 * becomes the record's `exception` (the SDK writes `exception.type`,
 * `exception.message`, `exception.stacktrace`); `null`/`undefined` are left out.
 */
export function otelAttributes(attributes: LogAttributes | null | undefined): { attrs: Attributes; exception: Error | undefined } {
  const attrs: Attributes = {};
  let exception: Error | undefined;
  if (attributes === undefined || attributes === null) return { attrs, exception };
  const keys = keysOf(attributes);
  if (keys === undefined) throw new TypeError('the attributes cannot be listed');
  for (const key of keys) {
    const value = read(attributes, key);
    if (value === UNREADABLE) {
      define(attrs, key, UNREADABLE_ACCESSOR);
      continue;
    }
    if (value === undefined || value === null) continue;
    if (exception === undefined && isError(value)) {
      exception = value;
      continue;
    }
    define(attrs, key, otelValue(value));
  }
  return { attrs, exception };
}
