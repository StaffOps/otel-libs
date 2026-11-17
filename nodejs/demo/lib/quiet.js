// Tracing suppression and readiness waits of the demo (specs/demo-environment
// DE-D11 r3.6): no spans for the worker's queue reads (XREADGROUP) and for
// the start-up readiness probes.
'use strict';

const { setTimeout: sleep } = require('node:timers/promises');
const { context } = require('@opentelemetry/api');
const { suppressTracing } = require('@opentelemetry/core');

/** Runs `fn` with tracing suppressed: instrumented calls inside it create no spans. */
function withoutTracing(fn) {
  return context.with(suppressTracing(context.active()), fn);
}

/**
 * Resolves once `probe()` resolves true; a false or a rejection waits
 * `intervalMs` and tries again until `shouldStop()`. `onWait(attempt)` runs
 * on the first miss only (one log line, not one per attempt).
 */
async function waitUntil(probe, { intervalMs = 1_000, shouldStop = () => false, onWait = () => {}, pause = sleep } = {}) {
  for (let attempt = 1; !shouldStop(); attempt += 1) {
    let ok;
    try {
      ok = (await probe()) === true;
    } catch {
      ok = false;
    }
    if (ok) return true;
    if (attempt === 1) onWait(attempt);
    await pause(intervalMs);
  }
  return false;
}

module.exports = { waitUntil, withoutTracing };
