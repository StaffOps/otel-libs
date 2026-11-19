'use strict';
// redis.test.cjs fixture (task A39): redisInstrumentation() passed to setup()
// with OTLP over gRPC (an unreachable endpoint), the order of the demo apps.
// One HTTP request whose handler runs a Redis SET (REDIS_FAKE_URL) and a gRPC
// call; prints the finished spans. Before A39 the core http and gRPC
// instrumentations recorded nothing here.
for (const k of Object.keys(process.env)) if (k.startsWith('OTEL_') && k !== 'OTEL_SERVICE_NAME' && k !== 'OTEL_RESOURCE_ATTRIBUTES' && k !== 'OTEL_HELPER_METRICS_PORT') delete process.env[k];
process.env.OTEL_EXPORTER_OTLP_ENDPOINT = 'http://127.0.0.1:1';
process.env.OTEL_EXPORTER_OTLP_PROTOCOL = 'grpc';
process.env.OTEL_METRICS_EXPORTER = 'none';
process.env.OTEL_LOGS_EXPORTER = 'none';
const { InMemorySpanExporter, SimpleSpanProcessor } = require('@opentelemetry/sdk-trace');
require('@staffops/otel-helper/testing').setDiagnosticSink(() => {});
const helper = require('@staffops/otel-helper');
const { redisInstrumentation } = require('@staffops/otel-helper-redis');

const exporter = new InMemorySpanExporter();
helper.setup({ instrumentations: [redisInstrumentation({ redis: true, ioredis: false })], spanProcessors: [new SimpleSpanProcessor({ exporter })] });

const http = require('node:http');
const grpc = require('@grpc/grpc-js');
const redis = require('redis');

const identity = (b) => b;
const method = { path: '/a39.v1.Orders/Place', requestStream: false, responseStream: false, requestSerialize: identity, requestDeserialize: identity, responseSerialize: identity, responseDeserialize: identity };

(async () => {
  const grpcServer = new grpc.Server();
  grpcServer.addService({ Place: method }, { Place: (_call, cb) => cb(null, Buffer.alloc(0)) });
  const grpcPort = await new Promise((resolve, reject) => grpcServer.bindAsync('127.0.0.1:0', grpc.ServerCredentials.createInsecure(), (err, p) => (err ? reject(err) : resolve(p))));
  const client = new grpc.Client(`127.0.0.1:${grpcPort}`, grpc.credentials.createInsecure());
  const c = redis.createClient({ url: process.env.REDIS_FAKE_URL });
  c.on('error', () => undefined);
  await c.connect();
  const server = http.createServer((_req, res) => {
    c.set('k', 'v')
      .then(() => new Promise((resolve, reject) => client.makeUnaryRequest(method.path, identity, identity, Buffer.alloc(0), (err) => (err ? reject(err) : resolve()))))
      .then(() => res.end('ok'), () => {
        res.statusCode = 500;
        res.end();
      });
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  await new Promise((resolve, reject) => {
    http.get(`http://127.0.0.1:${server.address().port}/orders`, (res) => {
      res.resume();
      res.on('end', resolve);
    }).on('error', reject);
  });
  await c.quit();
  client.close();
  grpcServer.forceShutdown();
  await new Promise((resolve) => server.close(resolve));
  const spans = exporter.getFinishedSpans().map((s) => ({ name: s.name, kind: s.kind, scope: s.instrumentationScope.name, traceId: s.spanContext().traceId }));
  console.log(JSON.stringify({ spans }));
  process.exit(0);
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
