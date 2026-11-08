'use strict';
// The command matrix redis.test.cjs runs against the RESP fake and
// server.test.cjs against a real Redis (task B10b): every command form of
// node-redis 6 and ioredis 6 — promise and callback, MULTI and pipeline,
// pub/sub, explicit AUTH, and the failures WRONGTYPE, an unknown command,
// NOAUTH, WRONGPASS and connection refused. Values are `secret-*` strings and
// the password; `assertScenario` checks none reaches a span.
const assert = require('node:assert/strict');

/** Values the commands send: never in a span attribute, name or status. */
const SECRETS = ['secret-value', 'secret-key', 'secret-multi', 'secret-pipe', 'secret-msg', 'secret-io', 'secret-io-cb', 'secret-io-pipe', 'secret-io-multi', 'secret-io-msg', 'wrong-secret'];
/** Quoted back by the Lua error_reply (server text: exception event only, the A25 residual). */
const EVAL_ARG = 'secret-eval-arg';
const SCRIPT = `return redis.error_reply('MYCODE ${EVAL_ARG}')`;
/** A Lua reply whose first word is data (R1, 2026-11-03): never an attribute, error.type _OTHER. */
const ARGV_SCRIPT = "return redis.error_reply(ARGV[1] .. ' bad')";
const LUA_WORD = 'SECRET_TOKEN42';
/** The argument the unknown command sends: the server quotes it in its error (exception event residual, contract D1 A25). */
const QUOTED = 'secret-quoted-arg';

const quiet = (client) => (client.on('error', () => undefined), client);

/**
 * Runs the matrix against `url` (`redis://default:<password>@host:port`).
 * `refusedUrl`: an address nobody listens on.
 */
async function runScenario({ url, refusedUrl }) {
  const { createClient } = require('redis');
  const IORedis = require('ioredis');
  const { host } = new URL(url);
  const password = decodeURIComponent(new URL(url).password);

  // node-redis: promise API, MULTI, pipeline, pub/sub, explicit AUTH, errors.
  const c = quiet(createClient({ url }));
  await c.connect();
  await c.set('secret-key', 'secret-value');
  assert.equal(await c.get('secret-key'), 'secret-value');
  await assert.rejects(c.lPush('secret-key', 'secret-value'), /WRONGTYPE/);
  await assert.rejects(c.sendCommand(['NOSUCHCMD', QUOTED]), /unknown command/);
  await assert.rejects(c.sendCommand(['EVAL', SCRIPT, '0']), /MYCODE/);
  await assert.rejects(c.sendCommand(['EVAL', ARGV_SCRIPT, '0', LUA_WORD]), new RegExp(LUA_WORD));
  await c.multi().set('secret-key', 'secret-multi').get('secret-key').exec();
  await c.multi().set('secret-key', 'secret-pipe').get('secret-key').execAsPipeline();
  await c.auth({ username: 'default', password });
  const sub = quiet(c.duplicate());
  await sub.connect();
  const received = new Promise((resolve) => sub.subscribe('chan', resolve));
  await c.publish('chan', 'secret-msg');
  assert.equal(await received, 'secret-msg');
  await sub.unsubscribe('chan');
  await sub.quit();
  await c.quit();

  // ioredis: promise and callback API, pipeline, MULTI, pub/sub, AUTH, errors.
  const r = quiet(new IORedis(url, { lazyConnect: true }));
  await r.connect();
  await r.set('io-key', 'secret-io');
  assert.equal(await r.get('io-key'), 'secret-io');
  await new Promise((resolve, reject) => r.set('io-key', 'secret-io-cb', (err) => (err ? reject(err) : resolve())));
  await assert.rejects(r.lpush('io-key', 'secret-io'), /WRONGTYPE/);
  await assert.rejects(r.eval(SCRIPT, 0), /MYCODE/);
  await assert.rejects(r.eval(ARGV_SCRIPT, 0, LUA_WORD), new RegExp(LUA_WORD));
  await r.pipeline().set('io-key', 'secret-io-pipe').get('io-key').exec();
  await r.multi().set('io-key', 'secret-io-multi').exec();
  await r.auth(password);
  const rs = quiet(new IORedis(url, { lazyConnect: true }));
  await rs.connect();
  const got = new Promise((resolve) => rs.on('message', (_ch, m) => resolve(m)));
  await rs.subscribe('io-chan');
  await r.publish('io-chan', 'secret-io-msg');
  assert.equal(await got, 'secret-io-msg');
  rs.disconnect();
  await r.quit();

  // Failures: no password (NOAUTH), a wrong password (WRONGPASS), nobody listening.
  const noAuth = quiet(createClient({ url: `redis://${host}`, socket: { reconnectStrategy: false } }));
  await assert.rejects(noAuth.connect(), /NOAUTH/);
  const wrong = quiet(createClient({ url: `redis://default:wrong-secret@${host}`, socket: { reconnectStrategy: false } }));
  await assert.rejects(wrong.connect(), /WRONGPASS/);
  const refused = quiet(createClient({ url: refusedUrl, socket: { reconnectStrategy: false } }));
  await assert.rejects(refused.connect(), /ECONNREFUSED/);
  const ioNoAuth = quiet(new IORedis(`redis://${host}`, { lazyConnect: true, maxRetriesPerRequest: 0, retryStrategy: () => null }));
  await assert.rejects(ioNoAuth.connect().then(() => ioNoAuth.get('io-key')));
  ioNoAuth.disconnect();
  const ioRefused = quiet(new IORedis(refusedUrl, { lazyConnect: true, maxRetriesPerRequest: 0, retryStrategy: () => null }));
  await assert.rejects(ioRefused.connect());
  ioRefused.disconnect();
}

