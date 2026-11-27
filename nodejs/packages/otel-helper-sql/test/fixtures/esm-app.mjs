// db.test.cjs fixture: the ES module app, loaded after esm-preload.mjs.
import pg from 'pg';
import { shutdown } from '@staffops/otel-helper';
import { getState } from '@staffops/otel-helper/testing';

const client = new pg.Client({ connectionString: process.env.SQL_TEST_POSTGRES });
await client.connect();
await client.query('SELECT $1::int AS n', [1]);
await client.end();
const spans = globalThis.__fixtureExporter.getFinishedSpans().map((s) => ({ name: s.name, attributes: s.attributes }));
const hooks = getState()?.hooks ?? null;
await shutdown();
console.log(JSON.stringify({ hooks, spans }));
