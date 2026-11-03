'use strict';
// Package contract (task B1a, implemented in B10d): the entry loads, and the
// peer dependencies pin the core at the same version and range pino.
const test = require('node:test');
const assert = require('node:assert/strict');
const pkg = require('../package.json');
const mod = require('@staffops/otel-helper-pino');

test('@staffops/otel-helper-pino exports createPinoDestination and the unsupported pino.transport target (default)', () => {
  assert.deepEqual(Object.keys(mod).filter((k) => k !== '__esModule').sort(), ['createPinoDestination', 'default']);
  assert.equal(typeof mod.createPinoDestination, 'function');
});

test('peer dependencies: the core at the same version, pino by range', () => {
  assert.equal(pkg.peerDependencies['@staffops/otel-helper'], pkg.version);
  assert.equal(require('@staffops/otel-helper/package.json').version, pkg.version);
  assert.equal(pkg.peerDependencies.pino, '^10.0.0');
  assert.equal(pkg.dependencies, undefined, 'no runtime dependency of its own');
});