/** Every attribute value, event attribute, name and status message of `spans`, as text (exception events apart). */
function spanText(spans) {
  return spans.flatMap((s) => [s.name, s.status.message ?? '', ...Object.values(s.attributes).map(String)]);
}

/** The assertions both suites make on the spans of {@link runScenario}. */
function assertScenario(spans, { url, refusedUrl }) {
  const { hostname, port } = new URL(url);
  const password = decodeURIComponent(new URL(url).password);
  const REDIS = '@opentelemetry/instrumentation-redis';
  const IOREDIS = '@opentelemetry/instrumentation-ioredis';
  const of = (scope) => (name, text) => spans.filter((s) => s.instrumentationScope.name === scope && s.name === name && (text === undefined || s.attributes['db.query.text'] === text));
  const pick = of(REDIS);
  const io = of(IOREDIS);
  // Span names: the command uppercased (ruling 2026-11-03, Q3), connection spans `connect`.
  for (const s of spans) assert.match(s.name, /^(connect|[A-Z]+)$/, s.name);

  for (const s of spans) {
    assert.equal(s.kind, 2 /* CLIENT */, s.name);
    assert.equal(s.attributes['db.system.name'], 'redis', s.name);
    assert.equal(s.attributes['db.system'], 'redis', `${s.name}: legacy key by default (database/dup)`);
    // A30: the legacy db.statement is db.query.text in dup mode (as Python contrib redis).
    assert.equal(s.attributes['db.statement'], s.attributes['db.query.text'], `${s.name}: legacy db.statement by default`);
    assert.equal(typeof s.attributes['server.address'], 'string', s.name);
  }
  // No value, key, password or connection string anywhere but in exception events.
  for (const text of spanText(spans)) {
    for (const secret of [...SECRETS, QUOTED, EVAL_ARG, LUA_WORD, password, url]) assert.ok(!text.includes(secret), `${secret} in ${text}`);
  }
  for (const s of spans) for (const e of s.events) for (const secret of [...SECRETS, password]) assert.ok(!JSON.stringify(e.attributes).includes(secret), `${secret} in an exception event of ${s.name}`);
  // Every ERROR span: no description; error.type the reply's code, also as
  // db.response.status_code, or _OTHER without one (A26 fix round, 2026-11-03).
  const failed = spans.filter((s) => s.status.code === 2);
  assert.ok(failed.length >= 10, `failed spans: ${failed.map((s) => s.name)}`);
  for (const s of failed) {
    assert.deepEqual(s.status, { code: 2 }, s.name);
    const type = s.attributes['error.type'];
    assert.match(String(type), /^([A-Z][A-Z0-9_]*|_OTHER)$/, s.name);
    assert.equal(s.attributes['db.response.status_code'], type === '_OTHER' ? undefined : type, s.name);
  }
  const typeOf = (list) => list.map((s) => s.attributes['error.type']).sort();

  // node-redis: db.operation.name the command, the statement sanitized.
  const set = pick('SET', 'SET ? ?')[0];
  assert.ok(set, 'node-redis SET');
  assert.equal(set.attributes['db.operation.name'], 'SET');
  assert.equal(set.attributes['server.address'], hostname);
  assert.equal(set.attributes['server.port'], Number(port));
  assert.equal(pick('GET', 'GET ?')[0].attributes['db.operation.name'], 'GET');
  assert.deepEqual(typeOf(pick('LPUSH', 'LPUSH ? ?')), ['WRONGTYPE']);
  assert.deepEqual(typeOf(pick('EVAL', 'EVAL ? ?')), ['_OTHER'], 'a Lua error_reply word is not a reply code (R1)');
  assert.deepEqual(typeOf(pick('EVAL', 'EVAL ? ? ?')), ['_OTHER'], 'ARGV[1] as the first word: _OTHER, never the word');
  const unknown = pick('NOSUCHCMD', 'NOSUCHCMD ?')[0];
  assert.equal(unknown.attributes['error.type'], 'ERR');
  // Pinned residual (contract D1 "DB error text in exception events"): the server quotes the argument.
  assert.ok(unknown.events[0].attributes['exception.message'].includes(QUOTED), 'the exception event keeps the server text (A25 residual)');
  assert.deepEqual(pick('SET', 'SET ? ?').map((s) => s.attributes['db.operation.name']).sort(), ['MULTI', 'PIPELINE', 'SET']);
  assert.deepEqual(pick('AUTH').map((s) => s.attributes['db.query.text']), ['AUTH ? ?'], 'AUTH: never the password');
  assert.equal(pick('PUBLISH', 'PUBLISH ? ?').length, 1);
  const connects = pick('connect');
  assert.deepEqual(connects.map((s) => s.status.code).sort(), [0, 0, 2, 2, 2], 'two clients connected; NOAUTH, WRONGPASS and refused failed');
  assert.deepEqual(typeOf(connects.filter((s) => s.status.code === 2)), ['NOAUTH', 'WRONGPASS', '_OTHER']);
  assert.ok(connects.some((s) => s.attributes['server.address'] === new URL(refusedUrl).hostname && s.status.code === 2), 'refused');

  // ioredis: db.operation.name the lower-case command, MULTI/PIPELINE prefixed.
  const ioSets = io('SET', 'SET ? ?');
  assert.deepEqual(ioSets.map((s) => s.attributes['db.operation.name']).sort(), ['MULTI set', 'PIPELINE set', 'set', 'set'], 'promise, callback, pipeline, MULTI');
  assert.equal(io('GET', 'GET ?')[0].attributes['db.operation.name'], 'get');
  assert.deepEqual(typeOf(io('LPUSH', 'LPUSH ? ?')), ['WRONGTYPE']);
  assert.deepEqual(typeOf(io('EVAL', 'EVAL ? ?')), ['_OTHER']);
  assert.deepEqual(typeOf(io('EVAL', 'EVAL ? ? ?')), ['_OTHER']);
  assert.ok(io('AUTH', 'AUTH ?').length >= 1, 'AUTH: never the password');
  assert.equal(io('SUBSCRIBE', 'SUBSCRIBE ?').length, 1);
  assert.equal(io('PUBLISH', 'PUBLISH ? ?').length, 1);
  assert.ok(io('HELLO').some((s) => s.status.code === 2), 'NOAUTH on the HELLO of a client without a password');
  assert.ok(io('connect', 'connect').length >= 3, 'connect spans (ioredis records them without a parent too)');
}

module.exports = { runScenario, assertScenario, SECRETS, QUOTED, EVAL_ARG };
