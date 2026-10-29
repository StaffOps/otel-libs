// ESM entry: re-exports the CommonJS implementation so `import` and `require`
// share one module instance and one state (design D-JS10). Keep the names in
// sync with src/index.ts; test/esm-wrapper.test.mjs fails on drift.
import cjs from '../dist/index.js';

export const { VERSION } = cjs;
