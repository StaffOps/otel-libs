'use strict';
// Resolver building blocks (task B3): trim set, booleans, OTEL_RESOURCE_ATTRIBUTES,
// log levels and code-option validation. Spec JS4, contract R4/R5/R8, D1 rulings.
const test = require('node:test');
const assert = require('node:assert/strict');
const { TRIM_CHARS, trimWS, envValue, envBool } = require('../dist/internal/env.js');
const { parseResourceAttributes } = require('../dist/internal/resource-attributes.js');
const { defaultLogLevel, knownEnvironment, LOG_LEVELS } = require('../dist/internal/log-level.js');
const { validateOptions } = require('../dist/internal/resolve.js');
const helper = require('@staffops/otel-helper');
const { reset, setDiagnosticSink } = require('@staffops/otel-helper/testing');

function capture() {
  const lines = [];
  setDiagnosticSink((line, meta) => lines.push({ line, ...meta }));
  return lines;
}

function withEnv(vars, fn) {
  const saved = {};
  for (const k of Object.keys(vars)) saved[k] = process.env[k];
  try {
    for (const [k, v] of Object.entries(vars)) {
      if (v === undefined) delete process.env[k];
      else process.env[k] = v;
    }
    return fn();
  } finally {
    for (const [k, v] of Object.entries(saved)) {
      if (v === undefined) delete process.env[k];
      else process.env[k] = v;
    }
  }
}

test.afterEach(() => reset());

test('JS4 trim set: exactly " \\t\\n\\r\\v\\f" (design D1), not Unicode whitespace', () => {
  assert.equal(TRIM_CHARS, ' \t\n\r\v\f');
  assert.equal(trimWS(' \t\n\r\v\fsvc\f\v\r\n\t '), 'svc');
  for (const ch of [' ', ' ', '\u001c', '\u001f', '﻿']) assert.equal(trimWS(`${ch}x${ch}`), `${ch}x${ch}`, JSON.stringify(ch));
  assert.equal(trimWS('a b'), 'a b');
  assert.equal(trimWS(''), '');
});

test('envValue: unset, empty and whitespace-only read as unset; values are trimmed', () => {
  withEnv({ B3_X: undefined }, () => assert.equal(envValue('B3_X'), undefined));
  withEnv({ B3_X: '' }, () => assert.equal(envValue('B3_X'), undefined));
  withEnv({ B3_X: ' \t ' }, () => assert.equal(envValue('B3_X'), undefined));
  withEnv({ B3_X: ' v ' }, () => assert.equal(envValue('B3_X'), 'v'));
});

test('R8 envBool: true/false case-insensitive; anything else warns and reads false', () => {
  const lines = capture();
  withEnv({ B3_B: ' TRUE ' }, () => assert.equal(envBool('B3_B'), true));
  withEnv({ B3_B: 'False' }, () => assert.equal(envBool('B3_B'), false));
  withEnv({ B3_B: '' }, () => assert.equal(envBool('B3_B'), undefined));
  assert.equal(lines.length, 0);
  withEnv({ B3_B: 'yes' }, () => assert.equal(envBool('B3_B'), false));
  assert.equal(lines.length, 1);
  assert.match(lines[0].line, /^otelhelper: unparseable boolean variable=B3_B value=yes using=false$/);
  assert.deepEqual([lines[0].origin, lines[0].level], ['helper', 'warn']);
});

test('JS4 OTEL_RESOURCE_ATTRIBUTES: valid items kept, malformed skipped by position, values decoded and trimmed', () => {
  const p = parseResourceAttributes(' a = 1 ,broken,=x,, b=%20two%2C ,c=x=y,a=last,d=%zz, ');
  assert.deepEqual({ ...p.attributes }, { a: 'last', b: 'two,', c: 'x=y', d: '%zz' });
  assert.deepEqual(p.malformed, [2, 3]);
  assert.deepEqual(p.undecodable, [8]);
  assert.equal(Object.getPrototypeOf(p.attributes), null);
  const proto = parseResourceAttributes('__proto__=v,constructor=w');
  assert.deepEqual(Object.keys(proto.attributes).sort(), ['__proto__', 'constructor']);
  assert.equal(proto.attributes.__proto__, 'v');
  assert.deepEqual(parseResourceAttributes('k=').attributes.k, '');
});

