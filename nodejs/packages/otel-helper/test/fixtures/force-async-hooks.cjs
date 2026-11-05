'use strict';
// Forces the asynchronous module.register path (supported on every Node >= 22.15)
// and checks the loader works by importing an ESM module afterwards.
const { installHooks, currentEnvironment } = require('../../dist/internal/hooks.js');
const env = { ...currentEnvironment(), supportsSyncHooks: () => false, evalMode: false };
const mode = installHooks(env);
const again = installHooks(env);
import('node:path').then((p) => {
  console.log(JSON.stringify({ mode, again, imported: typeof p.join === 'function' }));
});
