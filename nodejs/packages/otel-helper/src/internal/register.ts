import { isInternalThread, isMainThread } from 'node:worker_threads';
import { redactErrorText, report } from './diagnostics';
import { installHooks } from './hooks';
import { setupFromRegister } from './setup';

/**
 * Body of `@staffops/otel-helper/register`: the hooks, then `setup()` from
 * the environment (spec JS5, design D-JS11). Skipped in Node's internal
 * threads, where `--require` preloads also run (spike-B1c.md F2). In a worker
 * thread (where Node runs `--require` preloads again) only the hooks are
 * installed, silently: the main thread's setup covers the process, and a
 * worker that wants its own telemetry calls `setup()` itself (B9 fix round M2,
 * final round). A failed setup is
 * reported, never thrown: a preload must not crash the app. Returns whether
 * it ran.
 */
export function runRegister(internalThread: boolean = isInternalThread, mainThread: boolean = isMainThread): boolean {
  if (internalThread) return false;
  installHooks();
  // Silent in a worker (B9 final round): one line per worker was noise; the
  // HOW-TO says a worker calls setup() itself for its own telemetry.
  if (!mainThread) return true;
  try {
    setupFromRegister();
  } catch (err) {
    report('setup() from @staffops/otel-helper/register failed; telemetry is not installed', {
      error: redactErrorText(err instanceof Error ? err.message : String(err)),
    });
  }
  return true;
}
