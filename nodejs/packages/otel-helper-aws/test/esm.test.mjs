// The ESM wrapper re-exports exactly the CommonJS named exports (D-JS10).
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import * as esm from '@staffops/otel-helper-aws';

const require = createRequire(import.meta.url);

test('ESM and CJS entries expose the same implementation', () => {
  const cjs = require('@staffops/otel-helper-aws');
  const cjsNames = Object.keys(cjs).filter((k) => k !== '__esModule').sort();
  assert.deepEqual(Object.keys(esm).sort(), cjsNames);
  for (const name of cjsNames) assert.equal(esm[name], cjs[name], name);
});
