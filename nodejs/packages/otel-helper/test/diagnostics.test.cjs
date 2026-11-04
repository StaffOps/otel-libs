'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const diag = require('../dist/internal/diagnostics.js');
const { setDiagnosticSink } = require('@staffops/otel-helper/testing');
const { runCjs } = require('./helpers.cjs');

test.afterEach(() => setDiagnosticSink(undefined));

test('formats message and key=value attributes, quoting only when needed', () => {
  assert.equal(diag.formatDiagnostic('hello'), 'otelhelper: hello');
  assert.equal(
    diag.formatDiagnostic('bad value', { var: 'OTEL_X', got: 'a b', n: 3, ok: false, empty: '', q: 'x"y', eq: 'k=v', 'a.b_c': 'x' }),
    'otelhelper: bad value var=OTEL_X got="a b" n=3 ok=false empty="" q="x\\"y" eq="k=v" a.b_c=x',
  );
});

test('control characters, DEL and backslashes are quoted and escaped (no forged records)', () => {
  assert.equal(diag.formatDiagnostic('m', { v: 'a\nb' }), 'otelhelper: m v="a\\nb"');
  assert.equal(diag.formatDiagnostic('m', { v: 'a\u0000b' }), 'otelhelper: m v="a\\u0000b"');
  assert.equal(diag.formatDiagnostic('m', { v: 'a\u007fb' }), 'otelhelper: m v="a\\u007fb"');
  assert.equal(diag.formatDiagnostic('m', { v: 'a\u0085b\u2028c\u2029d\u009f' }), 'otelhelper: m v="a\\u0085b\\u2028c\\u2029d\\u009f"');
  assert.equal(diag.formatDiagnostic('m', { v: 'a\\b' }), 'otelhelper: m v="a\\\\b"');
});

test('attribute keys outside [A-Za-z0-9_.] are a programming error', () => {
  for (const key of ['a b', 'a=b', '', 'k\n', 'é']) {
    assert.throws(() => diag.formatDiagnostic('m', { [key]: 1 }), TypeError, JSON.stringify(key));
  }
});

test('report() writes through the sink installed on the shared state', () => {
  const lines = [];
  setDiagnosticSink((l) => lines.push(l));
  diag.report('m', { a: 1 });
  assert.deepEqual(lines, ['otelhelper: m a=1']);
});

test('the sink receives {origin, level}: a helper warning by default, else what the caller passes', () => {
  const records = [];
  setDiagnosticSink((line, meta) => records.push([line, meta]));
  diag.report('own');
  diag.report('forwarded', { n: 1 }, { origin: 'diag', level: 'debug' });
  diag.report('fatal', undefined, { origin: 'helper', level: 'error' });
  assert.deepEqual(records, [
    ['otelhelper: own', { origin: 'helper', level: 'warn' }],
    ['otelhelper: forwarded n=1', { origin: 'diag', level: 'debug' }],
    ['otelhelper: fatal', { origin: 'helper', level: 'error' }],
  ]);
  assert.ok(Object.isFrozen(records[0][1]), 'the default metadata is shared and frozen');
});

test('the default sink writes one line to stderr, nothing to stdout', () => {
  const res = runCjs("require('./dist/internal/diagnostics.js').report('to stderr', { k: 'v' })");
  assert.equal(res.status, 0);
  assert.equal(res.stdout, '');
  assert.equal(res.stderr, 'otelhelper: to stderr k=v\n');
});

test('a throwing custom sink falls back to stderr and never throws', () => {
  const res = runCjs(`
    require('@staffops/otel-helper/testing').setDiagnosticSink(() => { throw new Error('boom'); });
    require('./dist/internal/diagnostics.js').report('still visible');
  `);
  assert.equal(res.status, 0);
  assert.equal(res.stderr, 'otelhelper: still visible\n');
});

test('a second copy of the same version reports through the hooked sink', () => {
  const res = runCjs(`
    const lines = [];
    require('@staffops/otel-helper/testing').setDiagnosticSink((l) => lines.push(l));
    // Simulate a second install of the same version: fresh module instances.
    for (const k of Object.keys(require.cache)) if (k.includes('/otel-helper/dist/')) delete require.cache[k];
    require('./dist/internal/diagnostics.js').report('from copy two');
    console.log(JSON.stringify(lines));
  `);
  assert.deepEqual(JSON.parse(res.stdout.trim()), ['otelhelper: from copy two']);
  assert.equal(res.stderr, '');
});

