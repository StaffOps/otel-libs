import * as fs from 'node:fs';
import * as nodeModule from 'node:module';
import * as path from 'node:path';
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

/**
 * How the hooks were installed in this process (`undefined` = not installed,
 * or this copy is inactive). Never throws.
 */
export function hooksMode(): HooksMode | undefined {
  const state = getState();
  return state === null ? undefined : recordedMode(state);
}

/** Process facts {@link entryIsEsm} decides on; injectable for tests. */
export interface EntryFacts {
  /** The main script, as Node resolved it (`process.argv[1]`); `undefined` under `--eval`/`--print`. */
  readonly script: string | undefined;
  readonly execArgv: readonly string[];
}

const ESM_EXTENSIONS: ReadonlySet<string> = new Set(['.mjs', '.mts']);
const CJS_EXTENSIONS: ReadonlySet<string> = new Set(['.cjs', '.cts']);

/** The `type` of the package.json nearest to `file`; `undefined` when none is found or readable. */
function packageType(file: string): unknown {
  let dir = path.dirname(file);
  for (;;) {
    const candidate = path.join(dir, 'package.json');
    if (fs.existsSync(candidate)) {
      try {
        return (JSON.parse(fs.readFileSync(candidate, 'utf8')) as { type?: unknown }).type;
      } catch {
        return undefined;
      }
    }
    const parent = path.dirname(dir);
    if (parent === dir) return undefined;
    dir = parent;
  }
}

/**
 * Whether the process's entry point is an ES module (spec JS5): an
 * `--input-type=module` eval, an `.mjs`/`.mts` script, or a `.js`/`.ts`/
 * extensionless script in a `"type": "module"` package (or, in a package
 * with no `type`, under `--experimental-default-type=module`). A `.js` file Node runs as ESM only by
 * syntax detection is not recognized (design D-JS14). Never throws.
 */
export function entryIsEsm(facts: EntryFacts = { script: process.argv[1], execArgv: process.execArgv }): boolean {
  const args = facts.execArgv;
  const flag = (name: string, value: string): boolean =>
    args.some((a, i) => a === `${name}=${value}` || (a === name && args[i + 1] === value));
  if (facts.script === undefined || facts.script === '') return flag('--input-type', 'module');
  const ext = path.extname(facts.script);
  if (ESM_EXTENSIONS.has(ext)) return true;
  if (CJS_EXTENSIONS.has(ext)) return false;
  // An explicit package "type" wins over --experimental-default-type (Node's order).
  const type = packageType(facts.script);
  if (type === 'module' || type === 'commonjs') return type === 'module';
  return flag('--experimental-default-type', 'module');
}
