import * as nodeModule from 'node:module';
import { pathToFileURL } from 'node:url';
import { isInternalThread } from 'node:worker_threads';
import { report } from './diagnostics';
import { getState, type HelperState, type HooksMode } from './state';

interface SupportsSyncHooks {
  supportsSyncHooks(): boolean;
}

interface RegisterSyncHooks {
  register(options?: object): void;
}

/** Process facts {@link installHooks} decides on; injectable for tests. */
export interface HooksEnvironment {
  /** import-in-the-middle's check (Node 22.22.3+, 24.11.1+, 25.1+, 26+). */
  readonly supportsSyncHooks: () => boolean;
  /** True inside Node's internal threads (the module-hooks thread). */
  readonly internalThread: boolean;
  /** True under `node --eval` / `--print` (`process._eval` is set before preloads run). */
  readonly evalMode: boolean;
  /** True where `module.register()` is runtime-deprecated (DEP0205, Node >= 26). */
  readonly registerDeprecated: boolean;
}

function probeSyncHooks(): boolean {
  // eslint-disable-next-line @typescript-eslint/no-require-imports -- require(esm) of an untyped .mjs entry
  return (require('import-in-the-middle/supports-sync-hooks.mjs') as SupportsSyncHooks).supportsSyncHooks();
}

/** The real process facts. */
export function currentEnvironment(): HooksEnvironment {
  return {
    supportsSyncHooks: probeSyncHooks,
    internalThread: isInternalThread,
    evalMode: (process as { _eval?: unknown })._eval !== undefined,
    registerDeprecated: Number.parseInt(process.versions.node, 10) >= 26,
  };
}

// Kept per module instance too, in case the shared state is read-only.
let localMode: HooksMode | undefined;

function recordedMode(state: HelperState): HooksMode | undefined {
  if (localMode !== undefined) return localMode;
  try {
    return state.hooks;
  } catch {
    return undefined;
  }
}

function errorText(err: unknown): string {
  return err instanceof Error ? `${err.name}: ${err.message}` : String(err);
}

/**
 * Installs the import-in-the-middle loader hooks once per process (design
 * D-JS11): synchronous `module.registerHooks` where import-in-the-middle says
 * the running Node supports it, else the asynchronous `module.register`
 * loader. CommonJS `require` patching is require-in-the-middle's and needs no
 * hook. The `.mjs` entries are loaded with `require(esm)` (Node >= 22.12); the
 * version check first, because it has no imports.
 *
 * Exceptions (spike-B1c.md):
 * - Node also runs `--require` preloads in its internal module-hooks thread;
 *   registering there again makes the loader initialize twice (F2): skipped.
 * - Under `--eval`/`--print`, iitm's sync hooks crash the process (F1): the
 *   async loader is used instead, or, where `module.register` is deprecated,
 *   no ESM hook at all (`cjs-only`) with one diagnostic line.
 *
 * Never throws: a failure is reported on the sink and returns `undefined`.
 */
export function installHooks(env: HooksEnvironment = currentEnvironment()): HooksMode | undefined {
  if (env.internalThread) return undefined;
  const state = getState();
  if (state === null) return undefined;
  const prior = recordedMode(state);
  if (prior !== undefined) return prior;
  let mode: HooksMode;
  try {
    if (env.evalMode && env.registerDeprecated) {
      mode = 'cjs-only';
      report('ESM import hooks not installed under --eval/--print on this Node; only require() is instrumented', {
        node: process.versions.node,
      });
    } else if (!env.evalMode && env.supportsSyncHooks()) {
      // eslint-disable-next-line @typescript-eslint/no-require-imports -- require(esm) of an untyped .mjs entry
      (require('import-in-the-middle/register-hooks.mjs') as RegisterSyncHooks).register();
      mode = 'sync';
    } else {
      nodeModule.register('import-in-the-middle/hook.mjs', pathToFileURL(__filename));
      mode = 'async';
    }
  } catch (err) {
    report('module hooks could not be installed; ESM imports are not instrumented', { error: errorText(err) });
    return undefined;
  }
  localMode = mode;
  try {
    state.hooks = mode;
  } catch {
    report('the hooks mode cannot be recorded: the shared state is read-only', { hooks: mode });
  }
  return mode;
}
