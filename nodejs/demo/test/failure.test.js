'use strict';

const assert = require('node:assert/strict');
const { test } = require('node:test');
const { SpanStatusCode } = require('@opentelemetry/api');

const { DemoUnhandledError, OrderExistsError, OrderRejectedError } = require('../lib/errors');
const { DEMO_SCOPE, errorTypeOf, logFailure, markPropagated, ownFailure, recordFailure } = require('../lib/failure');

function fakeSpan() {
  const span = { status: undefined, attributes: {}, events: [] };
  span.setStatus = (s) => { span.status = s; };
  span.setAttribute = (k, v) => { span.attributes[k] = v; };
  span.addEvent = (name, attributes) => { span.events.push({ name, attributes }); };
  return span;
}

function fakeLog() {
  const records = [];
  return { records, error: (fields, msg) => records.push({ fields, msg }) };
}

test('the demo scope name is pinned', () => {
  assert.equal(DEMO_SCOPE, 'otel-libs-demo');
});

test('demo errors carry the pinned type names and no code', () => {
  for (const [Cls, name] of [[OrderRejectedError, 'OrderRejectedError'], [DemoUnhandledError, 'DemoUnhandledError']]) {
    const err = new Cls('m', { cause: new Error('c') });
    assert.equal(err.name, name);
    assert.equal(err.code, undefined);
    assert.equal(err.message, 'm');
    assert.equal(err.cause.message, 'c');
    assert.equal(errorTypeOf(err), name);
  }
  // The driver error is kept off `cause` and off the enumerable keys, so a log
  // serializer cannot join it into exception.message.
  const driver = Object.assign(new Error('duplicate order dup-r1'), { code: '23505' });
  const exists = new OrderExistsError('order dup-r1 exists', driver);
  assert.equal(exists.name, 'OrderExistsError');
  assert.equal(exists.cause, undefined);
  assert.equal(exists.driverError, driver);
  assert.deepEqual(Object.keys(exists), ['name']);
  assert.equal(exists.code, undefined);
});

test('errorTypeOf falls back to Error for non-errors and nameless errors', () => {
  assert.equal(errorTypeOf('text'), 'Error');
  assert.equal(errorTypeOf(undefined), 'Error');
  const nameless = new Error('x');
  nameless.name = '';
  assert.equal(errorTypeOf(nameless), 'Error');
  const hostile = new Proxy({}, { getPrototypeOf() { throw new Error('trap'); } });
  assert.equal(errorTypeOf(hostile), 'Error');
});

test('recordFailure: ERROR without description, error.type, one exception event with only the type', () => {
  const span = fakeSpan();
  const type = recordFailure(span, new OrderRejectedError('amount over limit rej-r1'));
  assert.equal(type, 'OrderRejectedError');
  assert.deepEqual(span.status, { code: SpanStatusCode.ERROR });
  assert.deepEqual(span.attributes, { 'error.type': 'OrderRejectedError' });
  assert.deepEqual(span.events, [{ name: 'exception', attributes: { 'exception.type': 'OrderRejectedError' } }]);
  assert.ok(!JSON.stringify(span).includes('rej-r1'), 'no run token on the span');
});

test('logFailure: the message is the body and the error goes under err', () => {
  const log = fakeLog();
  const err = new OrderExistsError('order dup-r1 exists');
  logFailure(log, err, { 'demo.scenario': 'db_error' });
  assert.equal(log.records.length, 1);
  assert.equal(log.records[0].msg, 'order dup-r1 exists');
  assert.equal(log.records[0].fields.err, err);
  assert.equal(log.records[0].fields['demo.scenario'], 'db_error');
});

test('logFailure keeps a fixed body for non-errors and hostile values', () => {
  const log = fakeLog();
  logFailure(log, 'plain');
  const hostile = new Proxy({}, { getPrototypeOf() { throw new Error('trap'); } });
  logFailure(log, hostile);
  assert.deepEqual(log.records.map((r) => r.msg), ['error', 'error']);
  assert.deepEqual(Object.keys(log.records[0].fields), ['err']);
});

test('ownFailure records and logs once', () => {
  const span = fakeSpan();
  const log = fakeLog();
  const type = ownFailure(span, log, new DemoUnhandledError('boom unh-r1'), { 'demo.scenario': 'unhandled' });
  assert.equal(type, 'DemoUnhandledError');
  assert.equal(span.events.length, 1);
  assert.equal(log.records.length, 1);
  assert.equal(log.records[0].msg, 'boom unh-r1');
});

test('markPropagated: ERROR without description, error.type as text, no event', () => {
  const span = fakeSpan();
  markPropagated(span, 422);
  assert.deepEqual(span.status, { code: SpanStatusCode.ERROR });
  assert.deepEqual(span.attributes, { 'error.type': '422' });
  assert.deepEqual(span.events, []);
});
