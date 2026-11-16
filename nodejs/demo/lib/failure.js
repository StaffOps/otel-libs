// ADR-014 by hand for demo spans (specs/demo-environment DE-D8): the owning
// span keeps only the exception type; the message and stack go to one ERROR
// log record written while that span is current.
'use strict';

const { SpanStatusCode } = require('@opentelemetry/api');

/** Tracer and meter (instrumentation scope) name of every demo span and instrument. */
const DEMO_SCOPE = 'otel-libs-demo';

/** The error's type name (the pinned literal for the demo errors); "Error" when it has none. */
function errorTypeOf(err) {
  try {
    if (err instanceof Error && typeof err.name === 'string' && err.name !== '') return err.name;
  } catch {
    // a hostile Proxy: treated as a non-Error value
  }
  return 'Error';
}

/**
 * Marks `span` as the owning span of a failure: status ERROR without a
 * description, `error.type`, and one `exception` event with only
 * `exception.type` (never span.recordException, which adds the message and
 * stack). Returns the type name.
 */
function recordFailure(span, err) {
  const type = errorTypeOf(err);
  span.setStatus({ code: SpanStatusCode.ERROR });
  span.setAttribute('error.type', type);
  span.addEvent('exception', { 'exception.type': type });
  return type;
}

/**
 * The ERROR record of a failure, through pino: the message is the body, the
 * error under pino's `err` key becomes exception.type / exception.message /
 * exception.stacktrace (@staffops/otel-helper-pino). Call it while the owning
 * span is current, before any rethrow, so the record carries its ids.
 */
function logFailure(log, err, fields = {}) {
  let message = 'error';
  try {
    if (err instanceof Error && typeof err.message === 'string') message = err.message;
  } catch {
    // keep the fallback body
  }
  log.error({ ...fields, err }, message);
}

/** recordFailure + logFailure on the current owning span; returns the type name. */
function ownFailure(span, log, err, fields = {}) {
  const type = recordFailure(span, err);
  logFailure(log, err, fields);
  return type;
}

/**
 * A parent demo span that only propagates a failure: status ERROR without a
 * description and `error.type`, no event, no log (DE-D8).
 */
function markPropagated(span, errorType) {
  span.setStatus({ code: SpanStatusCode.ERROR });
  span.setAttribute('error.type', String(errorType));
}

module.exports = { DEMO_SCOPE, errorTypeOf, logFailure, markPropagated, ownFailure, recordFailure };
