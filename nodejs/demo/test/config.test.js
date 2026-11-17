'use strict';

const assert = require('node:assert/strict');
const { test } = require('node:test');

const { DEFAULT_RUN, LANG, envOr, parseHostPort, portEnv, readSecretFile, requireEnv, runId, secondsEnv } = require('../lib/config');

test('LANG is node', () => {
  assert.equal(LANG, 'node');
});

test('runId reads otel_libs.run from OTEL_RESOURCE_ATTRIBUTES', () => {
  const env = { OTEL_RESOURCE_ATTRIBUTES: 'deployment.environment.name=dev, otel_libs.run=20261116t1812' };
  assert.equal(runId(env), '20261116t1812');
});

test('runId percent-decodes and skips malformed or empty entries', () => {
  assert.equal(runId({ OTEL_RESOURCE_ATTRIBUTES: 'novalue,otel_libs.run=,otel%5Flibs.run=r%2D1' }), 'r-1');
  assert.equal(runId({ OTEL_RESOURCE_ATTRIBUTES: 'otel_libs.run=%E0%A4%A' }), '%E0%A4%A');
});

test('runId falls back to "local" (no separate variable)', () => {
  assert.equal(runId({ OTEL_RESOURCE_ATTRIBUTES: 'a=b', DEMO_RUN: 'r2' }), DEFAULT_RUN);
  assert.equal(runId({}), 'local');
});

test('runId defaults to process.env', () => {
  assert.equal(typeof runId(), 'string');
});

test('requireEnv returns the trimmed value or names the variable', () => {
  assert.equal(requireEnv({ X: ' v ' }, 'X'), 'v');
  assert.throws(() => requireEnv({}, 'X'), { message: 'X is not set' });
  assert.throws(() => requireEnv({ X: ' ' }, 'X'), { message: 'X is not set' });
});

test('secondsEnv parses positive seconds', () => {
  assert.equal(secondsEnv({}, 'S', 5), 5);
  assert.equal(secondsEnv({ S: '0.5' }, 'S', 5), 0.5);
  for (const bad of ['0', '-1', 'abc', 'Infinity']) {
    assert.throws(() => secondsEnv({ S: bad }, 'S', 5), /S must be a positive number of seconds/);
  }
});

test('portEnv parses a TCP port', () => {
  assert.equal(portEnv({}, 'P', 8080), 8080);
  assert.equal(portEnv({ P: '50051' }, 'P', 1), 50051);
  for (const bad of ['0', '65536', '1.5', 'x']) {
    assert.throws(() => portEnv({ P: bad }, 'P', 1), /P must be a TCP port/);
  }
});

test('parseHostPort splits host and port', () => {
  assert.deepEqual(parseHostPort('demo-redis:6380', 6379), { host: 'demo-redis', port: 6380 });
  assert.deepEqual(parseHostPort(' demo-postgres ', 5432), { host: 'demo-postgres', port: 5432 });
  assert.deepEqual(parseHostPort('[::1]:7000', 1), { host: '::1', port: 7000 });
  assert.deepEqual(parseHostPort('[::1]', 9), { host: '::1', port: 9 });
  assert.deepEqual(parseHostPort('h:', 9), { host: 'h', port: 9 });
});

test('parseHostPort rejects malformed addresses, naming the variable', () => {
  assert.throws(() => parseHostPort('[::1', 1, 'A'), /A has an unclosed IPv6 bracket/);
  assert.throws(() => parseHostPort('[::1]x', 1, 'A'), /A must be host\[:port\]/);
  assert.throws(() => parseHostPort(':80', 1, 'A'), /A has no host/);
  assert.throws(() => parseHostPort('h:99999', 1, 'A'), /A has an invalid port/);
  assert.throws(() => parseHostPort('h:x', 1), /address has an invalid port/);
});

test('readSecretFile strips one trailing line break', () => {
  const read = (file) => (file === '/s' ? 'pw-value\n' : 'other\r\n');
  assert.equal(readSecretFile({ F: '/s' }, 'F', read), 'pw-value');
  assert.equal(readSecretFile({ F: '/t' }, 'F', read), 'other');
});

test('readSecretFile errors name the variable, never the content', () => {
  assert.throws(() => readSecretFile({}, 'F'), { message: 'F is not set' });
  const missing = () => { throw Object.assign(new Error('ENOENT: /secret/path'), { code: 'ENOENT' }); };
  assert.throws(() => readSecretFile({ F: '/x' }, 'F', missing), { message: 'F: cannot read the file (ENOENT)' });
  const noCode = () => { throw new Error('boom'); };
  assert.throws(() => readSecretFile({ F: '/x' }, 'F', noCode), { message: 'F: cannot read the file (error)' });
  assert.throws(() => readSecretFile({ F: '/x' }, 'F', () => '\n'), { message: 'F: the file is empty' });
});

test('readSecretFile reads a real file by default', (t) => {
  const fs = require('node:fs');
  const os = require('node:os');
  const path = require('node:path');
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'demo-secret-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const file = path.join(dir, 'pw');
  fs.writeFileSync(file, 'abc\n');
  assert.equal(readSecretFile({ F: file }, 'F'), 'abc');
});

test('envOr returns the trimmed value or the default', () => {
  assert.equal(envOr({ A: ' x ' }, 'A', 'd'), 'x');
  assert.equal(envOr({ A: ' ' }, 'A', 'd'), 'd');
  assert.equal(envOr({}, 'A', 'd'), 'd');
});
