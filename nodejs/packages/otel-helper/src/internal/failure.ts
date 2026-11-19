/**
 * The failure rule of application spans (contract R11, design D15,
 * ADR-016; the detail goes to a correlated log, ADR-014): a failure marks
 * the span with status ERROR without a description and `error.type`, adds
 * no `exception` event, and hands one ERROR record per marked span to the
 * helper's log channel, in that span's context (owner ruling 2026-11-17: no
 * deduplication across spans). A cancellation is not a failure. The helper
 * never sets Ok.
 */
import { context, SpanStatusCode, trace, type Span } from '@opentelemetry/api';
import { emitFailureRecord } from './logging';

/**
 * The override key of `error.type`: a non-empty string under this symbol
 * (own or inherited — a prototype getter — or a `static` member of the
 * value's class) wins over the default. Namespaced, so payload fields such
 * as `errorType` are never read.
 */
export const ERROR_TYPE_KEY = Symbol.for('otel.error_type');

/** Logger name (and instrumentation scope on the OTel path) of the failure records. */
export const FAILURE_LOGGER = 'otel-helper';

const UNREADABLE: unique symbol = Symbol('unreadable');

function isObject(value: unknown): value is object {
  return (typeof value === 'object' && value !== null) || typeof value === 'function';
}

/** `holder[key]`, or {@link UNREADABLE} when a getter or a Proxy trap throws. */
function read(holder: object, key: PropertyKey): unknown {
  try {
    return (holder as Record<PropertyKey, unknown>)[key];
  } catch {
    return UNREADABLE;
  }
}

/** `value instanceof Error`, false when the prototype lookup throws (a Proxy trap). */
function isError(value: unknown): value is Error {
  try {
    return value instanceof Error;
  } catch {
    return false;
  }
}

function nonEmptyString(value: unknown): value is string {
  return typeof value === 'string' && value !== '';
}

/** `String(value)` that never throws (`Object.create(null)`, a throwing `toString`). */
function safeString(value: unknown): string {
  try {
    return String(value);
  } catch {
    try {
      return Object.prototype.toString.call(value);
    } catch {
      return 'unknown error';
    }
  }
}

/**
 * An `AbortError` whose `cause` is a `TimeoutError`: how Node core APIs
 * (`timers/promises`, `fs`, `events`, `stream`) reject when given
 * `AbortSignal.timeout()`. A timeout, so a failure (design D15).
 */
function isTimeoutAbort(err: object): boolean {
  if (read(err, 'name') !== 'AbortError') return false;
  const cause = read(err, 'cause');
  return isObject(cause) && read(cause, 'name') === 'TimeoutError';
}

/**
 * The `error.type` of a failure (also the record's `exception.type`): the
 * override under {@link ERROR_TYPE_KEY} (on the value, own or inherited,
 * else a `static` member of its class); `TimeoutError` for an `AbortError`
 * caused by a timeout; else, for an `Error`, its `name` when that is not
 * `"Error"`, else a non-empty `constructor.name`; else `"Error"`.
 */
export function errorTypeOf(err: unknown): string {
  if (isObject(err)) {
    const override = read(err, ERROR_TYPE_KEY);
    if (nonEmptyString(override)) return override;
    // A `static` member (on the class, inherited by subclasses), as a Python class attribute.
    const ctor = read(err, 'constructor');
    if (isObject(ctor)) {
      const classOverride = read(ctor, ERROR_TYPE_KEY);
      if (nonEmptyString(classOverride)) return classOverride;
    }
    if (isTimeoutAbort(err)) return 'TimeoutError';
  }
  if (!isError(err)) return 'Error';
  const name = read(err, 'name');
  if (nonEmptyString(name) && name !== 'Error') return name;
  const ctor = read(err, 'constructor');
  if (isObject(ctor)) {
    const ctorName = read(ctor, 'name');
    if (nonEmptyString(ctorName)) return ctorName;
  }
  return 'Error';
}

