'use strict';
// @staffops/otel-helper-sql failed-query status codes (contract design D16,
// task A44) through the real pg and mysql2 drivers and the real contrib
// instrumentations, against in-process servers in this test process: a small
// PostgreSQL wire stub that answers ErrorResponse with a `C` (SQLSTATE) field,
// and mysql2's own `createServer()` answering `writeError({ code })`. No
// database is needed, so these run in every CI run; db.test.cjs repeats the
// main cases against real PostgreSQL and MySQL.
const test = require('node:test');
const assert = require('node:assert/strict');
const net = require('node:net');
const path = require('node:path');
const { InMemorySpanExporter, SimpleSpanProcessor } = require('@opentelemetry/sdk-trace');
const helper = require('@staffops/otel-helper');
const testing = require('@staffops/otel-helper/testing');
const { sqlInstrumentation } = require('@staffops/otel-helper-sql');

const instrumentation = sqlInstrumentation();
const pg = require('pg');
const mysql = require('mysql2');
// mysql2's packet class (test only): the stub's COM_STMT_PREPARE OK reply.
const Packet = require(path.join(path.dirname(require.resolve('mysql2')), 'lib', 'packets', 'packet.js'));

const SECRET = 'server text bound-secret';

// ── PostgreSQL wire stub ────────────────────────────────────────────────
// Startup: a database named `err_<SQLSTATE>` is refused with that code.
// Simple and extended queries containing `ERR:<SQLSTATE>` fail with it.

function pgMessage(type, body) {
  const b = Buffer.alloc(5 + body.length);
  b.write(type, 0);
  b.writeInt32BE(4 + body.length, 1);
  body.copy(b, 5);
  return b;
}
const cstr = (s) => Buffer.from(`${s}\0`);
const pgError = (code) => pgMessage('E', Buffer.concat([cstr('SERROR'), cstr('VERROR'), cstr(`C${code}`), cstr(`M${SECRET}`), Buffer.from([0])]));
const ready = () => pgMessage('Z', Buffer.from('I'));
const errCode = (text) => /ERR:(\S+)/.exec(text)?.[1];

function pgServer() {
  return net.createServer((sock) => {
    let buf = Buffer.alloc(0);
    let started = false;
    let pending;
    sock.on('error', () => undefined);
    sock.on('data', (data) => {
      buf = Buffer.concat([buf, data]);
      for (;;) {
        if (!started) {
          if (buf.length < 4) return;
          const len = buf.readInt32BE(0);
          if (buf.length < len) return;
          const parts = buf.subarray(8, len).toString().split('\0');
          buf = buf.subarray(len);
          started = true;
          const params = {};
          for (let i = 0; i + 1 < parts.length; i += 2) params[parts[i]] = parts[i + 1];
          if (params.database?.startsWith('err_')) {
            sock.end(pgError(params.database.slice(4)));
            return;
          }
          sock.write(Buffer.concat([pgMessage('R', Buffer.from([0, 0, 0, 0])), ready()]));
          continue;
        }
        if (buf.length < 5) return;
        const type = String.fromCharCode(buf[0]);
        const len = buf.readInt32BE(1);
        if (buf.length < len + 1) return;
        const body = buf.subarray(5, len + 1);
        buf = buf.subarray(len + 1);
        if (type === 'Q') {
          const code = errCode(body.toString());
          sock.write(code ? Buffer.concat([pgError(code), ready()]) : Buffer.concat([pgMessage('C', cstr('SELECT 0')), ready()]));
        } else if (type === 'P') {
          pending = errCode(body.toString());
        } else if (type === 'S') {
          const empty = Buffer.alloc(0);
          sock.write(pending ? Buffer.concat([pgError(pending), ready()]) : Buffer.concat([pgMessage('1', empty), pgMessage('2', empty), pgMessage('n', empty), pgMessage('C', cstr('SELECT 0')), ready()]));
          pending = undefined;
        } else if (type === 'X') {
          sock.end();
          return;
        }
      }
    });
  });
}

// ── MySQL server (mysql2 createServer) ──────────────────────────────────
// Queries and executed statements containing `ERR:<number>` fail with it.

function mysqlServer() {
  const server = mysql.createServer();
  server.on('connection', (conn) => {
    conn.on('error', () => undefined);
    conn.serverHandshake({ protocolVersion: 10, serverVersion: '8.0.0-stub', connectionId: 1, statusFlags: 2, characterSet: 8, capabilityFlags: 0xffffff });
    const answer = (sql) => {
      const m = /ERR:(\d+)/.exec(sql ?? '');
      if (m) conn.writeError({ code: Number(m[1]), message: SECRET });
      else conn.writeOk();
    };
    conn.on('query', answer);
    let statement = '';
    conn.on('stmt_prepare', (sql) => {
      statement = sql;
      // COM_STMT_PREPARE OK: status 0, statement id 1, no columns, no params.
      const body = Buffer.alloc(12);
      body.writeUInt32LE(1, 1);
      const packet = Buffer.alloc(4 + body.length);
      body.copy(packet, 4);
      conn.writePacket(new Packet(0, packet, 0, packet.length));
    });
    conn.on('stmt_execute', () => answer(statement));
  });
  return server;
}

const listen = (server) => new Promise((resolve) => server.listen(0, '127.0.0.1', () => resolve((server._server ?? server).address().port)));
const closedPort = () =>
  new Promise((resolve) => {
    const s = net.createServer().listen(0, '127.0.0.1', () => {
      const { port } = s.address();
      s.close(() => resolve(port));
    });
  });

