'use strict';
// Package contract (task B1a, implemented in B10d): the entry loads, and the
// peer dependencies pin the core at the same version and range winston.
const test = require('node:test');
const assert = require('node:assert/strict');
const pkg = require('../package.json');
const mod = require('@staffops/otel-helper-winston');

test('@staffops/otel-helper-winston exports OtelHelperTransport only', () => {
  assert.deepEqual(Object.keys(mod).filter((k) => k !== '__esModule'), ['OtelHelperTransport']);
  assert.equal(typeof mod.OtelHelperTransport, 'function');
});

test('peer dependencies: the core at the same version, winston by range', () => {
  assert.equal(pkg.peerDependencies['@staffops/otel-helper'], pkg.version);
  assert.equal(require('@staffops/otel-helper/package.json').version, pkg.version);
  assert.equal(pkg.peerDependencies.winston, '^3.10.0');
  assert.equal(pkg.dependencies, undefined, 'no runtime dependency of its own');
});
