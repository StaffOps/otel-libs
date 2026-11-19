'use strict';
// db.test.cjs fixture: one pg query through the helper, in its own process
// (the semconv mode is read when sqlInstrumentation() is called). With
// FIXTURE_PRELOAD_PG=1, pg is loaded before the instrumentation exists.
const preloaded = process.env.FIXTURE_PRELOAD_PG === '1' ? require('pg') : undefined;
const { InMemorySpanExporter, SimpleSpanProcessor } = require('@opentelemetry/sdk-trace');
const helper = require('@staffops/otel-helper');
const { sqlInstrumentation } = require('@staffops/otel-helper-sql');
const scrape = require('./scrape.cjs');

// FIXTURE_PRELOAD_PG: a client built before setup() too (fix round L12).
const early = preloaded === undefined ? undefined : new preloaded.Client({ connectionString: process.env.SQL_TEST_POSTGRES });
const exporter = new InMemorySpanExporter();
helper.setup({ instrumentations: [sqlInstrumentation({ pg: true })], spanProcessors: [new SimpleSpanProcessor({ exporter })] });
const pg = preloaded ?? require('pg');

(async () => {
  for (const client of [early, new pg.Client({ connectionString: process.env.SQL_TEST_POSTGRES })]) {
    if (client === undefined) continue;
    await client.connect();
    await client.query('SELECT $1::int AS n', [1]);
    await client.end();
  }
  const metrics = await scrape(helper);
  const spans = exporter.getFinishedSpans().map((s) => ({ name: s.name, attributes: s.attributes }));
  await helper.shutdown();
  console.log(JSON.stringify({ preloaded: preloaded !== undefined, spans, metrics }));
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
