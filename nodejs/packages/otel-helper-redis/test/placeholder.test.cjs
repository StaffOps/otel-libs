'use strict';
// Placeholder contract (task B1a): the entry loads and the factory is loud.
const test = require('node:test');
const assert = require('node:assert/strict');
const pkg = require('../package.json');
const mod = require('@staffops/otel-helper-redis');

test('@staffops/otel-helper-redis exports redisInstrumentation', () => {
  assert.equal(typeof mod.redisInstrumentation, 'function');
});

test('redisInstrumentation() throws until task B10b', () => {
  assert.throws(() => mod.redisInstrumentation(), { message: /not implemented yet \(task B10b/ });
});

test('peer dependency on the core pins the same version', () => {
  assert.equal(pkg.peerDependencies['@staffops/otel-helper'], pkg.version);
  assert.equal(require('@staffops/otel-helper/package.json').version, pkg.version);
});
