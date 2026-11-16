// Helper set-up of the demo apps, with a workaround for a helper defect this
// demo found (DE3c, 2026-11-16; measured in Docker with the workspace helper):
// when an opt-in factory (redisInstrumentation(), sqlInstrumentation()) exists
// while setup() runs with an OTLP signal on gRPC, the core http, express and
// gRPC instrumentations record nothing — the gRPC exporter loads @grpc/grpc-js
// inside setup() while the factory's require hook is already installed, and
// the core instrumentations then never patch grpc-js (and, in some runs, not
// http either). Without a factory, or with OTLP over http/protobuf, they work.
//
// Workaround, public API only: a first setup() without opt-in packages (the
// core instrumentations patch grpc-js and http), shutdown(), then the real
// setup() with the opt-in factory created only now; the re-enabled core
// instrumentations re-patch the modules they already know. Remove it when the
// helper is fixed.
'use strict';

const { randomUUID } = require('node:crypto');

const { hasInstanceId } = require('./config');
const { errorTypeOf } = require('./failure');

/**
 * setup() → shutdown() → setup({ instrumentations: makeInstrumentations() }).
 * Both setups get the same `service.instance.id` (one random UUID per process)
 * unless OTEL_RESOURCE_ATTRIBUTES already carries one, so the process exports
 * as one instance. A failing first shutdown is reported on stderr (error name
 * only) and the second setup still runs. Resolves with the second setup()'s result.
 */
async function bootstrap(setup, shutdown, makeInstrumentations, deps = {}) {
  const env = deps.env ?? process.env;
  const uuid = deps.uuid ?? randomUUID;
  const warn = deps.warn ?? ((line) => process.stderr.write(`${line}\n`));
  const base = hasInstanceId(env) ? {} : { resourceAttributes: { 'service.instance.id': uuid() } };
  setup(base);
  try {
    await shutdown(1_000);
  } catch (err) {
    warn(`otel-libs-demo: first telemetry shutdown failed (${errorTypeOf(err)}); continuing`);
  }
  return setup({ ...base, instrumentations: makeInstrumentations() });
}

module.exports = { bootstrap };
