export * from '../dist/index.js';
// The ESM default is the CommonJS `exports.default` (esm/index.mjs), declared here: a re-export
// of a CommonJS module's default would type the whole `module.exports` (fix round 2, 2026-11-03).
/** The `pino.transport({ target })` entry: unsupported, it throws (pass createPinoDestination() to pino()). */
declare function pinoTransportTarget(): never;
export default pinoTransportTarget;
