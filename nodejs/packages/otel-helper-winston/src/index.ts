/**
 * `@staffops/otel-helper-winston` — a winston transport that writes each
 * record by the helper's per-record rule (spec JS6, design D-JS6, contract
 * D6): through the OpenTelemetry Logs API while the helper's logs exporter is
 * active, otherwise as one stdout JSON line of the core shape (`time`,
 * `level`, `msg`, `logger`, the fields, `exception`) — never both. The app
 * adds it explicitly (`transports: [new OtelHelperTransport()]`); the helper
 * never rewrites the app's other transports, so there is no double ingestion
 * unless the app configures it.
 *
 * Trace correlation and the record time come from the logging call: while
 * the transport is attached to a winston logger (`'pipe'` to `'unpipe'`,
 * counted per logger across transports and installed copies of this
 * package), that logger's `write` is wrapped and records each call's active
 * context and time for the info object in a process-wide `WeakMap` — nothing
 * is added to the info. The transport looks the info up when winston hands it
 * over (`_write`, before a transport format copies it), so a record winston
 * buffers under backpressure keeps its caller's trace. winston's exception
 * handlers log through `logger.log`, so their records are stamped too. The
 * logger's `_transform` is wrapped as well, so an object a logger-level
 * format returns instead of the info (`format.errors()` for an Error) gets
 * the info's stamp. A record that never went through the logger's `write`
 * (written before the transport was attached, or straight to the transport)
 * is written with the root context: uncorrelated, never on the trace active
 * at delivery.
 */
import type * as winston from 'winston';
import type { LogLevel } from '@staffops/otel-helper';
import {
  activeContext,
  emitBridgeRecord,
  flushBridgeRecords,
  LOG_LEVELS,
  recordEnabled,
  ROOT_CONTEXT,
  type BridgeRecord,
  type RecordLevel,
} from '@staffops/otel-helper/internal/bridge';

/**
 * winston's transport options. The types come through winston's re-export of
 * `winston-transport` (`winston.transport`: its instance type), so only the
 * winston peer dependency has to resolve.
 */
type TransportStreamOptions = winston.transport.TransportStreamOptions;
/** winston's `Transport` base class (`winston-transport`). */
type TransportStreamClass = new (options?: TransportStreamOptions) => winston.transport;

/** Options of {@link OtelHelperTransport}: winston's transport options plus the helper's. */
export interface OtelHelperTransportOptions extends TransportStreamOptions {
  /** Logger name (stdout `logger`, OTel instrumentation scope). Default `winston`. */
  readonly name?: string | undefined;
  /**
   * Minimum helper level, applied on top of winston's own `level`. Default:
   * the level the last successful `setup()` resolved (`minimumLogLevel`, else
   * by environment), read per record, as `createLogger`.
   */
  readonly minimumLogLevel?: LogLevel | undefined;
}

const PACKAGE = '@staffops/otel-helper-winston';
const DEFAULT_NAME = 'winston';
/** triple-beam's keys: the level without colors, and the arguments of a `logger.log(level, msg, ...splat)` call. */
const LEVEL = Symbol.for('level');
const SPLAT = Symbol.for('splat');
/** winston's info fields that are not attributes: the level becomes the severity, the message the body. */
const DROPPED = new Set(['level', 'message']);
/**
 * Fields of winston's exception-handler info (`getAllInfo`) left out: the
 * error is the record exception; `date` repeats the record time; `process`
 * holds `argv`, `cwd`, memory (argv can carry secrets) and `os` host details.
 */
const EXCEPTION_INFO = new Set(['error', 'exception', 'trace', 'date', 'process', 'os']);
/** A stack text has a head line and at least one V8 frame. */
const STACK_FRAME = /\n {4}at /;
/** The head line of a stack: `Name`, `Name: message`, `Name [CODE]: message` (Node's coded errors). */
const STACK_HEAD = /^([A-Za-z_$][\w$]*)(?: \[([^\]\n]+)\])?(?:: ?(.*))?$/;

/** The context and time of one logging call. */
interface CallStamp {
  readonly context: NonNullable<BridgeRecord['context']>;
  readonly time: Date;
}

/** One wrapped method of a logger, and how to put it back. */
interface WrappedMethod {
  readonly name: 'write' | '_transform';
  readonly original: unknown;
  readonly hadOwn: boolean;
  readonly wrapped: (...args: unknown[]) => unknown;
}

