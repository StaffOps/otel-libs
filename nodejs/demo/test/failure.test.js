'use strict';

const assert = require('node:assert/strict');
const { test } = require('node:test');
const { SpanStatusCode } = require('@opentelemetry/api');

const { DemoUnhandledError, ERROR_TYPE, OrderExistsError, OrderRejectedError } = require('../lib/errors');
const { DEMO_SCOPE, errorTypeOf, markPropagated } = require('../lib/failure');

function fakeSpan() {
  const span = { status: undefined, attributes: {}, events: [] };
  span.setStatus = (s) => { span.status = s; };
  span.setAttribute = (k, v) => { span.attributes[k] = v; };
  span.addEvent = (name, attributes) => { span.events.push({ name, attributes }); };
  return span;
}

test('the demo scope name and the override key are pinned', () => {
  assert.equal(DEMO_SCOPE, 'otel-libs-demo');
  assert.equal(ERROR_TYPE, Symbol.for('otel.error_type'));
});

test('demo errors carry the pinned type names under the override key and no code', () => {
  for (const [Cls, name] of [[OrderRejectedError, 'OrderRejectedError'], [DemoUnhandledError, 'DemoUnhandledError']]) {
    const err = new Cls('m', { cause: new Error('c') });
    assert.equal(err.name, name);
    assert.equal(err[ERROR_TYPE], name);
    assert.equal(err.code, undefined);
    assert.equal(err.message, 'm');
    assert.equal(err.cause.message, 'c');
    assert.equal(errorTypeOf(err), name);
  }
  // The driver error is kept off `cause` and off the enumerable keys, so the
  // failure record's exception.message stays the pinned message.
  const driver = Object.assign(new Error('duplicate order dup-r1'), { code: '23505' });
  const exists = new OrderExistsError('order dup-r1 exists', driver);
  assert.equal(exists.name, 'OrderExistsError');
  assert.equal(exists[ERROR_TYPE], 'OrderExistsError');
  assert.equal(exists.cause, undefined);
  assert.equal(exists.driverError, driver);
  assert.deepEqual(Object.keys(exists), ['name']);
  assert.equal(exists.code, undefined);
  assert.equal(errorTypeOf(exists), 'OrderExistsError');
});

test('errorTypeOf follows the helper rule: override, name, constructor name, Error', () => {
  // The override wins over the name (the helper reads it the same way).
  const renamed = new OrderRejectedError('m');
  renamed.name = 'Other';
  assert.equal(errorTypeOf(renamed), 'OrderRejectedError');
  const typed = new TypeError('x');
  assert.equal(errorTypeOf(typed), 'TypeError');
  class CustomFailure extends Error {}
  assert.equal(errorTypeOf(new CustomFailure('x')), 'CustomFailure');
  const empty = Object.assign(new Error('x'), { [ERROR_TYPE]: '' });
  assert.equal(errorTypeOf(empty), 'Error');
  assert.equal(errorTypeOf(new Error('plain')), 'Error');
});

test('errorTypeOf falls back to Error for non-errors, nameless errors and hostile values', () => {
  assert.equal(errorTypeOf('text'), 'Error');
  assert.equal(errorTypeOf(undefined), 'Error');
  const nameless = new Error('x');
  nameless.name = '';
  assert.equal(errorTypeOf(nameless), 'Error');
  const anonymous = Object.assign(new Error('x'), { constructor: { name: '' } });
  assert.equal(errorTypeOf(anonymous), 'Error');
  assert.equal(errorTypeOf(Object.assign(new Error('x'), { constructor: undefined })), 'Error');
  const hostile = new Proxy({}, { getPrototypeOf() { throw new Error('trap'); } });
  assert.equal(errorTypeOf(hostile), 'Error');
});

test('errorTypeOf matches the helper: static override, timeout abort, non-Error override, throwing getters', () => {
  // A static member of the class (inherited by subclasses), as a Python class attribute.
  class Rejected extends Error {
    static get [ERROR_TYPE]() { return 'PinnedByClass'; }
  }
  class Subclass extends Rejected {}
  assert.equal(errorTypeOf(new Rejected('m')), 'PinnedByClass');
  assert.equal(errorTypeOf(new Subclass('m')), 'PinnedByClass');
  // Node core APIs given AbortSignal.timeout() reject with an AbortError caused by a TimeoutError.
  const timeout = Object.assign(new Error('t'), { name: 'TimeoutError' });
  assert.equal(errorTypeOf(Object.assign(new Error('aborted', { cause: timeout }), { name: 'AbortError' })), 'TimeoutError');
  assert.equal(errorTypeOf(Object.assign(new Error('aborted'), { name: 'AbortError' })), 'AbortError');
  assert.equal(errorTypeOf(Object.assign(new Error('aborted', { cause: 'x' }), { name: 'AbortError' })), 'AbortError');
  // An override on a non-Error value wins; without one a non-Error is "Error".
  assert.equal(errorTypeOf({ [ERROR_TYPE]: 'PlainObjectFailure' }), 'PlainObjectFailure');
  assert.equal(errorTypeOf({ name: 'NotAnError' }), 'Error');
  // A throwing override getter falls through to the next rule.
  const trap = new TypeError('x');
  Object.defineProperty(trap, ERROR_TYPE, { get() { throw new Error('trap'); } });
  assert.equal(errorTypeOf(trap), 'TypeError');
  const ctorTrap = new RangeError('x');
  Object.defineProperty(ctorTrap, 'constructor', { get() { throw new Error('trap'); } });
  assert.equal(errorTypeOf(ctorTrap), 'RangeError');
  const nameTrap = new Error('x');
  Object.defineProperty(nameTrap, 'name', { get() { throw new Error('trap'); } });
  assert.equal(errorTypeOf(nameTrap), 'Error');
});

test('markPropagated: ERROR without description, error.type as text, no event', () => {
  const span = fakeSpan();
  markPropagated(span, 422);
  assert.deepEqual(span.status, { code: SpanStatusCode.ERROR });
  assert.deepEqual(span.attributes, { 'error.type': '422' });
  assert.deepEqual(span.events, []);
});