let exporter;
let pgPort;
let myPort;
let servers;
test.before(async () => {
  for (const k of Object.keys(process.env)) if (k.startsWith('OTEL_')) delete process.env[k];
  process.env.OTEL_SERVICE_NAME = 'svc';
  process.env.OTEL_RESOURCE_ATTRIBUTES = 'deployment.environment.name=DEV';
  process.env.OTEL_HELPER_METRICS_PORT = '0';
  testing.setDiagnosticSink(() => undefined);
  exporter = new InMemorySpanExporter();
  helper.setup({ instrumentations: [instrumentation], spanProcessors: [new SimpleSpanProcessor({ exporter })] });
  servers = [pgServer(), mysqlServer()];
  [pgPort, myPort] = await Promise.all(servers.map(listen));
});
test.after(async () => {
  for (const s of servers) s.close();
  await helper.shutdown();
});
test.beforeEach(() => exporter.reset());

/** The failed spans as [name prefix, db.response.status_code, error.type]; every one ERROR without a description or server text. */
function failed() {
  const spans = exporter.getFinishedSpans().filter((s) => s.status.code === 2 /* ERROR */);
  for (const s of spans) {
    assert.deepEqual(s.status, { code: 2 }, `${s.name}: ERROR without a description`);
    assert.ok(!JSON.stringify([s.attributes, s.events.map((e) => e.attributes)]).includes('bound-secret'), `${s.name}: no server text`);
  }
  return spans.map((s) => [s.name.split(' ')[0], s.attributes['db.response.status_code'], s.attributes['error.type']]);
}

const settle = (p) => p.then(() => 'ok', (e) => e.code ?? e.errno);

test('D16 (A44) pg on a wire stub: callback 23505, promise 42P01, pool.query 23505, connect 3D000 and 28P01, ECONNREFUSED _OTHER', async () => {
  const cfg = (database = 'otel', port = pgPort) => ({ host: '127.0.0.1', port, user: 'u', password: 'p', database });
  const client = new pg.Client(cfg());
  await client.connect();
  assert.equal(await new Promise((resolve) => client.query('INSERT ERR:23505', (e) => resolve(e?.code))), '23505');
  assert.equal(await settle(client.query('SELECT $1 ERR:42P01', ['bound-secret'])), '42P01');
  await client.end();
  const pool = new pg.Pool(cfg());
  assert.equal(await settle(pool.query('UPDATE ERR:23505')), '23505');
  await pool.end();
  assert.equal(await settle(new pg.Client(cfg('err_3D000')).connect()), '3D000');
  assert.equal(await new Promise((resolve) => new pg.Client(cfg('err_28P01')).connect((e) => resolve(e?.code))), '28P01');
  assert.equal(await settle(new pg.Client(cfg('otel', await closedPort())).connect()), 'ECONNREFUSED');
  const rows = failed();
  const has = (row) => rows.some((r) => JSON.stringify(r) === JSON.stringify(row));
  for (const row of [
    ['pg.query:INSERT', '23505', '23505'],
    ['pg.query:SELECT', '42P01', '42P01'],
    ['pg.query:UPDATE', '23505', '23505'],
    ['pg.connect', '3D000', '3D000'],
    ['pg.connect', '28P01', '28P01'],
    ['pg.connect', undefined, '_OTHER'],
  ]) {
    assert.ok(has(row), `${JSON.stringify(row)} in ${JSON.stringify(rows)}`);
  }
  for (const r of rows) assert.ok(r[2] === r[1] || (r[1] === undefined && r[2] === '_OTHER'), JSON.stringify(r));
});

test('D16 (A44) mysql2 on its own createServer(): callback, promise, execute and pool queries carry the error number; streaming and 2013 are _OTHER', async () => {
  const cfg = { host: '127.0.0.1', port: myPort, user: 'u', password: 'p', database: 'd' };
  const conn = mysql.createConnection(cfg);
  conn.on('error', () => undefined);
  assert.equal(await new Promise((resolve) => conn.query('INSERT ERR:1062', (e) => resolve(e?.errno))), 1062);
  assert.equal(await new Promise((resolve) => conn.query('SELECT ? ERR:1146', ['bound-secret'], (e) => resolve(e?.errno))), 1146);
  assert.equal(await settle(conn.promise().query('SELECT ERR:1146')), 'ER_NO_SUCH_TABLE');
  assert.equal(await settle(conn.promise().execute('SELECT ERR:1064')), 'ER_PARSE_ERROR');
  assert.equal(await new Promise((resolve) => conn.query('DELETE ERR:1146').on('error', (e) => resolve(e.errno))), 1146);
  assert.equal(await new Promise((resolve) => conn.query('SELECT ERR:2013', (e) => resolve(e?.errno))), 2013);
  conn.destroy();
  const pool = mysql.createPool(cfg);
  assert.equal(await new Promise((resolve) => pool.query('UPDATE ERR:1062', (e) => resolve(e?.errno))), 1062);
  assert.equal(await settle(pool.promise().query('REPLACE ERR:1213')), 'ER_LOCK_DEADLOCK');
  await pool.promise().end();
  assert.deepEqual(failed(), [
    ['INSERT', '1062', '1062'],
    ['SELECT', '1146', '1146'],
    ['SELECT', '1146', '1146'],
    ['SELECT', '1064', '1064'],
    ['DELETE', undefined, '_OTHER'], // the streaming form: ended from the error event (documented)
    ['SELECT', undefined, '_OTHER'], // 2013: the client range
    ['UPDATE', '1062', '1062'],
    ['REPLACE', '1213', '1213'],
  ]);
});
