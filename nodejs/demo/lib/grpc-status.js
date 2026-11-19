// gRPC status mapping of the demo (design "Scenario table"): constant status
// messages, never the exception message (it carries the run token).
'use strict';

const { OrderExistsError, OrderRejectedError } = require('./errors');

/** The gRPC codes the demo uses (grpc.status values, kept here so the mapping is testable without grpc-js). */
const CODE = Object.freeze({
  OK: 0,
  INVALID_ARGUMENT: 3,
  DEADLINE_EXCEEDED: 4,
  NOT_FOUND: 5,
  ALREADY_EXISTS: 6,
  FAILED_PRECONDITION: 9,
  INTERNAL: 13,
  UNAVAILABLE: 14,
});

const NAMES = Object.freeze([
  'OK', 'CANCELLED', 'UNKNOWN', 'INVALID_ARGUMENT', 'DEADLINE_EXCEEDED', 'NOT_FOUND',
  'ALREADY_EXISTS', 'PERMISSION_DENIED', 'RESOURCE_EXHAUSTED', 'FAILED_PRECONDITION',
  'ABORTED', 'OUT_OF_RANGE', 'UNIMPLEMENTED', 'INTERNAL', 'UNAVAILABLE', 'DATA_LOSS',
  'UNAUTHENTICATED',
]);

/** The backend's answer for a failed order: code + constant message (unexpected failures: INTERNAL `order failed`, DE-D7 r3.5). */
function grpcErrorFor(err) {
  if (err instanceof OrderRejectedError) return { code: CODE.FAILED_PRECONDITION, details: 'order rejected' };
  if (err instanceof OrderExistsError) return { code: CODE.ALREADY_EXISTS, details: 'order exists' };
  return { code: CODE.INTERNAL, details: 'order failed' };
}

/** The api's HTTP status for a failed PlaceOrder: 422, 409, else 502 (a backend fault). */
function httpStatusForGrpc(code) {
  if (code === CODE.FAILED_PRECONDITION) return 422;
  if (code === CODE.ALREADY_EXISTS) return 409;
  return 502;
}

/** The status code name (`FAILED_PRECONDITION`), the gRPC form of error.type; unknown numbers as text. */
function grpcCodeName(code) {
  return Number.isInteger(code) && code >= 0 && code < NAMES.length ? NAMES[code] : String(code);
}

module.exports = { CODE, grpcCodeName, grpcErrorFor, httpStatusForGrpc };
