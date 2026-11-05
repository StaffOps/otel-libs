'use strict';
// CommonJS app; SPIKE_STYLE=named destructures node:http at require time.
const { trace } = require('@opentelemetry/api');
const { run } = require('./http-scenario.cjs');
const { summarize } = require('./setup-stub.cjs');

(async () => {
  if (process.env.SPIKE_STYLE === 'named') {
    const { createServer, get } = require('node:http');
    await run(createServer, get, trace.getTracer('spike-app'));
  } else {
    const http = require('node:http');
    await run(http.createServer, http.get, trace.getTracer('spike-app'));
  }
  console.log(JSON.stringify({ spans: summarize(globalThis.__spike.memory) }));
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
