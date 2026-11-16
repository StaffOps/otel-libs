// Entry point of demo-node-worker (specs/demo-environment DE3c): sets the
// helper up, then loads the app (app.js) so every library it instruments is
// required after setup(). See lib/bootstrap.js for why setup() runs twice.
'use strict';

const { setup, shutdown } = require('@staffops/otel-helper');
const { bootstrap } = require('../lib/bootstrap');

bootstrap(setup, shutdown, () => [require('@staffops/otel-helper-redis').redisInstrumentation({ redis: true, ioredis: false })])
  .then(() => require('./app'))
  .catch((err) => {
    console.error('demo-node-worker: telemetry setup failed:', err instanceof Error ? err.name : typeof err);
    process.exit(1);
  });
