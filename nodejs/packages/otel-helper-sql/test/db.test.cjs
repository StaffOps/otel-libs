'use strict';
// @staffops/otel-helper-sql against real PostgreSQL and MySQL (task B10a,
// spec JS8): spans and measurements carry db.system.name and the legacy
// db.system (contract R10, design D-JS9), bound values and credentials are
// never recorded, pool metrics, a library loaded before setup(), and the ESM
// custom preload (design D-JS11, moved here from B9).
//
// Databases: SQL_TEST_POSTGRES / SQL_TEST_MYSQL connection URLs (throwaway
// credentials; testing/docker-compose.yml services sql-postgres, sql-mysql).
// Unset: these tests are skipped, loudly; with SQL_TEST_REQUIRE=1 (compose)
// a missing URL fails the suite instead.
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { InMemorySpanExporter, SimpleSpanProcessor } = require('@opentelemetry/sdk-trace');
const helper = require('@staffops/otel-helper');
const testing = require('@staffops/otel-helper/testing');
const { sqlInstrumentation } = require('@staffops/otel-helper-sql');

const PG_URL = process.env.SQL_TEST_POSTGRES;
const MYSQL_URL = process.env.SQL_TEST_MYSQL;
/** Expected `db.namespace` / `server.address` from a connection URL (dev L8). */
const target = (url) => (url === undefined ? {} : { db: new URL(url).pathname.slice(1), host: new URL(url).hostname });
const PG = target(PG_URL);
const MYSQL = target(MYSQL_URL);
const REQUIRE = process.env.SQL_TEST_REQUIRE === '1';
const PKG_DIR = path.resolve(__dirname, '..');
const FIXTURES = path.join(__dirname, 'fixtures');

test('database URLs are configured (SQL_TEST_REQUIRE=1 makes them mandatory)', (t) => {
  if (!REQUIRE && (PG_URL === undefined || MYSQL_URL === undefined)) {
    t.skip('SQL_TEST_POSTGRES / SQL_TEST_MYSQL unset: database tests skipped (run node-tests in testing/docker-compose.yml)');
    return;
  }
  assert.ok(PG_URL, 'SQL_TEST_POSTGRES');
  assert.ok(MYSQL_URL, 'SQL_TEST_MYSQL');
});

// Pinned by the obs M1 test: instrumentation-pg 0.77 with two connectionString
// pools (max 3 and 2) after one query each — one series per pool name, the max
// summed, the connection counts shared across pools (one counter state).
const TWO_POOLS = [
  'db_client_connection_max{db_client_connection_pool_name="unknown_host:unknown_port/unknown_database"} 5',
  'db_client_connection_count{db_client_connection_pool_name="unknown_host:unknown_port/unknown_database",db_client_connection_state="used"} 1',
  'db_client_connection_count{db_client_connection_pool_name="unknown_host:unknown_port/unknown_database",db_client_connection_state="idle"} 0',
  'db_client_connection_pending_requests{db_client_connection_pool_name="unknown_host:unknown_port/unknown_database"} 0',
];

const pgOpts = { skip: PG_URL === undefined && !REQUIRE ? 'SQL_TEST_POSTGRES unset' : false };
const mysqlOpts = { skip: MYSQL_URL === undefined && !REQUIRE ? 'SQL_TEST_MYSQL unset' : false };

/** The password of a connection URL: asserted absent from every attribute value. */
function password(url) {
  return decodeURIComponent(new URL(url).password);
}

// One instance for the whole file: created before pg and mysql2 are loaded,
// as an app does (the contrib instrumentations are shared per process anyway).
const instrumentation = sqlInstrumentation();
const pg = require('pg');
const mysql = require('mysql2');
const mysqlPromise = require('mysql2/promise');

let exporter;
let saved;
test.beforeEach(() => {
  saved = { ...process.env };
  for (const k of Object.keys(process.env)) if (k.startsWith('OTEL_')) delete process.env[k];
  process.env.OTEL_SERVICE_NAME = 'svc';
  process.env.OTEL_RESOURCE_ATTRIBUTES = 'deployment.environment.name=DEV';
  process.env.OTEL_HELPER_METRICS_PORT = '0';
  testing.reset();
  testing.setDiagnosticSink(() => undefined);
  exporter = new InMemorySpanExporter();
  helper.setup({ instrumentations: [instrumentation], spanProcessors: [new SimpleSpanProcessor({ exporter })] });
});
test.afterEach(async () => {
  await helper.shutdown().catch(() => undefined);
  testing.reset();
  for (const k of Object.keys(process.env)) if (!(k in saved)) delete process.env[k];
  Object.assign(process.env, saved);
});