/** A logger whose `write` and `_transform` are wrapped, and how many helper transports are attached to it. */
interface Installed {
  readonly methods: readonly WrappedMethod[];
  count: number;
}

/** Shared by every installed copy of this package (a `Symbol.for` global), so their stamps and counts agree. */
interface Registry {
  readonly stamps: WeakMap<object, CallStamp>;
  readonly loggers: WeakMap<object, Installed>;
}

const REGISTRY_KEY = Symbol.for('@staffops/otel-helper-winston.registry.v1');

function registry(): Registry {
  const existing = (globalThis as Record<symbol, unknown>)[REGISTRY_KEY];
  if (typeof existing === 'object' && existing !== null) return existing as Registry;
  const created: Registry = { stamps: new WeakMap(), loggers: new WeakMap() };
  // Not enumerable, not writable: the stamps hold Contexts and must not show up in a dump of globalThis.
  Object.defineProperty(globalThis, REGISTRY_KEY, { value: created });
  return created;
}

const REGISTRY = registry();

function typeName(value: unknown): string {
  if (value === null) return 'null';
  return Array.isArray(value) ? 'array' : typeof value;
}

function fail(option: string, why: string): never {
  throw new TypeError(`OtelHelperTransport: ${option} ${why}`);
}

/**
 * winston's `Transport` base class (peer dependency), resolved from this
 * package when the module loads. When winston cannot be loaded, the base's
 * constructor throws the load error instead: a missing winston is named
 * (contract design D14 wording), anything else is reported by its first line
 * (the rest is a require stack of local paths), the original kept as `cause`.
 */
function loadBase(): TransportStreamClass {
  let failure: Error;
  try {
    // eslint-disable-next-line @typescript-eslint/no-require-imports -- the peer dependency is loaded guarded, so a missing winston is a construction-time error
    return (require('winston') as { Transport: TransportStreamClass }).Transport;
  } catch (err) {
    const code = (err as { code?: unknown } | null)?.code;
    const first = (err instanceof Error ? err.message : String(err)).replace(/\n[\s\S]*$/, '');
    failure =
      code === 'MODULE_NOT_FOUND' && first.startsWith("Cannot find module 'winston'")
        ? new Error(`${PACKAGE} is installed but the library 'winston' is not. Install it (e.g. npm install winston)`, { cause: err })
        : new Error(`${PACKAGE}: failed to load winston: ${first}`, { cause: err });
  }
  // A stand-in base: the class below stays definable, and using it is loud.
  // eslint-disable-next-line @typescript-eslint/no-extraneous-class -- only its throwing constructor is needed
  class Unavailable {
    constructor() {
      throw failure;
    }
  }
  return Unavailable as unknown as TransportStreamClass;
}

const Base = loadBase();

/** A winston logger (its `add`), not another stream piped into the transport. */
function isWinstonLogger(source: unknown): source is { write: unknown; add: unknown } {
  return typeof source === 'object' && source !== null && typeof (source as { add?: unknown }).add === 'function';
}

type Method = (...args: unknown[]) => unknown;

/** Defines `name` on `target` as an own, writable data property. */
function defineMethod(target: object, name: string, value: unknown): void {
  Object.defineProperty(target, name, { value, writable: true, configurable: true });
}

/**
 * `logger.write` wrapped: every call records its context and time for the
 * info object, replacing an older stamp of the same object (a reused Error or
 * info gets the current call's). The stamp is the call's Context and a Date —
 * what the record needs, nothing else; the WeakMap keeps it only while the
 * app keeps the info object reachable (a long-lived reused Error keeps its
 * last call's Context, and through it that call's span, alive with it).
 */
function wrapWrite(write: Method): Method {
  return function (this: unknown, chunk: unknown, ...rest: unknown[]): unknown {
    if (typeof chunk === 'object' && chunk !== null) REGISTRY.stamps.set(chunk, { context: activeContext(), time: new Date() });
    return write.call(this, chunk, ...rest);
  };
}

/**
 * `logger._transform` wrapped: a logger-level format may return a new object
 * (`format.errors()` for an Error); for the duration of the call `push`
 * gives the pushed object the incoming info's stamp when it has none, so the
 * record keeps its call's context (also when winston runs `_transform` later,
 * on buffered writes).
 */
