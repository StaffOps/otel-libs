'use strict';
// JS5 / D-JS10: `require` and `import` resolve to one implementation and one state.
const test = require('node:test');
const assert = require('node:assert/strict');
const { runEsm, runCjs, lastJson } = require('./helpers.cjs');

const probe = `
  const key = Symbol.for('staffops.otel-helper.state.v1');
  const out = {
    sameState: viaRequire.getState() === viaImport.getState(),
    isGlobal: viaRequire.getState() === globalThis[key],
    sameFunction: viaRequire.getState === viaImport.getState,
    sameVersion: mainRequire.VERSION === mainImport.VERSION,
    loadedOnce: Object.keys(require.cache).filter((f) => f.endsWith('/otel-helper/dist/internal/state.js')).length,
  };
  viaRequire.getState().marker = 'set-through-require';
  out.markerSeenByImport = viaImport.getState().marker;
  console.log(JSON.stringify(out));
`;
const expected = { sameState: true, isGlobal: true, sameFunction: true, sameVersion: true, loadedOnce: 1, markerSeenByImport: 'set-through-require' };

test('ESM process: import first, then require', () => {
  const res = runEsm(`
    import { createRequire } from 'node:module';
    const require = createRequire(process.cwd() + '/');
    const viaImport = await import('@staffops/otel-helper/testing');
    const mainImport = await import('@staffops/otel-helper');
    const viaRequire = require('@staffops/otel-helper/testing');
    const mainRequire = require('@staffops/otel-helper');
    ${probe}
  `);
  assert.deepEqual(lastJson(res), expected);
  assert.equal(res.stderr, '');
});

test('CJS process: require first, then import', () => {
  const res = runCjs(`
    (async () => {
      const viaRequire = require('@staffops/otel-helper/testing');
      const mainRequire = require('@staffops/otel-helper');
      const viaImport = await import('@staffops/otel-helper/testing');
      const mainImport = await import('@staffops/otel-helper');
      ${probe}
    })().catch((e) => { console.error(e); process.exit(1); });
  `);
  assert.deepEqual(lastJson(res), expected);
  assert.equal(res.stderr, '');
});
