'use strict';

const assert = require('node:assert/strict');
const { test } = require('node:test');
const grpc = require('@grpc/grpc-js');

const { DemoUnhandledError, OrderExistsError, OrderRejectedError } = require('../lib/errors');
const { CODE, grpcCodeName, grpcErrorFor, httpStatusForGrpc } = require('../lib/grpc-status');

test('codes match grpc-js', () => {
  for (const [name, value] of Object.entries(CODE)) assert.equal(value, grpc.status[name], name);
});

test('grpcErrorFor uses constant messages, never the exception message', () => {
  assert.deepEqual(grpcErrorFor(new OrderRejectedError('amount over limit rej-r1')), { code: 9, details: 'order rejected' });
  assert.deepEqual(grpcErrorFor(new OrderExistsError('order dup-r1 exists')), { code: 6, details: 'order exists' });
  assert.deepEqual(grpcErrorFor(new DemoUnhandledError('boom')), { code: 13, details: 'order failed' });
  assert.deepEqual(grpcErrorFor(new Error('db down')), { code: 13, details: 'order failed' });
});

test('httpStatusForGrpc: 422, 409, else 502', () => {
  assert.equal(httpStatusForGrpc(CODE.FAILED_PRECONDITION), 422);
  assert.equal(httpStatusForGrpc(CODE.ALREADY_EXISTS), 409);
  assert.equal(httpStatusForGrpc(CODE.UNAVAILABLE), 502);
  assert.equal(httpStatusForGrpc(CODE.DEADLINE_EXCEEDED), 502);
});

test('grpcCodeName matches grpc-js names; unknown numbers as text', () => {
  for (const [name, value] of Object.entries(grpc.status)) {
    if (typeof value === 'number') assert.equal(grpcCodeName(value), name);
  }
  assert.equal(grpcCodeName(99), '99');
  assert.equal(grpcCodeName(undefined), 'undefined');
});
