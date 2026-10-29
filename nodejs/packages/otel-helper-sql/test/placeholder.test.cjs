'use strict';
// Placeholder contract (task B1a): the entry loads and the factory is loud.
const test = require('node:test');
const assert = require('node:assert/strict');
const pkg = require('../package.json');
const mod = require('@staffops/otel-helper-sql');

test('@staffops/otel-helper-sql exports sqlInstrumentation', () => {
  assert.equal(typeof mod.sqlInstrumentation, 'function');
});

test('sqlInstrumentation() throws until task B10a', () => {
  assert.throws(() => mod.sqlInstrumentation(), { message: /not implemented yet \(task B10a/ });
});

test('peer dependency on the core pins the same version', () => {
  assert.equal(pkg.peerDependencies['@staffops/otel-helper'], pkg.version);
  assert.equal(require('@staffops/otel-helper/package.json').version, pkg.version);
});
