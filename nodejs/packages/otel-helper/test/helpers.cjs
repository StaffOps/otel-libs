'use strict';
// Shared test helpers: run a fresh Node process from the package directory so
// `@staffops/otel-helper` resolves through the workspace link. Children inherit
// NODE_V8_COVERAGE, so c8 counts what they execute.
const { spawnSync } = require('node:child_process');
const path = require('node:path');

const PKG_DIR = path.resolve(__dirname, '..');

/** Runs `node <args>` and returns { status, stdout, stderr }. */
function runNode(args, { env } = {}) {
  const res = spawnSync(process.execPath, args, {
    cwd: PKG_DIR,
    encoding: 'utf8',
    env: { ...process.env, ...env },
    timeout: 60_000,
  });
  if (res.error) throw res.error;
  return { status: res.status, stdout: res.stdout, stderr: res.stderr };
}

/** Runs an ESM snippet (`--input-type=module`). */
function runEsm(source, opts) {
  return runNode(['--input-type=module', '-e', source], opts);
}

/** Runs a CommonJS snippet. */
function runCjs(source, opts) {
  return runNode(['-e', source], opts);
}

/** Parses the last stdout line as JSON, failing with the child's output. */
function lastJson(res) {
  const lines = res.stdout.trim().split('\n');
  try {
    return JSON.parse(lines[lines.length - 1]);
  } catch {
    throw new Error(`child status=${res.status}\nstdout:\n${res.stdout}\nstderr:\n${res.stderr}`);
  }
}

module.exports = { PKG_DIR, runNode, runEsm, runCjs, lastJson };