test('R4 defaultLogLevel: LOCAL debug, DEV/HML info, PRD warn, unset/unknown info, debug wins', () => {
  assert.deepEqual(LOG_LEVELS, ['debug', 'info', 'warn', 'error']);
  assert.equal(helper.defaultLogLevel, defaultLogLevel);
  const table = [
    ['LOCAL', 'debug'], ['local', 'debug'], [' Local ', 'debug'], ['DEV', 'info'], ['hml', 'info'],
    ['PRD', 'warn'], ['prd', 'warn'], [undefined, 'info'], ['', 'info'], ['staging', 'info'], ['PRODUCTION', 'info'],
  ];
  for (const [env, want] of table) assert.equal(defaultLogLevel(env), want, String(env));
  assert.equal(defaultLogLevel('PRD', true), 'debug');
  assert.equal(defaultLogLevel(undefined, true), 'debug');
  assert.equal(defaultLogLevel(), 'info');
  assert.equal(knownEnvironment(' dev '), 'DEV');
  assert.equal(knownEnvironment('qa'), undefined);
  assert.equal(knownEnvironment(undefined), undefined);
});

test('R8 invalid code options fail fast naming the option; undefined means not set', () => {
  const bad = [
    [null, /options must be an object, got null/],
    [[], /options must be an object, got array/],
    ['x', /options must be an object, got string/],
    [new (class Opts {})(), /options must be an object/],
    [{ zeta: 1, yankee: 'x' }, /options has unknown key\(s\) yankee, zeta/],
    [{ serviceName: 1 }, /serviceName must be a string, got number/],
    [{ serviceName: ' \t' }, /serviceName must not be empty/],
    [{ environment: '' }, /environment must not be empty/],
    [{ environment: false }, /environment must be a string, got boolean/],
    [{ resourceAttributes: 'a=b' }, /resourceAttributes must be an object of strings, got string/],
    [{ resourceAttributes: { ' ': 'v' } }, /resourceAttributes has an empty key/],
    [{ resourceAttributes: { k: 1 } }, /resourceAttributes value of "k" must be a string, got number/],
    [{ debug: 'true' }, /debug must be a boolean, got string/],
    [{ installDiagLogger: 0 }, /installDiagLogger must be a boolean, got number/],
    [{ minimumLogLevel: 'WARN' }, /minimumLogLevel must be one of debug, info, warn, error, got "WARN"/],
    [{ minimumLogLevel: 5 }, /minimumLogLevel must be one of debug, info, warn, error, got number/],
    [{ minimumLogLevel: 10n }, /minimumLogLevel must be one of debug, info, warn, error, got bigint/],
    [{ spanProcessors: {} }, /spanProcessors must be an array, got object/],
    [{ spanProcessors: [{ onStart() {} }] }, /spanProcessors must contain SpanProcessor objects/],
    [{ spanProcessors: [null] }, /spanProcessors must contain SpanProcessor objects/],
  ];
  for (const [options, rx] of bad) {
    assert.throws(() => validateOptions(options), (err) => err instanceof TypeError && rx.test(err.message), String(rx));
    assert.throws(() => helper.setup(options), TypeError, 'setup() rejects it before building anything');
  }
  assert.deepEqual(validateOptions(undefined), {});
  // undefined = not set: dropped, and never reported as an unknown key.
  assert.deepEqual(validateOptions({ serviceName: undefined, debug: undefined, zeta: undefined }), {});
  const ok = { serviceName: 's', environment: 'e', resourceAttributes: { k: 'v' }, debug: false, minimumLogLevel: 'error', installDiagLogger: false, spanProcessors: [] };
  const snap = validateOptions(ok);
  assert.notEqual(snap, ok, 'a snapshot, not the caller object');
  assert.deepEqual({ ...snap, resourceAttributes: { ...snap.resourceAttributes } }, ok);
  assert.ok(Object.isFrozen(snap) && Object.isFrozen(snap.resourceAttributes) && Object.isFrozen(snap.spanProcessors));
  assert.equal(validateOptions(Object.assign(Object.create(null), { debug: true })).debug, true);
  // Each property is read once: a getter cannot validate one value and resolve another.
  let reads = 0;
  const tricky = { get serviceName() { reads += 1; return reads === 1 ? 'valid' : ''; } };
  assert.equal(validateOptions(tricky).serviceName, 'valid');
  assert.equal(reads, 1);
});