/**
 * A cancellation, which leaves the span Unset with no record: `name ===
 * 'AbortError'` (unless its `cause` is a `TimeoutError`), or a grpc-js
 * `ServiceError` with status `CANCELLED` (numeric `code === 1` together with
 * a `metadata` property). A `TimeoutError` (`AbortSignal.timeout()`) is a
 * failure, and so is a cancellation under another error's `cause`.
 */
export function isCancellation(err: unknown): boolean {
  if (!isObject(err)) return false;
  if (read(err, 'name') === 'AbortError') return !isTimeoutAbort(err);
  if (read(err, 'code') !== 1) return false;
  try {
    return 'metadata' in err;
  } catch {
    return false;
  }
}

/** The record body: `err.message` for an `Error` (`''` when unreadable), else the value's string form. */
function messageOf(err: unknown): string {
  if (!isError(err)) return safeString(err);
  const message = read(err, 'message');
  if (typeof message === 'string') return message;
  return message === UNREADABLE || message === undefined ? '' : safeString(message);
}

/**
 * The body and attributes of the record for `err` (ADR-014): the message
 * (`err.message`, or the value's string form), `exception.type` = `type`,
 * `exception.message` and, for an `Error` with a string `stack`,
 * `exception.stacktrace`. Shared with the DB spans of core `internal/db`
 * (task A36).
 */
export function exceptionRecord(err: unknown, type: string): { readonly body: string; readonly attributes: Record<string, string> } {
  const body = messageOf(err);
  const attributes: Record<string, string> = { 'exception.type': type, 'exception.message': body };
  if (isError(err)) {
    const stack = read(err, 'stack');
    if (typeof stack === 'string') attributes['exception.stacktrace'] = stack;
  }
  return { body, attributes };
}

/** The record of a failure, in `span`'s context (its ids, recording or not). */
function emitRecord(err: unknown, type: string, span: Span | undefined): void {
  const { body, attributes } = exceptionRecord(err, type);
  const ctx = span === undefined ? context.active() : trace.setSpan(context.active(), span);
  emitFailureRecord(FAILURE_LOGGER, body, attributes, ctx);
}

/**
 * Applies the rule to `span` (when there is one): status ERROR without a
 * description and `error.type`, then one record in its context (D15
 * "Nesting": one record per marked span, no deduplication). Each step is
 * guarded on its own, so span marking never depends on the log pipeline.
 * Never throws.
 */
export function markFailure(err: unknown, span: Span | undefined): void {
  const type = errorTypeOf(err);
  if (span !== undefined) {
    try {
      span.setStatus({ code: SpanStatusCode.ERROR });
    } catch {
      // Marking must never replace the application's own error.
    }
    try {
      span.setAttribute('error.type', type);
    } catch {
      // as above
    }
  }
  emitRecord(err, type, span);
}

/** A span-like value (what the SDK and the no-op API return). */
function isSpan(value: unknown): value is Span {
  return isObject(value) && typeof read(value, 'setStatus') === 'function' && typeof read(value, 'setAttribute') === 'function';
}

/**
 * Marks a failure the application handles itself (a caught error, a
 * business outcome it wants counted): `span` (default: the active span)
 * gets status ERROR without a description and `error.type`, and one ERROR
 * record carries the message and stack in that span's context, also when
 * the span is not active or not recording. The span is not ended. It marks
 * whatever it is given, a cancellation included, and always writes a record.
 * With no span at all, only the record is written (in the active context).
 *
 * `error.type`: a non-empty string under `Symbol.for('otel.error_type')`
 * wins; else the error's `name` when it is not `"Error"`, else its
 * `constructor.name`, else `"Error"`.
 *
 * @throws TypeError when `err` is `undefined` or `null`, or `span` is given
 * and is not a span (R8).
 */
export function recordFailure(err: unknown, span?: Span): void {
  if (err === undefined || err === null) throw new TypeError(`recordFailure: err must not be ${String(err)}`);
  if (span !== undefined && !isSpan(span)) throw new TypeError('recordFailure: span must be a Span');
  markFailure(err, span ?? trace.getActiveSpan());
}
