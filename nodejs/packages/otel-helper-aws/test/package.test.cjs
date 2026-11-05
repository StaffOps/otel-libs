'use strict';
// Package contract (task B1a, implemented by B10c): the entry loads, the
// version matches package.json, the core peer pins the same version, the
// shared OTel ranges are the core's and the contrib is matched to them (D-JS2).
// Behavior: aws.test.cjs (unit), sdk.test.cjs (the AWS SDK against a fake endpoint).
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const pkg = require('../package.json');
const mod = require('@staffops/otel-helper-aws');

test('@staffops/otel-helper-aws exports awsInstrumentation and VERSION', () => {
  assert.equal(typeof mod.awsInstrumentation, 'function');
  assert.equal(mod.VERSION, pkg.version);
  assert.deepEqual(Object.keys(mod).filter((k) => k !== '__esModule').sort(), ['VERSION', 'awsInstrumentation']);
});

test('peer dependency on the core pins the same version', () => {
  assert.equal(pkg.peerDependencies['@staffops/otel-helper'], pkg.version);
  assert.equal(require('@staffops/otel-helper/package.json').version, pkg.version);
});

test('the @opentelemetry/api peer range is the core one; the AWS SDK is not a dependency (JS8)', () => {
  const core = require('@staffops/otel-helper/package.json');
  assert.equal(pkg.peerDependencies['@opentelemetry/api'], core.peerDependencies['@opentelemetry/api']);
  for (const section of ['dependencies', 'peerDependencies']) {
    for (const dep of Object.keys(pkg[section] ?? {})) assert.doesNotMatch(dep, /^@(aws-sdk|smithy)\//, `${dep} is the app's`);
  }
  // Official OTel packages only (AGENTS.md principle 1).
  for (const dep of Object.keys(pkg.dependencies)) assert.match(dep, /^@opentelemetry\//);
});

test('@opentelemetry/instrumentation is the core range (one copy, one require hook; D-JS2)', () => {
  const core = require('@staffops/otel-helper/package.json');
  assert.equal(pkg.dependencies['@opentelemetry/instrumentation'], core.dependencies['@opentelemetry/instrumentation']);
});

test('the contrib is matched to the core instrumentation range (D-JS2)', () => {
  const contrib = '@opentelemetry/instrumentation-aws-sdk';
  assert.match(pkg.dependencies[contrib], /^~0\.\d+\.\d+$/, 'one contrib minor');
  // The contrib does not export ./package.json: read the installed file.
  const file = path.join(path.dirname(require.resolve(contrib)), '..', 'package.json');
  const installed = JSON.parse(fs.readFileSync(file, 'utf8'));
  assert.equal(installed.name, contrib);
  assert.equal(installed.dependencies['@opentelemetry/instrumentation'].replace(/^[~^]/, ''), pkg.dependencies['@opentelemetry/instrumentation'].replace(/^[~^]/, ''));
  const core = require('@staffops/otel-helper/package.json');
  assert.equal(installed.dependencies['@opentelemetry/core'].replace(/^[~^]/, '').split('.')[0], core.dependencies['@opentelemetry/core'].replace(/^[~^]/, '').split('.')[0], 'same SDK major');
});