test('a sink of another version is never used', () => {
  const res = runCjs(`
    const lines = [];
    Object.defineProperty(globalThis, Symbol.for('staffops.otel-helper.state.v1'),
      { value: { version: '0.0.0-other', hooks: undefined, sink: (l) => lines.push(l) } });
    require('./dist/internal/diagnostics.js').report('to stderr');
    console.log(JSON.stringify(lines));
  `);
  assert.deepEqual(JSON.parse(res.stdout.trim()), []);
  assert.equal(res.stderr, 'otelhelper: to stderr\n');
});

test('an unreadable sink falls back to stderr', () => {
  const res = runCjs(`
    const { VERSION } = require('@staffops/otel-helper');
    const s = { version: VERSION, hooks: undefined };
    Object.defineProperty(s, 'sink', { get() { throw new Error('boom'); } });
    Object.defineProperty(globalThis, Symbol.for('staffops.otel-helper.state.v1'), { value: s });
    require('./dist/internal/diagnostics.js').report('still on stderr');
  `);
  assert.equal(res.status, 0);
  assert.equal(res.stderr, 'otelhelper: still on stderr\n');
});

test('B9 fix round: redactErrorText drops URL userinfo, query and fragment, credential pairs and bearer tokens', () => {
  assert.equal(diag.redactErrorText('connect https://user:pw@collector.example.com:4318/v1/traces?sig=1#f failed'), 'connect https://collector.example.com:4318/v1/traces failed');
  assert.equal(diag.redactErrorText('a http://h.example.com b'), 'a http://h.example.com b');
  assert.equal(diag.redactErrorText('x grpc://u@h.example.com?k=v'), 'x grpc://h.example.com');
  assert.equal(diag.redactErrorText('x https://h.example.com/p@ss'), 'x https://h.example.com');
  assert.equal(diag.redactErrorText('Authorization: Bearer abc.def'), 'Authorization: <redacted> <redacted>');
  assert.equal(diag.redactErrorText('api-key=k1, token="t 2"; password: p3'), 'api-key=<redacted>, token=<redacted>; password: <redacted>');
  assert.equal(diag.redactErrorText('basic dXNlcjpwYXNz'), 'basic <redacted>');
  assert.equal(diag.redactErrorText('y'.repeat(100)), `${'y'.repeat(64)}…`);
});

test('B9 final round: redactErrorText key names, generic credential names, schemeless userinfo, dns:///, bearer forms, basic', () => {
  const r = diag.redactErrorText;
  assert.equal(r('xtoken=keep'), 'xtoken=<redacted>'); // generic rule: a name containing "token"
  assert.equal(r('"password":"p1"'), '"password":<redacted>');
  assert.equal(r('"api_key": "k"'), '"api_key": <redacted>');
  assert.equal(r('X-Team-Id=t1 my_secret_value: s'), 'X-Team-Id=<redacted> my_secret_value: <redacted>');
  assert.equal(r('client_auth=a; session-cookie=c'), 'client_auth=<redacted>; session-cookie=<redacted>');
  assert.equal(r('dial user:pw@collector.example.com:4317 failed'), 'dial collector.example.com:4317 failed');
  assert.equal(r('dns:///user:pw@collector.example.com:4317'), 'dns:///collector.example.com:4317');
  assert.equal(r('bearer:abc'), 'bearer:<redacted>');
  assert.equal(r('Bearer=abc'), 'Bearer=<redacted>');
  assert.equal(r('bearerabc'), 'bearer <redacted>');
  assert.equal(r('basic auth failed'), 'basic auth failed');
  assert.equal(r('Basic dXNlcjpw=='), 'Basic <redacted>');
  assert.equal(r('basic short'), 'basic short');
  assert.equal(r('the error: x@y'), 'the error: x@y');
  // Large inputs stay linear (no ReDoS).
  for (const big of ['a'.repeat(200_000), '_'.repeat(200_000), 'k'.repeat(200_000) + '=', `${'x:'.repeat(100_000)}@`, `https://${'u'.repeat(200_000)}`]) {
    const started = Date.now();
    r(big);
    assert.ok(Date.now() - started < 1000, `slow on ${big.slice(0, 8)}…`);
  }
});

test('redactText applies the redactErrorText rules without the 64-character cut', () => {
  const tail = 'y'.repeat(100);
  assert.equal(diag.redactText(`https://u:p@h/x?q=1 token=abc ${tail}`), `https://h/x token=<redacted> ${tail}`);
  assert.equal(diag.redactErrorText(`https://u:p@h/x?q=1 token=abc ${tail}`), diag.truncateValue(`https://h/x token=<redacted> ${tail}`));
  assert.equal(diag.redactText('z'.repeat(5000)).length, 4096, 'scans and keeps the first 4096 characters');
});

