'use strict';

const assert = require('node:assert/strict');
const { test } = require('node:test');

const { createDemoLogger } = require('../lib/logging');

test('createDemoLogger returns a named pino logger on the helper destination', () => {
  const log = createDemoLogger('demo-node-test');
  assert.equal(typeof log.info, 'function');
  assert.equal(typeof log.error, 'function');
  assert.equal(log.bindings().name, 'demo-node-test');
});
