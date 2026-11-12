'use strict';
// Check 5: explicit gRPC channel credentials created after
// registerInstrumentations. SPIKE_GRPC_MODE selects the scenario:
//   explicit-tls       createSslCredentials(CA) -> TLS <SPIKE_TLS_HOST|127.0.0.2>:14317; TLS env poisoned after setup
//   explicit-insecure  createInsecureCredentials() -> plain 127.0.0.2:4317; TLS env set after setup
//   exporters-first    control: exporter (and grpc-js) loaded BEFORE registerInstrumentations
//   lazy-env           control: no explicit credentials; CA env set only after construction
//   lazy-env-bogus     control: no explicit credentials; CA env valid at construction, bogus after
const fs = require('node:fs');
const path = require('node:path');
require('@staffops/otel-helper/hooks');
const { setupStub, summarize } = require('./setup-stub.cjs');

const mode = process.env.SPIKE_GRPC_MODE;
const ca = process.env.FAKEOTLP_CA;
const host = process.env.FAKEOTLP_HOST ?? '127.0.0.2';
for (const k of Object.keys(process.env)) if (k.startsWith('OTEL_')) delete process.env[k];

// SPIKE_TLS_HOST: a DNS name for the receiver (its cert has the SAN `fakeotlp`).
// Node 26 rejects an IP literal as TLS ServerName, which grpc-js 1.14 sets.
const tlsUrl = `https://${process.env.SPIKE_TLS_HOST || host}:14317`;
let exporter;
function makeExporter() {
  const base = require('@opentelemetry/otlp-grpc-exporter-base');
  const { OTLPTraceExporter } = require('@opentelemetry/exporter-trace-otlp-grpc');
  const metadata = base.createEmptyMetadata();
  metadata.set('x-parity-spike', mode);
  const common = { metadata, timeoutMillis: 5000 };
  switch (mode) {
    case 'explicit-tls':
    case 'exporters-first':
      exporter = new OTLPTraceExporter({ ...common, url: tlsUrl, credentials: base.createSslCredentials(fs.readFileSync(ca)) });
      break;
    case 'explicit-insecure':
      exporter = new OTLPTraceExporter({ ...common, url: `http://${host}:4317`, credentials: base.createInsecureCredentials() });
      break;
    case 'lazy-env':
      exporter = new OTLPTraceExporter({ ...common, url: tlsUrl });
      break;
    case 'lazy-env-bogus':
      process.env.OTEL_EXPORTER_OTLP_TRACES_CERTIFICATE = ca;
      exporter = new OTLPTraceExporter({ ...common, url: tlsUrl });
      break;
    default:
      throw new Error(`unknown SPIKE_GRPC_MODE=${mode}`);
  }
  return [exporter];
}

const state = setupStub({ exporters: makeExporter, exportersFirst: mode === 'exporters-first' });

// After setup: change the TLS env the exporter would read lazily.
switch (mode) {
  case 'explicit-tls':
    process.env.OTEL_EXPORTER_OTLP_CERTIFICATE = '/nonexistent/ca.pem';
    process.env.OTEL_EXPORTER_OTLP_TRACES_CERTIFICATE = '/nonexistent/ca.pem';
    process.env.OTEL_EXPORTER_OTLP_INSECURE = 'true';
    process.env.OTEL_EXPORTER_OTLP_TRACES_INSECURE = 'true';
    break;
  case 'explicit-insecure':
    process.env.OTEL_EXPORTER_OTLP_TRACES_CERTIFICATE = ca;
    process.env.OTEL_EXPORTER_OTLP_TRACES_INSECURE = 'false';
    break;
  case 'lazy-env':
    process.env.OTEL_EXPORTER_OTLP_TRACES_CERTIFICATE = ca;
    break;
  case 'lazy-env-bogus':
    process.env.OTEL_EXPORTER_OTLP_TRACES_CERTIFICATE = '/nonexistent/ca.pem';
    break;
}

(async () => {
  // The app's own grpc-js: a unary call against an in-process server.
  const grpc = require('@grpc/grpc-js');
  const exporterGrpc = require.resolve('@grpc/grpc-js', {
    paths: [path.dirname(require.resolve('@opentelemetry/otlp-grpc-exporter-base'))],
  });
  const identity = (b) => b;
  const def = {
    Ping: {
      path: '/spike.Spike/Ping', requestStream: false, responseStream: false,
      requestSerialize: identity, requestDeserialize: identity, responseSerialize: identity, responseDeserialize: identity,
    },
  };
  const server = new grpc.Server();
  server.addService(def, { Ping: (_call, cb) => cb(null, Buffer.from('pong')) });
  const port = await new Promise((resolve, reject) =>
    server.bindAsync('127.0.0.1:0', grpc.ServerCredentials.createInsecure(), (err, p) => (err ? reject(err) : resolve(p))));
  const client = new grpc.Client(`127.0.0.1:${port}`, grpc.credentials.createInsecure());
  await new Promise((resolve, reject) =>
    client.makeUnaryRequest('/spike.Spike/Ping', identity, identity, Buffer.from('ping'), (err) => (err ? reject(err) : resolve())));
  client.close();
  await new Promise((resolve) => server.tryShutdown(resolve));

  const spans = state.memory.getFinishedSpans();
  const result = await new Promise((resolve) => exporter.export(spans, resolve));
  await exporter.shutdown();
  console.log(JSON.stringify({
    mode,
    grpcLoadedBeforeSetup: state.grpcLoadedBeforeSetup,
    grpcLoadedAtRegister: state.grpcLoadedAtRegister,
    sameGrpcCopy: exporterGrpc === require.resolve('@grpc/grpc-js'),
    grpcPath: path.relative(process.cwd(), require.resolve('@grpc/grpc-js')),
    exportCode: result.code,
    exportError: result.error ? String(result.error.message ?? result.error).split('\n')[0] : null,
    spans: summarize(state.memory),
  }));
  process.exit(0);
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
