'use strict';
// Placeholder contract (task B1a): the entry loads and the factory is loud.
const test = require('node:test');
const assert = require('node:assert/strict');
const pkg = require('../package.json');
const mod = require('@staffops/otel-helper-aws');

test('@staffops/otel-helper-aws exports awsInstrumentation', () => {
  assert.equal(typeof mod.awsInstrumentation, 'function');
});

test('awsInstrumentation() throws until task B10c', () => {
  assert.throws(() => mod.awsInstrumentation(), { message: /not implemented yet \(task B10c/ });
});

test('peer dependency on the core pins the same version', () => {
  assert.equal(pkg.peerDependencies['@staffops/otel-helper'], pkg.version);
  assert.equal(require('@staffops/otel-helper/package.json').version, pkg.version);
});
