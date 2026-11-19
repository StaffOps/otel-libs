// Locates the shared contract files: demo/proto next to the app in the image
// (/app/proto), else in a source checkout (nodejs/demo/lib -> ../../../demo);
// the schema from DEMO_SCHEMA_FILE (default: the path baked into the image).
'use strict';

const fs = require('node:fs');
const path = require('node:path');

/** The first existing directory among the image and checkout locations of `demo/<name>`. */
function assetDir(name, baseDir = __dirname, exists = fs.existsSync) {
  const candidates = [path.join(baseDir, '..', name), path.join(baseDir, '..', '..', '..', 'demo', name)];
  const found = candidates.find((dir) => exists(dir));
  if (found === undefined) throw new Error(`demo/${name} not found (looked in ${candidates.join(', ')})`);
  return found;
}

/** The OrderService definition, loaded at run time with @grpc/proto-loader (DE-D3). */
function loadOrderService(grpc, protoLoader, protoDir = assetDir('proto')) {
  const definition = protoLoader.loadSync('otellibs/demo/v1/demo.proto', {
    includeDirs: [protoDir],
    keepCase: true,
    longs: String,
    enums: String,
    defaults: true,
  });
  return grpc.loadPackageDefinition(definition).demo.v1.OrderService;
}

/** Where the image bakes demo/sql/schema.sql (DE-D7 r3.4). */
const DEFAULT_SCHEMA_FILE = '/etc/otel-libs-demo/schema.sql';

/** The schema file: DEMO_SCHEMA_FILE, else the image path (DE-D7 r3.4). */
function schemaFile(env = process.env) {
  const value = (env.DEMO_SCHEMA_FILE ?? '').trim();
  return value === '' ? DEFAULT_SCHEMA_FILE : value;
}

/** The schema as one string (the backend sends it as one command, DE-D4). */
function readSchema(file = schemaFile()) {
  return fs.readFileSync(file, 'utf8');
}

module.exports = { DEFAULT_SCHEMA_FILE, assetDir, loadOrderService, readSchema, schemaFile };
