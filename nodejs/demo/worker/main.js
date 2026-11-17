// Entry point of demo-node-worker (specs/demo-environment DE3c): one setup(),
// then the app (app.js), so every library it instruments is required after
// setup(). Load order (nodejs/HOW-TO.md §9): the helper first, the opt-in
// factory created inside the setup() call, nothing loaded in between.
'use strict';

const { setup } = require('@staffops/otel-helper');
const { redisInstrumentation } = require('@staffops/otel-helper-redis');

setup({ instrumentations: [redisInstrumentation({ redis: true, ioredis: false })] });

require('./app');
