'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const pkg = require('../package.json');
const helper = require('@staffops/otel-helper');

test('VERSION matches package.json', () => {
  assert.equal(helper.VERSION, pkg.version);
});

test('engines floor is Node 22.15.0 (JS1)', () => {
  assert.equal(pkg.engines.node, '>=22.15.0');
});

test('@opentelemetry/api peer range equals the one the SDK 3.0 packages declare (D-JS2)', () => {
  // sdk-trace does not export ./package.json; read it next to its entry (dist/index.cjs).
  const sdkPkg = path.join(path.dirname(require.resolve('@opentelemetry/sdk-trace')), '..', 'package.json');
  const sdkPeer = JSON.parse(fs.readFileSync(sdkPkg, 'utf8')).peerDependencies['@opentelemetry/api'];
  assert.equal(pkg.peerDependencies['@opentelemetry/api'], sdkPeer);
});

// One import-in-the-middle copy: the helper's hooks and the instrumentations'
// Hook() must share its registry (spike-B1c.md check 3).
const readPkg = (entry) => JSON.parse(fs.readFileSync(path.join(path.dirname(entry), '..', 'package.json'), 'utf8'));
// Same major caret range, floor >= instrumentation's: always dedupes, and an
// app's iitm 3.x newer than both cannot nest a second copy under the helper.
// (instrumentation 0.300.0 declares ^3.0.0; the helper needs 3.5's sync-hooks entries.)
test('the import-in-the-middle range is a caret subset of the one @opentelemetry/instrumentation declares', () => {
  const inst = readPkg(require.resolve('@opentelemetry/instrumentation')).dependencies['import-in-the-middle'];
  const mine = pkg.dependencies['import-in-the-middle'];
  const parse = (r) => { const m = /^\^(\d+)\.(\d+)\.(\d+)$/.exec(r); assert.ok(m, `caret range expected: ${r}`); return m.slice(1).map(Number); };
  const [a, b] = [parse(mine), parse(inst)];
  assert.equal(a[0], b[0], 'same major');
  assert.ok(a[1] > b[1] || (a[1] === b[1] && a[2] >= b[2]), `${mine} floor below ${inst}`);
  assert.equal(mine, '^3.5.3');
});

test('the helper and @opentelemetry/instrumentation load the same import-in-the-middle files', () => {
  const helperDir = path.join(__dirname, '..', 'dist', 'internal');
  const instDir = path.dirname(require.resolve('@opentelemetry/instrumentation', { paths: [helperDir] }));
  const fromInstrumentation = path.dirname(require.resolve('import-in-the-middle', { paths: [instDir] }));
  const fromHelper = path.dirname(require.resolve('import-in-the-middle/supports-sync-hooks.mjs', { paths: [helperDir] }));
  assert.equal(fromHelper, fromInstrumentation);
});