function wrapTransform(transform: Method): Method {
  return function (this: unknown, info: unknown, ...rest: unknown[]): unknown {
    const stamp = stampOf(info);
    if (stamp === undefined) return transform.call(this, info, ...rest);
    const stream = this as { push: Method };
    const push = stream.push;
    const hadOwnPush = Object.hasOwn(stream, 'push');
    defineMethod(stream, 'push', function (this: unknown, chunk: unknown, ...more: unknown[]): unknown {
      if (typeof chunk === 'object' && chunk !== null && !REGISTRY.stamps.has(chunk)) REGISTRY.stamps.set(chunk, stamp);
      return push.call(this, chunk, ...more);
    });
    try {
      return transform.call(this, info, ...rest);
    } finally {
      if (hadOwnPush) defineMethod(stream, 'push', push);
      else delete (stream as { push?: unknown }).push;
    }
  };
}

/**
 * Wraps the logger's `write` and `_transform` (first helper transport on
 * that logger; later ones only count). Never throws; a frozen logger is left
 * as it is (its records uncorrelated).
 */
function attach(logger: { write: unknown }): boolean {
  const installed = REGISTRY.loggers.get(logger);
  if (installed !== undefined) {
    installed.count++;
    return true;
  }
  const target = logger as { write: unknown; _transform?: unknown };
  if (typeof target.write !== 'function') return false;
  const plan: [WrappedMethod['name'], (m: Method) => Method][] = [['write', wrapWrite]];
  if (typeof target._transform === 'function') plan.push(['_transform', wrapTransform]);
  const methods: WrappedMethod[] = [];
  try {
    for (const [name, wrap] of plan) {
      const original = target[name] as Method;
      const hadOwn = Object.hasOwn(target, name);
      const wrapped = wrap(original);
      defineMethod(target, name, wrapped);
      methods.push({ name, original, hadOwn, wrapped });
    }
  } catch {
    restore(target, methods);
    return false;
  }
  REGISTRY.loggers.set(logger, { methods, count: 1 });
  return true;
}

/** Puts back each wrapped method (unless something wrapped it since). */
function restore(logger: object, methods: readonly WrappedMethod[]): void {
  for (const { name, original, hadOwn, wrapped } of methods) {
    if ((logger as Record<string, unknown>)[name] !== wrapped) continue;
    if (hadOwn) defineMethod(logger, name, original);
    else Reflect.deleteProperty(logger, name);
  }
}

/** The last helper transport left the logger: its own `write` and `_transform` are put back. */
function detach(logger: { write: unknown }): void {
  const installed = REGISTRY.loggers.get(logger);
  if (installed === undefined || --installed.count > 0) return;
  REGISTRY.loggers.delete(logger);
  restore(logger, installed.methods);
}

/**
 * The transport. Invalid helper options throw a `TypeError` naming the
 * option (R8); a missing winston throws an `Error` naming it. It can be
 * created before `setup()`: destination and default level are read per
 * record. `log()` never throws.
 */
export class OtelHelperTransport extends Base {
  readonly #name: string;
  readonly #minimum: LogLevel | undefined;
  /** The loggers this transport is attached to (counted once each). */
  readonly #sources = new Set<object>();
  /** The stamp of the chunk `_write` is handling (the info a transport format copies). */
  #pending: CallStamp | undefined;

