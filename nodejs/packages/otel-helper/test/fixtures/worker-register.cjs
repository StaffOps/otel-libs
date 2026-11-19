'use strict';
// Fixture for test/hooks.test.cjs (B9 fix round M2): run under a /register
// preload; prints whether the main thread was set up and what a worker thread got.
const { Worker } = require('node:worker_threads');
const KEY = Symbol.for('staffops.otel-helper.state.v1');
const worker = new Worker(
  "const s = globalThis[Symbol.for('staffops.otel-helper.state.v1')]; require('node:worker_threads').parentPort.postMessage({ state: s !== undefined, active: !!(s && s.runtime && s.runtime.active) });",
  { eval: true },
);
worker.on('message', (m) => {
  console.log(JSON.stringify({ main: !!globalThis[KEY]?.runtime?.active, worker: m }));
});
