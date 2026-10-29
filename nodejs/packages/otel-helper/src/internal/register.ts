import { isInternalThread } from 'node:worker_threads';
import { installHooks } from './hooks';

/**
 * Body of `@staffops/otel-helper/register`: hooks, then (task B9) `setup()`
 * from the environment. Skipped in Node's internal threads, where `--require`
 * preloads also run (spike-B1c.md F2). Returns whether it ran.
 */
export function runRegister(internalThread: boolean = isInternalThread): boolean {
  if (internalThread) return false;
  installHooks();
  return true;
}