  constructor(options?: OtelHelperTransportOptions) {
    // Typed for callers, checked as unknown (JavaScript callers pass anything; null is not "no options").
    let o: unknown = options;
    if (o === undefined) o = {};
    if (typeof o !== 'object' || o === null || Array.isArray(o)) fail('options', `must be an object, got ${typeName(o)}`);
    const { name, minimumLogLevel, ...transport } = o as Record<string, unknown>;
    if (name !== undefined && (typeof name !== 'string' || name.trim() === '')) {
      fail('name', `must be a non-empty string, got ${typeof name === 'string' ? JSON.stringify(name) : typeName(name)}`);
    }
    if (minimumLogLevel !== undefined && !LOG_LEVELS.includes(minimumLogLevel as LogLevel)) {
      fail('minimumLogLevel', `must be one of ${LOG_LEVELS.join(', ')}, got ${typeof minimumLogLevel === 'string' ? JSON.stringify(minimumLogLevel) : typeName(minimumLogLevel)}`);
    }
    super(transport);
    this.#name = name ?? DEFAULT_NAME;
    this.#minimum = minimumLogLevel as LogLevel | undefined;
    this.on('pipe', (source: unknown) => {
      if (isWinstonLogger(source) && !this.#sources.has(source) && attach(source)) this.#sources.add(source);
    });
    this.on('unpipe', (source: unknown) => {
      if (isWinstonLogger(source) && this.#sources.delete(source)) detach(source);
    });
  }

  /** winston hands a record over here; its stamp is found before a transport format copies the info. */
  override _write(info: unknown, encoding: BufferEncoding, callback: (error?: Error | null) => void): void {
    this.#pending = stampOf(info);
    try {
      super._write(info, encoding, callback);
    } finally {
      this.#pending = undefined;
    }
  }

  /** Buffered records, one by one through {@link _write} (as winston-transport, with per-chunk callbacks). */
  override _writev(chunks: { chunk: unknown; encoding: BufferEncoding; callback: (error?: Error | null) => void }[], callback: (error?: Error | null) => void): void {
    for (const { chunk, encoding, callback: done } of chunks) this._write(chunk, encoding, done);
    callback(null);
  }

  /**
   * Called by winston for each record; writes it, emits `'logged'` and calls
   * `next` synchronously. `'logged'` is emitted for every record winston hands
   * over, also one the helper level then drops (winston-transport's own level
   * filter runs first; the helper's after it).
   */
  override log(info: unknown, next: () => void): void {
    try {
      writeInfo(info, read(this, 'levels'), this.#name, this.#minimum, stampOf(info) ?? this.#pending);
      /* c8 ignore next 3 -- defensive: writeInfo reads guarded, emitBridgeRecord never throws */
    } catch {
      // A logging call must not break the caller.
    }
    setImmediate(() => this.emit('logged', info));
    next();
  }

  /** Called by winston when the transport is removed or the logger closes: flushes the helper's logs pipeline. */
  override close(): void {
    void flushBridgeRecords();
  }

  /** Before `'finish'` (`logger.end()`): the helper's logs pipeline is flushed first. */
  override _final(callback: (error?: Error | null) => void): void {
    void flushBridgeRecords().then(() => {
      callback();
    });
  }
}

function stampOf(info: unknown): CallStamp | undefined {
  return typeof info === 'object' && info !== null ? REGISTRY.stamps.get(info) : undefined;
}

type Levels = Readonly<Record<string, unknown>>;

/** The priority of a level name, when the levels define it as a number. */
function priorityOf(levels: Levels, name: string): number | undefined {
  const p = Object.hasOwn(levels, name) ? read(levels, name) : undefined;
  return typeof p === 'number' ? p : undefined;
}

function read(holder: object, key: PropertyKey): unknown {
  try {
    return (holder as Record<PropertyKey, unknown>)[key];
  } catch {
    return undefined;
  }
}

function isError(value: unknown): value is Error {
  try {
    return value instanceof Error;
  } catch {
    return false;
  }
}

/**
 * The record level of a winston level name. With the logger's levels the
 * name is placed against the anchors `error`, `warn`/`warning`, `info`,
 * `debug` that the levels define (npm: `http`, `verbose` → debug, `silly` →
 * trace; syslog: `emerg`, `alert`, `crit` → fatal, `notice` → info; cli:
 * `help`, `data` → info, `prompt`, `verbose`, `input`, `silly` → trace); a
 * name the levels do not know is info.
 */
function recordLevel(name: string, levels: Levels | undefined): RecordLevel {
  const priority = levels === undefined ? undefined : priorityOf(levels, name);
  if (levels === undefined || priority === undefined) return byName(name);
  const anchor = (...names: string[]): number | undefined => {
    for (const n of names) {
      const p = priorityOf(levels, n);
      if (p !== undefined) return p;
    }
    return undefined;
  };
  const error = anchor('error');
  if (error !== undefined && priority < error) return 'fatal';
  for (const [level, names] of [
    ['error', ['error']],
    ['warn', ['warn', 'warning']],
    ['info', ['info']],
    ['debug', ['debug']],
  ] as const) {
    const p = anchor(...names);
    if (p !== undefined && priority <= p) return level;
  }
  return anchor('debug') !== undefined ? 'trace' : byName(name);
}

function byName(name: string): RecordLevel {
  switch (name) {
    case 'error':
      return 'error';
    case 'warn':
    case 'warning':
      return 'warn';
    case 'debug':
      return 'debug';
    default:
      return 'info';
  }
}

/** The record's own error, the info fields it makes redundant, and whether it is winston's exception-handler shape. */
interface Leading {
  readonly error: Error | undefined;
  readonly skip: ReadonlySet<string>;
  readonly handled: boolean;
}

/**
 * The record's error, in order: winston's exception-handler shape
 * (`{exception: true, error}`: `handleExceptions`/`handleRejections`), the
 * info itself (`logger.error(err)`), the first `Error` of the splat
 * (`logger.error(msg, err)`), a stack text of `format.errors({stack: true})`
 * (the Error was flattened before the transport: rebuilt from `stack`).
 */
function leadingError(info: object): Leading {
  const stack = read(info, 'stack');
  const withStack = (error: Error, extra: readonly string[] = [], handled = false): Leading => {
    const skip = new Set(extra);
    // winston copies the Error's stack onto the info: it is the exception's, not a field.
    if (typeof stack === 'string' && stack === read(error, 'stack')) skip.add('stack');
    return { error, skip, handled };
  };
  const handledError = read(info, 'error');
  if (read(info, 'exception') === true && isError(handledError)) return withStack(handledError, [...EXCEPTION_INFO], true);
  if (isError(info)) return { error: info, skip: new Set(), handled: false };
  const splat = read(info, SPLAT);
  if (Array.isArray(splat)) {
    for (const item of splat as unknown[]) if (isError(item)) return withStack(item);
  }
  const rebuilt = typeof stack === 'string' ? fromStack(stack, read(info, 'message'), read(info, 'code')) : undefined;
  return rebuilt === undefined ? { error: undefined, skip: new Set(), handled: false } : { error: rebuilt, skip: new Set(['stack']), handled: false };
}

/**
 * An Error rebuilt from a flattened stack text: only a real stack (a head
 * line and at least one `    at ` frame); the head is `Name`, `Name: message`
 * or `Name [CODE]: message`. A bare `Name` takes its message from the info's
 * `message`. The info's `code` (else the bracketed one) is the error's `code`,
 * which the SDK writes as `exception.type`, as for a live Error.
 */
function fromStack(stack: string, message: unknown, code: unknown): Error | undefined {
  if (!STACK_FRAME.test(stack)) return undefined;
  const head = STACK_HEAD.exec(stack.replace(/\n[\s\S]*$/, ''));
  if (head === null) return undefined;
  const [, name, bracketCode, headMessage] = head;
  const error = new Error(headMessage ?? (typeof message === 'string' ? message : ''));
  const props: PropertyDescriptorMap = {
    name: { value: name, configurable: true, writable: true },
    stack: { value: stack, configurable: true, writable: true },
  };
  const errorCode = typeof code === 'string' || typeof code === 'number' ? code : bracketCode;
  if (errorCode !== undefined) props.code = { value: errorCode, configurable: true, writable: true };
  Object.defineProperties(error, props);
  return error;
}

function writeInfo(info: unknown, levels: unknown, fallbackName: string, minimum: LogLevel | undefined, stamp: CallStamp | undefined): void {
  if (typeof info !== 'object' || info === null) return;
  const raw = read(info, LEVEL);
  const shown = read(info, 'level');
  const levelName = typeof raw === 'string' ? raw : typeof shown === 'string' ? shown : 'info';
  const level = recordLevel(levelName, typeof levels === 'object' && levels !== null ? (levels as Levels) : undefined);
  if (!recordEnabled(minimum, level)) return;
  // Never the context active at delivery: a buffered record would land on another trace.
  const call = { context: stamp?.context ?? ROOT_CONTEXT, time: stamp?.time };
  let keys: string[];
  try {
    keys = Object.keys(info);
  } catch {
    // Unlistable (a hostile Proxy): the core fails listing it too, and reports the dropped record.
    emitBridgeRecord({ logger: fallbackName, minimum, level, message: '', attributes: info as Record<string, unknown>, ...call });
    return;
  }
  const { error, skip, handled } = leadingError(info);
  const attributes: Record<string, unknown> = {};
  for (const key of keys) {
    if (DROPPED.has(key) || skip.has(key)) continue;
    // Read by the core through a getter, so a throwing accessor of the app's
    // info is marked `[unreadable accessor]` there (the core's guarded reads).
    Object.defineProperty(attributes, key, { get: () => (info as Record<string, unknown>)[key], enumerable: true, configurable: true });
  }
  let message = read(info, 'message');
  // winston's exception-handler message is `uncaughtException: <msg>\n<stack>`: the stack is the exception's.
  if (handled && typeof message === 'string') message = message.replace(/\n[\s\S]*$/, '');
  emitBridgeRecord({ logger: fallbackName, minimum, level, severityText: levelName.toUpperCase(), message, attributes, error, ...call });
}
