// The ESM wrappers re-export exactly the CommonJS named exports (D-JS10), and
// side-effect entries export nothing. Subpaths come from package.json exports.
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const pkg = require('../package.json');
const subpaths = Object.keys(pkg.exports).filter((p) => p !== './package.json');

test('exports lists the four entry points', () => {
  assert.deepEqual(subpaths.sort(), ['.', './hooks', './register', './testing']);
});

for (const sub of subpaths) {
  const spec = `${pkg.name}${sub.slice(1)}`;
  test(`${spec}: ESM and CJS expose the same names and values`, async () => {
    const esm = await import(spec);
    const cjs = require(spec);
    const cjsNames = Object.keys(cjs).filter((k) => k !== '__esModule').sort();
    assert.deepEqual(Object.keys(esm).sort(), cjsNames);
    for (const name of cjsNames) assert.equal(esm[name], cjs[name], name);
  });
}
