'use strict';
// Placeholder contract (task B1a): the entry loads and the factory is loud.
const test = require('node:test');
const assert = require('node:assert/strict');
const pkg = require('../package.json');
const mod = require('@staffops/otel-helper-winston');

test('@staffops/otel-helper-winston exports OtelHelperTransport', () => {
  assert.equal(typeof mod.OtelHelperTransport, 'function');
});

test('OtelHelperTransport throws until task B10d', () => {
  assert.throws(() => new mod.OtelHelperTransport(), { message: /not implemented yet \(task B10d/ });
});

test('peer dependency on the core pins the same version', () => {
  assert.equal(pkg.peerDependencies['@staffops/otel-helper'], pkg.version);
  assert.equal(require('@staffops/otel-helper/package.json').version, pkg.version);
});