test('redactText: util.inspect, JSON, escaped-JSON forms and more credential names (B14 re-probe M1, 2026-11-04)', () => {
  const util = require('node:util');
  const r = diag.redactText;
  const noSecret = (text) => assert.ok(!/SECRET/.test(r(text)), `${text} -> ${r(text)}`);
  // util.inspect: single-quoted keys and values.
  assert.equal(r(util.format('h', { 'x-api-key': 'SECRET1', password: 'SECRET2', other: 'keep' })), "h { 'x-api-key': <redacted>, password: <redacted>, other: 'keep' }");
  // JSON inside JSON: `\"api-key\":\"…\"`, and one level deeper.
  assert.equal(r(JSON.stringify({ data: JSON.stringify({ 'api-key': 'SECRETH1', 'x-tenant-token': 'SECRETH2', ok: 'keep' }) })), '{"data":"{\\"api-key\\":<redacted>,\\"x-tenant-token\\":<redacted>,\\"ok\\":\\"keep\\"}"}');
  noSecret(JSON.stringify(JSON.stringify({ data: JSON.stringify({ 'api-key': 'SECRETH1' }) })));
  // credential / session / signature names; `&`-joined query pairs.
  assert.equal(r('credential=SECRETC sessionid=SECRETS'), 'credential=<redacted> sessionid=<redacted>');
  assert.equal(r('Signature=SECRETSIG&X-Amz-Credential=SECRETCRED&X-Amz-Date=20260101'), 'Signature=<redacted>&X-Amz-Credential=<redacted>&X-Amz-Date=20260101');
  // Cookie headers lose every pair, whatever the names.
  assert.equal(r('Cookie: a=SECRETA; other=SECRETB'), 'Cookie: <redacted>');
  assert.equal(r('{"cookie":"a=SECRETA; b=SECRETB","x":"keep"}'), '{"cookie":<redacted>,"x":"keep"}');
  assert.equal(r('set-cookie: sid=SECRETSID; Path=/'), 'set-cookie: <redacted>');
  assert.equal(r('session-cookie=c'), 'session-cookie=<redacted>');
  // Digest parameters.
  assert.equal(r('Proxy-Authorization: Digest username="u", response="SECRETD"'), 'Proxy-Authorization: <redacted> <redacted>');
  noSecret('WWW: Digest username="u", realm="r", response="SECRETD", nonce=SECRETN');
  // Prose stays.
  assert.equal(r('basic auth failed'), 'basic auth failed');
  assert.equal(r('the session ended; the key was rotated'), 'the session ended; the key was rotated');
  // Bounded on hostile inputs (escaped quotes, backslash runs).
  for (const big of [`key="${'\\'.repeat(5000)}`, `${'k="\\'.repeat(2000)}`, `cookie=\\${'"'.repeat(5000)}`, '\\'.repeat(5000), `Digest ${'a=b,'.repeat(2000)}`]) {
    const started = Date.now();
    r(big);
    assert.ok(Date.now() - started < 1000, `slow on ${big.slice(0, 8)}…`);
  }
});

test('maskLiterals replaces each secret (and its JSON-escaped forms), skipping values shorter than 4 characters', () => {
  assert.equal(diag.MIN_MASKED_SECRET, 4);
  assert.equal(diag.maskLiterals('a SECRETV b "SECRETV"', ['SECRETV']), 'a <redacted> b "<redacted>"');
  assert.equal(diag.maskLiterals(JSON.stringify(JSON.stringify({ v: 'q"uo\\te' })), ['q"uo\\te']), JSON.stringify(JSON.stringify({ v: '<redacted>' })));
  assert.equal(diag.maskLiterals('on 1 abc', ['on', '1', 'abc', '']), 'on 1 abc');
  assert.equal(diag.maskLiterals('x', []), 'x');
});

test('redactText third round: Cookie with quoted pairs, status words, pwd/jwt, bounded backslash runs (B14 round 3)', () => {
  const r = diag.redactText;
  assert.equal(r('Cookie: a="b"; sid=abc123'), 'Cookie: <redacted>');
  assert.equal(r('16 UNAUTHENTICATED: denied'), '16 UNAUTHENTICATED: denied');
  assert.equal(r('7 PERMISSION_DENIED: unauthorized: no'), '7 PERMISSION_DENIED: unauthorized: no');
  assert.equal(r('authentication failed: bad'), 'authentication failed: bad');
  assert.equal(r('Authorization: Bearer x'), 'Authorization: <redacted> <redacted>');
  assert.equal(r('authorization=secret'), 'authorization=<redacted>');
  assert.equal(r('x-authentication: t1'), 'x-authentication: <redacted>');
  assert.equal(r('pwd=p1 jwt: j1 db_pwd="p 2"'), 'pwd=<redacted> jwt: <redacted> db_pwd=<redacted>');
  assert.equal(r('the jwt was invalid; check pwd and retry'), 'the jwt was invalid; check pwd and retry');
  const bs = '\\'.repeat(4096);
  const b2000 = '\\'.repeat(2000);
  for (const big of [`${b2000}"key${b2000}"`, `${b2000}"cookie${b2000}"`, `key:"${bs}`, `cookie:"${bs}`, `${bs}"key`, `key:${bs}`, `${'key=\\"\\\\\\\\\\\\\\'.repeat(400)}`, 'unauthorized:'.repeat(400)]) {
    const text = big.slice(0, 4096);
    const started = process.hrtime.bigint();
    r(text);
    const ms = Number(process.hrtime.bigint() - started) / 1e6;
    assert.ok(ms < 25, `${ms} ms on ${JSON.stringify(text.slice(0, 8))}…`);
  }
});