/** Calls the mounted /metrics handler (the Prometheus fallback: no OTLP endpoint). */
function scrape() {
  const handler = helper.metricsHandler();
  return new Promise((resolve) => {
    const chunks = [];
    const res = {
      statusCode: 200,
      headersSent: false,
      setHeader() {},
      getHeader() {},
      removeHeader() {},
      writeHead(status) {
        this.statusCode = status;
        return this;
      },
      write(chunk) {
        chunks.push(String(chunk));
        return true;
      },
      end(chunk) {
        if (chunk !== undefined && typeof chunk !== 'function') chunks.push(String(chunk));
        resolve(chunks.join(''));
        return this;
      },
    };
    handler({ method: 'GET', url: '/metrics', headers: {} }, res);
  });
}

/** Every attribute value, event attribute and status message of every finished span, as text. */
function allValues(spans) {
  return spans.flatMap((s) => [s.name, s.status.message ?? '', ...Object.values(s.attributes).map(String), ...s.events.flatMap((e) => Object.values(e.attributes ?? {}).map(String))]);
}

function assertNoSecrets(spans, url, values) {
  const text = allValues(spans);
  const secret = password(url);
  assert.ok(secret.length > 0);
  for (const v of text) {
    assert.ok(!v.includes(secret), `password in ${v}`);
    for (const bound of values) assert.ok(!v.includes(bound), `bound value ${bound} in ${v}`);
  }
}

test('pg: query spans carry db.system.name and db.system, the statement as written, no bound value or credential', pgOpts, async () => {
  const pool = new pg.Pool({ connectionString: PG_URL, max: 2 });
  try {
    await pool.query('SELECT $1::text AS v', ['bound-secret-1']);
    await pool.query({ text: 'SELECT $1::int + 1 AS n', values: [41] });
    const client = new pg.Client({ connectionString: PG_URL });
    await client.connect();
    await client.query('SELECT 2 AS two');
    await client.end();
  } finally {
    await pool.end();
  }
  const spans = exporter.getFinishedSpans();
  const queries = spans.filter((s) => s.name.startsWith('pg.query'));
  assert.deepEqual(
    queries.map((s) => s.attributes['db.query.text']),
    ['SELECT $1::text AS v', 'SELECT $1::int + 1 AS n', 'SELECT 2 AS two'],
  );
  for (const s of spans) {
    assert.equal(s.attributes['db.system.name'], 'postgresql', s.name);
    assert.equal(s.attributes['db.system'], 'postgresql', s.name);
    // A30 (Q5, 2026-11-06): the legacy db.statement = db.query.text in the legacy-keys mode (pg sets it after start).
    assert.equal(s.attributes['db.statement'], s.attributes['db.query.text'], s.name);
    assert.equal(s.attributes['db.postgresql.values'], undefined, 'enhancedDatabaseReporting stays off');
  }
  assert.equal(queries[0].kind, 2 /* CLIENT */);
  assert.equal(queries[0].attributes['db.namespace'], PG.db);
  assert.equal(queries[0].attributes['server.address'], PG.host);
  assert.equal(queries[0].attributes['db.operation.name'], undefined, 'span attributes as upstream (operation on the metric only)');
  assert.ok(spans.some((s) => s.name === 'pg-pool.connect'), JSON.stringify(spans.map((s) => s.name)));
  assertNoSecrets(spans, PG_URL, ['bound-secret-1']);
});

