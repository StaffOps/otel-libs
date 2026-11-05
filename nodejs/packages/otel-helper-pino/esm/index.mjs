// ESM entry: re-exports the CommonJS implementation (design D-JS10). Keep the
// names in sync with src/index.ts; test/esm.test.mjs fails on drift.
import cjs from '../dist/index.js';

export const { createPinoDestination, VERSION } = cjs;
// pino.transport({ target }) loads the default export: it throws a clear "not supported" (B10d fix round).
export default cjs.default;
