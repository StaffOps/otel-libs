'use strict';
// Package contract (task B1a, implemented by B10a): the entry loads, the
// version matches package.json, the core peer pins the same version and the
// shared OTel ranges are the core's. Behavior: sql.test.cjs, db.test.cjs.
const test = require('node:test');
const assert = require('node:assert/strict');
const pkg = require('../package.json');
const mod = require('@staffops/otel-helper-sql');

test('@staffops/otel-helper-sql exports sqlInstrumentation and VERSION', () => {
  assert.equal(typeof mod.sqlInstrumentation, 'function');
  assert.equal(mod.VERSION, pkg.version);
});

test('peer dependency on the core pins the same version', () => {
  assert.equal(pkg.peerDependencies['@staffops/otel-helper'], pkg.version);
  assert.equal(require('@staffops/otel-helper/package.json').version, pkg.version);
});

test('the @opentelemetry/api peer range is the core one; the instrumented libraries are not dependencies (JS8)', () => {
  const core = require('@staffops/otel-helper/package.json');
  assert.equal(pkg.peerDependencies['@opentelemetry/api'], core.peerDependencies['@opentelemetry/api']);
  for (const lib of ['pg', 'mysql2']) {
    assert.ok(!(lib in (pkg.dependencies ?? {})), `${lib} is the app's`);
    assert.ok(!(lib in (pkg.peerDependencies ?? {})), `${lib} is the app's`);
  }
  // Official OTel packages only (AGENTS.md principle 1).
  for (const dep of Object.keys(pkg.dependencies)) assert.match(dep, /^@opentelemetry\//);
});

test('@opentelemetry/instrumentation is the core range (one copy, one require hook; D-JS2)', () => {
  const core = require('@staffops/otel-helper/package.json');
  assert.equal(pkg.dependencies['@opentelemetry/instrumentation'], core.dependencies['@opentelemetry/instrumentation']);
});