test('pg: db.client.operation.duration and the pool metrics; the duration carries db.system too', pgOpts, async () => {
  const pool = new pg.Pool({ connectionString: PG_URL, max: 3 });
  try {
    await pool.query('SELECT 1');
    const text = await scrape();
    const duration = text.split('\n').find((l) => l.startsWith('db_client_operation_duration_seconds_count{'));
    assert.ok(duration, text);
    for (const label of ['db_system_name="postgresql"', 'db_system="postgresql"', 'db_operation_name="SELECT"', `db_namespace="${PG.db}"`]) {
      assert.ok(duration.includes(label), `${label} in ${duration}`);
    }
    // Pool metrics (current semconv `db.client.connection.*`; Go: db.sql.connection.*, design D-JS14).
    for (const name of ['db_client_connection_count', 'db_client_connection_max', 'db_client_connection_pending_requests']) {
      assert.match(text, new RegExp(`^${name}[_a-z]*\\{`, 'm'), name);
    }
    // Pool name from host:port/database in the Pool options; a connectionString
    // gives `unknown_host:unknown_port/unknown_database` (upstream getPoolName, D-JS14).
    assert.match(text, /^db_client_connection_max\{[^}]*db_client_connection_pool_name="unknown_host:unknown_port\/unknown_database"[^}]*\} 3$/m);
    assert.match(text, /^db_client_connection_count\{[^}]*db_client_connection_state="used"/m);
  } finally {
    await pool.end();
  }
});

test('pg: a failed query is an ERROR span with no bound value; error.type (SQLSTATE) on the metric of the callback form', pgOpts, async () => {
  const pool = new pg.Pool({ connectionString: PG_URL });
  const client = new pg.Client({ connectionString: PG_URL });
  await client.connect();
  try {
    // pool.query uses the callback form internally; client.query without a callback the promise form.
    await assert.rejects(pool.query('SELECT * FROM no_such_table WHERE v = $1', ['bound-secret-2']));
    await assert.rejects(client.query('SELECT * FROM no_such_table WHERE w = $1', ['bound-secret-8']));
  } finally {
    await client.end();
    await pool.end();
  }
  const failed = exporter.getFinishedSpans().filter((s) => s.name.startsWith('pg.query'));
  assert.equal(failed.length, 2);
  for (const span of failed) assert.equal(span.status.code, 2 /* ERROR */);
  assertNoSecrets(exporter.getFinishedSpans(), PG_URL, ['bound-secret-2', 'bound-secret-8']);
  const counts = (await scrape()).split('\n').filter((l) => l.startsWith('db_client_operation_duration_seconds_count{'));
  assert.equal(counts.filter((l) => l.includes('error_type="42P01"')).length, 1, counts.join('\n'));
  // instrumentation-pg 0.77: the promise form records the failure without error.type (D-JS14).
  assert.equal(counts.filter((l) => !l.includes('error_type=')).length, 1, counts.join('\n'));
});

test('mysql2: callback, promise and prepared statements; the statement as written, never formatted with its values', mysqlOpts, async () => {
  const conn = mysql.createConnection(MYSQL_URL);
  await new Promise((resolve, reject) => conn.query('SELECT ? AS v', ['bound-secret-3'], (err) => (err ? reject(err) : resolve())));
  await new Promise((resolve) => conn.end(resolve));
  const promiseConn = await mysqlPromise.createConnection(MYSQL_URL);
  try {
    await promiseConn.query('SELECT ? AS v, ? AS w', ['bound-secret-4', 9]);
    await promiseConn.execute('SELECT ? AS m', ['bound-secret-5']);
    await promiseConn.query({ sql: 'SELECT ? AS o', values: ['bound-secret-6'] });
    await assert.rejects(promiseConn.query('SELECT * FROM no_such_table WHERE v = ?', ['bound-secret-7']));
  } finally {
    await promiseConn.end();
  }
  const spans = exporter.getFinishedSpans();
  assert.deepEqual(spans.map((s) => s.attributes['db.query.text']), [
    'SELECT ? AS v',
    'SELECT ? AS v, ? AS w',
    'SELECT ? AS m',
    'SELECT ? AS o',
    'SELECT * FROM no_such_table WHERE v = ?',
  ]);
  for (const s of spans) {
    assert.equal(s.attributes['db.system.name'], 'mysql');
    assert.equal(s.attributes['db.system'], 'mysql');
    assert.equal(s.attributes['db.statement'], s.attributes['db.query.text'], 'A30: legacy db.statement (Q5)');
    assert.equal(s.attributes['db.namespace'], MYSQL.db);
    assert.equal(s.attributes['server.address'], MYSQL.host);
  }
  assert.equal(spans[4].status.code, 2 /* ERROR */);
  assertNoSecrets(spans, MYSQL_URL, ['bound-secret-3', 'bound-secret-4', 'bound-secret-5', 'bound-secret-6', 'bound-secret-7']);
  // instrumentation-mysql2 0.70 records no metrics (design D-JS14).
  assert.doesNotMatch(await scrape(), /db_system_name="mysql"/);
});

