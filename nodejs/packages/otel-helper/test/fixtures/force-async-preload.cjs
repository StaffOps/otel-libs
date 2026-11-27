'use strict';
// `--require` preload forcing the async loader: Node runs it in the main thread
// and again in its internal module-hooks thread, where it must do nothing.
const { installHooks, currentEnvironment } = require('../../dist/internal/hooks.js');
const mode = installHooks({ ...currentEnvironment(), supportsSyncHooks: () => false, evalMode: false });
if (require('node:worker_threads').isMainThread) globalThis.__preloadMode = mode;