test('maskLiterals: the util.inspect-escaped form too', () => {
  const util = require('node:util');
  // inspect single-quotes (escaping ' as \') only when the string holds ', " and ` together.
  const secret = 'q\'u"o`te\\x';
  assert.match(util.inspect(secret), /^'.*\\'/);
  assert.equal(diag.maskLiterals(util.inspect({ h: secret }), [secret]), "{ h: '<redacted>' }");
  const plain = "it's\\pw";
  assert.equal(diag.maskLiterals(util.inspect({ h: plain }), [plain]), '{ h: "<redacted>" }');
});

test('redactText fourth round: status words keep only their own text, unbounded escapes (B14 round 4)', () => {
  const r = diag.redactText;
  // A status word before `:` keeps its value, redacted pair by pair.
  assert.equal(r('unauthorized: token=abc'), 'unauthorized: token=<redacted>');
  assert.equal(r('16 UNAUTHENTICATED:api_key=SECRET'), '16 UNAUTHENTICATED:api_key=<redacted>');
  assert.equal(r('unauthenticated: "token: SECRET z"'), 'unauthenticated: "token: <redacted> z"');
  // `=`, a quoted name or a bare `authentication…` name is a credential pair.
  for (const leak of ['unauthorized="password=hunter2 x"', 'Authentication: SECRET3', 'authentication=SECRET4', '"authenticated":"SECRET2"', 'unauthorized=SECRET1', 'authentication_failed: SECRET5']) {
    const out = r(leak);
    assert.ok(!/SECRET|hunter2/.test(out) && out.includes('<redacted>'), `${leak} -> ${out}`);
  }
  for (const readable of ['16 UNAUTHENTICATED: denied', 'unauthorized: no', 'authentication failed: bad', '7 PERMISSION_DENIED: unauthorized: no']) assert.equal(r(readable), readable);
  // JSON escaped 4 and 5 times, values with spaces.
  for (const times of [4, 5]) {
    let text = JSON.stringify({ password: 'a b SECRETX c', ok: 'keep' });
    for (let i = 1; i < times; i++) text = JSON.stringify(text);
    const out = r(text);
    assert.ok(!out.includes('SECRETX') && out.includes('keep'), `${times}x: ${out}`);
  }
  // Backslash runs of 8 and 9 inside a quoted value; an escaped newline before a name.
  for (const n of [8, 9]) assert.equal(r(`key:"a${'\\'.repeat(n)}b SECRET9"`), 'key:<redacted>');
  assert.equal(r('"a\\napi_key=SECRETN"'), '"a\\napi_key=<redacted>');
  // A quoted value cut open by the end of the text is redacted to the end.
  assert.equal(r('x token="a b SECRETU c'), 'x token=<redacted>');
  // Round 5: a name after a status word or a credential name is a name, not their value.
  assert.equal(r('401 Unauthorized: x-api-key: SECRET'), '401 Unauthorized: x-api-key: <redacted>');
  assert.equal(r('Unauthorized: authorization: SECRET'), 'Unauthorized: authorization: <redacted>');
  assert.equal(r('UNAUTHENTICATED: session: SECRET'), 'UNAUTHENTICATED: session: <redacted>');
  assert.equal(r('unauthorized: secret: SECRET'), 'unauthorized: secret: <redacted>');
  assert.equal(r('Authorization: x-api-key: SECRET'), 'Authorization: x-api-key: <redacted>');
  assert.equal(r('16 UNAUTHENTICATED:api_key=x'), '16 UNAUTHENTICATED:api_key=<redacted>');
  assert.equal(r('unauthorized: token=x'), 'unauthorized: token=<redacted>');
  assert.equal(r('password=key=SECRETv'), 'password=<redacted>');
  // A value that is a plain word followed by `: ` is still a value.
  assert.equal(r('token: abc: def'), 'token: <redacted> def');
  assert.equal(r('Cookie: x-api-key: SECRET'), 'Cookie: <redacted>');
});