test('R1 (A25): ERROR spans keep the code and drop the database error text (pg invalid input, mysql2 duplicate key and parse error)', { skip: pgOpts.skip || mysqlOpts.skip }, async () => {
  const client = new pg.Client({ connectionString: PG_URL });
  await client.connect();
  try {
    // PostgreSQL: invalid input syntax for type integer: "bound-secret-x"
    await assert.rejects(client.query('SELECT $1::int', ['bound-secret-x']), /bound-secret-x/);
  } finally {
    await client.end();
  }
  const conn = await mysqlPromise.createConnection(MYSQL_URL);
  try {
    await conn.query('CREATE TABLE IF NOT EXISTS r1_uniq (v VARCHAR(64), UNIQUE KEY uv (v))');
    await conn.query('DELETE FROM r1_uniq');
    await conn.execute('INSERT INTO r1_uniq (v) VALUES (?)', ['bound-secret-y']);
    // MySQL: Duplicate entry 'bound-secret-y' for key 'r1_uniq.uv'
    await assert.rejects(conn.execute('INSERT INTO r1_uniq (v) VALUES (?)', ['bound-secret-y']), /bound-secret-y/);
    // mysql2 query() escapes the values client-side: the server's parse error quotes them.
    await assert.rejects(conn.query('SELECT * FROM r1_uniq WHERE v == ?', ['bound-secret-z']), /bound-secret-z/);
  } finally {
    await conn.end();
  }
  const failed = exporter.getFinishedSpans().filter((s) => s.status.code === 2 /* ERROR */);
  assert.deepEqual(failed.map((s) => s.attributes['db.system.name']), ['postgresql', 'mysql', 'mysql']);
  for (const span of failed) {
    assert.deepEqual(span.status, { code: 2 }, span.name);
    assert.equal(span.attributes['error.type'], '_OTHER', 'N1');
  }
  assertNoSecrets(exporter.getFinishedSpans(), PG_URL, ['bound-secret-x', 'bound-secret-y', 'bound-secret-z']);
  // pg's exception event is upstream-sanitized (type and SQLSTATE only); mysql2 records none.
  const pgFailed = failed[0];
  assert.match(pgFailed.events[0].attributes['exception.message'], /^PostgreSQL error of type '\w+' occurred \(code: 22P02\)$/);
  assert.equal(failed[1].events.length, 0);
});

test('M1: the pool max survives a re-setup (shutdown then setup on the same forwarding providers)', pgOpts, async () => {
  const pool = new pg.Pool({ connectionString: PG_URL, max: 3 });
  try {
    await pool.query('SELECT 1');
    const maxLine = (text) => text.split('\n').find((l) => l.startsWith('db_client_connection_max{'));
    assert.match(maxLine(await scrape()) ?? '', / 3$/);
    await helper.shutdown(); // also shuts the exporter down: a new one for the next setup
    const next = new InMemorySpanExporter();
    helper.setup({ instrumentations: [instrumentation], spanProcessors: [new SimpleSpanProcessor({ exporter: next })] });
    assert.match(maxLine(await scrape()) ?? '', / 3$/, 'not reset to 0 by the re-bind');
    await pool.query('SELECT 1');
    assert.ok(next.getFinishedSpans().some((s) => s.name.startsWith('pg.query')), 'spans after the re-setup');
  } finally {
    await pool.end();
  }
});

test('obs M1: two pools from connection strings share one pool name; the upstream numbers are pinned', pgOpts, async () => {
  const a = new pg.Pool({ connectionString: PG_URL, max: 3 });
  const b = new pg.Pool({ connectionString: PG_URL, max: 2 });
  try {
    await Promise.all([a.query('SELECT 1'), b.query('SELECT 1')]);
    const text = await scrape();
    const series = text.split('\n').filter((l) => /^db_client_connection_(max|count|pending_requests)\{/.test(l)).map((l) => l.replace(/,otel_scope_[^}]*/, ''));
    assert.deepEqual(series, TWO_POOLS, text);
  } finally {
    await a.end();
    await b.end();
  }
});

test('shutdown() disables it: queries after shutdown produce no span', pgOpts, async () => {
  assert.equal(pg.Client.prototype.query.__wrapped, true, 'patched while set up');
  await helper.shutdown();
  assert.equal(pg.Client.prototype.query.__wrapped, undefined, 'unpatched by shutdown (M4)');
  const client = new pg.Client({ connectionString: PG_URL });
  await client.connect();
  await client.query('SELECT 1');
  await client.end();
  assert.equal(exporter.getFinishedSpans().length, 0);
});

