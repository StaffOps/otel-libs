// Failure helpers of the demo spans (specs/demo-environment DE-D8). Owning
// spans use the helper's withSpan (contract design D15, A45): it marks the
// span (status ERROR without description, error.type, no exception event) and
// writes the one ERROR record with the message and the stack in the span's
// context. Spans that only propagate a failure are marked here by hand,
// without a record.
'use strict';

const { SpanStatusCode } = require('@opentelemetry/api');

const { ERROR_TYPE } = require('./errors');

/** Tracer and meter (instrumentation scope) name of every demo span and instrument. */
const DEMO_SCOPE = 'otel-libs-demo';

const UNREADABLE = Symbol('unreadable');

function isObject(value) {
  return (typeof value === 'object' && value !== null) || typeof value === 'function';
}

/** `holder[key]`, or UNREADABLE when a getter or a Proxy trap throws. */
function read(holder, key) {
  try {
    return holder[key];
  } catch {
    return UNREADABLE;
  }
}

function isError(value) {
  try {
    return value instanceof Error;
  } catch {
    return false;
  }
}

function nonEmptyString(value) {
  return typeof value === 'string' && value !== '';
}

/**
 * The error.type value the helper gives `err` (the rule of the helper's
 * `errorTypeOf`, `packages/otel-helper/src/internal/failure.ts`), so the
 * metric attribute (DE-D9) equals the span attribute: the override under
 * `Symbol.for('otel.error_type')` on the value (own or inherited, an Error or
 * not), else a `static` override on its class; `TimeoutError` for an
 * `AbortError` caused by a `TimeoutError`; else, for an Error, its `name`
 * when that is not "Error", else a non-empty `constructor.name`; else
 * "Error". A throwing getter or Proxy trap falls through to the next rule.
 */
function errorTypeOf(err) {
  if (isObject(err)) {
    const override = read(err, ERROR_TYPE);
    if (nonEmptyString(override)) return override;
    const ctor = read(err, 'constructor');
    if (isObject(ctor)) {
      const classOverride = read(ctor, ERROR_TYPE);
      if (nonEmptyString(classOverride)) return classOverride;
    }
    if (read(err, 'name') === 'AbortError') {
      const cause = read(err, 'cause');
      if (isObject(cause) && read(cause, 'name') === 'TimeoutError') return 'TimeoutError';
    }
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
 * A parent demo span that only propagates a failure: status ERROR without a
 * description and `error.type` (the answered HTTP status, or the transport
 * failure's type name), no event, no record (DE-D8).
 */
function markPropagated(span, errorType) {
  span.setStatus({ code: SpanStatusCode.ERROR });
  span.setAttribute('error.type', String(errorType));
}

module.exports = { DEMO_SCOPE, errorTypeOf, markPropagated };
