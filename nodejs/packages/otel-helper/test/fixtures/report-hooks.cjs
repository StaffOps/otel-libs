'use strict';
// Prints how /hooks installed the loader hooks next to what import-in-the-middle
// supports on this Node. A file, not `node -e`: with iitm sync hooks installed
// from a `--require` preload, `--eval`/`--print` crash (spike-B1c.md finding F1).
console.log(JSON.stringify({
  hooks: globalThis[Symbol.for('staffops.otel-helper.state.v1')]?.hooks,
  expected: require('import-in-the-middle/supports-sync-hooks.mjs').supportsSyncHooks() ? 'sync' : 'async',
}));