/** Runs a fixture with the package's environment; returns the JSON it prints last. */
function runFixture(args, env) {
  const res = spawnSync(process.execPath, args, {
    cwd: PKG_DIR,
    encoding: 'utf8',
    timeout: 60_000,
    env: { ...process.env, OTEL_SERVICE_NAME: 'svc', OTEL_RESOURCE_ATTRIBUTES: 'deployment.environment.name=DEV', OTEL_HELPER_METRICS_PORT: '0', ...env },
  });
  const last = res.stdout.trim().split('\n').pop();
  try {
    return { ...JSON.parse(last), stderr: res.stderr, status: res.status };
  } catch {
    throw new Error(`fixture status=${res.status}\nstdout:\n${res.stdout}\nstderr:\n${res.stderr}`);
  }
}

test('OTEL_SEMCONV_STABILITY_OPT_IN=database: only db.system.name, on spans and on the duration metric', pgOpts, () => {
  const out = runFixture([path.join(FIXTURES, 'pg-query.cjs')], { OTEL_SEMCONV_STABILITY_OPT_IN: 'http,database' });
  assert.equal(out.status, 0, out.stderr);
  assert.ok(out.spans.length > 0);
  for (const s of out.spans) {
    assert.equal(s.attributes['db.system.name'], 'postgresql');
    assert.equal(s.attributes['db.system'], undefined, s.name);
    assert.equal(s.attributes['db.statement'], undefined, `${s.name}: no legacy db.statement in the stable mode (A30)`);
  }
  assert.match(out.metrics, /db_system_name="postgresql"/);
  assert.doesNotMatch(out.metrics, /db_system="/);
});

test('pg loaded before sqlInstrumentation() and setup() is still instrumented, also a client built before (L12)', pgOpts, () => {
  const out = runFixture([path.join(FIXTURES, 'pg-query.cjs')], { FIXTURE_PRELOAD_PG: '1' });
  assert.equal(out.status, 0, out.stderr);
  assert.ok(out.preloaded, 'pg was in require.cache first');
  const queries = out.spans.filter((s) => s.name.startsWith('pg.query') && s.attributes['db.system'] === 'postgresql');
  assert.equal(queries.length, 2, JSON.stringify(out.spans));
});

test('mysql2 loaded before sqlInstrumentation() (lib/connection.js re-required) is still instrumented (L12)', mysqlOpts, () => {
  const out = runFixture([path.join(FIXTURES, 'mysql2-query.cjs')], {});
  assert.equal(out.status, 0, out.stderr);
  assert.ok(out.preloaded);
  assert.deepEqual(out.spans.map((s) => [s.name, s.attributes['db.query.text']]), [['SELECT', 'SELECT ? AS n']]);
});

test('D-JS11: an ESM app with a custom preload (hooks + setup({ instrumentations })) gets pg spans', pgOpts, () => {
  const out = runFixture(['--import', path.join(FIXTURES, 'esm-preload.mjs'), path.join(FIXTURES, 'esm-app.mjs')], {});
  assert.equal(out.status, 0, out.stderr);
  assert.ok(out.hooks === 'sync' || out.hooks === 'async', `hooks mode ${out.hooks}`);
  const query = out.spans.find((s) => s.name.startsWith('pg.query'));
  assert.ok(query, JSON.stringify(out.spans));
  assert.equal(query.attributes['db.query.text'], 'SELECT $1::int AS n');
  assert.equal(query.attributes['db.system'], 'postgresql');
  assert.doesNotMatch(out.stderr, /without the module hooks/);
});

test('D-JS11 control: without the hooks the JS5 warning is printed, yet pg (CommonJS) is still instrumented (dev L4)', pgOpts, () => {
  const out = runFixture(['--import', path.join(FIXTURES, 'esm-preload.mjs'), path.join(FIXTURES, 'esm-app.mjs')], { FIXTURE_NO_HOOKS: '1' });
  assert.equal(out.status, 0, out.stderr);
  assert.equal(out.hooks, null);
  assert.match(out.stderr, /without the module hooks/);
  // An ES module import of a CommonJS package goes through require-in-the-middle.
  assert.ok(out.spans.some((s) => s.name.startsWith('pg.query')), JSON.stringify(out.spans));
});
