'use strict';
// db.test.cjs fixture (fix round L12): mysql2 loaded before the
// instrumentation exists, so its lib/connection.js is patched by the re-require
// on the first enable.
const mysql = require('mysql2/promise');
const { InMemorySpanExporter, SimpleSpanProcessor } = require('@opentelemetry/sdk-trace');
const helper = require('@staffops/otel-helper');
const { sqlInstrumentation } = require('@staffops/otel-helper-sql');

const preloaded = Object.keys(require.cache).some((f) => f.endsWith(require('node:path').join('mysql2', 'lib', 'connection.js')));
const exporter = new InMemorySpanExporter();
helper.setup({ instrumentations: [sqlInstrumentation({ mysql2: true })], spanProcessors: [new SimpleSpanProcessor({ exporter })] });

(async () => {
  const conn = await mysql.createConnection(process.env.SQL_TEST_MYSQL);
  await conn.query('SELECT ? AS n', [1]);
  await conn.end();
  const spans = exporter.getFinishedSpans().map((s) => ({ name: s.name, attributes: s.attributes }));
  await helper.shutdown();
  console.log(JSON.stringify({ preloaded, spans }));
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
