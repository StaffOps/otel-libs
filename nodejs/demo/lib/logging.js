// The demo's logger: pino with the @staffops/otel-helper-pino destination, so
// records go through the OpenTelemetry Logs API with the active span's ids.
'use strict';

const pino = require('pino');
const { createPinoDestination } = require('@staffops/otel-helper-pino');

/** A pino logger named `name` (the records' instrumentation scope); create it after setup(). */
function createDemoLogger(name) {
  return pino({ name }, createPinoDestination());
}

module.exports = { createDemoLogger };
